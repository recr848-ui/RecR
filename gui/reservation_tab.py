"""「予約録音」タブ：予約一覧と予約ダイアログ"""

import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime
import math


class ReservationTabMixin:
    def _open_reservation_dialog_from_program(self, program):
        """番組表で番組をダブルクリックした時の処理

        現在放送中の番組であれば、予約ではなく「今すぐ録音するか」を確認するダイアログを出す。
        すでに終了した過去の番組（当日内で放送済みのもの）は、タイムフリーの取得確認ダイアログを開く。
        それ以外（未来の番組）は、その番組の内容を入力済みの状態で新規予約録音ダイアログを開く。
        """
        now = datetime.now()
        start_dt, end_dt = self._program_air_window(program)
        if start_dt and start_dt <= now < end_dt:
            self._confirm_immediate_recording(program, end_dt)
            return

        if end_dt and end_dt <= now:
            self._open_timefree_download_dialog(program)
            return

        date_iso = program.get('date_iso') or datetime.now().strftime("%Y-%m-%d")
        start = program.get('start') or '00:00'
        end = program.get('end') or '01:00'
        prefill = {
            'station': self.schedule_station_var.get(),
            'repeat': 'once',
            'date_iso': self._program_actual_date_iso(date_iso, start),
            'start': start,
            'end': end,
            'title': program.get('title', ''),
        }
        self._open_reservation_dialog(prefill=prefill)

    def _confirm_immediate_recording(self, program, end_dt):
        """現在放送中の番組をダブルクリックした時: 確認の上、残り時間分をその場で録音開始する

        録音タブのStart/Stopボタンは常に1局分しか状態を保持できないため、ここでは
        使わず、局ごとに独立して追跡する_watch_background_recordingに任せる。
        こうすることで、複数局を続けて「今すぐ録音」しても互いを上書きしない。
        """
        station = self.schedule_station_var.get()
        title = program.get('title') or station
        if not messagebox.askyesno(
            "録音", f"「{title}」は現在放送中です。今すぐ録音を開始しますか？"
        ):
            return
        if self.manager.is_recording_active(station):
            messagebox.showerror("エラー", f"{station} は既に録音中です")
            return

        duration_minutes = max(1, math.ceil((end_dt - datetime.now()).total_seconds() / 60))
        file_format = self.format_var.get()
        try:
            mp3_bitrate = int(self.bitrate_var.get())
        except ValueError:
            mp3_bitrate = 192

        self._set_app_busy(True)
        try:
            success, output_path = self.manager.start_recording(
                station, duration_minutes, file_format=file_format, mp3_bitrate=mp3_bitrate,
                title=title, filename_pattern=self.filename_pattern_var.get(),
                metadata=self._build_recording_metadata(station, program=program, fallback_title=title),
                on_complete=lambda had_data, path, error, st=station: self.root.after(
                    0, self._on_manual_recording_complete, st, had_data, path, error
                )
            )
        finally:
            self._set_app_busy(False)

        if success:
            self._register_active_recording(station, title, duration_minutes)
            self._watch_background_recording(station)
        else:
            messagebox.showerror(
                "録音エラー",
                f"{station} の録音を開始できませんでした。\n通信状況をご確認ください。"
            )

    def setup_reservation_tab(self, parent):
        """「予約録音」タブのUIをセットアップ（予約の一覧・追加・編集・削除）"""
        control_frame = ttk.Frame(parent, padding=10)
        control_frame.pack(fill=tk.X)

        ttk.Button(
            control_frame, text="新規予約...", command=self._open_reservation_dialog
        ).pack(side=tk.LEFT)
        ttk.Button(
            control_frame, text="編集...", command=self._edit_selected_reservation
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="有効にする",
            command=lambda: self._set_selected_reservations_enabled(True)
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="無効にする",
            command=lambda: self._set_selected_reservations_enabled(False)
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="削除", command=self._delete_selected_reservation
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="終了した予約を削除", command=self._delete_finished_reservations
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="録音を停止", command=self._stop_selected_reservation_recording
        ).pack(side=tk.LEFT, padx=5)

        list_frame = ttk.Frame(parent, padding=(10, 0, 10, 10))
        list_frame.pack(fill=tk.BOTH, expand=True)

        # 日付と時刻は1つの列にまとめる（日付で並べ替えたとき、同じ日の中も時刻順に並ぶように）
        columns = [
            ("enabled", "有効", 36),
            ("station", "局", 90),
            ("schedule", "日時/繰り返し", 210),
            ("title", "番組名", 240),
            ("source", "由来", 120),
            ("status", "状態", 100),
            ("download", "タイムフリー", 90),
        ]
        tree = self._create_list_treeview(list_frame, columns, self._edit_selected_reservation)
        tree.bind("<Button-1>", self._on_reservation_tree_click)

        self.reservation_tree = tree
        self._reservation_row_map = {}
        self._refresh_reservation_list()

    def _refresh_reservation_list(self):
        """予約録音の一覧表示をマネージャー側の最新データで作り直す"""
        tree = self.reservation_tree
        view = self._capture_treeview_view(tree, self._reservation_row_map)
        tree.delete(*tree.get_children())
        self._reservation_row_map = {}

        for res in self.manager.load_reservations():
            if res.get('repeat') == 'weekly':
                weekday = res.get('weekday', 0)
                schedule_text = f"毎週{self.manager.WEEKDAY_JA[weekday]}曜日"
            else:
                schedule_text = res.get('date_iso', '')
            schedule_text = f"{schedule_text} {res.get('start', '')}-{res.get('end', '')}"

            if self.manager.is_recording_active(res.get('station'), reservation_id=res.get('id')):
                status_text = "録音中"
            elif res.get('timefree_downloaded'):
                status_text = "DL済"
            elif res.get('last_result') == 'success':
                status_text = "成功"
            elif res.get('last_result') == 'partial':
                status_text = "中断（一部のみ）"
            elif res.get('last_result') == 'failed':
                status_text = "失敗"
            elif res.get('repeat') != 'weekly' and self._reservation_is_overdue_pending(res):
                # 放送時刻を過ぎたのに一度も実行されていない（その時刻にアプリが動いて
                # いなかった等）。毎週の予約は次回を待っている状態でもあるので対象外
                status_text = "未実行"
            else:
                status_text = "待機中"

            if res.get('source') == 'freeword':
                keyword = self.manager.get_freeword(res.get('keyword_id'))
                source_text = f"F: {keyword['keyword']}" if keyword else "フリーワード"
            else:
                source_text = "手動"

            download_text = "⬇ ダウンロード" if self._reservation_needs_timefree_recovery(res) else ""

            item_id = tree.insert(
                "", tk.END,
                values=(
                    "○" if res.get('enabled', True) else "×",
                    res.get('station', ''),
                    schedule_text,
                    res.get('title', ''),
                    source_text,
                    status_text,
                    download_text
                )
            )
            self._reservation_row_map[item_id] = res.get('id')
        self._restore_treeview_view(tree, self._reservation_row_map, view)

        # 予約の追加・変更・削除を、表示中の番組表の予約枠（囲み色）にも反映する
        if hasattr(self, 'schedule_canvas') and self._current_programs:
            self.display_schedule(self._current_programs, mode=self.schedule_mode_var.get())

    def _on_reservation_tree_click(self, event):
        """予約一覧のクリック処理: 「タイムフリー」列のクリックのみ、対象予約の
        タイムフリー取得ダイアログを開く（それ以外は通常の行選択に任せる）
        """
        tree = self.reservation_tree
        if tree.identify_region(event.x, event.y) != "cell":
            return
        columns = tree["columns"]
        try:
            col_index = int(tree.identify_column(event.x).replace("#", "")) - 1
        except ValueError:
            return
        if not (0 <= col_index < len(columns)) or columns[col_index] != "download":
            return
        row_id = tree.identify_row(event.y)
        if not row_id:
            return
        reservation = self.manager.get_reservation(self._reservation_row_map.get(row_id))
        if not reservation or not self._reservation_needs_timefree_recovery(reservation):
            return
        self._download_reservation_via_timefree(reservation)

    def _download_reservation_via_timefree(self, reservation):
        """予約一覧の「タイムフリー」列から、指定予約の直近の回をタイムフリーで取得する

        取得に成功したら、この予約を「DL済」としてマークし、一覧のボタンを消す。
        """
        program = self._reservation_occurrence_program_dict(reservation)
        if program is None:
            messagebox.showinfo("タイムフリー", "この予約の日時情報を取得できませんでした。")
            return
        reservation_id = reservation['id']
        self._open_timefree_download_dialog(
            program, station=reservation.get('station'),
            on_success=lambda rid=reservation_id: self._mark_reservation_timefree_downloaded(rid)
        )

    def _mark_reservation_timefree_downloaded(self, reservation_id):
        """予約をタイムフリーで取得し終えたことを記録し、一覧表示を更新する"""
        self.manager.update_reservation(reservation_id, {'timefree_downloaded': True})
        self._refresh_reservation_list()

    def _get_selected_reservations(self):
        """予約一覧で選択中の行に対応する予約データを全て取得する（未選択なら空リスト）"""
        selection = self.reservation_tree.selection()
        if not selection:
            messagebox.showinfo("予約録音", "予約を選択してください。")
            return []
        reservations = []
        for item_id in selection:
            reservation = self.manager.get_reservation(self._reservation_row_map.get(item_id))
            if reservation:
                reservations.append(reservation)
        return reservations

    def _edit_selected_reservation(self):
        reservations = self._get_selected_reservations()
        if not reservations:
            return
        if len(reservations) > 1:
            messagebox.showinfo("予約録音", "編集は1件ずつ選択して行ってください。")
            return
        self._open_reservation_dialog(existing=reservations[0])

    def _delete_selected_reservation(self):
        reservations = self._get_selected_reservations()
        if not reservations:
            return
        if len(reservations) == 1:
            message = f"「{reservations[0].get('title') or reservations[0].get('station')}」の予約を削除しますか？"
        else:
            message = f"選択した{len(reservations)}件の予約を削除しますか？"
        if not messagebox.askyesno("予約の削除", message):
            return
        for reservation in reservations:
            self.manager.delete_reservation(reservation['id'])
        self._refresh_reservation_list()

    def _set_selected_reservations_enabled(self, enabled):
        """選択中の予約すべてを有効/無効に一括変更する"""
        reservations = self._get_selected_reservations()
        if not reservations:
            return
        for reservation in reservations:
            self.manager.update_reservation(reservation['id'], {'enabled': enabled})
        self._refresh_reservation_list()

    def _delete_finished_reservations(self):
        """「終了した予約を削除」ボタン: 放送が終わった単発予約をまとめて削除する

        毎週予約は繰り返し使われ続けるため対象外。まだ一度も実行されていない
        単発予約や、現在録音中のものも「終了」ではないため対象外とする。

        タイムフリーで取得済み（一覧で「DL済」表示）の予約は、録音自体は
        失敗していて last_result が 'failed'/'partial' のままでも実質的には
        解決済みのため、「失敗」側には含めず、確認なしで通常の削除対象に含める。
        録音が一度も実行されないまま（last_run_date が未設定のまま）タイムフリーで
        直接取得したケースもあるため、その場合も timefree_downloaded だけで「終了」と
        みなす（last_run_date の有無は問わない）。
        """
        finished = [
            r for r in self.manager.load_reservations()
            if r.get('repeat') != 'weekly'
            and (r.get('last_run_date') or r.get('timefree_downloaded'))
            and not self.manager.is_recording_active(r.get('station'))
        ]
        if not finished:
            messagebox.showinfo("予約の削除", "削除対象の終了した予約はありません。")
            return

        failed = [
            r for r in finished
            if r.get('last_result') == 'failed' and not r.get('timefree_downloaded')
        ]
        include_failed = self._confirm_delete_finished_reservations(len(finished), len(failed))
        if include_failed is None:
            return

        targets = finished if include_failed else [r for r in finished if r not in failed]
        if not targets:
            return
        for reservation in targets:
            self.manager.delete_reservation(reservation['id'])
        self._refresh_reservation_list()
        messagebox.showinfo("予約の削除", f"終了した予約を{len(targets)}件削除しました。")

    def _confirm_delete_finished_reservations(self, finished_count, failed_count):
        """「終了した予約を削除」の確認ダイアログ。失敗した予約も含めて削除するかを
        チェックボックスで選べる（応答するまで処理をブロックする）。

        Returns:
            bool または None: 削除を実行するなら True/False（失敗分を含めるか）、
            キャンセルされた場合は None
        """
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("終了した予約の削除")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame, text=f"終了した予約が{finished_count}件あります。まとめて削除しますか？"
        ).pack(anchor=tk.W)

        include_failed_var = tk.BooleanVar(value=False)
        if failed_count:
            ttk.Checkbutton(
                frame, text=f"失敗した予約（{failed_count}件）も削除する",
                variable=include_failed_var
            ).pack(anchor=tk.W, pady=(8, 0))

        result = {'value': None}

        def on_delete():
            result['value'] = include_failed_var.get()
            dialog.destroy()

        def on_cancel():
            result['value'] = None
            dialog.destroy()

        button_frame = ttk.Frame(frame)
        button_frame.pack(pady=(15, 0))
        ttk.Button(button_frame, text="削除", command=on_delete).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="キャンセル", command=on_cancel).pack(side=tk.LEFT, padx=5)

        dialog.protocol("WM_DELETE_WINDOW", on_cancel)
        self._center_dialog_over_parent(dialog)
        dialog.deiconify()
        dialog.wait_window()
        return result['value']

    def _open_reservation_dialog(self, existing=None, prefill=None):
        """予約録音の新規追加・編集ダイアログを開く

        Args:
            existing (dict): 編集対象の予約データ（保存済みIDを含む）。編集時のみ指定
            prefill (dict): 新規予約の初期値（番組表からのダブルクリック等）。existingが
                優先されるため、新規追加時のみ指定する
        """
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("予約の編集" if existing else "新規予約録音")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)
        # 半角コロンだと日本語フォントとの幅計算がずれてラベル末尾が欠けて見えることがあるため
        # 全角コロンを使い、ラベル列にも十分な最小幅を確保しておく
        frame.columnconfigure(0, minsize=110)
        frame.columnconfigure(1, weight=1)

        data = existing or prefill or {}
        row = 0

        ttk.Label(frame, text="ステーション：").grid(row=row, column=0, sticky=tk.W, pady=4)
        station_var = tk.StringVar(value=data.get('station', self.schedule_station_var.get()))
        ttk.Combobox(
            frame, textvariable=station_var, values=self.manager.get_stations(),
            state="readonly", width=20
        ).grid(row=row, column=1, sticky=tk.EW, pady=4)
        row += 1

        ttk.Label(frame, text="繰り返し：").grid(row=row, column=0, sticky=tk.W, pady=4)
        repeat_var = tk.StringVar(value=data.get('repeat', 'once'))
        repeat_frame = ttk.Frame(frame)
        ttk.Radiobutton(
            repeat_frame, text="1回のみ", variable=repeat_var, value="once",
            command=lambda: update_repeat_fields()
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            repeat_frame, text="毎週", variable=repeat_var, value="weekly",
            command=lambda: update_repeat_fields()
        ).pack(side=tk.LEFT, padx=(10, 0))
        repeat_frame.grid(row=row, column=1, sticky=tk.W, pady=4)
        row += 1

        date_label = ttk.Label(frame, text="日付：")
        date_var = tk.StringVar(value=data.get('date_iso', datetime.now().strftime("%Y-%m-%d")))
        date_picker_frame = ttk.Frame(frame)
        date_entry = ttk.Entry(date_picker_frame, textvariable=date_var, width=14, state="readonly")
        date_entry.pack(side=tk.LEFT)
        date_picker_button = ttk.Button(
            date_picker_frame, text="📅",
            command=lambda: self._open_calendar_picker(date_picker_button, date_var)
        )
        date_picker_button.pack(side=tk.LEFT, padx=(4, 0))
        date_row = row
        row += 1

        weekday_label = ttk.Label(frame, text="曜日：")
        weekday_var = tk.StringVar(
            value=self.manager.WEEKDAY_JA[data.get('weekday', datetime.now().weekday())]
        )
        weekday_combo = ttk.Combobox(
            frame, textvariable=weekday_var, values=list(self.manager.WEEKDAY_JA),
            state="readonly", width=20
        )
        weekday_row = row
        row += 1

        def update_repeat_fields():
            if repeat_var.get() == 'weekly':
                date_label.grid_remove()
                date_picker_frame.grid_remove()
                weekday_label.grid(row=weekday_row, column=0, sticky=tk.W, pady=4)
                weekday_combo.grid(row=weekday_row, column=1, sticky=tk.EW, pady=4)
            else:
                weekday_label.grid_remove()
                weekday_combo.grid_remove()
                date_label.grid(row=date_row, column=0, sticky=tk.W, pady=4)
                date_picker_frame.grid(row=date_row, column=1, sticky=tk.W, pady=4)

        update_repeat_fields()

        ttk.Label(frame, text="開始時刻：").grid(row=row, column=0, sticky=tk.W, pady=4)
        start_frame, start_hour_var, start_minute_var = self._create_time_selectors(
            frame, data.get('start', '00:00')
        )
        start_frame.grid(row=row, column=1, sticky=tk.W, pady=4)
        row += 1

        ttk.Label(frame, text="終了時刻：").grid(row=row, column=0, sticky=tk.W, pady=4)
        end_frame, end_hour_var, end_minute_var = self._create_time_selectors(
            frame, data.get('end', '01:00')
        )
        end_frame.grid(row=row, column=1, sticky=tk.W, pady=4)
        row += 1

        ttk.Label(frame, text="番組名（任意）：").grid(row=row, column=0, sticky=tk.W, pady=4)
        title_var = tk.StringVar(value=data.get('title', ''))
        ttk.Entry(frame, textvariable=title_var, width=22).grid(row=row, column=1, sticky=tk.EW, pady=4)
        row += 1

        def on_save():
            if not station_var.get():
                messagebox.showerror("エラー", "ステーションを選択してください", parent=dialog)
                return

            start = self._get_time_value(start_hour_var, start_minute_var)
            end = self._get_time_value(end_hour_var, end_minute_var)
            if start == end:
                messagebox.showerror("エラー", "開始時刻と終了時刻が同じです", parent=dialog)
                return

            new_data = {
                'station': station_var.get(),
                'repeat': repeat_var.get(),
                'start': start,
                'end': end,
                'title': title_var.get().strip(),
            }
            if existing and existing.get('source') == 'freeword':
                # フリーワード由来の予約をダイアログで編集したら、以後は手動管理の
                # 予約として扱う。フリーワード由来のままだと、番組表の更新のたびに
                # 番組名・終了時刻が番組表の値に戻されたり、時刻を変えた枠に一致する
                # 番組が無いとして削除されたりする（scan_freewords_for_station）
                new_data['source'] = None
                new_data['keyword_id'] = None
            if repeat_var.get() == 'weekly':
                new_data['weekday'] = list(self.manager.WEEKDAY_JA).index(weekday_var.get())
            else:
                date_text = date_var.get().strip()
                try:
                    datetime.strptime(date_text, "%Y-%m-%d")
                except ValueError:
                    messagebox.showerror("エラー", "日付を選択してください", parent=dialog)
                    return
                new_data['date_iso'] = date_text

            if existing:
                self.manager.update_reservation(existing['id'], new_data)
            else:
                self.manager.add_reservation(new_data)
            self._refresh_reservation_list()
            dialog.destroy()

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=row, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(button_frame, text="保存", command=on_save).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="キャンセル", command=dialog.destroy).pack(side=tk.LEFT, padx=5)

        self._center_dialog_over_parent(dialog)
        dialog.deiconify()
        dialog.lift()
        dialog.focus_force()
