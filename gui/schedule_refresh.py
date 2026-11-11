"""番組表の取得・キャッシュ更新（手動一括・起動時・毎日の自動更新、らじる索引）"""

import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timedelta
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from utils.radiru_manager import RadiruManager

logger = logging.getLogger(__name__)


class ScheduleRefreshMixin:
    def _open_schedule_fetch_progress_dialog(self, title):
        """番組表取得中に表示する進捗ダイアログを開く

        「キャンセル」ボタンを押すと戻り値のイベントがセットされ、以降まだ
        着手していない局・日の取得が順次打ち切られる（実行中のリクエストは
        完了を待つため、キャンセル後も少しの間だけ処理が続くことがある）。

        Returns:
            (tk.Toplevel, tk.StringVar, ttk.Progressbar, threading.Event)
        """
        cancel_event = threading.Event()
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)

        progress_var = tk.StringVar(value="準備中...")
        ttk.Label(frame, textvariable=progress_var, justify=tk.LEFT, width=42).pack(
            anchor=tk.W, pady=(0, 12)
        )

        progressbar = ttk.Progressbar(frame, mode='determinate', length=300)
        progressbar.pack(fill=tk.X, pady=(0, 15))

        def on_cancel():
            cancel_event.set()
            cancel_button.configure(state='disabled', text="キャンセル中...")

        cancel_button = ttk.Button(frame, text="キャンセル", command=on_cancel)
        cancel_button.pack()

        dialog.protocol("WM_DELETE_WINDOW", on_cancel)

        self._center_dialog_over_parent(dialog)
        dialog.deiconify()
        dialog.grab_set()

        return dialog, progress_var, progressbar, cancel_event

    def _update_schedule_fetch_progress(self, progress_var, progressbar, completed, total, station):
        """進捗ダイアログの表示を更新する（メインスレッドから呼ばれる）"""
        progress_var.set(f"{completed}/{total}局  {station} 完了")
        progressbar.configure(value=completed)

    def _fetch_and_cache_schedule_no_ui(self, station, cancel_event):
        """番組表とタイムフリー番組表を取得してキャッシュに保存する（バックグラウンドスレッド用）

        fetch_and_cache_schedule と異なり、Tkinterウィジェットには一切触れない
        （フリーワード一致の有無だけを返し、予約一覧UIへの反映は呼び出し元が
        メインスレッドでまとめて行う）。

        Returns:
            (bool success, bool any_created, str or None error_label)
        """
        try:
            programs = self.manager.get_program_schedule(
                station, days=self.SCHEDULE_FETCH_DAYS, cancel_event=cancel_event
            )
            success = bool(programs)
            if not programs:
                programs = self.manager.get_sample_schedule(station, days=self.SCHEDULE_FETCH_DAYS)
            self.manager.save_schedule_cache(station, programs)
            any_created = success and self.manager.scan_freewords_for_station(station, programs)
        except Exception as e:
            return False, False, f"{station}（{e}）"

        if not cancel_event.is_set():
            self._fetch_and_cache_timefree_schedule(station, cancel_event)

        return success, any_created, (None if success else station)

    def _fetch_stations_with_progress(self, stations, title, on_done):
        """複数局の番組表を、進捗ダイアログ（キャンセル可）を出しつつ並列取得する

        局ごとに独立したHTTPリクエストであるため、複数局を同時に並列取得する
        ことで所要時間を短縮する（詳細は SCHEDULE_STATION_FETCH_WORKERS）。
        完了後、メインスレッドで
        on_done(dialog, cancelled, failed_stations, refreshed_stations, any_created) を呼ぶ。
        """
        dialog, progress_var, progressbar, cancel_event = self._open_schedule_fetch_progress_dialog(title)
        progressbar.configure(maximum=len(stations))
        progress_var.set(f"0/{len(stations)}局 取得中...")

        def worker():
            failed_stations = []
            refreshed_stations = []
            any_created = False
            completed = 0
            lock = threading.Lock()

            def fetch_one(station):
                nonlocal completed, any_created
                if cancel_event.is_set():
                    return
                success, created, error_label = self._fetch_and_cache_schedule_no_ui(station, cancel_event)
                with lock:
                    if success:
                        refreshed_stations.append(station)
                    else:
                        failed_stations.append(error_label)
                    if created:
                        any_created = True
                    completed += 1
                    n = completed
                self.root.after(
                    0, self._update_schedule_fetch_progress, progress_var, progressbar, n,
                    len(stations), station
                )

            with ThreadPoolExecutor(max_workers=self.SCHEDULE_STATION_FETCH_WORKERS) as executor:
                list(executor.map(fetch_one, stations))

            self.root.after(
                0, on_done, dialog, cancel_event.is_set(), failed_stations, refreshed_stations,
                any_created
            )

        threading.Thread(target=worker, daemon=True).start()

    def _close_schedule_fetch_progress_dialog(self, dialog, any_created):
        """進捗ダイアログを閉じ、取得結果を予約一覧・表示中の番組表に反映する"""
        dialog.grab_release()
        dialog.destroy()
        if any_created:
            self._refresh_reservation_list()
        self.load_schedule_for_current_station()

    def _bulk_refresh_schedules(self):
        """全ステーションの番組表を一括で再取得してキャッシュを更新する"""
        if not messagebox.askyesno(
            "番組表の一括更新",
            "すべてのステーションの番組表を再取得します。時間がかかる場合があります。よろしいですか？"
        ):
            return
        self._fetch_stations_with_progress(
            self.manager.get_stations(), "番組表の一括更新",
            lambda dialog, cancelled, failed, _refreshed, any_created:
                self._on_bulk_refresh_done(dialog, cancelled, failed, any_created),
        )

    def _on_bulk_refresh_done(self, dialog, cancelled, failed_stations, any_created):
        """番組表一括更新の完了処理（メインスレッドで実行）"""
        self._close_schedule_fetch_progress_dialog(dialog, any_created)

        if cancelled:
            messagebox.showinfo("番組表の一括更新", "取得をキャンセルしました。")
        elif failed_stations:
            messagebox.showwarning(
                "番組表の一括更新",
                "以下の局は番組表の取得に失敗しました（サンプルデータで代用しています）:\n\n"
                + "\n".join(failed_stations)
            )
        else:
            messagebox.showinfo("番組表の一括更新", "すべてのステーションの番組表を更新しました。")

    def fetch_and_cache_schedule(self, station):
        """番組表を取得してキャッシュに保存する

        Returns:
            (list, bool): 取得結果のprogramsリストと、実データの取得に成功したかどうか。
            実APIが空を返した場合はサンプルデータで代用し、成功フラグは False になる。
        """
        programs = self.manager.get_program_schedule(station, days=self.SCHEDULE_FETCH_DAYS)
        success = bool(programs)
        if not programs:
            programs = self.manager.get_sample_schedule(station, days=self.SCHEDULE_FETCH_DAYS)
        self.manager.save_schedule_cache(station, programs)
        if success:
            self._scan_freewords_and_refresh(station, programs)
        # 通常の番組表と同じタイミングで、タイムフリー（過去7日間）のキャッシュも
        # 一緒に更新する（開いたことのない局のタイムフリーがいつまでも古いまま
        # 残ってしまわないようにするため）。失敗しても通常の番組表更新自体は
        # 失敗扱いにしないベストエフォート
        self._fetch_and_cache_timefree_schedule(station)
        return programs, success

    def _fetch_and_cache_timefree_schedule(self, station, cancel_event=None):
        """指定局のタイムフリー（過去7日間）番組表を取得してキャッシュに保存する

        通常の番組表の更新から常に一緒に呼ばれる。失敗してもログに残すだけの
        ベストエフォート（通常の番組表更新自体は失敗扱いにしない）。
        """
        try:
            timefree_programs = self.manager.get_timefree_schedule(station, cancel_event=cancel_event)
            if timefree_programs:
                self.manager.save_schedule_cache(f"{station}::timefree", timefree_programs)
        except Exception:
            logger.exception(f"タイムフリー番組表の更新に失敗しました: {station}")

    def _scan_freewords_and_refresh(self, station, programs):
        """番組表更新後: フリーワードと照合して新規予約を自動作成し、
        予約一覧タブが既に構築済みならその場で表示を更新する
        """
        created = self.manager.scan_freewords_for_station(station, programs)
        if created and hasattr(self, 'reservation_tree'):
            self._refresh_reservation_list()

    def _refresh_stale_stations(self):
        """起動時: 当日以降の情報が一定日数以下しかない局の番組表を自動更新する

        局数が多いと時間がかかるため、進捗ダイアログ（キャンセル可）を表示しつつ
        複数局を並列でバックグラウンド取得する（_fetch_stations_with_progress）。
        """
        stale_stations = [
            s for s in self.manager.get_stations()
            if self.manager.count_cached_future_days(s) <= self.SCHEDULE_STALE_THRESHOLD_DAYS
        ]
        if not stale_stations:
            return

        self._stale_refresh_in_progress = True
        today_iso = datetime.now().strftime("%Y-%m-%d")
        logger.info(f"起動時の番組表自動更新を開始します（{len(stale_stations)}局）")
        self._fetch_stations_with_progress(
            stale_stations, "番組表の自動更新",
            lambda dialog, _cancelled, failed, refreshed, any_created:
                self._on_stale_refresh_done(dialog, any_created, failed, refreshed, today_iso),
        )

    def _on_stale_refresh_done(self, dialog, any_created, failed_stations,
                               refreshed_stations, today_iso):
        """起動時の自動番組表更新の完了処理（メインスレッドで実行）"""
        self._stale_refresh_in_progress = False
        self._stations_refreshed_today = (today_iso, set(refreshed_stations))
        # 実行中に見送った毎日の全局自動更新を、次のチェック（既定10分後）を待たずに判定し直す
        if self._full_refresh_check_job:
            self.root.after_cancel(self._full_refresh_check_job)
        self._check_full_schedule_refresh_due()

        self._close_schedule_fetch_progress_dialog(dialog, any_created)

        if failed_stations:
            messagebox.showwarning(
                "番組表の自動更新",
                "以下の局は番組表の取得に失敗しました（サンプルデータで代用しています）:\n\n"
                + "\n".join(failed_stations)
            )

    def _check_full_schedule_refresh_due(self):
        """全局番組表の自動更新（1日1回）が必要かを定期的にチェックする

        アプリを起動しっぱなしにしていても、フリーワード予約が新着番組を
        取りこぼさないよう、毎日 FULL_SCHEDULE_REFRESH_TIME（既定 05:00）を
        過ぎたタイミングで、その日まだ実行していなければ全局の番組表を
        取得し直す。対象時刻ちょうどを狙うのではなく「過ぎていればすぐ実行」
        にすることで、チェック間隔（既定10分）のズレや、対象時刻をまたいで
        アプリを起動した場合でも確実にその日のうちに1回は実行される。
        """
        now = datetime.now()
        try:
            target_time = datetime.strptime(self.FULL_SCHEDULE_REFRESH_TIME, "%H:%M").time()
        except ValueError:
            target_time = datetime.strptime("05:00", "%H:%M").time()
        target_dt = datetime.combine(now.date(), target_time)

        last_date = self.manager.load_settings().get('last_full_schedule_refresh_date')
        if (
            now >= target_dt
            and last_date != now.strftime("%Y-%m-%d")
            and not self._full_refresh_in_progress
            and not self._stale_refresh_in_progress
        ):
            self._start_full_schedule_refresh()

        self._full_refresh_check_job = self.root.after(
            self.FULL_SCHEDULE_REFRESH_CHECK_INTERVAL_MS, self._check_full_schedule_refresh_due
        )

    def _check_radiru_index_refresh_due(self):
        """NHK聴き逃し（らじる★らじる）の番組インデックス更新が必要かを定期的にチェックする

        全局番組表自動更新と違い「1日1回、特定時刻を過ぎたら」ではなく、
        new_arrivalsが特定の時刻に紐づく情報ではないため「前回取得から
        RadiruManager.INDEX_REFRESH_INTERVAL_HOURS時間経過したら」で判定する。
        アプリを起動していなかった間の分は、次に起動した直後のチェックで
        即座に取得される。
        """
        last_iso = self.manager.load_settings().get('last_radiru_index_fetch')
        due = True
        if last_iso:
            try:
                last_dt = datetime.fromisoformat(last_iso)
                due = (datetime.now() - last_dt) >= timedelta(
                    hours=RadiruManager.INDEX_REFRESH_INTERVAL_HOURS
                )
            except ValueError:
                due = True

        if due and not self._radiru_refresh_in_progress:
            self._start_radiru_index_refresh()

        self._radiru_refresh_check_job = self.root.after(
            self.FULL_SCHEDULE_REFRESH_CHECK_INTERVAL_MS, self._check_radiru_index_refresh_due
        )

    def _start_radiru_index_refresh(self):
        """NHK聴き逃しの新着番組インデックスをバックグラウンドスレッドで更新する"""
        self._radiru_refresh_in_progress = True
        thread = threading.Thread(target=self._radiru_index_refresh_worker, daemon=True)
        thread.start()

    def _radiru_index_refresh_worker(self):
        try:
            self.radiru_manager.update_index()
            self.manager.save_settings({
                'last_radiru_index_fetch': datetime.now().isoformat(timespec="seconds")
            })
        except Exception:
            logger.exception("NHK聴き逃しインデックスの更新に失敗しました")
        finally:
            self._radiru_refresh_in_progress = False

    def _start_full_schedule_refresh(self):
        """全局の番組表をバックグラウンドスレッドで取得し直す（UIは一切ブロックしない）"""
        self._full_refresh_in_progress = True
        today_iso = datetime.now().strftime("%Y-%m-%d")
        stations = self._stations_for_full_refresh(today_iso)
        thread = threading.Thread(
            target=self._full_schedule_refresh_worker, args=(stations, today_iso), daemon=True
        )
        thread.start()

    def _stations_for_full_refresh(self, today_iso):
        """毎日の全局自動更新で取得する局（その日に起動時の自動更新で取得済みの局は除く）"""
        stations = self.manager.get_stations()
        refreshed_date, refreshed = self._stations_refreshed_today
        if refreshed_date != today_iso or not refreshed:
            return stations
        remaining = [s for s in stations if s not in refreshed]
        logger.info(
            f"起動時の自動更新で取得済みの{len(stations) - len(remaining)}局は全局自動更新から除きます"
        )
        return remaining

    def _full_schedule_refresh_worker(self, stations, today_iso):
        """バックグラウンドスレッド本体: 全局を順に取得し、フリーワード照合も行う。
        Tkinterウィジェットには一切触れず、完了後の反映は root.after 経由で行う。

        通常の番組表（未来方向）だけでなく、タイムフリー対象期間（過去7日間）の
        番組表もここで一緒に更新する。タイムフリーのキャッシュは以前、この自動更新の
        対象に一切含まれておらず、該当局のタイムフリータブを開かない限り何日経っても
        更新されなかった（対象期間を丸ごと過ぎ去ってから開くと空の番組表になる）ため
        追加した。フリーワードによる自動予約作成は未来の番組のみが対象なので、
        タイムフリー側ではキャッシュの保存のみ行い、フリーワード照合は行わない。
        """
        logger.info(f"全局番組表自動更新を開始します（{len(stations)}局）")
        failed_stations = []
        any_created = False
        lock = threading.Lock()

        def fetch_one(station):
            nonlocal any_created
            try:
                programs = self.manager.get_program_schedule(station, days=self.SCHEDULE_FETCH_DAYS)
                if not programs:
                    with lock:
                        failed_stations.append(station)
                else:
                    self.manager.save_schedule_cache(station, programs)
                    if self.manager.scan_freewords_for_station(station, programs):
                        with lock:
                            any_created = True
            except Exception as e:
                with lock:
                    failed_stations.append(f"{station}（{e}）")

            try:
                timefree_programs = self.manager.get_timefree_schedule(station)
                if not timefree_programs:
                    with lock:
                        failed_stations.append(f"{station}（タイムフリー）")
                else:
                    self.manager.save_schedule_cache(f"{station}::timefree", timefree_programs)
            except Exception as e:
                with lock:
                    failed_stations.append(f"{station}（タイムフリー: {e}）")

        with ThreadPoolExecutor(max_workers=self.SCHEDULE_STATION_FETCH_WORKERS) as executor:
            list(executor.map(fetch_one, stations))

        self.manager.prune_stale_schedule_cache()
        self.manager.prune_image_cache()
        self.manager.save_settings({'last_full_schedule_refresh_date': today_iso})
        logger.info(
            f"全局番組表自動更新が完了しました（失敗{len(failed_stations)}件）"
        )
        self.root.after(0, self._on_full_schedule_refresh_done, failed_stations, any_created)

    def _on_full_schedule_refresh_done(self, failed_stations, any_created):
        """全局自動更新の完了処理（メインスレッドで実行）: 表示中の番組表・予約一覧に反映する"""
        self._full_refresh_in_progress = False

        if any_created:
            self._refresh_reservation_list()

        station = self.schedule_station_var.get()
        if self.schedule_mode_var.get() == 'timefree':
            cached = self.manager.load_cached_schedule(f"{station}::timefree")
            if cached and self._has_programs_in_timefree_window(cached):
                self.display_schedule(cached, mode='timefree')
        else:
            cached = self.manager.load_cached_schedule(station)
            if cached:
                self.display_schedule(cached)

        if failed_stations:
            logger.warning(f"[全局自動更新] 取得に失敗した局: {', '.join(failed_stations)}")
