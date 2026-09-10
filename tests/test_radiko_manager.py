from datetime import datetime, timedelta

import pytest


# --- _sanitize_filename ---------------------------------------------------

def test_sanitize_filename_replaces_illegal_characters(manager):
    assert manager._sanitize_filename('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"


def test_sanitize_filename_leaves_normal_text_untouched(manager):
    assert manager._sanitize_filename("ミュージックライン") == "ミュージックライン"


# --- _build_recording_filename ---------------------------------------------

def test_build_recording_filename_default_pattern(manager):
    dt = datetime(2026, 9, 10, 14, 30, 5)
    name = manager._build_recording_filename(
        "station_datetime", "NHK-FM", "テスト番組", dt, "aac"
    )
    assert name == "NHK-FM_20260910_143005.aac"


def test_build_recording_filename_title_date_pattern(manager):
    dt = datetime(2026, 9, 5, 7, 20, 0)
    name = manager._build_recording_filename(
        "title_date", "TBSラジオ", "ジャズ・トゥナイト", dt, "mp3"
    )
    assert name == "ジャズ・トゥナイト_20260905.mp3"


def test_build_recording_filename_falls_back_to_station_when_title_missing(manager):
    dt = datetime(2026, 9, 10, 9, 0, 0)
    name = manager._build_recording_filename(
        "date_title", "NHK-FM", "", dt, "aac"
    )
    assert name == "20260910_NHK-FM.aac"


def test_build_recording_filename_unknown_pattern_falls_back_to_default(manager):
    dt = datetime(2026, 9, 10, 9, 0, 0)
    name = manager._build_recording_filename(
        "no_such_pattern", "NHK-FM", "番組", dt, "aac"
    )
    assert name == "NHK-FM_20260910_090000.aac"


def test_build_recording_filename_sanitizes_title(manager):
    dt = datetime(2026, 9, 10, 9, 0, 0)
    name = manager._build_recording_filename(
        "date_title", "NHK-FM", "特集：明日/どうなる？", dt, "aac"
    )
    assert "/" not in name and ":" not in name and "?" not in name


# --- _unique_output_path ----------------------------------------------------

def test_unique_output_path_returns_same_path_when_no_collision(manager, tmp_path):
    path = tmp_path / "foo.aac"
    assert manager._unique_output_path(path) == path


def test_unique_output_path_appends_counter_on_collision(manager, tmp_path):
    path = tmp_path / "foo.aac"
    path.write_bytes(b"")
    (tmp_path / "foo_1.aac").write_bytes(b"")
    result = manager._unique_output_path(path)
    assert result == tmp_path / "foo_2.aac"


# --- _parse_medialist --------------------------------------------------------

SAMPLE_MEDIALIST = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:5
#EXT-X-ALLOW-CACHE:NO
#EXT-X-MEDIA-SEQUENCE:27104814
#EXT-X-DISCONTINUITY-SEQUENCE:0
#EXT-X-START:TIME-OFFSET=0
#EXT-X-PROGRAM-DATE-TIME:2026-09-10T05:00:00.004+09:00
#EXTINF:5.035,
https://tf-f-rpaa-radiko.smartstream.ne.jp/tf/segments/m/JOAK-FM/20260910/20260910_050000_mvta7.aac
#EXT-X-PROGRAM-DATE-TIME:2026-09-10T05:00:05.039+09:00
#EXTINF:5.035,
https://tf-f-rpaa-radiko.smartstream.ne.jp/tf/segments/m/JOAK-FM/20260910/20260910_050005_ihyel.aac
#EXT-X-PROGRAM-DATE-TIME:2026-09-10T05:00:10.074+09:00
#EXTINF:5.035,
https://tf-f-rpaa-radiko.smartstream.ne.jp/tf/segments/m/JOAK-FM/20260910/20260910_050010_zf7dr.aac
"""


def test_parse_medialist_extracts_sequence_and_duration(manager):
    media_sequence, target_duration, entries = manager._parse_medialist(SAMPLE_MEDIALIST)
    assert media_sequence == 27104814
    assert target_duration == 5.0
    assert len(entries) == 3


def test_parse_medialist_assigns_incrementing_sequence_numbers(manager):
    _, _, entries = manager._parse_medialist(SAMPLE_MEDIALIST)
    assert [e[0] for e in entries] == [27104814, 27104815, 27104816]


def test_parse_medialist_pairs_program_date_time_with_following_segment(manager):
    _, _, entries = manager._parse_medialist(SAMPLE_MEDIALIST)
    assert entries[0][1].endswith("20260910_050000_mvta7.aac")
    assert entries[0][2] == "2026-09-10T05:00:00.004+09:00"
    assert entries[2][2] == "2026-09-10T05:00:10.074+09:00"


def test_parse_medialist_empty_text_returns_no_entries(manager):
    media_sequence, target_duration, entries = manager._parse_medialist("#EXTM3U\n")
    assert entries == []
    assert media_sequence == 0
    assert target_duration == 5.0


# --- _parse_program_date_time ------------------------------------------------

def test_parse_program_date_time_strips_timezone(manager):
    dt = manager._parse_program_date_time("2026-09-10T05:00:00.004+09:00")
    assert dt == datetime(2026, 9, 10, 5, 0, 0, 4000)
    assert dt.tzinfo is None


def test_parse_program_date_time_none_for_empty_value(manager):
    assert manager._parse_program_date_time(None) is None
    assert manager._parse_program_date_time("") is None


def test_parse_program_date_time_none_for_garbage(manager):
    assert manager._parse_program_date_time("not-a-date") is None


# --- _carry_over_query_params -------------------------------------------------

def test_carry_over_query_params_adds_missing_keys(manager):
    source = "https://example.com/playlist.m3u8?station_id=JOAK-FM&ft=1&to=2&l=15&lsid=abc&type=b"
    target = "https://example.com/medialist?session=1.xxx&station_id=JOAK-FM"
    result = manager._carry_over_query_params(source, target, ("ft", "to", "l"))
    assert "ft=1" in result
    assert "to=2" in result
    assert "l=15" in result
    assert "session=1.xxx" in result
    assert "station_id=JOAK-FM" in result


def test_carry_over_query_params_does_not_overwrite_existing(manager):
    source = "https://example.com/playlist.m3u8?ft=1"
    target = "https://example.com/medialist?ft=999"
    result = manager._carry_over_query_params(source, target, ("ft",))
    assert "ft=999" in result
    assert "ft=1" not in result


# --- reservation CRUD --------------------------------------------------------

def test_add_and_get_reservation(manager):
    reservation = manager.add_reservation({
        "station": "NHK-FM", "repeat": "once", "date_iso": "2026-09-10",
        "start": "09:00", "end": "10:00", "title": "テスト番組",
    })
    assert reservation["id"]
    assert reservation["enabled"] is True
    assert reservation["last_run_date"] is None

    fetched = manager.get_reservation(reservation["id"])
    assert fetched == reservation


def test_update_reservation_merges_fields(manager):
    reservation = manager.add_reservation({
        "station": "NHK-FM", "repeat": "once", "date_iso": "2026-09-10",
        "start": "09:00", "end": "10:00", "title": "テスト番組",
    })
    manager.update_reservation(reservation["id"], {"enabled": False})
    fetched = manager.get_reservation(reservation["id"])
    assert fetched["enabled"] is False
    assert fetched["title"] == "テスト番組"


def test_delete_reservation_removes_it(manager):
    reservation = manager.add_reservation({
        "station": "NHK-FM", "repeat": "once", "date_iso": "2026-09-10",
        "start": "09:00", "end": "10:00", "title": "テスト番組",
    })
    manager.delete_reservation(reservation["id"])
    assert manager.get_reservation(reservation["id"]) is None


def test_mark_reservation_run_records_date_and_result(manager):
    reservation = manager.add_reservation({
        "station": "NHK-FM", "repeat": "once", "date_iso": "2026-09-10",
        "start": "09:00", "end": "10:00", "title": "テスト番組",
    })
    manager.mark_reservation_run(reservation["id"], "2026-09-10", result="failed")
    fetched = manager.get_reservation(reservation["id"])
    assert fetched["last_run_date"] == "2026-09-10"
    assert fetched["last_result"] == "failed"


def test_get_due_reservations_returns_reservation_within_grace_window(manager):
    now = datetime.now()
    start = now - timedelta(seconds=30)
    end = now + timedelta(minutes=30)
    manager.add_reservation({
        "station": "NHK-FM", "repeat": "once", "date_iso": now.date().isoformat(),
        "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"), "title": "現在放送中",
    })
    due = manager.get_due_reservations()
    assert len(due) == 1
    assert due[0][0]["title"] == "現在放送中"


def test_get_due_reservations_skips_disabled(manager):
    now = datetime.now()
    start = now - timedelta(seconds=30)
    end = now + timedelta(minutes=30)
    reservation = manager.add_reservation({
        "station": "NHK-FM", "repeat": "once", "date_iso": now.date().isoformat(),
        "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"), "title": "無効化済み",
    })
    manager.update_reservation(reservation["id"], {"enabled": False})
    assert manager.get_due_reservations() == []


def test_get_due_reservations_skips_already_run_occurrence(manager):
    now = datetime.now()
    start = now - timedelta(seconds=30)
    end = now + timedelta(minutes=30)
    reservation = manager.add_reservation({
        "station": "NHK-FM", "repeat": "once", "date_iso": now.date().isoformat(),
        "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"), "title": "実行済み",
    })
    manager.mark_reservation_run(reservation["id"], now.date().isoformat(), result="success")
    assert manager.get_due_reservations() == []


def test_get_due_reservations_returns_occurrence_iso_matching_scheduled_date(manager):
    now = datetime.now()
    start = now - timedelta(seconds=30)
    end = now + timedelta(minutes=30)
    manager.add_reservation({
        "station": "NHK-FM", "repeat": "once", "date_iso": now.date().isoformat(),
        "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"), "title": "現在放送中",
    })
    due = manager.get_due_reservations()
    _, _, _, occurrence_iso = due[0]
    assert occurrence_iso == now.date().isoformat()


def test_get_due_reservations_margin_triggers_before_scheduled_start(manager):
    # マージン無しではまだ「未来」で対象外の予約が、マージンを付けると
    # record_start_dt が繰り上がって対象に入ることを確認する。
    manager.recording_margin_seconds = 30
    now = datetime.now()
    start = now + timedelta(seconds=20)
    end = now + timedelta(minutes=30)
    manager.add_reservation({
        "station": "NHK-FM", "repeat": "once", "date_iso": now.date().isoformat(),
        "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"), "title": "もうすぐ開始",
    })
    due = manager.get_due_reservations()
    assert len(due) == 1


def test_get_due_reservations_margin_extends_record_end_dt(manager):
    manager.recording_margin_seconds = 30
    now = datetime.now()
    start = now - timedelta(seconds=30)
    end = now + timedelta(minutes=5)
    manager.add_reservation({
        "station": "NHK-FM", "repeat": "once", "date_iso": now.date().isoformat(),
        "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"), "title": "延長対象",
    })
    due = manager.get_due_reservations()
    _, record_start_dt, record_end_dt, _ = due[0]
    # 分単位に丸めた end（分単位フォーマット由来の誤差はあるが）に30秒のマージンが
    # 加算されていることを確認する。
    expected_end = datetime.combine(now.date(), end.replace(second=0, microsecond=0).time())
    assert record_end_dt == expected_end + timedelta(seconds=30)


# --- download concurrency helpers --------------------------------------------

class _FakeThread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


def test_is_download_active_true_when_thread_alive(manager):
    manager._downloads["NHK-FM|20260910050000"] = {"thread": _FakeThread(True)}
    assert manager.is_download_active("NHK-FM", "20260910050000") is True


def test_is_download_active_false_when_no_entry(manager):
    assert manager.is_download_active("NHK-FM", "20260910050000") is False


def test_is_download_limit_reached_respects_max_concurrent(manager):
    manager.max_concurrent_timefree_downloads = 2
    manager._downloads["a"] = {"thread": _FakeThread(True)}
    assert manager.is_download_limit_reached() is False
    manager._downloads["b"] = {"thread": _FakeThread(True)}
    assert manager.is_download_limit_reached() is True


def test_is_download_limit_reached_ignores_dead_threads(manager):
    manager.max_concurrent_timefree_downloads = 1
    manager._downloads["a"] = {"thread": _FakeThread(False)}
    assert manager.is_download_limit_reached() is False


# --- prune_stale_schedule_cache -----------------------------------------------

def test_prune_stale_schedule_cache_removes_unknown_station_keys(manager):
    manager.station_mapping = {"NHK-FM": "JOAK-FM", "TBSラジオ": "TBS"}
    manager._save_cache_file({
        "NHK-FM": {"fetched_at": "2026-09-10T00:00:00", "programs": []},
        "TBS": {"fetched_at": "2026-09-02T00:00:00", "programs": []},  # 改称前の古いキー
        "bayfm78": {"fetched_at": "2026-09-02T00:00:00", "programs": []},  # 表記違いの古いキー
    })
    removed = manager.prune_stale_schedule_cache()
    assert sorted(removed) == ["TBS", "bayfm78"]
    cache = manager._load_cache_file()
    assert set(cache.keys()) == {"NHK-FM"}


def test_prune_stale_schedule_cache_keeps_timefree_keys_for_current_stations(manager):
    manager.station_mapping = {"NHK-FM": "JOAK-FM"}
    manager._save_cache_file({
        "NHK-FM": {"fetched_at": "2026-09-10T00:00:00", "programs": []},
        "NHK-FM::timefree": {"fetched_at": "2026-09-10T00:00:00", "programs": []},
        "OldStation::timefree": {"fetched_at": "2026-09-02T00:00:00", "programs": []},
    })
    removed = manager.prune_stale_schedule_cache()
    assert removed == ["OldStation::timefree"]
    cache = manager._load_cache_file()
    assert set(cache.keys()) == {"NHK-FM", "NHK-FM::timefree"}


def test_prune_stale_schedule_cache_no_op_when_nothing_stale(manager):
    manager.station_mapping = {"NHK-FM": "JOAK-FM"}
    manager._save_cache_file({"NHK-FM": {"fetched_at": "2026-09-10T00:00:00", "programs": []}})
    assert manager.prune_stale_schedule_cache() == []
