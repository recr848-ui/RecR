"""アプリケーション共通のロギング設定

ログを出すスレッド（GUIのメインスレッド・録音・ダウンロード等）は、ファイルや
コンソールへの書き出しを直接は行わず、キューに積むだけにする。実際の書き出しは
出力先ごとの専用スレッドが行う。

以前は各スレッドが直接書き出していたため、コンソールへの書き出しが止まると
（Windowsのコンソールでは、ウィンドウ内をクリックして文字を選択する「簡易編集
モード」の間、出力が一時停止される）、そのとき書き出していたスレッドが出力用の
ロックを握ったまま止まり、以後ログを出そうとした全スレッドが待たされて、画面
ごと固まった（実機の freeze_trace.log で確認）。
"""
import atexit
import ctypes
import logging
import queue
import sys
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler

from utils.paths import get_base_dir

LOG_DIR = get_base_dir() / "config" / "logs"
LOG_FILE = LOG_DIR / "recr.log"

# コンソール出力が止まっている間に溜めておく件数の上限（超えた分はコンソールには出さない。
# ファイルには別のキューで書き出すため、ファイル側のログは欠けない）
CONSOLE_QUEUE_MAX = 1000

_configured = False


class _DroppingQueueHandler(QueueHandler):
    """キューが一杯なら待たずに捨てる QueueHandler（コンソール用）"""

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            pass


def build_queued_handlers(file_handler, stream_handler=None):
    """出力先のハンドラーを、キュー経由で別スレッドから書き出す形に包む

    Returns:
        (list, list): (ルートロガーに付けるハンドラー, 開始済みの QueueListener)
            QueueListener は [ファイル用, コンソール用] の順
    """
    handlers = []
    listeners = []

    file_queue = queue.SimpleQueue()
    handlers.append(QueueHandler(file_queue))
    listeners.append(QueueListener(file_queue, file_handler, respect_handler_level=True))

    if stream_handler is not None:
        # コンソールとファイルを同じスレッドで書き出すと、コンソールが止まったときに
        # ファイルへのログまで止まるため、スレッドを分ける
        console_queue = queue.Queue(maxsize=CONSOLE_QUEUE_MAX)
        handlers.append(_DroppingQueueHandler(console_queue))
        listeners.append(QueueListener(console_queue, stream_handler, respect_handler_level=True))

    for listener in listeners:
        listener.start()
    return handlers, listeners


def _disable_console_quick_edit():
    """Windowsのコンソールの「簡易編集モード」を切る

    有効なままだと、コンソールのウィンドウ内をクリックしただけで文字の選択が始まり、
    Escキー等で解除されるまでコンソールへの出力が止まる。ログはキュー経由にしたので
    アプリは固まらないが、その間のコンソールのログが捨てられるのを防ぐため切っておく。
    コンソールが無い場合（exeの配布版など）は何もしない。
    """
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        ENABLE_QUICK_EDIT_MODE = 0x0040
        ENABLE_EXTENDED_FLAGS = 0x0080
        kernel32.SetConsoleMode(handle, (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS)
    except (AttributeError, OSError):
        pass


def setup_logging(level=logging.INFO):
    """ログ出力を設定する（アプリ起動時に一度だけ呼び出す）

    config/logs/recr.log にローテート（5MB x 3世代）でログを書き出す。
    コンソールが存在する場合（開発時の実行等）は標準出力にも出す。
    PyInstallerのwindowedビルドではsys.stdoutがNoneになるため、その場合は
    ファイル出力のみになる。
    """
    global _configured
    if _configured:
        return
    _configured = True

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    stream_handler = None
    if sys.stdout is not None:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        _disable_console_quick_edit()

    handlers, listeners = build_queued_handlers(file_handler, stream_handler)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in handlers:
        root_logger.addHandler(handler)

    # 終了時にファイルへ書き残しが無いよう、ファイル側だけ書き出し終わるのを待つ。
    # コンソール側は止まっていると終了できなくなるため待たない（デーモンスレッド）
    atexit.register(listeners[0].stop)
