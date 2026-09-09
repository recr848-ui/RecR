"""
ラジコマネージャーモジュール
ラジコストリーム録音と管理を処理します
"""

import base64
import hashlib
import io
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime, timedelta
import numpy as np
import requests
from xml.etree import ElementTree as ET

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
        """PCMチャンク(shape=(フレーム数, 2))をバッファに追加する"""
        with self._lock:
            self._buffered_samples += pcm.shape[0]
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
                self._buffered_samples -= pcm.shape[0]
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

        self.output_dir = Path.home() / "Music" / "RecR"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # 局名 -> {'thread', 'stop_event', 'output_path'} で、局ごとに独立した録音を
        # 複数同時に管理する（局が異なれば並行録音でき、同じ局は多重録音しない）
        self._recordings = {}
        self._recordings_lock = threading.Lock()

        self.cache_dir = get_base_dir() / "config"
        self.cache_file = self.cache_dir / "schedule_cache.json"
        self.image_cache_dir = self.cache_dir / "images"
        self.settings_file = self.cache_dir / "settings.json"
        self.reservations_file = self.cache_dir / "reservations.json"
        self.freeword_file = self.cache_dir / "freeword_keywords.json"

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
        """キャッシュファイル全体を読み込む（存在しない/壊れている場合は空dict）"""
        if not self.cache_file.exists():
            return {}
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_cache_file(self, cache):
        """キャッシュファイル全体を書き込む"""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

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
        """ステーションの番組表をキャッシュに保存"""
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

    def load_settings(self):
        """アプリ設定（既定局・番組表取得日数など）を読み込む

        Returns:
            dict: 設定がなければ空dict
        """
        if not self.settings_file.exists():
            return {}
        try:
            with open(self.settings_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def save_settings(self, settings):
        """アプリ設定を保存する（既存の設定とマージ）"""
        current = self.load_settings()
        current.update(settings)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with open(self.settings_file, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)

    # 予約時刻を過ぎてもスケジューラの巡回間隔等の遅れを許容して録音を開始する猶予（秒）
    RESERVATION_GRACE_SECONDS = 180

    def _load_reservations_file(self):
        if not self.reservations_file.exists():
            return []
        try:
            with open(self.reservations_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

    def _save_reservations_file(self, reservations):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with open(self.reservations_file, "w", encoding="utf-8") as f:
            json.dump(reservations, f, ensure_ascii=False, indent=2)

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
        reservations = self._load_reservations_file()
        for res in reservations:
            if res.get('id') == reservation_id:
                res.update(data)
                break
        self._save_reservations_file(reservations)

    def delete_reservation(self, reservation_id):
        """予約録音を削除する"""
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

        Returns:
            list: [(reservation_dict, start_datetime, end_datetime), ...]
        """
        now = datetime.now()
        due = []
        for res in self._load_reservations_file():
            if not res.get('enabled', True):
                continue

            occurrence_date = self._reservation_occurrence_date(res, now)
            if occurrence_date is None:
                continue
            if res.get('last_run_date') == occurrence_date.isoformat():
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

            grace_end = start_dt + timedelta(seconds=self.RESERVATION_GRACE_SECONDS)
            if start_dt <= now <= grace_end and now < end_dt:
                due.append((res, start_dt, end_dt))

        return due

    def _load_freeword_file(self):
        if not self.freeword_file.exists():
            return []
        try:
            with open(self.freeword_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

    def _save_freeword_file(self, freewords):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with open(self.freeword_file, "w", encoding="utf-8") as f:
            json.dump(freewords, f, ensure_ascii=False, indent=2)

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

    def _iter_live_segments(self, playlist_url, headers, stop_event):
        """マスタープレイリストを起点に、ライブ配信の新規セグメント(生のAACバイト列)を
        順次生成するジェネレーター。

        radikoのHLS配信は「マスタープレイリスト → メディアリスト(session付) → .aacセグメント」
        という構成。再生・録音の両方でこのジェネレーターを共有する。
        """
        top_res = requests.get(playlist_url, headers=headers, timeout=10)
        top_res.raise_for_status()
        medialist_url = next(
            line.strip() for line in top_res.text.splitlines()
            if line.strip() and not line.startswith("#")
        )

        last_sequence = -1
        while not stop_event.is_set():
            media_res = requests.get(medialist_url, headers=headers, timeout=10)
            media_res.raise_for_status()

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

            for seq, seg_url in new_segments:
                if stop_event.is_set():
                    return
                seg_res = requests.get(seg_url, timeout=10)
                if seg_res.status_code == 200:
                    yield seg_res.content
                last_sequence = seq

            if stop_event.is_set():
                return
            time.sleep(max(target_duration / 2, 1))

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
                            pcm = resampled.to_ndarray()
                            buffer.push(pcm.reshape(-1, 2))
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
                    stream_handle = sd.OutputStream(
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
            if stream_handle is not None:
                try:
                    stream_handle.stop()
                    stream_handle.close()
                except Exception:
                    pass
            with self._levels_lock:
                self._levels = [0.0] * self.EQ_NUM_BANDS
                self._lr_levels = [0.0, 0.0]

    def _update_levels(self, pcm, sample_rate):
        """デコード済みPCM(int16, shape=(-1,2))からグラフィックイコライザー用の
        周波数バンド別レベル(0.0〜1.0)を計算し保持する
        """
        if pcm.size == 0:
            return

        # PyAVのto_ndarray()はインターリーブされた1行の配列 (1, N) で返るため、
        # 再生時と同様に (フレーム数, チャンネル数) へreshapeしてから処理する
        stereo = pcm.reshape(-1, 2).astype(np.float32) / 32768.0
        mono = stereo.mean(axis=1)
        n = len(mono)
        if n < 2:
            return

        lr_levels = []
        for ch in range(2):
            peak = np.max(np.abs(stereo[:, ch]))
            db = 20 * np.log10(peak + 1e-6)
            # ピークメーターらしく0dBFS（フルスケール）を基準に、
            # -24dB〜0dBFSを0.0〜1.0へ正規化する
            lr_levels.append(float(np.clip((db + 24) / 24, 0.0, 1.0)))

        spectrum = np.abs(np.fft.rfft(mono * np.hanning(n)))
        freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)

        max_freq = min(16000, sample_rate / 2 - 1)
        band_edges = np.logspace(np.log10(60), np.log10(max_freq), self.EQ_NUM_BANDS + 1)

        levels = []
        for i in range(self.EQ_NUM_BANDS):
            mask = (freqs >= band_edges[i]) & (freqs < band_edges[i + 1])
            magnitude = spectrum[mask].mean() if np.any(mask) else 0.0
            db = 20 * np.log10(magnitude + 1e-6)
            # 実測でおおよそ -20dB〜+35dBに収まるため、それを 0.0〜1.0 に正規化
            # （絶対的な音量ではなく見た目のためのスケーリング）
            level = (db + 20) / 55
            levels.append(float(np.clip(level, 0.0, 1.0)))

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

    def start_recording(self, station, duration, file_format="aac", mp3_bitrate=192, on_complete=None,
                         title=None, filename_pattern=None, metadata=None):
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
        thread = threading.Thread(
            target=self._recording_worker,
            args=(
                playlist_url, auth_token, stop_event,
                output_path, file_format, mp3_bitrate, duration * 60, station, on_complete,
                metadata,
            ),
            daemon=True,
        )
        with self._recordings_lock:
            self._recordings[station] = {
                'thread': thread, 'stop_event': stop_event, 'output_path': output_path
            }
        thread.start()
        logger.info(f"録音開始: {station} ({duration}分, {file_format}) -> {output_path}")
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

    def _recording_worker(self, playlist_url, auth_token, stop_event,
                           output_path, file_format, mp3_bitrate, duration_seconds, station,
                           on_complete=None, metadata=None):
        """バックグラウンドスレッドでライブ配信をファイルに保存する

        file_format="aac" の場合は取得したAAC(ADTS)の生バイト列をそのまま連結して
        書き出す（再エンコードなし・音質劣化なし）。
        file_format="m4a" の場合はデコードせず、AACパケットをそのままMP4コンテナに
        ストリームコピーする（再エンコードなし・音質劣化なし。Windows Explorer等の
        タグ表示に対応するためのコンテナ変換のみ）。
        file_format="mp3" の場合はデコードしてlibmp3lameで再エンコードする。
        いずれの場合もデコードした音声からグラフィックイコライザー用レベルは更新する。

        認証切れ・通信エラー等でセグメントを1つも取得できなかった場合、
        例外はこの関数内で捕捉されるだけで呼び出し元には伝わらないため、
        出力ファイルが実質空のまま「録音成功」に見えてしまう。on_complete を
        通じて実際にデータを取得できたか（had_data）を呼び出し元に返すことで、
        これを検知できるようにする。
        """
        import av

        headers = {"X-Radiko-AuthToken": auth_token}
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

            for segment_bytes in self._iter_live_segments(playlist_url, headers, stop_event):
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
                                self._update_levels(resampled.to_ndarray(), audio_stream.rate)
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
                                    self._update_levels(resampled.to_ndarray(), audio_stream.rate)
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
                                self._update_levels(resampled.to_ndarray(), audio_stream.rate)
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
            with self._recordings_lock:
                self._recordings.pop(station, None)
            if error_message:
                logger.warning(f"録音終了: {station} -> {output_path} (エラー: {error_message})")
            else:
                logger.info(f"録音終了: {station} -> {output_path} (had_data={had_data})")
            if on_complete is not None:
                try:
                    on_complete(had_data, output_path, error_message)
                except Exception:
                    logger.exception("Error in recording on_complete callback")

    def is_recording_active(self, station=None):
        """録音中かどうか

        Args:
            station (str または None): 指定した局が録音中かを調べる。
                Noneの場合はいずれかの局が録音中であればTrue
        """
        with self._recordings_lock:
            if station is not None:
                entry = self._recordings.get(station)
                return bool(entry and entry['thread'].is_alive())
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

    def get_program_schedule(self, station_name, days=10):
        """ラジコから番組表を取得

        Args:
            station_name (str): ステーション名
            days (int): 取得する日数（デフォルト: 10日分。radikoは実際には
                日によって最大13日程度先まで応答するが、直近以外は仮の定型
                スケジュールの割合が増えるため10日を既定値にしている）

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

            programs = []
            now = datetime.now()

            # 今日から指定日数分の番組表を取得
            for day_offset in range(days):
                target_date = now + timedelta(days=day_offset)
                date_str = target_date.strftime("%Y%m%d")
                date_label = f"{target_date.month}/{target_date.day}({self.WEEKDAY_JA[target_date.weekday()]})"
                date_iso = target_date.strftime("%Y-%m-%d")

                # 番組表APIのエンドポイント
                url = f"{self.radiko_api_url}/{date_str}/{station_id}.xml"

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
                    continue

            logger.info(f"番組表取得完了: {station_name} ({len(programs)}件)")
            return programs

        except Exception:
            logger.exception(f"Error in get_program_schedule: {station_name}")
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
