# RecR - Radiko Recording Application

RecR は radiko.jp のラジオ放送を Windows デスクトップで録音するアプリケーションです。ライブ再生・番組表・予約録音を備えた tkinter (sv-ttk) 製の GUI アプリです。

## 機能

- モダンな見た目の GUI（sv-ttk によるライト/ダークテーマ切り替え対応）
- radiko の全ステーションに対応、エリア自動判定
- ライブ再生とグラフィックイコライザー表示（スペクトラム／アナログ針ピークメーターの2モード）
- ラテ欄風の番組表グリッド表示（最大10日分、番組サムネイル付き）
- 番組表からのキーワード検索（局横断・番組名/概要/出演者が対象）
- 番組表からのダブルクリックで、放送中番組の即時録音・未来番組の予約作成
- 予約録音（1回のみ／毎週繰り返し、有効/無効の一括切り替え、複数局同時録音に対応）
- 録音フォーマットは AAC（再エンコードなし）または MP3（ビットレート選択可）
- 録音保存先フォルダのカスタマイズ、各種設定の永続化

## 必要な環境

- Python 3.10 以上（開発は 3.14 で確認）
- Windows 10 以降
- インターネット接続（radiko ストリーミング用）

## インストール

1. このリポジトリをクローンまたはダウンロード
2. プロジェクトディレクトリに移動
3. 依存ライブラリをインストール:

```bash
pip install -r requirements.txt
```

## 使い方

1. アプリケーションを実行:

```bash
python src/main.py
```

2. 「録音」タブでステーション・録音時間・ファイル形式を選択して手動録音、または
3. 「番組表」タブで番組を確認・検索し、ダブルクリックで即時録音／予約登録
4. 「予約録音」タブで登録済みの予約を一覧・編集・有効化/無効化・削除

録音ファイルは既定で `%USERPROFILE%\Music\RecR\` に保存されます（設定メニューから変更可能）。

## プロジェクト構成

```
RecR/
├── src/
│   └── main.py              # メインアプリケーション（GUI）
├── utils/
│   └── radiko_manager.py    # radiko ストリーム取得・録音・番組表・予約管理
├── config/
│   ├── settings.json        # アプリ設定（テーマ、既定局など）
│   ├── reservations.json    # 予約録音データ
│   ├── schedule_cache.json  # 番組表キャッシュ
│   └── images/               # 番組表サムネイル画像のキャッシュ
├── RecR.spec                # PyInstaller ビルド定義
├── requirements.txt         # Python 依存ライブラリ
└── README.md                 # このファイル
```

## 依存ライブラリ

- **requests**: HTTP リクエスト（radiko API・番組表取得）
- **av**: 音声ストリームの取得・デコード・エンコード（AAC/MP3）
- **sounddevice**: ライブ再生・音声デバイス出力
- **numpy**: 音声データ処理、イコライザー表示
- **Pillow**: 番組表サムネイル画像の表示
- **sv-ttk**: モダンなライト/ダークテーマ

## 開発環境セットアップ

```bash
# 仮想環境作成
python -m venv venv

# 仮想環境を有効化
venv\Scripts\activate

# 依存ライブラリをインストール
pip install -r requirements.txt

# アプリケーションを実行
python src/main.py
```

## 実行ファイルのビルド

PyInstaller (`RecR.spec`) を使って単体の Windows 実行ファイルを作成できます。

```bash
pyinstaller RecR.spec
```

生成された `dist/RecR.exe` は Python 環境なしで実行できます。

## ライセンス

このプロジェクトは個人用として提供されています。

## 注記

このアプリケーションは開発中です。今後、さらに機能拡張を予定しています。
