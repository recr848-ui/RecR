"""番組データの参照（放送中の番組・予約に対応する番組・録音用メタデータ）"""

from datetime import datetime

from utils import reservation_logic


class ProgramLookupMixin:
    def _program_actual_date_iso(self, date_iso, start_hhmm):
        """番組の date_iso（放送日、5:00始まり）と start（HH:MM）から、
        実際のカレンダー上の日付を求める（0-4時台の番組は翌カレンダー日になるため）

        ロジック本体は utils.reservation_logic（GUI非依存、単体テスト対象）に切り出してある。
        """
        return reservation_logic.program_actual_date_iso(date_iso, start_hhmm)

    def _program_air_window(self, program):
        """番組の実際の放送開始・終了datetimeを求める（不正なデータならNone, None）"""
        return reservation_logic.program_air_window(program)

    def _program_matches_reservation(self, station, program, reservation):
        """番組表上のある番組が、指定の予約と同じ回かどうかを判定する"""
        return reservation_logic.program_matches_reservation(station, program, reservation)

    def _find_now_airing_program(self, station):
        """指定局のキャッシュ済み番組表から、現在放送中の番組情報を探す（無ければNone）

        録音タブの手動録音や予約録音の実行時にはあらかじめ決まった番組情報が無いため、
        ファイル名や埋め込みメタデータに番組情報を使う場合に備えて、ベストエフォートで補完する。
        """
        cached = self.manager.load_cached_schedule(station)
        if not cached:
            return None
        now = datetime.now()
        for program in cached:
            start_dt, end_dt = self._program_air_window(program)
            if start_dt and start_dt <= now < end_dt:
                return program
        return None

    def _find_program_for_reservation(self, station, reservation):
        """指定局のキャッシュ済み番組表から、指定の予約に対応する回の番組情報を探す（無ければNone）

        予約録音の実行時（_check_due_reservations）専用。前後マージン設定により
        録音開始が番組本来の開始時刻より早まっている場合、_find_now_airing_program
        （現在時刻基準）では録音開始時点にまだ放送中の「前の番組」を誤って拾って
        しまうため、必ず予約自体の対象回（局・開始時刻・放送日/曜日が一致する回）
        を番組表から探して使う。
        """
        cached = self.manager.load_cached_schedule(station)
        if not cached:
            return None
        for program in cached:
            if self._program_matches_reservation(station, program, reservation):
                return program
        return None

    def _build_recording_metadata(self, station, program=None, fallback_title=None):
        """録音ファイルに埋め込むメタデータ（番組名・出演者・局名・概要・放送日）を組み立てる

        program（番組表の1件分の辞書）が渡されればそこから出演者・概要・放送日も
        補完し、無ければ番組名（fallback_title）と局名のみのメタデータになる。
        """
        metadata = {'album': station}
        title = (program.get('title') if program else None) or fallback_title
        if title:
            metadata['title'] = title
        if program:
            if program.get('pfm'):
                metadata['artist'] = program['pfm']
            if program.get('desc'):
                metadata['comment'] = program['desc']
            if program.get('img'):
                metadata['image_url'] = program['img']
            date_iso = program.get('date_iso')
            start = program.get('start')
            if date_iso and start:
                metadata['date'] = self._program_actual_date_iso(date_iso, start)
        return metadata
