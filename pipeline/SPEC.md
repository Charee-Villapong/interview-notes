# パイプライン実装仕様（インターフェース契約）

本書は、パイプラインの公開インターフェース契約である。実装は本書の公開インターフェースを満たすこと。状態と遷移条件の正本は `../docs/状態管理.md`。

## パッケージ

- パッケージ名：`interview_pipeline`（`pipeline/src/` レイアウト）
- Python 3.12、標準ライブラリのみに依存する（外部パッケージを新規に導入しない）
- 整形：Black＋isort（profile=black）。設定は `pyproject.toml` に置く

## 公開インターフェース

### `states.py`

- `State` — 状態の列挙（`docs/状態管理.md` の状態一覧のとおりの名前を使う）
- `TRANSITIONS: dict[State, set[State]]` — 許可された遷移
- `can_transition(src, dst) -> bool`
- `assert_transition(src, dst)` — 不正な遷移なら `InvalidTransitionError` を送出

### `store.py`

- `Session` — データクラス。少なくとも次のフィールドを持つ：
  `session_id: str`、`state: State`、`created_at`（録音日時）、`resume_state: State | None`、`error_kind: str | None`、`draft_saved_at`、`final_saved_at`、`vm_copy_deleted_at`、`mac_copy_deleted_at`、`processing_completed: bool`
- `SessionStore(path)` — JSON ファイルへ永続化する
  - `register(session_id, created_at) -> Session` — 冪等（既存なら既存を返す。正本 `../docs/状態管理.md`「二重処理・二重保存の防止」参照）
  - `get(session_id) -> Session`
  - `all() -> list[Session]`
  - `transition(session_id, dst, *, error_kind=None, resume_state=None)` — 遷移検証つきで状態を更新し、保存する。`draft_saved` へ進むとき `draft_saved_at` を、`final_saved` へ進むとき `final_saved_at` を自動で記録する
  - `save()` / `load()` — 中断・再起動を挟んでも状態から再開できる

### `manifest.py`

- `FileEntry(name: str, size: int, sha256: str)`、`Manifest(session_id: str, files: list[FileEntry])`
- `Manifest.from_dict(d)` / `Manifest.load(path)`
- 系統の区別：ファイル名の接頭辞 `system`／`mic` による。系統と話者の対応は、正本 `../docs/状態管理.md`「話者マッピング」を参照

### `receive.py`

- `verify_and_promote(store, manifest, staging_dir, inbox_dir) -> ReceiveResult`
  - 全ファイルのサイズ・SHA-256 が一致した場合のみ、ファイルを inbox へ移動し、状態を `received` に進め、`ReceiveResult(ok=True)` を返す
  - 1件でも不一致・欠落があれば、ファイルは inbox へ移さず、状態を `needs_reprocess`（resume: `transfer_pending`、`error_kind="transfer_verification_failed"`）にして `ReceiveResult(ok=False, reason=...)` を返す
  - すでに `received` 以降のセッションに対しては冪等に `ok=True`（二重処理しない）
  - 受信検証を通過していないファイルは、処理対象に絶対に含めない（正本 `../docs/状態管理.md`「二重処理・二重保存の防止」）

### `guards.py`

- `AUDIO_VIDEO_EXTENSIONS: frozenset[str]`（値の正本は `../docs/状態管理.md`「保存ガードの拡張子リスト」）
- `find_audio_files(paths) -> list[Path]` — 音声・動画拡張子（大文字小文字を区別しない）に一致するものを返す
- `assert_no_audio_files(paths)` — 1件でもあれば `AudioContentError` を送出（登録中止）
- `check_note_destination(notes_repo, *, github) -> bool` — push 先が非公開リポジトリであることの確認。`github.repo_info` で可視性を取得し、`private` のときだけ True。形式不正・取得できない場合も False（fail-closed）
- `vm_copy_deletable(session) -> bool` — 正本 `../docs/状態管理.md`「削除の条件」のとおり、処理完了（`processing` を正常終了している）かつ 要約の生成確認（`draft_saved` 以降の状態で、`draft_saved_at` が記録されている）の両方を満たすときだけ True
- `execute_vm_deletion(store, session_id, inbox_dir) -> Path` — 実行直前に `vm_copy_deletable` を再確認し、満たさなければ `DeletionGuardError`。満たせば inbox のセッションフォルダを削除し、`vm_copy_deleted_at` を記録する。記録後に状態が `deletable` で Mac 側コピーの削除完了も記録済みなら、`closed` へ進める
- `record_mac_deletion(store, session_id) -> Session` — Mac 側コピー削除完了の申告を記録する。実行直前に `mac_deletion_eligible` を再確認し、満たさなければ `DeletionGuardError`（状態・記録は変えない）。満たせば `mac_copy_deleted_at` を記録し、`final_saved` から `deletable` へ進める。VM 側コピーの削除完了も記録済みなら `closed` まで進める。既に記録済みの場合は冪等に成功し、初回の記録時刻を保持したまま状態の前進だけを再評価する
- `mac_deletion_eligible(session) -> bool` — 正本 `../docs/状態管理.md`「削除の条件」のとおり、要約が非公開リポジトリへ push 済み（`final_saved` 以降の状態で、`note_published_at` が記録されている）かつ未処理でないこと（`needs_reprocess` でないこと）を満たすときだけ True（期限（最長7日）は削除の許可条件ではない）
- `find_overdue_unprocessed(store, now, days=7) -> list[Session]` — 正本 `../docs/状態管理.md`「削除の条件」のとおり、録音から `days` を超え、`final_saved` 未到達のセッション（削除用ではなく可視化・再処理用）

### `transcribe.py`

- `Segment(start: float, end: float, text: str)`
- `Transcriber` — プロトコル。`transcribe(audio_path) -> list[Segment]`
- `UnavailableTranscriber` — 常に `EngineNotConfiguredError` を送出する（STT未接続の現状を表す。未接続時の扱いは正本 `../docs/状態管理.md`「STTエンジン未接続時の扱い」を参照）
- `merge_tracks(system_segments, mic_segments) -> list[tuple[str, Segment]]` — 時系列に合成し、正本 `../docs/状態管理.md`「話者マッピング」に定める話者ラベルを付ける

### `runner.py`

- `EventLog(path)` — `record(session_id, state, error_kind=None)` で JSONL に1行追記する。記録項目は日付・セッションID・状態・エラー種別のみ（内容・企業名・個人名を含めない設計。正本 `../docs/状態管理.md`「通知・ログ」参照）
- `run_once(store, *, staging_root, inbox_root, work_root, transcriber, event_log) -> RunReport`
  - 全件走査し、`transferring`（検証待ち：マニフェストがあれば検証）、`received`（処理）、`needs_reprocess`（resume 先に応じて再開）を処理する
  - 処理の順序は、正本 `../docs/状態管理.md`「処理順序」のとおり（作業用テキストは `work/<session_id>/transcript.md` に生成する）
  - セッションの inbox 内に、接頭辞 `system`／`mic` のどちらでも始まらないファイル（分類外ファイル）が1件でも存在する場合は、文字起こしを一切行わずエラーで停止し、セッションを `needs_reprocess`（resume: `received`、error_kind=`"unclassified_track_file"`）にしてイベントを記録する。分類外ファイルを話者として合成することはしない
  - 失敗時はセッションを `needs_reprocess` にし、イベントを記録する。他件の処理は止めない
  - 再試行の扱いは正本 `../docs/状態管理.md` を参照（回数上限なし・`needs_reprocess` は毎ランの対象）
- `RunReport` — `processed: list[str]`、`failed: list[str]`、`skipped: list[str]`

### `saving.py`

- `save_note(store, session_id, note_path) -> Path`
  - 登録前に `assert_no_audio_files` を通す（チェック対象は要約ファイル本体）。失敗したら登録せず、`needs_reprocess`（resume: `awaiting_editing`、`error_kind="audio_content_detected"`）にして送出する。対象拡張子は正本 `../docs/状態管理.md`「保存ガードの拡張子リスト」を参照
  - 要約ファイルが無い・空の場合は `NoteContentError` を送出し、状態は変えない
  - 成功時はファイルをコピーせず、そのままの場所を「push 待ち」として登録し、状態を `draft_saved` へ進める（既に `draft_saved` なら冪等に成功する）

### `publish.py`

- `publish_note(store, session_id, note_path, *, notes_repo, github) -> str` — 要約を非公開リポジトリへ push し、ノートの URL を返す
  - 対象は `draft_saved` のセッションのみ。それ以外は `NotePublishError`
  - push 前に `guards.check_note_destination` で宛先が非公開であることを確認する。確認できなければ push しない
  - 配置パスは `<日付>/<session_id>.md`（規則は正本 `../docs/状態管理.md`「セッション識別子」参照）。`GhGitHubClient` が `gh api` の contents API で作成／更新する
  - push 後、リモート上の blob sha がローカル内容の git blob sha と一致することを確認する。一致しなければ `NotePublishError`（要約はローカルに保持し、状態は `draft_saved` のまま）
  - 確認できたら `note_published_at` を記録し、状態を `final_saved` へ進め、ローカルの要約コピーを削除する（要約は VM ローカルに恒常保存しない。正本 `../docs/状態管理.md`「要約の保存先」参照）

## CLI（`cli.py`）

`python -m interview_pipeline <command>` の形で、最低限：`register`、`receive`、`pull`、`run`、`status`、`save-note`（要約の登録。対象は `work/<session_id>/note.md`）、`publish-note`（非公開リポジトリへの push。失敗時はイベントに `note_publish_failed` を記録し、終了コード 1）、`delete-vm-copy`、`mac-eligible`、`report-mac-deleted`（Mac 側コピー削除完了の申告。成功時は `<session_id> <state>` を出力して終了コード 0、ガード未達なら `DeletionGuardError` が伝播する）。データ領域は環境変数 `INTERVIEW_DATA_DIR`、push 先は `INTERVIEW_NOTES_REPO`（環境変数の定義は、正本 `../docs/状態管理.md`「データ領域と環境変数」を参照）。

## 決定事項

1. `closed` は終端状態とし、外向きの遷移を持たない（`closed → needs_reprocess` は不可）。
2. `needs_reprocess` からの復帰は `resume_state` への遷移のみとし、`TRANSITIONS` では `needs_reprocess → {transfer_pending, received, awaiting_editing}` を許可する。実際の再開先はレコードの `resume_state` に従う。
3. `verify_and_promote` の `staging_dir`／`inbox_dir` はセッション単位のディレクトリ。`execute_vm_deletion` の `inbox_dir` はルート（配下のセッションフォルダを削除する）。
4. マニフェストの配置は `staging/<session_id>/manifest.json` とする（手順書にも明記）。
5. `transferring` のセッションは、1回のランで検証→処理まで進めてよい（テストは両対応）。
6. イベントログのキーは `date`／`session_id`／`state`／`error_kind` の4つに固定する。
7. 例外型は専用モジュール `errors.py` に集約する（`InvalidTransitionError`、`AudioContentError`、`DeletionGuardError`、`NoteContentError`、`NotePublishError`、`EngineNotConfiguredError`）。
8. `register` の初期状態は `recorded`。
9. `processing_completed` は `awaiting_editing` への遷移時に立てる。
10. `SessionStore` はインスタンス化時に既存ファイルがあれば自動で読み込む。
11. 要約は本人の確認（照合）を経ず、生成後に非公開リポジトリへ push して完了とする。状態は11状態とする。

## テストの置き場

`pipeline/tests/` に pytest で置く。Mac や外部接続が要る部分はモック／一時ディレクトリで切り分ける。

## V1 追加契約（STTエンジン接続。2026-11-08 追記）

STT エンジン接続の追加契約。

### `transcribe.py` 追加

- `FasterWhisperTranscriber` — faster-whisper を実エンジンとする `Transcriber` 実装
  - 引数：`model_name`（既定は `large-v3-turbo`）、`compute_type="int8"`、`cpu_threads=4`、`language="ja"`、`beam_size=5`、`vad_filter=False`
  - `faster_whisper` の import とモデル読み込みは遅延する（生成時には読み込まない。初回 `transcribe` 時に読み込み、以降は再利用する）。これにより、エンジン未導入の環境でもパッケージの import と他機能は使える
  - テストでの差し替え点として、モデル生成関数を差し替え可能にする（実エンジンを要しない単体テストのため）
  - `faster_whisper` が import できない環境では、`EngineNotConfiguredError`（`stt_not_configured`）を送出する（正本 `../docs/状態管理.md`「STTエンジン未接続時の扱い」に従い、未設定として扱う）
  - 返却する `Segment` の `start`／`end`／`text` はエンジンのセグメント値をそのまま使う

### CLI 追加（`cli.py`）

- エンジン生成は `cli._build_transcriber()` に集約する（テストでの差し替え点。`_build_gateway` と同じ流儀）
- 環境変数 `INTERVIEW_STT_MODEL` が設定されていれば、そのモデル名で `FasterWhisperTranscriber` を返す。未設定・空の場合は従来どおり `UnavailableTranscriber` を返す（未設定時の扱いは正本どおり `stt_not_configured`）
- `run` は `_build_transcriber()` の戻り値を使う

## PULL 追加契約（転送のプル方式。2026-11-08 追記）

転送は **VM 側プル** で行う。方式の正本は `../docs/状態管理.md`「転送方式（プル方式）」。本節はプルの追加契約であり、状態・遷移・マニフェスト・受信検証・削除ガードの契約は変更しない。

### `states.py` 追加

- `ERROR_KIND_TRANSFER_FETCH_FAILED = "transfer_fetch_failed"` — プル取得の失敗（接続不可・ファイル欠落・マニフェスト取得失敗）の種別。検証不一致を表す `transfer_verification_failed` とは分ける

### `errors.py` 追加

- `MacFetchError` — Mac からの取得失敗を表す例外（例外型の集約先は決定事項 7 のとおり `errors.py`）

### `pull.py`

- `MacSessionRecord` — データクラス：`session_id: str`、`state: str`（Mac 側セッション記録の状態語彙）、`created_at: datetime | None`
- `MacSource` — プロトコル。Mac へのアクセスはすべてここを通す（テストはフェイク実装で切り分ける）：
  - `list_sessions() -> list[MacSessionRecord]` — Mac 側セッション記録の一覧を返す。記録自体を取得できない場合は `MacFetchError` を送出する
  - `fetch_file(session_id, name) -> bytes` — Mac 側の録音置き場から `<session_id>/<name>` の内容を取得する。失敗は `MacFetchError`
- `SshMacSource(ssh_command, remote_app_dir)` — 既存の VM→Mac SSH 経路で取得する実装。`ssh_command` は SSH 実行コマンド（ラッパースクリプト等）へのパス、`remote_app_dir` は Mac 側のアプリデータ領域（`sessions.json` と `recordings/` の親ディレクトリ）。リモートの読み取りは標準出力経由で行い、接続先・パスの実値は引数（設定）でのみ与える
- `PullReport` — `pulled: list[str]`、`failed: list[str]`、`skipped: list[str]`
- `pull_pending(store, source, *, staging_root, inbox_root, event_log) -> PullReport` — Mac 側の一覧から転送対象を拾い、ステージング取得→受信検証までを行う：
  - 対象は、Mac 側の状態が `recorded`／`transfer_pending`／`needs_reprocess` のセッション（録音確定済みで転送待ちのもの）。それ以外は対象にせず、レポートにも数えない
  - セッションIDが正本の形式（`YYYY-MM-DD_NN`）に合わないレコードは、登録も取得もせず、イベント（error_kind=`transfer_fetch_failed`）を記録して `failed` に入れる（ステージングや録音置き場の外を触らないための担保。MacSource の実装に依らず `pull_pending` 側で検証する）
  - VM 側で `received` 以降のセッションは再取得せず `skipped` に入れる（冪等。照合済みは再検証しない）
  - VM 側に未登録のセッションは、Mac 側の `created_at` で `register` してから進める。`created_at` が無い・解釈できない場合は、セッションIDの日付部分（UTC 0時）で登録する
  - 取得はマニフェスト（`manifest.json`）→マニフェストに載るファイルの順に行い、ステージング `<staging_root>/<session_id>/` へ配置する。再プル時はステージングを置き換える（正本「二重処理・二重保存の防止」）
  - 取得失敗は、当該セッションを `needs_reprocess`（resume: `transfer_pending`、error_kind=`transfer_fetch_failed`）にしてイベントを記録し、`failed` に入れる。他件の処理は止めない
  - 取得できたら、状態を正規の遷移経路で `transferring` まで進めてから、既存の `verify_and_promote` で検証・昇格する。成功は `pulled` に入れ、検証失敗は `failed` に入れる（検証失敗時の状態・種別は `verify_and_promote` の規則どおりとし、イベントも記録する）
  - `event_log` は `runner.EventLog` と同じ記録方式（日付・セッションID・状態・エラー種別のみ）

### CLI 追加（`cli.py`）

- `pull` — `pull_pending` を実行し、レポートのリストを `pulled=[...] failed=[...] skipped=[...]` の形式で表示する（`run` の表示形式と同一）。失敗があれば終了コード 1 とする
- セッション記録自体を取得できない全体失敗時は、リスト形式ではなく `failed: mac_fetch_failed` と表示して終了コード 1 とする（全体失敗を個別失敗の数え方と混同させないため）
- 接続情報は環境変数 `INTERVIEW_MAC_SSH_COMMAND`（SSH 実行コマンドへのパス）と `INTERVIEW_MAC_APP_DIR`（Mac 側アプリデータ領域のパス）で与える。いずれか未設定なら明確なエラーで終了する（黙って成功扱いにしない）
- ソース生成は `cli._build_mac_source()` に集約する（テストでの差し替え点。`_build_gateway` ／ `_build_transcriber` と同じ流儀）
