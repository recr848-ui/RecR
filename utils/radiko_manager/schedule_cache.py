"""番組表キャッシュ（schedule_cache.json）と番組画像キャッシュ"""

import hashlib
import json
import logging
import os
from datetime import datetime

import requests

from utils.json_store import write_json_atomic

logger = logging.getLogger(__name__)


class ScheduleCacheMixin:
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
