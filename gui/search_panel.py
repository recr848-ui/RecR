"""番組表タブの番組検索パネル"""

import tkinter as tk
from tkinter import ttk
from datetime import datetime

from utils.radiko_manager import keyword_matches


class SearchPanelMixin:
    def toggle_search_panel(self):
        """検索パネルの表示/非表示を切り替える"""
        self._search_panel_visible = not self._search_panel_visible
        if self._search_panel_visible:
            self.search_panel.pack(fill=tk.X, before=self._grid_container)
            self.search_toggle_button.config(text="検索 ▲")
        else:
            self.search_panel.pack_forget()
            self.search_toggle_button.config(text="検索 ▼")

    def setup_search_panel(self, parent):
        """検索パネル（キーワード・対象局チェックボックス・検索結果一覧）をセットアップ"""
        keyword_frame = ttk.Frame(parent)
        keyword_frame.pack(fill=tk.X)

        ttk.Label(keyword_frame, text="キーワード:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(keyword_frame, textvariable=self.search_var, width=30)
        search_entry.pack(side=tk.LEFT, padx=5)
        search_entry.bind("<Return>", lambda e: self.perform_search())

        ttk.Button(
            keyword_frame, text="検索", command=self.perform_search
        ).pack(side=tk.LEFT, padx=5)

        # 対象局チェックボックス
        stations_frame = ttk.LabelFrame(parent, text="対象局", padding=5)
        stations_frame.pack(fill=tk.X, pady=(8, 0))

        button_row = ttk.Frame(stations_frame)
        button_row.pack(fill=tk.X, anchor=tk.W)
        ttk.Button(
            button_row, text="全選択", command=lambda: self._set_all_search_stations(True)
        ).pack(side=tk.LEFT)
        ttk.Button(
            button_row, text="全解除", command=lambda: self._set_all_search_stations(False)
        ).pack(side=tk.LEFT, padx=5)

        checkbox_grid = ttk.Frame(stations_frame)
        checkbox_grid.pack(fill=tk.X, pady=(5, 0))

        self.search_station_vars = {}
        stations = self.manager.get_stations()
        columns = 5
        for i, station in enumerate(stations):
            var = tk.BooleanVar(value=True)
            self.search_station_vars[station] = var
            ttk.Checkbutton(
                checkbox_grid, text=station, variable=var
            ).grid(row=i // columns, column=i % columns, sticky=tk.W, padx=5, pady=2)

        # 検索結果一覧
        results_frame = ttk.LabelFrame(parent, text="検索結果", padding=5)
        results_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        columns = ("station", "date", "time", "title", "pfm")
        self.search_results_tree = ttk.Treeview(
            results_frame, columns=columns, show="headings", height=6
        )
        self.search_results_tree.heading("station", text="局")
        self.search_results_tree.heading("date", text="日付")
        self.search_results_tree.heading("time", text="時刻")
        self.search_results_tree.heading("title", text="番組名")
        self.search_results_tree.heading("pfm", text="出演者")
        self.search_results_tree.column("station", width=90, anchor=tk.W)
        self.search_results_tree.column("date", width=80, anchor=tk.W)
        self.search_results_tree.column("time", width=110, anchor=tk.W)
        self.search_results_tree.column("title", width=300, anchor=tk.W)
        self.search_results_tree.column("pfm", width=200, anchor=tk.W)

        results_scroll = ttk.Scrollbar(
            results_frame, orient=tk.VERTICAL, command=self.search_results_tree.yview
        )
        self.search_results_tree.configure(yscrollcommand=results_scroll.set)
        self.search_results_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        results_scroll.pack(side=tk.LEFT, fill=tk.Y)

        self.search_results_tree.bind("<<TreeviewSelect>>", self._on_search_result_select)
        self._search_result_map = {}

    def _set_all_search_stations(self, checked):
        for var in self.search_station_vars.values():
            var.set(checked)

    def perform_search(self):
        """キーワード・対象局チェックボックスに従って番組を検索し、結果一覧を更新する"""
        keyword = self.search_var.get().strip().lower()
        self.search_results_tree.delete(*self.search_results_tree.get_children())
        self._search_result_map = {}

        if not keyword:
            return

        today_iso = datetime.now().strftime("%Y-%m-%d")
        selected_stations = [
            station for station, var in self.search_station_vars.items() if var.get()
        ]

        for station in selected_stations:
            cached = self.manager.load_cached_schedule(station) or []
            for program in cached:
                if program.get('date_iso') and program['date_iso'] < today_iso:
                    continue
                haystack = " ".join([
                    program.get('title') or '',
                    program.get('desc') or '',
                    program.get('pfm') or ''
                ]).lower()
                if not keyword_matches(keyword, haystack):
                    continue

                item_id = self.search_results_tree.insert(
                    "", tk.END,
                    values=(
                        station,
                        program.get('date', ''),
                        f"{program['start']}-{program['end']}",
                        program.get('title', ''),
                        program.get('pfm', '')
                    )
                )
                self._search_result_map[item_id] = (station, program)

    def _on_search_result_select(self, event=None):
        selection = self.search_results_tree.selection()
        if not selection:
            return
        station, program = self._search_result_map[selection[0]]
        self._jump_to_program(station, program)
