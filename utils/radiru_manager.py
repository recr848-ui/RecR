"""
らじる★らじるマネージャーモジュール
NHKの番組はradikoのタイムフリーでは配信されない（ライブのみ）ため、
NHK独自の聴き逃し配信（らじる★らじる）から番組を探して録音するために使う。

聴き逃し側は「番組名→内部ID」を引ける検索APIを持たないため、新着一覧
（new_arrivals）と50音別の番組一覧（series?kana=）を定期的に取得して
ローカルにインデックスとして蓄積し、番組名から引けるようにしておく方式を
取っている。
"""

import json
import logging
import re
import threading
import unicodedata
from datetime import datetime
from functools import lru_cache

import av
import requests

from utils.json_store import write_json_atomic
from utils.paths import get_base_dir

logger = logging.getLogger(__name__)

NEW_ARRIVALS_URL = "https://www.nhk.or.jp/radio-api/app/v1/web/ondemand/corners/new_arrivals"
SERIES_URL = "https://www.nhk.or.jp/radio-api/app/v1/web/ondemand/series"

# 認証・APIキーは不要だが、User-Agentを送らないとリクエストが弾かれることを確認済み
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/96.0",
}

# 50音別の番組一覧（series?kana=）で指定できる行。聴き逃し対象の全番組を
# 列挙でき、new_arrivalsの直近200件から漏れた番組もここから拾える
_KANA_ROWS = ("a", "k", "s", "t", "n", "h", "m", "y", "r", "w")

_ONAIR_DATE_PATTERN = re.compile(r"(\d{1,2})月(\d{1,2})日")
_AA_START_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\+09:00_")
_WHITESPACE_PATTERN = re.compile(r"\s+")


@lru_cache(maxsize=4096)
def _normalize_title(text):
    """番組名の照合用に表記ゆれを吸収する

    radikoの番組表は全角英数・全角記号（「ＮＨＫのど自慢」「マイあさ！」）、
    NHK聴き逃し側は半角（「NHKのど自慢」）と表記が揃っていないため、NFKCで
    寄せたうえで空白を除去して比較する。
    """
    return _WHITESPACE_PATTERN.sub("", unicodedata.normalize("NFKC", text or "")).lower()


class RadiruManager:
    """NHK聴き逃し（らじる★らじる）の番組検索・ダウンロードを担当する"""

    # new_arrivalsは日付やページ指定を受け付けず、常に直近の新着最大200件を
    # 返すスナップショットであることを検証済み。1日あたりの入れ替わりは
    # 20〜50件程度なので、この間隔でも取りこぼしの心配はほぼない
    INDEX_REFRESH_INTERVAL_HOURS = 12

    # 聴き逃しダウンロード中、1パケットも読めないまま読み出しエラーが
    # この回数続いたら、復帰不能とみなして失敗にする
    MAX_CONSECUTIVE_DEMUX_ERRORS = 20

    def __init__(self):
        self.cache_dir = get_base_dir() / "config"
        self.index_file = self.cache_dir / "radiru_series_index.json"
        self._index_lock = threading.Lock()
        self._index_mem = None
        self._index_mtime = None

    # ---- 永続化されたインデックス（番組名 -> series_site_id/corner_site_id） ----

    def _load_index_file(self):
        """インデックスファイル全体を読み込む（存在しない/壊れている場合は空dict）

        schedule_cacheと同様、mtimeが前回と同じならメモリキャッシュを返す。
        """
        if not self.index_file.exists():
            self._index_mem = {}
            self._index_mtime = None
            return {}

        try:
            mtime = self.index_file.stat().st_mtime
        except OSError:
            mtime = None

        if self._index_mem is not None and mtime == self._index_mtime:
            return dict(self._index_mem)

        try:
            with open(self.index_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}

        self._index_mem = data
        self._index_mtime = mtime
        return dict(data)

    def _save_index_file(self, index):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.index_file, index)
        self._index_mem = index
        try:
            self._index_mtime = self.index_file.stat().st_mtime
        except OSError:
            self._index_mtime = None

    @staticmethod
    def _index_key(radio_broadcast, title, corner_name):
        return f"{radio_broadcast}::{title}::{corner_name or ''}"

    def fetch_new_arrivals(self):
        """聴き逃しの新着一覧（直近スナップショット、最大200件）を取得する"""
        r = requests.get(NEW_ARRIVALS_URL, headers=_HEADERS, timeout=15)
        r.raise_for_status()
        return r.json().get("corners", [])

    def fetch_series_list(self):
        """聴き逃し対象の番組一覧を50音の全行ぶん取得する

        new_arrivalsと同じ形（series_site_id/corner_site_id/title等）の要素を返す。
        一部の行の取得に失敗しても、取得できた行のぶんは返す。
        """
        series = []
        for row in _KANA_ROWS:
            try:
                r = requests.get(SERIES_URL, params={"kana": row}, headers=_HEADERS, timeout=15)
                r.raise_for_status()
                series.extend(r.json().get("series", []))
            except (requests.RequestException, ValueError):
                logger.warning(f"NHK聴き逃しの番組一覧取得に失敗しました (kana={row})", exc_info=True)
        return series

    def update_index(self):
        """new_arrivalsと番組一覧を取得し、ローカルインデックスへ差分マージする

        series_site_id/corner_site_idは番組（コーナー）ごとに不変なため、
        一度観測した番組は以後new_arrivalsに出てこなくても引き続き使える。

        Returns:
            int: 新規または変更されたエントリ数
        """
        corners = self.fetch_new_arrivals() + self.fetch_series_list()
        now_iso = datetime.now().isoformat(timespec="seconds")
        changed = 0
        with self._index_lock:
            index = self._load_index_file()
            for c in corners:
                series_id = c.get("series_site_id")
                corner_id = c.get("corner_site_id")
                title = c.get("title")
                if not series_id or not title:
                    continue
                key = self._index_key(c.get("radio_broadcast", ""), title, c.get("corner_name"))
                entry = index.get(key)
                if (
                    entry is None
                    or entry.get("series_site_id") != series_id
                    or entry.get("corner_site_id") != corner_id
                ):
                    changed += 1
                index[key] = {
                    "series_site_id": series_id,
                    "corner_site_id": corner_id,
                    "radio_broadcast": c.get("radio_broadcast", ""),
                    "title": title,
                    "corner_name": c.get("corner_name") or "",
                    "last_seen_at": now_iso,
                }
            if changed:
                self._save_index_file(index)
        logger.info(f"NHK聴き逃しインデックスを更新しました（{changed}件新規/変更）")
        return changed

    def load_index(self):
        """インデックス全体を読み込んで返す

        番組表グリッドの表示など、find_series_for_programを多数回（番組の
        数だけ）呼ぶ場合は、この戻り値を index 引数として渡すことで、
        呼び出しごとのファイル読み込みを1回にまとめられる。
        """
        return self._load_index_file()

    def find_series_for_program(self, radio_broadcast, title, corner_name=None, index=None):
        """番組名からインデックス済みのseries_site_id/corner_site_idを探す

        radikoの番組表のタイトルは「歌謡スクランブル　選▽市川昭介作品集」の
        ように回ごとの副題が付くため、インデックス側の番組名（「歌謡スクランブル」）
        が番組表のタイトルに含まれているかで照合する。複数の番組名が該当する
        場合は最も長い（＝最も具体的な）ものを採る。

        同じ番組に複数のコーナーがある場合（「マイあさ！」の「健康ライフ」等）は、
        コーナー名もタイトルに含まれているものを優先し、無ければコーナー名なしの
        番組本体を返す。どのコーナーか決められない場合は、別コーナーの音声を
        取得してしまわないようNoneを返す。

        index（dict または None）: 事前にload_index()で読み込んだインデックス。
        省略時はこのメソッド内でファイルから読み込む。

        Returns:
            dict または None
        """
        if index is None:
            index = self._load_index_file()

        haystack = _normalize_title(title) + _normalize_title(corner_name)
        candidates = []
        best_len = 0
        for entry in index.values():
            # 両波で放送される番組は radio_broadcast が "R1,FM" のように入る
            if radio_broadcast not in entry.get("radio_broadcast", "").split(","):
                continue
            entry_title = _normalize_title(entry.get("title"))
            if not entry_title or entry_title not in haystack:
                continue
            if len(entry_title) > best_len:
                best_len = len(entry_title)
                candidates = [entry]
            elif len(entry_title) == best_len:
                candidates.append(entry)
        if not candidates:
            return None

        with_corner = [
            (len(corner), entry) for entry in candidates
            if (corner := _normalize_title(entry.get("corner_name"))) and corner in haystack
        ]
        if with_corner:
            return max(with_corner, key=lambda pair: pair[0])[1]
        for entry in candidates:
            if not _normalize_title(entry.get("corner_name")):
                return entry
        return candidates[0] if len(candidates) == 1 else None

    # ---- エピソード検索・ダウンロード ----

    def fetch_series_episodes(self, series_site_id, corner_site_id):
        """指定番組の、現在配信中（未失効）の直近エピソード一覧を取得する

        各エピソードは stream_url（m3u8）と onair_date を持つ。
        """
        r = requests.get(
            SERIES_URL,
            params={"site_id": series_site_id, "corner_site_id": corner_site_id},
            headers=_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("episodes", [])

    @staticmethod
    def _episode_start(episode):
        """エピソードの放送開始日時を返す（分からなければNone）

        aa_contents_idの末尾に「2026-10-05T22:30:00+09:00_2026-10-05T23:20:00+09:00」
        の形で放送枠が入っているため、そこから開始日時（JST、naive）を取り出す。
        """
        m = _AA_START_PATTERN.search(episode.get("aa_contents_id") or "")
        if not m:
            return None
        try:
            return datetime.fromisoformat(m.group(1))
        except ValueError:
            return None

    def find_episode_for_date(self, episodes, target_date, target_end=None):
        """target_date（dateまたはdatetime）に放送されたエピソードを探す

        target_end（datetime または None）: 番組の終了日時。target_dateと
        両方がdatetimeで、エピソード側の放送開始日時も分かる場合は、開始日時が
        番組の放送枠 [target_date, target_end) に入っているものだけを採る。
        同じ日に本放送と再放送がある番組（「マイ・フェイバリット・アルバム」は
        午後6時が再放送、午後10時30分が本放送で、聴き逃しは本放送のみ）で、
        月日だけで照合すると再放送の枠から別の回を取得してしまうため。
        完全一致ではなく枠内判定なのは、コーナーのエピソードは親番組の途中から
        始まるため。

        エピソード側の開始日時が分からない場合は従来どおり、onair_date
        （「9月30日(水)午後9:05放送」のような日本語表記）の月日のみで比較する。
        """
        check_window = isinstance(target_date, datetime) and target_end is not None
        target_md = (target_date.month, target_date.day)
        for ep in episodes:
            ep_start = self._episode_start(ep) if check_window else None
            if ep_start is not None:
                if target_date <= ep_start < target_end:
                    return ep
                continue
            m = _ONAIR_DATE_PATTERN.search(ep.get("onair_date", ""))
            if m and (int(m.group(1)), int(m.group(2))) == target_md:
                return ep
        return None

    def download_episode(self, stream_url, output_path, on_progress=None):
        """聴き逃しのstream_url（m3u8）を音声ファイルとして保存する

        on_progress（callable または None）: on_progress(done_seconds, total_seconds)
        の形で一定間隔ごとに呼ばれる進捗コールバック。番組は30〜50分程度あり
        セグメントを1件ずつ順次取得するため数分かかることがあり、進捗が
        まったく分からないと「取得中のまま固まった」ように見えてしまうため。
        バックグラウンドスレッドから呼ばれるため、GUI操作を行う場合は
        呼び出し側でメインスレッドへのディスパッチが必要。total_secondsは
        VODの尺が事前に分からない場合Noneになることがある。

        radikoのタイムフリーと異なり暗号化・セグメント個別取得は不要で、
        通常のHLS（AES-128暗号化、PyAV/ffmpegが自動復号）としてPyAVから
        直接デマックスできることを検証済み。ただし以下の点に注意が必要。

        1. NHKの配信サーバーは、Rangeヘッダー付きのリクエスト（ffmpegは既定で
           "Range: bytes=0-" を送る）に対して、暗号化セグメントの末尾を
           PKCS7パディングの長さぶん（1〜16バイト）切り詰めて返す。最後の
           AESブロックが欠けるため復号結果の末尾が壊れ、セグメント（6秒）
           ごとに1フレーム〜約0.85秒の音声が欠落し、"Invalid data found"が
           読み出し・mux の両方で散発していた（50分番組で約85秒の欠落）。
           http_seekable=0 でRangeヘッダーを送らないようにすると、欠落も
           エラーも発生しないことを実測で確認済み。
        2. HLSのセグメント境界でタイムスタンプがリセットされる（後方に戻る）
           ことがあり、元のpts/dtsのまま使うと出力ファイル生成時に
           "Invalid data found" で失敗する（radiko側のセグメント結合処理
           _recording_workerと同じ問題）。そのため元の値は無視し、パケットの
           durationを積算した連番のタイムスタンプを振り直す。

        1.の対処後は発生しない見込みだが、サーバー側の挙動が変わった場合に
        全体が失敗しないよう、壊れたパケットのmux失敗は読み飛ばし、読み出し
        側のエラーもdemux()を呼び直して続きから継続する。通信断などで復帰
        しない場合に無限に繰り返さないよう、1パケットも読めないまま連続で
        失敗した回数に上限を設ける。
        """
        logger.info(f"聴き逃しダウンロード開始: {stream_url} -> {output_path}")
        container = av.open(
            stream_url,
            options={
                "headers": f"User-Agent: {_HEADERS['User-Agent']}\r\n",
                # HTTP接続・読み取りが無応答のままハングし続けないよう上限を設ける
                # （単位はマイクロ秒）。セグメント取得先での瞬断等で無限待ちに
                # ならないようにするための安全策
                "timeout": "20000000",
                # Rangeヘッダーを送らない（送るとセグメント末尾が切り詰められて
                # 音声が欠落する。docstringの1.を参照）
                "http_seekable": "0",
            },
        )
        try:
            audio_stream = container.streams.audio[0]
            # VODのHLSはプレイリスト全体の長さが既知のため、
            # container.durationから総尺（秒）を得られることが多い
            total_seconds = container.duration / 1_000_000 if container.duration else None

            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_container = av.open(str(output_path), mode="w")
            try:
                out_stream = output_container.add_stream_from_template(audio_stream)
                pts_counter = 0
                skipped = 0
                packet_count = 0
                demux_errors = 0
                consecutive_demux_errors = 0
                while True:
                    try:
                        for packet in container.demux(audio_stream):
                            consecutive_demux_errors = 0
                            if packet.dts is None:
                                continue
                            duration = packet.duration or 0
                            packet.pts = pts_counter
                            packet.dts = pts_counter
                            packet.stream = out_stream
                            try:
                                output_container.mux(packet)
                            except av.FFmpegError:
                                # "Invalid data found"以外に、実測で"Not yet implemented in
                                # FFmpeg"も稀に発生することを確認済みのため、特定のエラー型に
                                # 絞らずFFmpegError全般を読み飛ばし対象にする
                                skipped += 1
                                continue
                            finally:
                                pts_counter += duration
                            packet_count += 1
                            if on_progress is not None and packet_count % 100 == 0:
                                on_progress(pts_counter * float(audio_stream.time_base), total_seconds)
                        break
                    except av.FFmpegError:
                        demux_errors += 1
                        consecutive_demux_errors += 1
                        if consecutive_demux_errors >= self.MAX_CONSECUTIVE_DEMUX_ERRORS:
                            raise
                if on_progress is not None:
                    on_progress(pts_counter * float(audio_stream.time_base), total_seconds)
                logger.info(
                    f"聴き逃しダウンロード完了: {output_path} "
                    f"({packet_count}パケット, {skipped}件読み飛ばし, 読み出しエラー{demux_errors}回)"
                )
            finally:
                output_container.close()
        finally:
            container.close()
