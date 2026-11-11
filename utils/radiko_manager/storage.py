"""設定・予約・フリーワードの保存と、自動バックアップ"""

import logging
import os
import threading
import uuid
from datetime import datetime, timedelta

from utils.json_store import read_json, write_json_atomic

logger = logging.getLogger(__name__)


class StorageMixin:
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
            elif grace_end < now < record_end_dt:
                # 録音中なら直前の巡回で last_run_date が入らないまま進行中なだけなので除く
                key = (res.get('id'), occurrence_iso)
                if key not in self._missed_start_logged and not self.is_recording_active(
                    res.get('station'), reservation_id=res.get('id')
                ):
                    self._missed_start_logged.add(key)
                    logger.warning(
                        f"予約の開始猶予（{self.RESERVATION_GRACE_SECONDS}秒）を過ぎても録音が始まりませんでした: "
                        f"{res.get('station')} {occurrence_iso} {res.get('start')} {res.get('title')}"
                    )

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
