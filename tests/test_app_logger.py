import logging
import threading
import time

from utils.app_logger import CONSOLE_QUEUE_MAX, build_queued_handlers


class _BlockingStream:
    """write() が release されるまで戻らない出力先（簡易編集モードで止まったコンソールの再現）"""

    def __init__(self):
        self.release = threading.Event()
        self.written = []

    def write(self, text):
        self.release.wait()
        self.written.append(text)

    def flush(self):
        pass


def _make_logger(tmp_path, stream):
    file_handler = logging.FileHandler(tmp_path / "recr.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    handlers, listeners = build_queued_handlers(file_handler, logging.StreamHandler(stream))
    logger = logging.getLogger(f"test_app_logger.{id(stream)}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    for handler in handlers:
        logger.addHandler(handler)
    return logger, listeners, file_handler


def test_logging_does_not_block_callers_while_console_is_stalled(tmp_path):
    """コンソールへの書き出しが止まっていても、ログを出したスレッドは待たされず、
    ファイルへのログも書かれ続ける（以前はここで画面ごと固まっていた）"""
    stream = _BlockingStream()
    logger, listeners, file_handler = _make_logger(tmp_path, stream)
    try:
        started = time.monotonic()
        for i in range(CONSOLE_QUEUE_MAX + 50):
            logger.info(f"line {i}")
        assert time.monotonic() - started < 2

        listeners[0].stop()  # ファイル側を書き出し終える
        lines = (tmp_path / "recr.log").read_text(encoding="utf-8").splitlines()
        assert lines[0] == "line 0"
        assert lines[-1] == f"line {CONSOLE_QUEUE_MAX + 49}"
    finally:
        stream.release.set()
        # 止めるための合図もキューに積むので、溜まった分が掃けるのを待ってから止める
        deadline = time.monotonic() + 5
        while not listeners[1].queue.empty() and time.monotonic() < deadline:
            time.sleep(0.01)
        listeners[1].stop()
        file_handler.close()

    assert stream.written  # 止まりが解けたらコンソールにも出る
