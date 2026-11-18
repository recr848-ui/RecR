"""CMカット用フィンガープリント取得・CMチャプター付けの定期実行と設定ダイアログ"""

import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path

from utils.app_logger import LOG_DIR

logger = logging.getLogger(__name__)


class CmFingerprintMixin:
    # CMフィンガープリントの自動取得: 起動後に最初のチェックを行うまでの待ち時間と、
    # 以降のチェック間隔（ミリ秒）
    CM_FP_STARTUP_DELAY_MS = 2 * 60 * 1000
    CM_FP_CHECK_INTERVAL_MS = 60 * 1000
    # 放送から3日経った録音にCMチャプターを付け直すかを確かめる間隔
    CM_CHAPTER_DUE_INTERVAL = timedelta(minutes=30)

    def _check_cm_fp_run_due(self):
        """CMフィンガープリントの自動取得（1日1回）を始める時刻かを定期的にチェックする

        全局番組表の自動更新と同じく「指定時刻を過ぎていて、その日まだ始めていなければ
        始める」ので、指定時刻にPCが止まっていても、次に起動したときに取り戻せる。
        """
        try:
            settings = self.manager.load_settings()
            if settings.get('cm_fp_enabled'):
                now = datetime.now()
                try:
                    target_time = datetime.strptime(settings.get('cm_fp_time', "06:00"), "%H:%M").time()
                except ValueError:
                    target_time = datetime.strptime("06:00", "%H:%M").time()
                today_iso = now.strftime("%Y-%m-%d")
                if (
                    now >= datetime.combine(now.date(), target_time)
                    and settings.get('cm_fp_last_run_date') != today_iso
                    and not self.cm_fp_collector.is_running()
                ):
                    self.manager.save_settings({'cm_fp_last_run_date': today_iso})
                    self.cm_fp_collector.update_settings(settings)
                    self.cm_fp_collector.start_run("毎日の自動取得")
                    logger.info("CMフィンガープリントの自動取得を開始しました")
        except Exception:
            logger.exception("CMフィンガープリントの自動取得チェックでエラーが発生しました")
        try:
            self._queue_due_cm_chapters()
        except Exception:
            logger.exception("CMチャプターの付け直しチェックでエラーが発生しました")
        self._cm_fp_check_job = self.root.after(self.CM_FP_CHECK_INTERVAL_MS, self._check_cm_fp_run_due)

    def _queue_due_cm_chapters(self):
        """放送から3日経った録音に、CMチャプターを付け直す（指紋取得の実行中は待つ）"""
        now = datetime.now()
        last = self._cm_chapter_due_checked
        if last is not None and now - last < self.CM_CHAPTER_DUE_INTERVAL:
            return
        if not self.cm_chapterer.settings['cm_chapter_auto'] or self.cm_fp_collector.is_running():
            return
        self._cm_chapter_due_checked = now
        for path in self.cm_chapterer.due_paths(now):
            self.cm_chapterer.request(path, "放送から3日後の付け直し")

    def _request_cm_chapters(self, output_path):
        """録音・タイムフリー取得が終わったファイルに、CMチャプターを付ける（自動がオンのとき）"""
        if not output_path or not self.cm_chapterer.settings['cm_chapter_auto']:
            return
        path = Path(output_path)
        if path.suffix.lower() == ".m4a" and path.exists() and path.stat().st_size > 0:
            self.cm_chapterer.request(path, "録音直後")

    def _on_cm_chapters_finished(self, path, record):
        """CMチャプターを付け終えたとき（メインスレッドから呼ばれる）"""
        if record.get('result') == 'error':
            logger.warning(f"CMチャプターを付けられませんでした: {path}（{record.get('error')}）")
        if hasattr(self, 'files_tree'):
            self._refresh_files_list()

    def _cm_fp_busy_reason(self):
        """CMフィンガープリント取得を止めるべきなら理由を返す（取得スレッドから呼ばれる）

        オンタイム録音・タイムフリー取得・NHK聴き逃し取得の間は止める。
        """
        parts = []
        recordings = sorted(self._active_recordings)
        if recordings:
            parts.append(f"オンタイム録音中（{'、'.join(recordings)}）")
        elif self.manager.is_recording_active():
            parts.append("オンタイム録音中")
        downloads = list(self._active_downloads.values())
        if downloads:
            titles = "、".join(f"{e['station']}「{e['title']}」" for e in downloads)
            parts.append(f"タイムフリー・聴き逃し取得中（{titles}）")
        elif self.manager.has_active_downloads():
            parts.append("タイムフリー取得中")
        return "・".join(parts) or None

    def _open_cm_fp_dialog(self):
        """メニュー「設定」→「CMフィンガープリント取得...」: 設定と状態の表示、今すぐ取得・停止"""
        settings = self.manager.load_settings()
        collector = self.cm_fp_collector
        current = dict(collector.settings)
        current.update({k: settings[k] for k in current if k in settings})

        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("CMフィンガープリント取得")
        dialog.transient(self.root)
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame,
            text="CMカットの準備として、予約が入っている局（NHKを除く）の放送を\n"
                 "タイムフリーで取得し、音声の指紋を保存します。\n"
                 "録音・タイムフリー取得・聴き逃し取得の間は止まり、終わると再開します。",
            justify=tk.LEFT
        ).pack(anchor=tk.W, pady=(0, 10))

        enabled_var = tk.BooleanVar(value=bool(current['cm_fp_enabled']))
        ttk.Checkbutton(frame, text="毎日自動で取得する", variable=enabled_var).pack(anchor=tk.W)

        time_row = ttk.Frame(frame)
        time_row.pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(time_row, text="開始時刻: ").pack(side=tk.LEFT)
        time_frame, hour_var, minute_var = self._create_time_selectors(
            time_row, initial=current['cm_fp_time']
        )
        time_frame.pack(side=tk.LEFT)

        window_row = ttk.Frame(frame)
        window_row.pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(window_row, text="取得する時間帯: ").pack(side=tk.LEFT)
        hours = [str(h) for h in range(5, 30)]
        window_start_var = tk.StringVar(value=str(current['cm_fp_window_start']))
        window_end_var = tk.StringVar(value=str(current['cm_fp_window_end']))
        ttk.Combobox(
            window_row, textvariable=window_start_var, values=hours, state="readonly", width=3
        ).pack(side=tk.LEFT)
        ttk.Label(window_row, text="時 〜 ").pack(side=tk.LEFT, padx=(4, 4))
        ttk.Combobox(
            window_row, textvariable=window_end_var, values=hours, state="readonly", width=3
        ).pack(side=tk.LEFT)
        ttk.Label(window_row, text="時（24時以降は翌日。例: 27時 = 翌3時）").pack(side=tk.LEFT, padx=(4, 0))

        retention_row = ttk.Frame(frame)
        retention_row.pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(retention_row, text="指紋の保存期間: ").pack(side=tk.LEFT)
        retention_var = tk.StringVar(value=str(current['cm_fp_retention_days']))
        ttk.Combobox(
            retention_row, textvariable=retention_var,
            values=["7", "14", "21", "28", "42", "56", "90"], state="readonly", width=4
        ).pack(side=tk.LEFT)
        ttk.Label(retention_row, text="日").pack(side=tk.LEFT, padx=(4, 0))

        keep_audio_var = tk.BooleanVar(value=bool(current['cm_fp_keep_audio']))
        ttk.Checkbutton(
            frame, text="取得した音声も保存する（調整用。1局1時間あたり約20MB）",
            variable=keep_audio_var
        ).pack(anchor=tk.W, pady=(8, 0))

        chapter_auto_var = tk.BooleanVar(value=bool(self.cm_chapterer.settings['cm_chapter_auto']))
        ttk.Checkbutton(
            frame, text="録音にCMチャプターを自動で付ける（録音直後と、放送から3日後にもう一度）",
            variable=chapter_auto_var
        ).pack(anchor=tk.W, pady=(8, 0))

        status_var = tk.StringVar()
        ttk.Label(frame, textvariable=status_var, foreground="#3a6ea5").pack(anchor=tk.W, pady=(12, 0))
        chapter_status_var = tk.StringVar()
        ttk.Label(frame, textvariable=chapter_status_var, foreground="#3a6ea5").pack(anchor=tk.W)

        def refresh_status():
            if not dialog.winfo_exists():
                return
            status = collector.get_status()
            text = status['message']
            if status['state'] != 'idle' and status['total']:
                text += f"　{status['done']}/{status['total']}件完了"
            status_var.set(f"状態: {text}")
            chapter = self.cm_chapterer.get_status()
            chapter_text = chapter['message']
            if chapter['queued'] > 1:
                chapter_text += f"（ほか{chapter['queued'] - 1}件待ち）"
            chapter_status_var.set(f"CMチャプター: {chapter_text}")
            run_button.config(state=tk.DISABLED if collector.is_running() else tk.NORMAL)
            stop_button.config(state=tk.NORMAL if collector.is_running() else tk.DISABLED)
            dialog.after(1000, refresh_status)

        def collect_values():
            start, end = int(window_start_var.get()), int(window_end_var.get())
            if start >= end:
                messagebox.showerror(
                    "CMフィンガープリント取得", "時間帯の終わりは始まりより後にしてください", parent=dialog
                )
                return None
            return {
                'cm_fp_enabled': enabled_var.get(),
                'cm_fp_time': self._get_time_value(hour_var, minute_var),
                'cm_fp_window_start': start,
                'cm_fp_window_end': end,
                'cm_fp_retention_days': int(retention_var.get()),
                'cm_fp_keep_audio': keep_audio_var.get(),
                'cm_chapter_auto': chapter_auto_var.get(),
            }

        def apply_values():
            values = collect_values()
            if values is None:
                return None
            self.manager.save_settings(values)
            collector.update_settings(values)
            self.cm_chapterer.update_settings(values)
            if not values['cm_fp_enabled'] and collector.is_running():
                collector.stop("自動取得をオフにしました")
            return values

        def on_save():
            if apply_values() is not None:
                dialog.destroy()

        def on_run_now():
            if apply_values() is None:
                return
            collector.start_run("今すぐ取得")
            logger.info("CMフィンガープリントの取得を手動で開始しました")

        def on_stop():
            collector.stop("停止ボタン")

        button_frame = ttk.Frame(frame)
        button_frame.pack(pady=(15, 0))
        run_button = ttk.Button(button_frame, text="今すぐ取得", command=on_run_now)
        run_button.pack(side=tk.LEFT, padx=5)
        stop_button = ttk.Button(button_frame, text="停止", command=on_stop)
        stop_button.pack(side=tk.LEFT, padx=5)
        ttk.Button(
            button_frame, text="ログフォルダを開く", command=lambda: os.startfile(str(LOG_DIR))
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="保存", command=on_save).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="閉じる", command=dialog.destroy).pack(side=tk.LEFT, padx=5)

        refresh_status()
        self._center_dialog_over_parent(dialog)
        dialog.deiconify()
