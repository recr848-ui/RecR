"""録音・再生のセグメント処理（PyAV）を、合成したAACセグメントで大量に回して
ネイティブ側のメモリの伸びを測るソークテスト。ネットワークには出ない。
"""
import gc
import io
import math
import random
import sys
import tempfile
import threading
import time
from array import array
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import av
import psutil

from utils import radiko_manager as rm
from utils.radiko_manager import RadikoManager

N = int(sys.argv[1]) if len(sys.argv) > 1 else 6000
PROC = psutil.Process()
RadikoManager._detect_area_stations = lambda self: None


def make_segment(seed, seconds=5, rate=48000):
    """radikoのセグメント相当（ADTS AAC, 48kHz stereo, 約5秒）を合成する"""
    rnd = random.Random(seed)
    freq = rnd.choice([220, 440, 880, 1320])
    buf = io.BytesIO()
    out = av.open(buf, mode="w", format="adts")
    stream = out.add_stream("aac", rate=rate)
    stream.layout = "stereo"
    stream.bit_rate = 48000
    total = seconds * rate
    pos = 0
    while pos < total:
        n = min(1024, total - pos)
        pcm = array("h")
        for i in range(n):
            v = int(8000 * math.sin(2 * math.pi * freq * (pos + i) / rate) + rnd.randint(-500, 500))
            pcm.extend((v, v))
        frame = av.AudioFrame(format="s16", layout="stereo", samples=n)
        frame.planes[0].update(pcm.tobytes())
        frame.sample_rate = rate
        frame.pts = pos
        for packet in stream.encode(frame):
            out.mux(packet)
        pos += n
    for packet in stream.encode(None):
        out.mux(packet)
    out.close()
    return buf.getvalue()


def priv():
    gc.collect()
    return PROC.memory_info().private / 1e6


segments = [make_segment(s) for s in range(6)]
print(f"segments: {[len(s) for s in segments]} bytes, N={N} (~{N * 5 / 3600:.1f}時間分)")

tmp = Path(tempfile.mkdtemp(prefix="recr_av_"))
manager = RadikoManager()
manager.output_dir = tmp


def seg_iter(n, marks, label):
    for i in range(n):
        yield segments[i % len(segments)]
        if (i + 1) % (n // 5) == 0:
            marks.append(priv())
            print(f"  {label} seg {i + 1:6d} private={marks[-1]:8.1f}MB", flush=True)


for fmt in ("aac", "m4a", "mp3"):
    marks = []
    start = priv()
    t0 = time.time()
    done = {}
    n_fmt = N // 6 if fmt == "mp3" else N  # mp3はエンコードが遅いため本数を減らす
    manager._recording_worker(
        seg_iter(n_fmt, marks, fmt), threading.Event(), tmp / f"soak.{fmt}", fmt, 192, None, "soak",
        on_complete=lambda had, path, err: done.update(had=had, err=err),
    )
    print(f"{fmt}: {done} {time.time() - t0:.0f}s  start={start:.1f}MB "
          f"1/5地点→終了: {marks[-1] - marks[0]:+.1f}MB  終了後={priv():.1f}MB")
    (tmp / f"soak.{fmt}").unlink(missing_ok=True)

# ---- 短い録音を多数回（予約録音が毎日何本も走る状況: コンテナ/リサンプラの生成・破棄） ----
start = priv()
for i in range(400):
    for fmt in ("aac", "m4a", "mp3"):
        manager._recording_worker(
            iter(segments[:1]), threading.Event(), tmp / f"short.{fmt}", fmt, 192, None, "soak")
    if (i + 1) % 100 == 0:
        print(f"  短い録音 {(i + 1) * 3}本 private={priv():8.1f}MB", flush=True)
print(f"短い録音 1200本:{priv() - start:+.1f}MB (スレッド数={threading.active_count()})")

# ---- ライブ再生の取得側（出力側は実時間を待たず即消費） ----
manager._iter_live_segments = lambda url, headers, stop: (segments[i % 6] for i in range(N))
buffer = rm._LiveAudioBuffer()
stop = threading.Event()


def drain():
    while True:
        pcm = buffer.pop(timeout=30)
        if pcm is None:
            return
        manager._update_levels(pcm, 48000)


consumer = threading.Thread(target=drain, daemon=True)
start = priv()
consumer.start()
manager._playback_fetch_worker("x", "t", stop, buffer)
consumer.join()
print(f"再生fetch {N}セグメント(消費あり): {priv() - start:+.1f}MB")

# ---- 出力側が死んで取得側だけ動き続けた場合（消費なし）のバッファの伸び ----
buffer = rm._LiveAudioBuffer()
manager._iter_live_segments = lambda url, headers, stop: (segments[i % 6] for i in range(720))
start = priv()
manager._playback_fetch_worker("x", "t", threading.Event(), buffer)
print(f"再生fetch 720セグメント=1時間分(消費なし): {priv() - start:+.1f}MB  "
      f"buffered={buffer.buffered_seconds():.0f}s")
