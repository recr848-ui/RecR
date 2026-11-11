"""フリーワード予約の照合と、キャッシュ済み番組表のキーワード検索"""

import logging
import uuid
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def keyword_matches(keyword, haystack):
    """空白区切りのAND検索でキーワードがhaystackに一致するか判定する

    「A B」のようにスペース区切りで複数語を入力した場合、すべての語が
    haystackに含まれていれば一致とみなす。
    """
    terms = (keyword or '').strip().lower().split()
    if not terms:
        return False
    return all(term in haystack for term in terms)


class FreewordMixin:
    def _actual_calendar_date_iso(self, date_iso, start_hhmm):
        """番組の date_iso（放送日、5:00始まり）と start（HH:MM）から、
        実際のカレンダー上の日付を求める（0-4時台の番組は翌カレンダー日になるため）
        """
        try:
            target_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
            hour = int(start_hhmm.split(":")[0])
        except (ValueError, TypeError, AttributeError, IndexError):
            return date_iso
        if hour < 5:
            target_date += timedelta(days=1)
        return target_date.strftime("%Y-%m-%d")

    def _program_start_datetime(self, program):
        """番組の実際の放送開始datetimeを求める（不正なデータならNone）"""
        date_iso = program.get('date_iso')
        start = program.get('start')
        if not date_iso or not start:
            return None
        actual_date_iso = self._actual_calendar_date_iso(date_iso, start)
        try:
            base_date = datetime.strptime(actual_date_iso, "%Y-%m-%d").date()
            return datetime.combine(base_date, datetime.strptime(start, "%H:%M").time())
        except ValueError:
            return None

    @staticmethod
    def _once_reservation_start_datetime(reservation):
        """単発予約の開始datetime（date_isoは実カレンダー上の日付）。不正ならNone"""
        try:
            return datetime.combine(
                datetime.strptime(reservation.get('date_iso') or '', "%Y-%m-%d").date(),
                datetime.strptime(reservation.get('start') or '', "%H:%M").time(),
            )
        except ValueError:
            return None

    @staticmethod
    def _is_pending_freeword_reservation(reservation, freeword_ids):
        """フリーワード照合の結果に合わせて書き換え・削除してよい予約か
        （有効なフリーワードが自動作成し、有効で、まだ一度も実行されていない単発予約）"""
        return (
            reservation.get('source') == 'freeword'
            and reservation.get('keyword_id') in freeword_ids
            and reservation.get('repeat', 'once') == 'once'
            and reservation.get('enabled', True)
            and not reservation.get('last_run_date')
        )

    def scan_freewords_for_station(self, station, programs):
        """指定局の番組表をフリーワードと照合し、自動予約を番組表に合わせる

        - 一致する未来の番組の枠（局/実際の放送日/開始時刻）に予約が無ければ、
          1回のみの予約録音として登録する。手動予約・他フリーワード由来・同じ曜日と
          開始時刻の毎週予約など、既にその枠を録る予約があれば登録しない。
          番組名は判定に使わない（NHKは放送が近づくと「〜　選　９月３日」のように
          副題が付くため、番組名で判定すると同じ枠に予約が2件できてしまう）
        - 既存のフリーワード予約（未実行のもの）の番組名・終了時刻が番組表と
          食い違っていれば、番組表に合わせて書き換える
        - 番組表の対象期間内なのに、その枠にキーワードと一致する番組が無くなった
          未実行のフリーワード予約は削除する（時刻変更・休止で古い枠に残った予約が、
          新しい枠の予約より先に局を使ってしまい、新しい枠が録れなくなるのを防ぐ）
        - 同じ枠に未実行のフリーワード予約が複数あれば、1件を残して削除する

        照合から保存までを設定ロックの中で行い、複数の局を並列取得しているときに
        同じ枠が二重に登録されないようにする。

        Returns:
            list: 作成・更新・削除した予約録音データのリスト（何も変えなければ空）
        """
        freewords = [
            fw for fw in self.load_freewords()
            if fw.get('enabled', True) and fw.get('keyword', '').strip()
            and station in fw.get('stations', [])
        ]
        if not freewords or not programs:
            return []
        freeword_ids = {fw['id'] for fw in freewords}

        now = datetime.now()
        # その枠でキーワードに一致した未来の番組: (局, 実際の放送日, 開始時刻) -> (番組, フリーワード)
        matched_slots = {}
        program_starts = []
        for program in programs:
            start_dt = self._program_start_datetime(program)
            if not start_dt:
                continue
            program_starts.append(start_dt)
            if start_dt <= now:
                continue

            haystack = " ".join([
                program.get('title') or '', program.get('desc') or '', program.get('pfm') or ''
            ]).lower()
            matched = next(
                (fw for fw in freewords if keyword_matches(fw['keyword'], haystack)), None
            )
            if not matched:
                continue
            slot = (station, start_dt.strftime("%Y-%m-%d"), program.get('start'))
            matched_slots.setdefault(slot, (program, matched))
        if not program_starts:
            return []
        covered_from, covered_to = min(program_starts), max(program_starts)

        changed = []
        with self._config_lock:
            reservations = self._load_reservations_file()
            kept = []
            pending_by_slot = {}
            occupied_slots = set()
            occupied_weekly = set()
            for res in reservations:
                if res.get('station') != station:
                    kept.append(res)
                    continue
                slot = (station, res.get('date_iso'), res.get('start'))
                if self._is_pending_freeword_reservation(res, freeword_ids):
                    start_dt = self._once_reservation_start_datetime(res)
                    # 開始済みの枠には手を付けない（録音中の予約も終了までは
                    # last_run_date が空のまま＝未実行に見えるため）
                    upcoming = start_dt is not None and now < start_dt
                    if upcoming and slot in pending_by_slot:
                        logger.info(
                            f"フリーワード予約の重複を削除しました: {station} {slot[1]} {slot[2]} {res.get('title')}"
                        )
                        changed.append(res)
                        continue
                    if upcoming and covered_from <= start_dt <= covered_to and slot not in matched_slots:
                        logger.info(
                            "番組表にキーワードと一致する番組が無くなったため、フリーワード予約を削除しました: "
                            f"{station} {slot[1]} {slot[2]} {res.get('title')}"
                        )
                        changed.append(res)
                        continue
                    pending_by_slot.setdefault(slot, res)
                elif res.get('repeat') == 'weekly':
                    if res.get('enabled', True):
                        occupied_weekly.add((res.get('weekday'), res.get('start')))
                else:
                    occupied_slots.add(slot)
                kept.append(res)

            # 手動予約・毎週予約が同じ枠を録るなら、まだ始まっていないフリーワード予約は不要
            for slot, res in list(pending_by_slot.items()):
                start_dt = self._once_reservation_start_datetime(res)
                if start_dt is None or start_dt <= now:
                    continue
                if slot in occupied_slots or (start_dt.weekday(), slot[2]) in occupied_weekly:
                    logger.info(
                        f"同じ枠に別の予約があるため、フリーワード予約を削除しました: "
                        f"{station} {slot[1]} {slot[2]} {res.get('title')}"
                    )
                    kept = [r for r in kept if r is not res]
                    del pending_by_slot[slot]
                    changed.append(res)

            for slot, (program, matched) in matched_slots.items():
                _, date_iso, start = slot
                title = program.get('title') or ''
                end = program.get('end')
                existing = pending_by_slot.get(slot)
                if existing is not None:
                    if existing.get('title') != title or existing.get('end') != end:
                        logger.info(
                            f"フリーワード予約を番組表に合わせて更新しました: {station} {date_iso} {start} "
                            f"「{existing.get('title')}」{existing.get('end')} -> 「{title}」{end}"
                        )
                        existing['title'] = title
                        existing['end'] = end
                        changed.append(existing)
                    continue
                weekday = datetime.strptime(date_iso, "%Y-%m-%d").weekday()
                if slot in occupied_slots or (weekday, start) in occupied_weekly:
                    continue
                reservation = {
                    'id': uuid.uuid4().hex,
                    'station': station,
                    'repeat': 'once',
                    'date_iso': date_iso,
                    'start': start,
                    'end': end,
                    'title': title,
                    'source': 'freeword',
                    'keyword_id': matched['id'],
                    'enabled': True,
                    'last_run_date': None,
                }
                logger.info(f"フリーワード予約を作成しました: {station} {date_iso} {start} {title}")
                kept.append(reservation)
                pending_by_slot[slot] = reservation
                changed.append(reservation)

            if changed:
                self._save_reservations_file(kept)

        return changed

    def search_cached_programs_by_keyword(self, keyword, stations, future_only=True):
        """キャッシュ済み番組表から、フリーワードと同じ一致ロジック（タイトル＋概要＋
        出演者への部分一致、大文字小文字を区別しない）でキーワードに一致する番組を探す。

        実際に予約が作られるかどうかを保存前に確認できるよう、フリーワード登録
        ダイアログのプレビュー表示用に用意した検索専用メソッド（このメソッド自体は
        予約を作成しない）。

        Args:
            keyword (str): 検索キーワード
            stations (list): 検索対象の局名リスト
            future_only (bool): Trueなら放送開始前の番組のみを対象にする
                （＝実際にフリーワードで自動予約される番組と一致する範囲）

        Returns:
            list: [(station, program), ...]（キャッシュの並び順、局の指定順）
        """
        keyword = (keyword or '').strip().lower()
        if not keyword:
            return []

        now = datetime.now()
        results = []
        for station in stations:
            cached = self.load_cached_schedule(station) or []
            for program in cached:
                if future_only:
                    start_dt = self._program_start_datetime(program)
                    if not start_dt or start_dt <= now:
                        continue

                haystack = " ".join([
                    program.get('title') or '', program.get('desc') or '', program.get('pfm') or ''
                ]).lower()
                if keyword_matches(keyword, haystack):
                    results.append((station, program))

        return results
