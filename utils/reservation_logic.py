"""予約録音・番組の日時計算や状態判定など、GUI(Tkinter)にもradikoへの実通信にも
依存しない純粋なロジックをまとめたモジュール。

RecRApp（src/main.py）はこれらの薄いラッパーとして各メソッドを持ち、単体テストは
このモジュールを直接対象にする。
"""

from datetime import datetime, timedelta

# 録音中の予約と次の予約を「隣り合っている」とみなす、番組の終了と開始のずれの上限（秒）
HAND_OFF_ADJACENT_TOLERANCE_SECONDS = 60


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


def _broadcast_date_iso(actual_date_iso, start_hhmm):
    """program_actual_date_iso の逆変換: 実カレンダー上の日付と start（HH:MM）から、
    番組表の date_iso（放送日、5:00始まり）を求める（0-4時台は前日の放送日に属する）

    予約の date_iso / weekday は実カレンダー上の日付・曜日で保存されているため、
    予約を「番組」風の辞書にする際はこれで放送日に戻す。戻さないと
    program_air_window が0-4時台をもう一度翌日へ繰り上げてしまう。
    """
    try:
        target_date = datetime.strptime(actual_date_iso, "%Y-%m-%d").date()
        hour = int(start_hhmm.split(":")[0])
    except (ValueError, AttributeError, IndexError):
        return actual_date_iso
    if hour < 5:
        target_date -= timedelta(days=1)
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


def should_hand_off_recording(now, next_record_start, prior_record_end, margin_seconds, lead_seconds):
    """同じ局で録音中の予約を止めて、次の予約へ切り替えるタイミングかどうかを判定する

    前後マージンがあると、同じ局で続けて放送される番組の録音は重なる。局ごとに同時に
    録音できるのは1本だけなので、前の録音が終わるのを待つと次の番組の冒頭が欠ける。
    番組同士が隣り合っている（前の番組が次の番組の開始までに終わる）ときだけ、次の番組の
    開始の lead_seconds 秒前から切り替える。

    Args:
        now (datetime): 現在時刻
        next_record_start (datetime): 次の予約の録音開始時刻（マージン込み）
        prior_record_end (datetime): 録音中の予約の録音終了時刻（マージン込み）
        margin_seconds (int): 前後マージンの秒数
        lead_seconds (int): 次の番組の開始より何秒前から切り替えてよいか
    """
    margin = timedelta(seconds=margin_seconds)
    next_start = next_record_start + margin
    prior_end = prior_record_end - margin
    if prior_end > next_start + timedelta(seconds=HAND_OFF_ADJACENT_TOLERANCE_SECONDS):
        return False
    return now >= next_start - timedelta(seconds=lead_seconds)


def program_matches_reservation(station, program, reservation):
    """番組表上のある番組が、指定の予約と同じ回（同じ局・同じ開始時刻・同じ放送日/曜日）
    かどうかを判定する（番組表の枠に予約済みを示す装飾を付けるために使う）
    """
    if not reservation.get('enabled', True):
        return False
    if reservation.get('station') != station:
        return False
    start = program.get('start')
    if not start or reservation.get('start') != start:
        return False

    actual_date_iso = program_actual_date_iso(program.get('date_iso') or '', start)
    if reservation.get('repeat') == 'weekly':
        try:
            weekday = datetime.strptime(actual_date_iso, "%Y-%m-%d").weekday()
        except ValueError:
            return False
        return reservation.get('weekday') == weekday
    return bool(actual_date_iso) and reservation.get('date_iso') == actual_date_iso


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
    start = reservation.get('start') or '00:00'
    return {
        'date_iso': _broadcast_date_iso(date_iso, start),
        'start': start,
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
