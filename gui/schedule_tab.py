"""「番組表」タブ：番組表グリッドの描画と操作"""

import tkinter as tk
from tkinter import ttk, messagebox
from tkinter import font as tkfont
from datetime import datetime, timedelta
import io
import logging
import re
import threading
import time
import webbrowser

from PIL import Image, ImageTk

logger = logging.getLogger(__name__)


# 概要・詳細情報に含まれるURLを検出する正規表現
_URL_PATTERN = re.compile(r'https?://[^\s"\'<>]+')


class ScheduleTabMixin:
    def setup_schedule_tab(self, parent):
        """「番組表」タブのUIをセットアップ（縦軸=時刻のラテ欄風グリッドで表示）"""
        # 操作フレーム
        control_frame = ttk.Frame(parent, padding=10)
        control_frame.pack(fill=tk.X)

        ttk.Label(control_frame, text="ステーション：").pack(side=tk.LEFT)
        station_combo = ttk.Combobox(
            control_frame,
            textvariable=self.schedule_station_var,
            values=self.manager.get_stations(),
            state="readonly",
            width=14
        )
        station_combo.pack(side=tk.LEFT, padx=5)
        station_combo.bind("<<ComboboxSelected>>", self.on_schedule_station_changed)

        area_id, area_name = self.manager.get_area_info()
        area_text = f"エリア: {area_name}（{area_id}）" if area_id else "エリア: 判定失敗（関東の局一覧を表示中）"
        ttk.Label(control_frame, text=area_text, foreground="gray30").pack(side=tk.LEFT, padx=(15, 0))

        ttk.Button(
            control_frame,
            text="番組表を取得",
            command=self.load_schedule
        ).pack(side=tk.LEFT, padx=5)

        ttk.Radiobutton(
            control_frame, text="番組表", variable=self.schedule_mode_var,
            value="upcoming", command=self.on_schedule_mode_changed
        ).pack(side=tk.LEFT, padx=(15, 0))
        ttk.Radiobutton(
            control_frame, text="過去7日間（タイムフリー）", variable=self.schedule_mode_var,
            value="timefree", command=self.on_schedule_mode_changed
        ).pack(side=tk.LEFT, padx=(5, 0))

        ttk.Label(control_frame, text="文字サイズ：").pack(side=tk.LEFT, padx=(15, 0))
        for value, label in (('small', '小'), ('medium', '中'), ('large', '大')):
            ttk.Radiobutton(
                control_frame, text=label, variable=self.schedule_font_size_var,
                value=value, command=self._on_schedule_font_size_changed
            ).pack(side=tk.LEFT)

        self.search_toggle_button = ttk.Button(
            control_frame,
            text="検索 ▼",
            command=self.toggle_search_panel
        )
        self.search_toggle_button.pack(side=tk.LEFT, padx=5)

        # 検索パネル（トグルで表示/非表示）
        self.search_panel = ttk.Frame(parent, padding=10)
        self._search_panel_visible = False
        self.setup_search_panel(self.search_panel)

        # グリッド表示エリア: 上に日付ラベル（固定）、下に時刻グリッド（縦スクロール）
        grid_container = ttk.Frame(parent)
        grid_container.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self._grid_container = grid_container

        self.day_header_frame = tk.Frame(
            grid_container, height=self.DAY_HEADER_HEIGHT,
            background=self._get_schedule_colors()['bg']
        )
        self.day_header_frame.pack(side=tk.TOP, fill=tk.X)
        self.day_header_frame.pack_propagate(False)

        canvas_frame = ttk.Frame(grid_container)
        canvas_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.schedule_canvas = tk.Canvas(
            canvas_frame, background=self._get_schedule_colors()['bg'],
            highlightthickness=0, borderwidth=0
        )
        v_scroll = ttk.Scrollbar(
            canvas_frame, orient=tk.VERTICAL, command=self.schedule_canvas.yview
        )
        self.schedule_canvas.configure(yscrollcommand=v_scroll.set)
        self.schedule_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        v_scroll.pack(side=tk.LEFT, fill=tk.Y)

        self.schedule_canvas.bind(
            "<MouseWheel>",
            lambda e: self.schedule_canvas.yview_scroll(int(-e.delta / 120), "units")
        )

        # 番組枠のイベントは、アイテムごとではなく共有タグに対して一度だけ
        # バインドする。アイテムごとにtag_bindすると、毎分の再描画のたびに
        # Tclコマンドが登録され続け（canvas.delete では解放されない）、
        # 長時間起動でMemoryErrorになるため
        self._schedule_item_programs = {}
        self._schedule_item_urls = {}
        self._schedule_display_mode = 'upcoming'
        self.schedule_canvas.tag_bind(self.SCHEDULE_PROGRAM_TAG, "<Enter>", self._on_schedule_item_enter)
        self.schedule_canvas.tag_bind(
            self.SCHEDULE_PROGRAM_TAG, "<Leave>", lambda e: self._hide_tooltip()
        )
        self.schedule_canvas.tag_bind(
            self.SCHEDULE_PROGRAM_TAG, "<Double-Button-1>", self._on_schedule_item_double_click
        )
        self.schedule_canvas.tag_bind(
            self.SCHEDULE_LINK_TAG, "<Button-1>", self._on_schedule_link_click
        )
        self.schedule_canvas.tag_bind(
            self.SCHEDULE_LINK_TAG, "<Enter>",
            lambda e: self.schedule_canvas.config(cursor="hand2"), add="+"
        )
        self.schedule_canvas.tag_bind(
            self.SCHEDULE_LINK_TAG, "<Leave>",
            lambda e: self.schedule_canvas.config(cursor=""), add="+"
        )

        self._tooltip = None
        self.schedule_title_font = tkfont.Font(family="Yu Gothic UI", size=self.SCHEDULE_BASE_TITLE_SIZE)
        self.schedule_title_link_font = tkfont.Font(
            family="Yu Gothic UI", size=self.SCHEDULE_BASE_TITLE_SIZE, underline=1
        )
        self.schedule_desc_font = tkfont.Font(family="Yu Gothic UI", size=self.SCHEDULE_BASE_DESC_SIZE)
        self._apply_schedule_font_size()
        self._image_cache = {}
        self._wrap_cache = {}
        self._program_canvas_items = {}
        self._schedule_grid_height = 0
        self._highlight_reset_job = None
        # NHK聴き逃しの回ごとの配信確認: 描画し直すたびに増やし、古い描画への
        # 確認結果を捨てるための世代番号と、番組ごとのエピソード一覧のキャッシュ
        self._radiru_check_generation = 0
        self._radiru_episode_cache = {}

    def _jump_to_program(self, station, program):
        """検索結果クリック時: 対象局に切り替え、該当番組までスクロールして強調表示"""
        if self.schedule_station_var.get() != station:
            self.schedule_station_var.set(station)
            self.load_schedule_for_current_station()
        self.root.after(50, lambda: self._highlight_program(program))

    def _highlight_program(self, program):
        """該当番組の枠までスクロールし、一時的に赤枠で強調表示する"""
        key = (program.get('date_iso'), program.get('start'), program.get('title'))
        rect = self._program_canvas_items.get(key)
        if not rect:
            return

        bbox = self.schedule_canvas.bbox(rect)
        if bbox and self._schedule_grid_height:
            y1 = bbox[1]
            canvas_height = self.schedule_canvas.winfo_height()
            target_frac = max(0.0, (y1 - canvas_height / 3) / self._schedule_grid_height)
            self.schedule_canvas.yview_moveto(target_frac)

        if self._highlight_reset_job:
            self.root.after_cancel(self._highlight_reset_job)
            self._highlight_reset_job = None

        original_outline = self.schedule_canvas.itemcget(rect, 'outline')
        original_width = self.schedule_canvas.itemcget(rect, 'width')
        self.schedule_canvas.itemconfig(rect, outline="#ff2222", width=4)
        self._highlight_reset_job = self.root.after(
            self.HIGHLIGHT_DURATION_MS,
            lambda: self.schedule_canvas.itemconfig(
                rect, outline=original_outline, width=original_width
            )
        )

    def _scroll_schedule_to_now(self):
        """番組表（当日以降表示）を開いた直後、現在時刻が上寄りに見える位置まで
        自動スクロールする。過去の時間帯まで毎回手動でスクロールしなくて済むように。

        yview_moveto は表示領域全体に対する割合で指定するため、ウィンドウの
        描画がまだ完了していない起動直後でも（winfo_height()が未確定でも）
        正しく動く。
        """
        if not self._schedule_grid_height:
            return
        now_minutes = self._minutes_from_day_start(datetime.now().strftime("%H:%M"))
        y_now = now_minutes * self.PIXELS_PER_MINUTE
        margin = 20
        target_frac = max(0.0, (y_now - margin) / self._schedule_grid_height)
        self.schedule_canvas.yview_moveto(target_frac)

    def on_schedule_station_changed(self, event=None):
        """番組表タブのステーション切り替え時: 番組表を切り替え先のステーションのものに更新"""
        if self.schedule_mode_var.get() == 'timefree':
            self.load_timefree_schedule_for_current_station()
        else:
            self.load_schedule_for_current_station()

    def on_schedule_mode_changed(self):
        """「番組表」/「過去7日間」の表示モード切り替え時"""
        if self.schedule_mode_var.get() == 'timefree':
            self.load_timefree_schedule_for_current_station()
        else:
            self.load_schedule_for_current_station()

    def load_schedule_for_current_station(self):
        """選択中のステーションの番組表を表示する。
        キャッシュがあればそれを表示、なければ取得しないと表示しようがないので
        確認なしでそのまま取得する
        """
        station = self.schedule_station_var.get()
        cached = self.manager.load_cached_schedule(station)
        if cached:
            self.display_schedule(cached)
            self._scroll_schedule_to_now()
            return
        if self._stale_refresh_in_progress:
            # 起動時の自動更新がキャッシュの無い局（＝この局も含む）を取得中。ここで同じ局を
            # 同期取得すると二重取得になるうえ、その間メインスレッドが止まる。
            # 自動更新の完了時（_on_stale_refresh_done）にあらためて表示される
            return

        self._set_app_busy(True)
        try:
            programs, _ = self.fetch_and_cache_schedule(station)
        finally:
            self._set_app_busy(False)
        self.display_schedule(programs)
        self._scroll_schedule_to_now()

    def _has_programs_in_timefree_window(self, programs):
        """programsの中に、タイムフリー対象期間（当日を含む過去7日間）に入っている
        番組が1件でもあるか判定する。

        タイムフリーのキャッシュは日付が固定なので、キャッシュ自体は存在していても、
        7日以上前に取得したまま更新していないと、対象期間が丸ごと過ぎ去ってしまい
        display_scheduleの日付フィルタで全件除外されて空グリッドになる
        （実際に、9日前に取得したキャッシュを持つ局でこれが発生した）。
        そのため「キャッシュがあるか」だけでなく「今の対象期間内のデータが
        残っているか」まで見て、再取得を促すかどうかを判断する
        """
        today_iso = datetime.now().strftime("%Y-%m-%d")
        cutoff_iso = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        return any(
            p.get('date_iso') and cutoff_iso <= p['date_iso'] <= today_iso
            for p in programs
        )

    def load_timefree_schedule_for_current_station(self):
        """選択中のステーションのタイムフリー対象期間（過去7日間）の番組表を表示する。
        キャッシュがあればそれを表示、なければ取得しないと表示しようがないので
        確認なしでそのまま取得する

        通常の番組表キャッシュ（局名のみをキーにする）と衝突しないよう、
        "{局名}::timefree" というキーで別名空間に保存・取得
        する。
        """
        station = self.schedule_station_var.get()
        cache_key = f"{station}::timefree"
        cached = self.manager.load_cached_schedule(cache_key)
        if cached and self._has_programs_in_timefree_window(cached):
            self.display_schedule(cached, mode='timefree')
            return

        self._set_app_busy(True)
        try:
            programs = self.manager.get_timefree_schedule(station)
            self.manager.save_schedule_cache(cache_key, programs)
        finally:
            self._set_app_busy(False)
        self.display_schedule(programs, mode='timefree')

    def load_schedule(self):
        """「番組表を取得」ボタン: 選択中のステーションの番組表を取得してキャッシュ更新し表示"""
        station = self.schedule_station_var.get()
        self.schedule_mode_var.set('upcoming')
        self._set_app_busy(True)
        try:
            programs, success = self.fetch_and_cache_schedule(station)
        finally:
            self._set_app_busy(False)
        self.display_schedule(programs)
        self._scroll_schedule_to_now()
        if not success:
            messagebox.showwarning(
                "番組表の取得",
                f"{station} の番組表の取得に失敗したため、サンプルデータを表示しています。"
            )

    def _minutes_from_day_start(self, hhmm, is_end=False):
        """"HH:MM" を、その放送日の基準時刻 5:00 からの経過分に変換

        radikoの放送日は 5:00 始まり・翌 5:00 終わりのため、0-4時台は
        24時以降（例: 00:15 は 24:15 相当）として扱う。
        """
        try:
            hour, minute = map(int, hhmm.split(":"))
        except (ValueError, AttributeError):
            return 0
        if is_end and hour == 5 and minute == 0:
            return 24 * 60
        return ((hour - 5) % 24) * 60 + minute

    def _wrap_lines(self, text, tk_font, max_width, max_lines):
        """textを幅max_width・最大max_lines行に折り返す。
        収まりきらない場合は最終行の末尾を「…」に置き換える。

        結果は(フォント, 文字サイズ設定, 幅, 最大行数, テキスト)単位でキャッシュする。
        同じ局を再表示したりテーマ・タブを切り替えたりする際、番組の並びは
        変わらないのに毎回全番組分を測り直すと描画が遅くなるため。
        """
        if not text or max_lines <= 0:
            return []

        cache_key = (id(tk_font), self._schedule_font_size_key, max_width, max_lines, text)
        cached = self._wrap_cache.get(cache_key)
        if cached is not None:
            return cached

        lines = self._wrap_lines_uncached(text, tk_font, max_width, max_lines)
        # 番組表は日々入れ替わるため、長期間起動しっぱなしだと際限なく増える。
        # 上限に達したら丸ごと捨てる（直後の再描画1回分だけ測り直しになる）
        if len(self._wrap_cache) >= self.WRAP_CACHE_MAX_ENTRIES:
            self._wrap_cache.clear()
        self._wrap_cache[cache_key] = lines
        return lines

    def _wrap_lines_uncached(self, text, tk_font, max_width, max_lines):
        lines = []
        remaining = text
        for _ in range(max_lines):
            if not remaining:
                break
            if tk_font.measure(remaining) <= max_width:
                n = len(remaining)
            else:
                # 何文字までなら幅に収まるかを二分探索で求める。measure()は
                # Tclへの往復が発生し重いため、1文字ずつ測る線形探索だと
                # 番組数が多い時に番組表の描画が目に見えて遅くなる
                lo, hi = 1, len(remaining)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if tk_font.measure(remaining[:mid]) <= max_width:
                        lo = mid
                    else:
                        hi = mid - 1
                n = max(lo, 1)
            lines.append(remaining[:n])
            remaining = remaining[n:]

        if remaining:
            ellipsis = "…"
            last = lines[-1] if lines else ""
            if last and tk_font.measure(last + ellipsis) > max_width:
                lo, hi = 0, len(last)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if tk_font.measure(last[:mid] + ellipsis) <= max_width:
                        lo = mid
                    else:
                        hi = mid - 1
                last = last[:lo]
            lines[-1] = (last + ellipsis) if last else ellipsis

        return lines

    def _extract_url(self, program):
        """概要（desc）・詳細情報（info）から番組ホームページなどのURLを1件抜き出す"""
        for field in ('desc', 'info'):
            text = program.get(field) or ''
            match = _URL_PATTERN.search(text)
            if match:
                return match.group(0).rstrip('.,)、。」』')
        return None

    def display_schedule(self, programs, mode='upcoming'):
        """番組表データを、縦軸=時刻のグリッド（ラテ欄風）で表示

        Args:
            mode (str): 'upcoming'（通常の番組表、当日以降のみ表示）または
                'timefree'（過去7日間モード、当日を含む過去分のみ表示し、
                全番組を放送済みスタイルで表示、ダブルクリックでタイムフリー取得）
        """
        self._current_programs = programs
        colors = self._get_schedule_colors()
        canvas = self.schedule_canvas
        canvas.delete("all")
        for widget in self.day_header_frame.winfo_children():
            widget.destroy()
        self._program_canvas_items = {}
        self._schedule_item_programs = {}
        self._schedule_item_urls = {}
        self._schedule_display_mode = mode
        # 前回の描画に対する聴き逃しの配信確認が残っていても、結果を反映させない
        self._radiru_check_generation += 1

        today_iso = datetime.now().strftime("%Y-%m-%d")
        if mode == 'timefree':
            # 過去7日間モード: 今日を含む過去7日分のみ表示する
            cutoff_iso = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            programs = [
                p for p in programs
                if p.get('date_iso') and cutoff_iso <= p['date_iso'] <= today_iso
            ]
        else:
            # 当日より前の日は表示しない（キャッシュが古い場合に過去の日付が残るのを防ぐ）
            # date_iso を持たない古いキャッシュデータはそのまま表示する
            programs = [
                p for p in programs
                if p.get('date_iso') is None or p['date_iso'] >= today_iso
            ]

        # 日付ごとにグループ化
        days = {}
        for program in programs:
            days.setdefault(program['date'], []).append(program)

        # 列は日付の古い順（左が過去、右が現在/未来）に並べる。通常モードは
        # 取得順が既にその順（今日→未来）だが、タイムフリーは取得順が
        # 「今日→過去」のため、date_isoで明示的に並べ替えないと左右が逆になる
        days = dict(sorted(days.items(), key=lambda kv: kv[1][0].get('date_iso') or ''))

        num_cols = max(len(days), 1)
        grid_height = int(24 * 60 * self.PIXELS_PER_MINUTE)
        grid_width = self.TIME_LABEL_WIDTH + num_cols * self.DAY_COLUMN_WIDTH
        canvas.configure(scrollregion=(0, 0, grid_width, grid_height))
        self._schedule_grid_height = grid_height

        # 時刻軸（1時間ごとの目盛り、5:00始まり）
        for hour_offset in range(25):
            y = int(hour_offset * 60 * self.PIXELS_PER_MINUTE)
            label_hour = (5 + hour_offset) % 24
            canvas.create_line(self.TIME_LABEL_WIDTH, y, grid_width, y, fill=colors['hour_line'])
            canvas.create_text(
                4, y + 2, text=f"{label_hour:02d}:00",
                anchor=tk.NW, font=("Yu Gothic UI", self._schedule_hour_label_size),
                fill=colors['time_label']
            )

        # 日付ヘッダー側に時刻ラベル分の幅を合わせるスペーサー
        spacer = tk.Frame(self.day_header_frame, width=self.TIME_LABEL_WIDTH)
        spacer.pack(side=tk.LEFT, fill=tk.Y)
        spacer.pack_propagate(False)

        if not programs:
            # 番組が1件もない状態（未取得・取得を見送った・キャッシュが空、など）を
            # 目盛りだけの空グリッドのまま放置すると、壊れているように見えてしまうため
            # 案内メッセージを出す。スクロールしなくても必ず見える位置（先頭付近）に置く
            message = (
                "この局のタイムフリー番組表がありません。\n局を選び直すか表示モードを切り替えると取得を確認します"
                if mode == 'timefree' else
                "番組表がありません。「番組表を一括更新」または局を選び直すと取得を確認します"
            )
            canvas.create_text(
                self.TIME_LABEL_WIDTH + 20, 80,
                text=message, anchor=tk.NW, justify=tk.LEFT,
                font=("Yu Gothic UI", 10), fill=colors['desc']
            )

        now = datetime.now()
        now_minutes = self._minutes_from_day_start(now.strftime("%H:%M"))

        station = self.schedule_station_var.get()
        # NHK局のタイムフリー表示中のみ、番組ごとにNHK聴き逃しインデックスとの
        # 突き合わせを行う（radikoは他局と同じくタイムフリーで取得できるため
        # 対象外）。インデックスは番組の数だけ読み直さないよう、ここで1回だけ
        # 読み込んでおく
        show_radiru_availability = mode == 'timefree' and self._is_nhk_station(station)
        radiru_index = self.radiru_manager.load_index() if show_radiru_availability else None
        radiru_broadcast_code = self._nhk_radio_broadcast_code(station) if show_radiru_availability else None
        station_reservations = [
            res for res in self.manager.load_reservations()
            if res.get('enabled', True) and res.get('station') == station
        ]
        # 番組名がインデックスに載っている枠（rect, インデックスのエントリ, 放送枠）。
        # 描画後にバックグラウンドで回ごとの配信有無を確かめる
        radiru_checks = []

        for col, (date_label, day_programs) in enumerate(days.items()):
            col_left = self.TIME_LABEL_WIDTH + col * self.DAY_COLUMN_WIDTH
            canvas.create_line(col_left, 0, col_left, grid_height, fill=colors['day_line'])

            header_cell = tk.Frame(self.day_header_frame, width=self.DAY_COLUMN_WIDTH)
            header_cell.pack(side=tk.LEFT, fill=tk.Y)
            header_cell.pack_propagate(False)
            ttk.Label(
                header_cell, text=date_label,
                font=("Yu Gothic UI", self._schedule_date_header_size, "bold"),
                anchor=tk.W, padding=(4, 0)
            ).pack(expand=True, fill=tk.BOTH)

            # 当日の列かどうか（当日の列内のみ、終了済み番組をグレーアウトする）
            is_today_col = bool(day_programs) and day_programs[0].get('date_iso') == today_iso

            for program in day_programs:
                start_minutes = self._minutes_from_day_start(program['start'])
                end_minutes = self._minutes_from_day_start(program['end'], is_end=True)
                y1 = start_minutes * self.PIXELS_PER_MINUTE
                y2 = end_minutes * self.PIXELS_PER_MINUTE
                if y2 <= y1:
                    y2 = y1 + 2

                x1 = col_left + 2
                x2 = col_left + self.DAY_COLUMN_WIDTH - 2

                # タイムフリーモードでも、当日分は放送前の番組が含まれ得る。
                # 放送前の番組はまだradiko側にタイムフリー音声が存在せず、
                # 取得しようとすると失敗するのではなく現在ライブ中の別番組の
                # 内容が誤って取得されることを実機で確認したため、区別して
                # 見た目を変え、ダウンロードもできないようにする
                not_yet_aired = False
                if mode == 'timefree':
                    start_dt, _ = self._program_air_window(program)
                    not_yet_aired = start_dt is not None and now < start_dt

                is_past = mode == 'timefree' or (is_today_col and end_minutes <= now_minutes)
                if not_yet_aired:
                    fill_color = colors['unavailable_fill']
                    outline_color = colors['unavailable_outline']
                elif is_past:
                    fill_color = colors['past_fill']
                    outline_color = colors['past_outline']
                else:
                    fill_color = colors['future_fill']
                    outline_color = colors['future_outline']

                # 現在予約が入っている番組は枠を目立たせて一目で分かるようにする
                # （タイムフリー表示中や、放送が終わった番組には付けない）
                is_reserved = mode != 'timefree' and not is_past and any(
                    self._program_matches_reservation(station, program, res)
                    for res in station_reservations
                )
                outline_width = 2 if is_reserved else 1
                if is_reserved:
                    outline_color = colors['reserved_outline']

                # NHK局のタイムフリー表示: NHK聴き逃しインデックスに番組名が
                # 登録済みなら「ダウンロードできる見込みが高い」、未登録なら
                # 「非対応か、まだインデックスに載っていない」として枠線で区別する。
                # 番組名が載っていても、再放送の枠（聴き逃しは本放送のみ）や
                # 配信期限切れの回は取得できないため、描画後にバックグラウンドで
                # 番組ごとのエピソード一覧を取得して回ごとに確かめ直す
                # （_start_radiru_episode_check）
                if show_radiru_availability and not not_yet_aired:
                    series_entry = self.radiru_manager.find_series_for_program(
                        radiru_broadcast_code, program.get('title') or '', program.get('corner_name'),
                        index=radiru_index,
                    )
                    available = series_entry is not None
                    outline_color = (
                        colors['radiru_available_outline'] if available
                        else colors['radiru_unavailable_outline']
                    )
                    outline_width = 2 if available else 1

                rect = canvas.create_rectangle(
                    x1, y1, x2, y2, fill=fill_color, outline=outline_color, width=outline_width
                )
                items = [rect]

                if show_radiru_availability and not not_yet_aired and series_entry is not None:
                    air_start, air_end = self._program_air_window(program)
                    if air_start is not None:
                        radiru_checks.append((rect, series_entry, air_start, air_end))

                lookup_key = (program.get('date_iso'), program.get('start'), program.get('title'))
                self._program_canvas_items[lookup_key] = rect

                program_url = self._extract_url(program)
                title_font = self.schedule_title_link_font if program_url else self.schedule_title_font
                title_fill = colors['title_link'] if program_url else colors['title']

                box_width = x2 - x1 - 6
                box_height = y2 - y1 - 4
                title_line_height = title_font.metrics("linespace")
                title_max_lines = max(1, int(box_height // title_line_height))
                title_text = f"{program['start']} {program['title']}"
                title_lines = self._wrap_lines(
                    title_text, title_font, box_width, title_max_lines
                )

                # タイトルの下に余白があれば、そこに概要（desc）も表示する
                used_height = len(title_lines) * title_line_height
                remaining_height = box_height - used_height - 2
                desc_line_height = self.schedule_desc_font.metrics("linespace")
                desc_max_lines = int(remaining_height // desc_line_height)
                desc_lines = self._wrap_lines(
                    program.get('desc', ''), self.schedule_desc_font, box_width, desc_max_lines
                )

                text_y = y1 + 2
                if title_lines:
                    title_item = canvas.create_text(
                        x1 + 3, text_y,
                        text="\n".join(title_lines),
                        font=title_font,
                        justify=tk.LEFT,
                        anchor=tk.NW,
                        fill=title_fill
                    )
                    items.append(title_item)
                    if program_url:
                        canvas.addtag_withtag(self.SCHEDULE_LINK_TAG, title_item)
                        self._schedule_item_urls[title_item] = program_url
                    text_y += used_height + 2

                if desc_lines:
                    desc_item = canvas.create_text(
                        x1 + 3, text_y,
                        text="\n".join(desc_lines),
                        font=self.schedule_desc_font,
                        justify=tk.LEFT,
                        anchor=tk.NW,
                        fill=colors['desc']
                    )
                    items.append(desc_item)

                for item in items:
                    canvas.addtag_withtag(self.SCHEDULE_PROGRAM_TAG, item)
                    self._schedule_item_programs[item] = program

        right_edge = self.TIME_LABEL_WIDTH + num_cols * self.DAY_COLUMN_WIDTH
        canvas.create_line(right_edge, 0, right_edge, grid_height, fill=colors['day_line'])

        self._start_radiru_episode_check(radiru_checks)

    # NHK聴き逃しのエピソード一覧を使い回す時間（秒）。局の切替や表示モードの
    # 切替のたびに同じ番組を問い合わせ直さないため
    RADIRU_EPISODE_CACHE_SECONDS = 600

    def _start_radiru_episode_check(self, checks):
        """番組表の描画後、NHK聴き逃しに番組名が載っている枠について、その回が
        実際に配信中かをバックグラウンドで確かめる

        番組名単位の判定だけでは、再放送の枠（「マイ・フェイバリット・アルバム」の
        午後6時など、聴き逃しは本放送のみ）や配信期限切れの回まで「取得できる」
        表示になってしまうため。問い合わせは枠ごとではなく番組（シリーズ）ごとに
        1回だけ行う。
        """
        if not checks:
            return
        generation = self._radiru_check_generation
        threading.Thread(
            target=self._radiru_episode_check_worker, args=(generation, checks), daemon=True
        ).start()

    def _fetch_radiru_episodes_cached(self, series_site_id, corner_site_id):
        """番組のエピソード一覧を、一定時間キャッシュしながら取得する"""
        cache_key = (series_site_id, corner_site_id)
        cached = self._radiru_episode_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < self.RADIRU_EPISODE_CACHE_SECONDS:
            return cached[1]
        episodes = self.radiru_manager.fetch_series_episodes(series_site_id, corner_site_id)
        self._radiru_episode_cache[cache_key] = (time.monotonic(), episodes)
        return episodes

    def _radiru_episode_check_worker(self, generation, checks):
        """_start_radiru_episode_check の本体（バックグラウンドスレッド）"""
        unavailable_rects = []
        episodes_by_series = {}
        for rect, entry, air_start, air_end in checks:
            if generation != self._radiru_check_generation:
                return
            series_key = (entry['series_site_id'], entry['corner_site_id'])
            if series_key not in episodes_by_series:
                try:
                    episodes_by_series[series_key] = self._fetch_radiru_episodes_cached(*series_key)
                except Exception:
                    # 確かめられなかった番組は番組名単位の表示のままにしておく
                    logger.warning(f"NHK聴き逃しのエピソード一覧取得に失敗しました ({entry.get('title')})",
                                   exc_info=True)
                    episodes_by_series[series_key] = None
            episodes = episodes_by_series[series_key]
            if episodes is None:
                continue
            if self.radiru_manager.find_episode_for_date(episodes, air_start, air_end) is None:
                unavailable_rects.append(rect)
        self.root.after(0, self._apply_radiru_episode_check, generation, unavailable_rects)

    def _apply_radiru_episode_check(self, generation, unavailable_rects):
        """回ごとの配信確認の結果を番組表に反映する（メインスレッドから呼ばれる）"""
        if generation != self._radiru_check_generation:
            return
        outline = self._get_schedule_colors()['radiru_unavailable_outline']
        for rect in unavailable_rects:
            self.schedule_canvas.itemconfig(rect, outline=outline, width=1)

    def _schedule_item_under_pointer(self):
        """番組表キャンバス上で、いまマウスポインタの下にあるアイテムIDを返す（無ければNone）"""
        current = self.schedule_canvas.find_withtag("current")
        return current[0] if current else None

    def _on_schedule_item_enter(self, event):
        program = self._schedule_item_programs.get(self._schedule_item_under_pointer())
        if program is not None:
            self._show_tooltip(event, program)

    def _on_schedule_item_double_click(self, event):
        program = self._schedule_item_programs.get(self._schedule_item_under_pointer())
        if program is None:
            return
        if self._schedule_display_mode == 'timefree':
            self._open_timefree_download_dialog(program)
        else:
            self._open_reservation_dialog_from_program(program)

    def _on_schedule_link_click(self, event):
        url = self._schedule_item_urls.get(self._schedule_item_under_pointer())
        if url:
            webbrowser.open(url)

    def _get_program_image(self, url):
        """番組画像をPhotoImageとして取得する（メモリ内キャッシュ＋マネージャー側のディスクキャッシュを利用）"""
        if not url:
            return None
        if url in self._image_cache:
            return self._image_cache[url]

        photo = None
        data = self.manager.get_image(url)
        if data:
            try:
                image = Image.open(io.BytesIO(data))
                image.thumbnail((100, 100))
                photo = ImageTk.PhotoImage(image)
            except Exception:
                logger.exception(f"Error decoding image {url}")
                photo = None

        # 長期間起動しっぱなしで際限なく増えないよう、古いものから捨てる
        # （表示中のツールチップはラベル側が参照を保持しているので消えない）
        while len(self._image_cache) >= self.IMAGE_CACHE_MAX_ENTRIES:
            del self._image_cache[next(iter(self._image_cache))]
        self._image_cache[url] = photo
        return photo

    def _show_tooltip(self, event, program):
        """番組ブロックにマウスを乗せた時に詳細（画像・時刻・タイトル・概要）をポップアップ表示"""
        self._hide_tooltip()
        self._tooltip = tk.Toplevel(self.root)
        self._tooltip.wm_overrideredirect(True)
        self._tooltip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 12}")

        frame = tk.Frame(
            self._tooltip, background="#ffffe0", relief=tk.SOLID, borderwidth=1
        )
        frame.pack()

        photo = self._get_program_image(program.get('img'))
        if photo:
            image_label = tk.Label(frame, image=photo, background="#ffffe0")
            image_label.image = photo  # ガベージコレクション防止のため参照を保持
            image_label.pack(padx=4, pady=(4, 0))

        text_lines = [f"{program['start']}-{program['end']}  {program['title']}"]
        if program.get('desc'):
            text_lines.append(program['desc'])

        ttk.Label(
            frame,
            text="\n".join(text_lines),
            background="#ffffe0",
            padding=4,
            font=("Yu Gothic UI", 9),
            wraplength=260,
            justify=tk.LEFT
        ).pack()

    def _hide_tooltip(self):
        if self._tooltip is not None:
            self._tooltip.destroy()
            self._tooltip = None
