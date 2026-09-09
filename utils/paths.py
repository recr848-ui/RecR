"""アプリケーションの永続化データ用ベースディレクトリを求める

PyInstallerのonefileビルドでは、実行時にexeの中身が一時ディレクトリ
（sys._MEIPASS）に展開され、モジュールの __file__ はそこを指す。この
一時ディレクトリはexe終了時に自動削除されるため、設定・キャッシュ・
ログ等を永続化したいデータの保存先には使えない（次回起動時には消えている）。
そのため、実行ファイルが実際に置かれている場所を基準にする。
"""
import sys
from pathlib import Path


def get_base_dir():
    """設定・キャッシュ・ログの保存先の基準ディレクトリを返す

    - 通常のPython実行時: プロジェクトルート（このファイルの1つ上の階層）
    - PyInstallerでビルドされた実行ファイル: exe自身が置かれているディレクトリ
      （onefile/onedirいずれの場合も、展開先の一時ディレクトリではなく
      配布された実際のexeの場所になる）
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
