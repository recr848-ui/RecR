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


# --- ライブ再生 --------------------------------------------------------------

def test_playback_output_worker_stops_fetch_side_when_output_fails(manager, monkeypatch):
    """出力側が異常終了したら stop_event を立てて取得側も止める
    （立てないと、消費されないPCMがバッファに溜まり続けてメモリを食い潰す）"""
    import sys
    import threading
    import types

    from utils.radiko_manager import _LiveAudioBuffer

    def failing_stream(**kwargs):
        raise RuntimeError("audio device lost")

    monkeypatch.setitem(
        sys.modules, "sounddevice", types.SimpleNamespace(RawOutputStream=failing_stream)
    )
    monkeypatch.setattr(manager, "PLAYBACK_PREBUFFER_SECONDS", 0.0)
    buffer = _LiveAudioBuffer()
    buffer.sample_rate = 48000
    buffer.push(b"\x00" * 4096)
    stop_event = threading.Event()

    manager._playback_output_worker(stop_event, buffer)

    assert stop_event.is_set()


# --- 設定・予約ファイルの保存と破損からの復元 ------------------------------------

def test_save_leaves_no_temp_file_and_updates_backup(manager):
    import json

    manager.add_reservation({"station": "TBS", "start": "01:00", "end": "02:00"})

    assert not list(manager.cache_dir.glob("*.tmp"))
    backup = json.loads(manager.export_file.read_text(encoding="utf-8"))
    assert len(backup["reservations"]) == 1


def test_interrupted_save_keeps_previous_file(manager, monkeypatch):
    """書き込みの途中で落ちても、元のファイルは直前の内容のまま残る"""
    import json

    import utils.json_store as json_store

    manager.add_reservation({"station": "TBS", "start": "01:00", "end": "02:00"})

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("killed mid-write")

    monkeypatch.setattr(json_store.json, "dump", crash)
    try:
        manager.add_reservation({"station": "QRR", "start": "03:00", "end": "04:00"})
    except KeyboardInterrupt:
        pass
    monkeypatch.undo()

    saved = json.loads(manager.reservations_file.read_text(encoding="utf-8"))
    assert [r["station"] for r in saved] == ["TBS"]


def test_corrupt_reservations_are_restored_from_backup(manager):
    """壊れた予約ファイルを空扱いにせず、自動バックアップから復元する
    （空扱いにすると、次の保存でバックアップまで空で上書きされる）"""
    import json

    manager.add_reservation({"station": "TBS", "start": "01:00", "end": "02:00"})
    manager.add_reservation({"station": "QRR", "start": "03:00", "end": "04:00"})
    manager.reservations_file.write_text('[{"station": "TB', encoding="utf-8")

    manager.save_settings({"prevent_sleep": True})

    assert [r["station"] for r in manager.load_reservations()] == ["TBS", "QRR"]
    backup = json.loads(manager.export_file.read_text(encoding="utf-8"))
    assert len(backup["reservations"]) == 2
    assert (manager.cache_dir / "reservations.json.corrupt").exists()


def test_corrupt_reservations_without_backup_load_as_empty(manager):
    manager.reservations_file.write_text("", encoding="utf-8")

    assert manager.load_reservations() == []
    assert not manager.reservations_file.exists()
    assert (manager.cache_dir / "reservations.json.corrupt").exists()


# --- 通信エラー時の再試行 ------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, text="", content=b""):
        self.status_code = status_code
        self.text = text
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")


class _InstantEvent:
    """wait()で実際には待たない threading.Event の代用（再試行の待ち時間を省く）"""

    def __init__(self):
        self._set = False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, timeout=None):
        return self._set


def test_get_with_retry_recovers_from_transient_errors(manager):
    import requests

    outcomes = [
        requests.exceptions.ReadTimeout("timed out"),
        requests.exceptions.ConnectionError("unreachable"),
        _FakeResponse(503),
        _FakeResponse(200, text="ok"),
    ]

    def fake_get(url, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    res = manager._get_with_retry(fake_get, "http://x/", _InstantEvent(), timeout=10)

    assert res.text == "ok"
    assert outcomes == []


def test_get_with_retry_does_not_retry_client_errors(manager):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _FakeResponse(403)

    res = manager._get_with_retry(fake_get, "http://x/", _InstantEvent())

    assert res.status_code == 403
    assert len(calls) == 1


def test_get_with_retry_gives_up_after_budget(manager, monkeypatch):
    import pytest
    import requests

    monkeypatch.setattr(manager, "FETCH_RETRY_BUDGET_SECONDS", 0)

    def fake_get(url, **kwargs):
        raise requests.exceptions.ConnectionError("unreachable")

    with pytest.raises(requests.exceptions.ConnectionError):
        manager._get_with_retry(fake_get, "http://x/", _InstantEvent())


def test_get_with_retry_returns_none_when_stopped_while_waiting(manager):
    import threading

    import requests

    stop_event = threading.Event()

    def fake_get(url, **kwargs):
        stop_event.set()
        raise requests.exceptions.ConnectionError("unreachable")

    assert manager._get_with_retry(fake_get, "http://x/", stop_event) is None


def test_iter_live_segments_survives_single_medialist_failure(manager, monkeypatch):
    """medialistの取得が1回失敗しただけでは録音を打ち切らない"""
    import requests

    import utils.radiko_manager as radiko_manager

    polls = {"count": 0}

    def fake_get(url, headers=None, timeout=None):
        if "master" in url:
            return _FakeResponse(text="#EXTM3U\nhttp://x/medialist.m3u8\n")
        if "medialist" in url:
            polls["count"] += 1
            if polls["count"] == 3:
                raise requests.exceptions.ReadTimeout("timed out")
            return _FakeResponse(text=(
                "#EXTM3U\n#EXT-X-TARGETDURATION:5\n"
                f"#EXT-X-MEDIA-SEQUENCE:{polls['count']}\nhttp://x/seg{polls['count']}.aac\n"
            ))
        return _FakeResponse(content=url.encode())

    monkeypatch.setattr(radiko_manager.requests, "get", fake_get)
    monkeypatch.setattr(radiko_manager.time, "sleep", lambda seconds: None)

    segments = []
    for segment in manager._iter_live_segments("http://x/master.m3u8", {}, _InstantEvent()):
        segments.append(segment)
        if len(segments) == 5:
            break

    assert len(segments) == 5


def _fake_live_get_with_expiring_session(expired_sessions):
    """マスタープレイリストを取るたびに新しいセッションを払い出し、expired_sessions に
    含まれるセッションのmedialistは404を返す requests.get の代用"""
    state = {"session": 0, "polls": 0}

    def fake_get(url, headers=None, timeout=None):
        if "master" in url:
            state["session"] += 1
            return _FakeResponse(text=f"#EXTM3U\nhttp://x/medialist.m3u8?session={state['session']}\n")
        if "medialist" in url:
            state["polls"] += 1
            session = int(url.rsplit("=", 1)[1])
            if session in expired_sessions(state):
                return _FakeResponse(404)
            return _FakeResponse(text=(
                "#EXTM3U\n#EXT-X-TARGETDURATION:5\n"
                f"#EXT-X-MEDIA-SEQUENCE:{state['polls']}\nhttp://x/s{session}_{state['polls']}.aac\n"
            ))
        return _FakeResponse(content=url.encode())

    return fake_get, state


def test_iter_live_segments_renews_session_when_medialist_expires(manager, monkeypatch):
    """通信断のあいだにセッションが切れてmedialistが404になっても、取り直して続行する"""
    import utils.radiko_manager as radiko_manager

    # 3回目のポーリング以降、最初のセッションは期限切れになる
    fake_get, state = _fake_live_get_with_expiring_session(
        lambda state: {1} if state["polls"] >= 3 else set()
    )
    monkeypatch.setattr(radiko_manager.requests, "get", fake_get)
    monkeypatch.setattr(radiko_manager.time, "sleep", lambda seconds: None)

    segments = []
    for segment in manager._iter_live_segments("http://x/master.m3u8", {}, _InstantEvent()):
        segments.append(segment)
        if len(segments) == 5:
            break

    assert len(segments) == 5
    assert state["session"] == 2
    assert segments[-1].startswith(b"http://x/s2_")


def test_iter_live_segments_gives_up_when_renewed_session_also_fails(manager, monkeypatch):
    """取り直したセッションも直後に4xxなら、無限に取り直さず例外にする"""
    import pytest
    import requests

    import utils.radiko_manager as radiko_manager

    fake_get, state = _fake_live_get_with_expiring_session(lambda state: {1, 2, 3, 4})
    monkeypatch.setattr(radiko_manager.requests, "get", fake_get)
    monkeypatch.setattr(radiko_manager.time, "sleep", lambda seconds: None)

    with pytest.raises(requests.HTTPError):
        list(manager._iter_live_segments("http://x/master.m3u8", {}, _InstantEvent()))

    assert state["session"] == 2


# --- タイムフリー取得中のセッション切れ ----------------------------------------

_TF_BASE = datetime(2026, 10, 4, 19, 30, 0)


def _install_fake_timefree_server(monkeypatch, expired_sessions):
    """タイムフリー配信の代用を requests.Session に差し込む

    プレイリストを取るたびに新しいセッションを払い出す。medialistは5秒刻みの
    セグメントを3件ずつ、ポーリングのたびに1件ずつ進めて返す。expired_sessions に
    含まれるセッションは、3回目のポーリングから404を返す（通信断で期限切れになった
    状況の再現）。取り直したセッションは、指定された開始時刻の1件手前から始める
    （開始時刻を秒単位に丸めたせいで取得済みの1件が重複する状況の再現）。
    """
    from urllib.parse import parse_qs, urlparse

    import utils.radiko_manager as radiko_manager

    sessions = {}

    def fake_get(url, headers=None, timeout=None):
        query = parse_qs(urlparse(url).query)
        if "/playlist" in url:
            number = len(sessions) + 1
            start = datetime.strptime(query["ft"][0], "%Y%m%d%H%M%S")
            if number > 1:
                start -= timedelta(seconds=5)
            sessions[number] = {"start": start, "polls": 0}
            return _FakeResponse(text=f"#EXTM3U\nhttp://x/medialist?session={number}\n")
        if "/medialist" in url:
            number = int(query["session"][0])
            state = sessions[number]
            if number in expired_sessions and state["polls"] >= 2:
                return _FakeResponse(404)
            first = state["polls"]
            state["polls"] += 1
            lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:5", f"#EXT-X-MEDIA-SEQUENCE:{first}"]
            for i in range(first, first + 3):
                seg_dt = state["start"] + timedelta(seconds=5 * i)
                lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{seg_dt.isoformat()}+09:00")
                lines.append(f"http://x/seg/{seg_dt:%H%M%S}")
            return _FakeResponse(text="\n".join(lines) + "\n")
        return _FakeResponse(content=url.rsplit("/", 1)[1].encode())

    class FakeSession:
        get = staticmethod(fake_get)

    monkeypatch.setattr(radiko_manager.requests, "Session", FakeSession)
    monkeypatch.setattr(radiko_manager.time, "sleep", lambda seconds: None)
    return sessions


def _tf_playlist_url(ft_dt):
    return f"http://x/playlist?ft={ft_dt:%Y%m%d%H%M%S}"


def test_iter_timefree_segments_resumes_from_where_session_expired(manager, monkeypatch):
    """途中でmedialistが404になっても完了扱いにせず、続きの時刻から取り直して最後まで取る"""
    sessions = _install_fake_timefree_server(monkeypatch, expired_sessions={1})

    segments = list(manager._iter_timefree_segments(
        _tf_playlist_url(_TF_BASE), {}, _InstantEvent(), _TF_BASE + timedelta(seconds=60),
        renew_playlist_url=_tf_playlist_url,
    ))

    expected = [f"1930{second:02d}".encode() for second in range(0, 60, 5)]
    assert segments == expected
    assert len(sessions) == 2


def test_iter_timefree_segments_fails_when_it_cannot_resume(manager, monkeypatch):
    """取り直しても進まない（取り直した先も404）なら、途中までを成功扱いにせず例外にする"""
    import pytest
    import requests

    _install_fake_timefree_server(monkeypatch, expired_sessions={1, 2, 3})

    def renew_without_progress(ft_dt):
        # 取り直した先が1件も新しいセグメントを返さない状況にするため、
        # 取得済みの範囲しか載らない時刻から始めさせる
        return _tf_playlist_url(_TF_BASE - timedelta(seconds=5))

    with pytest.raises(requests.HTTPError):
        list(manager._iter_timefree_segments(
            _tf_playlist_url(_TF_BASE), {}, _InstantEvent(), _TF_BASE + timedelta(seconds=60),
            renew_playlist_url=renew_without_progress,
        ))


def test_iter_timefree_segments_treats_404_near_the_end_as_complete(manager, monkeypatch):
    """番組の終端付近での404は、これまでどおり正常終了とする"""
    _install_fake_timefree_server(monkeypatch, expired_sessions={1})

    segments = list(manager._iter_timefree_segments(
        _tf_playlist_url(_TF_BASE), {}, _InstantEvent(), _TF_BASE + timedelta(seconds=30),
        renew_playlist_url=_tf_playlist_url,
    ))

    assert segments == [b"193000", b"193005", b"193010", b"193015"]


# --- 番組画像のディスクキャッシュ ------------------------------------------------

def test_prune_image_cache_removes_oldest_until_under_limit(manager, monkeypatch):
    import os

    manager.image_cache_dir.mkdir(parents=True, exist_ok=True)
    for i in range(10):
        path = manager.image_cache_dir / f"{i}.jpg"
        path.write_bytes(b"x" * 100)
        os.utime(path, (1_000_000 + i, 1_000_000 + i))
    monkeypatch.setattr(manager, "IMAGE_CACHE_MAX_BYTES", 500)

    removed = manager.prune_image_cache()

    # 上限500の8割（400バイト＝4件）まで、古いものから削除する
    assert removed == 6
    assert sorted(p.name for p in manager.image_cache_dir.iterdir()) == [
        "6.jpg", "7.jpg", "8.jpg", "9.jpg"
    ]


def test_prune_image_cache_no_op_when_under_limit_or_missing(manager):
    assert manager.prune_image_cache() == 0

    manager.image_cache_dir.mkdir(parents=True, exist_ok=True)
    (manager.image_cache_dir / "a.jpg").write_bytes(b"x" * 100)

    assert manager.prune_image_cache() == 0
    assert (manager.image_cache_dir / "a.jpg").exists()


def test_write_json_atomic_retries_then_succeeds_when_replace_is_briefly_denied(tmp_path, monkeypatch):
    """差し替えが一時的に拒否されても（ウイルス対策ソフトの検査中など）、再試行して保存する"""
    import json
    import os

    import utils.json_store as json_store

    target = tmp_path / "data.json"
    real_replace = os.replace
    attempts = {"count": 0}

    def flaky_replace(src, dst):
        attempts["count"] += 1
        if attempts["count"] < 4:
            raise PermissionError("in use")
        real_replace(src, dst)

    monkeypatch.setattr(json_store.os, "replace", flaky_replace)
    monkeypatch.setattr(json_store.time, "sleep", lambda seconds: None)

    json_store.write_json_atomic(target, {"a": 1})

    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert attempts["count"] == 4


def test_write_json_atomic_falls_back_to_direct_write_when_replace_keeps_failing(tmp_path, monkeypatch):
    """差し替えがずっと拒否され続ける場合も、保存自体は諦めず直接上書きする"""
    import json

    import utils.json_store as json_store

    target = tmp_path / "data.json"
    target.write_text('{"old": true}', encoding="utf-8")

    def denied(src, dst):
        raise PermissionError("in use")

    monkeypatch.setattr(json_store.os, "replace", denied)
    monkeypatch.setattr(json_store.time, "sleep", lambda seconds: None)

    json_store.write_json_atomic(target, {"a": 1})

    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert not list(tmp_path.glob("*.tmp"))
