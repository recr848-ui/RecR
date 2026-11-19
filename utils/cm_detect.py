"""CMカット: 録音ファイルのCMの位置を指紋の繰り返しから見つけ、チャプターを付ける

考え方:
  CMは同じ局で1日に何度も、いろいろな時刻に流れる。録音の指紋を、同じ局の前後の日の
  指紋（utils.cm_fingerprint が集めたもの）と照らし合わせ、「別の時刻にも流れている音」が
  続く部分をCMの塊とみなす。CMの塊の頭と、番組に戻るところにチャプターを付ける。

  - 指紋（32ビット）が完全に一致するフレームは少ない（同じCMでもビットが約1割食い違い、
    放送ごとに区切りの位置もずれる）ため、1ビット違いまで一致とみなして引く
  - 2か所の時間差がほぼ一定のまま一致が続く区間を「同じ音が2回流れた」とみなす
  - 録音自身のタイムフリー版（同じ放送）との一致は除く
  - 別の日の同じ時刻（前後 SLOT_MINUTES 以内）にしか流れていない音は、番組のオープニングや
    毎週かかる曲なので、CMの根拠にしない
  - 1か所の相手と SONG_SECONDS 以上続けて一致する音は曲とみなす（CMは長くても60秒）
  - つなぎ音楽のループ（タイムフリー非対応の番組の時間など）はデジタルに同じ音なので、
    一致率が LOOP_DENSITY を超える。これは除く

音声そのものは削らず、チャプター情報だけを足す（判定を間違えても何も消えない）。
"""

import fractions
import logging
import os
import queue
import re
import struct
import threading
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from utils.cm_fingerprint import (
    BAND_COUNT, FRAME_SIZE, HOP_SIZE, SAMPLE_RATE, SILENCE_DBFS, _band_bins, _setup_fp_logger,
    is_nhk_station,
)
from utils.json_store import read_json, write_json_atomic

logger = logging.getLogger(__name__)

FPS = SAMPLE_RATE / HOP_SIZE  # 1秒あたりのフレーム数（31.25）
SILENT_LOUDNESS = int(SILENCE_DBFS + 100)  # loudness（dBFS+100）がこれ未満のフレームは使わない
_FP_HEADER = struct.Struct("<4sHHIII")

# 照合の相手にする指紋: 録音と同じ局の、放送日の前後この日数
REFERENCE_DAYS = 10
# 照合用データは1時間ごとのファイルをつなぐ。間に隙間を入れ、ファイルをまたぐ一致を出さない
FILE_GAP = int(60 * FPS)
# これより多く出る指紋の値は、ありふれた音（無音に近いなど）なので使わない
MAX_OCCURRENCES = 40
# 自分自身の近く（伸ばした音など）を一致とみなさない
MIN_DELTA = int(5 * FPS)
# 同じ時間差の一致がこれ以上空いたら別の区間とする
RUN_GAP = int(4 * FPS)
# 時間差のぶれ（数フレーム）を同じとみなす幅
DELTA_BUCKET = 3
# 一致区間とみなす最低の一致数と長さ
RUN_MIN_HITS = 6
RUN_MIN_SECONDS = 4
LOOP_DENSITY = 0.3
# 繰り返し部分の間の、これより短い切れ目はつなげる（秒）
REGION_GAP = 2.5
SONG_SECONDS = 65
# CMらしい部分の間がこれ以下なら、ひと続きのCMの塊とみなす（1回しか流れていないCMをはさんでも
# つながるように、15〜30秒のCM1本分）
BLOCK_GAP = 30
# 単独でこれより短い塊（ジングルなど）はCMとみなさない
MIN_CM_SECONDS = 12
SLOT_MINUTES = 15
# 録音自身のタイムフリー版を探す範囲（ファイル名の時刻の前後）と、同じ放送とみなす時刻のずれ
SELF_SEARCH_MINUTES = 10
SELF_TOLERANCE_SECONDS = 10
# 録音のうちこの割合以上がタイムフリー版と一致すれば、その位置合わせを使う
MIN_SELF_RATIO = 0.2
# チャプターの境目を、CMの塊の前後にこの秒数だけ広げる（一致の端は少し内側に出るため）
CHAPTER_PAD = 0.5
# 放送からこの日数が経ったら、後の日の指紋もそろったものとして、もう一度付け直す
SECOND_PASS_DAYS = 3

DEFAULT_SETTINGS = {
    'cm_chapter_auto': True,
}

_FILENAME_TIME = re.compile(r"(\d{8})_(\d{4})")


# --- 指紋 ---

def fingerprint_pcm(pcm):
    """8kHzモノラルs16のPCM（numpy配列）から、utils.cm_fingerprint.FingerprintBuilder と
    同じ方式の指紋と音量を作る（numpy で一度に計算する）

    Returns:
        (ndarray uint32, ndarray uint8): フレームごとの指紋と音量（dBFS+100）
    """
    if len(pcm) < FRAME_SIZE:
        return np.zeros(0, dtype=np.uint32), np.zeros(0, dtype=np.uint8)
    k = np.arange(FRAME_SIZE)
    window = 0.5 - 0.5 * np.cos(2 * np.pi * k / (FRAME_SIZE - 1))
    bins = _band_bins()
    n = 1 + (len(pcm) - FRAME_SIZE) // HOP_SIZE
    frames = np.lib.stride_tricks.sliding_window_view(pcm.astype(np.float64), FRAME_SIZE)[::HOP_SIZE][:n]
    energies = np.empty((n, BAND_COUNT))
    loudness = np.zeros(n, dtype=np.uint8)
    step = 20000  # 一度に計算するフレーム数（メモリを抑える）
    for s in range(0, n, step):
        f = frames[s:s + step]
        power2 = np.abs(np.fft.rfft(f * window, axis=1)) ** 2
        for b, (lo, hi) in enumerate(bins):
            energies[s:s + step, b] = power2[:, lo:hi].sum(axis=1)
        power = (f ** 2).mean(axis=1)
        with np.errstate(divide="ignore"):
            dbfs = np.where(power > 0, 10 * np.log10(power / 32768.0 ** 2), -100.0)
        loudness[s:s + step] = np.clip(np.round(dbfs + 100), 0, 255).astype(np.uint8)
    diff = energies[:, :-1] - energies[:, 1:]
    bits = (diff[1:] - diff[:-1]) > 0  # 1フレーム目は前のフレームがないので0
    weights = 1 << np.arange(BAND_COUNT - 1, dtype=np.uint64)
    hashes = np.zeros(n, dtype=np.uint32)
    hashes[1:] = (bits.astype(np.uint64) * weights).sum(axis=1).astype(np.uint32)
    return hashes, loudness


def decode_pcm(path):
    """音声ファイルを、指紋取得と同じ 8kHz モノラル s16 にする"""
    import av

    container = av.open(str(path))
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    chunks = []
    try:
        for frame in container.decode(container.streams.audio[0]):
            for r in resampler.resample(frame):
                chunks.append(np.frombuffer(bytes(r.planes[0])[:r.samples * 2], dtype="<i2"))
        for r in resampler.resample(None):
            chunks.append(np.frombuffer(bytes(r.planes[0])[:r.samples * 2], dtype="<i2"))
    finally:
        container.close()
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype="<i2")


def read_fp(path):
    data = Path(path).read_bytes()
    magic, _version, _, frames, rate, hop = _FP_HEADER.unpack_from(data)
    if magic != b"RFP1" or rate != SAMPLE_RATE or hop != HOP_SIZE:
        raise ValueError(f"指紋ファイルの形式が違います: {path}")
    offset = _FP_HEADER.size
    hashes = np.frombuffer(data, dtype="<u4", count=frames, offset=offset)
    loudness = np.frombuffer(data, dtype="u1", count=frames, offset=offset + frames * 4)
    return hashes, loudness


# --- 照合用データ ---

class Reference:
    """1局の、指定した期間の指紋を値の順に並べた索引"""

    def __init__(self, base_dir, station_id, first_day, last_day):
        base_dir = Path(base_dir)
        index_file = base_dir / "index.json"
        index = (read_json(index_file) or {}) if index_file.exists() else {}
        self.files = []
        hashes, valid = [], []
        pos = 0
        for key in sorted(index):
            entry = index[key]
            st, bday, hour = key.split("|")
            if st != station_id or entry.get('status') != 'done':
                continue
            day = datetime.strptime(bday, "%Y%m%d").date()
            if not first_day <= day <= last_day:
                continue
            path = base_dir / st / bday / f"{hour}.fp"
            try:
                h, loud = read_fp(path)
            except (OSError, ValueError, struct.error):
                continue
            ok = loud >= SILENT_LOUDNESS
            for a, b in entry.get('off_air') or []:
                ok[int(a * FPS):int(b * FPS)] = False
            start = datetime.combine(day, datetime.min.time()) + timedelta(hours=int(hour))
            self.files.append({'start_pos': pos, 'start_dt': start, 'frames': len(h)})
            hashes.append(h)
            valid.append(ok)
            hashes.append(np.zeros(FILE_GAP, dtype=np.uint32))
            valid.append(np.zeros(FILE_GAP, dtype=bool))
            pos += len(h) + FILE_GAP
        self.size = pos
        self.starts = np.array([f['start_pos'] for f in self.files], dtype=np.int64)
        if not self.files:
            self.h = np.zeros(0, dtype=np.uint32)
            self.pos = np.zeros(0, dtype=np.int32)
            return
        all_hashes = np.concatenate(hashes)
        positions = np.flatnonzero(np.concatenate(valid)).astype(np.int32)
        del hashes, valid
        h = all_hashes[positions]
        del all_hashes
        order = np.argsort(h, kind="stable")
        h = h[order]
        positions = positions[order]
        del order
        # ありふれた値は除く
        starts = np.concatenate(([0], np.flatnonzero(np.diff(h)) + 1))
        counts = np.diff(np.concatenate((starts, [len(h)])))
        keep = np.repeat(counts <= MAX_OCCURRENCES, counts)
        self.h = h[keep]
        self.pos = positions[keep]

    def __bool__(self):
        return bool(self.files)

    def when(self, pos):
        """索引の位置 pos の放送時刻"""
        i = int(np.searchsorted(self.starts, pos, side="right") - 1)
        f = self.files[i]
        return f['start_dt'] + timedelta(seconds=(pos - f['start_pos']) / FPS)


def _expand(lo, cnt):
    """各問い合わせ i について lo[i] から cnt[i] 個の添字を並べる"""
    rep = np.repeat(np.arange(len(lo)), cnt)
    first = np.repeat(np.cumsum(cnt) - cnt, cnt)
    return rep, lo[rep] + (np.arange(len(rep)) - first)


def query(ref, hashes, valid):
    """録音の指紋を索引に問い合わせ、一致した位置の組を返す

    録音側の位置は、索引と重ならないよう base 以降に置く。

    Returns:
        (pa, pb, base): pa は録音側（base + フレーム番号）、pb は索引側の位置
    """
    base = ref.size + 10 ** 6
    qpos = np.flatnonzero(valid)
    qh = hashes[qpos]
    pa, pb = [], []
    for mask in [0] + [1 << b for b in range(32)]:
        q = qh ^ np.uint32(mask)
        lo = np.searchsorted(ref.h, q, side="left")
        hi = np.searchsorted(ref.h, q, side="right")
        cnt = hi - lo
        has = np.flatnonzero(cnt)
        qi, ti = _expand(lo[has], cnt[has])
        pa.append(qpos[has][qi].astype(np.int64) + base)
        pb.append(ref.pos[ti].astype(np.int64))
    return np.concatenate(pa), np.concatenate(pb), base


def find_runs(pa, pb):
    """時間差ごとに並べ、まとまって一致している区間を返す

    Returns:
        ndarray: 行ごとに [pa の始まり, pa の終わり, 時間差(pb - pa), 一致数]
    """
    if len(pa) == 0:
        return np.zeros((0, 4), dtype=np.int64)
    delta = pb - pa
    bucket = delta // DELTA_BUCKET
    order = np.lexsort((pa, bucket))
    bucket, pa_s, delta_s = bucket[order], pa[order], delta[order]
    new_run = np.ones(len(pa_s), dtype=bool)
    new_run[1:] = (bucket[1:] != bucket[:-1]) | (pa_s[1:] - pa_s[:-1] > RUN_GAP)
    run_id = np.cumsum(new_run) - 1
    n = run_id[-1] + 1
    hits = np.bincount(run_id, minlength=n)
    start = pa_s[new_run]
    end = np.maximum.reduceat(pa_s, np.flatnonzero(new_run))
    dmed = np.round(np.bincount(run_id, weights=delta_s, minlength=n) / hits).astype(np.int64)
    runs = np.stack([start, end, dmed, hits], axis=1)
    ok = (hits >= RUN_MIN_HITS) & ((end - start) >= RUN_MIN_SECONDS * FPS)
    return runs[ok]


# --- 判定 ---

def detect_cm_blocks(ref, hashes, loudness, guess_start):
    """録音の指紋からCMの塊を探す

    Args:
        guess_start (datetime): 録音の先頭のおおよその放送時刻（ファイル名の時刻）

    Returns:
        dict: 'blocks'（[{'kind': 'CM'|'曲', 'start': 秒, 'end': 秒}]）、
        'rec_start'（位置合わせした先頭の放送時刻）、'self_ratio'、'counts'（一致の内訳）
    """
    valid = loudness >= SILENT_LOUDNESS
    pa, pb, base = query(ref, hashes, valid)
    runs = find_runs(pa, pb)
    n = len(hashes)
    result = {'blocks': [], 'rec_start': guess_start, 'self_ratio': 0.0,
              'counts': {'自分': 0, 'CM根拠': 0, '同じ時刻': 0, 'ループ': 0}}
    if len(runs) == 0 or n == 0:
        return result
    density = runs[:, 3] / (runs[:, 1] - runs[:, 0] + 1)
    partner_dt = [ref.when(int(a0 + d)) for a0, _, d, _ in runs]

    # 録音自身のタイムフリー版を探して、録音の先頭の放送時刻を正確に合わせる
    rec_start = guess_start
    near = [
        i for i, (a0, dt) in enumerate(zip(runs[:, 0], partner_dt))
        if abs((dt - (guess_start + timedelta(seconds=(a0 - base) / FPS))).total_seconds())
        < SELF_SEARCH_MINUTES * 60
    ]
    if near:
        # 録音の先頭に当たる放送時刻の候補ごとに、一致したフレーム数を数える
        offsets = {}
        for i in near:
            a0, a1 = runs[i, 0], runs[i, 1]
            start_dt = partner_dt[i] - timedelta(seconds=(a0 - base) / FPS)
            key = round(start_dt.timestamp() / SELF_TOLERANCE_SECONDS)
            frames, best = offsets.get(key, (0, None))
            if best is None or runs[i, 3] > runs[best, 3]:
                best = i
            offsets[key] = (frames + (a1 - a0), best)
        frames, best = max(offsets.values(), key=lambda v: v[0])
        if frames / n >= MIN_SELF_RATIO:
            rec_start = partner_dt[best] - timedelta(seconds=(runs[best, 0] - base) / FPS)
            result['self_ratio'] = min(1.0, frames / n)
    result['rec_start'] = rec_start

    counts = result['counts']
    cover = np.zeros(n, dtype=np.int32)
    longest = np.zeros(n, dtype=np.int32)
    for (a0, a1, _d, _hits), dens, dt in zip(runs, density, partner_dt):
        if dens > LOOP_DENSITY:
            counts['ループ'] += 1
            continue
        rec_dt = rec_start + timedelta(seconds=(a0 - base) / FPS)
        diff = abs((dt - rec_dt).total_seconds())
        if diff < SELF_TOLERANCE_SECONDS:
            counts['自分'] += 1
            continue
        s, e = int(a0 - base), int(a1 - base) + 1
        longest[s:e] = np.maximum(longest[s:e], e - s)
        time_of_day = diff % 86400
        if min(time_of_day, 86400 - time_of_day) < SLOT_MINUTES * 60:
            counts['同じ時刻'] += 1
        else:
            counts['CM根拠'] += 1
            cover[s:e] += 1

    idx = np.flatnonzero(cover > 0)
    regions = []
    if len(idx):
        for g in np.split(idx, np.flatnonzero(np.diff(idx) > REGION_GAP * FPS) + 1):
            s, e = int(g[0]), int(g[-1]) + 1
            regions.append({'start': s / FPS, 'end': e / FPS, 'longest': longest[s:e].max() / FPS})
    result['blocks'] = merge_blocks(regions)
    return result


def merge_blocks(regions):
    """繰り返し部分を「CMの塊」と「曲」にまとめる"""
    out = []
    for r in regions:
        kind = "曲" if r['longest'] >= SONG_SECONDS else "CM"
        if (out and kind == "CM" and out[-1]['kind'] == "CM"
                and r['start'] - out[-1]['end'] <= BLOCK_GAP):
            out[-1]['end'] = r['end']
            continue
        out.append({'kind': kind, 'start': r['start'], 'end': r['end']})
    return [b for b in out if b['kind'] == "曲" or b['end'] - b['start'] >= MIN_CM_SECONDS]


def format_offset(seconds):
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def chapters_for(blocks, total):
    """「番組」と「CM」が交互に並ぶチャプター [(始まり秒, 終わり秒, 名前)] を作る

    曲は番組の一部として扱う。CMが1つもなければ空にする。
    """
    cms = [b for b in blocks if b['kind'] == "CM"]
    if not cms:
        return []
    marks = []
    t = 0.0
    for n, b in enumerate(cms, 1):
        s = max(0.0, b['start'] - CHAPTER_PAD)
        e = min(total, b['end'] + CHAPTER_PAD)
        if s - t >= 1:
            marks.append((t, f"番組 {format_offset(t)}"))
        marks.append((s, f"CM{n}（{e - s:.0f}秒）"))
        t = e
    if total - t >= 1:
        marks.append((t, f"番組 {format_offset(t)}"))
    return [
        (s, marks[i + 1][0] if i + 1 < len(marks) else total, title)
        for i, (s, title) in enumerate(marks)
    ]


# --- ファイルへの書き込み ---

def write_chapters(path, chapters):
    """m4a のチャプターを置き換える（音声はそのままコピーし、タグも元のファイルから写す）

    同じフォルダに作った一時ファイルで置き換える。更新日時は元のまま残す
    （保存先フォルダの一覧の並び順が変わらないように）。
    """
    import av
    from mutagen.mp4 import MP4

    path = Path(path)
    tmp = path.with_name(f".{path.stem}.cmtmp.m4a")
    stat = path.stat()
    try:
        src = av.open(str(path))
        try:
            out = av.open(str(tmp), "w", format="ipod")
            try:
                in_stream = src.streams.audio[0]
                out_stream = out.add_stream_from_template(in_stream)
                tb = fractions.Fraction(1, 1000)
                out.set_chapters([
                    {'id': i + 1, 'start': int(s * 1000), 'end': int(e * 1000), 'time_base': tb,
                     'metadata': {'title': title}}
                    for i, (s, e, title) in enumerate(chapters)
                ])
                for packet in src.demux(in_stream):
                    if packet.dts is None:
                        continue
                    packet.stream = out_stream
                    out.mux(packet)
            finally:
                out.close()
        finally:
            src.close()
        original, copied = MP4(str(path)), MP4(str(tmp))
        if copied.tags is None:
            copied.add_tags()
        for key, value in (original.tags or {}).items():
            copied.tags[key] = value
        copied.save()
        os.utime(tmp, (stat.st_atime, stat.st_mtime))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def read_chapter_count(path):
    import av

    container = av.open(str(path))
    try:
        return len(container.chapters())
    finally:
        container.close()


# --- ファイルの情報 ---

def broadcast_start_from_name(path):
    """ファイル名の「YYYYMMDD_HHMM」から録音を始めた時刻を読む（読めなければ None）"""
    m = _FILENAME_TIME.search(Path(path).name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
    except ValueError:
        return None


def station_from_tags(path):
    """録音時に埋め込んだアルバム名（局名）を読む"""
    from mutagen.mp4 import MP4

    try:
        tags = MP4(str(path)).tags or {}
    except Exception:
        return None
    values = tags.get('\xa9alb')
    return str(values[0]) if values else None


# --- バックグラウンド処理 ---

class CmChapterer:
    """録音ファイルへのCMチャプター付けを1件ずつ順に行う

    状態（どのファイルにいつ付けたか）は config/cm_chapters.json に残し、放送から
    SECOND_PASS_DAYS 日経ったファイルにはもう一度付け直す（due_paths）。
    on_finished(path, record) は処理スレッドから呼ばれる。
    """

    def __init__(self, manager, log_dir, base_dir=None, on_finished=None):
        self.manager = manager
        self.base_dir = Path(base_dir) if base_dir else manager.cache_dir / "fingerprints"
        self.state_file = manager.cache_dir / "cm_chapters.json"
        self.log = _setup_fp_logger(Path(log_dir))
        self.on_finished = on_finished
        self.settings = dict(DEFAULT_SETTINGS)
        self._queue = queue.Queue()
        self._queued = set()
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._status = "待機中"

    def update_settings(self, settings):
        for key in DEFAULT_SETTINGS:
            if key in settings:
                self.settings[key] = settings[key]

    def get_status(self):
        with self._lock:
            return {'message': self._status, 'queued': len(self._queued)}

    def request(self, path, reason):
        """path にチャプターを付けるよう順番待ちに入れる。Returns: 入れたか"""
        path = Path(path)
        if path.suffix.lower() != ".m4a":
            return False
        key = str(path.resolve())
        with self._lock:
            if key in self._queued:
                return False
            self._queued.add(key)
            # 処理スレッドは順番待ちが空になると終わるため、入れるのと起こすのを同じロックの中で行う
            self._queue.put((key, reason))
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._loop, name="cm-chapters", daemon=True)
                self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def load_state(self):
        if not self.state_file.exists():
            return {}
        try:
            return read_json(self.state_file) or {}
        except Exception:
            self.log.exception("cm_chapters.json を読めませんでした。空として扱います")
            return {}

    def due_paths(self, now=None):
        """放送から SECOND_PASS_DAYS 日経ち、まだ付け直していないファイル"""
        now = now or datetime.now()
        due = []
        for key, record in self.load_state().items():
            if record.get('final') or record.get('result') == 'skipped':
                continue
            try:
                broadcast = datetime.fromisoformat(record['broadcast'])
            except (KeyError, ValueError):
                continue
            if now - broadcast >= timedelta(days=SECOND_PASS_DAYS) and Path(key).exists():
                due.append(key)
        return due

    def _set_status(self, message):
        with self._lock:
            self._status = message

    def _loop(self):
        while not self._stop.is_set():
            try:
                key, reason = self._queue.get(timeout=5)
            except queue.Empty:
                with self._lock:
                    if self._queue.empty():
                        self._thread = None
                        self._status = "待機中"
                        return
                continue
            try:
                record = self.process(key, reason)
            except Exception as e:
                self.log.exception(f"CMチャプター付けでエラー: {key}")
                record = {'result': 'error', 'error': f"{type(e).__name__}: {e}"}
            with self._lock:
                self._queued.discard(key)
            if self.on_finished:
                try:
                    self.on_finished(key, record)
                except Exception:
                    logger.exception("CMチャプター付けの完了通知でエラーが発生しました")

    def process(self, path, reason):
        """1ファイルにチャプターを付け、状態を記録して返す"""
        path = Path(path)
        name = path.name
        self._set_status(f"処理中: {name}")
        self.log.info(f"CMチャプター: 開始 {name}（{reason}）")
        now = datetime.now()
        record = {'last_run': now.isoformat(timespec="seconds"), 'reason': reason}
        if not path.exists():
            record.update(result='skipped', detail="ファイルがありません")
            # 無いファイルの記録は残さない（キーにするパスが存在しないため）。ログだけ出す
            self.log.info(f"CMチャプター: 対象外 {name}（ファイルがありません。移動・名前の変更・削除された可能性）")
            return record

        station = station_from_tags(path)
        station_id = self.manager.station_mapping.get(station) if station else None
        guess = broadcast_start_from_name(path)
        if guess is None:
            guess = datetime.fromtimestamp(path.stat().st_mtime)
        record.update(station=station, broadcast=guess.isoformat(timespec="seconds"))
        if not station or is_nhk_station(station) or not station_id:
            why = "NHKはCMがないため対象外" if station and is_nhk_station(station) else "局が分かりません"
            record.update(result='skipped', detail=why)
            self._save_record(path, record)
            self.log.info(f"CMチャプター: 対象外 {name}（{why}）")
            return record

        ref = Reference(self.base_dir, station_id, guess.date() - timedelta(days=REFERENCE_DAYS),
                        guess.date() + timedelta(days=REFERENCE_DAYS))
        if not ref:
            record.update(result='no_data', detail="この局・この時期の指紋がありません")
            self._save_record(path, record)
            self.log.info(f"CMチャプター: 指紋なし {name}（{station} {guess:%m/%d}前後）")
            return record
        if self._stop.is_set():
            record.update(result='stopped')
            return record

        pcm = decode_pcm(path)
        hashes, loudness = fingerprint_pcm(pcm)
        total = len(pcm) / SAMPLE_RATE
        del pcm
        detected = detect_cm_blocks(ref, hashes, loudness, guess)
        del ref
        chapters = chapters_for(detected['blocks'], total)
        cms = [b for b in detected['blocks'] if b['kind'] == "CM"]
        self.log.info(
            f"CMチャプター: {name} 先頭={detected['rec_start']:%m/%d %H:%M:%S} "
            f"（タイムフリー版と{detected['self_ratio']:.0%}一致） 一致の内訳 {detected['counts']}"
        )
        for b in detected['blocks']:
            self.log.info(
                f"    {b['kind']} {format_offset(b['start'])}〜{format_offset(b['end'])} "
                f"{b['end'] - b['start']:.0f}秒"
            )
        if chapters or read_chapter_count(path):
            write_chapters(path, chapters)
        record.update(
            result='done' if cms else 'no_cm', cms=len(cms),
            broadcast=detected['rec_start'].isoformat(timespec="seconds"),
        )
        self._save_record(path, record)
        logger.info(f"CMチャプターを付けました: {name}（CM {len(cms)}か所）")
        return record

    def _save_record(self, path, record):
        key = str(Path(path).resolve())
        state = self.load_state()
        previous = state.get(key) or {}
        record['passes'] = previous.get('passes', 0) + (1 if record.get('result') in ('done', 'no_cm') else 0)
        # 放送から SECOND_PASS_DAYS 日経ってから処理したものは、結果（指紋がなかった場合も）に
        # かかわらず付け直さない（古い録音を何度も処理し直さないように）
        broadcast = datetime.fromisoformat(record['broadcast'])
        record['final'] = datetime.fromisoformat(record['last_run']) - broadcast >= timedelta(days=SECOND_PASS_DAYS)
        state[key] = record
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.state_file, state)
