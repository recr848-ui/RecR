import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from utils.radiko_manager import RadikoManager


@pytest.fixture
def manager(monkeypatch, tmp_path):
    """ネットワーク呼び出し（起動時のエリア判定）を行わない RadikoManager インスタンス。

    キャッシュ・出力先ディレクトリはテストごとに使い捨てのtmp_pathに向ける。
    """
    monkeypatch.setattr(RadikoManager, "_detect_area_stations", lambda self: None)
    m = RadikoManager()
    m.output_dir = tmp_path / "output"
    m.output_dir.mkdir(parents=True, exist_ok=True)
    m.cache_dir = tmp_path / "config"
    m.cache_dir.mkdir(parents=True, exist_ok=True)
    m.cache_file = m.cache_dir / "schedule_cache.json"
    m.settings_file = m.cache_dir / "settings.json"
    m.reservations_file = m.cache_dir / "reservations.json"
    m.freeword_file = m.cache_dir / "freeword_keywords.json"
    return m
