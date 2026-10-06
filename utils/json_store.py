"""JSONファイルの安全な書き込み

open(path, "w") で直接上書きすると、開いた瞬間にファイルが0バイトになり、
書き終わるまでの間にプロセスが強制終了・電源断されると中身が失われる
（予約一覧や設定が丸ごと消える）。そのため一旦別名の一時ファイルに書き切って
から os.replace で差し替える。差し替えは「古い内容のまま」か「新しい内容に
なった」のどちらかにしかならないため、どの時点で落ちても壊れたファイルは残らない。
"""
import json
import logging
import os
import time

logger = logging.getLogger(__name__)


def write_json_atomic(path, data):
    """dataをJSONとしてpathへ書き込む（途中で落ちても元のファイルは壊れない）

    一時ファイルは同じディレクトリに「<ファイル名>.tmp」で作る（os.replaceは
    同一ボリューム内でないと使えないため）。同じpathへ複数スレッドから同時に
    書き込む場合は、呼び出し側でロックして直列化すること。
    """
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())

    # Windowsでは、書いたばかりの一時ファイルや差し替え先を他のプロセス（ウイルス対策
    # ソフトの検査やバックアップソフト等）が開いている間、os.replace が PermissionError に
    # なる（PCの負荷が高いときに0.5秒以上続くことを実機で確認済み）。待ち時間を
    # 延ばしながら合計3秒ほど再試行する
    delay = 0.05
    for _ in range(10):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            time.sleep(delay)
            delay = min(delay * 2, 0.5)

    # それでも差し替えられない場合、保存自体を諦めるよりは、従来どおり直接上書きする
    # （この書き込みの途中で落ちると壊れ得るが、保存できないよりはよい）
    logger.warning(f"{path.name} を一時ファイルから差し替えられなかったため、直接上書きします")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.remove(tmp_path)
    except OSError:
        pass


def read_json(path):
    """pathのJSONを読み込んで返す

    差し替え（os.replace）の直後や、他のプロセスがファイルを掴んでいる一瞬の間は
    Windowsでは開くこと自体が PermissionError になることがある（強制終了直後の
    読み込みで実際に発生を確認済み）。これを「読めなかった＝空」として扱うと、
    その後の保存で中身を空で上書きしてしまうため、少し待って再試行する。
    """
    for attempt in range(20):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)
