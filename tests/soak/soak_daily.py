"""毎日1回の全局番組表更新（全局ぶん save_schedule_cache）を何日分も繰り返し、
番組表データの入れ替えでメモリが伸びないかを測る。ネットワークには出ない。
"""
import gc
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import psutil

from utils.radiko_manager import RadikoManager

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 6
PROC = psutil.Process()
tmp = Path(tempfile.mkdtemp(prefix="recr_daily_"))
shutil.copy(REPO / "config" / "schedule_cache.json", tmp / "schedule_cache.json")

RadikoManager._detect_area_stations = lambda self: None
manager = RadikoManager()
manager.cache_dir = tmp
manager.cache_file = tmp / "schedule_cache.json"


def priv():
    gc.collect()
    return PROC.memory_info().private / 1e6


keys = list(manager._load_cache_file().keys())
print(f"keys={len(keys)} after first load: {priv():.1f}MB", flush=True)
for day in range(1, DAYS + 1):
    t0 = time.time()
    for key in keys:
        # ネットワークから取り直した想定で、毎回まったく新しいオブジェクトに入れ替える
        fresh = json.loads(json.dumps(manager.load_cached_schedule(key)))
        manager.save_schedule_cache(key, fresh)
    print(f"day {day}: private={priv():7.1f}MB ({time.time() - t0:.0f}s)", flush=True)
shutil.rmtree(tmp, ignore_errors=True)
