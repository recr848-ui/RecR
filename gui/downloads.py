"""番組表からのタイムフリー/らじる★らじる聞き逃しダウンロード"""

from tkinter import messagebox
from datetime import datetime, timedelta
import logging
import threading

logger = logging.getLogger(__name__)


class DownloadsMixin:
    def _open_timefree_download_dialog(self, program, station=None, on_success=None):
        """過去の番組をダブルクリックした時: 確認の上、タイムフリーでダウンロードする

        radikoのタイムフリーは放送から概ね1週間で聴取期限が切れるため、
        それを過ぎている番組はダウンロードできない旨を伝えて何もしない。

        station を指定しない場合は番組表タブで選択中の局を対象にする
        （予約一覧からは、予約自身の局を明示的に渡す）。
        on_success（callable または None）は、データを1バイト以上取得できて
        エラーなく完了した場合にのみ呼ばれる（予約一覧からの呼び出しで、
        対象予約を「DL済」にする用途）。
        """
        station = station or self.schedule_station_var.get()
        title = program.get('title') or station
        start_dt, end_dt = self._program_air_window(program)
        if not start_dt or not end_dt:
            messagebox.showinfo("タイムフリー", "この番組の時刻情報を取得できませんでした。")
            return

        now = datetime.now()
        if now < start_dt:
            # 放送前の番組はまだradiko側にタイムフリー音声が存在しない。取得を
            # 試みると失敗するのではなく、現在ライブ中の別番組の内容が誤って
            # 取得されてしまうことを実機で確認したため、事前にブロックする
            messagebox.showinfo(
                "タイムフリー",
                f"「{title}」はまだ放送されていないため、タイムフリーで取得できません。\n"
                "放送終了後に改めてお試しください。"
            )
            return

        # NHKはradikoではライブ配信のみでタイムフリーを一切提供しておらず
        # （取得自体は成功するが、内容が「配信しておりません」という案内音声に
        # なる）、代わりにNHK独自の聴き逃し配信（らじる★らじる）を使う
        if self._is_nhk_station(station):
            self._open_nhk_radiru_download_dialog(
                program, station, title, start_dt, end_dt, on_success
            )
            return

        if now - end_dt > timedelta(days=7):
            messagebox.showinfo(
                "タイムフリー",
                f"「{title}」はタイムフリーの聴取期限（放送から約1週間）を過ぎているため、"
                "取得できません。"
            )
            return

        if self.manager.is_download_limit_reached():
            messagebox.showinfo(
                "タイムフリー",
                "タイムフリーの同時ダウンロード数が上限"
                f"（{self.manager.max_concurrent_timefree_downloads}件）に達しています。\n"
                "他のダウンロードが終わってから、もう一度お試しください。\n"
                "上限は「設定」メニューから変更できます。"
            )
            return

        if not messagebox.askyesno(
            "タイムフリー", f"「{title}」をタイムフリーでダウンロードしますか？"
        ):
            return

        ft = start_dt.strftime("%Y%m%d%H%M%S")
        to = end_dt.strftime("%Y%m%d%H%M%S")
        if self.manager.is_download_active(station, ft):
            messagebox.showerror("エラー", f"{station} のこの番組は既に取得中です")
            return

        file_format = self.format_var.get()
        try:
            mp3_bitrate = int(self.bitrate_var.get())
        except ValueError:
            mp3_bitrate = 192

        key = f"{station}|{ft}"
        self._register_active_download(key, station, ft, title)

        self._set_app_busy(True)
        try:
            success, output_path = self.manager.start_timefree_download(
                station, ft, to, file_format=file_format, mp3_bitrate=mp3_bitrate,
                title=title, filename_pattern=self.filename_pattern_var.get(),
                metadata=self._build_recording_metadata(station, program=program, fallback_title=title),
                on_progress=lambda done, total, k=key: self.root.after(
                    0, self._update_active_download_progress, k, done, total
                ),
                on_complete=lambda had_data, path, error, k=key, st=station, t=title: self.root.after(
                    0, self._on_timefree_download_complete, k, st, t, had_data, path, error, on_success
                )
            )
        finally:
            self._set_app_busy(False)

        if not success:
            self._unregister_active_download(key)
            messagebox.showerror(
                "タイムフリー",
                f"{station} の「{title}」を取得できませんでした。\n通信状況をご確認ください。"
            )

    def _on_timefree_download_complete(self, key, station, title, had_data, output_path, error,
                                        on_success=None):
        """タイムフリーのダウンロード終了時（メインスレッドから呼ばれる）:
        データを1バイトも取得できなかった場合に警告する

        on_success は、データを取得できてエラーもなかった場合にのみ呼ぶ
        （途中で通信エラーが起きた場合は「完了できた」とは見なさない）。
        """
        self._unregister_active_download(key)
        if hasattr(self, 'files_tree'):
            self._refresh_files_list()
        if had_data:
            self._request_cm_chapters(output_path)
        if not had_data:
            detail = f"\n（{error}）" if error else ""
            message = (
                f"{station} の「{title}」のタイムフリー取得でデータを取得できませんでした"
                f"（ファイルが空です）。\n通信状況をご確認ください。{detail}"
            )
            if self._tray_icon is not None:
                self._notify_via_tray("タイムフリー", message)
            else:
                messagebox.showwarning("タイムフリー", message)
        elif error:
            message = (
                f"{station} の「{title}」のタイムフリー取得は途中で通信エラーが発生し、中断されました。\n"
                f"それまでの内容は保存されています。\n"
                f"保存先: {output_path}\n（{error}）"
            )
            if self._tray_icon is not None:
                self._notify_via_tray("タイムフリー", message)
            else:
                messagebox.showwarning("タイムフリー", message)
        else:
            if self._tray_icon is not None:
                self._notify_via_tray("タイムフリー", f"{station} の「{title}」の取得が完了しました。")
            if on_success is not None:
                on_success()

    @staticmethod
    def _is_nhk_station(station):
        """局名がNHK（NHK AM/NHK FM等、地域名付き表記を含む）かどうかを判定する"""
        return station.startswith("NHK")

    @staticmethod
    def _nhk_radio_broadcast_code(station):
        """局名からNHK聴き逃し側の放送区分コード（"FM" または "R1"）を求める"""
        return "FM" if "FM" in station else "R1"

    def _open_nhk_radiru_download_dialog(self, program, station, title, start_dt, end_dt, on_success):
        """NHKの番組をダブルクリックした時: らじる★らじるの聴き逃し配信から取得する

        radikoのタイムフリーはNHKに一切対応していない（ライブのみ）ため、NHKの
        番組はこちらのルートに誘導する。聴き逃し側は番組名からの検索APIを
        持たないため、定期的に蓄積しているローカルインデックス
        （radiru_series_index.json）から番組名で引き当てる。インデックスに
        無い場合や、その番組がそもそもNHKの聴き逃し対象外の場合は、その旨を
        伝えて終了する。
        """
        radio_broadcast = self._nhk_radio_broadcast_code(station)
        corner_name = program.get('corner_name') if isinstance(program, dict) else None

        self._set_app_busy(True)
        try:
            entry = self.radiru_manager.find_series_for_program(radio_broadcast, title, corner_name)
            if entry is None:
                messagebox.showinfo(
                    "NHK聴き逃し",
                    f"「{title}」はNHK聴き逃し配信のインデックスにまだ登録されていないか、"
                    "聴き逃し非対応の番組です。\n"
                    "インデックスは定期的に自動更新されるため、時間をおいて再度お試しください。"
                )
                return

            try:
                episodes = self.radiru_manager.fetch_series_episodes(
                    entry['series_site_id'], entry['corner_site_id']
                )
            except Exception:
                logger.exception(f"NHK聴き逃しのエピソード一覧取得に失敗しました ({title})")
                messagebox.showerror(
                    "NHK聴き逃し", f"「{title}」のエピソード情報を取得できませんでした。\n通信状況をご確認ください。"
                )
                return

            episode = self.radiru_manager.find_episode_for_date(episodes, start_dt, end_dt)
            if episode is None:
                messagebox.showinfo(
                    "NHK聴き逃し",
                    f"「{title}」の{start_dt.strftime('%m/%d')}放送分は、NHK聴き逃し配信の"
                    "対象外か、既に配信期限（放送から約1週間）を過ぎています。"
                )
                return

            stream_url = episode.get('stream_url')
            if not stream_url:
                messagebox.showinfo("NHK聴き逃し", f"「{title}」の配信データを取得できませんでした。")
                return

            if not messagebox.askyesno(
                "NHK聴き逃し", f"「{title}」（{start_dt.strftime('%m/%d')}放送）をNHK聴き逃し配信から"
                "ダウンロードしますか？"
            ):
                return

            filename = self.manager.build_recording_filename(
                self.filename_pattern_var.get(), station, title, start_dt, "m4a"
            )
            output_path = self.manager.unique_output_path(self.manager.output_dir / filename)
            metadata = self._build_recording_metadata(station, program=program, fallback_title=title)

            ft = start_dt.strftime("%Y%m%d%H%M%S")
            key = f"{station}|radiru|{ft}"
            self._register_active_download(key, station, ft, title)

            thread = threading.Thread(
                target=self._nhk_radiru_download_worker,
                args=(stream_url, output_path, metadata, key, station, title, on_success),
                daemon=True,
            )
            thread.start()
        finally:
            self._set_app_busy(False)

    def _nhk_radiru_download_worker(self, stream_url, output_path, metadata, key, station, title,
                                     on_success):
        """NHK聴き逃しのダウンロード本体（バックグラウンドスレッド）"""
        error = None
        try:
            self.radiru_manager.download_episode(
                stream_url, output_path,
                on_progress=lambda done, total, k=key: self.root.after(
                    0, self._update_active_download_progress, k, done, total
                ),
            )
            self.manager.write_metadata_tags(output_path, "m4a", metadata)
        except Exception as e:
            logger.exception(f"NHK聴き逃しのダウンロードに失敗しました ({title})")
            error = str(e)
        self.root.after(
            0, self._on_nhk_radiru_download_complete, key, station, title, output_path, error, on_success
        )

    def _on_nhk_radiru_download_complete(self, key, station, title, output_path, error, on_success):
        """NHK聴き逃しのダウンロード終了時（メインスレッドから呼ばれる）"""
        self._unregister_active_download(key)
        if hasattr(self, 'files_tree'):
            self._refresh_files_list()

        # 途中で失敗した場合も冒頭だけのファイルが残るため、ファイルの有無
        # だけで成功扱いにせず、エラーがあれば失敗として知らせる
        had_data = output_path.exists() and output_path.stat().st_size > 0
        if error or not had_data:
            detail = f"\n（{error}）" if error else ""
            message = (
                f"{station} の「{title}」のNHK聴き逃し取得に失敗しました。\n"
                f"通信状況をご確認ください。{detail}"
            )
            if self._tray_icon is not None:
                self._notify_via_tray("NHK聴き逃し", message)
            else:
                messagebox.showwarning("NHK聴き逃し", message)
        else:
            if self._tray_icon is not None:
                self._notify_via_tray("NHK聴き逃し", f"{station} の「{title}」の取得が完了しました。")
            if on_success is not None:
                on_success()
