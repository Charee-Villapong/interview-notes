# mac-app（Mac側ツール）

面談録音ツール（Mac 常駐のメニューバーツール）のソース。仕様と連携は `docs/P3計画.md`、状態と削除条件の正本は `docs/状態管理.md`、ビルドと配置の実働手順は `docs/P3実働手順.md`。

## 構成

| ファイル | 役割 |
|---|---|
| `Sources/InterviewRecorder/main.swift` / `AppDelegate.swift` | 起動（メニューバー常駐）と配線 |
| `StatusBarController.swift` | メニュー UI（録音開始／停止、状態表示、削除まわり） |
| `RecordingController.swift` | 録音の状態機械（無音検知・猶予・3時間の安全弁・確定と転送） |
| `SystemAudioCapture.swift` | システム音声（相手）：ScreenCaptureKit |
| `MicCapture.swift` | マイク（自分）：AVAudioEngine |
| `SilenceMonitor.swift` / `AudioLevel.swift` | 無音検知とレベル計算 |
| `ManifestBuilder.swift` | マニフェスト生成（サイズ＋SHA-256。形式は VM 側 P1 と同一） |
| `TransferClient.swift` | SSH 経由の登録・配置・受信検証の要求、状態照会 |
| `DeletionGuard.swift` | VM 側判定に基づくローカル音声の削除と、期限超過の未処理の可視化 |
| `SessionStore.swift` | セッション記録（`state` は VM 側12状態の語彙、`localPhase` は Mac 内側の補助状態） |
| `Config.swift` | ローカル設定の読み込み |
| `Resources/Info.plist` | バンドル情報と利用目的の文言 |
| `build.sh` | `.app` の組み立てと署名 |
| `config.example.json` | ローカル設定の雛形（実値はここに書かない） |

## ビルド手順

前提：Xcode（または Swift ツールチェイン）と、VM 側パイプラインが動作していること。

1. **バンドル ID の確認（初回ビルド前に必須）**：バンドル ID は `com.nishikawa.interviewrecorder` で確定済み。`Resources/Info.plist` の `CFBundleIdentifier` と `signing-requirement.txt` の識別子がこの値で一致していることを確認する。TCC の同一性はこの2つで決まるため、後から変更しない。
2. ビルドする：
   ```bash
   cd mac-app
   ./build.sh
   ```
   `InterviewRecorder.app` ができ、識別子ベースの指定要件で署名される（再ビルド後も TCC 許可を維持するため。理由は `docs/P3計画.md`）。
3. エンタイトルメントの確認：`codesign -d --entitlements - InterviewRecorder.app` の出力が空（想定どおり）であることを記録する。

## ローカル設定

1. `config.example.json` を `~/Library/Application Support/InterviewRecorder/config.json` へコピーし、接続先エイリアスと VM 側のパスを実環境の値に書き換える。**実値入りの config.json をリポジトリに置かない。**
2. SSH は既存の鍵認証と `~/.ssh/config` のエイリアスを使う前提。パスワード等は扱わない。
3. `autoTransferEnabled` は **false** にする（転送は VM 側からのプルが正。正本は `docs/状態管理.md`「転送方式（プル方式）」）。false の間、アプリは録音確定時に転送待ちの記録に留まり、自動プッシュのエラー表示・再試行を行わない。キーを置かない場合は有効（プッシュ動作）として扱われるため、運用では必ず明示する。

## 初回起動と権限付与（本人の手動操作）

権限付与はエージェントが代行しない。以下は本人が Mac の画面で行う。

1. `InterviewRecorder.app` を起動する（メニューバーに「面談レコーダー」と出る）。
2. メニューから「録音開始」を押す。マイクの許可ダイアログが出たら許可する。
3. 画面収録の許可が無い場合は案内が出る。システム設定 > プライバシーとセキュリティ > 画面収録 で `InterviewRecorder` を許可する（映像は使わないが、システム音声のキャプチャにこの許可が要る）。
4. 許可後、もう一度「録音開始」を押す。

## 試し方（短時間の通し確認）

1. 録音開始 → 数十秒話す → メニューの「停止」。
2. 転送は VM 側からのプルで行う（アプリからは送らない）。VM 側でプルを実行し、状態を確認する：
   ```bash
   cd pipeline
   INTERVIEW_DATA_DIR=<データ領域> python3 -m interview_pipeline pull
   INTERVIEW_DATA_DIR=<データ領域> python3 -m interview_pipeline status
   ```
   対象セッションが `received` になっていれば、受信検証（サイズ＋SHA-256）まで通っている。
3. 再ビルド後に TCC 許可が維持されることを確認する（`./build.sh` を再実行し、許可ダイアログが再表示されずに録音できること）。
4. 無音検知の確認：録音中に5分間無音にし、自動停止（猶予→確定→転送待ち）へ進むこと。猶予内に声を出せば同一セッションのまま続くこと。

## 注意

- 録音ファイルとセッション記録は `~/Library/Application Support/InterviewRecorder/` に置かれる。**このリポジトリには音声を置かない**（拡張子は `.gitignore` でも除外済み）。
- ファイル名は `system_…`／`mic_…` の接頭辞が契約（VM 側の系統分類）。セッションフォルダに接頭辞の無いファイルを混ぜない（VM 側で分類外エラーとして処理が止まる）。
- 失敗時はアラートで表示される（黙って成功扱いにしない）。転送に失敗したセッションは未処理として残る。`autoTransferEnabled` が有効な構成ではメニューの「未転送分を再送する」または次回起動時に再送され、無効な構成（現行運用）では VM 側のプルが拾う。
