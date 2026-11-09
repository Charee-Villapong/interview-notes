"""転送プル（VM 側が Mac から取得する）。

方式の正本は docs/状態管理.md「転送方式（プル方式）」、契約は
pipeline/SPEC.md「PULL 追加契約」。Mac へのアクセスは ``MacSource``
プロトコルを通し、実接続は ``SshMacSource`` が担う（テストは
フェイク実装で切り分ける）。
変えるのは「誰がファイルを運ぶか」だけで、受信検証は既存の
``verify_and_promote`` をそのまま使う。
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Protocol

from interview_pipeline.errors import MacFetchError
from interview_pipeline.layout import MANIFEST_FILENAME
from interview_pipeline.manifest import Manifest
from interview_pipeline.receive import verify_and_promote
from interview_pipeline.states import (
    ERROR_KIND_TRANSFER_FETCH_FAILED,
    ERROR_KIND_TRANSFER_VERIFICATION_FAILED,
    State,
)

__all__ = [
    "MacFetchError",
    "MacSessionRecord",
    "MacSource",
    "PullReport",
    "SshMacSource",
    "pull_pending",
]

# プル対象とする Mac 側セッション記録の状態（録音確定済みで転送待ち）。
_PULLABLE_MAC_STATES = frozenset({"recorded", "transfer_pending", "needs_reprocess"})

# セッションIDの形式（正本「セッション識別子」：面談日付ベースの
# ``YYYY-MM-DD_NN``。VM 側採番の ``-2`` 形式のサフィックスも許容する）。
_SESSION_ID_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}(-\d+)?$")

# received 以降とみなす状態（再取得しない。receive.py の冪等判定と同義）。
_RECEIVED_OR_LATER = frozenset(
    {
        State.RECEIVED,
        State.PROCESSING,
        State.AWAITING_EDITING,
        State.DRAFT_SAVED,
        State.FINAL_SAVED,
        State.DELETABLE,
        State.CLOSED,
    }
)


@dataclass
class MacSessionRecord:
    """Mac 側セッション記録の1件（Mac 側 JSON の読み取り結果）。"""

    session_id: str
    state: str
    created_at: datetime | None = None


class MacSource(Protocol):
    """Mac へのアクセスの集約点。実装は SshMacSource／テスト用フェイク。"""

    def list_sessions(self) -> list[MacSessionRecord]:
        """Mac 側セッション記録の一覧を返す（取得不可なら MacFetchError）。"""
        ...

    def fetch_file(self, session_id: str, name: str) -> bytes:
        """録音置き場の ``<session_id>/<name>`` の内容を返す。"""
        ...


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class SshMacSource:
    """既存の VM→Mac SSH 経路で Mac 側ファイルを読む実装。

    ``ssh_command`` は SSH 実行コマンド（ラッパースクリプト等）への
    パス、``remote_app_dir`` は Mac 側のアプリデータ領域
    （``sessions.json`` と ``recordings/`` の親）。リモートの読み取りは
    ``cat`` の標準出力経由で行う。接続先・パスの実値はここには
    書かず、引数（設定）でのみ与える。
    """

    # 取得のタイムアウト（秒）。調整値であり、検証結果には影響しない。
    _LIST_TIMEOUT_SECONDS = 60
    _FETCH_TIMEOUT_SECONDS = 900

    def __init__(self, ssh_command: str, remote_app_dir: str) -> None:
        self._ssh_command = ssh_command
        self._remote_app_dir = remote_app_dir.rstrip("/")

    def _run_remote(self, remote_command: str, *, timeout: int) -> bytes:
        try:
            completed = subprocess.run(
                [self._ssh_command, remote_command],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MacFetchError(
                f"ssh command failed: {exc.__class__.__name__}"
            ) from exc
        if completed.returncode != 0:
            raise MacFetchError(f"remote command failed: exit {completed.returncode}")
        return completed.stdout

    def _remote_path(self, *parts: str) -> str:
        return "/".join([self._remote_app_dir, *parts])

    def list_sessions(self) -> list[MacSessionRecord]:
        raw = self._run_remote(
            f"cat {shlex.quote(self._remote_path('sessions.json'))}",
            timeout=self._LIST_TIMEOUT_SECONDS,
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MacFetchError("mac sessions record is not valid JSON") from exc
        if not isinstance(payload, list):
            raise MacFetchError("mac sessions record must be a list")
        records: list[MacSessionRecord] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            session_id = item.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                continue
            state = item.get("state")
            records.append(
                MacSessionRecord(
                    session_id=session_id,
                    state=state if isinstance(state, str) else "",
                    created_at=_parse_datetime(item.get("createdAt")),
                )
            )
        return records

    def fetch_file(self, session_id: str, name: str) -> bytes:
        # 録音置き場の外を指す名前は取得対象にしない。
        if "/" in session_id or "/" in name or name in ("", ".", ".."):
            raise MacFetchError(f"invalid remote file: {session_id}/{name}")
        path = self._remote_path("recordings", session_id, name)
        return self._run_remote(
            f"cat {shlex.quote(path)}",
            timeout=self._FETCH_TIMEOUT_SECONDS,
        )


@dataclass
class PullReport:
    pulled: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _state_of(session) -> State:
    # receive.py の読み取りと同じ規則（``.value`` を持つ状態値も吸収する）。
    state = session.state
    if isinstance(state, State):
        return state
    return State(str(getattr(state, "value", state)))


def _session_or_none(store, session_id: str):
    try:
        return store.get(session_id)
    except KeyError:
        return None


def _created_at_for(record: MacSessionRecord) -> datetime:
    """登録用の録音日時。Mac 側の値が無ければセッションIDの日付で補う。"""
    if record.created_at is not None:
        return record.created_at
    date_part = record.session_id.split("_", 1)[0]
    try:
        day = datetime.strptime(date_part, "%Y-%m-%d").date()
    except ValueError:
        return datetime.now(timezone.utc)
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _mark_fetch_failed(store, session_id: str, event_log) -> None:
    # 既に needs_reprocess の場合は、store.transition 側が状態を変えずに
    # 失敗情報だけを更新する（receive / runner と同じ規則）。
    store.transition(
        session_id,
        State.NEEDS_REPROCESS,
        error_kind=ERROR_KIND_TRANSFER_FETCH_FAILED,
        resume_state=State.TRANSFER_PENDING,
    )
    event_log.record(
        session_id,
        State.NEEDS_REPROCESS,
        error_kind=ERROR_KIND_TRANSFER_FETCH_FAILED,
    )


def _advance_to_transferring(store, session_id: str) -> None:
    """正規の遷移経路で ``transferring`` まで進める。"""
    current = _state_of(store.get(session_id))
    if current is State.TRANSFERRING:
        return
    if current is State.NEEDS_REPROCESS:
        # 復帰は resume_state（transfer_pending）経由の経路を辿る。
        store.transition(session_id, State.TRANSFER_PENDING)
        current = State.TRANSFER_PENDING
    if current is State.RECORDED:
        store.transition(session_id, State.TRANSFER_PENDING)
        current = State.TRANSFER_PENDING
    if current is State.TRANSFER_PENDING:
        store.transition(session_id, State.TRANSFERRING)


def _pull_one(
    store,
    source: MacSource,
    record: MacSessionRecord,
    *,
    staging_root: Path,
    inbox_root: Path,
    event_log,
    report: PullReport,
) -> None:
    session_id = record.session_id
    session = _session_or_none(store, session_id)
    if session is not None and _state_of(session) in _RECEIVED_OR_LATER:
        # 取得済み・照合済みは再取得しない（冪等）。
        report.skipped.append(session_id)
        return
    if session is None:
        store.register(session_id, _created_at_for(record))

    staging_dir = staging_root / session_id
    # 取得はマニフェスト→掲載ファイルの順に行う。失敗はこの件だけを
    # 未処理として残し、他件の処理は止めない（失敗の隔離）。
    try:
        manifest_bytes = source.fetch_file(session_id, MANIFEST_FILENAME)
        manifest = Manifest.from_dict(json.loads(manifest_bytes.decode("utf-8")))
        if manifest.session_id != session_id:
            raise MacFetchError("manifest session mismatch")
        for entry in manifest.files:
            if Path(entry.name).name != entry.name:
                raise MacFetchError(f"invalid file name: {entry.name}")
        # 再プル時はステージングを置き換える（正本「遷移条件」）。
        # 前回の不完全なコピーの残骸が残らないよう、マニフェストが
        # 取れてからディレクトリごと入れ替える。
        shutil.rmtree(staging_dir, ignore_errors=True)
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / MANIFEST_FILENAME).write_bytes(manifest_bytes)
        for entry in manifest.files:
            data = source.fetch_file(session_id, entry.name)
            (staging_dir / entry.name).write_bytes(data)
    except (MacFetchError, ValueError, OSError):
        _mark_fetch_failed(store, session_id, event_log)
        report.failed.append(session_id)
        return

    _advance_to_transferring(store, session_id)
    result = verify_and_promote(
        store,
        manifest,
        staging_dir,
        inbox_root / session_id,
    )
    if result.ok:
        report.pulled.append(session_id)
    else:
        event_log.record(
            session_id,
            State.NEEDS_REPROCESS,
            error_kind=ERROR_KIND_TRANSFER_VERIFICATION_FAILED,
        )
        report.failed.append(session_id)


def pull_pending(
    store,
    source: MacSource,
    *,
    staging_root: Path | str,
    inbox_root: Path | str,
    event_log,
) -> PullReport:
    """Mac 側の転送待ちセッションを取得し、受信検証まで進める。

    セッション記録そのものを取得できない場合は ``MacFetchError`` を
    呼び出し側へ伝える（全体の失敗を成功扱いにしない）。個別
    セッションの失敗は隔離し、レポートの ``failed`` に入れる。
    """
    staging_root = Path(staging_root)
    inbox_root = Path(inbox_root)
    report = PullReport()

    records = source.list_sessions()
    for record in records:
        if record.state not in _PULLABLE_MAC_STATES:
            continue
        if not _SESSION_ID_PATTERN.match(record.session_id):
            # 形式外の ID は、ステージングや録音置き場の外を触らない
            # よう登録も取得もしない。黙って落とさず、失敗として
            # 可視化する（silent failure 禁止）。
            event_log.record(
                record.session_id,
                State.NEEDS_REPROCESS,
                error_kind=ERROR_KIND_TRANSFER_FETCH_FAILED,
            )
            report.failed.append(record.session_id)
            continue
        _pull_one(
            store,
            source,
            record,
            staging_root=staging_root,
            inbox_root=inbox_root,
            event_log=event_log,
            report=report,
        )

    return report
