"""「保存先フォルダ」タブ：録音ファイルの一覧・再生・削除"""

import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime
import os


class FilesTabMixin:
    AUDIO_FILE_EXTENSIONS = {".aac", ".m4a", ".mp3", ".wav"}

    def setup_files_tab(self, parent):
        """「保存先フォルダ」タブのUIをセットアップ（録音済み音声ファイルの一覧・再生・削除）"""
        control_frame = ttk.Frame(parent, padding=10)
        control_frame.pack(fill=tk.X)

        ttk.Button(
            control_frame, text="更新", command=self._refresh_files_list
        ).pack(side=tk.LEFT)
        ttk.Button(
            control_frame, text="再生", command=self._play_selected_file
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="フォルダを開く", command=self._open_output_folder
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="保存先を変更...", command=self._change_output_dir
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="削除", command=self._delete_selected_files
        ).pack(side=tk.LEFT, padx=5)

        self.files_dir_label = ttk.Label(parent, foreground="gray30", padding=(10, 0))
        self.files_dir_label.pack(fill=tk.X)

        list_frame = ttk.Frame(parent, padding=10)
        list_frame.pack(fill=tk.BOTH, expand=True)

        tree = self._create_list_treeview(list_frame, [
            ("name", "ファイル名", 400),
            ("modified", "更新日時", 150),
            ("size", "サイズ", 100),
        ], self._play_selected_file)

        self.files_tree = tree
        self._files_row_map = {}
        # サイズ列は「KB」「MB」の表示が混じるため、バイト数で並べ替える
        self._files_row_sizes = {}
        tree._sort_keys = {"size": lambda item: self._files_row_sizes.get(item, 0)}
        self._refresh_files_list()

    def _refresh_files_list(self):
        """保存先フォルダ内の音声ファイル一覧を作り直す（音声ファイルのみ表示）"""
        tree = self.files_tree
        view = self._capture_treeview_view(tree, self._files_row_map)
        tree.delete(*tree.get_children())
        self._files_row_map = {}
        self._files_row_sizes = {}

        output_dir = self.manager.output_dir
        self.files_dir_label.configure(text=f"保存先: {output_dir}")
        if not output_dir.exists():
            return

        files = [
            f for f in output_dir.iterdir()
            if f.is_file() and f.suffix.lower() in self.AUDIO_FILE_EXTENSIONS
        ]
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        for f in files:
            stat = f.stat()
            modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            size_kb = stat.st_size / 1024
            size_text = f"{size_kb / 1024:.1f} MB" if size_kb >= 1024 else f"{size_kb:.0f} KB"
            item_id = tree.insert("", tk.END, values=(f.name, modified, size_text))
            self._files_row_map[item_id] = f
            self._files_row_sizes[item_id] = stat.st_size
        self._restore_treeview_view(tree, self._files_row_map, view)

    def _get_selected_files(self):
        """ファイル一覧で選択中の行に対応するパスを全て取得する（未選択なら空リスト）"""
        selection = self.files_tree.selection()
        if not selection:
            messagebox.showinfo("保存先フォルダ", "ファイルを選択してください。")
            return []
        return [self._files_row_map[item_id] for item_id in selection if item_id in self._files_row_map]

    def _play_selected_file(self):
        """選択中のファイルをOSの既定アプリで再生する（1件ずつ）"""
        files = self._get_selected_files()
        if not files:
            return
        if len(files) > 1:
            messagebox.showinfo("保存先フォルダ", "再生は1件ずつ選択して行ってください。")
            return
        if not files[0].exists():
            messagebox.showerror("保存先フォルダ", "ファイルが見つかりません。")
            self._refresh_files_list()
            return
        os.startfile(str(files[0]))

    def _delete_selected_files(self):
        """選択中のファイルを削除する"""
        files = self._get_selected_files()
        if not files:
            return
        if len(files) == 1:
            message = f"「{files[0].name}」を削除しますか？\n（この操作は取り消せません）"
        else:
            message = f"選択した{len(files)}件のファイルを削除しますか？\n（この操作は取り消せません）"
        if not messagebox.askyesno("ファイルの削除", message):
            return
        for f in files:
            try:
                f.unlink()
            except OSError as e:
                messagebox.showerror("ファイルの削除", f"「{f.name}」を削除できませんでした。\n{e}")
        self._refresh_files_list()
