"""右上パネル：進行中の録音・タイムフリーダウンロードの表示と停止"""

import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timedelta

from utils import reservation_logic


class ActiveTasksMixin:
    def _register_active_recording(self, station, title, duration_minutes):
        """右上パネルに表示する「現在録音中」の情報を登録する"""
        start_dt = datetime.now()
        self._active_recordings[station] = {
            'title': title,
            'start_dt': start_dt,
            'end_dt': start_dt + timedelta(minutes=duration_minutes),
        }
        self._refresh_active_recordings_panel()
        self._update_tray_icon_state()

    def _unregister_active_recording(self, station):
        """録音の終了に伴い、右上パネルからその局の表示を取り除く"""
        if self._active_recordings.pop(station, None) is not None:
            self._refresh_active_recordings_panel()
            self._update_tray_icon_state()

    def _register_active_download(self, key, station, ft, title):
        """右上パネルに表示する「タイムフリー取得中」の情報を登録する"""
        self._active_downloads[key] = {
            'key': key, 'station': station, 'ft': ft, 'title': title, 'done': 0, 'total': None,
            'start_dt': datetime.now(),
        }
        self._refresh_active_recordings_panel()

    def _update_active_download_progress(self, key, done, total):
        """タイムフリー取得の進捗更新（メインスレッドから呼ばれる）

        パネル全体を再構築せず、該当行のラベルだけを直接書き換える
        （セグメント数が多い番組では頻繁に呼ばれるため）。
        """
        entry = self._active_downloads.get(key)
        if entry is None:
            return
        entry['done'] = done
        entry['total'] = total
        label = self._download_progress_labels.get(key)
        if label is not None:
            label.config(text=self._format_download_progress_text(entry))

    def _unregister_active_download(self, key):
        """タイムフリー取得の終了に伴い、右上パネルからその表示を取り除く"""
        if self._active_downloads.pop(key, None) is not None:
            self._download_progress_labels.pop(key, None)
            self._refresh_active_recordings_panel()

    def _format_download_progress_text(self, entry):
        label = "NHK聴き逃し" if "|radiru|" in entry.get('key', '') else "タイムフリー"
        base = f"⬇ {entry['station']}「{entry['title']}」 {label}取得中..."
        done, total = entry['done'], entry['total']
        if not total:
            return base

        percent = int(done * 100 / total)
        remaining_text = ""
        if done > 0:
            elapsed = (datetime.now() - entry['start_dt']).total_seconds()
            if elapsed > 0:
                remaining_seconds = (total - done) * (elapsed / done)
                remaining_text = f"（残り約{self._format_duration(remaining_seconds)}）"
        return f"{base} {percent}%{remaining_text}"

    def _format_duration(self, seconds):
        """秒数を「1時間2分」「5分」「30秒」のような大まかな日本語表記にする"""
        return reservation_logic.format_duration(seconds)

    def _refresh_active_recordings_panel(self):
        """右上の「現在録音中／タイムフリー取得中」パネルを、現在の状態から作り直す

        録音の各行には停止ボタンを添え、手動録音・予約録音のどちらもここから途中停止できるようにする。
        """
        for widget in self.active_recordings_frame.winfo_children():
            widget.destroy()
        self._download_progress_labels = {}
        for station in sorted(self._active_recordings):
            info = self._active_recordings[station]
            text = (
                f"● {station}「{info['title']}」 "
                f"{info['start_dt'].strftime('%H:%M')}-{info['end_dt'].strftime('%H:%M')}"
            )
            row = ttk.Frame(self.active_recordings_frame)
            row.pack(anchor=tk.E)
            ttk.Label(
                row, text=text, foreground="#d33", font=("Yu Gothic UI", 9)
            ).pack(side=tk.LEFT)
            ttk.Button(
                row, text="■", width=2,
                command=lambda s=station: self._stop_recording_by_station(s)
            ).pack(side=tk.LEFT, padx=(4, 0))
        for key in sorted(self._active_downloads):
            entry = self._active_downloads[key]
            row = ttk.Frame(self.active_recordings_frame)
            row.pack(anchor=tk.E)
            label = ttk.Label(
                row, text=self._format_download_progress_text(entry),
                foreground="#3a6ea5", font=("Yu Gothic UI", 9)
            )
            label.pack(side=tk.LEFT)
            self._download_progress_labels[key] = label
            ttk.Button(
                row, text="■", width=2,
                command=lambda k=key: self._stop_download_by_key(k)
            ).pack(side=tk.LEFT, padx=(4, 0))

    def _stop_download_by_key(self, key):
        """指定のタイムフリー取得（「局名|ft」キー）を停止する

        それまでにダウンロード済みの内容はファイルに残る。停止処理はネットワーク
        スレッドの終了待ち（最大5秒）を伴うため、_set_app_busy でその間操作を止める。
        """
        entry = self._active_downloads.get(key)
        if entry is None:
            return
        self._set_app_busy(True)
        try:
            self.manager.stop_download(entry['station'], entry['ft'])
        finally:
            self._set_app_busy(False)
        self._unregister_active_download(key)

    def _stop_recording_by_station(self, station):
        """指定局の録音を停止する（手動録音・予約録音のどちらでも使える共通処理）

        手動録音タブが今まさにその局を制御中であれば、ボタン状態やREC点滅も
        まとめてリセットするため録音タブ側のstop_recording()を経由させる。
        それ以外（予約録音などバックグラウンドの録音）はマネージャーを直接止める。
        """
        if not station:
            return
        if station == self._manual_recording_station:
            self.stop_recording()
        else:
            self.manager.stop_recording(station)
            self._unregister_active_recording(station)
            self._refresh_reservation_list()

    def _stop_selected_reservation_recording(self):
        """予約一覧で選択中の予約のうち、現在録音中のものを停止する"""
        reservations = self._get_selected_reservations()
        if not reservations:
            return
        stations = {
            r.get('station') for r in reservations
            if self.manager.is_recording_active(r.get('station'), reservation_id=r.get('id'))
        }
        if not stations:
            messagebox.showinfo("予約録音", "選択した予約の中に、現在録音中のものはありません。")
            return
        for station in stations:
            self._stop_recording_by_station(station)
