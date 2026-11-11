"""radiko APIからの番組表取得"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

import requests

logger = logging.getLogger(__name__)


class ScheduleFetchMixin:
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
