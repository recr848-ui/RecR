"""RecRApp（実際のTkウィンドウ + RadikoManager）を組み合わせた結合テスト。

単体テスト（test_radiko_manager.py / test_reservation_logic.py）はロジック単体を
対象にしているのに対し、ここでは実際のTkウィジェット生成やGUIクラスの初期化処理を
通して、「毎分/定期的に自身を再スケジュールするループが、処理中に例外が起きても
止まらずに回り続けるか」を確認する。長時間起動しっぱなしの運用で、この再スケジュール
が一度でも止まると該当機能（予約チェック・予約一覧更新・番組表更新）が復旧不能に
なるため、単体テストでは検出しにくいこの振る舞いを対象にしている。

ネットワークアクセス（エリア判定・番組表取得）は一切行わないようRadikoManagerを
モックし、tkinterのTkウィンドウのみ実物を使う。

Tkの実ウィンドウはテストごとに作り直すとTcl側の状態がまれに壊れて
「main thread is not in main loop」等でflakyになるため、モジュール内で
1つだけ生成して使い回す（各テストではその上にRecRAppを作り直し、
ウィジェットだけ後始末する）。
"""
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import messagebox

import pytest
import sv_ttk

from utils.radiko_manager import RadikoManager
from src.main import RecRApp


@pytest.fixture(scope="module")
def shared_root():
    try:
        root = tk.Tk()
        root.withdraw()
    except tk.TclError:
        pytest.skip("Tkが使えるディスプレイがない環境のためスキップ")
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


@pytest.fixture
def app(monkeypatch, tmp_path, shared_root):
    """ネットワークを一切使わない、実Tkウィンドウ上のRecRAppインスタンス"""
    monkeypatch.setattr(RadikoManager, "_detect_area_stations", lambda self: None)
    monkeypatch.setattr(
        RadikoManager, "get_program_schedule", lambda self, station, days=10: []
    )
    monkeypatch.setattr(
        RadikoManager, "get_sample_schedule", lambda self, station, days=10: []
    )
    monkeypatch.setattr(RadikoManager, "get_timefree_schedule", lambda self, station: [])
    # messagebox系は全部モックする。showwarning等を1つでも生で残すと、
    # 起動時の _refresh_stale_stations 等から本物のポップアップダイアログが
    # 実際の画面上に表示されてしまう（実際に一度これで表示させてしまった）
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: False)
    monkeypatch.setattr(messagebox, "showwarning", lambda *a, **k: None)
    monkeypatch.setattr(messagebox, "showerror", lambda *a, **k: None)
    monkeypatch.setattr(messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(messagebox, "askokcancel", lambda *a, **k: False)
    # 全局番組表の自動更新は本テストの対象外。バックグラウンドスレッドが
    # テスト終了後（Tk破棄後）に root.after を呼んでエラーになるのを避ける
    monkeypatch.setattr(RecRApp, "_start_full_schedule_refresh", lambda self: None)
    # お知らせ欄の取得も同じ理由（実ネットワークアクセス・テスト終了後の
    # バックグラウンドスレッド）でテスト対象外とする
    monkeypatch.setattr(RecRApp, "_refresh_notice", lambda self: None)
    # sv_ttkは同一プロセス内で複数回テーマを読み込むと
    # 「Theme ... already exists」で落ちるため、テストでは適用自体をスキップする
    monkeypatch.setattr(sv_ttk, "set_theme", lambda *a, **k: None)
    # quit_app()はTkウィンドウそのものを破棄するが、rootはモジュール内で
    # 使い回すため、実際の破棄はモジュール終了時のshared_rootに任せる
    monkeypatch.setattr(shared_root, "destroy", lambda: None)

    orig_init = RadikoManager.__init__

    def patched_init(self):
        orig_init(self)
        self.output_dir = tmp_path / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = tmp_path / "config"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / "schedule_cache.json"
        self.settings_file = self.cache_dir / "settings.json"
        self.reservations_file = self.cache_dir / "reservations.json"
        self.freeword_file = self.cache_dir / "freeword_keywords.json"

    monkeypatch.setattr(RadikoManager, "__init__", patched_init)

    recr_app = RecRApp(shared_root)
    yield recr_app

    # after()に積まれた定期ジョブが残ったままだと次のテスト（や後始末後）にも
    # コールバックが飛んでしまうため、明示的に全部キャンセルしてから
    # ウィジェットを後片付けする（rootそのものは次のテストのために残す）
    for job_attr in (
        "_reservation_check_job",
        "_reservation_list_refresh_job",
        "_program_guide_refresh_job",
        "_full_refresh_check_job",
        "_eq_update_job",
        "_recording_watch_job",
        "_rec_blink_job",
    ):
        job = getattr(recr_app, job_attr, None)
        if job:
            try:
                shared_root.after_cancel(job)
            except tk.TclError:
                pass
    for widget in list(shared_root.winfo_children()):
        widget.destroy()
    shared_root.config(menu="")


def test_startup_schedules_all_three_self_rescheduling_loops(app):
    """起動時に、予約チェック・予約一覧更新・番組表更新の3つのループが仕込まれている"""
    assert app._reservation_check_job is not None
    assert app._reservation_list_refresh_job is not None
    assert app._program_guide_refresh_job is not None


def test_reservation_check_reschedules_even_if_check_due_reservations_raises(app, monkeypatch):
    """_check_due_reservationsが例外を投げても、次回分は必ず再予約される"""
    app.root.after_cancel(app._reservation_check_job)
    app._reservation_check_job = None
    monkeypatch.setattr(
        app, "_check_due_reservations", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    app._schedule_reservation_check()  # 例外を投げずに完走すること

    assert app._reservation_check_job is not None


def test_reservation_list_refresh_reschedules_even_if_refresh_raises(app, monkeypatch):
    """_refresh_reservation_listが例外を投げても、次回分は必ず再予約される"""
    app.root.after_cancel(app._reservation_list_refresh_job)
    app._reservation_list_refresh_job = None
    monkeypatch.setattr(
        app, "_refresh_reservation_list", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    app._schedule_reservation_list_minute_refresh()

    assert app._reservation_list_refresh_job is not None


def test_program_guide_refresh_reschedules_even_if_display_schedule_raises(app, monkeypatch):
    """display_scheduleが例外を投げても、次回分は必ず再予約される"""
    app.root.after_cancel(app._program_guide_refresh_job)
    app._program_guide_refresh_job = None
    monkeypatch.setattr(
        app, "display_schedule", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    app._schedule_program_guide_minute_refresh()

    assert app._program_guide_refresh_job is not None


def test_quit_app_cancels_program_guide_job(app):
    """終了処理で番組表の再スケジュールジョブも他のジョブと同様に確実にキャンセルされる"""
    assert app._program_guide_refresh_job is not None
    app.quit_app()
    assert app._program_guide_refresh_job is None


def test_delete_finished_reservations_includes_timefree_downloaded_without_opt_in(
    app, monkeypatch
):
    """タイムフリーで取得済み（DL済）の予約は、last_resultがfailedのままでも
    「失敗を含める」を選ばなくても削除対象に入り、本当に未解決の失敗予約は残る
    """
    downloaded = app.manager.add_reservation({
        'station': 'TBSラジオ', 'date_iso': '2026-09-01', 'start': '05:00', 'end': '06:00',
        'title': 'DL済みで解決済みの予約', 'repeat': 'once',
        'last_run_date': '2026-09-01', 'last_result': 'failed', 'timefree_downloaded': True,
    })
    # 録音自体は一度も実行されず（last_run_date未設定のまま）、タイムフリーで
    # 直接取得できたケース（実際に報告された状況）
    downloaded_never_run = app.manager.add_reservation({
        'station': 'NHK AM（東京）', 'date_iso': '2026-09-07', 'start': '21:05', 'end': '21:55',
        'title': 'last_run_date未設定のままDL済みの予約', 'repeat': 'once',
        'last_run_date': None, 'timefree_downloaded': True,
    })
    unresolved = app.manager.add_reservation({
        'station': 'TBSラジオ', 'date_iso': '2026-09-01', 'start': '07:00', 'end': '08:00',
        'title': '本当に未解決の失敗予約', 'repeat': 'once',
        'last_run_date': '2026-09-01', 'last_result': 'failed',
    })
    succeeded = app.manager.add_reservation({
        'station': 'TBSラジオ', 'date_iso': '2026-09-01', 'start': '09:00', 'end': '10:00',
        'title': '普通に成功した予約', 'repeat': 'once',
        'last_run_date': '2026-09-01', 'last_result': 'success',
    })

    # 確認ダイアログでは「失敗を含める」のチェックを入れない操作を模擬する
    monkeypatch.setattr(app, "_confirm_delete_finished_reservations", lambda *a, **k: False)

    app._delete_finished_reservations()

    remaining_ids = {r['id'] for r in app.manager.load_reservations()}
    assert downloaded['id'] not in remaining_ids
    assert downloaded_never_run['id'] not in remaining_ids
    assert succeeded['id'] not in remaining_ids
    assert unresolved['id'] in remaining_ids


def test_menu_checkbutton_indicator_is_visible_against_menu_background(app):
    """ダークテーマ時、メニューのチェック/ラジオ印（selectcolor）が背景色と
    同化して見えなくならないこと（ライト・ダーク両方で背景と別の色であること）

    実機で「ダークテーマにするとメニューのチェックがどこについているか分からない」
    という報告があったための回帰テスト。tk.MenuはttkテーマではなくOS既定色に
    依存するため、背景色とselectcolorを明示的に管理している
    """
    for theme in ("light", "dark"):
        app.theme_var.set(theme)
        app._on_theme_changed()
        assert app._menus, "メニューが1つも登録されていない"
        for menu in app._menus:
            bg = str(menu.cget("bg"))
            select_color = str(menu.cget("selectcolor"))
            assert select_color, f"selectcolorが未設定（{theme}テーマ）"
            assert select_color.lower() != bg.lower(), (
                f"{theme}テーマでチェック印の色（{select_color}）が"
                f"メニュー背景（{bg}）と同化している"
            )


def _find_widgets(root, cls_name=None, text=None):
    """widget木を再帰的に探索し、クラス名/textが一致するウィジェットを全て返す"""
    results = []
    for child in root.winfo_children():
        matches = True
        if cls_name is not None and child.winfo_class() != cls_name:
            matches = False
        if matches and text is not None:
            try:
                if str(child.cget('text')) != text:
                    matches = False
            except tk.TclError:
                matches = False
        if matches:
            results.append(child)
        results.extend(_find_widgets(child, cls_name, text))
    return results


def test_editing_freeword_reservation_to_weekly_clears_freeword_source(app):
    """フリーワード由来の単発予約を編集ダイアログで「毎週」に切り替えて保存すると、
    source/keyword_idがクリアされ、以後は通常の手動予約として扱われること。

    クリアしないと、この予約のdate_isoが元の1回分の放送日のまま更新されないため、
    フリーワードの重複判定に毎回ひっかからず、同じキーワードに一致する将来の回を
    別の単発予約として際限なく自動作成し続けてしまう不具合があった
    """
    reservation = app.manager.add_reservation({
        'station': 'TBSラジオ', 'repeat': 'once', 'date_iso': '2026-09-01',
        'start': '05:00', 'end': '06:00', 'title': 'テスト番組',
        'source': 'freeword', 'keyword_id': 'kw1',
    })

    toplevels_before = set(app.root.winfo_children())
    app._open_reservation_dialog(existing=reservation)
    new_toplevels = [
        w for w in app.root.winfo_children()
        if w not in toplevels_before and isinstance(w, tk.Toplevel)
    ]
    assert len(new_toplevels) == 1, "予約編集ダイアログが開かれていない"
    dialog = new_toplevels[0]

    try:
        weekly_radios = _find_widgets(dialog, cls_name="TRadiobutton", text="毎週")
        assert weekly_radios, "「毎週」ラジオボタンが見つからない"
        weekly_radios[0].invoke()

        save_buttons = _find_widgets(dialog, cls_name="TButton", text="保存")
        assert save_buttons, "「保存」ボタンが見つからない"
        save_buttons[0].invoke()
    finally:
        if dialog.winfo_exists():
            dialog.destroy()

    updated = app.manager.get_reservation(reservation['id'])
    assert updated['repeat'] == 'weekly'
    assert updated.get('source') is None
    assert updated.get('keyword_id') is None


def _canvas_texts(canvas):
    return [
        canvas.itemcget(item, "text")
        for item in canvas.find_withtag("all")
        if canvas.type(item) == "text"
    ]


def test_display_schedule_shows_placeholder_when_empty(app):
    """番組表が0件のとき、目盛りだけの空グリッドを放置せず案内メッセージを出す"""
    app.display_schedule([], mode='upcoming')
    texts = _canvas_texts(app.schedule_canvas)
    assert any("番組表がありません" in t for t in texts)

    app.display_schedule([], mode='timefree')
    texts = _canvas_texts(app.schedule_canvas)
    assert any("タイムフリー番組表がありません" in t for t in texts)


def test_display_schedule_hides_placeholder_when_programs_exist(app):
    """番組が1件でもあれば、空状態の案内メッセージは出ない"""
    programs = [{
        'date': '9/11(金)', 'date_iso': '2099-01-01', 'start': '09:00', 'end': '10:00',
        'title': 'テスト番組',
    }]
    app.display_schedule(programs, mode='upcoming')
    texts = _canvas_texts(app.schedule_canvas)
    assert not any("番組表がありません" in t for t in texts)


def test_notice_text_is_read_only_and_updates_via_set_notice_text(app):
    """お知らせ欄は読み取り専用（ユーザーが誤って編集できない）で、
    _set_notice_textで内容とステータスを差し替えられること
    """
    assert str(app.notice_text.cget("state")) == "disabled"

    app._set_notice_text("新しいお知らせ本文", status="")
    assert app.notice_text.get("1.0", "end-1c") == "新しいお知らせ本文"
    assert str(app.notice_text.cget("state")) == "disabled"
    assert app.notice_status_var.get() == ""

    app._set_notice_text("取得失敗時の文面", status="取得失敗")
    assert app.notice_text.get("1.0", "end-1c") == "取得失敗時の文面"
    assert app.notice_status_var.get() == "取得失敗"


def test_notice_text_colors_track_theme(app):
    """お知らせ欄（tk.Text）もメニューやCanvasと同様、テーマ切り替えで
    背景色が更新されること（同化して読めなくなる回帰を防ぐ）
    """
    app.theme_var.set("light")
    app._on_theme_changed()
    light_bg = str(app.notice_text.cget("bg"))

    app.theme_var.set("dark")
    app._on_theme_changed()
    dark_bg = str(app.notice_text.cget("bg"))

    assert light_bg != dark_bg


def test_schedule_font_size_levels_scale_fonts_and_grid_height(app):
    """番組表の文字サイズ設定（小/中/大）で、文字サイズが2ptずつ増え、
    グリッドの縦方向の拡大率（PIXELS_PER_MINUTE、ひいてはグリッドの高さ）も
    タイトルの文字サイズに比例して大きくなること
    """
    app.display_schedule([], mode='upcoming')  # _schedule_grid_heightを確定させる

    expected = {
        'small': {'title': 8, 'desc': 7, 'hour_label': 8, 'date_header': 9, 'ppm': 2.0},
        'medium': {'title': 10, 'desc': 9, 'hour_label': 10, 'date_header': 11, 'ppm': 2.5},
        'large': {'title': 12, 'desc': 11, 'hour_label': 12, 'date_header': 13, 'ppm': 3.0},
    }

    grid_heights = {}
    for level, exp in expected.items():
        app.schedule_font_size_var.set(level)
        app._on_schedule_font_size_changed()

        assert app.schedule_title_font.cget('size') == exp['title']
        assert app.schedule_title_link_font.cget('size') == exp['title']
        assert app.schedule_desc_font.cget('size') == exp['desc']
        assert app._schedule_hour_label_size == exp['hour_label']
        assert app._schedule_date_header_size == exp['date_header']
        assert app.PIXELS_PER_MINUTE == pytest.approx(exp['ppm'])
        grid_heights[level] = app._schedule_grid_height

    assert grid_heights['small'] < grid_heights['medium'] < grid_heights['large']


def test_timefree_schedule_refetches_when_cache_is_entirely_out_of_window(app, monkeypatch):
    """タイムフリーのキャッシュが7日以上前に取得したままで、対象期間（当日を含む
    過去7日間）を丸ごと過ぎ去っている場合、キャッシュがあっても空グリッドで
    済ませず再取得を促すこと。

    実際にNHK FMでこれが発生していた（9日前に取得したキャッシュが残っていて、
    display_scheduleの日付フィルタで全件除外され、空の番組表になっていた）
    """
    station = "TBSラジオ"
    stale_programs = [{
        'date': '9/3(木)', 'date_iso': '2026-09-03', 'start': '05:00', 'end': '06:00',
        'title': '9日以上前の古いキャッシュ番組',
    }]
    app.manager.save_schedule_cache(f"{station}::timefree", stale_programs)

    fresh_programs = [{
        'date': '9/18(金)', 'date_iso': '2026-09-18', 'start': '05:00', 'end': '06:00',
        'title': '再取得された新しい番組',
    }]
    monkeypatch.setattr(app.manager, "get_timefree_schedule", lambda st: fresh_programs)

    asked = {}
    def fake_askyesno(*a, **k):
        asked['called'] = True
        return True
    monkeypatch.setattr(messagebox, "askyesno", fake_askyesno)

    app.schedule_station_var.set(station)
    app.load_timefree_schedule_for_current_station()

    assert asked.get('called'), "古いキャッシュのまま再取得を促さなかった"
    assert app._current_programs == fresh_programs


def test_timefree_schedule_uses_cache_when_still_within_window(app, monkeypatch):
    """タイムフリーのキャッシュに対象期間内のデータが残っていれば、
    再取得を促さずそのキャッシュをそのまま使うこと（正常系の回帰防止）
    """
    station = "TBSラジオ"
    fresh_cached = [{
        'date': '9/17(木)', 'date_iso': '2026-09-17', 'start': '05:00', 'end': '06:00',
        'title': '期間内のキャッシュ番組',
    }]
    app.manager.save_schedule_cache(f"{station}::timefree", fresh_cached)

    def fail_if_called(*a, **k):
        raise AssertionError("キャッシュが有効なのに再取得の確認ダイアログを出した")
    monkeypatch.setattr(messagebox, "askyesno", fail_if_called)

    app.schedule_station_var.set(station)
    app.load_timefree_schedule_for_current_station()

    assert app._current_programs == fresh_cached


def test_schedule_font_size_setting_persists(app):
    """番組表の文字サイズ設定は、次回起動時に反映されるよう保存されること"""
    app.schedule_font_size_var.set('large')
    app._on_schedule_font_size_changed()
    assert app.manager.load_settings().get('schedule_font_size') == 'large'


def test_full_schedule_refresh_also_refreshes_timefree_cache(app, monkeypatch):
    """毎日の全局自動更新（_full_schedule_refresh_worker）で、通常の番組表だけで
    なくタイムフリー（過去7日間）のキャッシュも一緒に更新されること。

    以前はこの自動更新がタイムフリー側のキャッシュに一切触れておらず、該当局の
    タイムフリータブを開かない限り何日経っても更新されなかった（対象期間を
    丸ごと過ぎ去ってから開くと空の番組表になる不具合があった）
    """
    # prune_stale_schedule_cacheが実行されるため、テスト環境のフォールバック局
    # 一覧（_DEFAULT_STATION_MAPPING）に実在する局名を使う必要がある
    station = next(iter(app.manager.station_mapping))
    today_iso = datetime.now().strftime("%Y-%m-%d")

    upcoming_programs = [{
        'date': '1/1(木)', 'date_iso': '2099-01-01', 'start': '09:00', 'end': '10:00',
        'title': '未来の番組',
    }]
    timefree_programs = [{
        'date': '今日', 'date_iso': today_iso, 'start': '05:00', 'end': '06:00',
        'title': '過去の番組',
    }]
    monkeypatch.setattr(
        app.manager, "get_program_schedule", lambda station, days=10: upcoming_programs
    )
    monkeypatch.setattr(app.manager, "get_timefree_schedule", lambda station: timefree_programs)
    monkeypatch.setattr(app.manager, "scan_freewords_for_station", lambda station, programs: [])

    app._full_schedule_refresh_worker([station], today_iso)
    for _ in range(10):
        app.root.update()

    assert app.manager.load_cached_schedule(station) == upcoming_programs
    assert app.manager.load_cached_schedule(f"{station}::timefree") == timefree_programs


def test_fetch_and_cache_schedule_also_refreshes_timefree_cache(app, monkeypatch):
    """fetch_and_cache_schedule（「番組表を取得」ボタン・起動時の自動更新・
    「番組表を一括更新」が共通で通る経路）を呼ぶと、通常の番組表と同じ
    タイミングでタイムフリー（過去7日間）のキャッシュも一緒に更新されること
    """
    station = next(iter(app.manager.station_mapping))
    today_iso = datetime.now().strftime("%Y-%m-%d")

    upcoming_programs = [{
        'date': '1/1(木)', 'date_iso': '2099-01-01', 'start': '09:00', 'end': '10:00',
        'title': '未来の番組',
    }]
    timefree_programs = [{
        'date': '今日', 'date_iso': today_iso, 'start': '05:00', 'end': '06:00',
        'title': '過去の番組',
    }]
    monkeypatch.setattr(
        app.manager, "get_program_schedule", lambda station, days=10: upcoming_programs
    )
    monkeypatch.setattr(app.manager, "get_timefree_schedule", lambda station: timefree_programs)

    app.fetch_and_cache_schedule(station)

    assert app.manager.load_cached_schedule(f"{station}::timefree") == timefree_programs


def test_fetch_and_cache_schedule_ignores_timefree_fetch_failure(app, monkeypatch):
    """タイムフリー側の取得が例外を投げても、通常の番組表取得自体は失敗にしない
    （ベストエフォート）こと
    """
    station = next(iter(app.manager.station_mapping))
    upcoming_programs = [{
        'date': '1/1(木)', 'date_iso': '2099-01-01', 'start': '09:00', 'end': '10:00',
        'title': '未来の番組',
    }]
    monkeypatch.setattr(
        app.manager, "get_program_schedule", lambda station, days=10: upcoming_programs
    )

    def raise_err(station):
        raise RuntimeError("boom")
    monkeypatch.setattr(app.manager, "get_timefree_schedule", raise_err)

    programs, success = app.fetch_and_cache_schedule(station)

    assert success is True
    assert programs == upcoming_programs


def test_timefree_columns_ordered_oldest_to_newest_left_to_right(app):
    """タイムフリー番組表の列は、通常の番組表と同じく左が過去・右が現在になる
    よう並ぶこと（get_timefree_scheduleの取得順は「今日→過去」なので、
    表示側で並べ替えないと逆順になってしまっていた）
    """
    now = datetime.now()
    programs = []
    # 取得順をそのまま模した並び: 今日(offset0)→過去(offset1..3)
    for offset in range(4):
        d = now - timedelta(days=offset)
        programs.append({
            'date': f'{d.month}/{d.day}', 'date_iso': d.strftime('%Y-%m-%d'),
            'start': '05:00', 'end': '06:00', 'title': f'offset{offset}',
        })

    app.display_schedule(programs, mode='timefree')

    labels = [
        w.cget('text')
        for cell in app.day_header_frame.winfo_children()
        for w in cell.winfo_children()
        if w.winfo_class() == 'TLabel'
    ]
    expected = [
        f"{(now - timedelta(days=offset)).month}/{(now - timedelta(days=offset)).day}"
        for offset in range(3, -1, -1)
    ]
    assert labels == expected
