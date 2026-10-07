"""
ラジコマネージャーモジュール
ラジコストリーム録音と管理を処理します
"""

import base64
import hashlib
import io
import json
import logging
import math
import os
import queue
import re
import threading
import time
from array import array
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import requests
from xml.etree import ElementTree as ET

from utils.json_store import read_json, write_json_atomic
from utils.paths import get_base_dir

logger = logging.getLogger(__name__)


def keyword_matches(keyword, haystack):
    """空白区切りのAND検索でキーワードがhaystackに一致するか判定する

    「A B」のようにスペース区切りで複数語を入力した場合、すべての語が
    haystackに含まれていれば一致とみなす。
    """
    terms = (keyword or '').strip().lower().split()
    if not terms:
        return False
    return all(term in haystack for term in terms)


# PCMはs16・ステレオ・インターリーブのバイト列で扱う（1フレーム = 2ch x 2バイト）
_PCM_BYTES_PER_FRAME = 4


def _frame_to_pcm(frame):
    """リサンプル済みのPyAVフレーム(s16, stereo)をPCMバイト列に変換する。

    planeのバッファにはアライメント用の余白が付く場合があるため、
    実サンプル数ぶんだけ切り出す。
    """
    return bytes(frame.planes[0])[:frame.samples * _PCM_BYTES_PER_FRAME]


_fft_cache = {}


def _fft_tables(n):
    """長さn(2のべき乗)のFFT用にビット反転順とひねり係数をキャッシュして返す"""
    tables = _fft_cache.get(n)
    if tables is None:
        bits = n.bit_length() - 1
        rev = [int(format(i, f"0{bits}b")[::-1], 2) for i in range(n)]
        twiddles = [complex(math.cos(-2 * math.pi * k / n), math.sin(-2 * math.pi * k / n))
                    for k in range(n // 2)]
        window = [0.5 - 0.5 * math.cos(2 * math.pi * k / (n - 1)) for k in range(n)]
        tables = (rev, twiddles, window)
        _fft_cache[n] = tables
    return tables


def _spectrum_magnitudes(samples):
    """実数列(長さは2のべき乗)にハン窓をかけた振幅スペクトル(n//2+1点)を返す。

    numpyに依存しないよう、反復型の基数2 FFTで計算する。
    """
    n = len(samples)
    rev, twiddles, window = _fft_tables(n)
    data = [complex(samples[r] * window[r]) for r in rev]
    size = 2
    while size <= n:
        half = size // 2
        step = n // size
        for start in range(0, n, size):
            for k in range(half):
                i = start + k
                j = i + half
                t = twiddles[k * step] * data[j]
                u = data[i]
                data[i] = u + t
                data[j] = u - t
        size *= 2
    return [abs(c) for c in data[:n // 2 + 1]]


class _LiveAudioBuffer:
    """ライブ再生用の音声チャンク(PCM)を貯めておく簡易バッファ。

    取得・デコードを行うfetchスレッドと、実際に音声を鳴らすoutputスレッドの
    間に挟むことで、ネットワークの遅延・瞬断がそのまま音切れに直結しないよう
    プリバッファ（再生開始前に一定秒数ためておく）を実現する。
    """

    def __init__(self):
        self._queue = queue.Queue()
        self._buffered_samples = 0
        self._lock = threading.Lock()
        self.sample_rate = None
        self._finished = False

    def push(self, pcm):
        """PCMチャンク(s16ステレオのバイト列)をバッファに追加する"""
        with self._lock:
            self._buffered_samples += len(pcm) // _PCM_BYTES_PER_FRAME
        self._queue.put(pcm)

    def mark_finished(self):
        """これ以上データが来ないことを示す（fetch側の終了時に呼ぶ）"""
        with self._lock:
            self._finished = True
        self._queue.put(None)

    def is_finished(self):
        with self._lock:
            return self._finished

    def buffered_seconds(self):
        """現在バッファに貯まっている音声の長さ（秒）"""
        with self._lock:
            if not self.sample_rate:
                return 0.0
            return self._buffered_samples / self.sample_rate

    def pop(self, timeout=1.0):
        """PCMチャンクを1つ取り出す。終了通知の場合は None を返す。

        Raises:
            queue.Empty: timeout秒以内にデータが来なかった場合
        """
        pcm = self._queue.get(timeout=timeout)
        if pcm is not None:
            with self._lock:
                self._buffered_samples -= len(pcm) // _PCM_BYTES_PER_FRAME
        return pcm


class RadikoManager:
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

    def _load_cache_file(self):
        """キャッシュファイル全体を読み込む（存在しない/壊れている場合は空dict）

        ファイルのmtimeが前回読み込み時と同じならメモリキャッシュを返し、
        ディスクI/OとJSONパースを省略する。呼び出し元が戻り値の辞書を
        直接書き換えてもメモリキャッシュ側に影響しないよう、浅いコピーを返す
        （値であるステーションごとの辞書は差し替えのみで直接編集されない前提）。
        """
        if not self.cache_file.exists():
            self._schedule_cache_mem = {}
            self._schedule_cache_mtime = None
            return {}

        try:
            mtime = self.cache_file.stat().st_mtime
        except OSError:
            mtime = None

        if self._schedule_cache_mem is not None and mtime == self._schedule_cache_mtime:
            return dict(self._schedule_cache_mem)

        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}

        self._schedule_cache_mem = data
        self._schedule_cache_mtime = mtime
        return dict(data)

    def _save_cache_file(self, cache):
        """キャッシュファイル全体を書き込む"""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.cache_file, cache)
        self._schedule_cache_mem = cache
        try:
            self._schedule_cache_mtime = self.cache_file.stat().st_mtime
        except OSError:
            self._schedule_cache_mtime = None

    def load_cached_schedule(self, station_name):
        """ステーションのキャッシュ済み番組表を読み込む

        Returns:
            list または None: キャッシュがなければ None
        """
        cache = self._load_cache_file()
        entry = cache.get(station_name)
        if not entry:
            return None
        return entry.get("programs")

    def save_schedule_cache(self, station_name, programs):
        """ステーションの番組表をキャッシュに保存

        複数局を並列取得中は他スレッドも同時にこのメソッドを呼ぶため、
        「読み込み→1局分書き換え→書き戻し」の区間はロックで直列化する
        （でないと他スレッドの更新を読み落として上書きしてしまう）。
        """
        with self._schedule_cache_lock:
            cache = self._load_cache_file()
            cache[station_name] = {
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "programs": programs
            }
            self._save_cache_file(cache)

    def count_cached_future_days(self, station_name):
        """キャッシュのうち当日以降の日数を数える（キャッシュがなければ0）

        date_iso を持たない古い形式のキャッシュは日数を数えられないため 0 として扱う
        （＝自動更新の対象になる）。
        """
        cached = self.load_cached_schedule(station_name)
        if not cached:
            return 0
        today_iso = datetime.now().strftime("%Y-%m-%d")
        future_dates = {
            p.get('date_iso') for p in cached
            if p.get('date_iso') and p['date_iso'] >= today_iso
        }
        return len(future_dates)

    def prune_stale_schedule_cache(self):
        """schedule_cache.json から、現在の局一覧に存在しない局名のキーを削除する

        局名の表記変更（例: 大文字小文字の変更、局名の改称）があると、古い表記の
        キーが二度と上書きされることなくキャッシュに残り続けてしまう。全局自動更新の
        たびに、そうした孤立エントリを掃除する。タイムフリー用キー
        （"{局名}::timefree"）も、対応する局が現在の一覧にあるかで判定する。

        Returns:
            list: 削除したキーの一覧
        """
        with self._schedule_cache_lock:
            cache = self._load_cache_file()
            current_stations = set(self.station_mapping.keys())

            removed = []
            for key in list(cache.keys()):
                station_name = key[:-len("::timefree")] if key.endswith("::timefree") else key
                if station_name not in current_stations:
                    removed.append(key)
                    del cache[key]

            if removed:
                self._save_cache_file(cache)
                logger.info(f"番組表キャッシュの孤立エントリを削除しました: {', '.join(removed)}")

        return removed

    def _image_cache_path(self, url):
        """画像URLに対応するキャッシュファイルパスを求める"""
        digest = hashlib.md5(url.encode("utf-8")).hexdigest()
        ext = os.path.splitext(url.split("?")[0])[1]
        if not ext or len(ext) > 5:
            ext = ".jpg"
        return self.image_cache_dir / f"{digest}{ext}"

    def get_image(self, url):
        """番組画像を取得する（ディスクキャッシュ優先、なければダウンロードして保存）

        Args:
            url (str): 番組画像のURL

        Returns:
            bytes または None: 取得できなかった場合は None
        """
        if not url:
            return None

        cache_path = self._image_cache_path(url)
        if cache_path.exists():
            try:
                return cache_path.read_bytes()
            except OSError:
                pass

        try:
            response = requests.get(url, timeout=5)
            if response.status_code != 200:
                return None
            data = response.content
        except Exception:
            logger.exception(f"Error fetching image {url}")
            return None

        try:
            self.image_cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(data)
        except OSError:
            logger.exception(f"Error caching image {url}")

        return data

    # 番組画像のディスクキャッシュ（config/images）の上限。番組が入れ替わるたびに
    # 新しい画像が増えていくため、上限なしだと使い続ける限り際限なく膨らむ
    IMAGE_CACHE_MAX_BYTES = 100 * 1024 * 1024

    def prune_image_cache(self):
        """番組画像のディスクキャッシュが上限を超えていたら、古いものから削除する

        毎回上限ぎりぎりで削除を繰り返さないよう、上限の8割まで減らす。削除した
        画像がまた必要になった場合は get_image が取得し直すだけなので実害はない。

        Returns:
            int: 削除したファイル数
        """
        try:
            with os.scandir(self.image_cache_dir) as it:
                entries = [
                    (entry.stat().st_mtime, entry.stat().st_size, entry.path)
                    for entry in it if entry.is_file()
                ]
        except OSError:
            return 0

        total = sum(size for _, size, _ in entries)
        if total <= self.IMAGE_CACHE_MAX_BYTES:
            return 0

        removed = 0
        for _, size, path in sorted(entries):
            if total <= self.IMAGE_CACHE_MAX_BYTES * 0.8:
                break
            try:
                os.remove(path)
            except OSError:
                continue
            total -= size
            removed += 1
        logger.info(f"番組画像キャッシュを整理しました（{removed}件削除）")
        return removed

    def load_settings(self):
        """アプリ設定（既定局・番組表取得日数など）を読み込む

        Returns:
            dict: 設定がなければ空dict
        """
        return self._load_config_file(self.settings_file, "settings", {})

    def save_settings(self, settings):
        """アプリ設定を保存する（既存の設定とマージ）"""
        with self._config_lock:
            current = self.load_settings()
            current.update(settings)
            self._save_config_file(self.settings_file, current)

    # 設定・予約一覧・フリーワード一覧の読み書きを直列化するロック。
    # 「読む→書き換える→書き戻す」の途中に別スレッドの書き込みが割り込んで
    # 変更が失われるのを防ぐ。_export_backup等から入れ子で取得するためRLock
    _config_lock = threading.RLock()

    def _load_config_file(self, path, backup_key, default):
        """設定・予約一覧・フリーワード一覧のいずれかを読み込む（無ければdefault）

        中身が壊れていて読めない場合、黙って空として扱うと、その後の保存で
        自動バックアップ（settings_export.json）まで空で上書きされて復旧できなく
        なる。そのため壊れたファイルは「<ファイル名>.corrupt」として退避し、
        自動バックアップ内の該当部分（backup_key）から復元する。
        """
        with self._config_lock:
            if not path.exists():
                return default
            try:
                return read_json(path)
            except OSError:
                logger.exception(f"{path.name} を読み込めませんでした")
                return default
            except ValueError:
                # JSONDecodeError（途中で切れている等）とUnicodeDecodeError（文字化け）の両方
                logger.error(f"{path.name} が壊れています。自動バックアップからの復元を試みます")

            try:
                os.replace(path, path.with_name(path.name + ".corrupt"))
            except OSError:
                logger.exception(f"壊れた {path.name} を退避できませんでした")

            restored = self._read_backup_section(backup_key)
            if not isinstance(restored, type(default)):
                logger.error(f"{path.name} を自動バックアップから復元できませんでした（空として扱います）")
                return default
            try:
                write_json_atomic(path, restored)
                logger.warning(f"{path.name} を自動バックアップから復元しました")
            except OSError:
                logger.exception(f"復元した {path.name} を書き戻せませんでした")
            return restored

    def _read_backup_section(self, backup_key):
        """自動バックアップ（settings_export.json）から指定キーの内容を取り出す（読めなければNone）"""
        try:
            return read_json(self.export_file).get(backup_key)
        except (OSError, ValueError, AttributeError):
            return None

    def _save_config_file(self, path, data):
        """設定・予約一覧・フリーワード一覧のいずれかを保存し、自動バックアップも更新する"""
        with self._config_lock:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            write_json_atomic(path, data)
            self._export_backup()

    def _export_backup(self):
        """設定・予約一覧・フリーワード一覧をまとめて自動バックアップファイルに書き出す

        設定変更時、および予約一覧・フリーワード一覧の更新時に毎回呼び出される。
        書き込みに失敗してもアプリ本来の動作は継続させたいため、例外は握りつぶす。
        """
        with self._config_lock:
            data = {
                "settings": self.load_settings(),
                "reservations": self._load_reservations_file(),
                "freeword_keywords": self._load_freeword_file(),
            }
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                write_json_atomic(self.export_file, data)
            except OSError:
                logger.exception("Error writing settings_export.json backup")

    # 予約時刻を過ぎてもスケジューラの巡回間隔等の遅れを許容して録音を開始する猶予（秒）
    RESERVATION_GRACE_SECONDS = 180

    def _load_reservations_file(self):
        return self._load_config_file(self.reservations_file, "reservations", [])

    def _save_reservations_file(self, reservations):
        self._save_config_file(self.reservations_file, reservations)

    def replace_reservations(self, reservations):
        """予約録音の一覧を丸ごと置き換える（設定インポート用）"""
        self._save_reservations_file(reservations)

    def load_reservations(self):
        """予約録音の一覧を読み込む"""
        return self._load_reservations_file()

    def get_reservation(self, reservation_id):
        """IDを指定して予約を1件取得する（無ければNone）"""
        for res in self._load_reservations_file():
            if res.get('id') == reservation_id:
                return res
        return None

    def add_reservation(self, data):
        """予約録音を新規追加する。dataにidを付与して保存する"""
        with self._config_lock:
            reservations = self._load_reservations_file()
            reservation = dict(data)
            reservation['id'] = uuid.uuid4().hex
            reservation.setdefault('enabled', True)
            reservation.setdefault('last_run_date', None)
            reservations.append(reservation)
            self._save_reservations_file(reservations)
            return reservation

    def update_reservation(self, reservation_id, data):
        """既存の予約録音を更新する（dataの内容をマージ）"""
        with self._config_lock:
            reservations = self._load_reservations_file()
            for res in reservations:
                if res.get('id') == reservation_id:
                    res.update(data)
                    break
            self._save_reservations_file(reservations)

    def delete_reservation(self, reservation_id):
        """予約録音を削除する"""
        with self._config_lock:
            reservations = self._load_reservations_file()
            reservations = [r for r in reservations if r.get('id') != reservation_id]
            self._save_reservations_file(reservations)

    def mark_reservation_run(self, reservation_id, occurrence_date_iso, result=None):
        """予約録音を実行したことを記録する（同じ回の重複実行を防ぐため）

        Args:
            result (str): 'success' または 'failed'。指定した場合、一覧表示用に
                直近の実行結果として保存する
        """
        data = {'last_run_date': occurrence_date_iso}
        if result is not None:
            data['last_result'] = result
        self.update_reservation(reservation_id, data)

    def _reservation_occurrence_date(self, reservation, now):
        """予約が今日実行されるべきものかどうかを判定し、対象日を返す（対象外ならNone）"""
        if reservation.get('repeat') == 'weekly':
            if reservation.get('weekday') == now.weekday():
                return now.date()
            return None

        date_iso = reservation.get('date_iso')
        if not date_iso:
            return None
        try:
            target = datetime.strptime(date_iso, "%Y-%m-%d").date()
        except ValueError:
            return None
        return target if target == now.date() else None

    def get_due_reservations(self):
        """今まさに開始すべき（開始時刻を過ぎて猶予時間内であり、終了時刻はまだ来ていない）
        有効な予約録音の一覧を返す

        recording_margin_seconds が設定されていれば、実際に録音すべき開始・終了時刻
        （record_start_dt/record_end_dt）はその分だけ前後に広げる（番組表の時刻と実際の
        配信のわずかなズレを吸収するため）。occurrence_iso（重複実行防止・状態記録に
        使う対象日）はマージンの影響を受けない、番組本来の予定日のまま。

        Returns:
            list: [(reservation_dict, record_start_dt, record_end_dt, occurrence_iso), ...]
        """
        now = datetime.now()
        margin = timedelta(seconds=self.recording_margin_seconds)
        due = []
        for res in self._load_reservations_file():
            if not res.get('enabled', True):
                continue

            occurrence_date = self._reservation_occurrence_date(res, now)
            if occurrence_date is None:
                continue
            occurrence_iso = occurrence_date.isoformat()
            if res.get('last_run_date') == occurrence_iso:
                continue

            try:
                start_dt = datetime.combine(
                    occurrence_date,
                    datetime.strptime(res['start'], "%H:%M").time()
                )
                end_dt = datetime.combine(
                    occurrence_date,
                    datetime.strptime(res['end'], "%H:%M").time()
                )
            except (KeyError, ValueError):
                continue
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)

            record_start_dt = start_dt - margin
            record_end_dt = end_dt + margin

            grace_end = record_start_dt + timedelta(seconds=self.RESERVATION_GRACE_SECONDS)
            if record_start_dt <= now <= grace_end and now < record_end_dt:
                due.append((res, record_start_dt, record_end_dt, occurrence_iso))

        return due

    def _load_freeword_file(self):
        return self._load_config_file(self.freeword_file, "freeword_keywords", [])

    def _save_freeword_file(self, freewords):
        self._save_config_file(self.freeword_file, freewords)

    def replace_freewords(self, freewords):
        """フリーワードの一覧を丸ごと置き換える（設定インポート用）"""
        self._save_freeword_file(freewords)

    def load_freewords(self):
        """フリーワード（キーワード自動録音）の一覧を読み込む"""
        return self._load_freeword_file()

    def get_freeword(self, freeword_id):
        """IDを指定してフリーワードを1件取得する（無ければNone）"""
        for fw in self._load_freeword_file():
            if fw.get('id') == freeword_id:
                return fw
        return None

    def add_freeword(self, data):
        """フリーワードを新規追加する。dataにidを付与して保存する"""
        freewords = self._load_freeword_file()
        freeword = dict(data)
        freeword['id'] = uuid.uuid4().hex
        freeword.setdefault('enabled', True)
        freeword.setdefault('stations', [])
        freewords.append(freeword)
        self._save_freeword_file(freewords)
        return freeword

    def update_freeword(self, freeword_id, data):
        """既存のフリーワードを更新する（dataの内容をマージ）"""
        freewords = self._load_freeword_file()
        for fw in freewords:
            if fw.get('id') == freeword_id:
                fw.update(data)
                break
        self._save_freeword_file(freewords)

    def delete_freeword(self, freeword_id):
        """フリーワードを削除する（既に自動作成済みの予約はそのまま残す）"""
        freewords = self._load_freeword_file()
        freewords = [f for f in freewords if f.get('id') != freeword_id]
        self._save_freeword_file(freewords)

    def _actual_calendar_date_iso(self, date_iso, start_hhmm):
        """番組の date_iso（放送日、5:00始まり）と start（HH:MM）から、
        実際のカレンダー上の日付を求める（0-4時台の番組は翌カレンダー日になるため）
        """
        try:
            target_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
            hour = int(start_hhmm.split(":")[0])
        except (ValueError, TypeError, AttributeError, IndexError):
            return date_iso
        if hour < 5:
            target_date += timedelta(days=1)
        return target_date.strftime("%Y-%m-%d")

    def _program_start_datetime(self, program):
        """番組の実際の放送開始datetimeを求める（不正なデータならNone）"""
        date_iso = program.get('date_iso')
        start = program.get('start')
        if not date_iso or not start:
            return None
        actual_date_iso = self._actual_calendar_date_iso(date_iso, start)
        try:
            base_date = datetime.strptime(actual_date_iso, "%Y-%m-%d").date()
            return datetime.combine(base_date, datetime.strptime(start, "%H:%M").time())
        except ValueError:
            return None

    def scan_freewords_for_station(self, station, programs):
        """指定局の番組表をフリーワードと照合し、一致する未来の番組を自動的に
        1回のみの予約録音として登録する。

        既に同一の予約（手動・他フリーワード由来を問わず、局/実際の放送日/開始時刻/
        番組名が一致するもの）が存在する場合は二重登録しない。

        Returns:
            list: 新規作成された予約録音データのリスト
        """
        freewords = [
            fw for fw in self.load_freewords()
            if fw.get('enabled', True) and fw.get('keyword', '').strip()
            and station in fw.get('stations', [])
        ]
        if not freewords or not programs:
            return []

        now = datetime.now()
        existing_keys = {
            (res.get('station'), res.get('date_iso'), res.get('start'), res.get('title'))
            for res in self._load_reservations_file()
        }

        created = []
        for program in programs:
            start_dt = self._program_start_datetime(program)
            if not start_dt or start_dt <= now:
                continue

            title = program.get('title') or ''
            haystack = " ".join([
                title, program.get('desc') or '', program.get('pfm') or ''
            ]).lower()

            matched = next(
                (fw for fw in freewords if keyword_matches(fw['keyword'], haystack)), None
            )
            if not matched:
                continue

            actual_date_iso = self._actual_calendar_date_iso(program.get('date_iso'), program.get('start'))
            key = (station, actual_date_iso, program.get('start'), title)
            if key in existing_keys:
                continue

            reservation = self.add_reservation({
                'station': station,
                'repeat': 'once',
                'date_iso': actual_date_iso,
                'start': program.get('start'),
                'end': program.get('end'),
                'title': title,
                'source': 'freeword',
                'keyword_id': matched['id'],
            })
            existing_keys.add(key)
            created.append(reservation)

        return created

    def search_cached_programs_by_keyword(self, keyword, stations, future_only=True):
        """キャッシュ済み番組表から、フリーワードと同じ一致ロジック（タイトル＋概要＋
        出演者への部分一致、大文字小文字を区別しない）でキーワードに一致する番組を探す。

        実際に予約が作られるかどうかを保存前に確認できるよう、フリーワード登録
        ダイアログのプレビュー表示用に用意した検索専用メソッド（このメソッド自体は
        予約を作成しない）。

        Args:
            keyword (str): 検索キーワード
            stations (list): 検索対象の局名リスト
            future_only (bool): Trueなら放送開始前の番組のみを対象にする
                （＝実際にフリーワードで自動予約される番組と一致する範囲）

        Returns:
            list: [(station, program), ...]（キャッシュの並び順、局の指定順）
        """
        keyword = (keyword or '').strip().lower()
        if not keyword:
            return []

        now = datetime.now()
        results = []
        for station in stations:
            cached = self.load_cached_schedule(station) or []
            for program in cached:
                if future_only:
                    start_dt = self._program_start_datetime(program)
                    if not start_dt or start_dt <= now:
                        continue

                haystack = " ".join([
                    program.get('title') or '', program.get('desc') or '', program.get('pfm') or ''
                ]).lower()
                if keyword_matches(keyword, haystack):
                    results.append((station, program))

        return results

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

    def _get_live_playlist_url(self, station_id):
        """指定局のライブ配信プレイリストURL（エリアフリーでない通常のライブ配信）を1件取得する

        radikoが返すXMLでは areafree/timefree は <url> タグの属性である点に注意
        （例: <url areafree="0" timefree="0"><playlist_create_url>...</playlist_create_url></url>）。
        """
        url = self.stream_list_url.format(station_id=station_id)
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        root = ET.fromstring(response.content)

        for url_elem in root.findall(".//url"):
            if url_elem.get("areafree") == "0" and url_elem.get("timefree") == "0":
                playlist_create_url = url_elem.findtext("playlist_create_url")
                if playlist_create_url:
                    return playlist_create_url
        return None

    def _get_timefree_playlist_url(self, station_id):
        """指定局のタイムフリー配信プレイリストURL（エリアフリーでない通常のタイムフリー）を1件取得する

        _get_live_playlist_url と同じXMLから、timefree="1" のurl要素を見る点だけが異なる。
        """
        url = self.stream_list_url.format(station_id=station_id)
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        root = ET.fromstring(response.content)

        for url_elem in root.findall(".//url"):
            if url_elem.get("areafree") == "0" and url_elem.get("timefree") == "1":
                playlist_create_url = url_elem.findtext("playlist_create_url")
                if playlist_create_url:
                    return playlist_create_url
        return None

    # 再生開始前にためておく音声バッファ量（秒）。ネットワークの瞬断・遅延を
    # 吸収し、音切れ（アンダーラン）を減らすためのプリバッファ。
    PLAYBACK_PREBUFFER_SECONDS = 5.0

    def play_live(self, station_name):
        """指定局のライブ配信を再生する（PyAVでデコードし、sounddeviceで出力）。

        ffmpeg/ffplay本体のインストールは不要。PyAVがffmpegのデコード用DLLを
        wheelに同梱しているため、それを直接ライブラリとして呼び出す方式。

        取得・デコード（fetchスレッド）と音声出力（outputスレッド）を分離し、
        間に PLAYBACK_PREBUFFER_SECONDS 秒分の音声バッファを挟むことで、
        ネットワークの遅延・瞬断が直接音切れにつながらないようにしている。

        Returns:
            bool: 再生開始に成功した場合True
        """
        station_id = self.station_mapping.get(station_name)
        if not station_id:
            return False

        self.stop_playback()

        try:
            auth_token = self._ensure_authenticated()
            playlist_base_url = self._get_live_playlist_url(station_id)
        except Exception:
            logger.exception(f"Error preparing live stream for {station_name}")
            return False

        if not playlist_base_url:
            return False

        lsid = uuid.uuid4().hex
        playlist_url = f"{playlist_base_url}?station_id={station_id}&l=15&lsid={lsid}&type=b"

        self._playback_stop_event = threading.Event()
        self._playback_buffer = _LiveAudioBuffer()
        fetch_thread = threading.Thread(
            target=self._playback_fetch_worker,
            args=(playlist_url, auth_token, self._playback_stop_event, self._playback_buffer),
            daemon=True,
        )
        output_thread = threading.Thread(
            target=self._playback_output_worker,
            args=(self._playback_stop_event, self._playback_buffer),
            daemon=True,
        )
        self._playback_threads = [fetch_thread, output_thread]
        fetch_thread.start()
        output_thread.start()
        return True

    # 配信の取得中に通信エラーが起きたとき、諦めるまでに再試行を続ける秒数。
    # 一瞬の瞬断やルーターの再起動程度（1〜2分）なら録音を打ち切らずに乗り切る
    FETCH_RETRY_BUDGET_SECONDS = 120
    # 再試行の待ち時間（秒）。1秒から始めて倍々に延ばし、この値で頭打ちにする
    FETCH_RETRY_MAX_DELAY_SECONDS = 10

    def _get_with_retry(self, get, url, stop_event, **kwargs):
        """get(url, **kwargs) を、一時的な通信エラーなら再試行しながら実行する

        再試行するのは接続エラー・タイムアウト・HTTP 5xx のみ。4xx（認証切れや
        セッション切れ等）は待っても直らないため、そのままレスポンスを返して
        呼び出し側の判断に任せる。FETCH_RETRY_BUDGET_SECONDS の間ずっと失敗し
        続けた場合は、最後の例外を送出する（5xxなら最後のレスポンスを返す）。

        get は requests.get または requests.Session.get。

        Returns:
            Response または None: 再試行の待機中に stop_event がセットされた
            （停止要求があった）場合は None
        """
        deadline = time.monotonic() + self.FETCH_RETRY_BUDGET_SECONDS
        delay = 1.0
        retried = False
        while True:
            res = None
            try:
                res = get(url, **kwargs)
                if res.status_code < 500:
                    if retried:
                        logger.info("通信が復旧しました。取得を再開します")
                    return res
                failure = f"HTTP {res.status_code}"
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if time.monotonic() + delay > deadline:
                    raise
                failure = type(e).__name__

            if res is not None and time.monotonic() + delay > deadline:
                return res
            logger.warning(f"通信エラー（{failure}）のため{delay:.0f}秒後に再試行します")
            retried = True
            if stop_event.wait(delay):
                return None
            delay = min(delay * 2, self.FETCH_RETRY_MAX_DELAY_SECONDS)

    def _get_live_medialist_url(self, playlist_url, headers, stop_event):
        """マスタープレイリストを取得し、そこに書かれたmedialist(session付)のURLを返す

        Returns:
            str または None: 停止要求があった場合は None
        """
        top_res = self._get_with_retry(
            requests.get, playlist_url, stop_event, headers=headers, timeout=10
        )
        if top_res is None:
            return None
        top_res.raise_for_status()
        return next(
            line.strip() for line in top_res.text.splitlines()
            if line.strip() and not line.startswith("#")
        )

    def _iter_live_segments(self, playlist_url, headers, stop_event):
        """マスタープレイリストを起点に、ライブ配信の新規セグメント(生のAACバイト列)を
        順次生成するジェネレーター。

        radikoのHLS配信は「マスタープレイリスト → メディアリスト(session付) → .aacセグメント」
        という構成。再生・録音の両方でこのジェネレーターを共有する。
        """
        medialist_url = self._get_live_medialist_url(playlist_url, headers, stop_event)
        if medialist_url is None:
            return

        last_sequence = -1
        session_renewed = False
        while not stop_event.is_set():
            media_res = self._get_with_retry(
                requests.get, medialist_url, stop_event, headers=headers, timeout=10
            )
            if media_res is None:
                return
            if 400 <= media_res.status_code < 500 and not session_renewed:
                # 通信断が長引くと、その間にradiko側でセッションが期限切れになり、
                # 復旧後のmedialistが404になる（実機で確認済み）。マスタープレイリスト
                # からセッションを取り直して続行する。取り直した直後も4xxなら諦める
                logger.warning(
                    f"ライブ配信: medialistが{media_res.status_code}になったため、"
                    "セッションを取り直します"
                )
                medialist_url = self._get_live_medialist_url(playlist_url, headers, stop_event)
                if medialist_url is None:
                    return
                # 新しいセッションではシーケンス番号が引き継がれる保証がない
                last_sequence = -1
                session_renewed = True
                continue
            media_res.raise_for_status()
            session_renewed = False

            media_sequence = 0
            target_duration = 5.0
            segment_urls = []
            for line in media_res.text.splitlines():
                if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    media_sequence = int(line.split(":", 1)[1])
                elif line.startswith("#EXT-X-TARGETDURATION:"):
                    target_duration = float(line.split(":", 1)[1])
                elif line and not line.startswith("#"):
                    segment_urls.append(line.strip())

            new_segments = [
                (media_sequence + i, seg_url)
                for i, seg_url in enumerate(segment_urls)
                if media_sequence + i > last_sequence
            ]

            # ライブのmedialistは直近の数セグメントしか載せていないため、通信断が
            # 長引くとその間のセグメントは流れて消え、復旧後も取得できない
            if last_sequence >= 0 and new_segments and new_segments[0][0] > last_sequence + 1:
                logger.warning(
                    f"ライブ配信: 通信断等により{new_segments[0][0] - last_sequence - 1}件の"
                    "セグメントを取得できませんでした（その区間の音声は欠落します）"
                )

            for seq, seg_url in new_segments:
                if stop_event.is_set():
                    return
                seg_res = self._get_with_retry(requests.get, seg_url, stop_event, timeout=10)
                if seg_res is None:
                    return
                if seg_res.status_code == 200:
                    yield seg_res.content
                else:
                    logger.warning(
                        f"ライブセグメント取得失敗: status={seg_res.status_code} url={seg_url}"
                    )
                last_sequence = seq

            if stop_event.is_set():
                return
            time.sleep(max(target_duration / 2, 1))

    def _carry_over_query_params(self, source_url, target_url, keys):
        """source_url が持つ指定キーのクエリパラメータを、target_url にコピーする
        （target_url が既にそのキーを持っていれば上書きしない）
        """
        source_params = parse_qs(urlparse(source_url).query)
        parsed_target = urlparse(target_url)
        target_params = parse_qs(parsed_target.query)
        for key in keys:
            if key not in target_params and key in source_params:
                target_params[key] = source_params[key]
        new_query = urlencode(target_params, doseq=True)
        return urlunparse(parsed_target._replace(query=new_query))

    def _parse_medialist(self, media_text):
        """メディアリスト(m3u8)本文から (media_sequence, target_duration, セグメント情報のlist) を返す

        セグメント情報は (連番, セグメントURL, そのセグメントの#EXT-X-PROGRAM-DATE-TIME文字列
        またはNone) のタプル。
        """
        media_sequence = 0
        target_duration = 5.0
        entries = []
        pending_dt = None
        index = 0
        for line in media_text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                media_sequence = int(line.split(":", 1)[1])
            elif line.startswith("#EXT-X-TARGETDURATION:"):
                target_duration = float(line.split(":", 1)[1])
            elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                pending_dt = line.split(":", 1)[1]
            elif line.startswith("#"):
                continue
            else:
                entries.append((media_sequence + index, line, pending_dt))
                index += 1
                pending_dt = None
        return media_sequence, target_duration, entries

    def _parse_program_date_time(self, value):
        """#EXT-X-PROGRAM-DATE-TIME の値（例: "2026-09-10T05:00:00.004+09:00"）を
        タイムゾーン情報なしのdatetimeにして返す（radikoのft/toはJSTのローカル時刻として
        扱っているため、比較しやすいようtzinfoを落とす）。パースできなければNone。
        """
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    def _split_time_range(self, ft_dt, to_dt, num_splits):
        """[ft_dt, to_dt) を num_splits 個の連続する区間に均等分割する

        Returns:
            list[(datetime, datetime)]: 区間の (開始, 終了) のリスト（時系列順）
        """
        total_seconds = (to_dt - ft_dt).total_seconds()
        step = total_seconds / num_splits
        bounds = [ft_dt + timedelta(seconds=round(step * i)) for i in range(num_splits)]
        bounds.append(to_dt)
        return list(zip(bounds[:-1], bounds[1:]))

    def _iter_timefree_segments_parallel(self, playlist_base_url, station_id, auth_token,
                                          ft_dt, to_dt, stop_event, on_progress=None,
                                          num_splits=4):
        """タイムフリーの取得区間を num_splits 分割し、それぞれ別セッション（別lsid）で
        並列に取得することで、実質的に num_splits 倍速でダウンロードするジェネレーター。

        _iter_timefree_segments は1セッション内では実時間ペースからは逃れられない
        （連続ポーリングしても待たない限り新規セグメントは増えない）が、別セッションで
        別のft/toを指定すれば、待ち時間なくその地点から即座に取得を開始できることを
        実機で確認済み。そのため区間ごとに独立したスレッド・セッションで並列取得し、
        時系列順（区間0→1→2...）を保ったまま生のAACバイト列を生成し直す。

        並列に取得したデータは一旦区間ごとのQueueに貯め、呼び出し側へは区間の順番通りに
        取り出して渡すため、出力される音声の時系列は分割前と変わらない。
        """
        ranges = self._split_time_range(ft_dt, to_dt, num_splits)
        result_queues = [queue.Queue() for _ in ranges]
        progress_lock = threading.Lock()
        sub_done = [0] * len(ranges)
        sub_total = [0] * len(ranges)

        def make_sub_progress(idx):
            def sub_progress(done, total):
                with progress_lock:
                    sub_done[idx] = done
                    sub_total[idx] = total
                    if on_progress:
                        on_progress(sum(sub_done), sum(sub_total))
            return sub_progress

        def worker(idx, sub_ft_dt, sub_to_dt):
            to_str = sub_to_dt.strftime("%Y%m%d%H%M%S")

            def build_playlist_url(from_dt):
                # セッション（lsid）ごとに別のURLにする。通信断でセッションが切れた
                # ときは、続きの時刻を from_dt にして同じ形で作り直す
                lsid = uuid.uuid4().hex
                ft_str = from_dt.strftime("%Y%m%d%H%M%S")
                return (
                    f"{playlist_base_url}?station_id={station_id}&start_at={ft_str}&ft={ft_str}"
                    f"&end_at={to_str}&to={to_str}&l=15&lsid={lsid}&type=b"
                )

            headers = {"X-Radiko-AuthToken": auth_token}
            try:
                for chunk in self._iter_timefree_segments(
                    build_playlist_url(sub_ft_dt), headers, stop_event, sub_to_dt,
                    on_progress=make_sub_progress(idx),
                    renew_playlist_url=build_playlist_url,
                ):
                    result_queues[idx].put(("data", chunk))
            except Exception as e:
                result_queues[idx].put(("error", e))
            finally:
                result_queues[idx].put(("done", None))

        threads = [
            threading.Thread(target=worker, args=(i, r[0], r[1]), daemon=True)
            for i, r in enumerate(ranges)
        ]
        for t in threads:
            t.start()

        try:
            for idx in range(len(ranges)):
                while True:
                    kind, payload = result_queues[idx].get()
                    if kind == "data":
                        yield payload
                    elif kind == "error":
                        raise payload
                    else:
                        break
        finally:
            # 呼び出し側が早期に打ち切った場合（例外・途中終了）でも、
            # 残っている区間のスレッドを確実に止めてから返す
            stop_event.set()
            for t in threads:
                t.join(timeout=5)

    # タイムフリーの取得がこの秒数以内まで to_dt に迫っていれば、medialistの404や
    # 新規セグメントの枯渇を「番組の終わりに達した」とみなす。これより手前で
    # 起きた場合は途中で切れたものとして扱う
    TIMEFREE_END_TOLERANCE_SECONDS = 15

    def _iter_timefree_segments(self, playlist_url, headers, stop_event, to_dt, on_progress=None,
                                renew_playlist_url=None):
        """タイムフリープレイリストの全セグメント(生のAACバイト列)を順に生成するジェネレーター。

        タイムフリーのmedialistは、1回の取得ではプレイリストURLのl(秒数)パラメータ分の
        短い窓（実測でl=15なら約3セグメント分）しか返さない。radikoのタイムフリーは内部的には
        「ftを起点に巻き戻したライブストリーム」として扱われているらしく、ライブ用の
        _iter_live_segments と同様に medialist を繰り返しポーリングし、#EXT-X-MEDIA-SEQUENCE の
        進みに応じて新規セグメントを取り込み続ける必要がある。

        高速化のため type=c（チャンク取得）+ seek パラメータや、l を大きくする方式も
        試したが、いずれも実機で400 Bad Requestになり使えなかった（type=b + l=15の
        ポーリング方式のみ実機で正しいデータが取得できることを確認済み）。そのため
        ダウンロードには放送時間相当の時間がかかる。

        セグメントの#EXT-X-PROGRAM-DATE-TIMEが to_dt に達したら終了する。

        renew_playlist_url (callable または None): renew_playlist_url(ft_dt) の形で、
        ft_dt から to_dt までを新しいセッションで取得するプレイリストURLを返す。
        通信断でセッションが切れたとき、続きから取り直すのに使う（Noneなら取り直さず
        例外にする）。

        on_progress (callable または None): on_progress(done, total_estimate) の形で
        セグメント取得のたびに呼ばれる進捗コールバック（total_estimateはft〜to間の
        推定セグメント数で、実際の総数とは多少ずれることがある）。バックグラウンドスレッドから
        呼ばれる。
        """
        session = requests.Session()

        def open_session(url):
            """プレイリストURLからmedialistのURLを得る（停止要求があった場合は None）"""
            top_res = self._get_with_retry(
                session.get, url, stop_event, headers=headers, timeout=10
            )
            if top_res is None:
                return None
            top_res.raise_for_status()
            if "#EXTINF" in top_res.text:
                return url
            medialist = next(
                line.strip() for line in top_res.text.splitlines()
                if line.strip() and not line.startswith("#")
            )
            # マスタープレイリストが案内するmedialist URLは station_id と session パラメータの
            # みで ft/to/l を含まないため、元のプレイリストURLが持っていたものを引き継ぐ
            return self._carry_over_query_params(url, medialist, ('ft', 'to', 'l'))

        medialist_url = open_session(playlist_url)
        if medialist_url is None:
            return

        total_estimate = None
        last_sequence = -1
        done = 0
        failed_count = 0
        stall_count = 0
        first_segment_dt = None
        last_segment_dt = None
        target_duration = 5.0
        done_at_last_renewal = -1

        def remaining_seconds():
            """最後に取得したセグメントの終わりから to_dt までの秒数（不明なら None）"""
            if last_segment_dt is None:
                return None
            return (to_dt - last_segment_dt).total_seconds() - target_duration

        while not stop_event.is_set():
            media_res = self._get_with_retry(
                session.get, medialist_url, stop_event, headers=headers, timeout=10
            )
            if media_res is None:
                return
            if media_res.status_code == 404 and done > 0:
                remaining = remaining_seconds()
                if remaining is None or remaining <= self.TIMEFREE_END_TOLERANCE_SECONDS:
                    # CDN側のセッションには寿命があるらしく、番組の終端付近で
                    # medialistが404になることがある（実機で確認済み）。末尾まで
                    # 到達していれば正常終了とする。
                    logger.info(
                        f"タイムフリー: medialistが404になりました（done={done}件）。"
                        "セッション終了とみなして完了とします"
                    )
                    break
                # 終端まで距離があるのに404＝通信断のあいだにセッションが期限切れに
                # なった（実機で確認済み）。ここで完了扱いにすると、途中で切れた
                # ファイルが「成功」になってしまうため、続きの時刻から取り直す。
                # 取り直しても1件も進まないまま再び404なら諦める
                if renew_playlist_url is None or done == done_at_last_renewal:
                    media_res.raise_for_status()
                logger.warning(
                    f"タイムフリー: medialistが404になったため、続きからセッションを取り直します"
                    f"（done={done}件、残り約{remaining:.0f}秒）"
                )
                done_at_last_renewal = done
                resume_dt = last_segment_dt + timedelta(seconds=target_duration)
                medialist_url = open_session(renew_playlist_url(resume_dt))
                if medialist_url is None:
                    return
                last_sequence = -1
                continue
            media_res.raise_for_status()
            media_sequence, target_duration, entries = self._parse_medialist(media_res.text)

            new_entries = [e for e in entries if e[0] > last_sequence]
            if not new_entries:
                stall_count += 1
                if stall_count >= 10:
                    logger.warning(
                        f"タイムフリー: {stall_count}回連続で新規セグメントを取得できず中断しました "
                        f"(last_sequence={last_sequence})"
                    )
                    remaining = remaining_seconds()
                    if remaining is not None and remaining > self.TIMEFREE_END_TOLERANCE_SECONDS:
                        raise RuntimeError(
                            f"タイムフリーの取得が途中で止まりました（残り約{remaining:.0f}秒）"
                        )
                    return
                if stop_event.is_set():
                    return
                time.sleep(max(target_duration / 2, 1))
                continue
            stall_count = 0

            reached_end = False
            for seq, seg_url, dt_str in new_entries:
                seg_dt = self._parse_program_date_time(dt_str)
                is_last_segment = False
                if (seg_dt is not None and last_segment_dt is not None
                        and seg_dt <= last_segment_dt):
                    # セッションを取り直した直後は、秒単位に丸めた開始時刻のせいで
                    # 取得済みのセグメントがもう一度載ることがある
                    last_sequence = seq
                    continue
                if seg_dt is not None:
                    if first_segment_dt is None:
                        first_segment_dt = seg_dt
                        total_estimate = max(
                            1, int((to_dt - first_segment_dt).total_seconds() / target_duration)
                        )
                        if on_progress:
                            on_progress(0, total_estimate)
                    if seg_dt >= to_dt:
                        # セグメントは target_duration 間隔で並ぶため、ちょうど to_dt と
                        # 一致するセグメントは基本的に存在しない。この判定だけに頼ると
                        # 「新規セグメントが取得できない」スタール判定（10回試行後に諦める。
                        # 約25秒の無駄待ちと警告ログ付き）に落ちてしまうため、直前の
                        # セグメント側（下記）で先に終端を検知する
                        reached_end = True
                        break
                    if seg_dt + timedelta(seconds=target_duration) >= to_dt:
                        # このセグメントの再生範囲が to_dt に達する＝実質最後のセグメント。
                        # 取得してから終了する
                        is_last_segment = True

                if stop_event.is_set():
                    return
                # セグメント本体のURLは既に認証情報が埋め込まれた署名付きURLであることが多く、
                # ここで X-Radiko-AuthToken ヘッダーを付けると拒否されることがある
                # （_iter_live_segments のセグメント取得も同様の理由でヘッダーなし）。
                seg_res = self._get_with_retry(session.get, seg_url, stop_event, timeout=10)
                if seg_res is None:
                    return
                if seg_res.status_code == 200:
                    yield seg_res.content
                else:
                    failed_count += 1
                    logger.warning(
                        f"タイムフリーセグメント取得失敗: status={seg_res.status_code} url={seg_url}"
                    )
                last_sequence = seq
                if seg_dt is not None:
                    last_segment_dt = seg_dt
                done += 1
                if on_progress and total_estimate:
                    on_progress(min(done, total_estimate), total_estimate)
                if is_last_segment:
                    reached_end = True
                    break

            if reached_end:
                break
            if stop_event.is_set():
                return
            time.sleep(max(target_duration / 2, 1))

        logger.info(f"タイムフリー: セグメント取得完了 {done}件（失敗{failed_count}件）")

    def _playback_fetch_worker(self, playlist_url, auth_token, stop_event, buffer):
        """バックグラウンドスレッドでHLSストリームを取得・デコードし、PCMを
        buffer(_LiveAudioBuffer)に貯めていく（実際の音声出力は行わない）。

        セグメントの取得は requests で行い（PyAV自身にHTTP/HLSを直接読ませると
        一部環境でセッション絡みの取得に失敗するため）、取得した生のAAC(ADTS)
        データのデコードのみPyAVに任せる。
        """
        import av

        headers = {"X-Radiko-AuthToken": auth_token}
        resampler = None

        try:
            for segment_bytes in self._iter_live_segments(playlist_url, headers, stop_event):
                container = av.open(io.BytesIO(segment_bytes), format="aac")
                try:
                    audio_stream = container.streams.audio[0]
                    if resampler is None:
                        resampler = av.AudioResampler(
                            format="s16", layout="stereo", rate=audio_stream.rate
                        )
                        buffer.sample_rate = audio_stream.rate

                    for frame in container.decode(audio_stream):
                        for resampled in resampler.resample(frame):
                            if stop_event.is_set():
                                break
                            buffer.push(_frame_to_pcm(resampled))
                finally:
                    container.close()
        except Exception:
            logger.exception("Error fetching live stream")
        finally:
            buffer.mark_finished()

    def _playback_output_worker(self, stop_event, buffer):
        """buffer(_LiveAudioBuffer)からPCMを取り出してsounddeviceで再生するスレッド。

        再生開始前に PLAYBACK_PREBUFFER_SECONDS 秒分たまるまで待つことで、
        ネットワークの遅延・瞬断がそのまま音切れにつながらないようにする。
        """
        import queue as queue_module

        import sounddevice as sd

        stream_handle = None
        try:
            while (
                not stop_event.is_set()
                and not buffer.is_finished()
                and buffer.buffered_seconds() < self.PLAYBACK_PREBUFFER_SECONDS
            ):
                time.sleep(0.05)

            while not stop_event.is_set():
                try:
                    pcm = buffer.pop(timeout=1.0)
                except queue_module.Empty:
                    if buffer.is_finished():
                        break
                    continue
                if pcm is None:
                    break
                if stream_handle is None:
                    stream_handle = sd.RawOutputStream(
                        samplerate=buffer.sample_rate, channels=2, dtype="int16"
                    )
                    stream_handle.start()
                stream_handle.write(pcm)
                # 実際に音として出るタイミングでレベルを更新する（sounddeviceへの
                # 書き込みが自然にブロッキングし実時間でペーシングされるため、
                # ここで更新することでグラフィックイコライザーの動きが滑らかになる）
                self._update_levels(pcm, buffer.sample_rate)
        except Exception:
            logger.exception("Error during live playback output")
        finally:
            # 出力側が（音声デバイスの切断等で）異常終了した場合に取得側だけが動き続けると、
            # 誰にも消費されないPCMがbufferに溜まり続けてメモリを食い潰す
            # （実測で音声1時間あたり約800MB）。出力が終わったら取得側も必ず止める
            stop_event.set()
            if stream_handle is not None:
                try:
                    stream_handle.stop()
                    stream_handle.close()
                except Exception:
                    pass
            with self._levels_lock:
                self._levels = [0.0] * self.EQ_NUM_BANDS
                self._lr_levels = [0.0, 0.0]

    # グラフィックイコライザーの描画間隔(50ms)より細かく計算しても表示されないため、
    # レベル計算はこの間隔に間引く（純PythonのFFTでCPUを食いすぎないようにする）
    LEVELS_UPDATE_INTERVAL = 0.04

    def _update_levels(self, pcm, sample_rate):
        """デコード済みPCM(s16ステレオのバイト列)からグラフィックイコライザー用の
        周波数バンド別レベル(0.0〜1.0)を計算し保持する
        """
        now = time.monotonic()
        if now - self._levels_updated_at < self.LEVELS_UPDATE_INTERVAL:
            return

        samples = array("h")
        samples.frombytes(pcm[:len(pcm) - len(pcm) % _PCM_BYTES_PER_FRAME])
        left = samples[0::2]
        right = samples[1::2]
        frames = len(left)
        if frames < 2:
            return
        self._levels_updated_at = now

        lr_levels = []
        for channel in (left, right):
            peak = max(max(channel), -min(channel)) / 32768.0
            db = 20 * math.log10(peak + 1e-6)
            # ピークメーターらしく0dBFS（フルスケール）を基準に、
            # -24dB〜0dBFSを0.0〜1.0へ正規化する
            lr_levels.append(min(max((db + 24) / 24, 0.0), 1.0))

        # FFTは2のべき乗長で行うため、収まる最大の長さに切り詰める
        n = 1 << (frames.bit_length() - 1)
        mono = [(left[i] + right[i]) / 65536.0 for i in range(n)]
        spectrum = _spectrum_magnitudes(mono)
        bin_width = sample_rate / n

        max_freq = min(16000, sample_rate / 2 - 1)
        log_lo = math.log10(60)
        log_step = (math.log10(max_freq) - log_lo) / self.EQ_NUM_BANDS
        band_edges = [10 ** (log_lo + log_step * i) for i in range(self.EQ_NUM_BANDS + 1)]

        levels = []
        for i in range(self.EQ_NUM_BANDS):
            lo = math.ceil(band_edges[i] / bin_width)
            hi = math.ceil(band_edges[i + 1] / bin_width)
            band = spectrum[lo:hi]
            magnitude = sum(band) / len(band) if band else 0.0
            db = 20 * math.log10(magnitude + 1e-6)
            # 実測でおおよそ -20dB〜+35dBに収まるため、それを 0.0〜1.0 に正規化
            # （絶対的な音量ではなく見た目のためのスケーリング）
            level = (db + 20) / 55
            levels.append(min(max(level, 0.0), 1.0))

        with self._levels_lock:
            self._levels = levels
            self._lr_levels = lr_levels

    def get_levels(self):
        """グラフィックイコライザー表示用の最新バンドレベル(0.0〜1.0)を取得"""
        with self._levels_lock:
            return list(self._levels)

    def get_lr_levels(self):
        """ピークメーター表示用の最新L/Rチャンネルレベル(0.0〜1.0、[L, R])を取得"""
        with self._levels_lock:
            return list(self._lr_levels)

    def is_playing(self):
        """ライブ再生中かどうか"""
        return any(t.is_alive() for t in self._playback_threads)

    def stop_playback(self):
        """再生中のライブストリームを停止する"""
        if self._playback_stop_event is not None:
            self._playback_stop_event.set()
        for t in self._playback_threads:
            if t.is_alive():
                t.join(timeout=5)
        self._playback_threads = []
        self._playback_stop_event = None
        self._playback_buffer = None

    # 選択可能な録音ファイル形式
    RECORDING_FORMATS = ("aac", "mp3", "m4a")
    MP3_BITRATE_CHOICES = (128, 192, 256, 320)

    # 選択可能な録音ファイル名の形式。{station}/{title}/{date}(YYYYMMDD)/
    # {time_full}(HHMMSS)/{time_short}(HHMM) が使えるテンプレート文字列。
    # {title} は番組名が不明な場合（手動録音タブでの録音等）は局名で代用する。
    RECORDING_FILENAME_PATTERNS = {
        'station_datetime': '{station}_{date}_{time_full}',
        'datetime_title': '{date}_{time_short}_{title}',
        'date_title': '{date}_{title}',
        'title_datetime': '{title}_{date}_{time_short}',
        'title_date': '{title}_{date}',
    }
    DEFAULT_FILENAME_PATTERN = 'station_datetime'

    def _sanitize_filename(self, name):
        """ファイル名に使えない文字を置換する"""
        return re.sub(r'[\\/:*?"<>|]', "_", name)

    def _build_recording_filename(self, filename_pattern, station, title, dt, ext):
        """設定された形式に従って録音ファイル名を組み立てる"""
        template = self.RECORDING_FILENAME_PATTERNS.get(
            filename_pattern, self.RECORDING_FILENAME_PATTERNS[self.DEFAULT_FILENAME_PATTERN]
        )
        safe_station = self._sanitize_filename(station)
        safe_title = self._sanitize_filename(title) if title else safe_station
        values = {
            'station': safe_station,
            'title': safe_title,
            'date': dt.strftime("%Y%m%d"),
            'time_full': dt.strftime("%H%M%S"),
            'time_short': dt.strftime("%H%M"),
        }
        return f"{template.format(**values)}.{ext}"

    def _unique_output_path(self, path):
        """同名ファイルが既に存在する場合、末尾に連番を振って重複を避ける

        （時刻を含まない/分単位までのファイル名形式では、同じ局・番組・日の
        録音が複数回行われると同名になり得るため）
        """
        if not path.exists():
            return path
        counter = 1
        while True:
            candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
            if not candidate.exists():
                return candidate
            counter += 1

    def build_recording_filename(self, filename_pattern, station, title, dt, ext):
        """_build_recording_filenameの公開ラッパー（radiko以外の取得元でも同じ命名規則を使うため）"""
        return self._build_recording_filename(filename_pattern, station, title, dt, ext)

    def unique_output_path(self, path):
        """_unique_output_pathの公開ラッパー"""
        return self._unique_output_path(path)

    def start_recording(self, station, duration, file_format="aac", mp3_bitrate=192, on_complete=None,
                         title=None, filename_pattern=None, metadata=None, reservation_id=None):
        """ラジコストリームの録音を開始する（局が異なれば複数を同時に録音できる）

        Args:
            station (str): ステーション名
            duration (int): 録音時間（分）
            file_format (str): "aac"（再エンコードなしでそのまま保存）、"m4a"（再エンコードなし
                でMP4コンテナにストリームコピー。Windows Explorer等でもタグが読める）、
                または "mp3"（re-encode）
            mp3_bitrate (int): file_format="mp3" のときのビットレート(kbps)
            on_complete (callable または None): 録音スレッド終了時に
                on_complete(had_data: bool, output_path: Path, error_message: str または None)
                の形で呼び出されるコールバック。had_data は実際に音声データを
                1バイトでも取得できたか（Falseなら出力ファイルは実質空）。
                バックグラウンドスレッドから呼ばれるため、GUI操作を行う場合は
                呼び出し側でメインスレッドへのディスパッチ（例: Tkinterのafter）が必要。
            title (str または None): ファイル名に使う番組名（未指定/空なら局名で代用）
            filename_pattern (str または None): RECORDING_FILENAME_PATTERNS のキー。
                未指定なら DEFAULT_FILENAME_PATTERN を使う
            metadata (dict または None): 録音完了後にファイルへ埋め込むタグ情報。
                'title'（番組名）, 'artist'（出演者）, 'album'（局名）,
                'comment'（番組概要）, 'date'（放送日 "YYYY-MM-DD"）のいずれかを
                含む辞書。未指定のキーは書き込まない
            reservation_id (str または None): 予約録音から呼ばれた場合の予約ID。
                is_recording_active(station, reservation_id=...) で、どの予約が
                実際に録音中かを区別するために使う。手動録音の場合はNone

        Returns:
            (bool, str または None): (録音開始に成功したか, 保存先ファイルパス文字列)
        """
        if self.is_recording_active(station):
            logger.warning(f"録音開始スキップ: {station} は既に録音中です")
            return False, None

        station_id = self.station_mapping.get(station)
        if not station_id:
            logger.warning(f"録音開始失敗: 局が見つかりません ({station})")
            return False, None

        if file_format not in self.RECORDING_FORMATS:
            logger.warning(f"録音開始失敗: 未対応のフォーマットです ({file_format})")
            return False, None

        try:
            auth_token = self._ensure_authenticated()
            playlist_base_url = self._get_live_playlist_url(station_id)
        except Exception:
            logger.exception(f"Error preparing recording for {station}")
            return False, None

        if not playlist_base_url:
            return False, None

        lsid = uuid.uuid4().hex
        playlist_url = f"{playlist_base_url}?station_id={station_id}&l=15&lsid={lsid}&type=b"

        # "aac"形式はセグメントの生バイト列（ADTS AACストリーム）をそのまま連結するため
        # MP4コンテナではなく .aac 拡張子にする
        ext = {"aac": "aac", "m4a": "m4a"}.get(file_format, "mp3")
        filename = self._build_recording_filename(
            filename_pattern or self.DEFAULT_FILENAME_PATTERN, station, title, datetime.now(), ext
        )
        output_path = self._unique_output_path(self.output_dir / filename)

        stop_event = threading.Event()
        headers = {"X-Radiko-AuthToken": auth_token}
        segment_iter = self._iter_live_segments(playlist_url, headers, stop_event)
        thread = threading.Thread(
            target=self._recording_worker,
            args=(
                segment_iter, stop_event,
                output_path, file_format, mp3_bitrate, duration * 60, station, on_complete,
                metadata,
            ),
            daemon=True,
        )
        with self._recordings_lock:
            self._recordings[station] = {
                'thread': thread, 'stop_event': stop_event, 'output_path': output_path,
                'reservation_id': reservation_id,
            }
        thread.start()
        logger.info(f"録音開始: {station} ({duration}分, {file_format}) -> {output_path}")
        return True, str(output_path)

    def is_download_active(self, station, ft):
        """指定局・指定開始時刻(ft)のタイムフリーダウンロードが実行中かどうか"""
        key = f"{station}|{ft}"
        with self._downloads_lock:
            entry = self._downloads.get(key)
            return bool(entry and entry['thread'].is_alive())

    def is_download_limit_reached(self):
        """タイムフリーの同時ダウンロード数が max_concurrent_timefree_downloads の
        上限に達しているかどうか"""
        with self._downloads_lock:
            active = sum(1 for e in self._downloads.values() if e['thread'].is_alive())
        return active >= self.max_concurrent_timefree_downloads

    def stop_download(self, station, ft):
        """指定局・指定開始時刻(ft)のタイムフリーダウンロードを停止する

        stop_recording と同様、それまでにダウンロード済みの内容はファイルに残る
        （_recording_worker の had_data 判定・保存処理を録音と共有しているため）。
        """
        key = f"{station}|{ft}"
        with self._downloads_lock:
            entry = self._downloads.get(key)

        if entry is None:
            return

        logger.info(f"タイムフリー取得停止要求: {station} ({ft})")
        entry['stop_event'].set()
        if entry['thread'].is_alive():
            entry['thread'].join(timeout=5)

        with self._downloads_lock:
            self._downloads.pop(key, None)

    def stop_all_downloads(self):
        """進行中の全てのタイムフリーダウンロードを停止する（アプリ終了時等）

        stop_recording(station=None) と同様、それまでにダウンロード済みの内容は
        ファイルに残る。
        """
        with self._downloads_lock:
            targets = dict(self._downloads)

        if targets:
            logger.info(f"タイムフリー取得停止要求（全件）: {len(targets)}件")

        for entry in targets.values():
            entry['stop_event'].set()
        for entry in targets.values():
            if entry['thread'].is_alive():
                entry['thread'].join(timeout=5)

        with self._downloads_lock:
            for key in targets:
                self._downloads.pop(key, None)

    def start_timefree_download(self, station, ft, to, file_format="aac", mp3_bitrate=192,
                                 on_complete=None, on_progress=None, title=None,
                                 filename_pattern=None, metadata=None):
        """radikoタイムフリーで指定区間の番組をダウンロードする

        Args:
            station (str): ステーション名
            ft (str): 取得開始時刻 "YYYYMMDDHHMMSS"
            to (str): 取得終了時刻 "YYYYMMDDHHMMSS"
            on_progress (callable または None): on_progress(done: int, total: int) の形で
                セグメント取得のたびに呼ばれる進捗コールバック。バックグラウンドスレッドから
                呼ばれるため、GUI操作を行う場合は呼び出し側でメインスレッドへのディスパッチが必要
            その他の引数は start_recording と同じ意味。

        ライブ録音（self._recordings、局名をキーに多重実行を防止）とは別の
        名前空間（self._downloads、"局名|ft"をキー）で多重実行を防止するため、
        同じ局のライブ録音中でもタイムフリーのダウンロードは独立して実行できる。

        Returns:
            (bool, str または None): (ダウンロード開始に成功したか, 保存先ファイルパス文字列)
        """
        if self.is_download_active(station, ft):
            logger.warning(f"タイムフリー取得スキップ: {station} ({ft}) は取得中です")
            return False, None

        if self.is_download_limit_reached():
            logger.warning(
                f"タイムフリー取得スキップ: 同時ダウンロード数が上限"
                f"（{self.max_concurrent_timefree_downloads}）に達しています"
            )
            return False, None

        station_id = self.station_mapping.get(station)
        if not station_id:
            logger.warning(f"タイムフリー取得失敗: 局が見つかりません ({station})")
            return False, None

        if file_format not in self.RECORDING_FORMATS:
            logger.warning(f"タイムフリー取得失敗: 未対応のフォーマットです ({file_format})")
            return False, None

        try:
            auth_token = self._ensure_authenticated()
            playlist_base_url = self._get_timefree_playlist_url(station_id)
        except Exception:
            logger.exception(f"Error preparing timefree download for {station}")
            return False, None

        if not playlist_base_url:
            return False, None

        # start_at/end_at と ft/to の両方を送る必要がある（streamlinkのradikoプラグイン実装が
        # 典拠。ft/toだけだとCDNのセッションに時間範囲が紐付かず、常に現在時刻付近の
        # ライブ相当の内容が返ってきてしまうことを実機で確認した）。
        # l は medialist が1回のリクエストで返す秒数の上限らしいが、大きい値（例: 600）を
        # 送ると400 Bad Requestになることを実機で確認したため、動作確認済みの15固定とする。
        # type=c（チャンク取得）+ seek での高速化も試したが実機で400になったため type=b のまま。
        #
        # ただし別セッション（別lsid）であれば、任意のft地点から待ち時間なく即座に
        # 取得を開始できることを実機で確認済みなので、区間をtimefree_parallel_splits個に
        # 分割し並列取得することで高速化する（_iter_timefree_segments_parallel）。
        ft_dt = datetime.strptime(ft, "%Y%m%d%H%M%S")
        to_dt = datetime.strptime(to, "%Y%m%d%H%M%S")
        # 極端に短い区間（分割後が30秒未満になる）では分割の意味が薄いため縮退させる
        num_splits = max(1, min(
            self.timefree_parallel_splits,
            int((to_dt - ft_dt).total_seconds() // 30) or 1
        ))

        # ファイル名の日時には、ダウンロードした時刻ではなく放送開始時刻(ft)を使う
        # （タイムフリーは後から取得するものなので、いつ聴いたかではなく、いつ放送された
        # 番組かがファイル名から分かるようにする）
        ext = {"aac": "aac", "m4a": "m4a"}.get(file_format, "mp3")
        filename = self._build_recording_filename(
            filename_pattern or self.DEFAULT_FILENAME_PATTERN, station, title, ft_dt, ext
        )
        output_path = self._unique_output_path(self.output_dir / filename)

        stop_event = threading.Event()
        segment_iter = self._iter_timefree_segments_parallel(
            playlist_base_url, station_id, auth_token, ft_dt, to_dt, stop_event,
            on_progress=on_progress, num_splits=num_splits
        )
        registry_key = f"{station}|{ft}"
        thread = threading.Thread(
            target=self._recording_worker,
            args=(
                segment_iter, stop_event,
                output_path, file_format, mp3_bitrate, None, station, on_complete,
                metadata,
            ),
            kwargs={
                'registry': self._downloads,
                'registry_lock': self._downloads_lock,
                'registry_key': registry_key,
            },
            daemon=True,
        )
        with self._downloads_lock:
            self._downloads[registry_key] = {
                'thread': thread, 'stop_event': stop_event, 'output_path': output_path
            }
        thread.start()
        logger.info(f"タイムフリー取得開始: {station} ({ft}-{to}, {file_format}) -> {output_path}")
        return True, str(output_path)

    def _write_metadata_tags(self, output_path, file_format, metadata):
        """録音済みファイルにタグ（番組名・出演者・局名・概要・番組画像など）を埋め込む

        file_format="aac" の場合、出力は生のADTS AACストリームでMP4コンテナでは
        ないが、ID3v2タグはファイル先頭に独立したチャンクとして追加されるだけなので、
        ADTSストリームの前に付与しても再生自体には影響しない（対応プレイヤーであれば
        タグも読み取れる。ただしWindows Explorerは.aac用のタグハンドラーを持たないため
        エクスプローラーのプロパティ列には表示されない）。
        file_format="m4a" はMP4コンテナのためExplorerでもタグが表示できる。
        """
        title = metadata.get('title')
        artist = metadata.get('artist')
        album = metadata.get('album')
        comment = metadata.get('comment')
        date = metadata.get('date')
        image_url = metadata.get('image_url')
        image_data = self.get_image(image_url) if image_url else None

        if file_format == "m4a":
            from mutagen.mp4 import MP4, MP4Cover

            tags = MP4(output_path)
            if title:
                tags['\xa9nam'] = [title]
            if artist:
                tags['\xa9ART'] = [artist]
            if album:
                tags['\xa9alb'] = [album]
            if comment:
                tags['\xa9cmt'] = [comment]
            if date:
                tags['\xa9day'] = [date]
            if image_data:
                ext = os.path.splitext(image_url.split("?")[0])[1].lower()
                fmt = MP4Cover.FORMAT_PNG if ext == ".png" else MP4Cover.FORMAT_JPEG
                tags['covr'] = [MP4Cover(image_data, imageformat=fmt)]
            tags.save()
            return

        from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, COMM, TDRC, APIC

        try:
            tags = ID3(output_path)
        except ID3NoHeaderError:
            tags = ID3()

        if image_data:
            ext = os.path.splitext(image_url.split("?")[0])[1].lower()
            mime = "image/png" if ext == ".png" else "image/jpeg"
            tags.setall('APIC', [APIC(
                encoding=3, mime=mime, type=3, desc='Cover', data=image_data
            )])

        if title:
            tags.setall('TIT2', [TIT2(encoding=3, text=[title])])
        if artist:
            tags.setall('TPE1', [TPE1(encoding=3, text=[artist])])
        if album:
            tags.setall('TALB', [TALB(encoding=3, text=[album])])
        if comment:
            tags.setall('COMM', [COMM(encoding=3, lang='jpn', desc='', text=[comment])])
        if date:
            tags.setall('TDRC', [TDRC(encoding=3, text=[date])])

        tags.save(output_path, v2_version=3)

    def write_metadata_tags(self, output_path, file_format, metadata):
        """_write_metadata_tagsの公開ラッパー（radiko以外の取得元でも同じタグ付けを使うため）"""
        return self._write_metadata_tags(output_path, file_format, metadata)

    def _recording_worker(self, segment_iter, stop_event,
                           output_path, file_format, mp3_bitrate, duration_seconds, station,
                           on_complete=None, metadata=None, registry=None, registry_lock=None,
                           registry_key=None):
        """バックグラウンドスレッドで配信をファイルに保存する（ライブ録音・タイムフリー共通）

        file_format="aac" の場合は取得したAAC(ADTS)の生バイト列をそのまま連結して
        書き出す（再エンコードなし・音質劣化なし）。
        file_format="m4a" の場合はデコードせず、AACパケットをそのままMP4コンテナに
        ストリームコピーする（再エンコードなし・音質劣化なし。Windows Explorer等の
        タグ表示に対応するためのコンテナ変換のみ）。
        file_format="mp3" の場合はデコードしてlibmp3lameで再エンコードする。
        いずれの場合もデコードした音声からグラフィックイコライザー用レベルは更新する。

        segment_iter はセグメントの生バイト列を順に返すイテレーター
        （ライブなら _iter_live_segments、タイムフリーなら _iter_timefree_segments）。
        duration_seconds が指定されていればその時間経過で打ち切る（ライブ録音用）。
        None ならセグメントを取得し尽くすまで（タイムフリーは配信自体が有限のため
        イテレーターが自然に終わる）。

        registry/registry_key は多重実行防止用の辞書とそのキー
        （ライブ録音は self._recordings、タイムフリーは self._downloads）。
        未指定なら self._recordings と station を使う。

        認証切れ・通信エラー等でセグメントを1つも取得できなかった場合、
        例外はこの関数内で捕捉されるだけで呼び出し元には伝わらないため、
        出力ファイルが実質空のまま「録音成功」に見えてしまう。on_complete を
        通じて実際にデータを取得できたか（had_data）を呼び出し元に返すことで、
        これを検知できるようにする。
        """
        import av

        if registry is None:
            registry = self._recordings
        if registry_lock is None:
            registry_lock = self._recordings_lock
        if registry_key is None:
            registry_key = station

        start_time = time.monotonic()
        raw_file = None
        output_container = None
        output_stream = None
        resampler = None
        pts_counter = 0
        had_data = False
        error_message = None

        try:
            if file_format == "aac":
                raw_file = open(output_path, "wb")
            else:
                output_container = av.open(str(output_path), mode="w")

            for segment_bytes in segment_iter:
                container = av.open(io.BytesIO(segment_bytes), format="aac")
                try:
                    audio_stream = container.streams.audio[0]

                    if file_format == "aac":
                        raw_file.write(segment_bytes)
                        had_data = True
                        if resampler is None:
                            resampler = av.AudioResampler(
                                format="s16", layout="stereo", rate=audio_stream.rate
                            )
                        for frame in container.decode(audio_stream):
                            for resampled in resampler.resample(frame):
                                self._update_levels(_frame_to_pcm(resampled), audio_stream.rate)
                    elif file_format == "m4a":
                        if output_stream is None:
                            output_stream = output_container.add_stream_from_template(audio_stream)
                        if resampler is None:
                            resampler = av.AudioResampler(
                                format="s16", layout="stereo", rate=audio_stream.rate
                            )
                        for packet in container.demux(audio_stream):
                            if packet.dts is None:
                                continue
                            duration = packet.duration or 0
                            for frame in packet.decode():
                                for resampled in resampler.resample(frame):
                                    self._update_levels(_frame_to_pcm(resampled), audio_stream.rate)
                            had_data = True
                            packet.pts = pts_counter
                            packet.dts = pts_counter
                            packet.stream = output_stream
                            output_container.mux(packet)
                            pts_counter += duration
                    else:
                        if resampler is None:
                            resampler = av.AudioResampler(
                                format="s16", layout="stereo", rate=audio_stream.rate
                            )
                            output_stream = output_container.add_stream(
                                "libmp3lame", rate=audio_stream.rate
                            )
                            output_stream.bit_rate = mp3_bitrate * 1000
                            output_stream.layout = "stereo"

                        for frame in container.decode(audio_stream):
                            for resampled in resampler.resample(frame):
                                had_data = True
                                self._update_levels(_frame_to_pcm(resampled), audio_stream.rate)
                                resampled.pts = pts_counter
                                pts_counter += resampled.samples
                                for packet in output_stream.encode(resampled):
                                    output_container.mux(packet)
                finally:
                    container.close()

                if duration_seconds and (time.monotonic() - start_time) >= duration_seconds:
                    stop_event.set()
                    break
        except Exception as e:
            error_message = str(e)
            logger.exception(f"Error during recording: {station}")
        finally:
            if output_container is not None:
                try:
                    if file_format == "mp3" and output_stream is not None:
                        for packet in output_stream.encode(None):
                            output_container.mux(packet)
                finally:
                    output_container.close()
            if raw_file is not None:
                raw_file.close()
            if had_data and metadata:
                try:
                    self._write_metadata_tags(output_path, file_format, metadata)
                except Exception:
                    logger.exception("Error writing metadata tags")
            elif not had_data:
                # データを1バイトも取得できなかった場合、空の出力ファイルを残さない
                try:
                    if os.path.exists(output_path):
                        os.remove(output_path)
                except OSError:
                    logger.exception("Error removing empty recording file")
            with self._levels_lock:
                self._levels = [0.0] * self.EQ_NUM_BANDS
                self._lr_levels = [0.0, 0.0]
            with registry_lock:
                registry.pop(registry_key, None)
            if error_message:
                logger.warning(f"録音終了: {station} -> {output_path} (エラー: {error_message})")
            else:
                logger.info(f"録音終了: {station} -> {output_path} (had_data={had_data})")
            if on_complete is not None:
                try:
                    on_complete(had_data, output_path, error_message)
                except Exception:
                    logger.exception("Error in recording on_complete callback")

    def is_recording_active(self, station=None, reservation_id=None):
        """録音中かどうか

        Args:
            station (str または None): 指定した局が録音中かを調べる。
                Noneの場合はいずれかの局が録音中であればTrue
            reservation_id (str または None): 指定した場合、その局で録音中なのが
                この予約IDによるものかどうかまで区別する。局が同じでも別の予約
                （や手動録音）による録音であればFalseを返す
        """
        with self._recordings_lock:
            if station is not None:
                entry = self._recordings.get(station)
                if not entry or not entry['thread'].is_alive():
                    return False
                if reservation_id is not None:
                    return entry.get('reservation_id') == reservation_id
                return True
            return any(entry['thread'].is_alive() for entry in self._recordings.values())

    def stop_recording(self, station=None):
        """録音を停止する

        Args:
            station (str または None): 指定した局の録音のみ停止する。
                Noneの場合は現在進行中の全ての録音を停止する（アプリ終了時等）
        """
        with self._recordings_lock:
            targets = (
                {station: self._recordings[station]} if station is not None and station in self._recordings
                else dict(self._recordings) if station is None
                else {}
            )

        if targets:
            logger.info(f"録音停止要求: {', '.join(targets.keys())}")

        for entry in targets.values():
            entry['stop_event'].set()
        for entry in targets.values():
            if entry['thread'].is_alive():
                entry['thread'].join(timeout=5)

        with self._recordings_lock:
            for st in targets:
                self._recordings.pop(st, None)

    WEEKDAY_JA = ['月', '火', '水', '木', '金', '土', '日']

    def _fetch_day_programs(self, station_id, target_date, cancel_event=None):
        """指定局・指定日（datetime）1日分の番組表を取得してパースする

        get_program_schedule（未来方向）と get_timefree_schedule（過去方向）の
        どちらからも呼ばれる共通処理。番組表APIのURL構築・XMLパースは対象日の
        前後に関わらず同じ形式のため、日付計算部分だけを呼び出し元で変える。

        cancel_event が指定され、かつセットされている場合は通信を行わず
        空リストを返す（呼び出し元の並列取得ループで早期に打ち切るため）。

        Returns:
            list: get_program_schedule と同じ形式の辞書のリスト（取得失敗時は空リスト）
        """
        if cancel_event is not None and cancel_event.is_set():
            return []

        date_str = target_date.strftime("%Y%m%d")
        date_label = f"{target_date.month}/{target_date.day}({self.WEEKDAY_JA[target_date.weekday()]})"
        date_iso = target_date.strftime("%Y-%m-%d")

        url = f"{self.radiko_api_url}/{date_str}/{station_id}.xml"
        programs = []

        try:
            for attempt in range(2):
                try:
                    response = requests.get(url, timeout=8)
                    break
                except requests.exceptions.RequestException:
                    if attempt == 0:
                        continue
                    raise
            response.encoding = 'utf-8'

            if response.status_code == 200:
                root = ET.fromstring(response.content)

                # 各番組情報を抽出
                for program in root.findall(".//prog"):
                    start = program.get("ft", "")
                    end = program.get("to", "")
                    title = program.findtext("title", "不明な番組")
                    desc = program.findtext("desc", "")
                    info = program.findtext("info", "")
                    pfm = program.findtext("pfm", "")
                    img = program.findtext("img", "")

                    # 時刻をフォーマット（ft/to は YYYYMMDDHHmmss 形式）
                    try:
                        start_dt = datetime.strptime(start, "%Y%m%d%H%M%S")
                        end_dt = datetime.strptime(end, "%Y%m%d%H%M%S")
                        start_time = start_dt.strftime("%H:%M")
                        end_time = end_dt.strftime("%H:%M")
                    except ValueError:
                        start_time = start
                        end_time = end

                    programs.append({
                        'date': date_label,
                        'date_iso': date_iso,
                        'start': start_time,
                        'end': end_time,
                        'title': title,
                        'desc': desc,
                        'info': info,
                        'pfm': pfm,
                        'img': img
                    })
        except Exception:
            logger.exception(f"Error fetching schedule for {date_str}")

        return programs

    def _fetch_days_parallel(self, station_id, target_dates, cancel_event=None):
        """複数日分の番組表を並列取得し、日付順に結合して返す（内部共通処理）

        radiko側のレスポンスが遅く1リクエストあたり数秒かかるため、日ごとに
        直列で待つと局あたりの取得に時間がかかる。ThreadPoolExecutorで並列に
        投げることでI/O待ち時間を重ね合わせる。
        """
        results = [[] for _ in target_dates]
        with ThreadPoolExecutor(max_workers=self.SCHEDULE_DAY_FETCH_WORKERS) as executor:
            future_to_index = {
                executor.submit(self._fetch_day_programs, station_id, target_date, cancel_event): i
                for i, target_date in enumerate(target_dates)
            }
            for future in future_to_index:
                index = future_to_index[future]
                try:
                    results[index] = future.result()
                except Exception:
                    logger.exception(f"Error fetching schedule day (index={index})")

        programs = []
        for day_programs in results:
            programs.extend(day_programs)
        return programs

    def get_program_schedule(self, station_name, days=10, cancel_event=None):
        """ラジコから番組表を取得

        Args:
            station_name (str): ステーション名
            days (int): 取得する日数（デフォルト: 10日分。radikoは実際には
                日によって最大13日程度先まで応答するが、直近以外は仮の定型
                スケジュールの割合が増えるため10日を既定値にしている）
            cancel_event (threading.Event, optional): セットされている場合、
                未着手の日の取得を打ち切って現時点までの結果を返す

        Returns:
            list: [
                {
                    'date': '9/1(火)',
                    'date_iso': '2026-09-01',
                    'start': '09:00',
                    'end': '10:00',
                    'title': '番組名',
                    'desc': '説明',
                    'pfm': '出演者'
                },
                ...
            ]
        """
        logger.info(f"番組表取得開始: {station_name} ({days}日分)")
        try:
            station_id = self.station_mapping.get(station_name)
            if not station_id:
                logger.warning(f"番組表取得失敗: 局が見つかりません ({station_name})")
                return []

            now = datetime.now()
            target_dates = [now + timedelta(days=day_offset) for day_offset in range(days)]
            programs = self._fetch_days_parallel(station_id, target_dates, cancel_event)

            logger.info(f"番組表取得完了: {station_name} ({len(programs)}件)")
            return programs

        except Exception:
            logger.exception(f"Error in get_program_schedule: {station_name}")
            return []

    def get_timefree_schedule(self, station_name, days_back=7, cancel_event=None):
        """ラジコのタイムフリー対象期間（過去 days_back 日分、当日を含む）の番組表を取得

        Args:
            station_name (str): ステーション名
            days_back (int): 遡る日数（デフォルト7日。radikoのタイムフリーは
                放送から概ね1週間で聴取期限が切れるため）
            cancel_event (threading.Event, optional): セットされている場合、
                未着手の日の取得を打ち切って現時点までの結果を返す

        Returns:
            list: get_program_schedule と同じ形式の辞書のリスト
        """
        logger.info(f"タイムフリー番組表取得開始: {station_name} (過去{days_back}日分)")
        try:
            station_id = self.station_mapping.get(station_name)
            if not station_id:
                logger.warning(f"タイムフリー番組表取得失敗: 局が見つかりません ({station_name})")
                return []

            now = datetime.now()
            target_dates = [now - timedelta(days=day_offset) for day_offset in range(days_back)]
            programs = self._fetch_days_parallel(station_id, target_dates, cancel_event)

            logger.info(f"タイムフリー番組表取得完了: {station_name} ({len(programs)}件)")
            return programs

        except Exception:
            logger.exception(f"Error in get_timefree_schedule: {station_name}")
            return []

    def get_sample_schedule(self, station_name, days=10):
        """サンプル番組表を返す（API接続できない場合用）

        Args:
            station_name (str): ステーション名
            days (int): 生成する日数（デフォルト: 10日分）

        Returns:
            list: サンプル番組データ
        """
        now = datetime.now()
        sample_programs = []

        for day_offset in range(days):
            target_date = now + timedelta(days=day_offset)
            date_label = f"{target_date.month}/{target_date.day}({self.WEEKDAY_JA[target_date.weekday()]})"
            date_iso = target_date.strftime("%Y-%m-%d")
            hour = now.hour if day_offset == 0 else 6

            day_programs = [
                {
                    'start': f"{hour:02d}:00",
                    'end': f"{hour:02d}:30",
                    'title': f"{station_name} ニュース",
                    'desc': "最新ニュースと天気予報"
                },
                {
                    'start': f"{hour:02d}:30",
                    'end': f"{(hour+1)%24:02d}:00",
                    'title': f"{station_name} モーニング番組",
                    'desc': "朝の情報番組"
                },
                {
                    'start': f"{(hour+1)%24:02d}:00",
                    'end': f"{(hour+2)%24:02d}:00",
                    'title': "音楽ライブラリー",
                    'desc': "様々なジャンルの音楽をお届けします"
                },
                {
                    'start': f"{(hour+2)%24:02d}:00",
                    'end': f"{(hour+3)%24:02d}:00",
                    'title': "トーク番組",
                    'desc': "ゲストとのトークショー"
                }
            ]

            for program in day_programs:
                program['date'] = date_label
                program['date_iso'] = date_iso
                sample_programs.append(program)

        return sample_programs
