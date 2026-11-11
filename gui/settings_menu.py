"""メニューバーと各種設定（テーマ・保存先・番組表取得など）の変更"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pathlib import Path
import json
import os
import webbrowser

import sv_ttk


class SettingsMenuMixin:
    def _on_filename_pattern_changed(self):
        """録音ファイル名の形式切り替え時: 設定を保存する"""
        self.manager.save_settings({'recording_filename_pattern': self.filename_pattern_var.get()})

    def setup_menu(self):
        """ウィンドウ上部のメニューバーをセットアップ"""
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="終了", command=self.quit_app)
        menubar.add_cascade(label="ファイル", menu=file_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_radiobutton(
            label="ライトテーマ", variable=self.theme_var,
            value="light", command=self._on_theme_changed
        )
        view_menu.add_radiobutton(
            label="ダークテーマ", variable=self.theme_var,
            value="dark", command=self._on_theme_changed
        )
        view_menu.add_separator()

        eq_mode_menu = tk.Menu(view_menu, tearoff=0)
        eq_mode_menu.add_radiobutton(
            label="スペクトラム（マルチバンド）", variable=self.eq_mode_var,
            value="spectrum", command=self._on_eq_mode_changed
        )
        eq_mode_menu.add_radiobutton(
            label="ピークメーター", variable=self.eq_mode_var,
            value="peak", command=self._on_eq_mode_changed
        )
        view_menu.add_cascade(label="イコライザー表示", menu=eq_mode_menu)
        menubar.add_cascade(label="表示", menu=view_menu)

        schedule_menu = tk.Menu(menubar, tearoff=0)
        schedule_menu.add_command(label="番組表を一括更新", command=self._bulk_refresh_schedules)
        menubar.add_cascade(label="番組表", menu=schedule_menu)

        settings_menu = tk.Menu(menubar, tearoff=0)

        quality_menu = tk.Menu(settings_menu, tearoff=0)
        quality_menu.add_radiobutton(
            label="AAC（再エンコードなし）", variable=self.format_var,
            value="aac", command=self._on_format_changed
        )
        quality_menu.add_radiobutton(
            label="M4A（再エンコードなし・タグ対応）", variable=self.format_var,
            value="m4a", command=self._on_format_changed
        )
        quality_menu.add_radiobutton(
            label="MP3", variable=self.format_var,
            value="mp3", command=self._on_format_changed
        )
        quality_menu.add_separator()
        for bitrate in self.manager.MP3_BITRATE_CHOICES:
            quality_menu.add_radiobutton(
                label=f"MP3ビットレート: {bitrate}kbps",
                variable=self.bitrate_var, value=str(bitrate),
                command=self._on_bitrate_changed
            )
        settings_menu.add_cascade(label="録音品質", menu=quality_menu)

        filename_menu = tk.Menu(settings_menu, tearoff=0)
        filename_pattern_labels = {
            'station_datetime': "局名_日付_時刻（従来の形式） 例: NHK-FM_20260903_143000",
            'datetime_title': "日付_時刻_番組名 例: 20260903_1430_ミュージックライン",
            'date_title': "日付_番組名 例: 20260903_ミュージックライン",
            'title_datetime': "番組名_日付_時刻 例: ミュージックライン_20260903_1430",
            'title_date': "番組名_日付 例: ミュージックライン_20260903",
        }
        for pattern_key, label in filename_pattern_labels.items():
            filename_menu.add_radiobutton(
                label=label, variable=self.filename_pattern_var,
                value=pattern_key, command=self._on_filename_pattern_changed
            )
        settings_menu.add_cascade(label="録音ファイル名の形式", menu=filename_menu)

        settings_menu.add_command(label="録音保存先...", command=self._change_output_dir)
        settings_menu.add_separator()

        default_station_menu = tk.Menu(settings_menu, tearoff=0)
        for station in self.manager.get_stations():
            default_station_menu.add_radiobutton(
                label=station, variable=self.default_station_var,
                value=station, command=self._on_default_station_changed
            )
        settings_menu.add_cascade(label="起動時のデフォルト局", menu=default_station_menu)

        settings_menu.add_command(
            label="番組表の取得日数...", command=self._change_schedule_fetch_days
        )
        settings_menu.add_command(
            label="番組表の自動更新しきい値...", command=self._change_schedule_stale_threshold
        )
        settings_menu.add_command(
            label="全局自動更新の時刻...", command=self._change_full_schedule_refresh_time
        )
        settings_menu.add_command(
            label="タイムフリー同時ダウンロード数...",
            command=self._change_max_concurrent_timefree_downloads
        )
        settings_menu.add_command(
            label="CMフィンガープリント取得...", command=self._open_cm_fp_dialog
        )

        margin_menu = tk.Menu(settings_menu, tearoff=0)
        margin_labels = {0: "なし", 15: "15秒", 30: "30秒", 45: "45秒", 60: "60秒"}
        for seconds, label in margin_labels.items():
            margin_menu.add_radiobutton(
                label=label, variable=self.recording_margin_var,
                value=seconds, command=self._on_recording_margin_changed
            )
        settings_menu.add_cascade(label="予約録音の前後マージン", menu=margin_menu)

        settings_menu.add_separator()
        settings_menu.add_checkbutton(
            label="予約待機中・録音中は自動スリープを抑止する",
            variable=self.prevent_sleep_var, command=self._on_prevent_sleep_changed
        )
        settings_menu.add_checkbutton(
            label="取り逃した予約をタイムフリー・聴き逃しで自動取得する",
            variable=self.auto_recover_missed_var, command=self._on_auto_recover_missed_changed
        )

        settings_menu.add_separator()
        settings_menu.add_command(label="設定をエクスポート...", command=self._export_settings)
        settings_menu.add_command(label="設定をインポート...", command=self._import_settings)

        menubar.add_cascade(label="設定", menu=settings_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="ヘルプ", command=lambda: webbrowser.open(self.HELP_URL))
        help_menu.add_command(label="バージョン情報", command=self._show_version)
        menubar.add_cascade(label="ヘルプ", menu=help_menu)

        self.root.config(menu=menubar)

        # tk.Menuはttkテーマの管理外のため、テーマ切り替え時に自前で配色し直せるよう
        # 全メニューを保持しておく（_apply_menu_theme参照）
        self._menus = [
            menubar, file_menu, view_menu, eq_mode_menu, schedule_menu,
            settings_menu, quality_menu, filename_menu, default_station_menu, margin_menu,
            help_menu,
        ]
        self._apply_menu_theme()

    def _show_version(self):
        """メニュー「ヘルプ」→「バージョン情報」: 版数を表示する"""
        messagebox.showinfo("バージョン情報", f"RecR Ver{self.APP_VERSION}")

    def _on_theme_changed(self):
        """テーマ（ライト/ダーク）切り替え時: sv_ttkのテーマを反映し、Canvas系の配色も更新する"""
        theme = self.theme_var.get()
        sv_ttk.set_theme(theme)
        self.manager.save_settings({'theme': theme})
        self._apply_canvas_theme()
        self._apply_menu_theme()
        self._apply_notice_theme()
        self.display_schedule(self._current_programs, mode=self.schedule_mode_var.get())

    def _apply_canvas_theme(self):
        """ttkテーマの管理外であるtk.Canvasの背景色を、現在のテーマに合わせて更新する"""
        colors = self._get_schedule_colors()
        self.schedule_canvas.configure(background=colors['bg'])
        self.day_header_frame.configure(background=colors['bg'])

    def _get_menu_colors(self):
        """現在のテーマ（ライト/ダーク）に応じたメニューの配色を返す

        tk.Menu（ネイティブメニュー）はttkテーマの管理外なので、テーマ切り替え時に
        自前で配色し直す必要がある。特に selectcolor はチェックボタン/ラジオボタン
        項目のチェックマーク・丸印そのものの色で、ここを明示しないとダークテーマ時に
        既定色のままでチェックが見えにくくなる（背景と同化する）ため必ず指定する。
        """
        if self.theme_var.get() == "dark":
            return {
                'bg': '#2b2b2b', 'fg': '#eaeaea',
                'activebackground': '#3d3d3d', 'activeforeground': '#ffffff',
                'selectcolor': '#78b3ff',
            }
        return {
            'bg': '#ffffff', 'fg': '#1e1e1e',
            'activebackground': '#e5e5e5', 'activeforeground': '#000000',
            'selectcolor': '#1a5fb4',
        }

    def _apply_menu_theme(self):
        """setup_top_barで作成した全てのtk.Menuに、現在のテーマの配色を反映し直す"""
        colors = self._get_menu_colors()
        for menu in getattr(self, '_menus', []):
            menu.configure(**colors)

    def _apply_schedule_font_size(self):
        """番組表の文字サイズ設定（小/中/大）を、フォントオブジェクトと
        グリッドの縦方向の拡大率（PIXELS_PER_MINUTE）に反映する。

        再描画は行わないので、呼び出し側で必要に応じてdisplay_scheduleを呼ぶこと。
        タイトル・概要・時刻目盛・日付ヘッダーの文字サイズは「小」を基準に同じ
        ポイント数だけ引き上げ、縦の拡大率はタイトルの文字サイズに比例させることで、
        文字を大きくしても番組枠からはみ出しにくくしている。
        """
        delta = self.SCHEDULE_FONT_SIZE_DELTA.get(self.schedule_font_size_var.get(), 0)
        self._schedule_font_size_key = delta
        title_size = self.SCHEDULE_BASE_TITLE_SIZE + delta
        self.schedule_title_font.configure(size=title_size)
        self.schedule_title_link_font.configure(size=title_size)
        self.schedule_desc_font.configure(size=self.SCHEDULE_BASE_DESC_SIZE + delta)
        self._schedule_hour_label_size = self.SCHEDULE_BASE_HOUR_LABEL_SIZE + delta
        self._schedule_date_header_size = self.SCHEDULE_BASE_DATE_HEADER_SIZE + delta
        self.PIXELS_PER_MINUTE = (
            self.SCHEDULE_BASE_PIXELS_PER_MINUTE * title_size / self.SCHEDULE_BASE_TITLE_SIZE
        )

    def _on_schedule_font_size_changed(self):
        """番組表の文字サイズ設定切り替え時: 設定を保存し、フォント・グリッドを再構築する"""
        self.manager.save_settings({'schedule_font_size': self.schedule_font_size_var.get()})
        self._apply_schedule_font_size()
        self.display_schedule(self._current_programs, mode=self.schedule_mode_var.get())

    def _get_schedule_colors(self):
        """現在のテーマ（ライト/ダーク）に応じた番組表グリッドの配色を返す"""
        if self.theme_var.get() == "dark":
            return {
                'bg': '#2b2b2b',
                'hour_line': '#3d3d3d',
                'day_line': '#4a4a4a',
                'time_label': '#a5a5a5',
                'future_fill': '#324a61',
                'future_outline': '#5b8fc2',
                'past_fill': '#3a3a3a',
                'past_outline': '#5a5a5a',
                # タイムフリーモードで、当日中のまだ放送されていない番組用
                # （斜線ハッチング風に見えるよう、はっきり別系統の色にする）
                'unavailable_fill': '#4a2f2f',
                'unavailable_outline': '#8a5a5a',
                'reserved_outline': '#ff8c3f',
                # NHK番組のタイムフリー表示時、NHK聴き逃しインデックスに
                # 登録済み（ダウンロードできる見込みが高い）かどうかの区別用
                'radiru_available_outline': '#4cc785',
                'radiru_unavailable_outline': '#7a7a7a',
                'title': '#eaeaea',
                'title_link': '#78b3ff',
                'desc': '#b5b5b5',
            }
        return {
            'bg': '#ffffff',
            'hour_line': '#e6e6e6',
            'day_line': '#cccccc',
            'time_label': 'gray30',
            'future_fill': '#eaf3fb',
            'future_outline': '#7fa8c9',
            'past_fill': '#d9d9d9',
            'past_outline': '#aaaaaa',
            'unavailable_fill': '#f7e6e6',
            'unavailable_outline': '#c98a8a',
            'reserved_outline': '#e2690a',
            'radiru_available_outline': '#2e9e5b',
            'radiru_unavailable_outline': '#999999',
            'title': '#222222',
            'title_link': '#1a5fb4',
            'desc': '#666666',
        }

    def _open_output_folder(self):
        """録音ファイルの保存先フォルダをエクスプローラーで開く"""
        output_dir = self.manager.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(output_dir))

    def _export_settings(self):
        """現在の設定・予約一覧・フリーワード一覧をユーザー指定のJSONファイルに書き出す"""
        dest = filedialog.asksaveasfilename(
            title="設定のエクスポート先を選択",
            defaultextension=".json",
            filetypes=[("JSONファイル", "*.json")],
            initialfile="RecR_settings.json",
        )
        if not dest:
            return
        data = {
            "settings": self.manager.load_settings(),
            "reservations": self.manager.load_reservations(),
            "freeword_keywords": self.manager.load_freewords(),
        }
        try:
            with open(dest, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            messagebox.showerror("設定のエクスポート", f"書き出しに失敗しました:\n{e}")
            return
        messagebox.showinfo(
            "設定のエクスポート",
            f"設定・予約一覧・フリーワード一覧を書き出しました:\n{dest}"
        )

    def _import_settings(self):
        """ユーザー指定のJSONファイルから設定・予約一覧・フリーワード一覧を読み込む

        エクスポート機能で書き出した新形式（settings/reservations/freeword_keywords
        をまとめたdict）と、設定のみのフラットな旧形式の両方を受け付ける。
        録音保存先など一部の設定は起動時にのみ反映されるため、インポート後は
        アプリの再起動を促す。
        """
        src = filedialog.askopenfilename(
            title="インポートする設定ファイルを選択",
            filetypes=[("JSONファイル", "*.json")],
        )
        if not src:
            return
        try:
            with open(src, "r", encoding="utf-8") as f:
                imported = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            messagebox.showerror("設定のインポート", f"読み込みに失敗しました:\n{e}")
            return
        if not isinstance(imported, dict):
            messagebox.showerror("設定のインポート", "設定ファイルの形式が不正です。")
            return

        is_bundled = any(
            key in imported for key in ("settings", "reservations", "freeword_keywords")
        )
        settings = imported.get("settings", {}) if is_bundled else imported
        reservations = imported.get("reservations") if is_bundled else None
        freewords = imported.get("freeword_keywords") if is_bundled else None

        if not messagebox.askyesno(
            "設定のインポート",
            "現在の設定・予約一覧・フリーワード一覧を選択したファイルの内容で上書きします。\n"
            "変更を完全に反映するにはアプリの再起動が必要です。続行しますか？"
        ):
            return
        self.manager.save_settings(settings)
        if reservations is not None:
            self.manager.replace_reservations(reservations)
        if freewords is not None:
            self.manager.replace_freewords(freewords)
        messagebox.showinfo(
            "設定のインポート",
            "インポートしました。\n変更を反映するにはRecRを再起動してください。"
        )

    def _change_output_dir(self):
        """録音ファイルの保存先フォルダを選び直す"""
        chosen = filedialog.askdirectory(
            initialdir=str(self.manager.output_dir),
            title="録音保存先フォルダを選択"
        )
        if not chosen:
            return
        self.manager.output_dir = Path(chosen)
        self.manager.output_dir.mkdir(parents=True, exist_ok=True)
        if hasattr(self, 'files_tree'):
            self._refresh_files_list()
        messagebox.showinfo("録音保存先", f"録音保存先を変更しました:\n{chosen}")

    def _on_default_station_changed(self):
        """起動時のデフォルト局が変更された時: 設定を保存する"""
        self.manager.save_settings({'default_station': self.default_station_var.get()})

    def _change_schedule_fetch_days(self):
        """番組表の取得日数を変更する"""
        days = self._ask_integer_ja(
            "番組表の取得日数",
            "番組表を何日分取得しますか？（1〜13）",
            initialvalue=self.SCHEDULE_FETCH_DAYS,
            minvalue=1, maxvalue=13
        )
        if days is None:
            return
        self.SCHEDULE_FETCH_DAYS = days
        self.manager.save_settings({'schedule_fetch_days': days})

    def _change_schedule_stale_threshold(self):
        """番組表の自動更新しきい値（残り日数）を変更する"""
        threshold = self._ask_integer_ja(
            "番組表の自動更新しきい値",
            "起動時、番組表の残り日数がこの日数以下の局を自動更新します。",
            initialvalue=self.SCHEDULE_STALE_THRESHOLD_DAYS,
            minvalue=0, maxvalue=13
        )
        if threshold is None:
            return
        self.SCHEDULE_STALE_THRESHOLD_DAYS = threshold
        self.manager.save_settings({'schedule_stale_threshold_days': threshold})

    def _change_full_schedule_refresh_time(self):
        """全局番組表の自動更新（1日1回、フリーワード予約のための取りこぼし防止）を
        行う時刻（時:分）を変更する
        """
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("全局自動更新の時刻")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame,
            text="毎日、全局の番組表を自動で取得し直す時刻を指定してください。\n"
                 "アプリが起動している間、この時刻を過ぎたタイミングで\n"
                 "その日1回だけバックグラウンドで自動実行されます。",
            justify=tk.LEFT
        ).pack(anchor=tk.W, pady=(0, 10))

        time_frame, hour_var, minute_var = self._create_time_selectors(
            frame, initial=self.FULL_SCHEDULE_REFRESH_TIME
        )
        time_frame.pack(anchor=tk.W)

        def on_save():
            self.FULL_SCHEDULE_REFRESH_TIME = self._get_time_value(hour_var, minute_var)
            self.manager.save_settings({'full_schedule_refresh_time': self.FULL_SCHEDULE_REFRESH_TIME})
            dialog.destroy()

        button_frame = ttk.Frame(frame)
        button_frame.pack(pady=(15, 0))
        ttk.Button(button_frame, text="保存", command=on_save).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="キャンセル", command=dialog.destroy).pack(side=tk.LEFT, padx=5)

        self._center_dialog_over_parent(dialog)
        dialog.deiconify()

    def _change_max_concurrent_timefree_downloads(self):
        """タイムフリーの同時ダウンロード数の上限を変更する（1〜10、既定3）"""
        value = self._ask_integer_ja(
            "タイムフリー同時ダウンロード数",
            "タイムフリーを同時にいくつまでダウンロードできるようにしますか？（1〜10）",
            initialvalue=self.manager.max_concurrent_timefree_downloads,
            minvalue=1, maxvalue=10
        )
        if value is None:
            return
        self.manager.max_concurrent_timefree_downloads = value
        self.manager.save_settings({'max_concurrent_timefree_downloads': value})

    def _on_recording_margin_changed(self):
        """予約録音の前後マージン変更時: 設定を保存する

        番組表の時刻と実際の配信のわずかなズレで、予約録音の冒頭・末尾が
        欠けてしまうのを防ぐため、開始前・終了後にこの秒数だけ余分に録音する。
        """
        seconds = self.recording_margin_var.get()
        self.manager.recording_margin_seconds = seconds
        self.manager.save_settings({'recording_margin_seconds': seconds})
