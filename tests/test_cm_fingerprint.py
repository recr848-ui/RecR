import io
import json
import math
from array import array
from datetime import datetime, timedelta, date

import av
import pytest

from utils import cm_fingerprint as cmfp
from utils.cm_fingerprint import (
    FingerprintBuilder, FingerprintCollector, plan_chunks, read_fp_file, target_stations,
    write_fp_file,
)

MAPPING = {"TBS": "TBS", "文化放送": "QRR", "NHK-FM": "JOAK-FM", "NHK AM（東京）": "JOAK"}


def _pcm(samples):
    return array("h", samples).tobytes()


def _tone(freq, seconds, amp=8000):
    n = int(cmfp.SAMPLE_RATE * seconds)
    return [int(amp * math.sin(2 * math.pi * freq * i / cmfp.SAMPLE_RATE)) for i in range(n)]


def _aac_segments(seconds, segment_seconds=5):
    """テスト用: 正弦波をAAC(ADTS)にエンコードし、segment_seconds ごとのバイト列に分ける"""
    rate = 48000
    out = io.BytesIO()
    container = av.open(out, mode="w", format="adts")
    stream = container.add_stream("aac", rate=rate)
    stream.layout = "stereo"
    total = int(rate * seconds)
    frame_samples = 1024
    pts = 0
    while pts < total:
        n = min(frame_samples, total - pts)
        samples = array("h")
        for i in range(n):
            v = int(6000 * math.sin(2 * math.pi * (440 + 200 * ((pts + i) // rate % 3)) * (pts + i) / rate))
            samples.extend((v, v))
        frame = av.AudioFrame(format="s16", layout="stereo", samples=n)
        frame.planes[0].update(samples.tobytes() + b"\0" * (frame.planes[0].buffer_size - n * 4))
        frame.sample_rate = rate
        frame.pts = pts
        pts += n
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    data = out.getvalue()

    # ADTSフレームの境界で区切る（1フレーム1024サンプル）
    frames = []
    pos = 0
    while pos + 7 <= len(data):
        length = ((data[pos + 3] & 0x03) << 11) | (data[pos + 4] << 3) | (data[pos + 5] >> 5)
        frames.append(data[pos:pos + length])
        pos += length
    per_segment = int(segment_seconds * rate / 1024)
    return [b"".join(frames[i:i + per_segment]) for i in range(0, len(frames), per_segment)]


class FakeManager:
    def __init__(self, tmp_path, segments, reservations=()):
        self.cache_dir = tmp_path / "config"
        self.station_mapping = dict(MAPPING)
        self.timefree_parallel_splits = 4
        self.area_id = "JP13"
        self.segments = segments
        self.reservations = list(reservations)
        self.calls = []

    def _ensure_authenticated(self):
        return "token"

    def _get_timefree_playlist_url(self, station_id):
        return "https://example.invalid/playlist"

    def _iter_timefree_segments_parallel(self, base, station_id, token, ft_dt, to_dt, stop_event,
                                         on_progress=None, num_splits=4):
        self.calls.append((station_id, ft_dt, to_dt, num_splits))
        for i, seg in enumerate(self.segments):
            if stop_event.is_set():
                return
            if on_progress:
                on_progress(i + 1, len(self.segments))
            yield seg

    def load_reservations(self):
        return self.reservations


def _collector(tmp_path, segments, busy=lambda: None, reservations=()):
    manager = FakeManager(tmp_path, segments, reservations)
    return FingerprintCollector(manager, busy, tmp_path / "logs"), manager


def _chunk(seconds, bday=date(2026, 10, 29), hour=13):
    start = cmfp.broadcast_hour_start(bday, hour)
    return {
        'key': cmfp.chunk_key("TBS", bday, hour), 'station': "TBS", 'station_id': "TBS",
        'bday': bday, 'hour': hour, 'start_dt': start, 'end_dt': start + timedelta(seconds=seconds),
        'attempts': 0,
    }


def test_target_stations_excludes_nhk_disabled_and_unknown():
    reservations = [
        {'station': "TBS", 'enabled': True},
        {'station': "TBS", 'enabled': True},
        {'station': "文化放送", 'enabled': False},
        {'station': "NHK-FM", 'enabled': True},
        {'station': "NHK AM（東京）"},
        {'station': "MBSラジオ", 'enabled': True},
    ]
    stations, skipped = target_stations(reservations, MAPPING)
    assert stations == ["TBS"]
    assert set(skipped) == {"NHK-FM", "NHK AM（東京）", "MBSラジオ"}


def test_plan_first_run_takes_every_available_day_oldest_first():
    """初回は、タイムフリーで取れる7日分すべて（当日分はまだ取らない）"""
    now = datetime(2026, 10, 30, 6, 0)
    chunks, counts = plan_chunks(["TBS"], MAPPING, {}, now, 11, 27)
    days = sorted({c['bday'] for c in chunks})
    assert days == [date(2026, 10, 23) + timedelta(days=i) for i in range(7)]
    assert len(chunks) == 7 * 16
    assert chunks[0]['start_dt'] == datetime(2026, 10, 23, 11, 0)
    assert chunks[-1]['start_dt'] == datetime(2026, 10, 30, 2, 0)
    assert [c['start_dt'] for c in chunks] == sorted(c['start_dt'] for c in chunks)
    assert counts['expired'] > 0


def test_plan_second_run_takes_only_missing_chunks():
    now = datetime(2026, 10, 31, 6, 0)
    chunks, _ = plan_chunks(["TBS"], MAPPING, {}, now, 11, 27)
    index = {c['key']: {'status': 'done'} for c in chunks if c['bday'] < date(2026, 10, 30)}
    index[cmfp.chunk_key("TBS", date(2026, 10, 30), 15)] = {'status': 'failed'}
    remaining, counts = plan_chunks(["TBS"], MAPPING, index, now, 11, 27)
    assert {c['bday'] for c in remaining} == {date(2026, 10, 30)}
    assert len(remaining) == 15
    assert counts['failed'] == 1


def test_plan_does_not_take_today_until_window_end_plus_delay():
    now = datetime(2026, 10, 30, 4, 30)  # 10/29の27時（=10/30 3時）から2時間経っていない
    chunks, _ = plan_chunks(["TBS"], MAPPING, {}, now, 11, 27)
    assert date(2026, 10, 29) not in {c['bday'] for c in chunks}


def test_builder_frame_count_and_determinism():
    samples = _tone(440, 3) + _tone(1200, 3)
    a = FingerprintBuilder()
    a.feed(_pcm(samples))
    b = FingerprintBuilder()
    for i in range(0, len(samples), 1000):  # 細切れで渡しても同じ結果になる
        b.feed(_pcm(samples[i:i + 1000]))
    expected = 1 + (len(samples) - cmfp.FRAME_SIZE) // cmfp.HOP_SIZE
    assert a.frame_count == b.frame_count == expected
    assert list(a.fingerprints) == list(b.fingerprints)
    assert a.loudness == b.loudness
    assert any(a.fingerprints)


def test_builder_loudness_silence_and_tone():
    builder = FingerprintBuilder()
    builder.feed(_pcm([0] * 4096 + _tone(1000, 1, amp=32767)))
    assert builder.loudness[0] == 0
    # フルスケールの正弦波は約 -3dBFS
    assert abs((builder.loudness[-1] - 100) - (-3)) <= 1


def test_fp_file_round_trip(tmp_path):
    path = tmp_path / "x" / "13.fp"
    write_fp_file(path, [1, 2, 0xFFFFFFFF], bytes([0, 50, 100]))
    data = read_fp_file(path)
    assert list(data['fingerprints']) == [1, 2, 0xFFFFFFFF]
    assert data['loudness'] == bytes([0, 50, 100])
    assert data['version'] == cmfp.FP_VERSION
    assert data['hop'] == cmfp.HOP_SIZE


def test_fetch_chunk_saves_fingerprint_and_stats(tmp_path):
    collector, manager = _collector(tmp_path, _aac_segments(12))
    chunk = _chunk(12)
    outcome = collector._fetch_chunk(chunk, cmfp.threading.Event())
    assert outcome['result'] == 'done'
    assert outcome['audio_seconds'] == pytest.approx(12, abs=0.5)
    fp = read_fp_file(collector.fp_path("TBS", chunk['bday'], chunk['hour']))
    assert len(fp['fingerprints']) == outcome['frames'] > 0
    stats = [json.loads(l) for l in (tmp_path / "logs" / "fingerprint_stats.jsonl").read_text("utf-8").splitlines()]
    assert stats[-1]['result'] == 'done'
    assert stats[-1]['segments'] == 3
    assert manager.calls[0][3] == 4
    assert not collector.audio_path("TBS", chunk['bday'], chunk['hour']).exists()


def test_fetch_chunk_keeps_audio_when_enabled(tmp_path):
    collector, _ = _collector(tmp_path, _aac_segments(6))
    collector.update_settings({'cm_fp_keep_audio': True})
    chunk = _chunk(6)
    assert collector._fetch_chunk(chunk, cmfp.threading.Event())['result'] == 'done'
    assert collector.audio_path("TBS", chunk['bday'], chunk['hour']).stat().st_size > 0


def test_fetch_chunk_fails_when_audio_is_short(tmp_path):
    collector, _ = _collector(tmp_path, _aac_segments(6))
    outcome = collector._fetch_chunk(_chunk(60), cmfp.threading.Event())
    assert outcome['result'] == 'failed'
    assert "足りません" in outcome['error']


def test_fetch_chunk_aborts_when_recording_starts(tmp_path):
    busy = {'calls': 0}

    def busy_reason():
        busy['calls'] += 1
        return "オンタイム録音中（TBS）" if busy['calls'] >= 2 else None

    collector, _ = _collector(tmp_path, _aac_segments(12), busy=busy_reason)
    chunk = _chunk(12)
    assert collector._fetch_chunk(chunk, cmfp.threading.Event())['result'] == 'paused'
    assert not collector.fp_path("TBS", chunk['bday'], chunk['hour']).exists()


def test_chunk_is_retried_after_recording_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(cmfp, "PAUSE_POLL_SECONDS", 0.01)
    state = {'calls': 0}

    def busy_reason():
        # 取得途中で録音が始まり（2回目）、数回のチェックの後に終わる
        state['calls'] += 1
        return "オンタイム録音中" if 2 <= state['calls'] <= 4 else None

    collector, manager = _collector(tmp_path, _aac_segments(12), busy=busy_reason)
    # 期限切れの判定は実際の現在時刻で行うため、チャンクは前日のものにする
    chunk = _chunk(12, bday=(datetime.now() - timedelta(days=1)).date())
    index = {}
    result = collector._process_chunk_with_pause(chunk, index, cmfp.threading.Event(), 0, 1)
    assert result == 'done'
    assert len(manager.calls) == 2
    assert index[chunk['key']]['status'] == 'done'


def test_failed_chunk_gives_up_after_max_attempts(tmp_path):
    collector, _ = _collector(tmp_path, [])
    chunk = _chunk(60)
    index = {}
    for _ in range(cmfp.MAX_ATTEMPTS):
        collector._record_outcome(chunk, index, {'result': 'failed', 'error': "x"})
    assert index[chunk['key']]['status'] == 'failed'
    assert index[chunk['key']]['attempts'] == cmfp.MAX_ATTEMPTS


def test_prune_removes_old_days(tmp_path):
    collector, _ = _collector(tmp_path, [])
    collector.update_settings({'cm_fp_retention_days': 7})
    now = datetime(2026, 10, 30, 6, 0)
    old, new = date(2026, 10, 20), date(2026, 10, 28)
    for d in (old, new):
        write_fp_file(collector.fp_path("TBS", d, 13), [1], b"\0")
        audio = collector.audio_path("TBS", d, 13)
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"x")
    index = {cmfp.chunk_key("TBS", old, 13): {'status': 'done'},
             cmfp.chunk_key("TBS", new, 13): {'status': 'done'}}
    assert collector.prune(index, now) == 1
    assert list(index) == [cmfp.chunk_key("TBS", new, 13)]
    assert not collector.fp_path("TBS", old, 13).exists()
    assert not collector.audio_path("TBS", old, 13).exists()
    assert collector.fp_path("TBS", new, 13).exists()
    assert collector.audio_path("TBS", new, 13).exists()


def test_run_with_no_target_stations(tmp_path):
    collector, _ = _collector(tmp_path, [], reservations=[{'station': "NHK-FM"}])
    collector.start_run("テスト")
    collector._thread.join(timeout=10)
    assert collector.get_status()['message'] == "対象の局がありません"
