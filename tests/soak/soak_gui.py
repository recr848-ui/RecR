"""GUIの定期処理（毎分の番組表再描画・予約一覧更新・15秒ごとの予約チェック）を
実時間を待たずに連続実行し、メモリの伸びを測るソークテスト。

実データ(config/)を一時ディレクトリへコピーして使い、ネットワークには一切出ない。
"""
import gc
import shutil
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import json
import tkinter as tk
from tkinter import messagebox

import psutil

from utils.radiko_manager import RadikoManager
from utils.radiru_manager import RadiruManager
from src.main import RecRApp

TICKS = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
PROC = psutil.Process()

tmp = Path(tempfile.mkdtemp(prefix="recr_soak_"))
cfg = tmp / "config"
shutil.copytree(REPO / "config", cfg, ignore=shutil.ignore_patterns("logs"))

cache = json.loads((cfg / "schedule_cache.json").read_text(encoding="utf-8"))
station_names = [k for k in cache if not k.endswith("::timefree")]
del cache

RadikoManager._detect_area_stations = lambda self: {n: n for n in station_names}
RadikoManager.get_program_schedule = lambda self, station, days=10: []
RadikoManager.get_sample_schedule = lambda self, station, days=10: []
RadikoManager.get_timefree_schedule = lambda self, station: []
RadikoManager.start_recording = lambda self, *a, **k: (False, None)
for name in ("askyesno", "askokcancel"):
    setattr(messagebox, name, lambda *a, **k: False)
for name in ("showwarning", "showerror", "showinfo"):
    setattr(messagebox, name, lambda *a, **k: None)
for name in ("_start_full_schedule_refresh", "_refresh_notice", "_refresh_stale_stations",
             "_check_full_schedule_refresh_due", "_check_radiru_index_refresh_due"):
    setattr(RecRApp, name, lambda self, *a, **k: None)

_orig_init = RadikoManager.__init__


def _patched_init(self):
    _orig_init(self)
    self.output_dir = tmp / "output"
    self.output_dir.mkdir(parents=True, exist_ok=True)
    self.cache_dir = cfg
    self.cache_file = cfg / "schedule_cache.json"
    self.image_cache_dir = cfg / "images"
    self.settings_file = cfg / "settings.json"
    self.reservations_file = cfg / "reservations.json"
    self.freeword_file = cfg / "freeword_keywords.json"
    self.export_file = cfg / "settings_export.json"


RadikoManager.__init__ = _patched_init
_orig_radiru_init = RadiruManager.__init__


def _patched_radiru_init(self):
    _orig_radiru_init(self)
    self.cache_dir = cfg
    self.index_file = cfg / "radiru_series_index.json"


RadiruManager.__init__ = _patched_radiru_init


def mem():
    gc.collect()
    info = PROC.memory_info()
    return info.rss / 1e6, info.private / 1e6


def report(label):
    rss, priv = mem()
    print(f"{label:<34} rss={rss:8.1f}MB private={priv:8.1f}MB "
          f"wrap_cache={len(app._wrap_cache):6d} image_cache={len(app._image_cache):4d}", flush=True)
    return priv


root = tk.Tk()
root.withdraw()  # トレイ常駐中と同じ状態
app = RecRApp(root)
for attr in ("_reservation_check_job", "_reservation_list_refresh_job", "_program_guide_refresh_job"):
    job = getattr(app, attr, None)
    if job:
        root.after_cancel(job)
root.update()

print(f"stations={len(station_names)} current={app.schedule_station_var.get()} "
      f"programs={len(app._current_programs)} canvas_items={len(app.schedule_canvas.find_all())}")
report("startup")


def minute_tick():
    """実運用の1分間に相当する定期処理"""
    for _ in range(4):
        app._check_due_reservations()
        app._update_sleep_prevention()
    app._refresh_reservation_list()  # 内部で display_schedule も呼ぶ
    app.display_schedule(app._current_programs, mode=app.schedule_mode_var.get())
    root.update()


# ---- フェーズ1: 何も操作せず放置（毎分の定期処理のみ） ----
for _ in range(50):
    minute_tick()
base = report("phase1 warmup (50 ticks)")
tracemalloc.start(15)
snap0 = tracemalloc.take_snapshot()
for _ in range(120):
    minute_tick()
snap1 = tracemalloc.take_snapshot()
tracemalloc.stop()
print("  tracemalloc: 120 ticksでのPythonヒープ増分 上位:")
for stat in snap1.compare_to(snap0, "lineno")[:8]:
    print(f"    {stat.size_diff / 1e3:+9.1f}KB count{stat.count_diff:+6d}  {stat.traceback[0]}")
del snap0, snap1
base = report("phase1 after tracemalloc")
t0 = time.time()
samples = []
for i in range(1, TICKS + 1):
    minute_tick()
    if i % max(TICKS // 6, 1) == 0:
        samples.append((i, report(f"phase1 tick {i} (~{i / 1440:.1f}日分)")))
print(f"  {TICKS} ticks in {time.time() - t0:.0f}s")
growth = samples[-1][1] - base
print(f"  private growth over {TICKS} ticks: {growth:+.1f}MB "
      f"({growth / TICKS * 1440:+.2f}MB/日 相当)")

# ---- フェーズ2: 局・表示モードを切り替えて回る（_wrap_cacheの伸び） ----
for cycle in range(1, 4):
    for station in station_names:
        for mode, key in (("upcoming", station), ("timefree", f"{station}::timefree")):
            programs = app.manager.load_cached_schedule(key) or []
            app.schedule_station_var.set(station)
            app.schedule_mode_var.set(mode)
            app.display_schedule(programs, mode=mode)
            root.update()
    report(f"phase2 全局巡回 {cycle}周目")

wrap_bytes = sum(
    sys.getsizeof(k) + sys.getsizeof(k[4]) + sys.getsizeof(v) + sum(sys.getsizeof(s) for s in v)
    for k, v in app._wrap_cache.items()
)
print(f"  _wrap_cache: {len(app._wrap_cache)} entries ~= {wrap_bytes / 1e6:.1f}MB (dict本体除く)")

# ---- フェーズ3: ツールチップ画像（_image_cacheの伸び、ディスクキャッシュ済み画像のみ） ----
app.manager.get_image = lambda url: (
    app.manager._image_cache_path(url).read_bytes()
    if url and app.manager._image_cache_path(url).exists() else None
)
urls = []
for station in station_names:
    for p in app.manager.load_cached_schedule(station) or []:
        if p.get("img"):
            urls.append(p["img"])
urls = list(dict.fromkeys(urls))
before = report("phase3 before images")
loaded = 0
for url in urls:
    if app._get_program_image(url) is not None:
        loaded += 1
after = report(f"phase3 {loaded}枚ロード後")
if loaded:
    print(f"  画像1枚あたり ~{(after - before) / loaded * 1000:.0f}KB "
          f"(番組表内のユニーク画像URL: {len(urls)})")

root.destroy()
shutil.rmtree(tmp, ignore_errors=True)
