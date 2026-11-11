"""ダイアログ・時刻/日付入力・Treeview・操作ロックなど画面共通の部品"""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from datetime import datetime
import calendar


class WidgetHelpersMixin:
    def _ask_integer_ja(self, title, prompt, initialvalue, minvalue, maxvalue):
        """整数入力ダイアログ（エラーメッセージを日本語で表示する）"""
        value = initialvalue
        while True:
            text = simpledialog.askstring(title, prompt, initialvalue=value)
            if text is None:
                return None
            try:
                value = int(text)
            except ValueError:
                messagebox.showerror("入力エラー", "整数を入力してください。")
                continue
            if value < minvalue or value > maxvalue:
                messagebox.showerror(
                    "入力エラー",
                    f"{minvalue}〜{maxvalue}の範囲で入力してください。"
                )
                continue
            return value

    def _create_list_treeview(self, list_frame, columns, on_double_click):
        """一覧タブ共通の、列見出しクリックで並べ替えできる複数選択Treeviewを作る

        Args:
            columns: (列キー, 見出し, 幅) のリスト
            on_double_click: 行をダブルクリックしたときに呼ぶ関数（引数なし）
        """
        # Ctrl/Shiftクリックで複数選択し、まとめて操作できるようにする
        tree = ttk.Treeview(
            list_frame, columns=[c[0] for c in columns], show="headings", selectmode="extended"
        )
        for col, heading, width in columns:
            tree.heading(col, text=heading, command=lambda c=col: self._sort_treeview_column(tree, c, False))
            tree.column(col, width=width, anchor=tk.W)

        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.LEFT, fill=tk.Y)
        tree.bind("<Double-1>", lambda e: on_double_click())
        return tree

    def _sort_treeview_column(self, tree, col, reverse):
        """Treeviewの列見出しクリックで、その列の値に基づき行を並べ替える
        （再クリックで昇順・降順を反転）。
        """
        self._apply_treeview_sort(tree, col, reverse)
        # 一覧を作り直した後も同じ並びを再現できるよう、最後のソート条件を覚えておく
        tree._sort_state = (col, reverse)
        tree.heading(col, command=lambda: self._sort_treeview_column(tree, col, not reverse))

    def _apply_treeview_sort(self, tree, col, reverse):
        """Treeviewの行を、指定列の値で並べ替える

        表示文字列のままでは正しく並ばない列（「980 KB」と「1.2 MB」が混じるサイズ列
        など）は、tree._sort_keys に {列名: 行ID→比較用の値} を登録しておくとそちらを使う。
        """
        sort_key = getattr(tree, "_sort_keys", {}).get(col)
        if sort_key is None:
            def sort_key(item):
                return tree.set(item, col)
        items = [(sort_key(item), item) for item in tree.get_children("")]
        items.sort(key=lambda pair: pair[0], reverse=reverse)
        for index, (_, item) in enumerate(items):
            tree.move(item, "", index)

    def _capture_treeview_view(self, tree, row_map):
        """一覧を作り直す前に、選択行・フォーカス行・スクロール位置を控えておく

        行のIDは作り直すたびに変わるため、row_map（行ID→データのキー）を使って
        データ側のキーで覚える。
        """
        return {
            'selected': {row_map[item] for item in tree.selection() if item in row_map},
            'focus': row_map.get(tree.focus()),
            'yview': tree.yview()[0],
        }

    def _restore_treeview_view(self, tree, row_map, view):
        """一覧の作り直し後に、ユーザーが最後に選んだソート条件を掛け直し、
        _capture_treeview_view で控えた選択行・フォーカス行・スクロール位置を戻す
        """
        sort_state = getattr(tree, "_sort_state", None)
        if sort_state is not None:
            self._apply_treeview_sort(tree, *sort_state)

        selected = [item for item, key in row_map.items() if key in view['selected']]
        if selected:
            tree.selection_set(selected)
        if view['focus'] is not None:
            for item, key in row_map.items():
                if key == view['focus']:
                    tree.focus(item)
                    break
        tree.yview_moveto(view['yview'])

    def _center_dialog_over_parent(self, dialog):
        """ダイアログをメインウィンドウの中央（正面）に配置する"""
        dialog.update_idletasks()
        width = dialog.winfo_reqwidth()
        height = dialog.winfo_reqheight()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _create_time_selectors(self, parent, initial="00:00"):
        """時・分をそれぞれ選択できる読み取り専用コンボボックス2つを1つのFrameにまとめて返す

        ttk.Spinboxはsv_ttkテーマだと矢印ボタンの画像が大きく浮いて見えるため、
        他のコンボボックスと統一感のある見た目にするためこちらを使う。
        読み取り専用のため値は常に妥当で、個別のバリデーションも不要。

        Returns:
            (ttk.Frame, tk.StringVar, tk.StringVar): (配置用フレーム, 時変数, 分変数)
        """
        try:
            hour, minute = (int(p) for p in initial.split(":"))
        except (ValueError, AttributeError):
            hour, minute = 0, 0

        hour_var = tk.StringVar(value=f"{hour:02d}")
        minute_var = tk.StringVar(value=f"{minute:02d}")

        time_frame = ttk.Frame(parent)
        ttk.Combobox(
            time_frame, textvariable=hour_var, values=[f"{h:02d}" for h in range(24)],
            state="readonly", width=3
        ).pack(side=tk.LEFT)
        ttk.Label(time_frame, text="時").pack(side=tk.LEFT, padx=(4, 10))
        ttk.Combobox(
            time_frame, textvariable=minute_var, values=[f"{m:02d}" for m in range(60)],
            state="readonly", width=3
        ).pack(side=tk.LEFT)
        ttk.Label(time_frame, text="分").pack(side=tk.LEFT, padx=(4, 0))

        return time_frame, hour_var, minute_var

    def _get_time_value(self, hour_var, minute_var):
        """時・分のtk.StringVarから "HH:MM" 文字列を作る"""
        return f"{int(hour_var.get()):02d}:{int(minute_var.get()):02d}"

    def _open_calendar_picker(self, anchor_widget, date_var):
        """anchor_widgetの下にカレンダーをポップアップ表示し、選択した日付をdate_varに設定する"""
        try:
            initial = datetime.strptime(date_var.get(), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            initial = datetime.now().date()

        popup = tk.Toplevel(self.root)
        popup.wm_overrideredirect(True)
        popup.transient(self.root)
        x = anchor_widget.winfo_rootx()
        y = anchor_widget.winfo_rooty() + anchor_widget.winfo_height()
        popup.wm_geometry(f"+{x}+{y}")

        state = {'year': initial.year, 'month': initial.month}

        outer = ttk.Frame(popup, padding=6, relief=tk.SOLID, borderwidth=1)
        outer.pack()

        header = ttk.Frame(outer)
        header.pack(fill=tk.X)
        month_label_var = tk.StringVar()
        ttk.Button(header, text="◀", width=2, command=lambda: change_month(-1)).pack(side=tk.LEFT)
        ttk.Label(
            header, textvariable=month_label_var, width=10, anchor=tk.CENTER
        ).pack(side=tk.LEFT, expand=True)
        ttk.Button(header, text="▶", width=2, command=lambda: change_month(1)).pack(side=tk.LEFT)

        days_frame = ttk.Frame(outer)
        days_frame.pack(pady=(6, 0))

        def render():
            for widget in days_frame.winfo_children():
                widget.destroy()
            month_label_var.set(f"{state['year']}年 {state['month']}月")
            for col, weekday_name in enumerate(self.manager.WEEKDAY_JA):
                ttk.Label(days_frame, text=weekday_name, width=3, anchor=tk.CENTER).grid(row=0, column=col)

            today = datetime.now().date()
            for row, week in enumerate(calendar.Calendar(firstweekday=0).monthdayscalendar(
                state['year'], state['month']
            ), start=1):
                for col, day in enumerate(week):
                    if day == 0:
                        ttk.Label(days_frame, text="").grid(row=row, column=col)
                        continue
                    is_today = (state['year'], state['month'], day) == (today.year, today.month, today.day)
                    ttk.Button(
                        days_frame, text=str(day), width=3,
                        style="Accent.TButton" if is_today else "TButton",
                        command=lambda d=day: select_day(d)
                    ).grid(row=row, column=col, padx=1, pady=1)

        def change_month(delta):
            month = state['month'] + delta
            year = state['year']
            if month < 1:
                month, year = 12, year - 1
            elif month > 12:
                month, year = 1, year + 1
            state['month'], state['year'] = month, year
            render()

        def select_day(day):
            date_var.set(f"{state['year']:04d}-{state['month']:02d}-{day:02d}")
            popup.destroy()

        render()
        popup.focus_set()
        popup.bind("<Escape>", lambda e: popup.destroy())

    def _set_app_busy(self, busy):
        """番組表取得中、カーソルを砂時計にしてアプリ全体の操作を不可にする"""
        self.root.config(cursor="wait" if busy else "")
        if busy:
            self._busy_saved_states = {}
            self._disable_widgets_recursive(self.root)
        else:
            self._restore_widgets_recursive(self.root)
        self.root.update()

    def _disable_widgets_recursive(self, widget):
        for child in widget.winfo_children():
            try:
                self._busy_saved_states[child] = child.cget('state')
                child.configure(state='disabled')
            except tk.TclError:
                pass
            self._disable_widgets_recursive(child)

    def _restore_widgets_recursive(self, widget):
        for child in widget.winfo_children():
            if child in self._busy_saved_states:
                try:
                    child.configure(state=self._busy_saved_states[child])
                except tk.TclError:
                    pass
            self._restore_widgets_recursive(child)
