#!/usr/bin/env python3
"""
RecR - ラジコ録音アプリケーション
radiko.jp の放送を Windows で録音するデスクトップアプリケーション
"""

import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime
import ctypes
import faulthandler
import hashlib
import logging
import os
import sys
import threading
import time

import sv_ttk

# 親ディレクトリをパスに追加してインポート
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.app_logger import LOG_DIR, setup_logging
from utils.cm_detect import CmChapterer
from utils.cm_fingerprint import FingerprintCollector
from utils.paths import get_base_dir
from utils.radiko_manager import RadikoManager
from utils.radiru_manager import RadiruManager
from gui.tray import TrayMixin
from gui.meters import MetersMixin
from gui.settings_menu import SettingsMenuMixin
from gui.widgets import WidgetHelpersMixin
from gui.schedule_refresh import ScheduleRefreshMixin
from gui.record_tab import RecordTabMixin
from gui.notice_panel import NoticePanelMixin
from gui.active_tasks import ActiveTasksMixin
from gui.schedule_tab import ScheduleTabMixin
from gui.search_panel import SearchPanelMixin
from gui.programs import ProgramLookupMixin
from gui.downloads import DownloadsMixin
from gui.reservation_tab import ReservationTabMixin
from gui.missed_recovery import MissedRecoveryMixin
from gui.reservation_runner import ReservationRunnerMixin
from gui.freeword_tab import FreewordTabMixin
from gui.files_tab import FilesTabMixin
from gui.cm_fingerprint import CmFingerprintMixin

setup_logging()
logger = logging.getLogger(__name__)


class RecRApp(
    TrayMixin,
    MetersMixin,
    SettingsMenuMixin,
    WidgetHelpersMixin,
    ScheduleRefreshMixin,
    RecordTabMixin,
    NoticePanelMixin,
    ActiveTasksMixin,
    ScheduleTabMixin,
    SearchPanelMixin,
    ProgramLookupMixin,
    DownloadsMixin,
    ReservationTabMixin,
    MissedRecoveryMixin,
    ReservationRunnerMixin,
    FreewordTabMixin,
    FilesTabMixin,
    CmFingerprintMixin,
):
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

    # メニュー「ヘルプ」→「バージョン情報」に表示する版数。リリースのたびに更新する
    APP_VERSION = "1.20β2"
    # メニュー「ヘルプ」→「ヘルプ」で開くGitHub wikiの操作マニュアル
    HELP_URL = "https://github.com/recr848-ui/RecR/wiki/操作マニュアル"

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
        # CMカット用の音声フィンガープリント取得（毎日指定時刻にタイムフリーで前日分を取る）
        self.cm_fp_collector = FingerprintCollector(self.manager, self._cm_fp_busy_reason, LOG_DIR)
        self.cm_fp_collector.update_settings(settings)
        self._cm_fp_check_job = None
        # 録音ファイルへのCMチャプター付け（録音直後と、放送から3日後にもう一度）
        self.cm_chapterer = CmChapterer(
            self.manager, LOG_DIR,
            on_finished=lambda path, record: self.root.after(0, self._on_cm_chapters_finished, path, record),
        )
        self.cm_chapterer.update_settings(settings)
        self._cm_chapter_due_checked = None

        self._busy_saved_states = {}
        self._reservation_check_job = None
        self._reservation_list_refresh_job = None
        self._program_guide_refresh_job = None
        self._full_refresh_check_job = None
        self._full_refresh_in_progress = False
        # 起動時の自動番組表更新（_refresh_stale_stations）の実行中は、毎日の全局自動更新を
        # 始めない。両方が同時に走ると、同じ局の番組表を二重に取得してしまうため
        self._stale_refresh_in_progress = False
        # (日付, {局名}): その日のうちに起動時の自動更新で取得し終えた局。
        # 毎日の全局自動更新では取得し直さない
        self._stations_refreshed_today = (None, set())
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
        # 局が使用中で開始を見送った (予約ID, 対象日)。ログを1回だけ出すため
        self._busy_skip_logged = set()
        # 取り逃した予約の自動取得（_run_missed_reservation_recovery）の状態
        self.auto_recover_missed_var = tk.BooleanVar(value=settings.get('auto_recover_missed', True))
        self._missed_recovery_in_progress = False
        self._missed_recovery_job = None
        # 予約チェックの巡回を最後に実行した時刻。間隔が大きく空いたら、スリープや
        # 応答なしで止まっていたとみなして取り逃しの回収を前倒しする
        self._last_reservation_check_at = None
        self.setup_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close_button)
        self._refresh_stale_stations()
        self.load_schedule_for_current_station()
        self._schedule_reservation_check()
        self._schedule_reservation_list_minute_refresh()
        self._schedule_program_guide_minute_refresh()
        self._check_full_schedule_refresh_due()
        self._check_radiru_index_refresh_due()
        # 起動直後は番組表の自動更新などと重ならないよう少し待ってから始める
        self._cm_fp_check_job = self.root.after(
            self.CM_FP_STARTUP_DELAY_MS, self._check_cm_fp_run_due
        )
        self.root.after(1500, self._notify_missed_reservations_on_startup)
        # 起動直後は番組表の自動更新などと重ならないよう少し待ってから始める
        self._missed_recovery_job = self.root.after(60 * 1000, self._schedule_missed_reservation_recovery)
        self._refresh_notice()

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


# 画面が固まったときのスタックの書き出し先（recr.log とは別ファイル）
FREEZE_TRACE_FILE = LOG_DIR / "freeze_trace.log"
FREEZE_TRACE_MAX_BYTES = 1024 * 1024
# メインループがこの秒数応答しなければ「固まった」とみなす。確認ダイアログの表示中も
# メインループは回り続けるため、通常の操作でこの時間を超えることはない
FREEZE_TIMEOUT_SECONDS = 60
FREEZE_HEARTBEAT_INTERVAL_MS = 5000


def _install_freeze_trace(root):
    """画面（メインループ）が固まったとき、全スレッドのスタックをファイルに書き出すようにする

    固まった場合は例外が出ないため recr.log には何も残らず、後から原因を追えない。
    メインループから定期的に「FREEZE_TIMEOUT_SECONDS 秒後にスタックを書き出す」予約を
    入れ直し続けることで、メインループが止まったときだけ予約が実行されるようにする。

    書き出しは faulthandler が持つPythonの外側のスレッドが行うため、ネイティブコードが
    GILを握ったまま止まってPython全体が動けなくなっている場合でも記録が残る
    （Pythonのスレッドで監視する方式では、その場合に監視側も止まってしまう）。
    同じファイルに、異常終了（アクセス違反等）時のスタックも書き出す。
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if (FREEZE_TRACE_FILE.exists()
                and FREEZE_TRACE_FILE.stat().st_size > FREEZE_TRACE_MAX_BYTES):
            FREEZE_TRACE_FILE.unlink()
        # faulthandler がファイル記述子を直接使うため、アプリ終了まで開いたままにする
        trace_file = open(FREEZE_TRACE_FILE, "a", encoding="utf-8")
        faulthandler.enable(file=trace_file, all_threads=True)
    except Exception:
        logger.exception("フリーズ時のスタック記録を有効にできませんでした")
        return

    last_beat = time.monotonic()

    def beat():
        nonlocal last_beat
        now = time.monotonic()
        stalled = now - last_beat
        last_beat = now
        try:
            if stalled >= FREEZE_TIMEOUT_SECONDS:
                # faulthandler の出力には時刻が入らないため、復帰できた場合はここで補う
                # （復帰しなかった場合は、ファイルの更新日時が固まった時刻の目安になる）。
                # PCのスリープからの復帰直後にも出ることがある
                message = (
                    f"メインループが約{stalled:.0f}秒応答していませんでした"
                    f"（{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} に復帰）"
                )
                logger.warning(f"{message}。スタックは {FREEZE_TRACE_FILE.name} を参照")
                trace_file.write(f"--- {message} ---\n\n")
                trace_file.flush()
            # 予約を入れ直す（前回の予約は取り消される）
            faulthandler.dump_traceback_later(FREEZE_TIMEOUT_SECONDS, file=trace_file)
        except Exception:
            logger.exception("フリーズ監視の更新でエラーが発生しました")
        root.after(FREEZE_HEARTBEAT_INTERVAL_MS, beat)

    beat()


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

    logger.info(
        f"RecR Ver{RecRApp.APP_VERSION} を起動しました"
        f"（{'exe' if getattr(sys, 'frozen', False) else 'Python ' + sys.version.split()[0]}）"
    )
    root = tk.Tk()
    _install_exception_logging(root)
    app = RecRApp(root)
    _install_freeze_trace(root)
    root.mainloop()
    faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    main()
