"""予約録音・番組の日時計算や状態判定など、GUI(Tkinter)にもradikoへの実通信にも
依存しない純粋なロジックをまとめたモジュール。

RecRApp（src/main.py）はこれらの薄いラッパーとして各メソッドを持ち、単体テストは
このモジュールを直接対象にする。
"""

from datetime import datetime, timedelta


def program_actual_date_iso(date_iso, start_hhmm):
    """番組の date_iso（放送日、5:00始まり）と start（HH:MM）から、
    実際のカレンダー上の日付を求める（0-4時台の番組は翌カレンダー日になるため）
    """
    try:
        target_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
        hour = int(start_hhmm.split(":")[0])
    except (ValueError, AttributeError, IndexError):
        return date_iso
    if hour < 5:
        target_date += timedelta(days=1)
    return target_date.strftime("%Y-%m-%d")


def program_air_window(program):
    """番組の実際の放送開始・終了datetimeを求める（不正なデータなら (None, None)）"""
    date_iso = program.get('date_iso') or datetime.now().strftime("%Y-%m-%d")
    start = program.get('start') or ''
    end = program.get('end') or ''
    actual_date_iso = program_actual_date_iso(date_iso, start)
    try:
        base_date = datetime.strptime(actual_date_iso, "%Y-%m-%d").date()
        start_dt = datetime.combine(base_date, datetime.strptime(start, "%H:%M").time())
        end_dt = datetime.combine(base_date, datetime.strptime(end, "%H:%M").time())
    except ValueError:
        return None, None
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return start_dt, end_dt


def reservation_occurrence_program_dict(reservation, now=None):
    """予約の直近の回（1回のみならその日、毎週なら直近のその曜日の日）を、
    program_air_window 等が扱える「番組」風の辞書にして返す（計算できなければNone）

    now を指定するとテストで基準日時を固定できる（省略時は datetime.now()）。
    """
    now = now or datetime.now()
    if reservation.get('repeat') == 'weekly':
        weekday = reservation.get('weekday')
        if weekday is None:
            return None
        today = now.date()
        days_back = (today.weekday() - weekday) % 7
        date_iso = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
    else:
        date_iso = reservation.get('date_iso')
        if not date_iso:
            return None
    return {
        'date_iso': date_iso,
        'start': reservation.get('start') or '00:00',
        'end': reservation.get('end') or '00:00',
        'title': reservation.get('title') or '',
    }


def reservation_is_overdue_pending(reservation, is_recording_active_fn, now=None):
    """予約が「待機中」のまま、録音終了予定時刻を過ぎてしまっているか
    （＝実行されるはずだったのに実行されなかった予約）を判定する

    is_recording_active_fn(station) は、その局が現在録音中かを返す呼び出し可能オブジェクト
    （RecRAppからは manager.is_recording_active を渡す）。
    """
    now = now or datetime.now()
    if reservation.get('last_run_date'):
        return False
    if is_recording_active_fn(reservation.get('station')):
        return False
    occurrence = reservation_occurrence_program_dict(reservation, now=now)
    if occurrence is None:
        return False
    _, end_dt = program_air_window(occurrence)
    if end_dt is None:
        return False
    return now > end_dt


def reservation_needs_timefree_recovery(reservation, is_recording_active_fn, now=None):
    """この予約をタイムフリーで取り直す価値があるか（失敗・中断・実行し損ねて
    終了時刻を過ぎた待機中、のいずれか。既にタイムフリーで取得済みなら対象外）"""
    if reservation.get('timefree_downloaded'):
        return False
    if reservation.get('last_result') in ('failed', 'partial'):
        return True
    return reservation_is_overdue_pending(reservation, is_recording_active_fn, now=now)


def format_duration(seconds):
    """秒数を「1時間2分」「5分」「30秒」のような大まかな日本語表記にする"""
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}時間{minutes}分"
    if minutes:
        return f"{minutes}分"
    return f"{secs}秒"
