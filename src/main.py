#!/usr/bin/env python3
"""
RecR - ラジコ録音アプリケーション
radiko.jp の放送を Windows で録音するデスクトップアプリケーション
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from tkinter import font as tkfont
from datetime import datetime, timedelta
from pathlib import Path
import calendar
import ctypes
import hashlib
import io
import json
import logging
import math
import os
import re
import sys
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor

# Windows の電源管理API（SetThreadExecutionState）用フラグ。
# 予約録音を控えている間・録音中は、アイドルによる自動スリープを抑止するために使う。
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

from PIL import Image, ImageDraw, ImageTk
import pystray
import requests
import sv_ttk

# 概要・詳細情報に含まれるURLを検出する正規表現
_URL_PATTERN = re.compile(r'https?://[^\s"\'<>]+')

# 親ディレクトリをパスに追加してインポート
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.app_logger import setup_logging
from utils.paths import get_base_dir
from utils.radiko_manager import RadikoManager, keyword_matches
from utils.radiru_manager import RadiruManager
from utils import reservation_logic

setup_logging()
logger = logging.getLogger(__name__)


class RecRApp:
    # 番組表グリッドのレイアウト定数（縦軸=時刻、横軸=日付）
    PIXELS_PER_MINUTE = 2.0
    DAY_COLUMN_WIDTH = 130
    TIME_LABEL_WIDTH = 55
    SCHEDULE_PROGRAM_TAG = "program"
    SCHEDULE_LINK_TAG = "program_link"
    DAY_HEADER_HEIGHT = 28

    # 番組表の文字サイズ設定（小/中/大）。「小」を基準に、文字サイズ設定に応じて
    # 2ポイントずつ引き上げる。縦方向の1分あたりピクセル数（PIXELS_PER_MINUTE）も
    # タイトルの文字サイズに比例して拡大し、大きい文字でも番組枠に収まりやすくする
    SCHEDULE_FONT_SIZE_DELTA = {'small': 0, 'medium': 2, 'large': 4}
    SCHEDULE_BASE_TITLE_SIZE = 8
    SCHEDULE_BASE_DESC_SIZE = 7
    SCHEDULE_BASE_HOUR_LABEL_SIZE = 8
    SCHEDULE_BASE_DATE_HEADER_SIZE = 9
    SCHEDULE_BASE_PIXELS_PER_MINUTE = 2.0

    # 番組表の取得日数、および自動更新のしきい値
    SCHEDULE_FETCH_DAYS = 10
    SCHEDULE_STALE_THRESHOLD_DAYS = 4

    # 複数局の番組表を一括取得する際、同時に取得する局数（並列ワーカー数）。
    # radiko側の負荷を考えて控えめな値にしている。
    SCHEDULE_STATION_FETCH_WORKERS = 4

    # 全局番組表の1日1回自動更新を行う時刻（"HH:MM"形式。この時刻を過ぎた状態で
    # アプリが起動していれば、その日のうちに実行される）。フリーワード予約が
    # 新着番組を取りこぼさないよう、アプリを起動しっぱなしにしていても定期的に更新する
    FULL_SCHEDULE_REFRESH_TIME = "05:00"
    # 上記時刻の取りこぼしを防ぐための定期チェック間隔（ミリ秒）
    FULL_SCHEDULE_REFRESH_CHECK_INTERVAL_MS = 10 * 60 * 1000

    # 検索結果クリック時のハイライト表示時間
    HIGHLIGHT_DURATION_MS = 8000

    # メモリ内キャッシュの上限件数（長期間起動しっぱなしでも際限なく増えないようにする）。
    # 折り返し結果は全局・両表示モードを一巡して約4000件（数MB）、
    # 番組画像(100x100のPhotoImage)は1枚あたり約33KB
    WRAP_CACHE_MAX_ENTRIES = 8000
    IMAGE_CACHE_MAX_ENTRIES = 200

    # 「録音」タブ右側のお知らせ欄に表示するMarkdownファイルの取得元。
    # NOTICE.mdをmasterブランチにpushすると、次回起動時（または「更新」ボタン）で反映される
    NOTICE_URL = "https://raw.githubusercontent.com/recr848-ui/RecR/master/NOTICE.md"

    def __init__(self, root):
        self.root = root
        self.root.title("RecR - ラジコ録音アプリ")
        # 番組表を横スクロールなしで10日分表示できるよう幅広めに確保
        self.root.geometry("1450x800")

        self.manager = RadikoManager()
        self.radiru_manager = RadiruManager()
        stations = self.manager.get_stations()
        settings = self.manager.load_settings()

        self.theme_var = tk.StringVar(value=settings.get('theme', 'light'))
        sv_ttk.set_theme(self.theme_var.get())
        self._current_programs = []

        self.eq_mode_var = tk.StringVar(value=settings.get('eq_mode', 'spectrum'))
        self.prevent_sleep_var = tk.BooleanVar(value=settings.get('prevent_sleep', True))
        self.filename_pattern_var = tk.StringVar(
            value=settings.get('recording_filename_pattern', self.manager.DEFAULT_FILENAME_PATTERN)
        )

        saved_default_station = settings.get('default_station')
        if saved_default_station in stations:
            default_station = saved_default_station
        else:
            default_station = "NHK-FM" if "NHK-FM" in stations else (stations[0] if stations else "")
        self.default_station_var = tk.StringVar(value=default_station)
        # 「再生中」「録音タブでの手動録音対象」「番組表タブで閲覧中」は、それぞれ別の局を
        # 同時に扱えるよう独立した変数にする（連動させると、何を聞いていて何を録音していて
        # 何の番組表を見ているか分からなくなるため）
        self.playback_station_var = tk.StringVar(value=default_station)
        self.recording_station_var = tk.StringVar(value=default_station)
        self.schedule_station_var = tk.StringVar(value=default_station)
        self.schedule_mode_var = tk.StringVar(value='upcoming')
        self.schedule_font_size_var = tk.StringVar(
            value=settings.get('schedule_font_size', 'small')
        )

        self.SCHEDULE_FETCH_DAYS = settings.get('schedule_fetch_days', self.SCHEDULE_FETCH_DAYS)
        self.SCHEDULE_STALE_THRESHOLD_DAYS = settings.get(
            'schedule_stale_threshold_days', self.SCHEDULE_STALE_THRESHOLD_DAYS
        )
        self.FULL_SCHEDULE_REFRESH_TIME = settings.get(
            'full_schedule_refresh_time', self.FULL_SCHEDULE_REFRESH_TIME
        )
        self.manager.max_concurrent_timefree_downloads = settings.get(
            'max_concurrent_timefree_downloads', self.manager.max_concurrent_timefree_downloads
        )
        self.manager.recording_margin_seconds = settings.get(
            'recording_margin_seconds', self.manager.recording_margin_seconds
        )
        self.recording_margin_var = tk.IntVar(value=self.manager.recording_margin_seconds)

        self._busy_saved_states = {}
        self._reservation_check_job = None
        self._reservation_list_refresh_job = None
        self._program_guide_refresh_job = None
        self._full_refresh_check_job = None
        self._full_refresh_in_progress = False
        self._radiru_refresh_check_job = None
        self._radiru_refresh_in_progress = False
        # 局名 -> {'title', 'start_dt', 'end_dt'}。手動・予約を問わず、現在進行中の
        # 全ての録音を右上のパネルに表示するための情報
        self._active_recordings = {}
        # "局名|ft" -> {'station', 'title', 'done', 'total'}。進行中のタイムフリー
        # ダウンロードを右上のパネルに進捗付きで表示するための情報
        self._active_downloads = {}
        # ダウンロードの進捗ラベルを、パネル全体を再構築せず直接書き換えるための参照
        self._download_progress_labels = {}
        self._tray_icon = None
        self._tray_hint_shown = settings.get('tray_hint_shown', False)
        self._sleep_prevented = False
        # (予約ID, 対象日) -> {'attempts': 連続して再開に失敗した回数, 'had_data': これまでに
        # 一部でも録音できたか}。開始失敗・途中中断した予約録音を番組終了まで再開し続ける
        # ための状態（_schedule_reservation_resume）
        self._reservation_resume_state = {}
        self.setup_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close_button)
        self._refresh_stale_stations()
        self.load_schedule_for_current_station()
        self._schedule_reservation_check()
        self._schedule_reservation_list_minute_refresh()
        self._schedule_program_guide_minute_refresh()
        self._check_full_schedule_refresh_due()
        self._check_radiru_index_refresh_due()
        self.root.after(1500, self._notify_missed_reservations_on_startup)
        self._refresh_notice()

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
        self.manager.stop_playback()
        self.manager.stop_recording()
        self.manager.stop_all_downloads()
        self.root.destroy()

    def setup_ui(self):
        """ユーザーインターフェースをセットアップ"""
        self.setup_top_bar(self.root)

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True)

        record_tab = ttk.Frame(notebook)
        schedule_tab = ttk.Frame(notebook)
        reservation_tab = ttk.Frame(notebook)
        freeword_tab = ttk.Frame(notebook)
        files_tab = ttk.Frame(notebook)
        notebook.add(record_tab, text="録音")
        notebook.add(schedule_tab, text="番組表")
        notebook.add(reservation_tab, text="予約録音")
        notebook.add(freeword_tab, text="フリーワード")
        notebook.add(files_tab, text="保存先フォルダ")

        self.setup_record_tab(record_tab)
        self.setup_schedule_tab(schedule_tab)
        self.setup_reservation_tab(reservation_tab)
        self.setup_freeword_tab(freeword_tab)
        self.setup_files_tab(files_tab)
        self.setup_menu()

    def setup_top_bar(self, parent):
        """タブ切り替えに関わらず常に表示される上部バー
        （ステーション選択・ライブ再生の開始/停止・イコライザー）をセットアップ
        """
        top_bar = ttk.Frame(parent, padding=(10, 8))
        top_bar.pack(side=tk.TOP, fill=tk.X)

        # 現在進行中の録音（手動・予約を問わず）を右端に一覧表示するパネル
        self.active_recordings_frame = ttk.Frame(top_bar)
        self.active_recordings_frame.pack(side=tk.RIGHT, anchor=tk.N, padx=(10, 0))

        ttk.Label(top_bar, text="ステーション：").pack(side=tk.LEFT)
        top_bar_station_combo = ttk.Combobox(
            top_bar,
            textvariable=self.playback_station_var,
            values=self.manager.get_stations(),
            state="readonly",
            width=14
        )
        top_bar_station_combo.pack(side=tk.LEFT, padx=(5, 15))
        self.top_bar_station_combo = top_bar_station_combo

        self.play_button = ttk.Button(
            top_bar,
            text="▶ 再生",
            command=self.start_playback
        )
        self.play_button.pack(side=tk.LEFT, padx=5)

        self.play_stop_button = ttk.Button(
            top_bar,
            text="■ 再生停止",
            command=self.stop_playback,
            state=tk.DISABLED
        )
        self.play_stop_button.pack(side=tk.LEFT, padx=5)

        # グラフィックイコライザー（LEDセグメント＋ピークホールド付きのVUメーター風表示）
        # 「マルチバンド（スペクトラム）」「ピークメーター（全体音量を1本で表示）」を
        # 設定＞イコライザー表示 から切り替え可能（_rebuild_eq_meterが実体を作り直す）
        self.EQ_WIDTH = 200
        self.EQ_HEIGHT = 40
        self.EQ_NUM_SEGMENTS = 10
        self.EQ_PEAK_HOLD_HEIGHT = 2
        self._eq_segment_gap = 1
        self._eq_band_gap = 3
        self._eq_off_color = "#262626"

        # 筐体風の黒縁でCanvasを囲み、機材然とした見た目にする
        eq_bezel = tk.Frame(top_bar, background="#000000", padx=3, pady=3)
        eq_bezel.pack(side=tk.LEFT, padx=(15, 0))
        self.eq_canvas = tk.Canvas(
            eq_bezel, width=self.EQ_WIDTH, height=self.EQ_HEIGHT,
            background="#141414", highlightthickness=0
        )
        self.eq_canvas.pack()

        self._eq_update_job = None
        self._rebuild_eq_meter()

    def _eq_segment_color(self, seg_index):
        # 上段ほど赤、中段は黄、下段は緑（ハードウェアVUメーター風の3色配色）
        ratio = (seg_index + 1) / self.EQ_NUM_SEGMENTS
        if ratio > 0.85:
            return "#ff4d4d"
        if ratio > 0.6:
            return "#f5c542"
        return "#3fdd6a"

    def _rebuild_eq_meter(self):
        """イコライザー表示モード（スペクトラム/ピークメーター）に応じてCanvasの中身を作り直す"""
        self.eq_canvas.delete("all")
        if self.eq_mode_var.get() == "peak":
            self._build_analog_peak_meter()
        else:
            self._build_led_spectrum()

    def _build_led_spectrum(self):
        """マルチバンドのLEDセグメント表示（スペクトラムアナライザー風）を構築"""
        self.eq_canvas.configure(width=self.EQ_WIDTH, height=self.EQ_HEIGHT)

        num_bands = self.manager.EQ_NUM_BANDS
        band_width = (self.EQ_WIDTH - (num_bands - 1) * self._eq_band_gap) / num_bands
        segment_height = (
            self.EQ_HEIGHT - (self.EQ_NUM_SEGMENTS - 1) * self._eq_segment_gap
        ) / self.EQ_NUM_SEGMENTS

        self._eq_segments = []
        self._eq_peak_rects = []
        self._eq_band_bounds = []
        for i in range(num_bands):
            x0 = i * (band_width + self._eq_band_gap)
            x1 = x0 + band_width
            self._eq_band_bounds.append((x0, x1))

            band_segments = []
            for s in range(self.EQ_NUM_SEGMENTS):
                seg_y1 = self.EQ_HEIGHT - s * (segment_height + self._eq_segment_gap)
                seg_y0 = seg_y1 - segment_height
                rect = self.eq_canvas.create_rectangle(
                    x0, seg_y0, x1, seg_y1, fill=self._eq_off_color, outline=""
                )
                band_segments.append((rect, self._eq_segment_color(s)))
            self._eq_segments.append(band_segments)

            peak_rect = self.eq_canvas.create_rectangle(
                x0, self.EQ_HEIGHT, x1, self.EQ_HEIGHT, fill="#ffffff", outline=""
            )
            self._eq_peak_rects.append(peak_rect)

        # VUメーター風に「素早く上昇・ゆっくり減衰」させて表示する現在値、
        # およびそれよりもゆっくり減衰して直近の最大値を示すピークホールド値
        self._eq_display_levels = [0.0] * num_bands
        self._eq_peak_levels = [0.0] * num_bands

    # アナログ針メーター（ピークメーター）のジオメトリ定数
    EQ_ANALOG_WIDTH = 200
    EQ_ANALOG_HEIGHT = 54
    EQ_ANALOG_RADIUS = 32
    EQ_ANALOG_ANGLE_MIN = -50  # 針の振れ角（垂直=0度、左がマイナス）
    EQ_ANALOG_ANGLE_MAX = 50

    def _analog_tk_angle(self, level):
        """0.0〜1.0のレベルをCanvas角度系（東=0度、反時計回りが正）の角度[度]に変換"""
        angle = self.EQ_ANALOG_ANGLE_MIN + level * (self.EQ_ANALOG_ANGLE_MAX - self.EQ_ANALOG_ANGLE_MIN)
        return 90 - angle

    def _build_analog_peak_meter(self):
        """アナログ針式のL/Rピークメーターを構築（ラックスマン等の角形VUメーター風）

        扇形（PIESLICE）ではなく、往年のオーディオアンプに見られる角形の窓に
        スケール弧を収めた見た目にする。スケール弧は針の可動域（angle_min〜max）
        と正確に一致させ、左右非対称な余白による「傾き」が出ないようにする。
        """
        self.eq_canvas.configure(width=self.EQ_ANALOG_WIDTH, height=self.EQ_ANALOG_HEIGHT)

        gauge_width = self.EQ_ANALOG_WIDTH / 2
        radius = self.EQ_ANALOG_RADIUS
        margin = 4

        scale_start = self._analog_tk_angle(1.0)
        scale_extent = self._analog_tk_angle(0.0) - scale_start

        self._eq_needles = []
        self._eq_pivots = []
        for ch, label in enumerate(("L", "R")):
            gx0 = gauge_width * ch
            rect_x0, rect_y0 = gx0 + margin, margin
            rect_x1, rect_y1 = gx0 + gauge_width - margin, self.EQ_ANALOG_HEIGHT - margin
            pivot_x = (rect_x0 + rect_x1) / 2
            pivot_y = rect_y1 - 6
            bbox = (pivot_x - radius, pivot_y - radius, pivot_x + radius, pivot_y + radius)

            # メーター面（乳白色の角窓。ラックスマン等のアンプに見られる意匠）
            self.eq_canvas.create_rectangle(
                rect_x0, rect_y0, rect_x1, rect_y1,
                fill="#f2ead9", outline="#1a1a1a", width=1
            )
            # スケール弧（針の可動域と完全に一致させ左右対称にする）
            self.eq_canvas.create_arc(
                *bbox, start=scale_start, extent=scale_extent,
                style=tk.ARC, outline="#4a4a4a", width=1
            )
            # レッドゾーン（レベル0.85〜1.0を縁取りで示す）
            red_start = self._analog_tk_angle(1.0)
            red_extent = self._analog_tk_angle(0.85) - red_start
            self.eq_canvas.create_arc(
                *bbox, start=red_start, extent=red_extent, style=tk.ARC,
                outline="#c1392b", width=3
            )
            # 目盛り
            for tick_level in (0.0, 0.25, 0.5, 0.75, 1.0):
                tick_angle = math.radians(self._analog_tk_angle(tick_level))
                x0 = pivot_x + (radius - 6) * math.cos(tick_angle)
                y0 = pivot_y - (radius - 6) * math.sin(tick_angle)
                x1 = pivot_x + radius * math.cos(tick_angle)
                y1 = pivot_y - radius * math.sin(tick_angle)
                self.eq_canvas.create_line(x0, y0, x1, y1, fill="#4a4a4a", width=1)

            # "VU" ロゴ風の文字とチャンネルラベル
            self.eq_canvas.create_text(
                pivot_x, rect_y0 + 9, text="VU",
                font=("Times New Roman", 8, "bold"), fill="#1a1a1a"
            )
            self.eq_canvas.create_text(
                rect_x0 + 8, rect_y1 - 7, text=label,
                font=("Yu Gothic UI", 7, "bold"), fill="#4a4a4a"
            )

            # 針（初期位置は0レベル）
            tick_angle = math.radians(self._analog_tk_angle(0.0))
            tip_x = pivot_x + radius * math.cos(tick_angle)
            tip_y = pivot_y - radius * math.sin(tick_angle)
            needle = self.eq_canvas.create_line(
                pivot_x, pivot_y, tip_x, tip_y, fill="#a02020", width=2
            )
            self.eq_canvas.create_oval(
                pivot_x - 3, pivot_y - 3, pivot_x + 3, pivot_y + 3,
                fill="#1a1a1a", outline=""
            )

            self._eq_needles.append(needle)
            self._eq_pivots.append((pivot_x, pivot_y))

        # アナログ針にはVUメーター同様「素早く上昇・ゆっくり減衰」の値のみを使い、
        # デジタル的なピークホールド表示は設けない（実機のアナログVUメーターに準拠）
        self._eq_display_levels = [0.0, 0.0]
        self._eq_peak_levels = [0.0, 0.0]

    def _on_eq_mode_changed(self):
        """イコライザー表示モード切り替え時: 設定を保存し、表示を作り直す"""
        self.manager.save_settings({'eq_mode': self.eq_mode_var.get()})
        self._rebuild_eq_meter()

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

        settings_menu.add_separator()
        settings_menu.add_command(label="設定をエクスポート...", command=self._export_settings)
        settings_menu.add_command(label="設定をインポート...", command=self._import_settings)

        menubar.add_cascade(label="設定", menu=settings_menu)

        self.root.config(menu=menubar)

        # tk.Menuはttkテーマの管理外のため、テーマ切り替え時に自前で配色し直せるよう
        # 全メニューを保持しておく（_apply_menu_theme参照）
        self._menus = [
            menubar, file_menu, view_menu, eq_mode_menu, schedule_menu,
            settings_menu, quality_menu, filename_menu, default_station_menu, margin_menu,
        ]
        self._apply_menu_theme()

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

    def _open_schedule_fetch_progress_dialog(self, title):
        """番組表取得中に表示する進捗ダイアログを開く

        「キャンセル」ボタンを押すと戻り値のイベントがセットされ、以降まだ
        着手していない局・日の取得が順次打ち切られる（実行中のリクエストは
        完了を待つため、キャンセル後も少しの間だけ処理が続くことがある）。

        Returns:
            (tk.Toplevel, tk.StringVar, ttk.Progressbar, threading.Event)
        """
        cancel_event = threading.Event()
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)

        progress_var = tk.StringVar(value="準備中...")
        ttk.Label(frame, textvariable=progress_var, justify=tk.LEFT, width=42).pack(
            anchor=tk.W, pady=(0, 12)
        )

        progressbar = ttk.Progressbar(frame, mode='determinate', length=300)
        progressbar.pack(fill=tk.X, pady=(0, 15))

        def on_cancel():
            cancel_event.set()
            cancel_button.configure(state='disabled', text="キャンセル中...")

        cancel_button = ttk.Button(frame, text="キャンセル", command=on_cancel)
        cancel_button.pack()

        dialog.protocol("WM_DELETE_WINDOW", on_cancel)

        self._center_dialog_over_parent(dialog)
        dialog.deiconify()
        dialog.grab_set()

        return dialog, progress_var, progressbar, cancel_event

    def _update_schedule_fetch_progress(self, progress_var, progressbar, completed, total, station):
        """進捗ダイアログの表示を更新する（メインスレッドから呼ばれる）"""
        progress_var.set(f"{completed}/{total}局  {station} 完了")
        progressbar.configure(value=completed)

    def _fetch_and_cache_schedule_no_ui(self, station, cancel_event):
        """番組表とタイムフリー番組表を取得してキャッシュに保存する（バックグラウンドスレッド用）

        fetch_and_cache_schedule と異なり、Tkinterウィジェットには一切触れない
        （フリーワード一致の有無だけを返し、予約一覧UIへの反映は呼び出し元が
        メインスレッドでまとめて行う）。

        Returns:
            (bool success, bool any_created, str or None error_label)
        """
        try:
            programs = self.manager.get_program_schedule(
                station, days=self.SCHEDULE_FETCH_DAYS, cancel_event=cancel_event
            )
            success = bool(programs)
            if not programs:
                programs = self.manager.get_sample_schedule(station, days=self.SCHEDULE_FETCH_DAYS)
            self.manager.save_schedule_cache(station, programs)
            any_created = success and self.manager.scan_freewords_for_station(station, programs)
        except Exception as e:
            return False, False, f"{station}（{e}）"

        if not cancel_event.is_set():
            try:
                timefree_programs = self.manager.get_timefree_schedule(station, cancel_event=cancel_event)
                if timefree_programs:
                    self.manager.save_schedule_cache(f"{station}::timefree", timefree_programs)
            except Exception:
                logger.exception(f"タイムフリー番組表の更新に失敗しました: {station}")

        return success, any_created, (None if success else station)

    def _bulk_refresh_schedules(self):
        """全ステーションの番組表を一括で再取得してキャッシュを更新する

        局ごとに独立したHTTPリクエストであるため、複数局を同時に並列取得する
        ことで所要時間を短縮する（詳細は SCHEDULE_STATION_FETCH_WORKERS）。
        """
        if not messagebox.askyesno(
            "番組表の一括更新",
            "すべてのステーションの番組表を再取得します。時間がかかる場合があります。よろしいですか？"
        ):
            return

        stations = self.manager.get_stations()
        dialog, progress_var, progressbar, cancel_event = self._open_schedule_fetch_progress_dialog(
            "番組表の一括更新"
        )
        progressbar.configure(maximum=len(stations))
        progress_var.set(f"0/{len(stations)}局 取得中...")

        def worker():
            failed_stations = []
            any_created = False
            completed = 0
            lock = threading.Lock()

            def fetch_one(station):
                nonlocal completed, any_created
                if cancel_event.is_set():
                    return
                success, created, error_label = self._fetch_and_cache_schedule_no_ui(station, cancel_event)
                with lock:
                    if not success:
                        failed_stations.append(error_label)
                    if created:
                        any_created = True
                    completed += 1
                    n = completed
                self.root.after(
                    0, self._update_schedule_fetch_progress, progress_var, progressbar, n, len(stations), station
                )

            with ThreadPoolExecutor(max_workers=self.SCHEDULE_STATION_FETCH_WORKERS) as executor:
                list(executor.map(fetch_one, stations))

            self.root.after(0, self._on_bulk_refresh_done, dialog, cancel_event.is_set(), failed_stations, any_created)

        threading.Thread(target=worker, daemon=True).start()

    def _on_bulk_refresh_done(self, dialog, cancelled, failed_stations, any_created):
        """番組表一括更新の完了処理（メインスレッドで実行）"""
        dialog.grab_release()
        dialog.destroy()

        if any_created:
            self._refresh_reservation_list()
        self.load_schedule_for_current_station()

        if cancelled:
            messagebox.showinfo("番組表の一括更新", "取得をキャンセルしました。")
        elif failed_stations:
            messagebox.showwarning(
                "番組表の一括更新",
                "以下の局は番組表の取得に失敗しました（サンプルデータで代用しています）:\n\n"
                + "\n".join(failed_stations)
            )
        else:
            messagebox.showinfo("番組表の一括更新", "すべてのステーションの番組表を更新しました。")

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

    def _program_actual_date_iso(self, date_iso, start_hhmm):
        """番組の date_iso（放送日、5:00始まり）と start（HH:MM）から、
        実際のカレンダー上の日付を求める（0-4時台の番組は翌カレンダー日になるため）

        ロジック本体は utils.reservation_logic（GUI非依存、単体テスト対象）に切り出してある。
        """
        return reservation_logic.program_actual_date_iso(date_iso, start_hhmm)

    def _program_air_window(self, program):
        """番組の実際の放送開始・終了datetimeを求める（不正なデータならNone, None）"""
        return reservation_logic.program_air_window(program)

    def _program_matches_reservation(self, station, program, reservation):
        """番組表上のある番組が、指定の予約と同じ回かどうかを判定する"""
        return reservation_logic.program_matches_reservation(station, program, reservation)

    def _find_now_airing_program(self, station):
        """指定局のキャッシュ済み番組表から、現在放送中の番組情報を探す（無ければNone）

        録音タブの手動録音や予約録音の実行時にはあらかじめ決まった番組情報が無いため、
        ファイル名や埋め込みメタデータに番組情報を使う場合に備えて、ベストエフォートで補完する。
        """
        cached = self.manager.load_cached_schedule(station)
        if not cached:
            return None
        now = datetime.now()
        for program in cached:
            start_dt, end_dt = self._program_air_window(program)
            if start_dt and start_dt <= now < end_dt:
                return program
        return None

    def _find_now_airing_title(self, station):
        """指定局のキャッシュ済み番組表から、現在放送中の番組名を探す（無ければNone）"""
        program = self._find_now_airing_program(station)
        return (program.get('title') or None) if program else None

    def _find_program_for_reservation(self, station, reservation):
        """指定局のキャッシュ済み番組表から、指定の予約に対応する回の番組情報を探す（無ければNone）

        予約録音の実行時（_check_due_reservations）専用。前後マージン設定により
        録音開始が番組本来の開始時刻より早まっている場合、_find_now_airing_program
        （現在時刻基準）では録音開始時点にまだ放送中の「前の番組」を誤って拾って
        しまうため、必ず予約自体の対象回（局・開始時刻・放送日/曜日が一致する回）
        を番組表から探して使う。
        """
        cached = self.manager.load_cached_schedule(station)
        if not cached:
            return None
        for program in cached:
            if self._program_matches_reservation(station, program, reservation):
                return program
        return None

    def _build_recording_metadata(self, station, program=None, fallback_title=None):
        """録音ファイルに埋め込むメタデータ（番組名・出演者・局名・概要・放送日）を組み立てる

        program（番組表の1件分の辞書）が渡されればそこから出演者・概要・放送日も
        補完し、無ければ番組名（fallback_title）と局名のみのメタデータになる。
        """
        metadata = {'album': station}
        title = (program.get('title') if program else None) or fallback_title
        if title:
            metadata['title'] = title
        if program:
            if program.get('pfm'):
                metadata['artist'] = program['pfm']
            if program.get('desc'):
                metadata['comment'] = program['desc']
            if program.get('img'):
                metadata['image_url'] = program['img']
            date_iso = program.get('date_iso')
            start = program.get('start')
            if date_iso and start:
                metadata['date'] = self._program_actual_date_iso(date_iso, start)
        return metadata

    def _open_reservation_dialog_from_program(self, program):
        """番組表で番組をダブルクリックした時の処理

        現在放送中の番組であれば、予約ではなく「今すぐ録音するか」を確認するダイアログを出す。
        すでに終了した過去の番組（当日内で放送済みのもの）は、タイムフリーの取得確認ダイアログを開く。
        それ以外（未来の番組）は、その番組の内容を入力済みの状態で新規予約録音ダイアログを開く。
        """
        now = datetime.now()
        start_dt, end_dt = self._program_air_window(program)
        if start_dt and start_dt <= now < end_dt:
            self._confirm_immediate_recording(program, end_dt)
            return

        if end_dt and end_dt <= now:
            self._open_timefree_download_dialog(program)
            return

        date_iso = program.get('date_iso') or datetime.now().strftime("%Y-%m-%d")
        start = program.get('start') or '00:00'
        end = program.get('end') or '01:00'
        prefill = {
            'station': self.schedule_station_var.get(),
            'repeat': 'once',
            'date_iso': self._program_actual_date_iso(date_iso, start),
            'start': start,
            'end': end,
            'title': program.get('title', ''),
        }
        self._open_reservation_dialog(prefill=prefill)

    def _confirm_immediate_recording(self, program, end_dt):
        """現在放送中の番組をダブルクリックした時: 確認の上、残り時間分をその場で録音開始する

        録音タブのStart/Stopボタンは常に1局分しか状態を保持できないため、ここでは
        使わず、局ごとに独立して追跡する_watch_background_recordingに任せる。
        こうすることで、複数局を続けて「今すぐ録音」しても互いを上書きしない。
        """
        station = self.schedule_station_var.get()
        title = program.get('title') or station
        if not messagebox.askyesno(
            "録音", f"「{title}」は現在放送中です。今すぐ録音を開始しますか？"
        ):
            return
        if self.manager.is_recording_active(station):
            messagebox.showerror("エラー", f"{station} は既に録音中です")
            return

        duration_minutes = max(1, math.ceil((end_dt - datetime.now()).total_seconds() / 60))
        file_format = self.format_var.get()
        try:
            mp3_bitrate = int(self.bitrate_var.get())
        except ValueError:
            mp3_bitrate = 192

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
            self._register_active_recording(station, title, duration_minutes)
            self._watch_background_recording(station)
        else:
            messagebox.showerror(
                "録音エラー",
                f"{station} の録音を開始できませんでした。\n通信状況をご確認ください。"
            )

    def _open_timefree_download_dialog(self, program, station=None, on_success=None):
        """過去の番組をダブルクリックした時: 確認の上、タイムフリーでダウンロードする

        radikoのタイムフリーは放送から概ね1週間で聴取期限が切れるため、
        それを過ぎている番組はダウンロードできない旨を伝えて何もしない。

        station を指定しない場合は番組表タブで選択中の局を対象にする
        （予約一覧からは、予約自身の局を明示的に渡す）。
        on_success（callable または None）は、データを1バイト以上取得できて
        エラーなく完了した場合にのみ呼ばれる（予約一覧からの呼び出しで、
        対象予約を「DL済」にする用途）。
        """
        station = station or self.schedule_station_var.get()
        title = program.get('title') or station
        start_dt, end_dt = self._program_air_window(program)
        if not start_dt or not end_dt:
            messagebox.showinfo("タイムフリー", "この番組の時刻情報を取得できませんでした。")
            return

        now = datetime.now()
        if now < start_dt:
            # 放送前の番組はまだradiko側にタイムフリー音声が存在しない。取得を
            # 試みると失敗するのではなく、現在ライブ中の別番組の内容が誤って
            # 取得されてしまうことを実機で確認したため、事前にブロックする
            messagebox.showinfo(
                "タイムフリー",
                f"「{title}」はまだ放送されていないため、タイムフリーで取得できません。\n"
                "放送終了後に改めてお試しください。"
            )
            return

        # NHKはradikoではライブ配信のみでタイムフリーを一切提供しておらず
        # （取得自体は成功するが、内容が「配信しておりません」という案内音声に
        # なる）、代わりにNHK独自の聴き逃し配信（らじる★らじる）を使う
        if self._is_nhk_station(station):
            self._open_nhk_radiru_download_dialog(
                program, station, title, start_dt, end_dt, on_success
            )
            return

        if now - end_dt > timedelta(days=7):
            messagebox.showinfo(
                "タイムフリー",
                f"「{title}」はタイムフリーの聴取期限（放送から約1週間）を過ぎているため、"
                "取得できません。"
            )
            return

        if self.manager.is_download_limit_reached():
            messagebox.showinfo(
                "タイムフリー",
                "タイムフリーの同時ダウンロード数が上限"
                f"（{self.manager.max_concurrent_timefree_downloads}件）に達しています。\n"
                "他のダウンロードが終わってから、もう一度お試しください。\n"
                "上限は「設定」メニューから変更できます。"
            )
            return

        if not messagebox.askyesno(
            "タイムフリー", f"「{title}」をタイムフリーでダウンロードしますか？"
        ):
            return

        ft = start_dt.strftime("%Y%m%d%H%M%S")
        to = end_dt.strftime("%Y%m%d%H%M%S")
        if self.manager.is_download_active(station, ft):
            messagebox.showerror("エラー", f"{station} のこの番組は既に取得中です")
            return

        file_format = self.format_var.get()
        try:
            mp3_bitrate = int(self.bitrate_var.get())
        except ValueError:
            mp3_bitrate = 192

        key = f"{station}|{ft}"
        self._register_active_download(key, station, ft, title)

        self._set_app_busy(True)
        try:
            success, output_path = self.manager.start_timefree_download(
                station, ft, to, file_format=file_format, mp3_bitrate=mp3_bitrate,
                title=title, filename_pattern=self.filename_pattern_var.get(),
                metadata=self._build_recording_metadata(station, program=program, fallback_title=title),
                on_progress=lambda done, total, k=key: self.root.after(
                    0, self._update_active_download_progress, k, done, total
                ),
                on_complete=lambda had_data, path, error, k=key, st=station, t=title: self.root.after(
                    0, self._on_timefree_download_complete, k, st, t, had_data, path, error, on_success
                )
            )
        finally:
            self._set_app_busy(False)

        if not success:
            self._unregister_active_download(key)
            messagebox.showerror(
                "タイムフリー",
                f"{station} の「{title}」を取得できませんでした。\n通信状況をご確認ください。"
            )

    def _on_timefree_download_complete(self, key, station, title, had_data, output_path, error,
                                        on_success=None):
        """タイムフリーのダウンロード終了時（メインスレッドから呼ばれる）:
        データを1バイトも取得できなかった場合に警告する

        on_success は、データを取得できてエラーもなかった場合にのみ呼ぶ
        （途中で通信エラーが起きた場合は「完了できた」とは見なさない）。
        """
        self._unregister_active_download(key)
        if hasattr(self, 'files_tree'):
            self._refresh_files_list()
        if not had_data:
            detail = f"\n（{error}）" if error else ""
            message = (
                f"{station} の「{title}」のタイムフリー取得でデータを取得できませんでした"
                f"（ファイルが空です）。\n通信状況をご確認ください。{detail}"
            )
            if self._tray_icon is not None:
                self._notify_via_tray("タイムフリー", message)
            else:
                messagebox.showwarning("タイムフリー", message)
        elif error:
            message = (
                f"{station} の「{title}」のタイムフリー取得は途中で通信エラーが発生し、中断されました。\n"
                f"それまでの内容は保存されています。\n"
                f"保存先: {output_path}\n（{error}）"
            )
            if self._tray_icon is not None:
                self._notify_via_tray("タイムフリー", message)
            else:
                messagebox.showwarning("タイムフリー", message)
        else:
            if self._tray_icon is not None:
                self._notify_via_tray("タイムフリー", f"{station} の「{title}」の取得が完了しました。")
            if on_success is not None:
                on_success()

    @staticmethod
    def _is_nhk_station(station):
        """局名がNHK（NHK AM/NHK FM等、地域名付き表記を含む）かどうかを判定する"""
        return station.startswith("NHK")

    @staticmethod
    def _nhk_radio_broadcast_code(station):
        """局名からNHK聴き逃し側の放送区分コード（"FM" または "R1"）を求める"""
        return "FM" if "FM" in station else "R1"

    def _open_nhk_radiru_download_dialog(self, program, station, title, start_dt, end_dt, on_success):
        """NHKの番組をダブルクリックした時: らじる★らじるの聴き逃し配信から取得する

        radikoのタイムフリーはNHKに一切対応していない（ライブのみ）ため、NHKの
        番組はこちらのルートに誘導する。聴き逃し側は番組名からの検索APIを
        持たないため、定期的に蓄積しているローカルインデックス
        （radiru_series_index.json）から番組名で引き当てる。インデックスに
        無い場合や、その番組がそもそもNHKの聴き逃し対象外の場合は、その旨を
        伝えて終了する。
        """
        radio_broadcast = self._nhk_radio_broadcast_code(station)
        corner_name = program.get('corner_name') if isinstance(program, dict) else None

        self._set_app_busy(True)
        try:
            entry = self.radiru_manager.find_series_for_program(radio_broadcast, title, corner_name)
            if entry is None:
                messagebox.showinfo(
                    "NHK聴き逃し",
                    f"「{title}」はNHK聴き逃し配信のインデックスにまだ登録されていないか、"
                    "聴き逃し非対応の番組です。\n"
                    "インデックスは定期的に自動更新されるため、時間をおいて再度お試しください。"
                )
                return

            try:
                episodes = self.radiru_manager.fetch_series_episodes(
                    entry['series_site_id'], entry['corner_site_id']
                )
            except Exception:
                logger.exception(f"NHK聴き逃しのエピソード一覧取得に失敗しました ({title})")
                messagebox.showerror(
                    "NHK聴き逃し", f"「{title}」のエピソード情報を取得できませんでした。\n通信状況をご確認ください。"
                )
                return

            episode = self.radiru_manager.find_episode_for_date(episodes, start_dt, end_dt)
            if episode is None:
                messagebox.showinfo(
                    "NHK聴き逃し",
                    f"「{title}」の{start_dt.strftime('%m/%d')}放送分は、NHK聴き逃し配信の"
                    "対象外か、既に配信期限（放送から約1週間）を過ぎています。"
                )
                return

            stream_url = episode.get('stream_url')
            if not stream_url:
                messagebox.showinfo("NHK聴き逃し", f"「{title}」の配信データを取得できませんでした。")
                return

            if not messagebox.askyesno(
                "NHK聴き逃し", f"「{title}」（{start_dt.strftime('%m/%d')}放送）をNHK聴き逃し配信から"
                "ダウンロードしますか？"
            ):
                return

            filename = self.manager.build_recording_filename(
                self.filename_pattern_var.get(), station, title, start_dt, "m4a"
            )
            output_path = self.manager.unique_output_path(self.manager.output_dir / filename)
            metadata = self._build_recording_metadata(station, program=program, fallback_title=title)

            ft = start_dt.strftime("%Y%m%d%H%M%S")
            key = f"{station}|radiru|{ft}"
            self._register_active_download(key, station, ft, title)

            thread = threading.Thread(
                target=self._nhk_radiru_download_worker,
                args=(stream_url, output_path, metadata, key, station, title, on_success),
                daemon=True,
            )
            thread.start()
        finally:
            self._set_app_busy(False)

    def _nhk_radiru_download_worker(self, stream_url, output_path, metadata, key, station, title,
                                     on_success):
        """NHK聴き逃しのダウンロード本体（バックグラウンドスレッド）"""
        error = None
        try:
            self.radiru_manager.download_episode(
                stream_url, output_path,
                on_progress=lambda done, total, k=key: self.root.after(
                    0, self._update_active_download_progress, k, done, total
                ),
            )
            self.manager.write_metadata_tags(output_path, "m4a", metadata)
        except Exception as e:
            logger.exception(f"NHK聴き逃しのダウンロードに失敗しました ({title})")
            error = str(e)
        self.root.after(
            0, self._on_nhk_radiru_download_complete, key, station, title, output_path, error, on_success
        )

    def _on_nhk_radiru_download_complete(self, key, station, title, output_path, error, on_success):
        """NHK聴き逃しのダウンロード終了時（メインスレッドから呼ばれる）"""
        self._unregister_active_download(key)
        if hasattr(self, 'files_tree'):
            self._refresh_files_list()

        # 途中で失敗した場合も冒頭だけのファイルが残るため、ファイルの有無
        # だけで成功扱いにせず、エラーがあれば失敗として知らせる
        had_data = output_path.exists() and output_path.stat().st_size > 0
        if error or not had_data:
            detail = f"\n（{error}）" if error else ""
            message = (
                f"{station} の「{title}」のNHK聴き逃し取得に失敗しました。\n"
                f"通信状況をご確認ください。{detail}"
            )
            if self._tray_icon is not None:
                self._notify_via_tray("NHK聴き逃し", message)
            else:
                messagebox.showwarning("NHK聴き逃し", message)
        else:
            if self._tray_icon is not None:
                self._notify_via_tray("NHK聴き逃し", f"{station} の「{title}」の取得が完了しました。")
            if on_success is not None:
                on_success()

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

    def setup_reservation_tab(self, parent):
        """「予約録音」タブのUIをセットアップ（予約の一覧・追加・編集・削除）"""
        control_frame = ttk.Frame(parent, padding=10)
        control_frame.pack(fill=tk.X)

        ttk.Button(
            control_frame, text="新規予約...", command=self._open_reservation_dialog
        ).pack(side=tk.LEFT)
        ttk.Button(
            control_frame, text="編集...", command=self._edit_selected_reservation
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="有効にする",
            command=lambda: self._set_selected_reservations_enabled(True)
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="無効にする",
            command=lambda: self._set_selected_reservations_enabled(False)
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="削除", command=self._delete_selected_reservation
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="終了した予約を削除", command=self._delete_finished_reservations
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="録音を停止", command=self._stop_selected_reservation_recording
        ).pack(side=tk.LEFT, padx=5)

        list_frame = ttk.Frame(parent, padding=(10, 0, 10, 10))
        list_frame.pack(fill=tk.BOTH, expand=True)

        # 日付と時刻は1つの列にまとめる（日付で並べ替えたとき、同じ日の中も時刻順に並ぶように）
        columns = ("enabled", "station", "schedule", "title", "source", "status", "download")
        headings = {
            "enabled": "有効", "station": "局", "schedule": "日時/繰り返し",
            "title": "番組名", "source": "由来", "status": "状態",
            "download": "タイムフリー"
        }
        widths = {
            "enabled": 36, "station": 90, "schedule": 210,
            "title": 240, "source": 120, "status": 100, "download": 90
        }
        # Ctrl/Shiftクリックで複数選択し、まとめて有効化・無効化・削除できるようにする
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="extended")
        for col in columns:
            tree.heading(col, text=headings[col], command=lambda c=col: self._sort_treeview_column(tree, c, False))
            tree.column(col, width=widths[col], anchor=tk.W)

        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.LEFT, fill=tk.Y)
        tree.bind("<Double-1>", lambda e: self._edit_selected_reservation())
        tree.bind("<Button-1>", self._on_reservation_tree_click)

        self.reservation_tree = tree
        self._reservation_row_map = {}
        self._refresh_reservation_list()

    def _refresh_reservation_list(self):
        """予約録音の一覧表示をマネージャー側の最新データで作り直す"""
        tree = self.reservation_tree
        view = self._capture_treeview_view(tree, self._reservation_row_map)
        tree.delete(*tree.get_children())
        self._reservation_row_map = {}

        for res in self.manager.load_reservations():
            if res.get('repeat') == 'weekly':
                weekday = res.get('weekday', 0)
                schedule_text = f"毎週{self.manager.WEEKDAY_JA[weekday]}曜日"
            else:
                schedule_text = res.get('date_iso', '')
            schedule_text = f"{schedule_text} {res.get('start', '')}-{res.get('end', '')}"

            if self.manager.is_recording_active(res.get('station'), reservation_id=res.get('id')):
                status_text = "録音中"
            elif res.get('timefree_downloaded'):
                status_text = "DL済"
            elif res.get('last_result') == 'success':
                status_text = "成功"
            elif res.get('last_result') == 'partial':
                status_text = "中断（一部のみ）"
            elif res.get('last_result') == 'failed':
                status_text = "失敗"
            elif res.get('repeat') != 'weekly' and self._reservation_is_overdue_pending(res):
                # 放送時刻を過ぎたのに一度も実行されていない（その時刻にアプリが動いて
                # いなかった等）。毎週の予約は次回を待っている状態でもあるので対象外
                status_text = "未実行"
            else:
                status_text = "待機中"

            if res.get('source') == 'freeword':
                keyword = self.manager.get_freeword(res.get('keyword_id'))
                source_text = f"F: {keyword['keyword']}" if keyword else "フリーワード"
            else:
                source_text = "手動"

            download_text = "⬇ ダウンロード" if self._reservation_needs_timefree_recovery(res) else ""

            item_id = tree.insert(
                "", tk.END,
                values=(
                    "○" if res.get('enabled', True) else "×",
                    res.get('station', ''),
                    schedule_text,
                    res.get('title', ''),
                    source_text,
                    status_text,
                    download_text
                )
            )
            self._reservation_row_map[item_id] = res.get('id')
        self._restore_treeview_view(tree, self._reservation_row_map, view)

        # 予約の追加・変更・削除を、表示中の番組表の予約枠（囲み色）にも反映する
        if hasattr(self, 'schedule_canvas') and self._current_programs:
            self.display_schedule(self._current_programs, mode=self.schedule_mode_var.get())

    def _reservation_occurrence_program_dict(self, reservation):
        """予約の直近の回（1回のみならその日、毎週なら直近のその曜日の日）を、
        _program_air_window 等が扱える「番組」風の辞書にして返す（計算できなければNone）
        """
        return reservation_logic.reservation_occurrence_program_dict(reservation)

    def _reservation_is_overdue_pending(self, reservation):
        """予約が「待機中」のまま、録音終了予定時刻を過ぎてしまっているか
        （＝実行されるはずだったのに実行されなかった予約）を判定する
        """
        return reservation_logic.reservation_is_overdue_pending(
            reservation, self.manager.is_recording_active
        )

    def _reservation_needs_timefree_recovery(self, reservation):
        """この予約をタイムフリーで取り直す価値があるか（失敗・中断・実行し損ねて
        終了時刻を過ぎた待機中、のいずれか。既にタイムフリーで取得済みなら対象外）"""
        return reservation_logic.reservation_needs_timefree_recovery(
            reservation, self.manager.is_recording_active
        )

    def _notify_missed_reservations_on_startup(self):
        """起動時: 取り逃した予約（失敗・中断・実行され損ねた待機中）があれば通知する

        予約録音タブの「タイムフリー」列からいつでも後追いでダウンロードできるが、
        気づかず取りこぼしたままになるのを防ぐため、起動のたびに一度だけ知らせる。
        """
        missed = [
            r for r in self.manager.load_reservations()
            if self._reservation_needs_timefree_recovery(r)
        ]
        if not missed:
            return
        message = (
            f"取り逃した予約が{len(missed)}件あります。\n"
            "予約録音タブの「タイムフリー」列からダウンロードできます。"
        )
        if self._tray_icon is not None:
            self._notify_via_tray("予約録音", message)
        else:
            messagebox.showinfo("予約録音", message)

    def _on_reservation_tree_click(self, event):
        """予約一覧のクリック処理: 「タイムフリー」列のクリックのみ、対象予約の
        タイムフリー取得ダイアログを開く（それ以外は通常の行選択に任せる）
        """
        tree = self.reservation_tree
        if tree.identify_region(event.x, event.y) != "cell":
            return
        columns = tree["columns"]
        try:
            col_index = int(tree.identify_column(event.x).replace("#", "")) - 1
        except ValueError:
            return
        if not (0 <= col_index < len(columns)) or columns[col_index] != "download":
            return
        row_id = tree.identify_row(event.y)
        if not row_id:
            return
        reservation = self.manager.get_reservation(self._reservation_row_map.get(row_id))
        if not reservation or not self._reservation_needs_timefree_recovery(reservation):
            return
        self._download_reservation_via_timefree(reservation)

    def _download_reservation_via_timefree(self, reservation):
        """予約一覧の「タイムフリー」列から、指定予約の直近の回をタイムフリーで取得する

        取得に成功したら、この予約を「DL済」としてマークし、一覧のボタンを消す。
        """
        program = self._reservation_occurrence_program_dict(reservation)
        if program is None:
            messagebox.showinfo("タイムフリー", "この予約の日時情報を取得できませんでした。")
            return
        reservation_id = reservation['id']
        self._open_timefree_download_dialog(
            program, station=reservation.get('station'),
            on_success=lambda rid=reservation_id: self._mark_reservation_timefree_downloaded(rid)
        )

    def _mark_reservation_timefree_downloaded(self, reservation_id):
        """予約をタイムフリーで取得し終えたことを記録し、一覧表示を更新する"""
        self.manager.update_reservation(reservation_id, {'timefree_downloaded': True})
        self._refresh_reservation_list()

    def _get_selected_reservations(self):
        """予約一覧で選択中の行に対応する予約データを全て取得する（未選択なら空リスト）"""
        selection = self.reservation_tree.selection()
        if not selection:
            messagebox.showinfo("予約録音", "予約を選択してください。")
            return []
        reservations = []
        for item_id in selection:
            reservation = self.manager.get_reservation(self._reservation_row_map.get(item_id))
            if reservation:
                reservations.append(reservation)
        return reservations

    def _edit_selected_reservation(self):
        reservations = self._get_selected_reservations()
        if not reservations:
            return
        if len(reservations) > 1:
            messagebox.showinfo("予約録音", "編集は1件ずつ選択して行ってください。")
            return
        self._open_reservation_dialog(existing=reservations[0])

    def _delete_selected_reservation(self):
        reservations = self._get_selected_reservations()
        if not reservations:
            return
        if len(reservations) == 1:
            message = f"「{reservations[0].get('title') or reservations[0].get('station')}」の予約を削除しますか？"
        else:
            message = f"選択した{len(reservations)}件の予約を削除しますか？"
        if not messagebox.askyesno("予約の削除", message):
            return
        for reservation in reservations:
            self.manager.delete_reservation(reservation['id'])
        self._refresh_reservation_list()

    def _set_selected_reservations_enabled(self, enabled):
        """選択中の予約すべてを有効/無効に一括変更する"""
        reservations = self._get_selected_reservations()
        if not reservations:
            return
        for reservation in reservations:
            self.manager.update_reservation(reservation['id'], {'enabled': enabled})
        self._refresh_reservation_list()

    def _delete_finished_reservations(self):
        """「終了した予約を削除」ボタン: 放送が終わった単発予約をまとめて削除する

        毎週予約は繰り返し使われ続けるため対象外。まだ一度も実行されていない
        単発予約や、現在録音中のものも「終了」ではないため対象外とする。

        タイムフリーで取得済み（一覧で「DL済」表示）の予約は、録音自体は
        失敗していて last_result が 'failed'/'partial' のままでも実質的には
        解決済みのため、「失敗」側には含めず、確認なしで通常の削除対象に含める。
        録音が一度も実行されないまま（last_run_date が未設定のまま）タイムフリーで
        直接取得したケースもあるため、その場合も timefree_downloaded だけで「終了」と
        みなす（last_run_date の有無は問わない）。
        """
        finished = [
            r for r in self.manager.load_reservations()
            if r.get('repeat') != 'weekly'
            and (r.get('last_run_date') or r.get('timefree_downloaded'))
            and not self.manager.is_recording_active(r.get('station'))
        ]
        if not finished:
            messagebox.showinfo("予約の削除", "削除対象の終了した予約はありません。")
            return

        failed = [
            r for r in finished
            if r.get('last_result') == 'failed' and not r.get('timefree_downloaded')
        ]
        include_failed = self._confirm_delete_finished_reservations(len(finished), len(failed))
        if include_failed is None:
            return

        targets = finished if include_failed else [r for r in finished if r not in failed]
        if not targets:
            return
        for reservation in targets:
            self.manager.delete_reservation(reservation['id'])
        self._refresh_reservation_list()
        messagebox.showinfo("予約の削除", f"終了した予約を{len(targets)}件削除しました。")

    def _confirm_delete_finished_reservations(self, finished_count, failed_count):
        """「終了した予約を削除」の確認ダイアログ。失敗した予約も含めて削除するかを
        チェックボックスで選べる（応答するまで処理をブロックする）。

        Returns:
            bool または None: 削除を実行するなら True/False（失敗分を含めるか）、
            キャンセルされた場合は None
        """
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("終了した予約の削除")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame, text=f"終了した予約が{finished_count}件あります。まとめて削除しますか？"
        ).pack(anchor=tk.W)

        include_failed_var = tk.BooleanVar(value=False)
        if failed_count:
            ttk.Checkbutton(
                frame, text=f"失敗した予約（{failed_count}件）も削除する",
                variable=include_failed_var
            ).pack(anchor=tk.W, pady=(8, 0))

        result = {'value': None}

        def on_delete():
            result['value'] = include_failed_var.get()
            dialog.destroy()

        def on_cancel():
            result['value'] = None
            dialog.destroy()

        button_frame = ttk.Frame(frame)
        button_frame.pack(pady=(15, 0))
        ttk.Button(button_frame, text="削除", command=on_delete).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="キャンセル", command=on_cancel).pack(side=tk.LEFT, padx=5)

        dialog.protocol("WM_DELETE_WINDOW", on_cancel)
        self._center_dialog_over_parent(dialog)
        dialog.deiconify()
        dialog.wait_window()
        return result['value']

    def setup_freeword_tab(self, parent):
        """「フリーワード」タブのUIをセットアップ（キーワードの一覧・追加・編集・削除）

        登録したキーワードは、番組表の取得・更新のたびに対象局の番組と自動的に
        照合され、一致した未来の番組が「予約録音」タブへ1回のみの予約として
        自動登録される。
        """
        control_frame = ttk.Frame(parent, padding=10)
        control_frame.pack(fill=tk.X)

        ttk.Button(
            control_frame, text="新規キーワード...", command=self._open_freeword_dialog
        ).pack(side=tk.LEFT)
        ttk.Button(
            control_frame, text="編集...", command=self._edit_selected_freeword
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="有効にする",
            command=lambda: self._set_selected_freewords_enabled(True)
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="無効にする",
            command=lambda: self._set_selected_freewords_enabled(False)
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            control_frame, text="削除", command=self._delete_selected_freeword
        ).pack(side=tk.LEFT, padx=5)

        ttk.Label(
            parent,
            text="登録したキーワードは、番組表の取得・更新のたびに対象局と照合され、"
                 "一致した未来の番組が「予約録音」タブへ自動登録されます。",
            foreground="gray30", padding=(10, 0)
        ).pack(fill=tk.X)

        list_frame = ttk.Frame(parent, padding=10)
        list_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("enabled", "keyword", "stations")
        headings = {"enabled": "有効", "keyword": "キーワード", "stations": "対象局"}
        widths = {"enabled": 50, "keyword": 200, "stations": 500}
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="extended")
        for col in columns:
            tree.heading(col, text=headings[col], command=lambda c=col: self._sort_treeview_column(tree, c, False))
            tree.column(col, width=widths[col], anchor=tk.W)

        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.LEFT, fill=tk.Y)
        tree.bind("<Double-1>", lambda e: self._edit_selected_freeword())

        self.freeword_tree = tree
        self._freeword_row_map = {}
        self._refresh_freeword_list()

    def _refresh_freeword_list(self):
        """フリーワードの一覧表示をマネージャー側の最新データで作り直す"""
        tree = self.freeword_tree
        view = self._capture_treeview_view(tree, self._freeword_row_map)
        tree.delete(*tree.get_children())
        self._freeword_row_map = {}

        for fw in self.manager.load_freewords():
            item_id = tree.insert(
                "", tk.END,
                values=(
                    "○" if fw.get('enabled', True) else "×",
                    fw.get('keyword', ''),
                    "、".join(fw.get('stations', [])),
                )
            )
            self._freeword_row_map[item_id] = fw.get('id')
        self._restore_treeview_view(tree, self._freeword_row_map, view)

    def _get_selected_freewords(self):
        """フリーワード一覧で選択中の行に対応するデータを全て取得する（未選択なら空リスト）"""
        selection = self.freeword_tree.selection()
        if not selection:
            messagebox.showinfo("フリーワード", "キーワードを選択してください。")
            return []
        freewords = []
        for item_id in selection:
            freeword = self.manager.get_freeword(self._freeword_row_map.get(item_id))
            if freeword:
                freewords.append(freeword)
        return freewords

    def _edit_selected_freeword(self):
        freewords = self._get_selected_freewords()
        if not freewords:
            return
        if len(freewords) > 1:
            messagebox.showinfo("フリーワード", "編集は1件ずつ選択して行ってください。")
            return
        self._open_freeword_dialog(existing=freewords[0])

    def _delete_selected_freeword(self):
        freewords = self._get_selected_freewords()
        if not freewords:
            return
        if len(freewords) == 1:
            message = f"キーワード「{freewords[0].get('keyword')}」を削除しますか？\n" \
                      "（既に自動作成された予約は削除されません）"
        else:
            message = f"選択した{len(freewords)}件のキーワードを削除しますか？\n" \
                      "（既に自動作成された予約は削除されません）"
        if not messagebox.askyesno("キーワードの削除", message):
            return
        for freeword in freewords:
            self.manager.delete_freeword(freeword['id'])
        self._refresh_freeword_list()

    def _set_selected_freewords_enabled(self, enabled):
        """選択中のキーワードすべてを有効/無効に一括変更する"""
        freewords = self._get_selected_freewords()
        if not freewords:
            return
        for freeword in freewords:
            self.manager.update_freeword(freeword['id'], {'enabled': enabled})
        self._refresh_freeword_list()
        if enabled:
            self._rescan_freewords_against_cache(freewords)

    def _rescan_freewords_against_cache(self, freewords):
        """指定したフリーワードの対象局について、キャッシュ済み番組表と即座に
        照合し直す（次回の番組表取得を待たずに反映するため）。一致した番組があれば
        予約録音タブへ自動登録し、一覧を更新する。
        """
        stations = {s for fw in freewords for s in fw.get('stations', [])}
        created_total = []
        for station in stations:
            cached = self.manager.load_cached_schedule(station)
            if cached:
                created_total.extend(self.manager.scan_freewords_for_station(station, cached))
        if created_total:
            self._refresh_reservation_list()

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

        columns = ("name", "modified", "size")
        headings = {"name": "ファイル名", "modified": "更新日時", "size": "サイズ"}
        widths = {"name": 400, "modified": 150, "size": 100}
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="extended")
        for col in columns:
            tree.heading(col, text=headings[col], command=lambda c=col: self._sort_treeview_column(tree, c, False))
            tree.column(col, width=widths[col], anchor=tk.W)

        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.LEFT, fill=tk.Y)
        tree.bind("<Double-1>", lambda e: self._play_selected_file())

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

    def _open_freeword_dialog(self, existing=None):
        """フリーワードの新規追加・編集ダイアログを開く"""
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("キーワードの編集" if existing else "新規フリーワード")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(0, minsize=110)
        frame.columnconfigure(1, weight=1)

        data = existing or {}
        row = 0

        ttk.Label(frame, text="キーワード：").grid(row=row, column=0, sticky=tk.W, pady=4)
        keyword_var = tk.StringVar(value=data.get('keyword', ''))
        ttk.Entry(frame, textvariable=keyword_var, width=30).grid(
            row=row, column=1, sticky=tk.EW, pady=4
        )
        row += 1

        ttk.Label(
            frame, text="番組名・概要・出演者のいずれかに部分一致した番組を自動予約します。",
            foreground="gray30", wraplength=280
        ).grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))
        row += 1

        stations_frame = ttk.LabelFrame(frame, text="対象局", padding=5)
        stations_frame.grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=4)
        row += 1

        button_row = ttk.Frame(stations_frame)
        button_row.pack(fill=tk.X, anchor=tk.W)
        station_vars = {}
        ttk.Button(
            button_row, text="全選択",
            command=lambda: [v.set(True) for v in station_vars.values()]
        ).pack(side=tk.LEFT)
        ttk.Button(
            button_row, text="全解除",
            command=lambda: [v.set(False) for v in station_vars.values()]
        ).pack(side=tk.LEFT, padx=5)

        checkbox_grid = ttk.Frame(stations_frame)
        checkbox_grid.pack(fill=tk.X, pady=(5, 0))

        selected_stations = set(data.get('stations', []))
        columns = 5
        for i, station in enumerate(self.manager.get_stations()):
            var = tk.BooleanVar(value=(station in selected_stations) if existing else True)
            station_vars[station] = var
            ttk.Checkbutton(
                checkbox_grid, text=station, variable=var
            ).grid(row=i // columns, column=i % columns, sticky=tk.W, padx=5, pady=2)

        preview_frame = ttk.LabelFrame(frame, text="対象番組の確認（保存前プレビュー）", padding=5)
        preview_frame.grid(row=row, column=0, columnspan=2, sticky=tk.NSEW, pady=4)
        frame.rowconfigure(row, weight=1)
        row += 1

        preview_top = ttk.Frame(preview_frame)
        preview_top.pack(fill=tk.X)
        ttk.Button(
            preview_top, text="対象番組を確認", command=lambda: run_preview()
        ).pack(side=tk.LEFT)
        preview_status_var = tk.StringVar(value="キーワードと対象局を指定して「対象番組を確認」を押してください")
        ttk.Label(preview_top, textvariable=preview_status_var, foreground="gray30").pack(
            side=tk.LEFT, padx=(10, 0)
        )

        preview_columns = ("station", "date", "time", "title", "pfm")
        preview_tree = ttk.Treeview(
            preview_frame, columns=preview_columns, show="headings", height=6
        )
        preview_tree.heading("station", text="局")
        preview_tree.heading("date", text="日付")
        preview_tree.heading("time", text="時刻")
        preview_tree.heading("title", text="番組名")
        preview_tree.heading("pfm", text="出演者")
        preview_tree.column("station", width=80, anchor=tk.W)
        preview_tree.column("date", width=80, anchor=tk.W)
        preview_tree.column("time", width=100, anchor=tk.W)
        preview_tree.column("title", width=260, anchor=tk.W)
        preview_tree.column("pfm", width=160, anchor=tk.W)
        preview_scroll = ttk.Scrollbar(
            preview_frame, orient=tk.VERTICAL, command=preview_tree.yview
        )
        preview_tree.configure(yscrollcommand=preview_scroll.set)
        preview_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=(5, 0))
        preview_scroll.pack(side=tk.LEFT, fill=tk.Y, pady=(5, 0))

        def run_preview():
            preview_tree.delete(*preview_tree.get_children())
            keyword = keyword_var.get().strip()
            stations = [s for s, v in station_vars.items() if v.get()]
            if not keyword or not stations:
                preview_status_var.set("キーワードと対象局を指定して「対象番組を確認」を押してください")
                return

            matches = self.manager.search_cached_programs_by_keyword(keyword, stations)
            for station, program in matches:
                preview_tree.insert(
                    "", tk.END,
                    values=(
                        station,
                        program.get('date', ''),
                        f"{program.get('start', '')}-{program.get('end', '')}",
                        program.get('title', ''),
                        program.get('pfm', '')
                    )
                )
            preview_status_var.set(
                f"{len(matches)}件ヒット（今後放送予定のもののみ。番組表未取得の局は対象外）"
            )

        def on_save():
            keyword = keyword_var.get().strip()
            if not keyword:
                messagebox.showerror("エラー", "キーワードを入力してください", parent=dialog)
                return
            stations = [s for s, v in station_vars.items() if v.get()]
            if not stations:
                messagebox.showerror("エラー", "対象局を1つ以上選択してください", parent=dialog)
                return

            new_data = {'keyword': keyword, 'stations': stations}
            if existing:
                self.manager.update_freeword(existing['id'], new_data)
                freeword = self.manager.get_freeword(existing['id'])
            else:
                freeword = self.manager.add_freeword(new_data)
            self._refresh_freeword_list()
            dialog.destroy()
            if freeword.get('enabled', True):
                self._rescan_freewords_against_cache([freeword])

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=row, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(button_frame, text="保存", command=on_save).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="キャンセル", command=dialog.destroy).pack(side=tk.LEFT, padx=5)

        self._center_dialog_over_parent(dialog)
        dialog.deiconify()

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

    def _open_reservation_dialog(self, existing=None, prefill=None):
        """予約録音の新規追加・編集ダイアログを開く

        Args:
            existing (dict): 編集対象の予約データ（保存済みIDを含む）。編集時のみ指定
            prefill (dict): 新規予約の初期値（番組表からのダブルクリック等）。existingが
                優先されるため、新規追加時のみ指定する
        """
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()
        dialog.title("予約の編集" if existing else "新規予約録音")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        frame = ttk.Frame(dialog, padding=15)
        frame.pack(fill=tk.BOTH, expand=True)
        # 半角コロンだと日本語フォントとの幅計算がずれてラベル末尾が欠けて見えることがあるため
        # 全角コロンを使い、ラベル列にも十分な最小幅を確保しておく
        frame.columnconfigure(0, minsize=110)
        frame.columnconfigure(1, weight=1)

        data = existing or prefill or {}
        row = 0

        ttk.Label(frame, text="ステーション：").grid(row=row, column=0, sticky=tk.W, pady=4)
        station_var = tk.StringVar(value=data.get('station', self.schedule_station_var.get()))
        ttk.Combobox(
            frame, textvariable=station_var, values=self.manager.get_stations(),
            state="readonly", width=20
        ).grid(row=row, column=1, sticky=tk.EW, pady=4)
        row += 1

        ttk.Label(frame, text="繰り返し：").grid(row=row, column=0, sticky=tk.W, pady=4)
        repeat_var = tk.StringVar(value=data.get('repeat', 'once'))
        repeat_frame = ttk.Frame(frame)
        ttk.Radiobutton(
            repeat_frame, text="1回のみ", variable=repeat_var, value="once",
            command=lambda: update_repeat_fields()
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            repeat_frame, text="毎週", variable=repeat_var, value="weekly",
            command=lambda: update_repeat_fields()
        ).pack(side=tk.LEFT, padx=(10, 0))
        repeat_frame.grid(row=row, column=1, sticky=tk.W, pady=4)
        row += 1

        date_label = ttk.Label(frame, text="日付：")
        date_var = tk.StringVar(value=data.get('date_iso', datetime.now().strftime("%Y-%m-%d")))
        date_picker_frame = ttk.Frame(frame)
        date_entry = ttk.Entry(date_picker_frame, textvariable=date_var, width=14, state="readonly")
        date_entry.pack(side=tk.LEFT)
        date_picker_button = ttk.Button(
            date_picker_frame, text="📅",
            command=lambda: self._open_calendar_picker(date_picker_button, date_var)
        )
        date_picker_button.pack(side=tk.LEFT, padx=(4, 0))
        date_row = row
        row += 1

        weekday_label = ttk.Label(frame, text="曜日：")
        weekday_var = tk.StringVar(
            value=self.manager.WEEKDAY_JA[data.get('weekday', datetime.now().weekday())]
        )
        weekday_combo = ttk.Combobox(
            frame, textvariable=weekday_var, values=list(self.manager.WEEKDAY_JA),
            state="readonly", width=20
        )
        weekday_row = row
        row += 1

        def update_repeat_fields():
            if repeat_var.get() == 'weekly':
                date_label.grid_remove()
                date_picker_frame.grid_remove()
                weekday_label.grid(row=weekday_row, column=0, sticky=tk.W, pady=4)
                weekday_combo.grid(row=weekday_row, column=1, sticky=tk.EW, pady=4)
            else:
                weekday_label.grid_remove()
                weekday_combo.grid_remove()
                date_label.grid(row=date_row, column=0, sticky=tk.W, pady=4)
                date_picker_frame.grid(row=date_row, column=1, sticky=tk.W, pady=4)

        update_repeat_fields()

        ttk.Label(frame, text="開始時刻：").grid(row=row, column=0, sticky=tk.W, pady=4)
        start_frame, start_hour_var, start_minute_var = self._create_time_selectors(
            frame, data.get('start', '00:00')
        )
        start_frame.grid(row=row, column=1, sticky=tk.W, pady=4)
        row += 1

        ttk.Label(frame, text="終了時刻：").grid(row=row, column=0, sticky=tk.W, pady=4)
        end_frame, end_hour_var, end_minute_var = self._create_time_selectors(
            frame, data.get('end', '01:00')
        )
        end_frame.grid(row=row, column=1, sticky=tk.W, pady=4)
        row += 1

        ttk.Label(frame, text="番組名（任意）：").grid(row=row, column=0, sticky=tk.W, pady=4)
        title_var = tk.StringVar(value=data.get('title', ''))
        ttk.Entry(frame, textvariable=title_var, width=22).grid(row=row, column=1, sticky=tk.EW, pady=4)
        row += 1

        def on_save():
            if not station_var.get():
                messagebox.showerror("エラー", "ステーションを選択してください", parent=dialog)
                return

            start = self._get_time_value(start_hour_var, start_minute_var)
            end = self._get_time_value(end_hour_var, end_minute_var)
            if start == end:
                messagebox.showerror("エラー", "開始時刻と終了時刻が同じです", parent=dialog)
                return

            new_data = {
                'station': station_var.get(),
                'repeat': repeat_var.get(),
                'start': start,
                'end': end,
                'title': title_var.get().strip(),
            }
            if repeat_var.get() == 'weekly':
                new_data['weekday'] = list(self.manager.WEEKDAY_JA).index(weekday_var.get())
                if existing and existing.get('source') == 'freeword':
                    # フリーワード由来の単発予約を毎週予約に切り替えた場合、
                    # source/keyword_idを残したままだと、この予約のdate_isoが
                    # 元の1回分の放送日のまま更新されないため、フリーワードの
                    # 重複判定（局・放送日・開始時刻・番組名が一致するか）に
                    # 毎回ひっかからず、同じキーワードに一致する将来の回を
                    # 別の単発予約として際限なく自動作成し続けてしまう。
                    # 毎週予約への切り替え後は手動管理の予約として扱う
                    new_data['source'] = None
                    new_data['keyword_id'] = None
            else:
                date_text = date_var.get().strip()
                try:
                    datetime.strptime(date_text, "%Y-%m-%d")
                except ValueError:
                    messagebox.showerror("エラー", "日付を選択してください", parent=dialog)
                    return
                new_data['date_iso'] = date_text

            if existing:
                self.manager.update_reservation(existing['id'], new_data)
            else:
                self.manager.add_reservation(new_data)
            self._refresh_reservation_list()
            dialog.destroy()

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=row, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(button_frame, text="保存", command=on_save).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="キャンセル", command=dialog.destroy).pack(side=tk.LEFT, padx=5)

        self._center_dialog_over_parent(dialog)
        dialog.deiconify()
        dialog.lift()
        dialog.focus_force()

    # 予約録音の開始時刻が来ていないかを巡回する間隔（ミリ秒）
    RESERVATION_CHECK_INTERVAL_MS = 15000
    # 開始失敗・途中中断した予約録音を再開する際の待ち時間（秒）。最初はこの秒数で、
    # 失敗が続くたびに倍にして上限で頭打ちにする（通信断が長引いている間、
    # radiko側へ認証要求を送り続けないようにするため）
    RESERVATION_RESUME_RETRY_SECONDS = 15
    RESERVATION_RESUME_RETRY_MAX_SECONDS = 300
    # 番組の残りがこの秒数を切っていたら、もう再開は試みない
    RESERVATION_RESUME_MIN_REMAINING_SECONDS = 30

    def _schedule_reservation_check(self):
        """予約録音の開始時刻が来ていないか一定間隔でチェックする

        処理中に予期せぬ例外が発生しても、再スケジュールだけは必ず行う。
        そうしないと、長時間起動しっぱなしの環境でこのループが静かに
        止まってしまい、以降の予約録音が一切行われなくなる
        """
        try:
            self._check_due_reservations()
            self._update_sleep_prevention()
        except Exception:
            logger.exception("予約チェック処理でエラーが発生しました")
        self._reservation_check_job = self.root.after(
            self.RESERVATION_CHECK_INTERVAL_MS, self._schedule_reservation_check
        )

    def _on_prevent_sleep_changed(self):
        """設定メニューの「自動スリープを抑止する」チェック切り替え時の保存処理"""
        self.manager.save_settings({'prevent_sleep': self.prevent_sleep_var.get()})
        self._update_sleep_prevention()

    def _update_sleep_prevention(self):
        """有効な予約が存在する間、または録音中は、Windowsのアイドルによる自動スリープを抑止する。

        SetThreadExecutionStateはユーザーの手動スリープ/休止操作や休止状態への
        移行までは防げないが、放置による自動スリープでの予約録音の取りこぼしは防げる。
        設定メニューでオフにしている場合はこの抑止を行わない。
        """
        should_prevent = self.prevent_sleep_var.get() and (
            self.manager.is_recording_active() or any(
                res.get('enabled', True) for res in self.manager.load_reservations()
            )
        )
        if should_prevent == self._sleep_prevented:
            return
        try:
            flags = _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED if should_prevent else _ES_CONTINUOUS
            ctypes.windll.kernel32.SetThreadExecutionState(flags)
            self._sleep_prevented = should_prevent
        except (AttributeError, OSError) as e:
            logger.warning(f"スリープ抑止状態の変更に失敗しました: {e}")

    def _schedule_reservation_list_minute_refresh(self):
        """予約一覧の状態（成功/失敗/録音中）は時間経過で変わるため、毎分0秒に再描画する

        再描画中に例外が起きても再スケジュールだけは必ず行い、長時間起動時に
        このループ自体が止まってしまわないようにする
        """
        try:
            self._refresh_reservation_list()
        except Exception:
            logger.exception("予約一覧の再描画でエラーが発生しました")
        now = datetime.now()
        seconds_until_next_minute = 60 - now.second - now.microsecond / 1_000_000
        self._reservation_list_refresh_job = self.root.after(
            int(seconds_until_next_minute * 1000), self._schedule_reservation_list_minute_refresh
        )

    def _schedule_program_guide_minute_refresh(self):
        """番組表の「終了済み番組のグレーアウト」は時間経過で変わるため、毎分0秒に再描画する

        再描画中に例外が起きても再スケジュールだけは必ず行い、長時間起動時に
        このループ自体が止まってしまわないようにする
        """
        try:
            self.display_schedule(self._current_programs, mode=self.schedule_mode_var.get())
        except Exception:
            logger.exception("番組表の再描画でエラーが発生しました")
        now = datetime.now()
        seconds_until_next_minute = 60 - now.second - now.microsecond / 1_000_000
        self._program_guide_refresh_job = self.root.after(
            int(seconds_until_next_minute * 1000), self._schedule_program_guide_minute_refresh
        )

    def _check_due_reservations(self):
        """開始時刻を迎えた予約があれば録音を開始する

        局が異なれば複数の予約（および手動録音）を同時に進行できる。
        同じ局が既に録音中の場合はその予約だけ今回は見送り、猶予時間内であれば
        次回の巡回で再度試みる（実行済みマークは付けない）。
        """
        due = self.manager.get_due_reservations()
        started_any = False

        for reservation, start_dt, end_dt, occurrence_iso in due:
            station = reservation.get('station')

            if station not in self.manager.get_stations():
                self.manager.mark_reservation_run(reservation['id'], occurrence_iso, result='failed')
                started_any = True
                continue

            if self.manager.is_recording_active(station):
                # 他の予約や手動録音でその局が使用中。マークせず次回の巡回で再試行する
                continue

            started_any = True
            if self._start_reservation_recording(reservation, end_dt, occurrence_iso):
                continue

            # 開始に失敗した（開始時刻ちょうどの瞬断・認証エラー等）。ここで諦めると
            # その回は丸ごと録れないため、実行済みとして記録したうえで、番組終了まで
            # 再開を試み続ける
            self.manager.mark_reservation_run(reservation['id'], occurrence_iso, result='failed')
            key = (reservation['id'], occurrence_iso)
            self._reservation_resume_state[key] = {'attempts': 0, 'had_data': False}
            if self._schedule_reservation_resume(reservation['id'], occurrence_iso, end_dt):
                message = (
                    f"{station} の予約録音を開始できませんでした。\n"
                    "番組が終わるまで再試行を続けます。通信状況をご確認ください。"
                )
            else:
                self._reservation_resume_state.pop(key, None)
                message = f"{station} の予約録音を開始できませんでした。\n通信状況をご確認ください。"
            if self._tray_icon is not None:
                self._notify_via_tray("予約録音", message)
            else:
                # ここで直接ダイアログを出すと、閉じられるまでこの巡回処理から戻れず、
                # 次回の巡回が予約されない（＝以降の予約録音が始まらない）ため、後回しにする
                self.root.after(0, messagebox.showwarning, "予約録音", message)

        if started_any:
            self._refresh_reservation_list()

    def _start_reservation_recording(self, reservation, end_dt, occurrence_iso):
        """予約 reservation の録音を、今から end_dt までの長さで開始する

        予約の開始時刻が来たとき（_check_due_reservations）と、開始失敗・途中中断の後に
        残りを録り直すとき（_resume_reservation_recording）の両方から呼ばれる。

        Returns:
            bool: 録音を開始できたか
        """
        station = reservation.get('station')
        duration_minutes = max(1, math.ceil((end_dt - datetime.now()).total_seconds() / 60))
        file_format = self.format_var.get()
        try:
            mp3_bitrate = int(self.bitrate_var.get())
        except ValueError:
            mp3_bitrate = 192

        reservation_title = reservation.get('title')
        program = self._find_program_for_reservation(station, reservation)
        success, output_path = self.manager.start_recording(
            station, duration_minutes,
            file_format=file_format, mp3_bitrate=mp3_bitrate,
            title=reservation_title, filename_pattern=self.filename_pattern_var.get(),
            metadata=self._build_recording_metadata(
                station, program=program, fallback_title=reservation_title
            ),
            reservation_id=reservation['id'],
            on_complete=lambda had_data, path, error, rid=reservation['id'], occ=occurrence_iso, st=station:
                self.root.after(
                    0, self._on_reservation_recording_complete, rid, occ, st, had_data, path, error, end_dt
                )
        )
        if success:
            self._register_active_recording(station, reservation_title or station, duration_minutes)
            self._watch_background_recording(station)
        return success

    def _schedule_reservation_resume(self, reservation_id, occurrence_iso, end_dt):
        """開始失敗・途中中断した予約録音の再開を、少し待ってから試みるよう予約する

        Returns:
            bool: 再開を予約したか（番組の残り時間がほとんど無ければFalse）
        """
        key = (reservation_id, occurrence_iso)
        state = self._reservation_resume_state.setdefault(key, {'attempts': 0, 'had_data': False})
        delay = min(
            self.RESERVATION_RESUME_RETRY_SECONDS * (2 ** state['attempts']),
            self.RESERVATION_RESUME_RETRY_MAX_SECONDS,
        )
        remaining = (end_dt - datetime.now()).total_seconds()
        if remaining - delay < self.RESERVATION_RESUME_MIN_REMAINING_SECONDS:
            return False
        state['attempts'] += 1
        self.root.after(
            int(delay * 1000), self._resume_reservation_recording, reservation_id, occurrence_iso, end_dt
        )
        return True

    def _resume_reservation_recording(self, reservation_id, occurrence_iso, end_dt):
        """開始失敗・途中中断した予約録音の残りを、新しいファイルに録音し直す

        開始できなければ（通信がまだ復旧していない、同じ局を別の録音が使っている等）、
        番組が終わるまで _schedule_reservation_resume で繰り返し試みる。
        """
        key = (reservation_id, occurrence_iso)
        reservation = self.manager.get_reservation(reservation_id)
        if reservation is None or not reservation.get('enabled', True):
            # 待っている間に予約が削除・無効化された
            self._reservation_resume_state.pop(key, None)
            return

        station = reservation.get('station')
        started = False
        try:
            if not self.manager.is_recording_active(station):
                started = self._start_reservation_recording(reservation, end_dt, occurrence_iso)
        except Exception:
            logger.exception(f"予約録音の再開処理でエラーが発生しました: {station}")

        if started:
            logger.info(f"予約録音を再開しました: {station}")
            self._refresh_reservation_list()
        elif not self._schedule_reservation_resume(reservation_id, occurrence_iso, end_dt):
            logger.warning(f"予約録音を再開できないまま番組が終了しました: {station}")
            self._reservation_resume_state.pop(key, None)

    def _on_reservation_recording_complete(self, reservation_id, occurrence_iso, station, had_data, output_path,
                                           error, end_dt=None):
        """予約録音のバックグラウンドスレッド終了時（メインスレッドから呼ばれる）:
        実際にデータを取得できたかに基づいて予約の実行結果を記録する。

        録音開始時点では「配信URLの取得に成功したか」しか分からず、認証切れ等で
        セグメントを1つも受信できなかった場合でも見た目上は「開始成功」になってしまう
        （出力ファイルが0バイトのまま）。この結果を録音終了後に上書きすることで、
        予約一覧の状態表示（成功/失敗）が実態と一致するようにする。

        通信エラーで中断された場合、番組の残り時間（end_dt まで）があれば、残りを
        別のファイルに録音し直す（_schedule_reservation_resume）。一度でも中断が
        あった回は、最後まで録れても欠落があるため「成功」ではなく「中断」と記録する。
        """
        key = (reservation_id, occurrence_iso)
        state = self._reservation_resume_state.get(key)
        interrupted_before = state is not None
        any_data = had_data or (interrupted_before and state['had_data'])

        will_resume = False
        if error and end_dt is not None:
            state = self._reservation_resume_state.setdefault(key, {'attempts': 0, 'had_data': False})
            if had_data:
                # 録音できていた＝通信は一度復旧していたので、待ち時間を最初からやり直す
                state['attempts'] = 0
                state['had_data'] = True
            will_resume = self._schedule_reservation_resume(reservation_id, occurrence_iso, end_dt)
        if not will_resume:
            self._reservation_resume_state.pop(key, None)

        if error or interrupted_before:
            result = 'partial' if any_data else 'failed'
        else:
            result = 'success' if had_data else 'failed'
        self.manager.mark_reservation_run(reservation_id, occurrence_iso, result=result)
        self._refresh_reservation_list()
        if hasattr(self, 'files_tree'):
            self._refresh_files_list()

        if not had_data:
            if interrupted_before:
                # 再開を試みたが今回も取得できなかった。最初の中断・開始失敗の時点で
                # 既に知らせているため、試行のたびに通知を繰り返さない
                return
            detail = f"\n（{error}）" if error else ""
            retry_note = "\n番組が終わるまで再試行を続けます。" if will_resume else ""
            message = (
                f"{station} の予約録音でデータを取得できませんでした（ファイルが空です）。\n"
                f"通信状況をご確認ください。{retry_note}{detail}"
            )
        elif error:
            resume_note = (
                "通信が復旧しだい、残りを別のファイルに録音します。\n" if will_resume else ""
            )
            message = (
                f"{station} の予約録音は途中で通信エラーが発生し、中断されました。\n"
                f"それまでの内容は保存されています。\n{resume_note}"
                f"保存先: {output_path}\n（{error}）"
            )
        else:
            if self._tray_icon is not None:
                note = "（途中で中断があったため、ファイルが分かれています）" if interrupted_before else ""
                self._notify_via_tray("予約録音", f"{station} の予約録音が完了しました。{note}")
            return

        if self._tray_icon is not None:
            self._notify_via_tray("予約録音", message)
        else:
            messagebox.showwarning("予約録音", message)

    def _on_manual_recording_complete(self, station, had_data, output_path, error):
        """手動録音（録音タブ・番組表からの「今すぐ録音」）終了時（メインスレッドから呼ばれる）:
        データを1バイトも取得できなかった場合に警告する
        """
        if hasattr(self, 'files_tree'):
            self._refresh_files_list()
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

    def fetch_and_cache_schedule(self, station):
        """番組表を取得してキャッシュに保存する

        Returns:
            (list, bool): 取得結果のprogramsリストと、実データの取得に成功したかどうか。
            実APIが空を返した場合はサンプルデータで代用し、成功フラグは False になる。
        """
        programs = self.manager.get_program_schedule(station, days=self.SCHEDULE_FETCH_DAYS)
        success = bool(programs)
        if not programs:
            programs = self.manager.get_sample_schedule(station, days=self.SCHEDULE_FETCH_DAYS)
        self.manager.save_schedule_cache(station, programs)
        if success:
            self._scan_freewords_and_refresh(station, programs)
        # 通常の番組表と同じタイミングで、タイムフリー（過去7日間）のキャッシュも
        # 一緒に更新する（開いたことのない局のタイムフリーがいつまでも古いまま
        # 残ってしまわないようにするため）。失敗しても通常の番組表更新自体は
        # 失敗扱いにしないベストエフォート
        self._fetch_and_cache_timefree_schedule(station)
        return programs, success

    def _fetch_and_cache_timefree_schedule(self, station):
        """指定局のタイムフリー（過去7日間）番組表を取得してキャッシュに保存する

        fetch_and_cache_schedule（通常の番組表更新）から常に一緒に呼ばれる。
        """
        try:
            timefree_programs = self.manager.get_timefree_schedule(station)
            if timefree_programs:
                self.manager.save_schedule_cache(f"{station}::timefree", timefree_programs)
        except Exception:
            logger.exception(f"タイムフリー番組表の更新に失敗しました: {station}")

    def _scan_freewords_and_refresh(self, station, programs):
        """番組表更新後: フリーワードと照合して新規予約を自動作成し、
        予約一覧タブが既に構築済みならその場で表示を更新する
        """
        created = self.manager.scan_freewords_for_station(station, programs)
        if created and hasattr(self, 'reservation_tree'):
            self._refresh_reservation_list()

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

    def _refresh_stale_stations(self):
        """起動時: 当日以降の情報が一定日数以下しかない局の番組表を自動更新する

        局数が多いと時間がかかるため、進捗ダイアログ（キャンセル可）を表示しつつ
        複数局を並列でバックグラウンド取得する（詳細は _bulk_refresh_schedules と同様）。
        """
        stale_stations = [
            s for s in self.manager.get_stations()
            if self.manager.count_cached_future_days(s) <= self.SCHEDULE_STALE_THRESHOLD_DAYS
        ]
        if not stale_stations:
            return

        dialog, progress_var, progressbar, cancel_event = self._open_schedule_fetch_progress_dialog(
            "番組表の自動更新"
        )
        progressbar.configure(maximum=len(stale_stations))
        progress_var.set(f"0/{len(stale_stations)}局 取得中...")

        def worker():
            failed_stations = []
            any_created = False
            completed = 0
            lock = threading.Lock()

            def fetch_one(station):
                nonlocal completed, any_created
                if cancel_event.is_set():
                    return
                success, created, error_label = self._fetch_and_cache_schedule_no_ui(station, cancel_event)
                with lock:
                    if not success:
                        failed_stations.append(error_label)
                    if created:
                        any_created = True
                    completed += 1
                    n = completed
                self.root.after(
                    0, self._update_schedule_fetch_progress, progress_var, progressbar, n,
                    len(stale_stations), station
                )

            with ThreadPoolExecutor(max_workers=self.SCHEDULE_STATION_FETCH_WORKERS) as executor:
                list(executor.map(fetch_one, stale_stations))

            self.root.after(0, self._on_stale_refresh_done, dialog, any_created, failed_stations)

        threading.Thread(target=worker, daemon=True).start()

    def _on_stale_refresh_done(self, dialog, any_created, failed_stations):
        """起動時の自動番組表更新の完了処理（メインスレッドで実行）"""
        dialog.grab_release()
        dialog.destroy()

        if any_created:
            self._refresh_reservation_list()
        self.load_schedule_for_current_station()

        if failed_stations:
            messagebox.showwarning(
                "番組表の自動更新",
                "以下の局は番組表の取得に失敗しました（サンプルデータで代用しています）:\n\n"
                + "\n".join(failed_stations)
            )

    def _check_full_schedule_refresh_due(self):
        """全局番組表の自動更新（1日1回）が必要かを定期的にチェックする

        アプリを起動しっぱなしにしていても、フリーワード予約が新着番組を
        取りこぼさないよう、毎日 FULL_SCHEDULE_REFRESH_TIME（既定 05:00）を
        過ぎたタイミングで、その日まだ実行していなければ全局の番組表を
        取得し直す。対象時刻ちょうどを狙うのではなく「過ぎていればすぐ実行」
        にすることで、チェック間隔（既定10分）のズレや、対象時刻をまたいで
        アプリを起動した場合でも確実にその日のうちに1回は実行される。
        """
        now = datetime.now()
        try:
            target_time = datetime.strptime(self.FULL_SCHEDULE_REFRESH_TIME, "%H:%M").time()
        except ValueError:
            target_time = datetime.strptime("05:00", "%H:%M").time()
        target_dt = datetime.combine(now.date(), target_time)

        last_date = self.manager.load_settings().get('last_full_schedule_refresh_date')
        if (
            now >= target_dt
            and last_date != now.strftime("%Y-%m-%d")
            and not self._full_refresh_in_progress
        ):
            self._start_full_schedule_refresh()

        self._full_refresh_check_job = self.root.after(
            self.FULL_SCHEDULE_REFRESH_CHECK_INTERVAL_MS, self._check_full_schedule_refresh_due
        )

    def _check_radiru_index_refresh_due(self):
        """NHK聴き逃し（らじる★らじる）の番組インデックス更新が必要かを定期的にチェックする

        全局番組表自動更新と違い「1日1回、特定時刻を過ぎたら」ではなく、
        new_arrivalsが特定の時刻に紐づく情報ではないため「前回取得から
        RadiruManager.INDEX_REFRESH_INTERVAL_HOURS時間経過したら」で判定する。
        アプリを起動していなかった間の分は、次に起動した直後のチェックで
        即座に取得される。
        """
        last_iso = self.manager.load_settings().get('last_radiru_index_fetch')
        due = True
        if last_iso:
            try:
                last_dt = datetime.fromisoformat(last_iso)
                due = (datetime.now() - last_dt) >= timedelta(
                    hours=RadiruManager.INDEX_REFRESH_INTERVAL_HOURS
                )
            except ValueError:
                due = True

        if due and not self._radiru_refresh_in_progress:
            self._start_radiru_index_refresh()

        self._radiru_refresh_check_job = self.root.after(
            self.FULL_SCHEDULE_REFRESH_CHECK_INTERVAL_MS, self._check_radiru_index_refresh_due
        )

    def _start_radiru_index_refresh(self):
        """NHK聴き逃しの新着番組インデックスをバックグラウンドスレッドで更新する"""
        self._radiru_refresh_in_progress = True
        thread = threading.Thread(target=self._radiru_index_refresh_worker, daemon=True)
        thread.start()

    def _radiru_index_refresh_worker(self):
        try:
            self.radiru_manager.update_index()
            self.manager.save_settings({
                'last_radiru_index_fetch': datetime.now().isoformat(timespec="seconds")
            })
        except Exception:
            logger.exception("NHK聴き逃しインデックスの更新に失敗しました")
        finally:
            self._radiru_refresh_in_progress = False

    def _start_full_schedule_refresh(self):
        """全局の番組表をバックグラウンドスレッドで取得し直す（UIは一切ブロックしない）"""
        self._full_refresh_in_progress = True
        today_iso = datetime.now().strftime("%Y-%m-%d")
        stations = self.manager.get_stations()
        thread = threading.Thread(
            target=self._full_schedule_refresh_worker, args=(stations, today_iso), daemon=True
        )
        thread.start()

    def _full_schedule_refresh_worker(self, stations, today_iso):
        """バックグラウンドスレッド本体: 全局を順に取得し、フリーワード照合も行う。
        Tkinterウィジェットには一切触れず、完了後の反映は root.after 経由で行う。

        通常の番組表（未来方向）だけでなく、タイムフリー対象期間（過去7日間）の
        番組表もここで一緒に更新する。タイムフリーのキャッシュは以前、この自動更新の
        対象に一切含まれておらず、該当局のタイムフリータブを開かない限り何日経っても
        更新されなかった（対象期間を丸ごと過ぎ去ってから開くと空の番組表になる）ため
        追加した。フリーワードによる自動予約作成は未来の番組のみが対象なので、
        タイムフリー側ではキャッシュの保存のみ行い、フリーワード照合は行わない。
        """
        logger.info(f"全局番組表自動更新を開始します（{len(stations)}局）")
        failed_stations = []
        any_created = False
        lock = threading.Lock()

        def fetch_one(station):
            nonlocal any_created
            try:
                programs = self.manager.get_program_schedule(station, days=self.SCHEDULE_FETCH_DAYS)
                if not programs:
                    with lock:
                        failed_stations.append(station)
                else:
                    self.manager.save_schedule_cache(station, programs)
                    if self.manager.scan_freewords_for_station(station, programs):
                        with lock:
                            any_created = True
            except Exception as e:
                with lock:
                    failed_stations.append(f"{station}（{e}）")

            try:
                timefree_programs = self.manager.get_timefree_schedule(station)
                if not timefree_programs:
                    with lock:
                        failed_stations.append(f"{station}（タイムフリー）")
                else:
                    self.manager.save_schedule_cache(f"{station}::timefree", timefree_programs)
            except Exception as e:
                with lock:
                    failed_stations.append(f"{station}（タイムフリー: {e}）")

        with ThreadPoolExecutor(max_workers=self.SCHEDULE_STATION_FETCH_WORKERS) as executor:
            list(executor.map(fetch_one, stations))

        self.manager.prune_stale_schedule_cache()
        self.manager.prune_image_cache()
        self.manager.save_settings({'last_full_schedule_refresh_date': today_iso})
        logger.info(
            f"全局番組表自動更新が完了しました（失敗{len(failed_stations)}件）"
        )
        self.root.after(0, self._on_full_schedule_refresh_done, failed_stations, any_created)

    def _on_full_schedule_refresh_done(self, failed_stations, any_created):
        """全局自動更新の完了処理（メインスレッドで実行）: 表示中の番組表・予約一覧に反映する"""
        self._full_refresh_in_progress = False

        if any_created:
            self._refresh_reservation_list()

        station = self.schedule_station_var.get()
        if self.schedule_mode_var.get() == 'timefree':
            cached = self.manager.load_cached_schedule(f"{station}::timefree")
            if cached and self._has_programs_in_timefree_window(cached):
                self.display_schedule(cached, mode='timefree')
        else:
            cached = self.manager.load_cached_schedule(station)
            if cached:
                self.display_schedule(cached)

        if failed_stations:
            logger.warning(f"[全局自動更新] 取得に失敗した局: {', '.join(failed_stations)}")

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
                # 対象日のエピソードが実際に配信中かまでは番組表の描画時点では
                # 確認していない（番組の数だけネットワーク問い合わせが必要になり
                # 重くなるため）ので、あくまで目安の表示
                if show_radiru_availability and not not_yet_aired:
                    available = self.radiru_manager.find_series_for_program(
                        radiru_broadcast_code, program.get('title') or '', program.get('corner_name'),
                        index=radiru_index,
                    ) is not None
                    outline_color = (
                        colors['radiru_available_outline'] if available
                        else colors['radiru_unavailable_outline']
                    )
                    outline_width = 2 if available else 1

                rect = canvas.create_rectangle(
                    x1, y1, x2, y2, fill=fill_color, outline=outline_color, width=outline_width
                )
                items = [rect]

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

    def _reset_eq(self):
        """イコライザーの表示を即座に0へリセットする"""
        num_channels = len(self._eq_display_levels)
        self._eq_display_levels = [0.0] * num_channels
        self._eq_peak_levels = [0.0] * num_channels
        self._update_eq()

    def _schedule_eq_update(self):
        """再生中、グラフィックイコライザーを一定間隔で再描画する

        録音時はデコードが実時間より速く進み更新が間欠的になるため使わない
        （録音中は代わりに「● REC」の点滅表示を使う。_schedule_recording_watch参照）。
        """
        self._update_eq()
        if self.manager.is_playing():
            self._eq_update_job = self.root.after(50, self._schedule_eq_update)
        else:
            # エラー等でバックグラウンド再生が止まっていたらUIも再生停止状態に戻す
            self._eq_update_job = None
            if str(self.play_stop_button['state']) == tk.NORMAL:
                self.stop_playback()

    def _schedule_recording_watch(self):
        """手動録音中の局のバックグラウンドスレッドが（時間経過等で）自然終了していないか監視する"""
        if self.manager.is_recording_active(self._manual_recording_station):
            self._recording_watch_job = self.root.after(500, self._schedule_recording_watch)
        else:
            self._recording_watch_job = None
            if str(self.stop_button['state']) == tk.NORMAL:
                self.stop_recording()

    def _start_rec_blink(self):
        """「● REC」インジケーターの点滅を開始する"""
        self._rec_blink_visible = True
        self._blink_rec_indicator()

    def _blink_rec_indicator(self):
        self.rec_indicator_var.set("● REC" if self._rec_blink_visible else "")
        self._rec_blink_visible = not self._rec_blink_visible
        self._rec_blink_job = self.root.after(500, self._blink_rec_indicator)

    def _stop_rec_blink(self):
        """「● REC」インジケーターの点滅を止めて非表示にする"""
        if self._rec_blink_job:
            self.root.after_cancel(self._rec_blink_job)
            self._rec_blink_job = None
        self.rec_indicator_var.set("")

    # 減衰速度（1回の描画更新あたりの減衰量）。値が大きいほど素早く0に落ちる
    EQ_DECAY_PER_TICK = 0.06
    # ピークホールド（直近の最大値を示す白いバー）の減衰速度。本体より緩やかに落とす
    EQ_PEAK_DECAY_PER_TICK = 0.012

    def _update_eq(self):
        """マネージャーが保持する最新のレベルでイコライザー表示を更新する（モードに応じて分岐）"""
        if self.eq_mode_var.get() == "peak":
            self._update_analog_peak_meter()
        else:
            self._update_led_spectrum()

    def _update_analog_peak_meter(self):
        """L/Rチャンネルレベルでアナログ針メーターの針を更新

        VUメーターと同様「素早く上昇・ゆっくり減衰」させることで、
        実際の針の慣性のような自然な動きに見せる。
        """
        levels = self.manager.get_lr_levels()
        radius = self.EQ_ANALOG_RADIUS
        for i, needle in enumerate(self._eq_needles):
            target = levels[i] if i < len(levels) else 0.0
            current = self._eq_display_levels[i]
            current = target if target >= current else max(target, current - self.EQ_DECAY_PER_TICK)
            self._eq_display_levels[i] = current

            pivot_x, pivot_y = self._eq_pivots[i]
            tick_angle = math.radians(self._analog_tk_angle(current))
            tip_x = pivot_x + radius * math.cos(tick_angle)
            tip_y = pivot_y - radius * math.sin(tick_angle)
            self.eq_canvas.coords(needle, pivot_x, pivot_y, tip_x, tip_y)

    def _update_led_spectrum(self):
        """マネージャーが保持する最新のバンドレベルでイコライザーのLEDセグメントを更新

        録音時はデコードが実時間より大幅に速く進む（再生時と違い音声出力デバイスに
        よる自然なペーシングが無い）ため、セグメント単位でレベルがまとめて更新され、
        その後の取得待ち時間中は値が変化しない「バースト＋停止」になりがちで、
        そのまま描画すると動きがギクシャクして見える。VUメーターと同様に
        「新しい値が高ければ即座に反映し、低ければゆっくり減衰させる」ことで、
        更新が間欠的でも見た目は滑らかにする。ピークホールド（白いバー）はさらに
        ゆっくり減衰させ、直近の最大値を一時的に示す。
        """
        levels = self.manager.get_levels()
        for i, band_segments in enumerate(self._eq_segments):
            target = levels[i] if i < len(levels) else 0.0
            current = self._eq_display_levels[i]
            current = target if target >= current else max(target, current - self.EQ_DECAY_PER_TICK)
            self._eq_display_levels[i] = current

            lit_count = round(current * self.EQ_NUM_SEGMENTS)
            for s, (rect, on_color) in enumerate(band_segments):
                self.eq_canvas.itemconfig(rect, fill=on_color if s < lit_count else self._eq_off_color)

            peak = self._eq_peak_levels[i]
            peak = current if current >= peak else max(0.0, peak - self.EQ_PEAK_DECAY_PER_TICK)
            self._eq_peak_levels[i] = peak

            x0, x1 = self._eq_band_bounds[i]
            peak_y1 = max(self.EQ_PEAK_HOLD_HEIGHT, self.EQ_HEIGHT - peak * self.EQ_HEIGHT)
            peak_y0 = peak_y1 - self.EQ_PEAK_HOLD_HEIGHT
            self.eq_canvas.coords(self._eq_peak_rects[i], x0, peak_y0, x1, peak_y1)


_ERROR_ALREADY_EXISTS = 183
# 多重起動防止用ミューテックスのハンドル。プロセスが生きている間ずっと保持し続ける
# 必要があるため、モジュール変数に入れておく（閉じると他の起動を検知できなくなる）
_single_instance_mutex = None


def _acquire_single_instance_lock():
    """このRecR（同じ場所に置かれたもの）が既に起動していないかを確認する

    多重起動すると、同じ予約を両方が録音して重複ファイルができるうえ、設定・予約
    ファイルを互いに上書きし合ってしまう。Windowsの名前付きミューテックスは
    プロセスが強制終了しても自動的に解放されるため、ロックファイル方式と違い
    「異常終了後に起動できなくなる」ことがない。

    Returns:
        bool: 起動してよければTrue、既に起動中ならFalse
    """
    global _single_instance_mutex
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return True
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    # 設定・予約は実行ファイルの置き場所ごとに独立しているため、置き場所ごとに判定する
    digest = hashlib.md5(str(get_base_dir()).lower().encode("utf-8")).hexdigest()
    handle = kernel32.CreateMutexW(None, False, f"RecR_SingleInstance_{digest}")
    if not handle:
        # 判定できない場合は、起動できなくなるよりは起動を優先する
        logger.warning("多重起動の確認に失敗しました（確認なしで起動します）")
        return True
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _single_instance_mutex = handle
    return True


def _install_exception_logging(root):
    """どこにも捕捉されなかった例外をログファイルに記録するようにする

    既定ではこれらは標準エラー出力に書かれるだけで、コンソールを持たない配布版
    （PyInstallerのwindowedビルド）では誰にも見えないまま消えてしまう。長期間
    起動しっぱなしで「いつの間にか動かなくなった」ときに原因を追えるようにする。
    """
    def log_tk_exception(exc_type, exc_value, exc_tb):
        logger.error(
            "画面の処理中に予期しないエラーが発生しました", exc_info=(exc_type, exc_value, exc_tb)
        )

    def log_thread_exception(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "不明"
        logger.error(
            f"スレッド（{name}）で予期しないエラーが発生しました",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    def log_uncaught_exception(exc_type, exc_value, exc_tb):
        logger.critical(
            "予期しないエラーでアプリが終了します", exc_info=(exc_type, exc_value, exc_tb)
        )

    root.report_callback_exception = log_tk_exception
    threading.excepthook = log_thread_exception
    sys.excepthook = log_uncaught_exception


def main():
    if not _acquire_single_instance_lock():
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "RecR",
            "RecRは既に起動しています。\n"
            "ウィンドウが見当たらない場合は、タスクトレイのアイコンから表示できます。"
        )
        root.destroy()
        return

    root = tk.Tk()
    _install_exception_logging(root)
    app = RecRApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
