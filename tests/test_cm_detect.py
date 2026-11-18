import json
import math
import os
from array import array
from datetime import datetime, timedelta
from types import SimpleNamespace

import av
import numpy as np
import pytest
from mutagen.mp4 import MP4

from utils import cm_detect
from utils.cm_detect import (
    CmChapterer, Reference, chapters_for, detect_cm_blocks, fingerprint_pcm, read_chapter_count,
    write_chapters,
)
from utils.cm_fingerprint import FingerprintBuilder, write_fp_file

FPS = cm_detect.FPS
FILE_FRAMES = int(300 * FPS)  # テスト用の指紋ファイルは1件5分
RNG = np.random.default_rng(1)


def _random_hashes(n):
    return RNG.integers(0, 2 ** 32, n, dtype=np.uint64).astype(np.uint32)


def _noisy(hashes, ber=0.05):
    """別の放送で同じ音が流れたときのように、ビットを ber の割合で食い違わせる（2回分を比べると約1割食い違う。実際のデータと同じくらい）"""
    flips = RNG.random((len(hashes), 32)) < ber
    mask = (flips.astype(np.uint64) << np.arange(32, dtype=np.uint64)).sum(axis=1).astype(np.uint32)
    return hashes ^ mask


def _write_station_day(base, station_id, day, hour, hashes, index):
    bday = day.strftime("%Y%m%d")
    write_fp_file(base / station_id / bday / f"{hour:02d}.fp", hashes.tolist(), bytes([80] * len(hashes)))
    index[f"{station_id}|{bday}|{hour:02d}"] = {'status': 'done'}


CM = _random_hashes(int(30 * FPS))
OPENING = _random_hashes(int(40 * FPS))


def _fingerprint_data(tmp_path):
    """CM は別の日の別の時刻に、オープニングは毎日13:00ちょうどに流れる、という指紋データ"""
    base = tmp_path / "fingerprints"
    index = {}
    for offset, hour in [(0, 13), (1, 13), (0, 18), (1, 18)]:
        day = datetime(2026, 10, 1) + timedelta(days=offset)
        h = _random_hashes(FILE_FRAMES)
        if hour == 13:
            h[:len(OPENING)] = _noisy(OPENING)
        if (offset, hour) in [(0, 18), (1, 18)]:
            at = int(100 * FPS)
            h[at:at + len(CM)] = _noisy(CM)
        _write_station_day(base, "TBS", day, hour, h, index)
    # 録音の日（10/3 13時）のタイムフリー版
    rec = _random_hashes(FILE_FRAMES)
    rec[:len(OPENING)] = _noisy(OPENING)
    at = int(120 * FPS)
    rec[at:at + len(CM)] = _noisy(CM)
    _write_station_day(base, "TBS", datetime(2026, 10, 3), 13, _noisy(rec, 0.03), index)
    (base / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return base, rec


def test_fingerprint_pcm_matches_fingerprint_builder():
    rate = 8000
    samples = [
        int(6000 * math.sin(2 * math.pi * (300 + 50 * (i // 4000)) * i / rate) + 500 * math.sin(i * 1.7))
        for i in range(rate * 3)
    ]
    builder = FingerprintBuilder()
    builder.feed(array("h", samples).tobytes())
    hashes, loudness = fingerprint_pcm(np.array(samples, dtype=np.int16))
    assert len(hashes) == builder.frame_count
    assert np.array_equal(loudness, np.frombuffer(bytes(builder.loudness), dtype=np.uint8))
    # 計算の細かな誤差で、差がごく小さい帯域のビットだけ食い違うことがある
    assert (hashes == np.array(builder.fingerprints, dtype=np.uint32)).mean() > 0.9


def test_detects_cm_but_not_opening_and_aligns_to_timefree(tmp_path):
    base, rec = _fingerprint_data(tmp_path)
    ref = Reference(base, "TBS", datetime(2026, 9, 25).date(), datetime(2026, 10, 10).date())
    true_start = datetime(2026, 10, 3, 13, 0)
    # ファイル名の時刻は、実際の放送より1分ほど後になる（ライブ配信の遅れ）
    result = detect_cm_blocks(ref, _noisy(rec, 0.02), np.full(len(rec), 80, dtype=np.uint8),
                              true_start + timedelta(seconds=70))
    assert abs((result['rec_start'] - true_start).total_seconds()) < 1
    assert result['self_ratio'] > 0.5
    cms = [b for b in result['blocks'] if b['kind'] == "CM"]
    assert len(cms) == 1
    assert abs(cms[0]['start'] - 120) < 3 and abs(cms[0]['end'] - 150) < 3
    assert result['counts']['同じ時刻'] > 0  # オープニングは同じ時刻の一致として除いた


def test_reference_without_data_is_empty(tmp_path):
    assert not Reference(tmp_path, "TBS", datetime(2026, 10, 1).date(), datetime(2026, 10, 2).date())


def test_chapters_alternate_program_and_cm():
    blocks = [{'kind': "CM", 'start': 60, 'end': 120}, {'kind': "曲", 'start': 200, 'end': 400},
              {'kind': "CM", 'start': 500, 'end': 530}]
    chapters = chapters_for(blocks, 600)
    assert [c[2] for c in chapters] == ["番組 0:00", "CM1（61秒）", "番組 2:00", "CM2（31秒）", "番組 8:50"]
    assert chapters[1][0] == 59.5 and chapters[-1][1] == 600
    assert all(a[1] == b[0] for a, b in zip(chapters, chapters[1:]))
    assert chapters_for([{'kind': "曲", 'start': 0, 'end': 100}], 600) == []


def _make_m4a(path, seconds=6):
    out = av.open(str(path), "w", format="ipod")
    stream = out.add_stream("aac", rate=48000)
    stream.layout = "mono"
    pts = 0
    while pts < 48000 * seconds:
        frame = av.AudioFrame(format="s16", layout="mono", samples=1024)
        frame.planes[0].update(np.array(
            3000 * np.sin(np.arange(pts, pts + 1024) * 0.05), dtype=np.int16
        ).tobytes())
        frame.sample_rate = 48000
        frame.pts = pts
        pts += 1024
        for packet in stream.encode(frame):
            out.mux(packet)
    for packet in stream.encode(None):
        out.mux(packet)
    out.close()
    tags = MP4(str(path))
    if tags.tags is None:
        tags.add_tags()
    tags.tags['\xa9alb'] = ["TBSラジオ"]
    tags.tags['\xa9nam'] = ["テスト番組"]
    tags.save()


def test_write_chapters_keeps_audio_tags_and_mtime(tmp_path):
    path = tmp_path / "20261003_1259_テスト番組.m4a"
    _make_m4a(path)
    os.utime(path, (1_700_000_000, 1_700_000_000))
    write_chapters(path, [(0, 2, "番組 0:00"), (2, 4, "CM1（2秒）"), (4, 6, "番組 0:04")])
    assert read_chapter_count(path) == 3
    tags = MP4(str(path)).tags
    assert tags['\xa9alb'] == ["TBSラジオ"] and tags['\xa9nam'] == ["テスト番組"]
    assert path.stat().st_mtime == 1_700_000_000
    assert [p.name for p in tmp_path.iterdir()] == [path.name]
    container = av.open(str(path))
    assert container.duration / 1e6 == pytest.approx(6, abs=0.2)
    container.close()
    # 付け直すときは置き換わる（増えない）
    write_chapters(path, [(0, 6, "番組 0:00")])
    assert read_chapter_count(path) == 1


def _chapterer(tmp_path, mapping=None):
    manager = SimpleNamespace(cache_dir=tmp_path / "config", station_mapping=mapping or {"TBSラジオ": "TBS"})
    return CmChapterer(manager, tmp_path / "logs", base_dir=tmp_path / "fingerprints")


def test_process_without_fingerprints_records_no_data_and_is_retried_later(tmp_path):
    """録音直後に指紋がなくても、放送から3日後にもう一度試す"""
    chapterer = _chapterer(tmp_path)
    broadcast = datetime.now().replace(second=0, microsecond=0) - timedelta(hours=2)
    path = tmp_path / f"{broadcast:%Y%m%d_%H%M}_テスト番組.m4a"
    _make_m4a(path)
    record = chapterer.process(path, "テスト")
    assert record['result'] == 'no_data' and record['final'] is False
    assert record['broadcast'] == broadcast.isoformat(timespec="seconds")
    assert chapterer.due_paths(broadcast + timedelta(days=2)) == []
    assert chapterer.due_paths(broadcast + timedelta(days=3)) == [str(path.resolve())]


def test_old_recording_without_fingerprints_is_not_retried(tmp_path):
    """放送から3日以上経ってから処理したものは、指紋がなくても繰り返し処理しない"""
    chapterer = _chapterer(tmp_path)
    path = tmp_path / "20261003_1259_テスト番組.m4a"
    _make_m4a(path)
    record = chapterer.process(path, "テスト")
    assert record['result'] == 'no_data' and record['final'] is True
    assert chapterer.due_paths(datetime(2027, 1, 1)) == []


def test_process_skips_nhk(tmp_path):
    chapterer = _chapterer(tmp_path, {"NHK-FM": "JOAK-FM"})
    path = tmp_path / "20261003_1259_テスト番組.m4a"
    _make_m4a(path)
    tags = MP4(str(path))
    tags.tags['\xa9alb'] = ["NHK-FM"]
    tags.save()
    assert chapterer.process(path, "テスト")['result'] == 'skipped'
    assert chapterer.due_paths(datetime(2027, 1, 1)) == []


def test_process_writes_chapters_from_fingerprints(tmp_path, monkeypatch):
    base, rec = _fingerprint_data(tmp_path)
    chapterer = _chapterer(tmp_path)
    path = tmp_path / "20261003_1301_テスト番組.m4a"
    _make_m4a(path, seconds=300)
    # 音声のデコードと指紋づくりは別のテストで確かめているので、ここでは録音の指紋を差し替える
    monkeypatch.setattr(cm_detect, "decode_pcm", lambda p: np.zeros(8000 * 300, dtype=np.int16))
    monkeypatch.setattr(cm_detect, "fingerprint_pcm",
                        lambda pcm: (_noisy(rec, 0.02), np.full(len(rec), 80, dtype=np.uint8)))
    record = chapterer.process(path, "テスト")
    assert record['result'] == 'done' and record['cms'] == 1
    assert record['broadcast'] == "2026-10-03T13:00:00"
    assert record['final'] is True  # 放送から3日以上経っているので付け直しは要らない
    assert read_chapter_count(path) == 3
    state = json.loads(chapterer.state_file.read_text(encoding="utf-8"))
    assert state[str(path.resolve())]['passes'] == 1


def test_request_accepts_only_m4a_once(tmp_path, monkeypatch):
    chapterer = _chapterer(tmp_path)
    done = []
    monkeypatch.setattr(chapterer, "process", lambda path, reason: done.append(path) or {'result': 'done'})
    finished = cm_detect.threading.Event()
    chapterer.on_finished = lambda path, record: finished.set()
    assert not chapterer.request(tmp_path / "a.mp3", "テスト")
    assert chapterer.request(tmp_path / "a.m4a", "テスト")
    assert finished.wait(5)
    assert done == [str((tmp_path / "a.m4a").resolve())]
