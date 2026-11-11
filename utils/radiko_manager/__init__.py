"""
ラジコマネージャーモジュール
ラジコストリーム録音と管理を処理します

RadikoManager は機能ごとの mixin（同じパッケージ内の各モジュール）を束ねたもの。
ここには認証・局一覧と、全 mixin が共有する状態の初期化だけを置く。
"""

import base64
import logging
import threading
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

from utils.paths import get_base_dir
from utils.radiko_manager.schedule_cache import ScheduleCacheMixin
from utils.radiko_manager.storage import StorageMixin
from utils.radiko_manager.freeword import FreewordMixin
from utils.radiko_manager.stream import StreamMixin
from utils.radiko_manager.playback import PlaybackMixin
from utils.radiko_manager.recording import RecordingMixin
from utils.radiko_manager.metadata import MetadataMixin
from utils.radiko_manager.schedule import ScheduleFetchMixin

from utils.radiko_manager.audio import _LiveAudioBuffer
from utils.radiko_manager.freeword import keyword_matches

# 他モジュール・テストから utils.radiko_manager 経由で参照される名前
__all__ = ["RadikoManager", "keyword_matches", "_LiveAudioBuffer"]

logger = logging.getLogger(__name__)


class RadikoManager(
    ScheduleCacheMixin,
    StorageMixin,
    FreewordMixin,
    StreamMixin,
    PlaybackMixin,
    RecordingMixin,
    MetadataMixin,
    ScheduleFetchMixin,
):
    """ラジコストリーム録音を管理"""

    # 接続元のエリア判定（auth1/auth2）に失敗した場合のフォールバック用局一覧（関東）
    _DEFAULT_STATION_MAPPING = {
        "NHK-FM": "JOAK-FM",
        "NHK-R1": "JOAK",
        "TBS": "TBS",
        "Fuji": "FM-FUJI",
        "Nikkei": "RN1",
        "文化放送": "QRR",
        "ニッポン放送": "LFR",
        "TOKYO FM": "FMT",
        "J-WAVE": "FMJ",
        "interfm": "INT",
        "ラジオ日本": "JORF",
        "bayfm78": "BAYFM78",
        "NACK5": "NACK5",
        "FMヨコハマ": "YFM",
        "茨城放送": "IBS"
    }

    # radikoクライアント認証用の固定キー（多くのOSS radikoクライアントで公知の値）。
    # auth1が返すkeyoffset/keylengthでこのキーの一部を切り出し、
    # partialkeyとしてauth2に送ることで正規クライアントとして認証される。
    _AUTH_KEY = "bcd151073c03b352e1ef2fd66c32209da9ca0afa"

    # グラフィックイコライザー表示用の周波数バンド数
    EQ_NUM_BANDS = 10

    # 番組表取得（1局分）で、複数日を同時に取得する際の並列数。
    # radiko側のレスポンスが遅いため並列化するが、上げすぎると相手サーバーに
    # 負荷をかけるため控えめな値にしている。
    SCHEDULE_DAY_FETCH_WORKERS = 5

    def __init__(self):
        self.radiko_api_url = "https://radiko.jp/v3/program/station/date"
        self.auth1_url = "https://radiko.jp/v2/api/auth1"
        self.auth2_url = "https://radiko.jp/v2/api/auth2"
        self.station_list_url = "https://radiko.jp/v3/station/list/{area_id}.xml"

        self.area_id = None
        self.area_name = None
        self.auth_token = None
        self.station_mapping = self._detect_area_stations() or dict(self._DEFAULT_STATION_MAPPING)

        self.stream_list_url = "https://radiko.jp/v3/station/stream/pc_html5/{station_id}.xml"
        self._playback_threads = []
        self._playback_stop_event = None
        self._playback_buffer = None
        self._levels_lock = threading.Lock()
        self._levels = [0.0] * self.EQ_NUM_BANDS
        self._lr_levels = [0.0, 0.0]
        self._levels_updated_at = 0.0

        self.output_dir = Path.home() / "Music" / "RecR"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # 局名 -> {'thread', 'stop_event', 'output_path'} で、局ごとに独立した録音を
        # 複数同時に管理する（局が異なれば並行録音でき、同じ局は多重録音しない）
        self._recordings = {}
        self._recordings_lock = threading.Lock()

        # "局名|ft" -> {'thread', 'stop_event', 'output_path'} で、タイムフリーの
        # ダウンロードをライブ録音とは別の名前空間で多重実行防止する
        self._downloads = {}
        self._downloads_lock = threading.Lock()
        # タイムフリーの同時ダウンロード数の上限（GUI側の設定で1〜10に変更可能。
        # 既定値はここ）
        self.max_concurrent_timefree_downloads = 3
        # タイムフリー1件あたりの並列分割数。1セッション（lsid）内では実時間ペース
        # からは逃れられないが、別セッションで別のft/to（時間区間）を指定すれば
        # 待ち時間なく即座にその地点から取得を開始できることを実機で確認済み。
        # そのため取得区間をこの数だけ分割し、別セッションで並列取得することで
        # 実質的にこの倍数分ダウンロードを高速化する
        self.timefree_parallel_splits = 4
        # 予約録音の開始・終了時刻に前後に足す余白秒数（GUI側の設定で0/15/30/45/60から
        # 選択可能。番組表の時刻と実際の配信のわずかなズレで冒頭・末尾が欠けるのを防ぐ）
        self.recording_margin_seconds = 0
        # 開始猶予を過ぎても始まらなかった予約の (予約ID, 対象日)。ログを1回だけ出すため
        self._missed_start_logged = set()

        self.cache_dir = get_base_dir() / "config"
        self.cache_file = self.cache_dir / "schedule_cache.json"
        # schedule_cache.jsonはキャッシュ済み局が増えると数MBになり、
        # 毎回ディスクから読み直してJSONパースし直すコストが無視できない。
        # 起動時の複数局チェックやフリーワード再照合など、短時間に局数分だけ
        # 繰り返し読む処理があるため、ファイルが変わっていない間はプロセス内の
        # メモリキャッシュを返す（更新はmtimeで検知）
        self._schedule_cache_mem = None
        self._schedule_cache_mtime = None
        # 複数局を並列取得する際（起動時のstale局チェック・全局自動更新）、
        # 各ワーカースレッドが save_schedule_cache で「読む→1局分だけ書き換える
        # →全体を書き戻す」を行うため、ロックなしだと他スレッドの書き込みを
        # 読み落として上書きし、その局のキャッシュが丸ごと消えてしまうことがある
        # （実際に発生を確認済み）。読み込み～書き戻しの区間をロックで直列化する
        self._schedule_cache_lock = threading.Lock()
        self.image_cache_dir = self.cache_dir / "images"
        self.settings_file = self.cache_dir / "settings.json"
        self.reservations_file = self.cache_dir / "reservations.json"
        self.freeword_file = self.cache_dir / "freeword_keywords.json"
        # 設定・予約一覧・フリーワード一覧をまとめた自動バックアップ（VerUP時の再設定を
        # 楽にするため、いずれかが変更されるたびに書き出す。手動エクスポート/インポート
        # とは別に、常に最新状態を保持する）
        self.export_file = self.cache_dir / "settings_export.json"

    def _authenticate(self):
        """radikoのauth1/auth2を実行し、接続元IPからエリアを判定する

        Returns:
            (str, str, str): (area_id, area_name, auth_token) 例: ("JP13", "東京都", "xxxx...")
        """
        headers1 = {
            "X-Radiko-App": "pc_html5",
            "X-Radiko-App-Version": "0.0.1",
            "X-Radiko-User": "dummy_user",
            "X-Radiko-Device": "pc",
        }
        res1 = requests.get(self.auth1_url, headers=headers1, timeout=5)
        res1.raise_for_status()

        auth_token = res1.headers["X-Radiko-AuthToken"]
        key_length = int(res1.headers["X-Radiko-KeyLength"])
        key_offset = int(res1.headers["X-Radiko-KeyOffset"])

        key_bytes = self._AUTH_KEY.encode("utf-8")
        partial_key = base64.b64encode(
            key_bytes[key_offset:key_offset + key_length]
        ).decode("utf-8")

        headers2 = {
            "X-Radiko-AuthToken": auth_token,
            "X-Radiko-Partialkey": partial_key,
            "X-Radiko-User": "dummy_user",
            "X-Radiko-Device": "pc",
        }
        res2 = requests.get(self.auth2_url, headers=headers2, timeout=5)
        res2.encoding = "utf-8"
        res2.raise_for_status()

        # レスポンス本文は "JP13,東京都,Tokyo Japan" のようなCSV1行
        area_id, area_name = res2.text.strip().split(",")[:2]
        return area_id, area_name, auth_token

    def _fetch_area_station_mapping(self, area_id):
        """指定エリアの局一覧を取得し、{表示名: 局ID} の辞書を返す"""
        url = self.station_list_url.format(area_id=area_id)
        response = requests.get(url, timeout=5)
        response.encoding = "utf-8"
        response.raise_for_status()
        root = ET.fromstring(response.content)

        mapping = {}
        for station in root.findall(".//station"):
            station_id = station.findtext("id")
            name = station.findtext("name")
            if station_id and name:
                mapping[name] = station_id
        return mapping

    def _detect_area_stations(self):
        """接続元IPからradikoのエリアを判定し、そのエリアの局一覧を取得する。

        失敗した場合（オフライン時など）は None を返し、呼び出し側は
        既定（関東）の局一覧にフォールバックする。
        """
        try:
            area_id, area_name, auth_token = self._authenticate()
            mapping = self._fetch_area_station_mapping(area_id)
            if not mapping:
                return None
            self.area_id = area_id
            self.area_name = area_name
            self.auth_token = auth_token
            return mapping
        except Exception:
            logger.exception("Error detecting radiko area")
            return None

    def get_stations(self):
        """利用可能なラジコステーションを取得"""
        return list(self.station_mapping.keys())

    def get_area_info(self):
        """判定された接続元エリアの情報を取得

        Returns:
            (str, str) または (None, None): (area_id, area_name)。判定できていない場合は (None, None)
        """
        return self.area_id, self.area_name

    def _ensure_authenticated(self):
        """ライブ再生・録音に必要な認証トークンを取得する

        radikoの認証トークンは一定時間で失効するため、前回取得したものを
        使い回さず、再生・録音を開始するたび（＝ここが呼ばれるたび）に
        必ず新しく取得し直す。アプリを起動しっぱなしにしていても、失効した
        古いトークンのまま再生・録音を試みて失敗し続ける（録音の場合は
        0バイトの空ファイルになる）ことがないようにするための対応。
        """
        area_id, area_name, auth_token = self._authenticate()
        self.area_id = area_id
        self.area_name = area_name
        self.auth_token = auth_token
        return self.auth_token

