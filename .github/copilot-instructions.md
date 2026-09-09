<!-- RecR プロジェクト用 Copilot カスタム指示 -->

# RecR プロジェクト - Copilot 指示

## プロジェクト概要
RecR は、radiko.jp のラジオ放送を Windows で tkinter GUI を使用して録音する Python デスクトップアプリケーションです。

## 技術スタック
- **言語**: Python 3.7 以上
- **GUI フレームワーク**: tkinter（標準ライブラリ）
- **音声処理**: pydub, ffmpeg-python
- **ストリーミング**: radiko ライブラリ
- **対応 OS**: Windows

## プロジェクト構造
```
RecR/
├── src/
│   └── main.py              # メイン tkinter アプリケーション
├── utils/
│   └── radiko_manager.py    # radiko ストリーム処理
├── config/                  # 設定ファイル
├── requirements.txt         # 依存ライブラリ
└── README.md               # ドキュメント
```

## 開発ガイドライン
1. GUI には tkinter コンポーネントを使用
2. UI はシンプルで直感的にする
3. エラーは messagebox アラートで適切に処理
4. 録音ファイルはユーザーの Music フォルダに保存
5. 複数の radiko ステーション対応

## 依存ライブラリ
- requests: HTTP 操作
- pydub: 音声処理
- ffmpeg-python: 音声エンコーディング
- radiko: Radiko API クライアント

## セットアップ手順
1. Python 3.7 以上をインストール
2. 仮想環境を作成: `python -m venv venv`
3. 有効化: `venv\Scripts\activate`
4. インストール: `pip install -r requirements.txt`
5. 実行: `python src/main.py`

## テスト項目
- ステーション選択と UI の応答性確認
- 録音ディレクトリの作成確認
- 無効な入力のエラーハンドリング確認

## 次のステップ
- 実際の radiko ストリーム録音機能の実装
- 認証が必要な場合は実装
- 進捗トラッキング機能の実装
- 設定/プリファレンス UI の追加
