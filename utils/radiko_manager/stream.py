"""ライブ・タイムフリーのプレイリスト取得とHLSセグメントの取得"""

import logging
import queue
import threading
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from xml.etree import ElementTree as ET

import requests

logger = logging.getLogger(__name__)


class StreamMixin:
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
            def build_playlist_url(from_dt):
                return self._timefree_range_playlist_url(playlist_base_url, station_id, from_dt, sub_to_dt)

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

    @staticmethod
    def _timefree_range_playlist_url(playlist_base_url, station_id, from_dt, to_dt):
        """from_dt〜to_dt を取得するタイムフリーのプレイリストURL

        セッション（lsid）ごとに別のURLにする。通信断でセッションが切れたときや、
        中断した続きを取るときは、続きの時刻を from_dt にして作り直す
        """
        lsid = uuid.uuid4().hex
        ft_str = from_dt.strftime("%Y%m%d%H%M%S")
        to_str = to_dt.strftime("%Y%m%d%H%M%S")
        return (
            f"{playlist_base_url}?station_id={station_id}&start_at={ft_str}&ft={ft_str}"
            f"&end_at={to_str}&to={to_str}&l=15&lsid={lsid}&type=b"
        )

    def _fetch_timefree_ranges_parallel(self, playlist_base_url, station_id, auth_token, ranges,
                                        stop_event, on_segment, should_stop=None, log=None):
        """ranges（[(ft_dt, to_dt), ...]）をそれぞれ別セッションで並列に取得する

        _iter_timefree_segments_parallel と違い、取得した順に
        on_segment(区間番号, セグメントの開始時刻, データ) を呼ぶ（取得スレッドから呼ばれる）。
        区間ごとにどこまで取れたかを呼び出し側が持てるので、途中で止めても続きから取り直せる。
        すべての区間が終わるか stop_event がセットされるまで戻らない。should_stop を渡すと
        約1秒ごとに呼び、True を返したら stop_event をセットして止める。

        Returns:
            list: 区間ごとの結果（'done' 最後まで取れた / 'stopped' 途中で止めた / 取得中の例外）
        """
        outcomes = ['stopped'] * len(ranges)

        def worker(idx, sub_ft_dt, sub_to_dt):
            def build_playlist_url(from_dt):
                return self._timefree_range_playlist_url(playlist_base_url, station_id, from_dt, sub_to_dt)

            headers = {"X-Radiko-AuthToken": auth_token}
            try:
                for seg_dt, data in self._iter_timefree_segments(
                    build_playlist_url(sub_ft_dt), headers, stop_event, sub_to_dt,
                    renew_playlist_url=build_playlist_url, log=log, with_times=True,
                ):
                    on_segment(idx, seg_dt, data)
                if not stop_event.is_set():
                    outcomes[idx] = 'done'
            except Exception as e:
                outcomes[idx] = e

        threads = [
            threading.Thread(target=worker, args=(i, r[0], r[1]), daemon=True)
            for i, r in enumerate(ranges)
        ]
        for t in threads:
            t.start()
        try:
            while any(t.is_alive() for t in threads):
                next(t for t in threads if t.is_alive()).join(timeout=1)
                if should_stop is not None and not stop_event.is_set() and should_stop():
                    stop_event.set()
        finally:
            stop_event.set()
            for t in threads:
                t.join(timeout=5)
        return outcomes

    # タイムフリーの取得がこの秒数以内まで to_dt に迫っていれば、medialistの404や
    # 新規セグメントの枯渇を「番組の終わりに達した」とみなす。これより手前で
    # 起きた場合は途中で切れたものとして扱う
    TIMEFREE_END_TOLERANCE_SECONDS = 15
    # セグメントの開始時刻は数ミリ秒早いことがある（例: 21:59:54.996）。その分は最後の
    # セグメントの判定で許す（許さないと to_dt にわずかに届かず、続きが無いことを確かめる
    # 約25秒の待ちに落ちる）
    TIMEFREE_LAST_SEGMENT_SLACK_SECONDS = 1

    def _iter_timefree_segments(self, playlist_url, headers, stop_event, to_dt, on_progress=None,
                                renew_playlist_url=None, log=None, with_times=False):
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

        log: ログの出力先（指紋取得のように本体のログに出したくない場合に渡す）。
        with_times が True なら、(セグメントの開始時刻, データ) の組を生成する
        （開始時刻が分からないセグメントは None）。
        """
        log = log or logger
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
                    log.info(
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
                log.warning(
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
                    remaining = remaining_seconds()
                    if remaining is not None and remaining > self.TIMEFREE_END_TOLERANCE_SECONDS:
                        log.warning(
                            f"タイムフリー: {stall_count}回連続で新規セグメントを取得できず中断しました "
                            f"(last_sequence={last_sequence}、残り約{remaining:.0f}秒)"
                        )
                        raise RuntimeError(
                            f"タイムフリーの取得が途中で止まりました（残り約{remaining:.0f}秒）"
                        )
                    # 区間の終わりまで取り終えた後、続きが無いことを確かめて抜けただけ（正常終了）
                    log.info(
                        f"タイムフリー: 区間の終わりまで取得しました（以降の新規セグメントなし、"
                        f"last_sequence={last_sequence}）"
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
                    if seg_dt + timedelta(
                            seconds=target_duration + self.TIMEFREE_LAST_SEGMENT_SLACK_SECONDS) >= to_dt:
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
                    yield (seg_dt, seg_res.content) if with_times else seg_res.content
                else:
                    failed_count += 1
                    log.warning(
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

        log.info(f"タイムフリー: セグメント取得完了 {done}件（失敗{failed_count}件）")
