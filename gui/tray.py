"""ウィンドウを閉じたときのタスクトレイ常駐とアプリ終了"""

from tkinter import messagebox
import logging

from PIL import Image, ImageDraw
import pystray

logger = logging.getLogger(__name__)


class TrayMixin:
    def on_close_button(self):
        """ウィンドウの×ボタン: アプリを終了せず、タスクトレイへ最小化する"""
        self.root.withdraw()
        self._show_tray_icon()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.manager.save_settings({'tray_hint_shown': True})
            self.root.after(500, lambda: self._notify_via_tray(
                "RecR", "タスクトレイに最小化しました。アイコンをクリックすると元に戻ります。"
            ))

    def _create_tray_image(self):
        """タスクトレイアイコン用の画像を生成する（専用のアイコンファイルは無いため簡易生成）

        録音中かどうかで色を変え、ウィンドウを開かなくても状態が一目で分かるようにする
        """
        size = 64
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        color = "#c1392b" if self._active_recordings else "#3a6ea5"
        draw.ellipse((2, 2, size - 2, size - 2), fill=color)
        draw.ellipse((size * 0.32, size * 0.32, size * 0.68, size * 0.68), fill="#ffffff")
        return image

    def _tray_title(self):
        """タスクトレイアイコンのツールチップ文字列（録音中ならその局名を含める）"""
        if self._active_recordings:
            stations = "、".join(sorted(self._active_recordings))
            return f"RecR - 録音中（{stations}）"
        return "RecR - ラジコ録音アプリ"

    def _show_tray_icon(self):
        """タスクトレイアイコンを表示する（未生成なら生成する）"""
        if self._tray_icon is not None:
            return
        menu = pystray.Menu(
            pystray.MenuItem("表示", self._on_tray_restore, default=True),
            pystray.MenuItem("終了", self._on_tray_quit),
        )
        self._tray_icon = pystray.Icon(
            "RecR", self._create_tray_image(), self._tray_title(), menu
        )
        self._tray_icon.run_detached()

    def _hide_tray_icon(self):
        """タスクトレイアイコンを非表示にする"""
        if self._tray_icon is not None:
            self._tray_icon.stop()
            self._tray_icon = None

    def _update_tray_icon_state(self):
        """録音の開始/終了に合わせて、表示中のタスクトレイアイコンの見た目とツールチップを更新する"""
        if self._tray_icon is None:
            return
        self._tray_icon.icon = self._create_tray_image()
        self._tray_icon.title = self._tray_title()

    def _notify_via_tray(self, title, message):
        """タスクトレイアイコン経由でバルーン通知を出す（アイコン未表示なら何もしない）"""
        if self._tray_icon is None:
            return
        try:
            self._tray_icon.notify(message, title)
        except Exception:
            logger.exception("タスクトレイ通知の表示に失敗しました")

    def _on_tray_restore(self, icon=None, item=None):
        """タスクトレイアイコンのクリック/「表示」選択時: ウィンドウを元に戻す

        pystrayのコールバックは専用スレッドで呼ばれるため、Tkの操作はafter経由でメインスレッドに委譲する
        """
        self.root.after(0, self._restore_window)

    def _restore_window(self):
        self._hide_tray_icon()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_tray_quit(self, icon=None, item=None):
        """タスクトレイメニューの「終了」選択時: アプリを終了する"""
        self.root.after(0, self.quit_app)

    def quit_app(self):
        """アプリを終了する（メニューの「終了」、またはタスクトレイの「終了」から呼ばれる）

        録音中であれば、中断されることを確認してから終了する。
        了承が得られたら、再生中・録音中の処理を停止してから終了する
        """
        if self._active_recordings or self._active_downloads:
            parts = []
            if self._active_recordings:
                parts.append(f"録音中（{'、'.join(sorted(self._active_recordings))}）")
            if self._active_downloads:
                titles = "、".join(
                    f"{e['station']}「{e['title']}」" for e in self._active_downloads.values()
                )
                parts.append(f"タイムフリー取得中（{titles}）")
            if not messagebox.askyesno(
                "終了確認",
                f"現在{'・'.join(parts)}です。\n"
                "終了すると中断されます。終了してもよろしいですか？"
            ):
                return
        self._hide_tray_icon()
        if self._eq_update_job:
            self.root.after_cancel(self._eq_update_job)
            self._eq_update_job = None
        if self._recording_watch_job:
            self.root.after_cancel(self._recording_watch_job)
            self._recording_watch_job = None
        if self._rec_blink_job:
            self.root.after_cancel(self._rec_blink_job)
            self._rec_blink_job = None
        if self._reservation_check_job:
            self.root.after_cancel(self._reservation_check_job)
            self._reservation_check_job = None
        if self._reservation_list_refresh_job:
            self.root.after_cancel(self._reservation_list_refresh_job)
            self._reservation_list_refresh_job = None
        if self._program_guide_refresh_job:
            self.root.after_cancel(self._program_guide_refresh_job)
            self._program_guide_refresh_job = None
        if self._full_refresh_check_job:
            self.root.after_cancel(self._full_refresh_check_job)
            self._full_refresh_check_job = None
        if self._missed_recovery_job:
            self.root.after_cancel(self._missed_recovery_job)
            self._missed_recovery_job = None
        if self._cm_fp_check_job:
            self.root.after_cancel(self._cm_fp_check_job)
            self._cm_fp_check_job = None
        self.cm_fp_collector.stop("アプリ終了", wait=3)
        self.manager.stop_playback()
        self.manager.stop_recording()
        self.manager.stop_all_downloads()
        self.root.destroy()
