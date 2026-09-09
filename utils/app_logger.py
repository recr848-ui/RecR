"""アプリケーション共通のロギング設定"""
import logging
import sys
from logging.handlers import RotatingFileHandler

from utils.paths import get_base_dir

LOG_DIR = get_base_dir() / "config" / "logs"
LOG_FILE = LOG_DIR / "recr.log"

_configured = False


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

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    if sys.stdout is not None:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)
