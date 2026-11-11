"""予約録音の定期チェック・開始・再開・完了処理とスリープ抑止"""

from tkinter import messagebox
from datetime import datetime
import ctypes
import logging

from utils import reservation_logic

logger = logging.getLogger(__name__)


# Windows の電源管理API（SetThreadExecutionState）用フラグ。
# 予約録音を控えている間・録音中は、アイドルによる自動スリープを抑止するために使う。
_ES_CONTINUOUS = 0x80000000


_ES_SYSTEM_REQUIRED = 0x00000001


class ReservationRunnerMixin:
    # 予約チェックの巡回がこれ以上空いたら、スリープや応答なしで止まっていたとみなす
    RESERVATION_CHECK_GAP_SECONDS = 5 * 60

    # 予約録音の開始時刻が来ていないかを巡回する間隔（ミリ秒）
    RESERVATION_CHECK_INTERVAL_MS = 15000
    # 開始失敗・途中中断した予約録音を再開する際の待ち時間（秒）。最初はこの秒数で、
    # 失敗が続くたびに倍にして上限で頭打ちにする（通信断が長引いている間、
    # radiko側へ認証要求を送り続けないようにするため）
    RESERVATION_RESUME_RETRY_SECONDS = 15
    RESERVATION_RESUME_RETRY_MAX_SECONDS = 300
    # 番組の残りがこの秒数を切っていたら、もう再開は試みない
    RESERVATION_RESUME_MIN_REMAINING_SECONDS = 30

    def _schedule_reservation_check(self):
        """予約録音の開始時刻が来ていないか一定間隔でチェックする

        処理中に予期せぬ例外が発生しても、再スケジュールだけは必ず行う。
        そうしないと、長時間起動しっぱなしの環境でこのループが静かに
        止まってしまい、以降の予約録音が一切行われなくなる
        """
        try:
            self._detect_reservation_check_gap()
            self._check_due_reservations()
            self._update_sleep_prevention()
        except Exception:
            logger.exception("予約チェック処理でエラーが発生しました")
        self._reservation_check_job = self.root.after(
            self.RESERVATION_CHECK_INTERVAL_MS, self._schedule_reservation_check
        )

    def _detect_reservation_check_gap(self, now=None):
        """予約チェックの巡回が大きく空いていたら（スリープ・休止、またはメインスレッドが
        応答しなかった）、ログに残し、その間に取り逃した予約の自動取得を前倒しで試みる"""
        now = now or datetime.now()
        last = self._last_reservation_check_at
        self._last_reservation_check_at = now
        if last is None:
            return
        gap = (now - last).total_seconds()
        if gap < self.RESERVATION_CHECK_GAP_SECONDS:
            return
        logger.warning(
            f"予約チェックが{int(gap // 60)}分間実行されていませんでした"
            f"（{last.strftime('%Y-%m-%d %H:%M:%S')} 〜 {now.strftime('%Y-%m-%d %H:%M:%S')}）。"
            "スリープ・休止状態か、アプリが応答していなかった可能性があります"
        )
        self._run_missed_reservation_recovery()

    def _on_prevent_sleep_changed(self):
        """設定メニューの「自動スリープを抑止する」チェック切り替え時の保存処理"""
        self.manager.save_settings({'prevent_sleep': self.prevent_sleep_var.get()})
        self._update_sleep_prevention()

    def _update_sleep_prevention(self):
        """有効な予約が存在する間、または録音中は、Windowsのアイドルによる自動スリープを抑止する。

        SetThreadExecutionStateはユーザーの手動スリープ/休止操作や休止状態への
        移行までは防げないが、放置による自動スリープでの予約録音の取りこぼしは防げる。
        設定メニューでオフにしている場合はこの抑止を行わない。
        """
        should_prevent = self.prevent_sleep_var.get() and (
            self.manager.is_recording_active()
            or self.cm_fp_collector.is_running()
            or any(res.get('enabled', True) for res in self.manager.load_reservations())
        )
        if should_prevent == self._sleep_prevented:
            return
        try:
            flags = _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED if should_prevent else _ES_CONTINUOUS
            ctypes.windll.kernel32.SetThreadExecutionState(flags)
            self._sleep_prevented = should_prevent
        except (AttributeError, OSError) as e:
            logger.warning(f"スリープ抑止状態の変更に失敗しました: {e}")

    def _schedule_reservation_list_minute_refresh(self):
        """予約一覧の状態（成功/失敗/録音中）は時間経過で変わるため、毎分0秒に再描画する

        再描画中に例外が起きても再スケジュールだけは必ず行い、長時間起動時に
        このループ自体が止まってしまわないようにする
        """
        try:
            self._refresh_reservation_list()
        except Exception:
            logger.exception("予約一覧の再描画でエラーが発生しました")
        now = datetime.now()
        seconds_until_next_minute = 60 - now.second - now.microsecond / 1_000_000
        self._reservation_list_refresh_job = self.root.after(
            int(seconds_until_next_minute * 1000), self._schedule_reservation_list_minute_refresh
        )

    def _schedule_program_guide_minute_refresh(self):
        """番組表の「終了済み番組のグレーアウト」は時間経過で変わるため、毎分0秒に再描画する

        再描画中に例外が起きても再スケジュールだけは必ず行い、長時間起動時に
        このループ自体が止まってしまわないようにする
        """
        try:
            self.display_schedule(self._current_programs, mode=self.schedule_mode_var.get())
        except Exception:
            logger.exception("番組表の再描画でエラーが発生しました")
        now = datetime.now()
        seconds_until_next_minute = 60 - now.second - now.microsecond / 1_000_000
        self._program_guide_refresh_job = self.root.after(
            int(seconds_until_next_minute * 1000), self._schedule_program_guide_minute_refresh
        )

    def _check_due_reservations(self):
        """開始時刻を迎えた予約があれば録音を開始する

        局が異なれば複数の予約（および手動録音）を同時に進行できる。
        同じ局が既に録音中の場合はその予約だけ今回は見送り、猶予時間内であれば
        次回の巡回で再度試みる（実行済みマークは付けない）。
        """
        due = self.manager.get_due_reservations()
        started_any = False

        for reservation, start_dt, end_dt, occurrence_iso in due:
            station = reservation.get('station')

            if station not in self.manager.get_stations():
                self.manager.mark_reservation_run(reservation['id'], occurrence_iso, result='failed')
                started_any = True
                continue

            if self.manager.recording_reservation_id(station) == reservation['id']:
                # この予約自身が録音中（実行済みの印は録音が終わってから付くため、
                # 開始後も猶予時間のあいだはここに来る）
                continue

            if self.manager.is_recording_active(station) and not self._hand_off_recording(
                reservation, start_dt
            ):
                # 他の予約や手動録音でその局が使用中。マークせず次回の巡回で再試行する
                key = (reservation['id'], occurrence_iso)
                if key not in self._busy_skip_logged:
                    self._busy_skip_logged.add(key)
                    logger.info(
                        f"局が使用中のため予約録音の開始を見送りました（猶予時間内は再試行）: "
                        f"{station} {occurrence_iso} {reservation.get('start')} {reservation.get('title')}"
                    )
                continue

            started_any = True
            if self._start_reservation_recording(reservation, end_dt, occurrence_iso):
                continue

            # 開始に失敗した（開始時刻ちょうどの瞬断・認証エラー等）。ここで諦めると
            # その回は丸ごと録れないため、実行済みとして記録したうえで、番組終了まで
            # 再開を試み続ける
            self.manager.mark_reservation_run(reservation['id'], occurrence_iso, result='failed')
            key = (reservation['id'], occurrence_iso)
            self._reservation_resume_state[key] = {'attempts': 0, 'had_data': False}
            if self._schedule_reservation_resume(reservation['id'], occurrence_iso, end_dt):
                message = (
                    f"{station} の予約録音を開始できませんでした。\n"
                    "番組が終わるまで再試行を続けます。通信状況をご確認ください。"
                )
            else:
                self._reservation_resume_state.pop(key, None)
                message = f"{station} の予約録音を開始できませんでした。\n通信状況をご確認ください。"
            if self._tray_icon is not None:
                self._notify_via_tray("予約録音", message)
            else:
                # ここで直接ダイアログを出すと、閉じられるまでこの巡回処理から戻れず、
                # 次回の巡回が予約されない（＝以降の予約録音が始まらない）ため、後回しにする
                self.root.after(0, messagebox.showwarning, "予約録音", message)

        if started_any:
            self._refresh_reservation_list()

    def _hand_off_recording(self, reservation, record_start_dt):
        """同じ局で録音中の、隣り合う前の予約を止めて reservation に場所を空ける

        Returns:
            bool: 止めて録音できる状態になったか
        """
        station = reservation.get('station')
        prior_id = self.manager.recording_reservation_id(station)
        info = self._active_recordings.get(station)
        if prior_id is None or prior_id == reservation['id'] or info is None:
            return False
        if not reservation_logic.should_hand_off_recording(
            datetime.now(), record_start_dt, info['end_dt'],
            self.manager.recording_margin_seconds, self.RESERVATION_CHECK_INTERVAL_MS / 1000,
        ):
            return False
        logger.info(f"次の予約へ録音を引き継ぎます: {station} 「{info['title']}」→「{reservation.get('title')}」")
        self.manager.stop_recording(station)
        return not self.manager.is_recording_active(station)

    def _start_reservation_recording(self, reservation, end_dt, occurrence_iso):
        """予約 reservation の録音を、今から end_dt までの長さで開始する

        予約の開始時刻が来たとき（_check_due_reservations）と、開始失敗・途中中断の後に
        残りを録り直すとき（_resume_reservation_recording）の両方から呼ばれる。

        Returns:
            bool: 録音を開始できたか
        """
        station = reservation.get('station')
        # 分単位に切り上げると end_dt を最大1分過ぎても録音が続き、同じ局の次の予約の冒頭が欠ける
        duration_minutes = max(0.1, (end_dt - datetime.now()).total_seconds() / 60)
        file_format = self.format_var.get()
        try:
            mp3_bitrate = int(self.bitrate_var.get())
        except ValueError:
            mp3_bitrate = 192

        reservation_title = reservation.get('title')
        program = self._find_program_for_reservation(station, reservation)
        success, output_path = self.manager.start_recording(
            station, duration_minutes,
            file_format=file_format, mp3_bitrate=mp3_bitrate,
            title=reservation_title, filename_pattern=self.filename_pattern_var.get(),
            metadata=self._build_recording_metadata(
                station, program=program, fallback_title=reservation_title
            ),
            reservation_id=reservation['id'],
            on_complete=lambda had_data, path, error, rid=reservation['id'], occ=occurrence_iso, st=station:
                self.root.after(
                    0, self._on_reservation_recording_complete, rid, occ, st, had_data, path, error, end_dt
                )
        )
        if success:
            self._register_active_recording(station, reservation_title or station, duration_minutes)
            self._watch_background_recording(station)
        return success

    def _schedule_reservation_resume(self, reservation_id, occurrence_iso, end_dt):
        """開始失敗・途中中断した予約録音の再開を、少し待ってから試みるよう予約する

        Returns:
            bool: 再開を予約したか（番組の残り時間がほとんど無ければFalse）
        """
        key = (reservation_id, occurrence_iso)
        state = self._reservation_resume_state.setdefault(key, {'attempts': 0, 'had_data': False})
        delay = min(
            self.RESERVATION_RESUME_RETRY_SECONDS * (2 ** state['attempts']),
            self.RESERVATION_RESUME_RETRY_MAX_SECONDS,
        )
        remaining = (end_dt - datetime.now()).total_seconds()
        if remaining - delay < self.RESERVATION_RESUME_MIN_REMAINING_SECONDS:
            return False
        state['attempts'] += 1
        self.root.after(
            int(delay * 1000), self._resume_reservation_recording, reservation_id, occurrence_iso, end_dt
        )
        return True

    def _resume_reservation_recording(self, reservation_id, occurrence_iso, end_dt):
        """開始失敗・途中中断した予約録音の残りを、新しいファイルに録音し直す

        開始できなければ（通信がまだ復旧していない、同じ局を別の録音が使っている等）、
        番組が終わるまで _schedule_reservation_resume で繰り返し試みる。
        """
        key = (reservation_id, occurrence_iso)
        reservation = self.manager.get_reservation(reservation_id)
        if reservation is None or not reservation.get('enabled', True):
            # 待っている間に予約が削除・無効化された
            self._reservation_resume_state.pop(key, None)
            return

        station = reservation.get('station')
        started = False
        try:
            if not self.manager.is_recording_active(station):
                started = self._start_reservation_recording(reservation, end_dt, occurrence_iso)
        except Exception:
            logger.exception(f"予約録音の再開処理でエラーが発生しました: {station}")

        if started:
            logger.info(f"予約録音を再開しました: {station}")
            self._refresh_reservation_list()
        elif not self._schedule_reservation_resume(reservation_id, occurrence_iso, end_dt):
            logger.warning(f"予約録音を再開できないまま番組が終了しました: {station}")
            self._reservation_resume_state.pop(key, None)

    def _on_reservation_recording_complete(self, reservation_id, occurrence_iso, station, had_data, output_path,
                                           error, end_dt=None):
        """予約録音のバックグラウンドスレッド終了時（メインスレッドから呼ばれる）:
        実際にデータを取得できたかに基づいて予約の実行結果を記録する。

        録音開始時点では「配信URLの取得に成功したか」しか分からず、認証切れ等で
        セグメントを1つも受信できなかった場合でも見た目上は「開始成功」になってしまう
        （出力ファイルが0バイトのまま）。この結果を録音終了後に上書きすることで、
        予約一覧の状態表示（成功/失敗）が実態と一致するようにする。

        通信エラーで中断された場合、番組の残り時間（end_dt まで）があれば、残りを
        別のファイルに録音し直す（_schedule_reservation_resume）。一度でも中断が
        あった回は、最後まで録れても欠落があるため「成功」ではなく「中断」と記録する。
        """
        key = (reservation_id, occurrence_iso)
        state = self._reservation_resume_state.get(key)
        interrupted_before = state is not None
        any_data = had_data or (interrupted_before and state['had_data'])

        will_resume = False
        if error and end_dt is not None:
            state = self._reservation_resume_state.setdefault(key, {'attempts': 0, 'had_data': False})
            if had_data:
                # 録音できていた＝通信は一度復旧していたので、待ち時間を最初からやり直す
                state['attempts'] = 0
                state['had_data'] = True
            will_resume = self._schedule_reservation_resume(reservation_id, occurrence_iso, end_dt)
        if not will_resume:
            self._reservation_resume_state.pop(key, None)

        if error or interrupted_before:
            result = 'partial' if any_data else 'failed'
        else:
            result = 'success' if had_data else 'failed'
        self.manager.mark_reservation_run(reservation_id, occurrence_iso, result=result)
        self._refresh_reservation_list()
        if hasattr(self, 'files_tree'):
            self._refresh_files_list()

        if not had_data:
            if interrupted_before:
                # 再開を試みたが今回も取得できなかった。最初の中断・開始失敗の時点で
                # 既に知らせているため、試行のたびに通知を繰り返さない
                return
            detail = f"\n（{error}）" if error else ""
            retry_note = "\n番組が終わるまで再試行を続けます。" if will_resume else ""
            message = (
                f"{station} の予約録音でデータを取得できませんでした（ファイルが空です）。\n"
                f"通信状況をご確認ください。{retry_note}{detail}"
            )
        elif error:
            resume_note = (
                "通信が復旧しだい、残りを別のファイルに録音します。\n" if will_resume else ""
            )
            message = (
                f"{station} の予約録音は途中で通信エラーが発生し、中断されました。\n"
                f"それまでの内容は保存されています。\n{resume_note}"
                f"保存先: {output_path}\n（{error}）"
            )
        else:
            if self._tray_icon is not None:
                note = "（途中で中断があったため、ファイルが分かれています）" if interrupted_before else ""
                self._notify_via_tray("予約録音", f"{station} の予約録音が完了しました。{note}")
            return

        if self._tray_icon is not None:
            self._notify_via_tray("予約録音", message)
        else:
            messagebox.showwarning("予約録音", message)
