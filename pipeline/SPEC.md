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
- `assert_no_audio_files(paths)` — 1件でもあれば `AudioContentError` を送出（保存中止）
- `vm_copy_deletable(session) -> bool` — 正本 `../docs/状態管理.md`「削除の条件」のとおり、処理完了（`processing` を正常終了している）かつ 下書きの格納確認（`draft_saved` 以降の状態で、`draft_saved_at` が記録されている）の両方を満たすときだけ True
- `execute_vm_deletion(store, session_id, inbox_dir) -> Path` — 実行直前に `vm_copy_deletable` を再確認し、満たさなければ `DeletionGuardError`。満たせば inbox のセッションフォルダを削除し、`vm_copy_deleted_at` を記録する
- `mac_deletion_eligible(session) -> bool` — 正本 `../docs/状態管理.md`「削除の条件」のとおり、確定版が保存済み（`final_saved` 以降）かつ未処理でないこと（`needs_reprocess` でないこと）を満たすときだけ True（期限（最長7日）は削除の許可条件ではない）
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

- `save_draft(store, session_id, draft_path, notes_dir, *, storage_checker) -> Path`
  - 保存前に `assert_no_audio_files` を通す。チェック対象は「この保存で配置する全ファイル」（現行APIではドラフト本体。配置先のファイル名は `<session_id>.md` 固定で、保存処理がドラフト以外を配置することはない）。失敗したら保存せず、`needs_reprocess`（resume: `awaiting_editing`、`error_kind="audio_content_detected"`）にして送出する。第二の防壁（`.gitignore`）については、正本 `../docs/状態管理.md`「保存ガードの拡張子リスト」を参照
  - `storage_checker(destination)` が False なら保存せず `NoteStorageGuardError` を送出する（保存先が公開され得ない格納場所であることの確認。実チェッカーは `guards.check_note_storage`：保存先が git 作業コピー内にあり、`git check-ignore` で ignore 済みと判定され、格納フォルダの追跡ファイルが `README.md` のみであることを確認する。git が使えない等で確認できない場合も False＝保存しない）
  - 成功時は md を `notes_dir/<日付>/<session_id>.md` へ配置し（フォルダ・ファイル名の規則は正本 `../docs/状態管理.md`「セッション識別子」参照。下書き表記の検査は正本「下書き表記」のとおりとし、満たさなければ `DraftMarkingError`）、状態を `draft_saved` → `awaiting_review` へ進める

## CLI（`cli.py`）

`python -m interview_pipeline <command>` の形で、最低限：`register`、`receive`、`run`、`status`、`mark-final`（照合完了→確定版保存済み化）、`delete-vm-copy`、`mac-eligible`。データ領域は環境変数 `INTERVIEW_DATA_DIR`、保存先は `INTERVIEW_NOTES_DIR`（環境変数の定義は、正本 `../docs/状態管理.md`「データ領域と環境変数」を参照）。

## 決定事項（テスト係からの申し送りへの PM 回答。2026-11-07）

1. `closed` は終端状態とし、外向きの遷移を持たない（`closed → needs_reprocess` は不可）。
2. `needs_reprocess` からの復帰は `resume_state` への遷移のみとし、`TRANSITIONS` では `needs_reprocess → {transfer_pending, received, awaiting_editing}` を許可する。実際の再開先はレコードの `resume_state` に従う。
3. `verify_and_promote` の `staging_dir`／`inbox_dir` はセッション単位のディレクトリ。`execute_vm_deletion` の `inbox_dir` はルート（配下のセッションフォルダを削除する）。
4. マニフェストの配置は `staging/<session_id>/manifest.json` とする（手順書にも明記）。
5. `transferring` のセッションは、1回のランで検証→処理まで進めてよい（テストは両対応）。
6. イベントログのキーは `date`／`session_id`／`state`／`error_kind` の4つに固定する。
7. 例外型は専用モジュール `errors.py` に集約する（`InvalidTransitionError`、`AudioContentError`、`DeletionGuardError`、`NoteStorageGuardError`、`DraftMarkingError`、`EngineNotConfiguredError`）。
8. `register` の初期状態は `recorded`。
9. `processing_completed` は `awaiting_editing` への遷移時に立てる。
10. `SessionStore` はインスタンス化時に既存ファイルがあれば自動で読み込む。

## テストの置き場

`pipeline/tests/` に pytest で置く。Mac や外部接続が要る部分はモック／一時ディレクトリで切り分ける。

## P2 追加契約（Google連携。2026-11-08 追記）

P2 は面談検知（カレンダー起点）→検知レジストリ→Docsノート／Sheets面談一覧→リマインド判定を追加する。状態と遷移条件の正本は引き続き `../docs/状態管理.md` とし、**12状態と遷移は変更しない**。検知側の記録は録音セッション（`SessionStore`）とは別の「検知レジストリ」が持ち、セッションIDで紐付ける（`recorded` は「録音済み」を意味するため、未録音の予定を `SessionStore` へ登録しない。セッションIDの採番規則は正本「セッション識別子」に従い、同日の既存セッション（`SessionStore`）と検知レジストリの両方を見て連番を決める）。

### `google_config.py`

- `GoogleConfig` — Google連携の設定。環境変数 `INTERVIEW_GOOGLE_CONFIG` が指す JSON ファイルから読み込む（実値はリポジトリに置かない）。主なキー：
  `cli_path`（コネクタ CLI のパス）、`calendar_ids`（検知対象カレンダー）、`timezone`（日付判定のタイムゾーン）、`primary_keyword`（自己登録予定の主キー文字列）、`secondary_keywords`（自己登録予定の副キー文字列のリスト）、`invited_keywords`（招待予定の判定キーワード）、`sheets_spreadsheet_id`、`sheets_tab`、`docs_template_id`、`docs_folder_id`（任意）、`window_past_days`、`window_future_days`（検知ウィンドウ）
- `load_google_config(path) -> GoogleConfig` — 欠けたキーは既定値（キーワード類・ウィンドウ・タイムゾーン）で補い、必須キー（`sheets_spreadsheet_id` 等）の不足は利用時に明確なエラーにする

### `gateway.py`

- `CalendarEvent` — データクラス：`event_id`、`calendar_id`、`title`、`start`、`end`、`organizer_email`、`organizer_self: bool`、`location`、`description`、`meeting_url`、`cancelled: bool`
- `GoogleGateway` — プロトコル。外部（Google）へのアクセスはすべてここを通す：
  - `list_events(calendar_id, time_min, time_max) -> list[CalendarEvent]`
  - `copy_template(template_id, name, folder_id) -> tuple[str, str]` — テンプレートを複製し、(ドキュメントID, URL) を返す
  - `replace_note_fields(doc_id, fields: dict[str, str])` — ノート内の `{{キー}}` プレースホルダを置換する
  - `read_note_text(doc_id) -> str` — ノート本文の平文を返す（未記入チェック用）
  - `append_sheet_row(spreadsheet_id, tab, values: list[str]) -> int` — 行を追加し、追加された行番号を返す
  - `sheet_row_exists(spreadsheet_id, tab, session_id) -> bool` — セッションIDを含む行が既にあるか（二重追加防止の第二確認）
- `CliGoogleGateway(config)` — `hatch_gws_cli` を subprocess で呼ぶ実装。失敗時は `GoogleGatewayError` を送出する
- テストは `GoogleGateway` のフェイク実装で切り分ける（実接続しない）

### `interviews.py`

- `InterviewRecord` — データクラス：`event_id`、`session_id`、`date`（面談日。`YYYY-MM-DD`）、`start`、`end`、`title`、`company`（企業名の候補。抽出はベストエフォート）、`format`（形式）、`meeting_url`、`docs_doc_id`、`docs_url`、`sheet_row: int | None`、`note_error: str | None`、`sheet_error: str | None`、`reminders: dict[str, str]`（種別→送信日時）、`created_at`、`updated_at`
- `InterviewRegistry(path)` — JSON ファイルへ永続化する。`SessionStore` と同じ流儀（インスタンス化時に自動で読み込む）
  - `upsert(record) -> InterviewRecord` — `event_id` で冪等（既存なら既存を返す）
  - `get_by_event(event_id)` / `get(session_id)` / `all()` / `save()`
  - `assign_session_id(date, store) -> str` — 正本の採番規則（`<日付>_NN`）で、`SessionStore` の既存セッションとレジストリ内の同日レコードの両方と衝突しない最小の連番を返す
- `is_interview_event(event, config) -> str | None` — 検知判定。検知した場合は根拠（`"primary"`／`"secondary"`／`"invited"`）、しなければ `None` を返す：
  - キャンセル済み予定は常に対象外
  - 自己登録予定（`organizer_self`）：タイトルに主キーを含めば `"primary"`、副キーを含めば `"secondary"`
  - 招待予定：タイトルまたは説明文に招待キーワードを含めば `"invited"`
- `extract_company(event) -> str` — タイトル中の括弧内先頭要素などから企業名の候補を抽出する（取れなければ空文字列。推測で埋めない）
- `extract_format(event) -> str` — 場所または会議URLの種別から形式文字列を作る（取れなければ空文字列）

### `sync.py`

- `sync_interviews(registry, gateway, config, *, store, now) -> SyncReport` — 検知ウィンドウ内の全カレンダーを走査し、検知した予定について次を行う（既存レコードは不足分だけを補う冪等動作）：
  1. 新規予定はセッションIDを採番してレジストリへ登録する
  2. Docsノートが無ければテンプレートから作成し、基本情報プレースホルダ（`日時`／`企業`／`形式`／`会議URL`）を置換する。失敗したら `note_error` に種別のみを記録し、成功扱いにしない
  3. Sheets行が無く、かつDocsノートのリンクが得られている場合に、面談一覧へ1行追加する（列：日付・企業・形式・結果・次のアクション・Docsノートへのリンク）。ノート未作成の間は行追加を保留し、失敗扱いにしない。行が既にあるかはレジストリとシート側の両方で確認する
  4. 失敗は `SyncReport` とイベントログに記録する（silent failure 禁止。他件の処理は止めない）
- `SyncReport` — `detected: list[str]`（新規登録したセッションID）、`notes_created: list[str]`、`rows_added: list[str]`、`failed: list[str]`

### `reminders.py`

- リマインド種別は `eve`（前日）／`day_morning`（当日・午前）／`day_evening`（当日・午後＝振り返り記入）の3つ
- `due_reminders(registry, now, slot, *, note_texts) -> list[Reminder]` — 判定はコードで固定する：
  - 面談日が翌日：`eve`（スロット不問。送信済みなら出さない）。本文には準備セクションの未記入項目を含める
  - 面談日が当日かつ `slot == "morning"`：`day_morning`
  - 面談日が当日かつ `slot == "evening"`：`day_evening`
  - いずれでもない場合は空（何も送らない）
- 仕様変更（2026-11-09、本人の判断）：リマインドは全廃（前日 `eve`・当日 `day_morning`・振り返り `day_evening` のすべて。当初は振り返り `day_evening` のみを廃止し、同日中に残り2種別も廃止した）。コードの判定は従来どおり3種別を返すが、定期実行（朝・夕チェック cron）では `remind-check` 自体を実行せず、配信も `remind-mark` もしない。定期実行の役割は検知・同期（`sync`）と異常報告のみとする。コード（`reminders.py`・CLI）自体は変更しない
- `unfilled_sections(note_text) -> list[str]` — テンプレートの準備セクション（企業研究・想定問答・聞きたいこと）のうち、本文が空または「（未記入）」のままのものを返す
- `mark_reminded(registry, event_id, kind, at)` — 送信済みを記録する（`remind-check` では記録せず、送信後に `remind-mark` で記録する二段構え）
- `Reminder` — `event_id`、`session_id`、`kind`、`text`（定型文で生成した通知文）

### CLI 追加（`cli.py`）

- `detect` — 検知ウィンドウを走査し、検知結果（セッションID候補・根拠）を表示するだけ（書き込みなし）
- `sync` — `sync_interviews` を実行し、結果件数を表示する
- `interviews` — 検知レジストリの一覧を表示する
- `remind-check --slot morning|evening [--dry-run]` — 送信対象のリマインドを JSON で表示する（記録はしない）
- `remind-mark <event_id> <kind>` — リマインド送信済みを記録する
- Google連携の設定は環境変数 `INTERVIEW_GOOGLE_CONFIG`、データ領域は既存の `INTERVIEW_DATA_DIR`（レジストリは `state/interviews.json`）
- CLI のゲートウェイ生成は `cli._build_gateway(config)` に集約する（テストでの差し替え点）

### P2 決定事項（テスト係からの申し送りへの PM 回答。2026-11-08）

1. 検知ウィンドウの既定値は、過去1日・未来31日とする。
2. 必須キー不足の扱い：設定ファイル自体の欠落・破損は `GoogleConfigError` で失敗する。`docs_template_id` が無い状態でのノート作成は、例外にせず `note_error="docs_template_not_configured"` として記録し、成功扱いにしない。`sheets_spreadsheet_id` が無い状態でのシート操作は `GoogleGatewayError` とする。
3. `note_error`／`sheet_error` には例外クラス名のみを記録する（例外メッセージ等の内容は記録しない）。
4. `CalendarEvent`／`InterviewRecord` の `start`／`end` はタイムゾーン付き `datetime` とし、レジストリの永続化では ISO 文字列へ変換する。
5. `due_reminders` の `note_texts` のキーは `event_id` を主とし、`session_id` でも引けるようにする。
6. `InterviewRegistry.get_by_event`／`get` は、未登録なら `None` を返す。
7. `assign_session_id` の引数 `date` は `YYYY-MM-DD` 文字列とする。
8. `remind-check` の JSON は、`Reminder` の `event_id`／`session_id`／`kind`／`text` をキーとするオブジェクトのリストとする。
9. ノート作成の失敗は `SyncReport.failed` に入れない（`note_error` とイベントログで可視化する）。`failed` はカレンダー取得の失敗など想定外の失敗用とし、その場合はカレンダーIDを入れる。`sync_interviews` には任意引数 `event_log` を追加し、CLI はデータ領域のイベントログを渡す。
10. `extract_format` は、場所と会議URLの両方がある場合は場所を優先する。
11. `sheet_row` が `-1` の場合は「シート側に既存の行がある（行番号は不明）」を表す番兵値とする（レジストリを失った場合の二重追加防止）。

### P2 実装後の記録（QA申し送り・申し送り事項。2026-11-08）

- 採番規則の差異（QA必須対応）: 正本の状態管理にある `SessionStore._next_id` は、重複時に「-2」サフィックスを付ける規則（例: `2026-09-01_01-2`）を持つ。一方 `InterviewRegistry.assign_session_id` は `_NN` 連番のみを採番し、`SessionStore` 側の既存IDとは衝突しない番号を選ぶ（「-2」形式は発行しない）。検知レジストリとセッション台帳でID形式が完全には一致しない点を、既知の差異として記録する。P3 がレジストリのセッションIDを採用する際はこの差異を前提とする。
- `remind-check`（CLI）は `note_texts` を `event_id` キーで組み立てる（決定事項5の主キー側）。`due_reminders` は `session_id` キーでも引ける。
- Sheets への行追加は現在 `gateway.append_sheet_row`（values append）が担うが、書式担保のための共通 sheet-writer tool が別途開発中であり、完成後は行追加経路を同 tool 経由に差し替える（第1波の載せ替え対象。P2 の完了・push 後に別担当が実施）。

### Docsノート テンプレートの契約

- テンプレートの本文には、基本情報のプレースホルダ（`{{日時}}`／`{{企業}}`／`{{形式}}`／`{{会議URL}}`）と、準備（企業研究・想定問答・聞きたいこと）／振り返り（聞かれたこと・答え・次の一手）の見出しを置く。各セクションの初期本文は「（未記入）」とする（未記入チェックの機械判定に使う）
- ノートのタイトルは `<session_id> 面談ノート` とする（企業名・個人名はタイトルに入れない。正本・運用方針どおり企業名はノート本文内にのみ書く）

## V1 追加契約（STTエンジン接続。2026-11-08 追記）

V1 の実測（同一の正解テキスト付き日本語音声・同一条件：faster-whisper／CPU 4スレッド／int8／language=ja／beam_size=5／vad_filter=False）に基づく接続契約。実測値の詳細は `docs/開発ノート.md`「V1 実測結果」を参照。

### `transcribe.py` 追加

- `FasterWhisperTranscriber` — faster-whisper を実エンジンとする `Transcriber` 実装
  - 引数：`model_name`（既定は本人確定モデル `large-v3-turbo`）、`compute_type="int8"`、`cpu_threads=4`、`language="ja"`、`beam_size=5`、`vad_filter=False`（いずれも実測と同一条件を既定とする）
  - `faster_whisper` の import とモデル読み込みは遅延する（生成時には読み込まない。初回 `transcribe` 時に読み込み、以降は再利用する）。これにより、エンジン未導入の環境でもパッケージの import と他機能は使える
  - テストでの差し替え点として、モデル生成関数を差し替え可能にする（実エンジンを要しない単体テストのため）
  - `faster_whisper` が import できない環境では、`EngineNotConfiguredError`（`stt_not_configured`）を送出する（正本 `../docs/状態管理.md`「STTエンジン未接続時の扱い」に従い、未設定として扱う）
  - 返却する `Segment` の `start`／`end`／`text` はエンジンのセグメント値をそのまま使う
- モデルは 2026-11-08 に本人が `large-v3-turbo` で確定した（V1 実測：CER 合計0.50%／RTF 0.703／ピークRSS 1,623MB）。既定モデル名はこの確定モデルを指す。なお、合格ライン（J-9）の確定値は台本リハーサルの実測後に本人が別途最終確定するものであり、本契約の暫定ラインはそのまま維持する

### CLI 追加（`cli.py`）

- エンジン生成は `cli._build_transcriber()` に集約する（テストでの差し替え点。`_build_gateway` と同じ流儀）
- 環境変数 `INTERVIEW_STT_MODEL` が設定されていれば、そのモデル名で `FasterWhisperTranscriber` を返す。未設定・空の場合は従来どおり `UnavailableTranscriber` を返す（未設定時の扱いは正本どおり `stt_not_configured`）
- `run` は `_build_transcriber()` の戻り値を使う

## PULL 追加契約（転送のプル方式。2026-11-08 追記）

本人の設計変更決定（2026-11-08）により、転送は Mac→VM プッシュから **VM 側プル** へ変更する。経緯と方式の正本は `../docs/状態管理.md`「転送方式（プル方式）」。12状態・遷移・マニフェスト・受信検証・削除ガードの契約は変更しない。変えるのは「誰がファイルを運ぶか」だけである。

### `states.py` 追加

- `ERROR_KIND_TRANSFER_FETCH_FAILED = "transfer_fetch_failed"` — プル取得の失敗（接続不可・ファイル欠落・マニフェスト取得失敗）の種別。検証不一致を表す `transfer_verification_failed` とは分ける

### `errors.py` 追加

- `MacFetchError` — Mac からの取得失敗を表す例外（例外型の集約先は決定事項 7 のとおり `errors.py`）

### `pull.py`

- `MacSessionRecord` — データクラス：`session_id: str`、`state: str`（Mac 側セッション記録の状態語彙）、`created_at: datetime | None`
- `MacSource` — プロトコル。Mac へのアクセスはすべてここを通す（`gateway.py` の `GoogleGateway` と同じ流儀。テストはフェイク実装で切り分ける）：
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
