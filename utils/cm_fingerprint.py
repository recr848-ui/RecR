"""CMカット用の音声フィンガープリント（指紋）取得

予約が入っている民放局の放送を、radikoのタイムフリーで1時間ずつ取得し、
音声フィンガープリントを作って保存する（音声そのものは既定では保存しない）。
将来、複数日の指紋を照らし合わせて「何度も流れる区間＝CM」を見つけるための材料集め。

取得の単位は「局 × 放送日 × 1時間」（チャンク）。radikoの放送日は5時〜29時なので、
時刻は放送日基準の時（11〜26など。24以上は翌日の0時以降）で扱う。
タイムフリーで取れる（放送後7日以内）範囲のうち、まだ取っていないチャンクを取るため、
初回は取れる分すべて、2回目以降は前回からの差分（通常1日分）になる。予約番組の時間帯を
優先し、同じ優先度の中では古い順に、局が偏らないよう順番を回しながら取る。
番組表で放送休止とされている時間帯は取らない（無音の指紋は「繰り返し」と誤判定されやすい）。

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
import queue
import re
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
from utils.reservation_logic import program_air_window

FP_VERSION = 1
SAMPLE_RATE = 8000
FRAME_SIZE = 1024
HOP_SIZE = 256
BAND_COUNT = 33
BAND_LOW_HZ = 300.0
BAND_HIGH_HZ = 2000.0
# この音量（dBFS）未満のフレームを無音とみなす（ログの集計用）
SILENCE_DBFS = -50.0
# 番組表に休止がないのに、無音の割合がこれ以上なら警告を残す
SILENCE_WARN_RATIO = 0.3

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
# 1チャンクの同時接続数。1接続は放送と同じ速さでしか取れないため、これが取得の速さを決める
# （1時間分が約10分）。radikoの負荷を考えて、通常のタイムフリー取得3件分（12接続）より
# 控えめにしている
FETCH_PARALLEL_SPLITS = 6
# 中断した区間の続きは、最後に取れたセグメントの開始のこの秒数後から取る。セグメントの
# 長さ（5秒）ちょうどにすると、境目が少しずれたときに1つ取りこぼすため、重なる側に寄せる
# （重なって載った取得済みのセグメントは捨てる）
RESUME_OFFSET_SECONDS = 1
# 解析待ちにできるチャンク数（1件あたり約22MB。これを超えると取得側が待つ）
ANALYSIS_QUEUE_SIZE = 2
# 番組表で放送休止を表す番組名（「放送休止」「放送休止中」「休止中(PART1)」
# 「放送機器メンテナンスのため放送休止」など）
OFF_AIR_TITLE = re.compile(r"休止|停波")
# 1時間のうち放送休止でない部分がこの秒数未満なら、そのチャンクは取らない
OFF_AIR_MIN_ON_AIR_SECONDS = 60
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


def chunk_label(chunk):
    return f"{chunk['station']} {chunk['bday'].isoformat()} {chunk['hour']:02d}時"


def oldest_available(now):
    """タイムフリーで安全に取れる最も古い時刻"""
    return now - timedelta(days=TIMEFREE_AVAILABLE_DAYS) + EXPIRY_MARGIN


def off_air_windows(programs):
    """番組表から放送休止の枠を取り出す

    Returns:
        list[(datetime, datetime)]: 放送休止の (開始, 終了)
    """
    windows = []
    for program in programs:
        if not OFF_AIR_TITLE.search(program.get('title') or ''):
            continue
        start_dt, end_dt = program_air_window(program)
        if start_dt is not None:
            windows.append((start_dt, end_dt))
    return windows


def off_air_overlap(windows, start_dt, end_dt):
    """[start_dt, end_dt) のうち放送休止の範囲を、start_dt からの秒数で返す

    Returns:
        list[[int, int]]: 重なりをまとめた (開始秒, 終了秒) のリスト（時系列順）
    """
    merged = []
    for w_start, w_end in sorted(windows):
        start = max(w_start, start_dt)
        end = min(w_end, end_dt)
        if start >= end:
            continue
        from_sec = int((start - start_dt).total_seconds())
        to_sec = int((end - start_dt).total_seconds())
        if merged and from_sec <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], to_sec)
        else:
            merged.append([from_sec, to_sec])
    return merged


def reserved_hours(reservations, stations):
    """局ごとの、予約番組の時間帯とその前後1時間（放送日基準の時）

    同じ時間帯のCMが入りやすいので、取得が追いつかないときはここを優先する。曜日は見ない。

    Returns:
        dict[str, set[int]]: 局名 -> 時（5〜29）の集合
    """
    day_start = 5 * 60
    hours = {}
    for res in reservations:
        station = res.get('station')
        if station not in stations or not res.get('enabled', True):
            continue
        try:
            start_h, start_m = (int(p) for p in res['start'].split(":"))
            end_h, end_m = (int(p) for p in res['end'].split(":"))
        except (KeyError, ValueError, AttributeError):
            continue
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start < day_start:
            start += 24 * 60
        if end <= start:
            end += 24 * 60
        hours.setdefault(station, set()).update(range(start // 60 - 1, (end - 1) // 60 + 2))
    return hours


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


def plan_chunks(stations, station_mapping, index, now, window_start, window_end,
                reserved=None, off_air=None):
    """これから取得するチャンクを、取る順に返す

    予約番組の時間帯（reserved）を先に、同じ優先度の中では古い（期限が近い）順に並べる。
    同じ時刻の中の局の順番は時間ごとに回す。固定すると、取得が追いつかないときに後ろの局
    だけが毎回期限切れになる。

    Args:
        reserved (dict または None): reserved_hours の戻り値
        off_air (dict または None): 局名 -> off_air_windows の戻り値

    Returns:
        (list[dict], dict): (チャンクのリスト, 取得済み・期限切れ等で除いた件数の内訳)
    """
    oldest_ok = oldest_available(now)
    reserved = reserved or {}
    off_air = off_air or {}
    # 放送日は5時始まりなので、今日の放送日 = (now - 5時間) の日付
    today_bday = (now - timedelta(hours=5)).date()
    chunks = []
    counts = {'done': 0, 'failed': 0, 'expired': 0, 'not_ready': 0, 'off_air': 0}
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
                end_dt = start_dt + timedelta(hours=1)
                overlap = off_air_overlap(off_air.get(station, ()), start_dt, end_dt)
                off_air_seconds = sum(end - start for start, end in overlap)
                if 3600 - off_air_seconds < OFF_AIR_MIN_ON_AIR_SECONDS:
                    counts['off_air'] += 1
                    continue
                chunks.append({
                    'key': key, 'station': station, 'station_id': station_id,
                    'bday': bday, 'hour': hour,
                    'start_dt': start_dt, 'end_dt': end_dt,
                    'attempts': entry.get('attempts', 0), 'off_air': overlap,
                })

    position = {station: i for i, station in enumerate(stations)}

    def order(chunk):
        turn = (position[chunk['station']] + chunk['bday'].toordinal() + chunk['hour']) % len(stations)
        is_reserved = chunk['hour'] in reserved.get(chunk['station'], ())
        return (not is_reserved, chunk['start_dt'], turn)

    chunks.sort(key=order)
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
    取得を止め、終わったら同じチャンクを最初から取り直す。取得し終えた分の解析は止めない。
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
        # index は取得スレッドと解析スレッドの両方が書き換える
        self._index_lock = threading.Lock()
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

    def _load_off_air(self, stations):
        """対象局の番組表（タイムフリー分と通常分のキャッシュ）から放送休止の枠を集める"""
        off_air = {}
        for station in stations:
            programs = list(self.manager.load_cached_schedule(f"{station}::timefree") or [])
            programs += self.manager.load_cached_schedule(station) or []
            windows = off_air_windows(programs)
            if windows:
                off_air[station] = windows
        return off_air

    def _plan(self, stations, index, now):
        s = self.settings
        return plan_chunks(
            stations, self.manager.station_mapping, index, now,
            int(s['cm_fp_window_start']), int(s['cm_fp_window_end']),
            reserved=reserved_hours(self.manager.load_reservations(), stations),
            off_air=self._load_off_air(stations),
        )

    def _run(self, reason, stop_event):
        """取得スレッド本体。1件取るたびに計画を立て直す

        取得（ネットワーク待ちが大半）と解析（CPUが大半）を別スレッドに分け、解析の
        終わりを待たずに次の取得へ進む。取得が長引いても翌日分や予約の変更を取り込める
        よう、取得する順番は毎回最新の状態から決める。
        """
        log = self.log
        run_started = time.monotonic()
        jobs = queue.Queue(maxsize=ANALYSIS_QUEUE_SIZE)
        try:
            s = self.settings
            log.info(
                f"===== 取得開始（{reason}） 時間帯={s['cm_fp_window_start']}時〜{s['cm_fp_window_end']}時 "
                f"保存期間={s['cm_fp_retention_days']}日 音声保存={'あり' if s['cm_fp_keep_audio'] else 'なし'} "
                f"並列数={FETCH_PARALLEL_SPLITS} 指紋v{FP_VERSION} "
                f"({SAMPLE_RATE}Hz, 窓{FRAME_SIZE}, ホップ{HOP_SIZE}, {BAND_COUNT}帯域 "
                f"{BAND_LOW_HZ:.0f}-{BAND_HIGH_HZ:.0f}Hz)"
            )
            index = self.load_index()
            if self.prune(index, datetime.now()):
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

            chunks, counts = self._plan(stations, index, datetime.now())
            log.info(
                f"取得予定 {len(chunks)}件（1件=1時間） 取得済み{counts['done']}件 "
                f"失敗で断念{counts['failed']}件 期限切れ{counts['expired']}件 "
                f"放送休止{counts['off_air']}件 まだ取れない(当日分){counts['not_ready']}件"
            )
            for station in stations:
                days = sorted({c['bday'].isoformat() for c in chunks if c['station'] == station})
                if days:
                    log.info(f"  {station}: {len(days)}日分 {days[0]}〜{days[-1]}")
            self._set_status(state='running', message="取得中", done=0, total=len(chunks))

            analyzed = {'count': 0}
            analysis = threading.Thread(
                target=self._analysis_loop, args=(jobs, index, stop_event, analyzed),
                name="cm-fingerprint-analysis", daemon=True,
            )
            analysis.start()

            tried = set()
            planned = {}
            while not stop_event.is_set():
                now = datetime.now()
                chunks = [c for c in self._plan(stations, index, now)[0] if c['key'] not in tried]
                self._log_dropped(planned, chunks, tried, now)
                planned = {c['key']: c for c in chunks}
                total = analyzed['count'] + jobs.qsize() + len(chunks)
                self._set_status(total=total)
                if not chunks:
                    break

                chunk = chunks[0]
                tried.add(chunk['key'])
                job = self._download_with_pause(chunk, stop_event, analyzed['count'], total)
                if job['result'] == 'stopped':
                    break
                if job['result'] == 'expired':
                    continue
                if job['result'] == 'failed':
                    with self._index_lock:
                        self._record_outcome(chunk, index, job)
                    log.info(f"失敗したため{FAILURE_BACKOFF_SECONDS}秒待ってから次へ進みます")
                    stop_event.wait(FAILURE_BACKOFF_SECONDS)
                    continue
                self._put_job(jobs, job, stop_event)

            self._put_job(jobs, None, stop_event)
            analysis.join()

            elapsed = time.monotonic() - run_started
            done = analyzed['count']
            if stop_event.is_set():
                log.info(f"===== 取得を中止しました（{done}件、{elapsed / 60:.1f}分）")
                self._set_status(state='idle', message=f"中止しました（{done}件）")
            else:
                log.info(f"===== 取得完了（{done}件、{elapsed / 60:.1f}分）")
                self._set_status(
                    state='idle',
                    message=f"完了（{datetime.now().strftime('%m/%d %H:%M')}、{done}件）",
                )
        except Exception as e:
            log.exception("取得処理で予期しないエラーが発生しました")
            stop_event.set()
            self._set_status(state='idle', message=f"エラー: {e}")

    def _log_dropped(self, planned, chunks, tried, now):
        """前回の計画にあって今回消えたチャンク（取らずに対象から外れたもの）をログに残す"""
        current = {c['key'] for c in chunks}
        for key, chunk in planned.items():
            if key in current or key in tried:
                continue
            if chunk['start_dt'] < oldest_available(now):
                self.log.info(f"期限切れのため飛ばします: {chunk_label(chunk)}")
            else:
                self.log.info(f"対象から外れました（放送休止または予約の変更）: {chunk_label(chunk)}")

    @staticmethod
    def _put_job(jobs, job, stop_event):
        """解析待ちに積む（解析が詰まっている間はここで待つ）。停止要求があれば積まずに戻る"""
        while not stop_event.is_set():
            try:
                jobs.put(job, timeout=1)
                return
            except queue.Full:
                continue

    def _analysis_loop(self, jobs, index, stop_event, analyzed):
        """解析スレッド本体。取得済みの音声から指紋を作って保存する

        録音が始まっても止めない（通信を使わず、取得済みの分を捨てるのはもったいないため）。
        """
        while True:
            try:
                job = jobs.get(timeout=1)
            except queue.Empty:
                if stop_event.is_set():
                    return
                continue
            if job is None or stop_event.is_set():
                return
            chunk = job['chunk']
            try:
                outcome = self._analyze_chunk(job, stop_event)
            except Exception as e:
                self.log.exception(f"解析エラー: {chunk_label(chunk)}")
                outcome = {'result': 'failed', 'error': f"{type(e).__name__}: {e}"}
            if outcome['result'] == 'stopped':
                return
            with self._index_lock:
                self._record_outcome(chunk, index, outcome)
            analyzed['count'] += 1
            self._set_status(done=analyzed['count'])

    def _wait_while_busy(self, stop_event, label):
        """録音中などの間は待つ。Returns: 停止要求があったら False"""
        reason = self.busy_reason()
        if not reason:
            return True
        self.log.info(f"一時停止: {reason}（{label} は終わってから取り直します）")
        paused_at = time.monotonic()
        while reason:
            self._set_status(state='paused', message=f"一時停止中（{reason}）")
            if stop_event.wait(PAUSE_POLL_SECONDS):
                return False
            reason = self.busy_reason()
        self.log.info(f"再開します（一時停止 {(time.monotonic() - paused_at) / 60:.1f}分）")
        self._set_status(state='running', message="取得中")
        return True

    def _download_with_pause(self, chunk, stop_event, done, total):
        """1チャンクを取得する。録音が始まって中断した場合は、終わるのを待って続きから取り直す

        Returns:
            dict: _download_chunk の結果（'result' は 'done' / 'failed' / 'stopped' / 'expired'）
        """
        label = chunk_label(chunk)
        partial = None
        while True:
            if not self._wait_while_busy(stop_event, label):
                return {'result': 'stopped'}
            if chunk['start_dt'] < oldest_available(datetime.now()):
                self.log.info(f"期限切れのため飛ばします: {label}")
                return {'result': 'expired'}
            self._set_status(state='running', message=f"取得中: {label}（{done + 1}/{total}件目）")
            job = self._download_chunk(chunk, stop_event, partial)
            if job['result'] != 'paused':
                return job
            partial = job['partial']

    def _record_outcome(self, chunk, index, outcome):
        """結果を index に記録して保存する（呼び出し側で _index_lock を取ること）"""
        entry = index.get(chunk['key']) or {}
        attempts = entry.get('attempts', 0) + 1
        now_iso = datetime.now().isoformat(timespec="seconds")
        if outcome['result'] == 'done':
            index[chunk['key']] = {
                'status': 'done', 'attempts': attempts, 'updated': now_iso,
                'version': FP_VERSION, 'frames': outcome['frames'],
                'audio_seconds': round(outcome['audio_seconds'], 1),
            }
            if chunk.get('off_air'):
                # 判定のときに、この範囲（チャンク先頭からの秒数）を除けるように残す
                index[chunk['key']]['off_air'] = chunk['off_air']
        else:
            status = 'failed' if attempts >= MAX_ATTEMPTS else 'pending'
            index[chunk['key']] = {
                'status': status, 'attempts': attempts, 'updated': now_iso,
                'error': outcome.get('error'),
            }
            if status == 'failed':
                self.log.warning(f"{chunk_label(chunk)}: {attempts}回失敗したため、このチャンクは諦めます")
        self._save_index(index)

    def _download_chunk(self, chunk, stop_event, partial=None):
        """1チャンク（1時間）をタイムフリーで取得する（音声はメモリに持つだけで解析はしない）

        1時間を FETCH_PARALLEL_SPLITS 個の区間に分けて並列に取る。partial は、前回
        録音で中断したときに返した区間ごとの取得状況で、渡すとその続きから取る。

        Returns:
            dict: {'result': 'done'|'failed'|'paused'|'stopped', ...}。'done' なら解析用に
            'chunk', 'segments', 'stats' を、'paused' なら続きを取るための 'partial' を含む
        """
        log = self.log
        station_id = chunk['station_id']
        ft_dt, to_dt = chunk['start_dt'], chunk['end_dt']
        label = chunk_label(chunk)
        stats = {
            'ts': datetime.now().isoformat(timespec="seconds"), 'station': chunk['station'],
            'station_id': station_id, 'bday': chunk['bday'].isoformat(), 'hour': chunk['hour'],
            'ft': ft_dt.strftime("%Y%m%d%H%M%S"), 'to': to_dt.strftime("%Y%m%d%H%M%S"),
            'attempt': chunk['attempts'] + 1, 'version': FP_VERSION,
            'splits': FETCH_PARALLEL_SPLITS,
        }
        if partial is None:
            partial = [
                {'start': start, 'end': end, 'last_dt': None, 'segments': [], 'done': False}
                for start, end in self.manager._split_time_range(ft_dt, to_dt, FETCH_PARALLEL_SPLITS)
            ]
        held = sum(len(r['segments']) for r in partial)
        stats['resumed_segments'] = held
        if held:
            log.info(f"チャンク再開: {label} 取得済み{held}セグメントの続きから {stats['attempt']}回目")
        else:
            log.info(f"チャンク開始: {label} [{stats['ft']}-{stats['to']}] {stats['attempt']}回目")

        wall_start = time.monotonic()
        abort = {}
        error = None
        lock = threading.Lock()
        last_progress_log = [time.monotonic()]

        # 区間ごとの取り始めの時刻。開始時刻が分からないセグメントがあった区間は、
        # どこまで取れたか分からないので最初から取り直す
        todo = []
        for r in partial:
            if r['done']:
                continue
            if r.pop('unknown_dt', False):
                r['segments'], r['last_dt'] = [], None
            from_dt = r['start'] if r['last_dt'] is None else r['last_dt'] + timedelta(seconds=RESUME_OFFSET_SECONDS)
            if from_dt >= r['end']:
                r['done'] = True
                continue
            todo.append((r, from_dt))

        def on_segment(idx, seg_dt, data):
            r = todo[idx][0]
            with lock:
                if seg_dt is not None and r['last_dt'] is not None and seg_dt <= r['last_dt']:
                    return  # 続きから取り直した直後は、取得済みのセグメントがもう一度載ることがある
                r['segments'].append(data)
                if seg_dt is None:
                    r['unknown_dt'] = True
                else:
                    r['last_dt'] = seg_dt

        def should_stop():
            if stop_event.is_set():
                abort['result'] = 'stopped'
                return True
            busy = self.busy_reason()
            if busy:
                abort['result'] = 'paused'
                abort['reason'] = busy
                return True
            if time.monotonic() - last_progress_log[0] >= 60:
                last_progress_log[0] = time.monotonic()
                with lock:
                    got = sum(len(r['segments']) for r in partial)
                log.debug(f"取得中 {label}: {got}件 経過{time.monotonic() - wall_start:.0f}秒")
            return False

        try:
            t0 = time.monotonic()
            auth_token = self.manager._ensure_authenticated()
            playlist_base_url = self.manager._get_timefree_playlist_url(station_id)
            stats['auth_seconds'] = round(time.monotonic() - t0, 2)
            if not playlist_base_url:
                raise RuntimeError("タイムフリーのプレイリストURLが取得できませんでした")
            log.debug(f"認証・プレイリストURL取得 {stats['auth_seconds']}秒 area={self.manager.area_id}")

            if todo:
                outcomes = self.manager._fetch_timefree_ranges_parallel(
                    playlist_base_url, station_id, auth_token, [(f, r['end']) for r, f in todo],
                    threading.Event(), on_segment, should_stop=should_stop, log=log,
                )
                for (r, _), outcome in zip(todo, outcomes):
                    if outcome == 'done':
                        r['done'] = True
                    elif isinstance(outcome, Exception) and error is None:
                        error = f"{type(outcome).__name__}: {outcome}"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            log.exception(f"取得エラー: {label}")

        segments = [seg for r in partial for seg in r['segments']]
        wall = time.monotonic() - wall_start
        stats.update({
            'segments': len(segments), 'segment_bytes': sum(len(s) for s in segments),
            'download_seconds': round(wall, 1),
        })
        if abort and not error:
            stats['result'] = abort['result']
            self._write_stats(stats)
            if abort['result'] == 'paused':
                log.info(f"中断: {abort['reason']}（{label}、{len(segments)}セグメント取得済み。続きから取り直します）")
                return {'result': 'paused', 'partial': partial}
            return {'result': 'stopped'}
        if error:
            stats['result'] = 'failed'
            stats['error'] = error
            self._write_stats(stats)
            log.warning(f"チャンク失敗: {label}（{error}）経過{wall:.0f}秒")
            return {'result': 'failed', 'error': error}
        log.debug(
            f"取得完了 {label}: セグメント{len(segments)}件 "
            f"{stats['segment_bytes'] / 1e6:.1f}MB 経過{wall:.0f}秒"
        )
        return {'result': 'done', 'chunk': chunk, 'segments': segments, 'stats': stats}

    def _analyze_chunk(self, job, stop_event):
        """取得済みの音声をデコードして指紋を作り、保存する

        Returns:
            dict: {'result': 'done'|'failed'|'stopped', ...集計}
        """
        import av

        log = self.log
        chunk, segments, stats = job['chunk'], job['segments'], job['stats']
        station_id = chunk['station_id']
        label = chunk_label(chunk)
        expected_seconds = (chunk['end_dt'] - chunk['start_dt']).total_seconds()

        wall_start = time.monotonic()
        cpu_start = time.thread_time()
        last_progress_log = time.monotonic()
        builder = FingerprintBuilder()
        decode_errors = 0
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        for i, seg in enumerate(segments, 1):
            if stop_event.is_set():
                return {'result': 'stopped'}
            container = av.open(io.BytesIO(seg), format="aac")
            try:
                stream = container.streams.audio[0]
                for frame in container.decode(stream):
                    for resampled in resampler.resample(frame):
                        builder.feed(bytes(resampled.planes[0])[:resampled.samples * 2])
            except av.error.FFmpegError as e:
                decode_errors += 1
                log.warning(f"デコードエラー（{label}、{i}セグメント目）: {e}")
            finally:
                container.close()
            if time.monotonic() - last_progress_log >= 60:
                last_progress_log = time.monotonic()
                log.debug(
                    f"解析中 {label}: {i}/{len(segments)}件 音声{builder.samples_in / SAMPLE_RATE:.0f}秒 "
                    f"経過{time.monotonic() - wall_start:.0f}秒 CPU{time.thread_time() - cpu_start:.1f}秒"
                )
        for resampled in resampler.resample(None):
            builder.feed(bytes(resampled.planes[0])[:resampled.samples * 2])

        wall = time.monotonic() - wall_start
        cpu = time.thread_time() - cpu_start
        audio_seconds = builder.samples_in / SAMPLE_RATE
        frames = builder.frame_count
        loud = builder.loudness
        silent = sum(1 for v in loud if v < SILENCE_DBFS + 100)
        silence_ratio = silent / frames if frames else 0.0
        off_air_seconds = sum(end - start for start, end in chunk.get('off_air') or [])
        stats.update({
            'decode_errors': decode_errors,
            'audio_seconds': round(audio_seconds, 2), 'expected_seconds': expected_seconds,
            'audio_ratio': round(audio_seconds / expected_seconds, 4) if expected_seconds else None,
            'frames': frames, 'analysis_seconds': round(wall, 1), 'cpu_seconds': round(cpu, 1),
            'silence_ratio': round(silence_ratio, 4),
            'off_air_seconds': off_air_seconds,
            'loudness_mean_dbfs': round(sum(loud) / frames - 100, 1) if frames else None,
            'loudness_max_dbfs': (max(loud) - 100) if frames else None,
        })

        if audio_seconds < expected_seconds * MIN_AUDIO_RATIO:
            error = (
                f"音声が足りません（{audio_seconds:.0f}秒 / 予定{expected_seconds:.0f}秒、"
                f"セグメント{len(segments)}件）"
            )
            stats['result'] = 'failed'
            stats['error'] = error
            self._write_stats(stats)
            log.warning(f"チャンク失敗: {label}（{error}）")
            return {'result': 'failed', 'error': error}

        if self.settings.get('cm_fp_keep_audio'):
            path = self.audio_path(station_id, chunk['bday'], chunk['hour'])
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as f:
                for seg in segments:
                    f.write(seg)

        fp_path = self.fp_path(station_id, chunk['bday'], chunk['hour'])
        write_fp_file(fp_path, builder.fingerprints, loud)
        stats['result'] = 'done'
        stats['fp_bytes'] = fp_path.stat().st_size
        self._write_stats(stats)
        log.info(
            f"チャンク完了: {label} 音声{audio_seconds:.0f}秒（{stats['audio_ratio']:.1%}） "
            f"セグメント{len(segments)}件 {stats['segment_bytes'] / 1e6:.1f}MB フレーム{frames} "
            f"無音{silence_ratio:.1%} 平均{stats['loudness_mean_dbfs']}dBFS "
            f"取得{stats['download_seconds']:.0f}秒 解析{wall:.0f}秒 CPU{cpu:.0f}秒 -> {fp_path.name} "
            f"{stats['fp_bytes'] / 1024:.0f}KB"
        )
        if off_air_seconds:
            log.info(
                f"番組表との照合: {label} 放送休止{off_air_seconds / expected_seconds:.1%} / "
                f"実測の無音{silence_ratio:.1%}"
            )
        elif silence_ratio >= SILENCE_WARN_RATIO:
            log.warning(f"番組表に休止がないのに無音が多いチャンクです: {label} 無音{silence_ratio:.1%}")
        return {'result': 'done', 'frames': frames, 'audio_seconds': audio_seconds}
