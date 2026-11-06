# パイプライン実装仕様（インターフェース契約）

本書は、テスト係と実装係の共通の契約である。テストは本書と企画書の完了の定義だけから書き、実装は本書の公開インターフェースを満たすこと。状態と遷移条件の正本は `../docs/状態管理.md`。

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
  - `register(session_id, created_at) -> Session` — 冪等（既存なら既存を返す）
  - `get(session_id) -> Session`
  - `all() -> list[Session]`
  - `transition(session_id, dst, *, error_kind=None, resume_state=None)` — 遷移検証つきで状態を更新し、保存する。`draft_saved` へ進むとき `draft_saved_at` を、`final_saved` へ進むとき `final_saved_at` を自動で記録する
  - `save()` / `load()` — 中断・再起動を挟んでも状態から再開できる

### `manifest.py`

- `FileEntry(name: str, size: int, sha256: str)`、`Manifest(session_id: str, files: list[FileEntry])`
- `Manifest.from_dict(d)` / `Manifest.load(path)`
- 系統の区別：ファイル名の接頭辞 `system`（相手）／`mic`（自分）

### `receive.py`

- `verify_and_promote(store, manifest, staging_dir, inbox_dir) -> ReceiveResult`
  - 全ファイルのサイズ・SHA-256 が一致した場合のみ、ファイルを inbox へ移動し、状態を `received` に進め、`ReceiveResult(ok=True)` を返す
  - 1件でも不一致・欠落があれば、ファイルは inbox へ移さず、状態を `needs_reprocess`（resume: `transfer_pending`、`error_kind="transfer_verification_failed"`）にして `ReceiveResult(ok=False, reason=...)` を返す
  - すでに `received` 以降のセッションに対しては冪等に `ok=True`（二重処理しない）

### `guards.py`

- `AUDIO_VIDEO_EXTENSIONS: frozenset[str]`
- `find_audio_files(paths) -> list[Path]` — 音声・動画拡張子（大文字小文字を区別しない）に一致するものを返す
- `assert_no_audio_files(paths)` — 1件でもあれば `AudioContentError` を送出（保存中止）
- `vm_copy_deletable(session) -> bool` — 処理完了かつ `draft_saved_at` 記録済みのときだけ True
- `execute_vm_deletion(store, session_id, inbox_dir) -> Path` — 実行直前に `vm_copy_deletable` を再確認し、満たさなければ `DeletionGuardError`。満たせば inbox のセッションフォルダを削除し、`vm_copy_deleted_at` を記録する
- `mac_deletion_eligible(session) -> bool` — `final_saved_at` 記録済み、かつ `needs_reprocess` でないときだけ True（経過日数は許可条件にしない）
- `find_overdue_unprocessed(store, now, days=7) -> list[Session]` — 録音から `days` を超え、`final_saved` 未到達のセッション（削除用ではなく可視化・再処理用）

### `transcribe.py`

- `Segment(start: float, end: float, text: str)`
- `Transcriber` — プロトコル。`transcribe(audio_path) -> list[Segment]`
- `UnavailableTranscriber` — 常に `EngineNotConfiguredError` を送出する（STT未接続の現状を表す）
- `merge_tracks(system_segments, mic_segments) -> list[tuple[str, Segment]]` — 時系列に合成し、話者ラベル（システム系統＝`"B"`、マイク系統＝`"A"`）を付ける

### `runner.py`

- `EventLog(path)` — `record(session_id, state, error_kind=None)` で JSONL に1行追記する。記録項目は日付・セッションID・状態・エラー種別のみ（内容・企業名・個人名を含めない設計）
- `run_once(store, *, staging_root, inbox_root, work_root, transcriber, event_log) -> RunReport`
  - 全件走査し、`transferring`（検証待ち：マニフェストがあれば検証）、`received`（処理）、`needs_reprocess`（resume 先に応じて再開）を処理する
  - 処理の順序：受信検証 → 文字起こし → 話者合成 → 作業用テキスト（`work/<session_id>/transcript.md`）生成 → `awaiting_editing`
  - 失敗時はセッションを `needs_reprocess` にし、イベントを記録する。他件の処理は止めない
  - 再試行に回数上限は設けない（`needs_reprocess` は毎ランの対象）
- `RunReport` — `processed: list[str]`、`failed: list[str]`、`skipped: list[str]`

### `saving.py`

- `save_draft(store, session_id, draft_path, notes_dir, *, privacy_checker) -> Path`
  - 保存前に `assert_no_audio_files`（対象はドラフトと notes_dir へ置く全ファイル）を通す。失敗したら保存せず、`needs_reprocess`（resume: `awaiting_editing`、`error_kind="audio_content_detected"`）にして送出する
  - `privacy_checker()` が False なら保存せず `RepositoryNotPrivateError` を送出する（保存先が非公開であることの確認）
  - 成功時は md を `notes_dir/<日付>/<session_id>.md` へ配置し（下書き表記の検査：冒頭に「下書き（未照合）」を含むこと。含まなければ `DraftMarkingError`）、状態を `draft_saved` → `awaiting_review` へ進める

## CLI（`cli.py`）

`python -m interview_pipeline <command>` の形で、最低限：`register`、`receive`、`run`、`status`、`mark-final`（照合完了→確定版保存済み化）、`delete-vm-copy`、`mac-eligible`。データ領域は環境変数 `INTERVIEW_DATA_DIR`、保存先は `INTERVIEW_NOTES_DIR`。

## テストの置き場

`pipeline/tests/` に pytest で置く。Mac や外部接続が要る部分はモック／一時ディレクトリで切り分ける。
