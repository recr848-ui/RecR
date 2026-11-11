"""取り逃した予約録音の通知と、タイムフリー/聞き逃しでの自動回収"""

from tkinter import messagebox
from datetime import datetime, timedelta
import logging
import threading

from utils import reservation_logic

logger = logging.getLogger(__name__)


class MissedRecoveryMixin:
    def _reservation_occurrence_program_dict(self, reservation):
        """予約の直近の回（1回のみならその日、毎週なら直近のその曜日の日）を、
        _program_air_window 等が扱える「番組」風の辞書にして返す（計算できなければNone）
        """
        return reservation_logic.reservation_occurrence_program_dict(reservation)

    def _reservation_is_overdue_pending(self, reservation):
        """予約が「待機中」のまま、録音終了予定時刻を過ぎてしまっているか
        （＝実行されるはずだったのに実行されなかった予約）を判定する
        """
        return reservation_logic.reservation_is_overdue_pending(
            reservation, self.manager.is_recording_active
        )

    def _reservation_needs_timefree_recovery(self, reservation):
        """この予約をタイムフリーで取り直す価値があるか（失敗・中断・実行し損ねて
        終了時刻を過ぎた待機中、のいずれか。既にタイムフリーで取得済みなら対象外）"""
        return reservation_logic.reservation_needs_timefree_recovery(
            reservation, self.manager.is_recording_active
        )

    def _notify_missed_reservations_on_startup(self):
        """起動時: 取り逃した予約（失敗・中断・実行され損ねた待機中）があれば通知する

        予約録音タブの「タイムフリー」列からいつでも後追いでダウンロードできるが、
        気づかず取りこぼしたままになるのを防ぐため、起動のたびに一度だけ知らせる。
        """
        missed = [
            r for r in self.manager.load_reservations()
            if self._reservation_needs_timefree_recovery(r)
        ]
        if self.auto_recover_missed_var.get():
            # 自動取得の対象になるもの（これから試すもの）は知らせず、
            # 自動取得を諦めたもの・期限切れのものだけを知らせる
            missed = [r for r in missed if self._missed_recovery_plan(r) is None]
        if not missed:
            return
        message = (
            f"取り逃した予約が{len(missed)}件あります。\n"
            "予約録音タブの「タイムフリー」列からダウンロードできます。"
        )
        if self._tray_icon is not None:
            self._notify_via_tray("予約録音", message)
        else:
            messagebox.showinfo("予約録音", message)

    # 取り逃した予約の自動取得: 巡回間隔、放送終了から取得を試み始めるまでの待ち時間、
    # 1回分の放送について試みる回数の上限（NHKの再放送枠のように聴き逃しに載らない
    # 回もあるため、いつまでも試し続けない）
    MISSED_RECOVERY_INTERVAL_MS = 30 * 60 * 1000
    MISSED_RECOVERY_DELAY_SECONDS = 15 * 60
    MISSED_RECOVERY_MAX_ATTEMPTS = 8
    # タイムフリー・聴き逃しで取得できる、放送終了からの期間
    MISSED_RECOVERY_WINDOW = timedelta(days=7)

    def _missed_recovery_plan(self, reservation, now=None):
        """取り逃した予約を自動取得の対象にするかを判定する

        Returns:
            dict または None: 対象なら {'program', 'start_dt', 'end_dt', 'occurrence',
            'attempts', 'ready'}（'ready' は放送終了から十分経ち、今すぐ試みてよいか）。
            取り逃していない・期限切れ・試行回数の上限に達した場合は None
        """
        now = now or datetime.now()
        if not self._reservation_needs_timefree_recovery(reservation):
            return None
        program = self._reservation_occurrence_program_dict(reservation)
        if program is None:
            return None
        start_dt, end_dt = self._program_air_window(program)
        if start_dt is None or end_dt is None:
            return None
        occurrence = start_dt.date().isoformat()
        # 失敗・中断の記録が別の回（毎週予約の先週分など）のものなら対象外
        if reservation.get('last_result') in ('failed', 'partial') and reservation.get('last_run_date') != occurrence:
            return None
        if now - end_dt > self.MISSED_RECOVERY_WINDOW:
            return None
        state = reservation.get('auto_recovery') or {}
        attempts = state.get('attempts', 0) if state.get('occurrence') == occurrence else 0
        if attempts >= self.MISSED_RECOVERY_MAX_ATTEMPTS:
            return None
        return {
            'program': program, 'start_dt': start_dt, 'end_dt': end_dt,
            'occurrence': occurrence, 'attempts': attempts,
            'ready': now >= end_dt + timedelta(seconds=self.MISSED_RECOVERY_DELAY_SECONDS),
        }

    def _schedule_missed_reservation_recovery(self):
        """取り逃した予約の自動取得を一定間隔で試みる（例外が起きても再スケジュールする）"""
        try:
            self._run_missed_reservation_recovery()
        except Exception:
            logger.exception("取り逃した予約の自動取得でエラーが発生しました")
        self._missed_recovery_job = self.root.after(
            self.MISSED_RECOVERY_INTERVAL_MS, self._schedule_missed_reservation_recovery
        )

    def _on_auto_recover_missed_changed(self):
        """設定メニューの「取り逃した予約を自動取得する」切り替え時の保存処理"""
        enabled = self.auto_recover_missed_var.get()
        self.manager.save_settings({'auto_recover_missed': enabled})
        if enabled:
            self._run_missed_reservation_recovery()

    def _run_missed_reservation_recovery(self):
        """取り逃した予約（失敗・中断・実行されなかった待機中）を、radikoの
        タイムフリー（NHKは聴き逃し配信）で自動的に取得する

        アプリが止まっていた・PCがスリープしていた間の回を、次に動き出したときに
        回収するためのもの。起動直後・一定間隔・予約チェックの巡回が大きく空いた
        直後（スリープ復帰など）に呼ばれる。放送終了から少し待ってから試み、
        取得できなければ次の巡回で再び試みる（1回分の放送につき上限回数まで）。
        通信を伴うためバックグラウンドスレッドで1件ずつ処理する。
        """
        if not self.auto_recover_missed_var.get() or self._missed_recovery_in_progress:
            return
        now = datetime.now()
        file_format = self.format_var.get()
        try:
            mp3_bitrate = int(self.bitrate_var.get())
        except ValueError:
            mp3_bitrate = 192
        filename_pattern = self.filename_pattern_var.get()

        targets = []
        for reservation in self.manager.load_reservations():
            plan = self._missed_recovery_plan(reservation, now=now)
            if plan is None or not plan['ready']:
                continue
            station = reservation.get('station')
            title = reservation.get('title') or station
            plan.update({
                'reservation_id': reservation['id'],
                'station': station,
                'title': title,
                'metadata': self._build_recording_metadata(
                    station, program=plan['program'], fallback_title=title
                ),
            })
            targets.append(plan)
        if not targets:
            return

        self._missed_recovery_in_progress = True
        threading.Thread(
            target=self._missed_recovery_worker,
            args=(targets, file_format, mp3_bitrate, filename_pattern),
            daemon=True,
        ).start()

    def _missed_recovery_worker(self, targets, file_format, mp3_bitrate, filename_pattern):
        """_run_missed_reservation_recovery のバックグラウンドスレッド本体"""
        try:
            for target in targets:
                try:
                    self._recover_missed_reservation(target, file_format, mp3_bitrate, filename_pattern)
                except Exception as e:
                    logger.exception(f"取り逃した予約の自動取得に失敗しました: {target['station']} {target['title']}")
                    self.root.after(0, self._on_missed_recovery_finished, target, None, False, str(e))
        finally:
            self.root.after(0, self._on_missed_recovery_worker_done)

    def _on_missed_recovery_worker_done(self):
        self._missed_recovery_in_progress = False

    def _recover_missed_reservation(self, target, file_format, mp3_bitrate, filename_pattern):
        """取り逃した予約1件を取得する（バックグラウンドスレッドから呼ばれる）

        radikoのタイムフリーは取得を開始したら戻る（完了は on_complete で通知される）。
        NHK聴き逃しはこのスレッド内でダウンロードし終えるまで戻らない。
        """
        station, title = target['station'], target['title']
        start_dt, end_dt = target['start_dt'], target['end_dt']
        ft = start_dt.strftime("%Y%m%d%H%M%S")

        if self._is_nhk_station(station):
            key = f"{station}|radiru|{ft}"
        else:
            key = f"{station}|{ft}"
            if self.manager.is_download_active(station, ft):
                return
            if self.manager.is_download_limit_reached():
                # 試行回数は数えずに、次の巡回で改めて試みる
                logger.info(f"タイムフリーの同時ダウンロード数が上限のため、自動取得を次回に回します: {station} {title}")
                return

        attempts = target['attempts'] + 1
        self.manager.update_reservation(target['reservation_id'], {
            'auto_recovery': {'occurrence': target['occurrence'], 'attempts': attempts}
        })
        target['attempts'] = attempts
        logger.info(
            f"取り逃した予約を自動取得します（{attempts}/{self.MISSED_RECOVERY_MAX_ATTEMPTS}回目）: "
            f"{station} {start_dt.strftime('%Y-%m-%d %H:%M')} {title}"
        )

        if self._is_nhk_station(station):
            self._recover_missed_reservation_via_radiru(target, key, ft, filename_pattern)
            return

        self.root.after(0, self._register_active_download, key, station, ft, title)
        success, output_path = self.manager.start_timefree_download(
            station, ft, end_dt.strftime("%Y%m%d%H%M%S"),
            file_format=file_format, mp3_bitrate=mp3_bitrate,
            title=title, filename_pattern=filename_pattern, metadata=target['metadata'],
            on_progress=lambda done, total, k=key: self.root.after(
                0, self._update_active_download_progress, k, done, total
            ),
            on_complete=lambda had_data, path, error, k=key, t=target: self.root.after(
                0, self._on_missed_recovery_finished, t, k, had_data and not error, error
            ),
        )
        if not success:
            self.root.after(0, self._on_missed_recovery_finished, target, key, False, "タイムフリーの取得を開始できませんでした")

    def _recover_missed_reservation_via_radiru(self, target, key, ft, filename_pattern):
        """NHKの取り逃した予約を、らじる★らじるの聴き逃し配信から取得する
        （バックグラウンドスレッドから呼ばれる）"""
        station, title = target['station'], target['title']
        start_dt, end_dt = target['start_dt'], target['end_dt']
        entry = self.radiru_manager.find_series_for_program(
            self._nhk_radio_broadcast_code(station), title, None
        )
        if entry is None:
            self.root.after(0, self._on_missed_recovery_finished, target, None, False,
                            "NHK聴き逃しのインデックスに番組が見つかりません")
            return
        episodes = self.radiru_manager.fetch_series_episodes(entry['series_site_id'], entry['corner_site_id'])
        episode = self.radiru_manager.find_episode_for_date(episodes, start_dt, end_dt)
        stream_url = episode.get('stream_url') if episode else None
        if not stream_url:
            # 再放送の枠（聴き逃しは本放送のみ）や、まだ配信が始まっていない回
            self.root.after(0, self._on_missed_recovery_finished, target, None, False,
                            "NHK聴き逃しに該当する回がありません")
            return

        filename = self.manager.build_recording_filename(filename_pattern, station, title, start_dt, "m4a")
        output_path = self.manager.unique_output_path(self.manager.output_dir / filename)
        self.root.after(0, self._register_active_download, key, station, ft, title)
        error = None
        try:
            self.radiru_manager.download_episode(
                stream_url, output_path,
                on_progress=lambda done, total, k=key: self.root.after(
                    0, self._update_active_download_progress, k, done, total
                ),
            )
            self.manager.write_metadata_tags(output_path, "m4a", target['metadata'])
        except Exception as e:
            logger.exception(f"NHK聴き逃しの自動取得に失敗しました ({title})")
            error = str(e)
        self.root.after(0, self._on_missed_recovery_finished, target, key, error is None, error)

    def _on_missed_recovery_finished(self, target, key, success, error):
        """取り逃した予約1件の自動取得の結果を反映する（メインスレッドから呼ばれる）"""
        if key is not None:
            self._unregister_active_download(key)
            if hasattr(self, 'files_tree'):
                self._refresh_files_list()
        station, title = target['station'], target['title']
        when = target['start_dt'].strftime('%m/%d %H:%M')
        if success:
            logger.info(f"取り逃した予約を自動取得しました: {station} {when} {title}")
            self._mark_reservation_timefree_downloaded(target['reservation_id'])
            if self._tray_icon is not None:
                self._notify_via_tray("予約録音", f"取り逃した「{title}」（{when}放送）を自動取得しました。")
            return

        logger.info(f"取り逃した予約の自動取得に失敗しました: {station} {when} {title}（{error}）")
        if target['attempts'] >= self.MISSED_RECOVERY_MAX_ATTEMPTS:
            logger.warning(f"取り逃した予約の自動取得を諦めました: {station} {when} {title}")
            if self._tray_icon is not None:
                self._notify_via_tray(
                    "予約録音", f"取り逃した「{title}」（{when}放送）を自動取得できませんでした。"
                )
