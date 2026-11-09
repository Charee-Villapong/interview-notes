"""要約の push（非公開リポジトリへの公開）。

登録済み（``draft_saved``）の要約を、設定された非公開リポジトリへ
GitHub API で直接 push する。要約は VM ローカルに恒常保存しない:
push 成功をリモート上の内容一致で確認できたら、ローカルコピーを
削除し、状態を ``final_saved`` へ進めて ``note_published_at`` を
記録する。push が完了しない場合は ``NotePublishError`` を送出し、
要約ファイルはローカルに保持したままにする（黙って成功扱いに
しない）。push 前には、宛先が非公開リポジトリであることを
``check_note_destination`` で確認する（公開リポジトリへは上げない）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from interview_pipeline.errors import NotePublishError
from interview_pipeline.guards import check_note_destination
from interview_pipeline.states import State

__all__ = [
    "GhGitHubClient",
    "NotePublishError",
    "git_blob_sha",
    "note_remote_path",
    "publish_note",
]

# gh 実行のタイムアウト（秒）。確認が終わらない場合は失敗として扱う。
_GH_TIMEOUT_SECONDS = 60

# セッションIDの区切り文字。リモート配置の日付部分の決定に使う
# （saving 側の旧規則と同じ前提: ID は ``<面談日付>_<連番>`` 形式）。
_SESSION_ID_SEPARATOR = "_"


def git_blob_sha(content: bytes) -> str:
    """git の blob オブジェクトとしての SHA-1（GitHub API の sha と一致）。"""
    header = f"blob {len(content)}\0".encode("utf-8")
    return hashlib.sha1(header + content).hexdigest()


def _session_date_part(session, session_id: str) -> str:
    """リモート配置先フォルダの日付部分を決める。

    通常は ``created_at`` の日付を使う。``created_at`` が無い場合に限り、
    セッションIDが ``<面談日付>_<連番>`` 形式である前提で、その日付
    部分にフォールバックする（ID 形式が変わるとこの前提も見直すこと）。
    """
    created_at = getattr(session, "created_at", None)
    if created_at is not None and hasattr(created_at, "date"):
        return created_at.date().isoformat()
    return session_id.split(_SESSION_ID_SEPARATOR)[0]


def note_remote_path(session, session_id: str) -> str:
    """リポジトリ内の配置パス（``<日付>/<session_id>.md``）。"""
    return f"{_session_date_part(session, session_id)}/{session_id}.md"


class GhGitHubClient:
    """``gh api`` 経由で GitHub へアクセスするクライアント。

    各メソッドは失敗時に例外を送出せず、取得できない場合は None /
    False を返す（呼び出し側が fail-closed に判定する）。
    """

    def __init__(self, command: str = "gh") -> None:
        self._command = command

    def _api(self, args: list[str], *, input_text: str | None = None) -> dict | None:
        try:
            result = subprocess.run(
                [self._command, "api", *args],
                input=input_text,
                capture_output=True,
                text=True,
                timeout=_GH_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    def repo_info(self, repo: str) -> dict | None:
        """リポジトリ情報（visibility / default_branch を含む）。"""
        return self._api([f"repos/{repo}"])

    def content_sha(self, repo: str, path: str) -> str | None:
        """配置済みファイルの blob sha。無い・取得できない場合は None。"""
        payload = self._api([f"repos/{repo}/contents/{path}"])
        if not payload:
            return None
        sha = payload.get("sha")
        return sha if isinstance(sha, str) else None

    def put_content(
        self,
        repo: str,
        path: str,
        content: bytes,
        *,
        message: str,
        sha: str | None = None,
    ) -> bool:
        """ファイルを作成／更新する。成功したら True。"""
        args = [
            "-X",
            "PUT",
            f"repos/{repo}/contents/{path}",
            "-f",
            f"message={message}",
            "-f",
            f"content={base64.b64encode(content).decode('ascii')}",
        ]
        if sha is not None:
            args += ["-f", f"sha={sha}"]
        return self._api(args) is not None


def publish_note(
    store,
    session_id: str,
    note_path: Path | str,
    *,
    notes_repo: str,
    github,
) -> str:
    """要約を非公開リポジトリへ push し、完了したら URL を返す。

    手順はコードで固定する: 状態と要約の確認 → 宛先が非公開である
    ことの確認 → push → リモート上の内容一致の確認 → 記録と状態
    遷移 → ローカルコピーの削除。途中で完了しない場合は
    ``NotePublishError`` を送出し、要約ファイルは保持する。
    """
    session = store.get(session_id)
    if getattr(session, "state", None) is not State.DRAFT_SAVED:
        raise NotePublishError("session is not in draft_saved state")

    note = Path(note_path)
    if not note.is_file():
        raise NotePublishError("note file is missing")
    content = note.read_bytes()
    if not content.strip():
        raise NotePublishError("note file is empty")

    if not check_note_destination(notes_repo, github=github):
        raise NotePublishError("destination repository is not confirmed private")

    remote_path = note_remote_path(session, session_id)
    blob_sha = git_blob_sha(content)

    existing_sha = github.content_sha(notes_repo, remote_path)
    if existing_sha != blob_sha:
        pushed = github.put_content(
            notes_repo,
            remote_path,
            content,
            message=f"notes: add {session_id}",
            sha=existing_sha,
        )
        if not pushed:
            raise NotePublishError("push failed")

    if github.content_sha(notes_repo, remote_path) != blob_sha:
        raise NotePublishError("push could not be verified")

    session.note_published_at = datetime.now(timezone.utc)
    store.transition(session_id, State.FINAL_SAVED)

    note.unlink()

    info = github.repo_info(notes_repo) or {}
    branch = info.get("default_branch") or "main"
    return f"https://github.com/{notes_repo}/blob/{branch}/{remote_path}"
