import io
import json
import math
from array import array
from datetime import datetime, timedelta, date

import av
import pytest

from utils import cm_fingerprint as cmfp
from utils.cm_fingerprint import (
    FingerprintBuilder, FingerprintCollector, off_air_overlap, off_air_windows, plan_chunks,
    read_fp_file, reserved_hours, target_stations, write_fp_file,
)
from utils.radiko_manager import RadikoManager

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
        self.schedules = {}
        self.calls = []
        self.base_dt = None
        self.delivered = 0

    def _ensure_authenticated(self):
        return "token"

    def _get_timefree_playlist_url(self, station_id):
        return "https://example.invalid/playlist"

    _split_time_range = RadikoManager._split_time_range

    def _fetch_timefree_ranges_parallel(self, base, station_id, token, ranges, stop_event, on_segment,
                                        should_stop=None, log=None):
        """セグメント i は放送開始の 5*i 秒後のものとして、区間に入るものを順に渡す

        続きから取り直したとき（最初の分割の境目以外から取るとき）は、本物と同じく
        取り始めの1秒前までに始まる取得済みのセグメントも載せる
        """
        self.calls.append((station_id, ranges[0][0], ranges[-1][1], len(ranges)))
        if self.base_dt is None:
            self.base_dt = ranges[0][0]
            self.split_starts = {start for start, _ in ranges}
        outcomes = []
        for from_dt, to_dt in ranges:
            if from_dt not in self.split_starts:
                from_dt -= timedelta(seconds=1)
            if stop_event.is_set():
                outcomes.append('stopped')
                continue
            for i, seg in enumerate(self.segments):
                seg_dt = self.base_dt + timedelta(seconds=5 * i)
                if not (from_dt <= seg_dt < to_dt):
                    continue
                on_segment(len(outcomes), seg_dt, seg)
                self.delivered += 1
                if should_stop is not None and should_stop():
                    stop_event.set()
                    break
            outcomes.append('stopped' if stop_event.is_set() else 'done')
        return outcomes

    def load_reservations(self):
        return self.reservations

    def load_cached_schedule(self, station_name):
        return self.schedules.get(station_name)


def _collector(tmp_path, segments, busy=lambda: None, reservations=()):
    manager = FakeManager(tmp_path, segments, reservations)
    return FingerprintCollector(manager, busy, tmp_path / "logs"), manager


def _fetch(collector, chunk, stop_event=None):
    """取得から解析までを1件通す"""
    stop_event = stop_event or cmfp.threading.Event()
    job = collector._download_chunk(chunk, stop_event)
    if job['result'] != 'done':
        return job
    return collector._analyze_chunk(job, stop_event)


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
    outcome = _fetch(collector, chunk)
    assert outcome['result'] == 'done'
    assert outcome['audio_seconds'] == pytest.approx(12, abs=0.5)
    fp = read_fp_file(collector.fp_path("TBS", chunk['bday'], chunk['hour']))
    assert len(fp['fingerprints']) == outcome['frames'] > 0
    stats = [json.loads(l) for l in (tmp_path / "logs" / "fingerprint_stats.jsonl").read_text("utf-8").splitlines()]
    assert stats[-1]['result'] == 'done'
    assert stats[-1]['segments'] == 3
    assert manager.calls[0][3] == cmfp.FETCH_PARALLEL_SPLITS
    assert not collector.audio_path("TBS", chunk['bday'], chunk['hour']).exists()


def test_fetch_chunk_keeps_audio_when_enabled(tmp_path):
    collector, _ = _collector(tmp_path, _aac_segments(6))
    collector.update_settings({'cm_fp_keep_audio': True})
    chunk = _chunk(6)
    assert _fetch(collector, chunk)['result'] == 'done'
    assert collector.audio_path("TBS", chunk['bday'], chunk['hour']).stat().st_size > 0


def test_fetch_chunk_fails_when_audio_is_short(tmp_path):
    collector, _ = _collector(tmp_path, _aac_segments(6))
    outcome = _fetch(collector, _chunk(60))
    assert outcome['result'] == 'failed'
    assert "足りません" in outcome['error']


def test_fetch_chunk_aborts_when_recording_starts(tmp_path):
    busy = {'calls': 0}

    def busy_reason():
        busy['calls'] += 1
        return "オンタイム録音中（TBS）" if busy['calls'] >= 2 else None

    collector, _ = _collector(tmp_path, _aac_segments(12), busy=busy_reason)
    chunk = _chunk(12)
    assert collector._download_chunk(chunk, cmfp.threading.Event())['result'] == 'paused'
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
    job = collector._download_with_pause(chunk, cmfp.threading.Event(), 0, 1)
    assert job['result'] == 'done'
    assert len(manager.calls) == 2
    # 中断までに取れた分は捨てずに、続きだけを取り直す
    assert job['segments'] == manager.segments
    assert job['stats']['resumed_segments'] == 1


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


def test_analysis_finishes_even_if_recording_starts_after_download(tmp_path):
    """取得を終えた分は、録音が始まっても捨てずに解析を続ける"""
    busy = {'reason': None}
    collector, _ = _collector(tmp_path, _aac_segments(6), busy=lambda: busy['reason'])
    chunk = _chunk(6)
    job = collector._download_chunk(chunk, cmfp.threading.Event())
    busy['reason'] = "オンタイム録音中"
    assert collector._analyze_chunk(job, cmfp.threading.Event())['result'] == 'done'
    assert collector.fp_path("TBS", chunk['bday'], chunk['hour']).exists()


def test_plan_rotates_stations_so_none_is_always_last():
    """同じ時刻の中の局の順番を時間ごとに回す（固定すると後ろの局だけ期限切れになる）"""
    stations = ["TBS", "文化放送"]
    chunks, _ = plan_chunks(stations, MAPPING, {}, datetime(2026, 10, 30, 6, 0), 11, 13)
    first_by_hour = {}
    for chunk in chunks:
        first_by_hour.setdefault((chunk['bday'], chunk['hour']), chunk['station'])
    assert set(first_by_hour.values()) == set(stations)


def test_reserved_hours_expands_one_hour_and_uses_broadcast_hours():
    reservations = [
        {'station': "TBS", 'start': "11:00", 'end': "12:50"},
        {'station': "TBS", 'start': "01:00", 'end': "02:00"},
        {'station': "文化放送", 'start': "23:30", 'end': "00:30", 'enabled': False},
    ]
    hours = reserved_hours(reservations, ["TBS", "文化放送"])
    assert hours == {"TBS": {10, 11, 12, 13, 24, 25, 26}}


def test_plan_takes_reserved_hours_first():
    chunks, _ = plan_chunks(
        ["TBS"], MAPPING, {}, datetime(2026, 10, 30, 6, 0), 11, 27, reserved={"TBS": {20}}
    )
    assert [c['hour'] for c in chunks[:7]] == [20] * 7
    assert chunks[7]['hour'] != 20
    assert [c['bday'] for c in chunks[:7]] == sorted(c['bday'] for c in chunks[:7])


def test_off_air_windows_picks_titles_from_schedule():
    programs = [
        {'date_iso': "2026-10-25", 'start': "01:00", 'end': "03:00", 'title': "放送休止"},
        {'date_iso': "2026-10-25", 'start': "03:00", 'end': "05:00", 'title': "休止中(PART1)"},
        {'date_iso': "2026-10-25", 'start': "22:00", 'end': "23:00", 'title': "放送機器メンテナンスのため放送休止"},
        {'date_iso': "2026-10-25", 'start': "12:00", 'end': "13:00", 'title': "お昼の番組"},
    ]
    windows = off_air_windows(programs)
    assert len(windows) == 3
    # 0-4時台は放送日の翌カレンダー日
    assert windows[0] == (datetime(2026, 10, 26, 1, 0), datetime(2026, 10, 26, 3, 0))


def test_off_air_overlap_merges_adjacent_windows():
    start = datetime(2026, 10, 26, 1, 0)
    windows = [
        (datetime(2026, 10, 26, 1, 30), datetime(2026, 10, 26, 1, 40)),
        (datetime(2026, 10, 26, 1, 40), datetime(2026, 10, 26, 2, 30)),
        (datetime(2026, 10, 26, 3, 0), datetime(2026, 10, 26, 4, 0)),
    ]
    assert off_air_overlap(windows, start, start + timedelta(hours=1)) == [[1800, 3600]]


def test_plan_skips_hours_that_are_off_air():
    """1時間まるごと休止なら取らず、一部だけ休止なら範囲を添えて取る"""
    windows = [(datetime(2026, 10, 30, 1, 30), datetime(2026, 10, 30, 3, 0))]
    chunks, counts = plan_chunks(
        ["TBS"], MAPPING, {}, datetime(2026, 10, 30, 6, 0), 11, 27, off_air={"TBS": windows}
    )
    day = [c for c in chunks if c['bday'] == date(2026, 10, 29)]
    assert 26 not in [c['hour'] for c in day]
    assert counts['off_air'] == 1
    assert next(c for c in day if c['hour'] == 25)['off_air'] == [[1800, 3600]]


def test_partial_off_air_is_kept_in_index(tmp_path):
    collector, _ = _collector(tmp_path, [])
    chunk = {**_chunk(60), 'off_air': [[1800, 3600]]}
    index = {}
    collector._record_outcome(chunk, index, {'result': 'done', 'frames': 1, 'audio_seconds': 60})
    assert index[chunk['key']]['off_air'] == [[1800, 3600]]


def test_run_downloads_and_analyzes_every_planned_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(cmfp, "MIN_AUDIO_RATIO", 0)
    collector, manager = _collector(
        tmp_path, _aac_segments(6), reservations=[{'station': "TBS", 'start': "11:00", 'end': "12:00"}]
    )
    collector.update_settings({'cm_fp_window_start': 11, 'cm_fp_window_end': 12})
    collector.start_run("テスト")
    collector._thread.join(timeout=60)
    assert not collector.is_running()
    index = collector.load_index()
    assert len(manager.calls) == len(index) > 0
    assert all(entry['status'] == 'done' for entry in index.values())
    status = collector.get_status()
    assert status['state'] == 'idle' and status['done'] == len(index)
    assert status['message'].startswith("完了")


def test_run_does_not_retry_failed_chunk_in_same_run(tmp_path, monkeypatch):
    """失敗したチャンクは同じ実行の中で取り直さず、次の実行に回す"""
    monkeypatch.setattr(cmfp, "FAILURE_BACKOFF_SECONDS", 0)
    collector, manager = _collector(
        tmp_path, [], reservations=[{'station': "TBS", 'start': "11:00", 'end': "12:00"}]
    )

    def fail(*a, **k):
        raise RuntimeError("通信エラー")

    manager._get_timefree_playlist_url = fail
    collector.update_settings({'cm_fp_window_start': 11, 'cm_fp_window_end': 12})
    collector.start_run("テスト")
    collector._thread.join(timeout=60)
    assert not collector.is_running()
    index = collector.load_index()
    assert index and all(e['status'] == 'pending' and e['attempts'] == 1 for e in index.values())
