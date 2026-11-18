"""「録音」タブ：手動録音とライブ再生"""

import tkinter as tk
from tkinter import ttk, messagebox


class RecordTabMixin:
    def setup_record_tab(self, parent):
        """「録音」タブのUIをセットアップ

        左側にステーション選択・録音設定を、右側にお知らせ欄を配置する2カラム構成。
        左側はfill=tk.Yのみ（横方向には広がらない）にすることで、ウィンドウ幅
        （番組表グリッド表示のため1450px確保）いっぱいまでコンボボックス等が
        間延びしてしまうのを防ぐ。
        """
        content_frame = ttk.Frame(parent)
        content_frame.pack(fill=tk.BOTH, expand=True)

        left_frame = ttk.Frame(content_frame)
        left_frame.pack(side=tk.LEFT, fill=tk.Y)

        # タイトル
        title_label = ttk.Label(
            left_frame,
            text="RecR - ラジコ録音アプリ",
            font=("Yu Gothic UI", 16, "bold")
        )
        title_label.pack(pady=10)

        # 説明
        instructions = ttk.Label(
            left_frame,
            text="ステーションを選択して録音設定をしてください",
            font=("Yu Gothic UI", 10)
        )
        instructions.pack(pady=5)

        # ステーション選択フレーム
        station_frame = ttk.LabelFrame(left_frame, text="ステーション選択", padding=10)
        station_frame.pack(fill=tk.BOTH, padx=10, pady=10)

        ttk.Label(station_frame, text="ステーション：").grid(row=0, column=0, sticky=tk.W)
        station_combo = ttk.Combobox(
            station_frame,
            textvariable=self.recording_station_var,
            values=self.manager.get_stations(),
            state="readonly"
        )
        station_combo.grid(row=0, column=1, sticky=tk.EW, padx=5)
        station_frame.columnconfigure(1, weight=1)

        # 録音時間フレーム
        duration_frame = ttk.LabelFrame(left_frame, text="録音設定", padding=10)
        duration_frame.pack(fill=tk.BOTH, padx=10, pady=10)

        ttk.Label(duration_frame, text="録音時間 (分):").grid(row=0, column=0, sticky=tk.W)
        self.duration_var = tk.StringVar(value="60")
        duration_spin = ttk.Spinbox(
            duration_frame,
            from_=1,
            to=360,
            textvariable=self.duration_var
        )
        duration_spin.grid(row=0, column=1, sticky=tk.EW, padx=5)
        duration_frame.columnconfigure(1, weight=1)

        settings = self.manager.load_settings()

        ttk.Label(duration_frame, text="ファイル形式:").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        self.format_var = tk.StringVar(value=settings.get('recording_format', 'aac'))
        format_combo = ttk.Combobox(
            duration_frame,
            textvariable=self.format_var,
            values=["aac", "m4a", "mp3"],
            state="readonly",
            width=10
        )
        format_combo.grid(row=1, column=1, sticky=tk.W, padx=5, pady=(8, 0))
        format_combo.bind("<<ComboboxSelected>>", self._on_format_changed)

        self.bitrate_label = ttk.Label(duration_frame, text="MP3ビットレート:")
        self.bitrate_var = tk.StringVar(value=settings.get('mp3_bitrate', '192'))
        self.bitrate_combo = ttk.Combobox(
            duration_frame,
            textvariable=self.bitrate_var,
            values=["128", "192", "256", "320"],
            state="readonly",
            width=10
        )
        self.bitrate_combo.bind("<<ComboboxSelected>>", self._on_bitrate_changed)
        self._set_bitrate_widgets_visible(self.format_var.get() == "mp3")

        # ボタンフレーム
        button_frame = ttk.Frame(left_frame)
        button_frame.pack(pady=20)

        start_button = ttk.Button(
            button_frame,
            text="録音開始",
            command=self.start_recording
        )
        start_button.pack(side=tk.LEFT, padx=5)
        self.start_button = start_button

        stop_button = ttk.Button(
            button_frame,
            text="録音停止",
            command=self.stop_recording,
            state=tk.DISABLED
        )
        stop_button.pack(side=tk.LEFT, padx=5)
        self.stop_button = stop_button

        # 録音中インジケーター（点滅する「● REC」表示）
        self.rec_indicator_var = tk.StringVar(value="")
        self.rec_indicator_label = ttk.Label(
            left_frame, textvariable=self.rec_indicator_var,
            foreground="red", font=("Yu Gothic UI", 12, "bold")
        )
        self.rec_indicator_label.pack(pady=(0, 10))
        self._rec_blink_job = None
        self._rec_blink_visible = False
        self._recording_watch_job = None
        # 録音タブのStart/Stopボタンが対象とする、手動録音中の局名（局ごとに複数同時録音が
        # 可能になったため、このタブが今どの局を制御しているかを明示的に管理する）
        self._manual_recording_station = None

        # ステータスラベル
        self.status_var = tk.StringVar(value="準備完了")
        status_label = ttk.Label(left_frame, textvariable=self.status_var, justify=tk.CENTER)
        status_label.pack(pady=10)
        # 録音中は保存先のフルパスを含む長い文になるため、左カラムの幅で折り返す
        # （折り返さないとラベル幅に引っ張られて左カラムが広がり、お知らせ欄が狭くなる）
        left_frame.bind(
            "<Configure>",
            lambda e: status_label.configure(wraplength=max(e.width - 20, 100)),
            add="+"
        )

        self._setup_notice_panel(content_frame)

    def _on_format_changed(self, event=None):
        """ファイル形式コンボボックス変更時: MP3のときだけビットレート選択を表示し、設定を保存する"""
        self._set_bitrate_widgets_visible(self.format_var.get() == "mp3")
        self.manager.save_settings({'recording_format': self.format_var.get()})

    def _on_bitrate_changed(self, event=None):
        """MP3ビットレート変更時: 設定を保存する"""
        self.manager.save_settings({'mp3_bitrate': self.bitrate_var.get()})

    def _set_bitrate_widgets_visible(self, visible):
        if visible:
            self.bitrate_label.grid(row=2, column=0, sticky=tk.W, pady=(8, 0))
            self.bitrate_combo.grid(row=2, column=1, sticky=tk.W, padx=5, pady=(8, 0))
        else:
            self.bitrate_label.grid_remove()
            self.bitrate_combo.grid_remove()

    def _on_manual_recording_complete(self, station, had_data, output_path, error):
        """手動録音（録音タブ・番組表からの「今すぐ録音」）終了時（メインスレッドから呼ばれる）:
        データを1バイトも取得できなかった場合に警告する
        """
        if hasattr(self, 'files_tree'):
            self._refresh_files_list()
        if had_data:
            self._request_cm_chapters(output_path)
        if not had_data:
            detail = f"\n（{error}）" if error else ""
            message = (
                f"{station} の録音でデータを取得できませんでした（ファイルが空です）。\n"
                f"通信状況をご確認ください。{detail}"
            )
            if self._tray_icon is not None:
                self._notify_via_tray("録音", message)
            else:
                messagebox.showwarning("録音", message)
        elif error:
            message = (
                f"{station} の録音は途中で通信エラーが発生し、中断されました。\n"
                f"それまでの内容は保存されています。\n"
                f"保存先: {output_path}\n（{error}）"
            )
            if self._tray_icon is not None:
                self._notify_via_tray("録音", message)
            else:
                messagebox.showwarning("録音", message)
        elif self._tray_icon is not None:
            self._notify_via_tray("録音", f"{station} の録音が完了しました。")

    def _watch_background_recording(self, station):
        """局: station の録音（予約録音・「今すぐ録音」）が終了したら、
        右上の録音中パネルと予約一覧を更新する

        録音タブのStart/Stopボタンなど、単一の局しか保持できない手動録音用のUIは
        一切操作しない。局ごとに独立して監視するため、複数の録音が同時に進行していても
        互いを上書きしたり、片方の終了を見逃したりしない。録音が完了しても確認ダイアログは出さない。
        """
        if self.manager.is_recording_active(station):
            self.root.after(2000, lambda: self._watch_background_recording(station))
        else:
            self._unregister_active_recording(station)
            self._refresh_reservation_list()

    def start_recording(self):
        """録音を開始（局が異なれば、予約録音や他の手動録音と同時に進行できる）"""
        station = self.recording_station_var.get()
        try:
            duration = int(self.duration_var.get())
        except ValueError:
            messagebox.showerror("エラー", "無効な時間です")
            return
        self._begin_manual_recording(station, duration)

    def _begin_manual_recording(self, station, duration_minutes):
        """指定局の手動録音を実際に開始し、録音タブのUIを更新する（録音タブの「録音開始」ボタン専用）"""
        if self.manager.is_recording_active(station):
            messagebox.showerror("エラー", f"{station} は既に録音中です")
            return

        file_format = self.format_var.get()
        try:
            mp3_bitrate = int(self.bitrate_var.get())
        except ValueError:
            mp3_bitrate = 192

        program = self._find_now_airing_program(station)
        title = (program.get('title') if program else None) or None

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
            self._manual_recording_station = station
            self.status_var.set(f"{station} を {duration_minutes} 分間録音中... ({output_path})")
            self.start_button.config(state=tk.DISABLED)
            self.stop_button.config(state=tk.NORMAL)
            self._start_rec_blink()
            self._schedule_recording_watch()
            self._register_active_recording(station, station, duration_minutes)
        else:
            messagebox.showerror(
                "録音エラー",
                f"{station} の録音を開始できませんでした。\n通信状況をご確認ください。"
            )

    def stop_recording(self):
        """録音タブから開始した手動録音を停止（完了の確認ダイアログは出さない）"""
        station = self._manual_recording_station
        self.manager.stop_recording(station)
        self._unregister_active_recording(station)
        self._manual_recording_station = None
        self.status_var.set("準備完了")
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        if self._recording_watch_job:
            self.root.after_cancel(self._recording_watch_job)
            self._recording_watch_job = None
        self._stop_rec_blink()

    def start_playback(self):
        """選択中の局のライブ配信を再生"""
        station = self.playback_station_var.get()
        self._set_app_busy(True)
        try:
            success = self.manager.play_live(station)
        finally:
            self._set_app_busy(False)

        if success:
            self.status_var.set(f"{station} を再生中...")
            self.play_button.config(state=tk.DISABLED)
            self.play_stop_button.config(state=tk.NORMAL)
            self.top_bar_station_combo.config(state=tk.DISABLED)
            self._schedule_eq_update()
        else:
            messagebox.showerror(
                "再生エラー",
                f"{station} の再生を開始できませんでした。\n"
                "対応していない局か、通信状況をご確認ください。"
            )

    def stop_playback(self):
        """ライブ配信の再生を停止"""
        self.manager.stop_playback()
        self.status_var.set("準備完了")
        self.play_button.config(state=tk.NORMAL)
        self.play_stop_button.config(state=tk.DISABLED)
        self.top_bar_station_combo.config(state="readonly")
        if self._eq_update_job:
            self.root.after_cancel(self._eq_update_job)
            self._eq_update_job = None
        self._reset_eq()

    def _schedule_recording_watch(self):
        """手動録音中の局のバックグラウンドスレッドが（時間経過等で）自然終了していないか監視する"""
        if self.manager.is_recording_active(self._manual_recording_station):
            self._recording_watch_job = self.root.after(500, self._schedule_recording_watch)
        else:
            self._recording_watch_job = None
            if str(self.stop_button['state']) == tk.NORMAL:
                self.stop_recording()
