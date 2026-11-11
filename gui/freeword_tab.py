"""「フリーワード」タブ：キーワード一覧と登録ダイアログ"""

import tkinter as tk
from tkinter import ttk, messagebox


class FreewordTabMixin:
    def setup_freeword_tab(self, parent):
        """「フリーワード」タブのUIをセットアップ（キーワードの一覧・追加・編集・削除）

        登録したキーワードは、番組表の取得・更新のたびに対象局の番組と自動的に
        照合され、一致した未来の番組が「予約録音」タブへ1回のみの予約として
        自動登録される。
        """
        control_frame = ttk.Frame(parent, padding=10)
        control_frame.pack(fill=tk.X)

        ttk.Button(
            control_frame, text="新規キーワード...", command=self._open_freeword_dialog
        ).pack(side=tk.LEFT)
        ttk.Button(
            control_frame, text="編集...", command=self._edit_selected_freeword
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="有効にする",
            command=lambda: self._set_selected_freewords_enabled(True)
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="無効にする",
            command=lambda: self._set_selected_freewords_enabled(False)
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="削除", command=self._delete_selected_freeword
        ).pack(side=tk.LEFT, padx=5)

        ttk.Label(
            parent,
            text="登録したキーワードは、番組表の取得・更新のたびに対象局と照合され、"
                 "一致した未来の番組が「予約録音」タブへ自動登録されます。",
            foreground="gray30", padding=(10, 0)
        ).pack(fill=tk.X)

        list_frame = ttk.Frame(parent, padding=10)
        list_frame.pack(fill=tk.BOTH, expand=True)

        tree = self._create_list_treeview(list_frame, [
            ("enabled", "有効", 50),
            ("keyword", "キーワード", 200),
            ("stations", "対象局", 500),
        ], self._edit_selected_freeword)

        self.freeword_tree = tree
        self._freeword_row_map = {}
        self._refresh_freeword_list()

    def _refresh_freeword_list(self):
        """フリーワードの一覧表示をマネージャー側の最新データで作り直す"""
        tree = self.freeword_tree
        view = self._capture_treeview_view(tree, self._freeword_row_map)
        tree.delete(*tree.get_children())
        self._freeword_row_map = {}

        for fw in self.manager.load_freewords():
            item_id = tree.insert(
                "", tk.END,
                values=(
                    "○" if fw.get('enabled', True) else "×",
                    fw.get('keyword', ''),
                    "、".join(fw.get('stations', [])),
                )
            )
            self._freeword_row_map[item_id] = fw.get('id')
        self._restore_treeview_view(tree, self._freeword_row_map, view)

    def _get_selected_freewords(self):
        """フリーワード一覧で選択中の行に対応するデータを全て取得する（未選択なら空リスト）"""
        selection = self.freeword_tree.selection()
        if not selection:
            messagebox.showinfo("フリーワード", "キーワードを選択してください。")
            return []
        freewords = []
        for item_id in selection:
            freeword = self.manager.get_freeword(self._freeword_row_map.get(item_id))
            if freeword:
                freewords.append(freeword)
        return freewords

    def _edit_selected_freeword(self):
        freewords = self._get_selected_freewords()
        if not freewords:
            return
        if len(freewords) > 1:
            messagebox.showinfo("フリーワード", "編集は1件ずつ選択して行ってください。")
            return
        self._open_freeword_dialog(existing=freewords[0])

    def _delete_selected_freeword(self):
        freewords = self._get_selected_freewords()
        if not freewords:
            return
        if len(freewords) == 1:
            message = f"キーワード「{freewords[0].get('keyword')}」を削除しますか？\n" \
                      "（既に自動作成された予約は削除されません）"
        else:
            message = f"選択した{len(freewords)}件のキーワードを削除しますか？\n" \
                      "（既に自動作成された予約は削除されません）"
        if not messagebox.askyesno("キーワードの削除", message):
            return
        for freeword in freewords:
            self.manager.delete_freeword(freeword['id'])
        self._refresh_freeword_list()

    def _set_selected_freewords_enabled(self, enabled):
        """選択中のキーワードすべてを有効/無効に一括変更する"""
        freewords = self._get_selected_freewords()
        if not freewords:
            return
        for freeword in freewords:
            self.manager.update_freeword(freeword['id'], {'enabled': enabled})
        self._refresh_freeword_list()
        if enabled:
            self._rescan_freewords_against_cache(freewords)

    def _rescan_freewords_against_cache(self, freewords):
        """指定したフリーワードの対象局について、キャッシュ済み番組表と即座に
        照合し直す（次回の番組表取得を待たずに反映するため）。一致した番組があれば
        予約録音タブへ自動登録し、一覧を更新する。
        """
        stations = {s for fw in freewords for s in fw.get('stations', [])}
        created_total = []
        for station in stations:
            cached = self.manager.load_cached_schedule(station)
            if cached:
                created_total.extend(self.manager.scan_freewords_for_station(station, cached))
        if created_total:
            self._refresh_reservation_list()

    def _open_freeword_dialog(self, existing=None):
        """フリーワードの新規追加・編集ダイアログを開く"""
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("キーワードの編集" if existing else "新規フリーワード")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, minsize=110)
        frame.columnconfigure(1, weight=1)

        data = existing or {}
        row = 0

        ttk.Label(frame, text="キーワード：").grid(row=row, column=0, sticky=tk.W, pady=4)
        keyword_var = tk.StringVar(value=data.get('keyword', ''))
        ttk.Entry(frame, textvariable=keyword_var, width=30).grid(
            row=row, column=1, sticky=tk.EW, pady=4
        )
        row += 1

        ttk.Label(
            frame, text="番組名・概要・出演者のいずれかに部分一致した番組を自動予約します。",
            foreground="gray30", wraplength=280
        ).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
        row += 1

        stations_frame = ttk.LabelFrame(frame, text="対象局", padding=5)
        stations_frame.grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=4)
        row += 1

        button_row = ttk.Frame(stations_frame)
        button_row.pack(fill=tk.X, anchor=tk.W)
        station_vars = {}
        ttk.Button(
            button_row, text="全選択",
            command=lambda: [v.set(True) for v in station_vars.values()]
        ).pack(side=tk.LEFT)
        ttk.Button(
            button_row, text="全解除",
            command=lambda: [v.set(False) for v in station_vars.values()]
        ).pack(side=tk.LEFT, padx=5)

        checkbox_grid = ttk.Frame(stations_frame)
        checkbox_grid.pack(fill=tk.X, pady=(5, 0))

        selected_stations = set(data.get('stations', []))
        columns = 5
        for i, station in enumerate(self.manager.get_stations()):
            var = tk.BooleanVar(value=(station in selected_stations) if existing else True)
            station_vars[station] = var
            ttk.Checkbutton(
                checkbox_grid, text=station, variable=var
            ).grid(row=i // columns, column=i % columns, sticky=tk.W, padx=5, pady=2)

        preview_frame = ttk.LabelFrame(frame, text="対象番組の確認（保存前プレビュー）", padding=5)
        preview_frame.grid(row=row, column=0, columnspan=2, sticky=tk.NSEW, pady=4)
        frame.rowconfigure(row, weight=1)
        row += 1

        preview_top = ttk.Frame(preview_frame)
        preview_top.pack(fill=tk.X)
        ttk.Button(
            preview_top, text="対象番組を確認", command=lambda: run_preview()
        ).pack(side=tk.LEFT)
        preview_status_var = tk.StringVar(value="キーワードと対象局を指定して「対象番組を確認」を押してください")
        ttk.Label(preview_top, textvariable=preview_status_var, foreground="gray30").pack(
            side=tk.LEFT, padx=(10, 0)
        )

        preview_columns = ("station", "date", "time", "title", "pfm")
        preview_tree = ttk.Treeview(
            preview_frame, columns=preview_columns, show="headings", height=6
        )
        preview_tree.heading("station", text="局")
        preview_tree.heading("date", text="日付")
        preview_tree.heading("time", text="時刻")
        preview_tree.heading("title", text="番組名")
        preview_tree.heading("pfm", text="出演者")
        preview_tree.column("station", width=80, anchor=tk.W)
        preview_tree.column("date", width=80, anchor=tk.W)
        preview_tree.column("time", width=100, anchor=tk.W)
        preview_tree.column("title", width=260, anchor=tk.W)
        preview_tree.column("pfm", width=160, anchor=tk.W)
        preview_scroll = ttk.Scrollbar(
            preview_frame, orient=tk.VERTICAL, command=preview_tree.yview
        )
        preview_tree.configure(yscrollcommand=preview_scroll.set)
        preview_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=(5, 0))
        preview_scroll.pack(side=tk.LEFT, fill=tk.Y, pady=(5, 0))

        def run_preview():
            preview_tree.delete(*preview_tree.get_children())
            keyword = keyword_var.get().strip()
            stations = [s for s, v in station_vars.items() if v.get()]
            if not keyword or not stations:
                preview_status_var.set("キーワードと対象局を指定して「対象番組を確認」を押してください")
                return

            matches = self.manager.search_cached_programs_by_keyword(keyword, stations)
            for station, program in matches:
                preview_tree.insert(
                    "", tk.END,
                    values=(
                        station,
                        program.get('date', ''),
                        f"{program.get('start', '')}-{program.get('end', '')}",
                        program.get('title', ''),
                        program.get('pfm', '')
                    )
                )
            preview_status_var.set(
                f"{len(matches)}件ヒット（今後放送予定のもののみ。番組表未取得の局は対象外）"
            )

        def on_save():
            keyword = keyword_var.get().strip()
            if not keyword:
                messagebox.showerror("エラー", "キーワードを入力してください", parent=dialog)
                return
            stations = [s for s, v in station_vars.items() if v.get()]
            if not stations:
                messagebox.showerror("エラー", "対象局を1つ以上選択してください", parent=dialog)
                return

            new_data = {'keyword': keyword, 'stations': stations}
            if existing:
                self.manager.update_freeword(existing['id'], new_data)
                freeword = self.manager.get_freeword(existing['id'])
            else:
                freeword = self.manager.add_freeword(new_data)
            self._refresh_freeword_list()
            dialog.destroy()
            if freeword.get('enabled', True):
                self._rescan_freewords_against_cache([freeword])

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=row, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(button_frame, text="保存", command=on_save).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="キャンセル", command=dialog.destroy).pack(side=tk.LEFT, padx=5)

        self._center_dialog_over_parent(dialog)
        dialog.deiconify()
