"""再生・録音で共用する音声処理（PCM変換・スペクトル計算・ライブ再生用バッファ）"""

import math
import queue
import threading


# PCMはs16・ステレオ・インターリーブのバイト列で扱う（1フレーム = 2ch x 2バイト）
_PCM_BYTES_PER_FRAME = 4


def _frame_to_pcm(frame):
    """リサンプル済みのPyAVフレーム(s16, stereo)をPCMバイト列に変換する。

    planeのバッファにはアライメント用の余白が付く場合があるため、
    実サンプル数ぶんだけ切り出す。
    """
    return bytes(frame.planes[0])[:frame.samples * _PCM_BYTES_PER_FRAME]


_fft_cache = {}


def _fft_tables(n):
    """長さn(2のべき乗)のFFT用にビット反転順とひねり係数をキャッシュして返す"""
    tables = _fft_cache.get(n)
    if tables is None:
        bits = n.bit_length() - 1
        rev = [int(format(i, f"0{bits}b")[::-1], 2) for i in range(n)]
        twiddles = [complex(math.cos(-2 * math.pi * k / n), math.sin(-2 * math.pi * k / n))
                    for k in range(n // 2)]
        window = [0.5 - 0.5 * math.cos(2 * math.pi * k / (n - 1)) for k in range(n)]
        tables = (rev, twiddles, window)
        _fft_cache[n] = tables
    return tables


def _spectrum_magnitudes(samples):
    """実数列(長さは2のべき乗)にハン窓をかけた振幅スペクトル(n//2+1点)を返す。

    numpyに依存しないよう、反復型の基数2 FFTで計算する。
    """
    n = len(samples)
    rev, twiddles, window = _fft_tables(n)
    data = [complex(samples[r] * window[r]) for r in rev]
    size = 2
    while size <= n:
        half = size // 2
        step = n // size
        for start in range(0, n, size):
            for k in range(half):
                i = start + k
                j = i + half
                t = twiddles[k * step] * data[j]
                u = data[i]
                data[i] = u + t
                data[j] = u - t
        size *= 2
    return [abs(c) for c in data[:n // 2 + 1]]


class _LiveAudioBuffer:
    """ライブ再生用の音声チャンク(PCM)を貯めておく簡易バッファ。

    取得・デコードを行うfetchスレッドと、実際に音声を鳴らすoutputスレッドの
    間に挟むことで、ネットワークの遅延・瞬断がそのまま音切れに直結しないよう
    プリバッファ（再生開始前に一定秒数ためておく）を実現する。
    """

    def __init__(self):
        self._queue = queue.Queue()
        self._buffered_samples = 0
        self._lock = threading.Lock()
        self.sample_rate = None
        self._finished = False

    def push(self, pcm):
        """PCMチャンク(s16ステレオのバイト列)をバッファに追加する"""
        with self._lock:
            self._buffered_samples += len(pcm) // _PCM_BYTES_PER_FRAME
        self._queue.put(pcm)

    def mark_finished(self):
        """これ以上データが来ないことを示す（fetch側の終了時に呼ぶ）"""
        with self._lock:
            self._finished = True
        self._queue.put(None)

    def is_finished(self):
        with self._lock:
            return self._finished

    def buffered_seconds(self):
        """現在バッファに貯まっている音声の長さ（秒）"""
        with self._lock:
            if not self.sample_rate:
                return 0.0
            return self._buffered_samples / self.sample_rate

    def pop(self, timeout=1.0):
        """PCMチャンクを1つ取り出す。終了通知の場合は None を返す。

        Raises:
            queue.Empty: timeout秒以内にデータが来なかった場合
        """
        pcm = self._queue.get(timeout=timeout)
        if pcm is not None:
            with self._lock:
                self._buffered_samples -= len(pcm) // _PCM_BYTES_PER_FRAME
        return pcm
