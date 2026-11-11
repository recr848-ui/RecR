"""ライブ再生とレベルメーター用の音量計算"""

import io
import logging
import math
import threading
import time
from array import array
import uuid

from utils.radiko_manager.audio import _LiveAudioBuffer
from utils.radiko_manager.audio import _PCM_BYTES_PER_FRAME
from utils.radiko_manager.audio import _frame_to_pcm
from utils.radiko_manager.audio import _spectrum_magnitudes

logger = logging.getLogger(__name__)


class PlaybackMixin:
    # 再生開始前にためておく音声バッファ量（秒）。ネットワークの瞬断・遅延を
    # 吸収し、音切れ（アンダーラン）を減らすためのプリバッファ。
    PLAYBACK_PREBUFFER_SECONDS = 5.0

    def play_live(self, station_name):
        """指定局のライブ配信を再生する（PyAVでデコードし、sounddeviceで出力）。

        ffmpeg/ffplay本体のインストールは不要。PyAVがffmpegのデコード用DLLを
        wheelに同梱しているため、それを直接ライブラリとして呼び出す方式。

        取得・デコード（fetchスレッド）と音声出力（outputスレッド）を分離し、
        間に PLAYBACK_PREBUFFER_SECONDS 秒分の音声バッファを挟むことで、
        ネットワークの遅延・瞬断が直接音切れにつながらないようにしている。

        Returns:
            bool: 再生開始に成功した場合True
        """
        station_id = self.station_mapping.get(station_name)
        if not station_id:
            return False

        self.stop_playback()

        try:
            auth_token = self._ensure_authenticated()
            playlist_base_url = self._get_live_playlist_url(station_id)
        except Exception:
            logger.exception(f"Error preparing live stream for {station_name}")
            return False

        if not playlist_base_url:
            return False

        lsid = uuid.uuid4().hex
        playlist_url = f"{playlist_base_url}?station_id={station_id}&l=15&lsid={lsid}&type=b"

        self._playback_stop_event = threading.Event()
        self._playback_buffer = _LiveAudioBuffer()
        fetch_thread = threading.Thread(
            target=self._playback_fetch_worker,
            args=(playlist_url, auth_token, self._playback_stop_event, self._playback_buffer),
            daemon=True,
        )
        output_thread = threading.Thread(
            target=self._playback_output_worker,
            args=(self._playback_stop_event, self._playback_buffer),
            daemon=True,
        )
        self._playback_threads = [fetch_thread, output_thread]
        fetch_thread.start()
        output_thread.start()
        return True

    def _playback_fetch_worker(self, playlist_url, auth_token, stop_event, buffer):
        """バックグラウンドスレッドでHLSストリームを取得・デコードし、PCMを
        buffer(_LiveAudioBuffer)に貯めていく（実際の音声出力は行わない）。

        セグメントの取得は requests で行い（PyAV自身にHTTP/HLSを直接読ませると
        一部環境でセッション絡みの取得に失敗するため）、取得した生のAAC(ADTS)
        データのデコードのみPyAVに任せる。
        """
        import av

        headers = {"X-Radiko-AuthToken": auth_token}
        resampler = None

        try:
            for segment_bytes in self._iter_live_segments(playlist_url, headers, stop_event):
                container = av.open(io.BytesIO(segment_bytes), format="aac")
                try:
                    audio_stream = container.streams.audio[0]
                    if resampler is None:
                        resampler = av.AudioResampler(
                            format="s16", layout="stereo", rate=audio_stream.rate
                        )
                        buffer.sample_rate = audio_stream.rate

                    for frame in container.decode(audio_stream):
                        for resampled in resampler.resample(frame):
                            if stop_event.is_set():
                                break
                            buffer.push(_frame_to_pcm(resampled))
                finally:
                    container.close()
        except Exception:
            logger.exception("Error fetching live stream")
        finally:
            buffer.mark_finished()

    def _playback_output_worker(self, stop_event, buffer):
        """buffer(_LiveAudioBuffer)からPCMを取り出してsounddeviceで再生するスレッド。

        再生開始前に PLAYBACK_PREBUFFER_SECONDS 秒分たまるまで待つことで、
        ネットワークの遅延・瞬断がそのまま音切れにつながらないようにする。
        """
        import queue as queue_module

        import sounddevice as sd

        stream_handle = None
        try:
            while (
                not stop_event.is_set()
                and not buffer.is_finished()
                and buffer.buffered_seconds() < self.PLAYBACK_PREBUFFER_SECONDS
            ):
                time.sleep(0.05)

            while not stop_event.is_set():
                try:
                    pcm = buffer.pop(timeout=1.0)
                except queue_module.Empty:
                    if buffer.is_finished():
                        break
                    continue
                if pcm is None:
                    break
                if stream_handle is None:
                    stream_handle = sd.RawOutputStream(
                        samplerate=buffer.sample_rate, channels=2, dtype="int16"
                    )
                    stream_handle.start()
                stream_handle.write(pcm)
                # 実際に音として出るタイミングでレベルを更新する（sounddeviceへの
                # 書き込みが自然にブロッキングし実時間でペーシングされるため、
                # ここで更新することでグラフィックイコライザーの動きが滑らかになる）
                self._update_levels(pcm, buffer.sample_rate)
        except Exception:
            logger.exception("Error during live playback output")
        finally:
            # 出力側が（音声デバイスの切断等で）異常終了した場合に取得側だけが動き続けると、
            # 誰にも消費されないPCMがbufferに溜まり続けてメモリを食い潰す
            # （実測で音声1時間あたり約800MB）。出力が終わったら取得側も必ず止める
            stop_event.set()
            if stream_handle is not None:
                try:
                    stream_handle.stop()
                    stream_handle.close()
                except Exception:
                    pass
            with self._levels_lock:
                self._levels = [0.0] * self.EQ_NUM_BANDS
                self._lr_levels = [0.0, 0.0]

    # グラフィックイコライザーの描画間隔(50ms)より細かく計算しても表示されないため、
    # レベル計算はこの間隔に間引く（純PythonのFFTでCPUを食いすぎないようにする）
    LEVELS_UPDATE_INTERVAL = 0.04

    def _update_levels(self, pcm, sample_rate):
        """デコード済みPCM(s16ステレオのバイト列)からグラフィックイコライザー用の
        周波数バンド別レベル(0.0〜1.0)を計算し保持する
        """
        now = time.monotonic()
        if now - self._levels_updated_at < self.LEVELS_UPDATE_INTERVAL:
            return

        samples = array("h")
        samples.frombytes(pcm[:len(pcm) - len(pcm) % _PCM_BYTES_PER_FRAME])
        left = samples[0::2]
        right = samples[1::2]
        frames = len(left)
        if frames < 2:
            return
        self._levels_updated_at = now

        lr_levels = []
        for channel in (left, right):
            peak = max(max(channel), -min(channel)) / 32768.0
            db = 20 * math.log10(peak + 1e-6)
            # ピークメーターらしく0dBFS（フルスケール）を基準に、
            # -24dB〜0dBFSを0.0〜1.0へ正規化する
            lr_levels.append(min(max((db + 24) / 24, 0.0), 1.0))

        # FFTは2のべき乗長で行うため、収まる最大の長さに切り詰める
        n = 1 << (frames.bit_length() - 1)
        mono = [(left[i] + right[i]) / 65536.0 for i in range(n)]
        spectrum = _spectrum_magnitudes(mono)
        bin_width = sample_rate / n

        max_freq = min(16000, sample_rate / 2 - 1)
        log_lo = math.log10(60)
        log_step = (math.log10(max_freq) - log_lo) / self.EQ_NUM_BANDS
        band_edges = [10 ** (log_lo + log_step * i) for i in range(self.EQ_NUM_BANDS + 1)]

        levels = []
        for i in range(self.EQ_NUM_BANDS):
            lo = math.ceil(band_edges[i] / bin_width)
            hi = math.ceil(band_edges[i + 1] / bin_width)
            band = spectrum[lo:hi]
            magnitude = sum(band) / len(band) if band else 0.0
            db = 20 * math.log10(magnitude + 1e-6)
            # 実測でおおよそ -20dB〜+35dBに収まるため、それを 0.0〜1.0 に正規化
            # （絶対的な音量ではなく見た目のためのスケーリング）
            level = (db + 20) / 55
            levels.append(min(max(level, 0.0), 1.0))

        with self._levels_lock:
            self._levels = levels
            self._lr_levels = lr_levels

    def get_levels(self):
        """グラフィックイコライザー表示用の最新バンドレベル(0.0〜1.0)を取得"""
        with self._levels_lock:
            return list(self._levels)

    def get_lr_levels(self):
        """ピークメーター表示用の最新L/Rチャンネルレベル(0.0〜1.0、[L, R])を取得"""
        with self._levels_lock:
            return list(self._lr_levels)

    def is_playing(self):
        """ライブ再生中かどうか"""
        return any(t.is_alive() for t in self._playback_threads)

    def stop_playback(self):
        """再生中のライブストリームを停止する"""
        if self._playback_stop_event is not None:
            self._playback_stop_event.set()
        for t in self._playback_threads:
            if t.is_alive():
                t.join(timeout=5)
        self._playback_threads = []
        self._playback_stop_event = None
        self._playback_buffer = None
