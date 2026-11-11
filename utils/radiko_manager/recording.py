"""ライブ録音・タイムフリーダウンロードの実行と管理"""

import io
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime

from utils.radiko_manager.audio import _frame_to_pcm

logger = logging.getLogger(__name__)


class RecordingMixin:
    # 選択可能な録音ファイル形式
    RECORDING_FORMATS = ("aac", "mp3", "m4a")
    MP3_BITRATE_CHOICES = (128, 192, 256, 320)

    # 選択可能な録音ファイル名の形式。{station}/{title}/{date}(YYYYMMDD)/
    # {time_full}(HHMMSS)/{time_short}(HHMM) が使えるテンプレート文字列。
    # {title} は番組名が不明な場合（手動録音タブでの録音等）は局名で代用する。
    RECORDING_FILENAME_PATTERNS = {
        'station_datetime': '{station}_{date}_{time_full}',
        'datetime_title': '{date}_{time_short}_{title}',
        'date_title': '{date}_{title}',
        'title_datetime': '{title}_{date}_{time_short}',
        'title_date': '{title}_{date}',
    }
    DEFAULT_FILENAME_PATTERN = 'station_datetime'

    def _sanitize_filename(self, name):
        """ファイル名に使えない文字を置換する"""
        return re.sub(r'[\\/:*?"<>|]', "_", name)

    def _build_recording_filename(self, filename_pattern, station, title, dt, ext):
        """設定された形式に従って録音ファイル名を組み立てる"""
        template = self.RECORDING_FILENAME_PATTERNS.get(
            filename_pattern, self.RECORDING_FILENAME_PATTERNS[self.DEFAULT_FILENAME_PATTERN]
        )
        safe_station = self._sanitize_filename(station)
        safe_title = self._sanitize_filename(title) if title else safe_station
        values = {
            'station': safe_station,
            'title': safe_title,
            'date': dt.strftime("%Y%m%d"),
            'time_full': dt.strftime("%H%M%S"),
            'time_short': dt.strftime("%H%M"),
        }
        return f"{template.format(**values)}.{ext}"

    def _unique_output_path(self, path):
        """同名ファイルが既に存在する場合、末尾に連番を振って重複を避ける

        （時刻を含まない/分単位までのファイル名形式では、同じ局・番組・日の
        録音が複数回行われると同名になり得るため）
        """
        if not path.exists():
            return path
        counter = 1
        while True:
            candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
            if not candidate.exists():
                return candidate
            counter += 1

    def build_recording_filename(self, filename_pattern, station, title, dt, ext):
        """_build_recording_filenameの公開ラッパー（radiko以外の取得元でも同じ命名規則を使うため）"""
        return self._build_recording_filename(filename_pattern, station, title, dt, ext)

    def unique_output_path(self, path):
        """_unique_output_pathの公開ラッパー"""
        return self._unique_output_path(path)

    def start_recording(self, station, duration, file_format="aac", mp3_bitrate=192, on_complete=None,
                         title=None, filename_pattern=None, metadata=None, reservation_id=None):
        """ラジコストリームの録音を開始する（局が異なれば複数を同時に録音できる）

        Args:
            station (str): ステーション名
            duration (int または float): 録音時間（分）
            file_format (str): "aac"（再エンコードなしでそのまま保存）、"m4a"（再エンコードなし
                でMP4コンテナにストリームコピー。Windows Explorer等でもタグが読める）、
                または "mp3"（re-encode）
            mp3_bitrate (int): file_format="mp3" のときのビットレート(kbps)
            on_complete (callable または None): 録音スレッド終了時に
                on_complete(had_data: bool, output_path: Path, error_message: str または None)
                の形で呼び出されるコールバック。had_data は実際に音声データを
                1バイトでも取得できたか（Falseなら出力ファイルは実質空）。
                バックグラウンドスレッドから呼ばれるため、GUI操作を行う場合は
                呼び出し側でメインスレッドへのディスパッチ（例: Tkinterのafter）が必要。
            title (str または None): ファイル名に使う番組名（未指定/空なら局名で代用）
            filename_pattern (str または None): RECORDING_FILENAME_PATTERNS のキー。
                未指定なら DEFAULT_FILENAME_PATTERN を使う
            metadata (dict または None): 録音完了後にファイルへ埋め込むタグ情報。
                'title'（番組名）, 'artist'（出演者）, 'album'（局名）,
                'comment'（番組概要）, 'date'（放送日 "YYYY-MM-DD"）のいずれかを
                含む辞書。未指定のキーは書き込まない
            reservation_id (str または None): 予約録音から呼ばれた場合の予約ID。
                is_recording_active(station, reservation_id=...) で、どの予約が
                実際に録音中かを区別するために使う。手動録音の場合はNone

        Returns:
            (bool, str または None): (録音開始に成功したか, 保存先ファイルパス文字列)
        """
        if self.is_recording_active(station):
            logger.warning(f"録音開始スキップ: {station} は既に録音中です")
            return False, None

        station_id = self.station_mapping.get(station)
        if not station_id:
            logger.warning(f"録音開始失敗: 局が見つかりません ({station})")
            return False, None

        if file_format not in self.RECORDING_FORMATS:
            logger.warning(f"録音開始失敗: 未対応のフォーマットです ({file_format})")
            return False, None

        try:
            auth_token = self._ensure_authenticated()
            playlist_base_url = self._get_live_playlist_url(station_id)
        except Exception:
            logger.exception(f"Error preparing recording for {station}")
            return False, None

        if not playlist_base_url:
            return False, None

        lsid = uuid.uuid4().hex
        playlist_url = f"{playlist_base_url}?station_id={station_id}&l=15&lsid={lsid}&type=b"

        # "aac"形式はセグメントの生バイト列（ADTS AACストリーム）をそのまま連結するため
        # MP4コンテナではなく .aac 拡張子にする
        ext = {"aac": "aac", "m4a": "m4a"}.get(file_format, "mp3")
        filename = self._build_recording_filename(
            filename_pattern or self.DEFAULT_FILENAME_PATTERN, station, title, datetime.now(), ext
        )
        output_path = self._unique_output_path(self.output_dir / filename)

        stop_event = threading.Event()
        headers = {"X-Radiko-AuthToken": auth_token}
        segment_iter = self._iter_live_segments(playlist_url, headers, stop_event)
        thread = threading.Thread(
            target=self._recording_worker,
            args=(
                segment_iter, stop_event,
                output_path, file_format, mp3_bitrate, duration * 60, station, on_complete,
                metadata,
            ),
            daemon=True,
        )
        with self._recordings_lock:
            self._recordings[station] = {
                'thread': thread, 'stop_event': stop_event, 'output_path': output_path,
                'reservation_id': reservation_id,
            }
        thread.start()
        logger.info(f"録音開始: {station} ({round(duration, 1):g}分, {file_format}) -> {output_path}")
        return True, str(output_path)

    def is_download_active(self, station, ft):
        """指定局・指定開始時刻(ft)のタイムフリーダウンロードが実行中かどうか"""
        key = f"{station}|{ft}"
        with self._downloads_lock:
            entry = self._downloads.get(key)
            return bool(entry and entry['thread'].is_alive())

    def has_active_downloads(self):
        """タイムフリーのダウンロードが1件でも実行中かどうか"""
        with self._downloads_lock:
            return any(e['thread'].is_alive() for e in self._downloads.values())

    def is_download_limit_reached(self):
        """タイムフリーの同時ダウンロード数が max_concurrent_timefree_downloads の
        上限に達しているかどうか"""
        with self._downloads_lock:
            active = sum(1 for e in self._downloads.values() if e['thread'].is_alive())
        return active >= self.max_concurrent_timefree_downloads

    def stop_download(self, station, ft):
        """指定局・指定開始時刻(ft)のタイムフリーダウンロードを停止する

        stop_recording と同様、それまでにダウンロード済みの内容はファイルに残る
        （_recording_worker の had_data 判定・保存処理を録音と共有しているため）。
        """
        key = f"{station}|{ft}"
        with self._downloads_lock:
            entry = self._downloads.get(key)

        if entry is None:
            return

        logger.info(f"タイムフリー取得停止要求: {station} ({ft})")
        entry['stop_event'].set()
        if entry['thread'].is_alive():
            entry['thread'].join(timeout=5)

        with self._downloads_lock:
            self._downloads.pop(key, None)

    def stop_all_downloads(self):
        """進行中の全てのタイムフリーダウンロードを停止する（アプリ終了時等）

        stop_recording(station=None) と同様、それまでにダウンロード済みの内容は
        ファイルに残る。
        """
        with self._downloads_lock:
            targets = dict(self._downloads)

        if targets:
            logger.info(f"タイムフリー取得停止要求（全件）: {len(targets)}件")

        for entry in targets.values():
            entry['stop_event'].set()
        for entry in targets.values():
            if entry['thread'].is_alive():
                entry['thread'].join(timeout=5)

        with self._downloads_lock:
            for key in targets:
                self._downloads.pop(key, None)

    def start_timefree_download(self, station, ft, to, file_format="aac", mp3_bitrate=192,
                                 on_complete=None, on_progress=None, title=None,
                                 filename_pattern=None, metadata=None):
        """radikoタイムフリーで指定区間の番組をダウンロードする

        Args:
            station (str): ステーション名
            ft (str): 取得開始時刻 "YYYYMMDDHHMMSS"
            to (str): 取得終了時刻 "YYYYMMDDHHMMSS"
            on_progress (callable または None): on_progress(done: int, total: int) の形で
                セグメント取得のたびに呼ばれる進捗コールバック。バックグラウンドスレッドから
                呼ばれるため、GUI操作を行う場合は呼び出し側でメインスレッドへのディスパッチが必要
            その他の引数は start_recording と同じ意味。

        ライブ録音（self._recordings、局名をキーに多重実行を防止）とは別の
        名前空間（self._downloads、"局名|ft"をキー）で多重実行を防止するため、
        同じ局のライブ録音中でもタイムフリーのダウンロードは独立して実行できる。

        Returns:
            (bool, str または None): (ダウンロード開始に成功したか, 保存先ファイルパス文字列)
        """
        if self.is_download_active(station, ft):
            logger.warning(f"タイムフリー取得スキップ: {station} ({ft}) は取得中です")
            return False, None

        if self.is_download_limit_reached():
            logger.warning(
                f"タイムフリー取得スキップ: 同時ダウンロード数が上限"
                f"（{self.max_concurrent_timefree_downloads}）に達しています"
            )
            return False, None

        station_id = self.station_mapping.get(station)
        if not station_id:
            logger.warning(f"タイムフリー取得失敗: 局が見つかりません ({station})")
            return False, None

        if file_format not in self.RECORDING_FORMATS:
            logger.warning(f"タイムフリー取得失敗: 未対応のフォーマットです ({file_format})")
            return False, None

        try:
            auth_token = self._ensure_authenticated()
            playlist_base_url = self._get_timefree_playlist_url(station_id)
        except Exception:
            logger.exception(f"Error preparing timefree download for {station}")
            return False, None

        if not playlist_base_url:
            return False, None

        # start_at/end_at と ft/to の両方を送る必要がある（streamlinkのradikoプラグイン実装が
        # 典拠。ft/toだけだとCDNのセッションに時間範囲が紐付かず、常に現在時刻付近の
        # ライブ相当の内容が返ってきてしまうことを実機で確認した）。
        # l は medialist が1回のリクエストで返す秒数の上限らしいが、大きい値（例: 600）を
        # 送ると400 Bad Requestになることを実機で確認したため、動作確認済みの15固定とする。
        # type=c（チャンク取得）+ seek での高速化も試したが実機で400になったため type=b のまま。
        #
        # ただし別セッション（別lsid）であれば、任意のft地点から待ち時間なく即座に
        # 取得を開始できることを実機で確認済みなので、区間をtimefree_parallel_splits個に
        # 分割し並列取得することで高速化する（_iter_timefree_segments_parallel）。
        ft_dt = datetime.strptime(ft, "%Y%m%d%H%M%S")
        to_dt = datetime.strptime(to, "%Y%m%d%H%M%S")
        # 極端に短い区間（分割後が30秒未満になる）では分割の意味が薄いため縮退させる
        num_splits = max(1, min(
            self.timefree_parallel_splits,
            int((to_dt - ft_dt).total_seconds() // 30) or 1
        ))

        # ファイル名の日時には、ダウンロードした時刻ではなく放送開始時刻(ft)を使う
        # （タイムフリーは後から取得するものなので、いつ聴いたかではなく、いつ放送された
        # 番組かがファイル名から分かるようにする）
        ext = {"aac": "aac", "m4a": "m4a"}.get(file_format, "mp3")
        filename = self._build_recording_filename(
            filename_pattern or self.DEFAULT_FILENAME_PATTERN, station, title, ft_dt, ext
        )
        output_path = self._unique_output_path(self.output_dir / filename)

        stop_event = threading.Event()
        segment_iter = self._iter_timefree_segments_parallel(
            playlist_base_url, station_id, auth_token, ft_dt, to_dt, stop_event,
            on_progress=on_progress, num_splits=num_splits
        )
        registry_key = f"{station}|{ft}"
        thread = threading.Thread(
            target=self._recording_worker,
            args=(
                segment_iter, stop_event,
                output_path, file_format, mp3_bitrate, None, station, on_complete,
                metadata,
            ),
            kwargs={
                'registry': self._downloads,
                'registry_lock': self._downloads_lock,
                'registry_key': registry_key,
            },
            daemon=True,
        )
        with self._downloads_lock:
            self._downloads[registry_key] = {
                'thread': thread, 'stop_event': stop_event, 'output_path': output_path
            }
        thread.start()
        logger.info(f"タイムフリー取得開始: {station} ({ft}-{to}, {file_format}) -> {output_path}")
        return True, str(output_path)

    def _recording_worker(self, segment_iter, stop_event,
                           output_path, file_format, mp3_bitrate, duration_seconds, station,
                           on_complete=None, metadata=None, registry=None, registry_lock=None,
                           registry_key=None):
        """バックグラウンドスレッドで配信をファイルに保存する（ライブ録音・タイムフリー共通）

        file_format="aac" の場合は取得したAAC(ADTS)の生バイト列をそのまま連結して
        書き出す（再エンコードなし・音質劣化なし）。
        file_format="m4a" の場合はデコードせず、AACパケットをそのままMP4コンテナに
        ストリームコピーする（再エンコードなし・音質劣化なし。Windows Explorer等の
        タグ表示に対応するためのコンテナ変換のみ）。
        file_format="mp3" の場合はデコードしてlibmp3lameで再エンコードする。
        いずれの場合もデコードした音声からグラフィックイコライザー用レベルは更新する。

        segment_iter はセグメントの生バイト列を順に返すイテレーター
        （ライブなら _iter_live_segments、タイムフリーなら _iter_timefree_segments）。
        duration_seconds が指定されていればその時間経過で打ち切る（ライブ録音用）。
        None ならセグメントを取得し尽くすまで（タイムフリーは配信自体が有限のため
        イテレーターが自然に終わる）。

        registry/registry_key は多重実行防止用の辞書とそのキー
        （ライブ録音は self._recordings、タイムフリーは self._downloads）。
        未指定なら self._recordings と station を使う。

        認証切れ・通信エラー等でセグメントを1つも取得できなかった場合、
        例外はこの関数内で捕捉されるだけで呼び出し元には伝わらないため、
        出力ファイルが実質空のまま「録音成功」に見えてしまう。on_complete を
        通じて実際にデータを取得できたか（had_data）を呼び出し元に返すことで、
        これを検知できるようにする。
        """
        import av

        if registry is None:
            registry = self._recordings
        if registry_lock is None:
            registry_lock = self._recordings_lock
        if registry_key is None:
            registry_key = station

        start_time = time.monotonic()
        raw_file = None
        output_container = None
        output_stream = None
        resampler = None
        pts_counter = 0
        had_data = False
        error_message = None

        try:
            if file_format == "aac":
                raw_file = open(output_path, "wb")
            else:
                output_container = av.open(str(output_path), mode="w")

            for segment_bytes in segment_iter:
                container = av.open(io.BytesIO(segment_bytes), format="aac")
                try:
                    audio_stream = container.streams.audio[0]

                    if file_format == "aac":
                        raw_file.write(segment_bytes)
                        had_data = True
                        if resampler is None:
                            resampler = av.AudioResampler(
                                format="s16", layout="stereo", rate=audio_stream.rate
                            )
                        for frame in container.decode(audio_stream):
                            for resampled in resampler.resample(frame):
                                self._update_levels(_frame_to_pcm(resampled), audio_stream.rate)
                    elif file_format == "m4a":
                        if output_stream is None:
                            output_stream = output_container.add_stream_from_template(audio_stream)
                        if resampler is None:
                            resampler = av.AudioResampler(
                                format="s16", layout="stereo", rate=audio_stream.rate
                            )
                        for packet in container.demux(audio_stream):
                            if packet.dts is None:
                                continue
                            duration = packet.duration or 0
                            for frame in packet.decode():
                                for resampled in resampler.resample(frame):
                                    self._update_levels(_frame_to_pcm(resampled), audio_stream.rate)
                            had_data = True
                            packet.pts = pts_counter
                            packet.dts = pts_counter
                            packet.stream = output_stream
                            output_container.mux(packet)
                            pts_counter += duration
                    else:
                        if resampler is None:
                            resampler = av.AudioResampler(
                                format="s16", layout="stereo", rate=audio_stream.rate
                            )
                            output_stream = output_container.add_stream(
                                "libmp3lame", rate=audio_stream.rate
                            )
                            output_stream.bit_rate = mp3_bitrate * 1000
                            output_stream.layout = "stereo"

                        for frame in container.decode(audio_stream):
                            for resampled in resampler.resample(frame):
                                had_data = True
                                self._update_levels(_frame_to_pcm(resampled), audio_stream.rate)
                                resampled.pts = pts_counter
                                pts_counter += resampled.samples
                                for packet in output_stream.encode(resampled):
                                    output_container.mux(packet)
                finally:
                    container.close()

                if duration_seconds and (time.monotonic() - start_time) >= duration_seconds:
                    stop_event.set()
                    break
        except Exception as e:
            error_message = str(e)
            logger.exception(f"Error during recording: {station}")
        finally:
            if output_container is not None:
                try:
                    if file_format == "mp3" and output_stream is not None:
                        for packet in output_stream.encode(None):
                            output_container.mux(packet)
                finally:
                    output_container.close()
            if raw_file is not None:
                raw_file.close()
            if had_data and metadata:
                try:
                    self._write_metadata_tags(output_path, file_format, metadata)
                except Exception:
                    logger.exception("Error writing metadata tags")
            elif not had_data:
                # データを1バイトも取得できなかった場合、空の出力ファイルを残さない
                try:
                    if os.path.exists(output_path):
                        os.remove(output_path)
                except OSError:
                    logger.exception("Error removing empty recording file")
            with self._levels_lock:
                self._levels = [0.0] * self.EQ_NUM_BANDS
                self._lr_levels = [0.0, 0.0]
            with registry_lock:
                # stop_recording の待ち時間を過ぎてから終わった場合、同じ局の次の録音が
                # 既に登録されていることがある。それを消さないよう自分の分だけ取り除く
                entry = registry.get(registry_key)
                if entry is not None and entry.get('thread') in (None, threading.current_thread()):
                    registry.pop(registry_key, None)
            if error_message:
                logger.warning(f"録音終了: {station} -> {output_path} (エラー: {error_message})")
            else:
                logger.info(f"録音終了: {station} -> {output_path} (had_data={had_data})")
            if on_complete is not None:
                try:
                    on_complete(had_data, output_path, error_message)
                except Exception:
                    logger.exception("Error in recording on_complete callback")

    def is_recording_active(self, station=None, reservation_id=None):
        """録音中かどうか

        Args:
            station (str または None): 指定した局が録音中かを調べる。
                Noneの場合はいずれかの局が録音中であればTrue
            reservation_id (str または None): 指定した場合、その局で録音中なのが
                この予約IDによるものかどうかまで区別する。局が同じでも別の予約
                （や手動録音）による録音であればFalseを返す
        """
        with self._recordings_lock:
            if station is not None:
                entry = self._recordings.get(station)
                if not entry or not entry['thread'].is_alive():
                    return False
                if reservation_id is not None:
                    return entry.get('reservation_id') == reservation_id
                return True
            return any(entry['thread'].is_alive() for entry in self._recordings.values())

    def recording_reservation_id(self, station):
        """指定した局で録音中の予約ID（録音中でない、または手動録音なら None）"""
        with self._recordings_lock:
            entry = self._recordings.get(station)
            if not entry or not entry['thread'].is_alive():
                return None
            return entry.get('reservation_id')

    def stop_recording(self, station=None):
        """録音を停止する

        Args:
            station (str または None): 指定した局の録音のみ停止する。
                Noneの場合は現在進行中の全ての録音を停止する（アプリ終了時等）
        """
        with self._recordings_lock:
            targets = (
                {station: self._recordings[station]} if station is not None and station in self._recordings
                else dict(self._recordings) if station is None
                else {}
            )

        if targets:
            logger.info(f"録音停止要求: {', '.join(targets.keys())}")

        for entry in targets.values():
            entry['stop_event'].set()
        for entry in targets.values():
            if entry['thread'].is_alive():
                entry['thread'].join(timeout=5)

        with self._recordings_lock:
            for st in targets:
                self._recordings.pop(st, None)
