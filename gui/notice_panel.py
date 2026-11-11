"""「録音」タブ右側のお知らせ欄（GitHub上のNOTICE.mdを表示）"""

import tkinter as tk
from tkinter import ttk
import logging
import threading

import requests

logger = logging.getLogger(__name__)


class NoticePanelMixin:
    def _setup_notice_panel(self, parent):
        """「録音」タブ右側のお知らせ欄をセットアップ

        GitHub上のNOTICE.md（NOTICE_URL）を取得し、テキストとしてそのまま表示する。
        本物のHTML/Markdownレンダリングは行わない（tk.Textによるプレーンテキスト表示）。
        """
        notice_frame = ttk.LabelFrame(parent, text="お知らせ", padding=10)
        notice_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 10), pady=10)

        header = ttk.Frame(notice_frame)
        header.pack(fill=tk.X)
        self.notice_status_var = tk.StringVar(value="")
        ttk.Label(header, textvariable=self.notice_status_var, font=("Yu Gothic UI", 8)).pack(
            side=tk.LEFT
        )
        ttk.Button(header, text="更新", width=6, command=self._refresh_notice).pack(side=tk.RIGHT)

        text_container = ttk.Frame(notice_frame)
        text_container.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        scrollbar = ttk.Scrollbar(text_container, orient=tk.VERTICAL)
        self.notice_text = tk.Text(
            text_container, wrap=tk.WORD, state=tk.DISABLED, relief=tk.FLAT,
            padx=8, pady=8, font=("Yu Gothic UI", 10),
            yscrollcommand=scrollbar.set, borderwidth=0, highlightthickness=0
        )
        scrollbar.config(command=self.notice_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.notice_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._apply_notice_theme()
        self._set_notice_text("読み込み中…")

    def _get_notice_colors(self):
        """現在のテーマ（ライト/ダーク）に応じたお知らせ欄（tk.Text）の配色を返す

        tk.Textもtk.Menu/tk.Canvasと同様にttkテーマの管理外のため、テーマ切り替え時に
        自前で配色し直す必要がある
        """
        schedule_colors = self._get_schedule_colors()
        return {'bg': schedule_colors['bg'], 'fg': schedule_colors['title']}

    def _apply_notice_theme(self):
        """お知らせ欄（tk.Text）に、現在のテーマの配色を反映し直す"""
        if hasattr(self, 'notice_text'):
            self.notice_text.configure(**self._get_notice_colors())

    def _refresh_notice(self):
        """お知らせ欄の内容をGitHubから取得し直す（バックグラウンドスレッドで実行）"""
        self.notice_status_var.set("取得中…")
        thread = threading.Thread(target=self._fetch_notice_worker, daemon=True)
        thread.start()

    def _fetch_notice_worker(self):
        """バックグラウンドスレッド本体: NOTICE_URLからテキストを取得する。
        Tkinterウィジェットには一切触れず、完了後の反映はroot.after経由で行う。
        """
        try:
            res = requests.get(self.NOTICE_URL, timeout=8)
            res.raise_for_status()
            text = res.text.strip() or "（お知らせはありません）"
            status = ""
        except Exception as e:
            logger.warning(f"お知らせの取得に失敗しました: {e}")
            text = "お知らせを取得できませんでした。\nネットワーク接続を確認して「更新」を押してください。"
            status = "取得失敗"
        self.root.after(0, self._set_notice_text, text, status)

    def _set_notice_text(self, text, status=""):
        """お知らせ欄のテキストを差し替える（メインスレッド専用）"""
        self.notice_text.configure(state=tk.NORMAL)
        self.notice_text.delete("1.0", tk.END)
        self.notice_text.insert("1.0", text)
        self.notice_text.configure(state=tk.DISABLED)
        self.notice_status_var.set(status)
