"""CMカット用の音声フィンガープリント（指紋）取得

予約が入っている民放局の放送を、radikoのタイムフリーで1時間ずつ取得し、
音声フィンガープリントを作って保存する（音声そのものは既定では保存しない）。
将来、複数日の指紋を照らし合わせて「何度も流れる区間＝CM」を見つけるための材料集め。

取得の単位は「局 × 放送日 × 1時間」（チャンク）。radikoの放送日は5時〜29時なので、
時刻は放送日基準の時（11〜26など。24以上は翌日の0時以降）で扱う。
タイムフリーで取れる（放送後7日以内）範囲のうち、まだ取っていないチャンクを
古い順に取るため、初回は取れる分すべて、2回目以降は前回からの差分（通常1日分）になる。

指紋の作り方（FP_VERSION=1）:
  音声を8kHzモノラルに変換し、1024サンプル（128ms）の窓を256サンプル（32ms）ずつ
  ずらしながらFFTする。300〜2000Hzを対数間隔の33帯域に分け、隣り合う帯域の
  エネルギー差が前のフレームより増えたかどうかを1ビットとして、1フレーム32ビットの
  指紋にする（Haitsma & Kalker の方式）。あわせてフレームごとの音量（dBFS）も保存する
  （後で無音・音の切り替わりから境目を調整するのに使う）。
  numpyを使わない（exeのサイズを増やさない）ため、FFTは radiko_manager の純Python実装を使う。

ログは解析に回せるよう、アプリ本体のログとは別に出す。
  fingerprint.log         … 文字のログ（DEBUG以上）
  fingerprint_stats.jsonl … チャンク1件ごとの集計（1行1JSON）
"""
import io
import json
import logging
import math
import struct
import threading
import time
from array import array
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from utils.app_logger import build_queued_handlers
from utils.json_store import read_json, write_json_atomic
from utils.radiko_manager import _spectrum_magnitudes

FP_VERSION = 1
SAMPLE_RATE = 8000
FRAME_SIZE = 1024
HOP_SIZE = 256
BAND_COUNT = 33
BAND_LOW_HZ = 300.0
BAND_HIGH_HZ = 2000.0
# この音量（dBFS）未満のフレームを無音とみなす（ログの集計用）
SILENCE_DBFS = -50.0

# .fpファイルの先頭: マジック, 指紋バージョン, 予備, フレーム数, サンプリング周波数, ホップ長
_FP_HEADER = struct.Struct("<4sHHIII")
_FP_MAGIC = b"RFP1"

DEFAULT_SETTINGS = {
    'cm_fp_enabled': False,
    # 毎日の自動取得を始める時刻。radikoの放送日は5時で切り替わるため、それより後にする
    'cm_fp_time': "06:00",
    # 取得する時間帯（放送日基準の時。27 = 翌日3時）
    'cm_fp_window_start': 11,
    'cm_fp_window_end': 27,
    'cm_fp_retention_days': 28,
    # 開発・調整用: 取得した音声（AAC）も保存する（1局1時間あたり約20MB）
    'cm_fp_keep_audio': False,
}

# radikoのタイムフリーは放送後7日間。期限ぎりぎりのチャンクは取得中に切れるため余裕を見る
TIMEFREE_AVAILABLE_DAYS = 7
EXPIRY_MARGIN = timedelta(hours=1)
# 放送日の取得時間帯が終わってから、この時間が経つまではその日を取りに行かない
# （時間帯の終わりをまたぐ番組が、終わるまでタイムフリーに出てこないため）
DAY_READY_DELAY = timedelta(hours=2)
# 1チャンクの取得を失敗扱いで諦めるまでの試行回数
MAX_ATTEMPTS = 3
# 取れた音声の長さがこの割合未満なら、途中で切れたとみなして失敗扱いにする
MIN_AUDIO_RATIO = 0.9
# 録音中で一時停止している間、再開できるかを確かめる間隔（秒）
PAUSE_POLL_SECONDS = 15
# 失敗したチャンクの直後に、次のチャンクへ進む前に待つ時間（秒）
FAILURE_BACKOFF_SECONDS = 60


def is_nhk_station(station):
    """NHKの局か（NHKはCMがないので対象外）。main.py の _is_nhk_station と同じ判定"""
    return station.startswith("NHK")


def broadcast_hour_start(bday, hour):
    """放送日 bday（date）の hour 時（5〜29）の開始時刻（実際の日時）"""
    return datetime.combine(bday, datetime.min.time()) + timedelta(hours=hour)


def chunk_key(station_id, bday, hour):
    return f"{station_id}|{bday.strftime('%Y%m%d')}|{hour:02d}"


def target_stations(reservations, station_mapping):
    """指紋を取る局: 有効な予約がある局のうち、NHK以外で、局一覧にあるもの

    Returns:
        (list[str], dict[str, str]): (対象の局名（名前順）, 除外した局名 -> 理由)
    """
    targets = set()
    skipped = {}
    for res in reservations:
        if not res.get('enabled', True):
            continue
        station = res.get('station')
        if not station:
            continue
        if is_nhk_station(station):
            skipped[station] = "NHK（CMなし）"
        elif station not in station_mapping:
            skipped[station] = "現在の局一覧にない"
        else:
            targets.add(station)
    return sorted(targets), skipped


def plan_chunks(stations, station_mapping, index, now, window_start, window_end):
    """これから取得するチャンクを、古い（期限が近い）順に返す

    Returns:
        (list[dict], dict): (チャンクのリスト, 取得済み・期限切れ等で除いた件数の内訳)
    """
    oldest_ok = now - timedelta(days=TIMEFREE_AVAILABLE_DAYS) + EXPIRY_MARGIN
    # 放送日は5時始まりなので、今日の放送日 = (now - 5時間) の日付
    today_bday = (now - timedelta(hours=5)).date()
    chunks = []
    counts = {'done': 0, 'failed': 0, 'expired': 0, 'not_ready': 0}
    for station in stations:
        station_id = station_mapping[station]
        for days_back in range(TIMEFREE_AVAILABLE_DAYS + 1, -1, -1):
            bday = today_bday - timedelta(days=days_back)
            day_end = broadcast_hour_start(bday, window_end)
            if day_end + DAY_READY_DELAY > now:
                counts['not_ready'] += window_end - window_start
                continue
            for hour in range(window_start, window_end):
                start_dt = broadcast_hour_start(bday, hour)
                key = chunk_key(station_id, bday, hour)
                entry = index.get(key) or {}
                status = entry.get('status')
                if status == 'done':
                    counts['done'] += 1
                    continue
                if status == 'failed':
                    counts['failed'] += 1
                    continue
                if start_dt < oldest_ok:
                    counts['expired'] += 1
                    continue
                chunks.append({
                    'key': key, 'station': station, 'station_id': station_id,
                    'bday': bday, 'hour': hour,
                    'start_dt': start_dt, 'end_dt': start_dt + timedelta(hours=1),
                    'attempts': entry.get('attempts', 0),
                })
    chunks.sort(key=lambda c: (c['start_dt'], c['station']))
    return chunks, counts


def _band_bins():
    """33帯域それぞれのFFTビン範囲 [lo, hi)"""
    bin_hz = SAMPLE_RATE / FRAME_SIZE
    ratio = (BAND_HIGH_HZ / BAND_LOW_HZ) ** (1.0 / BAND_COUNT)
    bins = []
    for b in range(BAND_COUNT):
        lo_hz = BAND_LOW_HZ * ratio ** b
        hi_hz = BAND_LOW_HZ * ratio ** (b + 1)
        lo = int(round(lo_hz / bin_hz))
        hi = max(lo + 1, int(round(hi_hz / bin_hz)))
        bins.append((lo, hi))
    return bins


class FingerprintBuilder:
    """8kHzモノラルs16のPCMを順に受け取り、フレームごとの指紋と音量を作る"""

    def __init__(self):
        self._bins = _band_bins()
        self._pending = array("h")
        self._prev_energies = None
        self.fingerprints = array("I")
        self.loudness = bytearray()
        self.samples_in = 0

    def feed(self, pcm_bytes):
        samples = array("h")
        samples.frombytes(pcm_bytes)
        self.samples_in += len(samples)
        self._pending.extend(samples)
        pos = 0
        while len(self._pending) - pos >= FRAME_SIZE:
            self._process_frame(self._pending[pos:pos + FRAME_SIZE])
            pos += HOP_SIZE
        if pos:
            del self._pending[:pos]

    def _process_frame(self, frame):
        mags = _spectrum_magnitudes(frame)
        energies = [sum(m * m for m in mags[lo:hi]) for lo, hi in self._bins]
        bits = 0
        prev = self._prev_energies
        if prev is not None:
            for m in range(BAND_COUNT - 1):
                if (energies[m] - energies[m + 1]) - (prev[m] - prev[m + 1]) > 0:
                    bits |= 1 << m
        self._prev_energies = energies
        self.fingerprints.append(bits)

        power = sum(s * s for s in frame) / FRAME_SIZE
        dbfs = 10 * math.log10(power / (32768.0 * 32768.0)) if power > 0 else -100.0
        self.loudness.append(max(0, min(255, int(round(dbfs + 100)))))

    @property
    def frame_count(self):
        return len(self.fingerprints)


def write_fp_file(path, fingerprints, loudness):
    """指紋ファイル(.fp)を書く: ヘッダー + uint32の指紋 × n + uint8の音量 × n"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fps = array("I", fingerprints)
    with open(tmp, "wb") as f:
        f.write(_FP_HEADER.pack(_FP_MAGIC, FP_VERSION, 0, len(fps), SAMPLE_RATE, HOP_SIZE))
        f.write(fps.tobytes())
        f.write(bytes(loudness))
    tmp.replace(path)


def read_fp_file(path):
    """write_fp_file で書いたファイルを読む

    Returns:
        dict: {'version', 'sample_rate', 'hop', 'fingerprints'(array), 'loudness'(bytes)}
    """
    data = Path(path).read_bytes()
    magic, version, _, count, rate, hop = _FP_HEADER.unpack_from(data, 0)
    if magic != _FP_MAGIC:
        raise ValueError(f"指紋ファイルではありません: {path}")
    offset = _FP_HEADER.size
    fps = array("I")
    fps.frombytes(data[offset:offset + count * 4])
    offset += count * 4
    return {
        'version': version, 'sample_rate': rate, 'hop': hop,
        'fingerprints': fps, 'loudness': data[offset:offset + count],
    }


def _setup_fp_logger(log_dir):
    """指紋取得専用のロガー（アプリ本体のログには流さない）"""
    fp_logger = logging.getLogger("recr.fingerprint")
    if getattr(fp_logger, "_recr_configured", False):
        return fp_logger
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "fingerprint.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(threadName)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    handlers, _ = build_queued_handlers(file_handler)
    for handler in handlers:
        fp_logger.addHandler(handler)
    fp_logger.setLevel(logging.DEBUG)
    fp_logger.propagate = False
    fp_logger._recr_configured = True
    return fp_logger


class FingerprintCollector:
    """指紋取得のバックグラウンド処理（GUIからは start_run / stop / get_status だけを使う）

    busy_reason は「今は取得を止めるべきか」を返す関数（止めるべきなら理由の文字列、
    そうでなければ None）。オンタイム録音・タイムフリー取得・聴き逃し取得の間は
    取得を止め、終わったら同じチャンクを最初から取り直す。
    """

    def __init__(self, manager, busy_reason, log_dir, base_dir=None):
        self.manager = manager
        self.busy_reason = busy_reason
        self.base_dir = Path(base_dir) if base_dir else manager.cache_dir / "fingerprints"
        self.index_file = self.base_dir / "index.json"
        self.stats_file = Path(log_dir) / "fingerprint_stats.jsonl"
        self.log = _setup_fp_logger(Path(log_dir))
        self.settings = dict(DEFAULT_SETTINGS)
        self._thread = None
        self._stop_event = threading.Event()
        self._status_lock = threading.Lock()
        self._status = {'state': 'idle', 'message': "待機中", 'done': 0, 'total': 0}

    # --- GUIから使うもの ---

    def update_settings(self, settings):
        for key in DEFAULT_SETTINGS:
            if key in settings:
                self.settings[key] = settings[key]

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start_run(self, reason):
        """取得を始める（実行中なら何もしない）。Returns: 始めたか"""
        if self.is_running():
            self.log.info(f"取得要求（{reason}）: 実行中のため無視しました")
            return False
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(reason, self._stop_event), name="cm-fingerprint", daemon=True
        )
        self._thread.start()
        return True

    def stop(self, reason="停止要求", wait=0):
        if self.is_running():
            self.log.info(f"停止要求: {reason}")
            self._stop_event.set()
            if wait:
                self._thread.join(timeout=wait)

    def get_status(self):
        with self._status_lock:
            return dict(self._status)

    def _set_status(self, **kwargs):
        with self._status_lock:
            self._status.update(kwargs)

    # --- 保存データ ---

    def load_index(self):
        if not self.index_file.exists():
            return {}
        try:
            return read_json(self.index_file) or {}
        except Exception:
            self.log.exception("index.json を読めませんでした。空として扱います")
            return {}

    def _save_index(self, index):
        self.base_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.index_file, index)

    def fp_path(self, station_id, bday, hour):
        return self.base_dir / station_id / bday.strftime("%Y%m%d") / f"{hour:02d}.fp"

    def audio_path(self, station_id, bday, hour):
        return self.base_dir / "audio" / station_id / bday.strftime("%Y%m%d") / f"{hour:02d}.aac"

    def prune(self, index, now):
        """保存期間を過ぎた指紋（と音声）を消す"""
        days = int(self.settings.get('cm_fp_retention_days') or DEFAULT_SETTINGS['cm_fp_retention_days'])
        cutoff = (now - timedelta(hours=5)).date() - timedelta(days=days)
        removed = 0
        for key in list(index):
            try:
                _, day_str, _ = key.split("|")
                bday = datetime.strptime(day_str, "%Y%m%d").date()
            except ValueError:
                continue
            if bday < cutoff:
                index.pop(key)
                removed += 1
        removed_files = 0
        for root in (self.base_dir, self.base_dir / "audio"):
            if not root.exists():
                continue
            for station_dir in root.iterdir():
                if not station_dir.is_dir() or station_dir.name == "audio":
                    continue
                for day_dir in station_dir.iterdir():
                    try:
                        bday = datetime.strptime(day_dir.name, "%Y%m%d").date()
                    except ValueError:
                        continue
                    if bday < cutoff and day_dir.is_dir():
                        for f in day_dir.iterdir():
                            f.unlink()
                            removed_files += 1
                        day_dir.rmdir()
        if removed or removed_files:
            self.log.info(
                f"保存期間（{days}日）を過ぎたデータを削除しました: 記録{removed}件、ファイル{removed_files}個"
                f"（{cutoff.isoformat()} より前の放送日）"
            )
        return removed

    def _write_stats(self, record):
        try:
            self.stats_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.stats_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            self.log.exception("集計ファイルに書き込めませんでした")

    # --- 本体 ---

    def _run(self, reason, stop_event):
        log = self.log
        run_started = time.monotonic()
        try:
            now = datetime.now()
            s = self.settings
            window_start = int(s['cm_fp_window_start'])
            window_end = int(s['cm_fp_window_end'])
            log.info(
                f"===== 取得開始（{reason}） 時間帯={window_start}時〜{window_end}時 "
                f"保存期間={s['cm_fp_retention_days']}日 音声保存={'あり' if s['cm_fp_keep_audio'] else 'なし'} "
                f"並列数={self.manager.timefree_parallel_splits} 指紋v{FP_VERSION} "
                f"({SAMPLE_RATE}Hz, 窓{FRAME_SIZE}, ホップ{HOP_SIZE}, {BAND_COUNT}帯域 "
                f"{BAND_LOW_HZ:.0f}-{BAND_HIGH_HZ:.0f}Hz)"
            )
            index = self.load_index()
            if self.prune(index, now):
                self._save_index(index)

            stations, skipped = target_stations(
                self.manager.load_reservations(), self.manager.station_mapping
            )
            for station, why in sorted(skipped.items()):
                log.info(f"対象外の局: {station}（{why}）")
            if not stations:
                log.info("対象の局がありません（NHK以外の局の有効な予約がない）")
                self._set_status(state='idle', message="対象の局がありません", done=0, total=0)
                return
            log.info(f"対象の局: {', '.join(f'{st}({self.manager.station_mapping[st]})' for st in stations)}")

            chunks, counts = plan_chunks(
                stations, self.manager.station_mapping, index, now, window_start, window_end
            )
            log.info(
                f"取得予定 {len(chunks)}件（1件=1時間） 取得済み{counts['done']}件 "
                f"失敗で断念{counts['failed']}件 期限切れ{counts['expired']}件 "
                f"まだ取れない(当日分){counts['not_ready']}件"
            )
            for station in stations:
                days = sorted({c['bday'].isoformat() for c in chunks if c['station'] == station})
                if days:
                    log.info(f"  {station}: {len(days)}日分 {days[0]}〜{days[-1]}")
            total = len(chunks)
            self._set_status(state='running', message="取得中", done=0, total=total)

            done = 0
            for chunk in chunks:
                if stop_event.is_set():
                    break
                result = self._process_chunk_with_pause(chunk, index, stop_event, done, total)
                if result == 'stopped':
                    break
                done += 1
                self._set_status(done=done)
                if result == 'failed' and not stop_event.is_set():
                    log.info(f"失敗したため{FAILURE_BACKOFF_SECONDS}秒待ってから次へ進みます")
                    stop_event.wait(FAILURE_BACKOFF_SECONDS)

            elapsed = time.monotonic() - run_started
            if stop_event.is_set():
                log.info(f"===== 取得を中止しました（{done}/{total}件、{elapsed / 60:.1f}分）")
                self._set_status(state='idle', message=f"中止しました（{done}/{total}件）")
            else:
                log.info(f"===== 取得完了（{done}/{total}件、{elapsed / 60:.1f}分）")
                self._set_status(
                    state='idle',
                    message=f"完了（{datetime.now().strftime('%m/%d %H:%M')}、{total}件）",
                )
        except Exception as e:
            log.exception("取得処理で予期しないエラーが発生しました")
            self._set_status(state='idle', message=f"エラー: {e}")

    def _wait_while_busy(self, stop_event, chunk_label):
        """録音中などの間は待つ。Returns: 停止要求があったら False"""
        reason = self.busy_reason()
        if not reason:
            return True
        self.log.info(f"一時停止: {reason}（{chunk_label} は終わってから取り直します）")
        paused_at = time.monotonic()
        while reason:
            self._set_status(state='paused', message=f"一時停止中（{reason}）")
            if stop_event.wait(PAUSE_POLL_SECONDS):
                return False
            reason = self.busy_reason()
        self.log.info(f"再開します（一時停止 {(time.monotonic() - paused_at) / 60:.1f}分）")
        self._set_status(state='running', message="取得中")
        return True

    def _process_chunk_with_pause(self, chunk, index, stop_event, done, total):
        """1チャンクを取得する。録音が始まって中断した場合は、終わるのを待って取り直す

        Returns:
            'done' / 'failed' / 'retry_later'（失敗したがまだ試行回数が残っている） / 'stopped'
        """
        label = f"{chunk['station']} {chunk['bday'].isoformat()} {chunk['hour']:02d}時"
        while True:
            if not self._wait_while_busy(stop_event, label):
                return 'stopped'
            if datetime.now() - timedelta(days=TIMEFREE_AVAILABLE_DAYS) + EXPIRY_MARGIN > chunk['start_dt']:
                self.log.info(f"期限切れのため飛ばします: {label}")
                return 'expired'
            self._set_status(
                state='running', message=f"取得中: {label}（{done + 1}/{total}件目）"
            )
            outcome = self._fetch_chunk(chunk, stop_event)
            if outcome['result'] == 'paused':
                continue
            if outcome['result'] == 'stopped':
                return 'stopped'
            return self._record_outcome(chunk, index, outcome)

    def _record_outcome(self, chunk, index, outcome):
        entry = index.get(chunk['key']) or {}
        attempts = entry.get('attempts', 0) + 1
        now_iso = datetime.now().isoformat(timespec="seconds")
        if outcome['result'] == 'done':
            index[chunk['key']] = {
                'status': 'done', 'attempts': attempts, 'updated': now_iso,
                'version': FP_VERSION, 'frames': outcome['frames'],
                'audio_seconds': round(outcome['audio_seconds'], 1),
            }
            result = 'done'
        else:
            status = 'failed' if attempts >= MAX_ATTEMPTS else 'pending'
            index[chunk['key']] = {
                'status': status, 'attempts': attempts, 'updated': now_iso,
                'error': outcome.get('error'),
            }
            if status == 'failed':
                self.log.warning(
                    f"{chunk['station']} {chunk['bday'].isoformat()} {chunk['hour']:02d}時: "
                    f"{attempts}回失敗したため、このチャンクは諦めます"
                )
            result = 'failed' if status == 'failed' else 'retry_later'
        self._save_index(index)
        return 'failed' if result != 'done' else 'done'

    def _fetch_chunk(self, chunk, stop_event):
        """1チャンク（1時間）をタイムフリーで取得して指紋を作り、保存する

        Returns:
            dict: {'result': 'done'|'failed'|'paused'|'stopped', ...集計}
        """
        import av

        log = self.log
        station, station_id = chunk['station'], chunk['station_id']
        ft_dt, to_dt = chunk['start_dt'], chunk['end_dt']
        label = f"{station} {chunk['bday'].isoformat()} {chunk['hour']:02d}時"
        expected_seconds = (to_dt - ft_dt).total_seconds()
        stats = {
            'ts': datetime.now().isoformat(timespec="seconds"), 'station': station,
            'station_id': station_id, 'bday': chunk['bday'].isoformat(), 'hour': chunk['hour'],
            'ft': ft_dt.strftime("%Y%m%d%H%M%S"), 'to': to_dt.strftime("%Y%m%d%H%M%S"),
            'attempt': chunk['attempts'] + 1, 'version': FP_VERSION,
        }
        log.info(f"チャンク開始: {label} [{stats['ft']}-{stats['to']}] {stats['attempt']}回目")

        wall_start = time.monotonic()
        cpu_start = time.thread_time()
        builder = FingerprintBuilder()
        segments = 0
        segment_bytes = 0
        decode_errors = 0
        abort = None
        error = None
        audio_file = None
        segment_iter = None
        progress = {'done': 0, 'total': 0}
        chunk_stop = threading.Event()
        last_progress_log = time.monotonic()

        def on_progress(done, total):
            progress['done'], progress['total'] = done, total

        try:
            t0 = time.monotonic()
            auth_token = self.manager._ensure_authenticated()
            playlist_base_url = self.manager._get_timefree_playlist_url(station_id)
            stats['auth_seconds'] = round(time.monotonic() - t0, 2)
            if not playlist_base_url:
                raise RuntimeError("タイムフリーのプレイリストURLが取得できませんでした")
            log.debug(f"認証・プレイリストURL取得 {stats['auth_seconds']}秒 area={self.manager.area_id}")

            if self.settings.get('cm_fp_keep_audio'):
                path = self.audio_path(station_id, chunk['bday'], chunk['hour'])
                path.parent.mkdir(parents=True, exist_ok=True)
                audio_file = open(path, "wb")

            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            segment_iter = self.manager._iter_timefree_segments_parallel(
                playlist_base_url, station_id, auth_token, ft_dt, to_dt, chunk_stop,
                on_progress=on_progress, num_splits=self.manager.timefree_parallel_splits,
            )
            for seg in segment_iter:
                if stop_event.is_set():
                    abort = 'stopped'
                    break
                busy = self.busy_reason()
                if busy:
                    abort = 'paused'
                    log.info(f"中断: {busy}（{label}、{segments}セグメント取得済みを破棄）")
                    break
                segments += 1
                segment_bytes += len(seg)
                if audio_file is not None:
                    audio_file.write(seg)
                container = av.open(io.BytesIO(seg), format="aac")
                try:
                    stream = container.streams.audio[0]
                    for frame in container.decode(stream):
                        for resampled in resampler.resample(frame):
                            builder.feed(bytes(resampled.planes[0])[:resampled.samples * 2])
                except av.error.FFmpegError as e:
                    decode_errors += 1
                    log.warning(f"デコードエラー（{segments}セグメント目）: {e}")
                finally:
                    container.close()
                if time.monotonic() - last_progress_log >= 60:
                    last_progress_log = time.monotonic()
                    log.debug(
                        f"進捗 {label}: セグメント{segments}件 ({progress['done']}/{progress['total']}) "
                        f"音声{builder.samples_in / SAMPLE_RATE:.0f}秒 フレーム{builder.frame_count} "
                        f"経過{time.monotonic() - wall_start:.0f}秒 CPU{time.thread_time() - cpu_start:.1f}秒"
                    )
            if abort is None:
                for resampled in resampler.resample(None):
                    builder.feed(bytes(resampled.planes[0])[:resampled.samples * 2])
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            log.exception(f"取得エラー: {label}")
        finally:
            chunk_stop.set()
            if segment_iter is not None:
                segment_iter.close()
            if audio_file is not None:
                audio_file.close()
                if abort or error:
                    try:
                        Path(audio_file.name).unlink()
                    except OSError:
                        pass

        wall = time.monotonic() - wall_start
        cpu = time.thread_time() - cpu_start
        audio_seconds = builder.samples_in / SAMPLE_RATE
        frames = builder.frame_count
        loud = builder.loudness
        silent = sum(1 for v in loud if v < SILENCE_DBFS + 100)
        stats.update({
            'segments': segments, 'segment_bytes': segment_bytes, 'decode_errors': decode_errors,
            'audio_seconds': round(audio_seconds, 2), 'expected_seconds': expected_seconds,
            'audio_ratio': round(audio_seconds / expected_seconds, 4) if expected_seconds else None,
            'frames': frames, 'wall_seconds': round(wall, 1), 'cpu_seconds': round(cpu, 1),
            'speed': round(audio_seconds / wall, 2) if wall else None,
            'silence_ratio': round(silent / frames, 4) if frames else None,
            'loudness_mean_dbfs': round(sum(loud) / frames - 100, 1) if frames else None,
            'loudness_max_dbfs': (max(loud) - 100) if frames else None,
        })

        if abort:
            stats['result'] = abort
            self._write_stats(stats)
            return {'result': abort}

        if error is None and audio_seconds < expected_seconds * MIN_AUDIO_RATIO:
            error = (
                f"音声が足りません（{audio_seconds:.0f}秒 / 予定{expected_seconds:.0f}秒、"
                f"セグメント{segments}件）"
            )
            log.warning(f"{label}: {error}")

        if error:
            stats['result'] = 'failed'
            stats['error'] = error
            self._write_stats(stats)
            log.warning(f"チャンク失敗: {label}（{error}）経過{wall:.0f}秒")
            return {'result': 'failed', 'error': error}

        fp_path = self.fp_path(station_id, chunk['bday'], chunk['hour'])
        write_fp_file(fp_path, builder.fingerprints, loud)
        stats['result'] = 'done'
        stats['fp_bytes'] = fp_path.stat().st_size
        self._write_stats(stats)
        log.info(
            f"チャンク完了: {label} 音声{audio_seconds:.0f}秒（{stats['audio_ratio']:.1%}） "
            f"セグメント{segments}件 {segment_bytes / 1e6:.1f}MB フレーム{frames} "
            f"無音{stats['silence_ratio']:.1%} 平均{stats['loudness_mean_dbfs']}dBFS "
            f"経過{wall:.0f}秒（{stats['speed']}倍速） CPU{cpu:.0f}秒 -> {fp_path.name} "
            f"{stats['fp_bytes'] / 1024:.0f}KB"
        )
        return {'result': 'done', 'frames': frames, 'audio_seconds': audio_seconds}
