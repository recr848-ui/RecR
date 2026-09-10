from datetime import datetime

from utils import reservation_logic as rl


# --- program_actual_date_iso -------------------------------------------------

def test_program_actual_date_iso_daytime_program_keeps_same_date():
    assert rl.program_actual_date_iso("2026-09-10", "09:00") == "2026-09-10"


def test_program_actual_date_iso_late_night_program_rolls_to_next_day():
    # 5:00始まり放送日の0-4時台は、実カレンダー上は翌日
    assert rl.program_actual_date_iso("2026-09-10", "02:30") == "2026-09-11"


def test_program_actual_date_iso_invalid_input_returns_original():
    assert rl.program_actual_date_iso("not-a-date", "09:00") == "not-a-date"


# --- program_air_window -------------------------------------------------------

def test_program_air_window_normal_program():
    program = {"date_iso": "2026-09-10", "start": "09:00", "end": "10:00"}
    start_dt, end_dt = rl.program_air_window(program)
    assert start_dt == datetime(2026, 9, 10, 9, 0)
    assert end_dt == datetime(2026, 9, 10, 10, 0)


def test_program_air_window_overnight_program_end_rolls_to_next_day():
    program = {"date_iso": "2026-09-10", "start": "23:00", "end": "01:00"}
    start_dt, end_dt = rl.program_air_window(program)
    assert start_dt == datetime(2026, 9, 10, 23, 0)
    assert end_dt == datetime(2026, 9, 11, 1, 0)


def test_program_air_window_late_night_program_uses_next_calendar_day():
    program = {"date_iso": "2026-09-10", "start": "02:30", "end": "03:00"}
    start_dt, end_dt = rl.program_air_window(program)
    assert start_dt == datetime(2026, 9, 11, 2, 30)
    assert end_dt == datetime(2026, 9, 11, 3, 0)


def test_program_air_window_invalid_time_returns_none_none():
    program = {"date_iso": "2026-09-10", "start": "bad", "end": "10:00"}
    assert rl.program_air_window(program) == (None, None)


# --- reservation_occurrence_program_dict --------------------------------------

def test_occurrence_dict_for_once_reservation_uses_its_own_date():
    reservation = {
        "repeat": "once", "date_iso": "2026-09-05",
        "start": "07:20", "end": "09:00", "title": "ジャズ・トゥナイト",
    }
    occurrence = rl.reservation_occurrence_program_dict(reservation)
    assert occurrence == {
        "date_iso": "2026-09-05", "start": "07:20", "end": "09:00",
        "title": "ジャズ・トゥナイト",
    }


def test_occurrence_dict_for_once_reservation_without_date_is_none():
    reservation = {"repeat": "once", "start": "07:20", "end": "09:00"}
    assert rl.reservation_occurrence_program_dict(reservation) is None


def test_occurrence_dict_for_weekly_reservation_picks_most_recent_matching_weekday():
    # 2026-09-10は木曜(weekday=3)。月曜(weekday=0)の直近の回は2026-09-07。
    now = datetime(2026, 9, 10, 12, 0)
    reservation = {"repeat": "weekly", "weekday": 0, "start": "06:00", "end": "06:30"}
    occurrence = rl.reservation_occurrence_program_dict(reservation, now=now)
    assert occurrence["date_iso"] == "2026-09-07"


def test_occurrence_dict_for_weekly_reservation_today_is_the_matching_weekday():
    # 2026-09-10は木曜(weekday=3)なので、weekday=3の直近の回は今日そのもの。
    now = datetime(2026, 9, 10, 12, 0)
    reservation = {"repeat": "weekly", "weekday": 3, "start": "06:00", "end": "06:30"}
    occurrence = rl.reservation_occurrence_program_dict(reservation, now=now)
    assert occurrence["date_iso"] == "2026-09-10"


def test_occurrence_dict_for_weekly_reservation_without_weekday_is_none():
    reservation = {"repeat": "weekly", "start": "06:00", "end": "06:30"}
    assert rl.reservation_occurrence_program_dict(reservation) is None


# --- reservation_is_overdue_pending --------------------------------------------

def _not_recording(station):
    return False


def _always_recording(station):
    return True


def test_overdue_pending_true_when_end_time_passed_and_never_run():
    now = datetime(2026, 9, 10, 12, 0)
    reservation = {
        "repeat": "once", "station": "NHK-FM", "date_iso": "2026-09-10",
        "start": "09:00", "end": "10:00",
    }
    assert rl.reservation_is_overdue_pending(reservation, _not_recording, now=now) is True


def test_overdue_pending_false_when_still_in_future():
    now = datetime(2026, 9, 10, 8, 0)
    reservation = {
        "repeat": "once", "station": "NHK-FM", "date_iso": "2026-09-10",
        "start": "09:00", "end": "10:00",
    }
    assert rl.reservation_is_overdue_pending(reservation, _not_recording, now=now) is False


def test_overdue_pending_false_when_already_run():
    now = datetime(2026, 9, 10, 12, 0)
    reservation = {
        "repeat": "once", "station": "NHK-FM", "date_iso": "2026-09-10",
        "start": "09:00", "end": "10:00", "last_run_date": "2026-09-10",
    }
    assert rl.reservation_is_overdue_pending(reservation, _not_recording, now=now) is False


def test_overdue_pending_false_when_currently_recording():
    now = datetime(2026, 9, 10, 12, 0)
    reservation = {
        "repeat": "once", "station": "NHK-FM", "date_iso": "2026-09-10",
        "start": "09:00", "end": "10:00",
    }
    assert rl.reservation_is_overdue_pending(reservation, _always_recording, now=now) is False


# --- reservation_needs_timefree_recovery ---------------------------------------

def test_needs_recovery_true_for_failed_reservation():
    reservation = {"last_result": "failed"}
    assert rl.reservation_needs_timefree_recovery(reservation, _not_recording) is True


def test_needs_recovery_true_for_partial_reservation():
    reservation = {"last_result": "partial"}
    assert rl.reservation_needs_timefree_recovery(reservation, _not_recording) is True


def test_needs_recovery_false_when_already_downloaded():
    reservation = {"last_result": "failed", "timefree_downloaded": True}
    assert rl.reservation_needs_timefree_recovery(reservation, _not_recording) is False


def test_needs_recovery_false_for_successful_reservation():
    reservation = {"last_result": "success"}
    assert rl.reservation_needs_timefree_recovery(reservation, _not_recording) is False


def test_needs_recovery_true_for_overdue_pending_reservation():
    now = datetime(2026, 9, 10, 12, 0)
    reservation = {
        "repeat": "once", "station": "NHK-FM", "date_iso": "2026-09-10",
        "start": "09:00", "end": "10:00",
    }
    assert rl.reservation_needs_timefree_recovery(reservation, _not_recording, now=now) is True


# --- format_duration -----------------------------------------------------------

def test_format_duration_seconds_only():
    assert rl.format_duration(45) == "45秒"


def test_format_duration_minutes():
    assert rl.format_duration(125) == "2分"


def test_format_duration_hours_and_minutes():
    assert rl.format_duration(3725) == "1時間2分"


def test_format_duration_negative_clamped_to_zero():
    assert rl.format_duration(-10) == "0秒"
