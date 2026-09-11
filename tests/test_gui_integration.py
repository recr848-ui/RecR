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
