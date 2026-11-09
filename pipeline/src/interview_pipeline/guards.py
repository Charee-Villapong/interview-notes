"""保存・削除ガード。

削除は実行直前にコードが条件を再確認し、満たさなければ例外で拒否する
（状態管理 M2）。未処理は日数に関わらず絶対に消さない（M1）。
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from interview_pipeline.errors import AudioContentError, DeletionGuardError
from interview_pipeline.states import State, state_name

__all__ = [
    "AUDIO_VIDEO_EXTENSIONS",
    "AudioContentError",
    "DEFAULT_OVERDUE_DAYS",
    "DeletionGuardError",
    "assert_no_audio_files",
    "check_note_destination",
    "execute_vm_deletion",
    "find_audio_files",
    "find_overdue_unprocessed",
    "mac_deletion_eligible",
    "vm_copy_deletable",
]

# 期限超過の未処理を検出する既定日数。正本 docs/状態管理.md の
# 「録音から7日を超え」が由来。削除の許可条件ではなく可視化用。
DEFAULT_OVERDUE_DAYS = 7

AUDIO_VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".m4a",
        ".mov",
        ".mkv",
        ".wav",
        ".mp3",
        ".aac",
        ".flac",
        ".ogg",
        ".opus",
        ".aiff",
        ".caf",
        ".mp4",
        ".webm",
    }
)


def _suffix(path: Path | str) -> str:
    return Path(path).suffix.lower()


def find_audio_files(paths: Iterable[Path | str]) -> list[Path]:
    """音声・動画拡張子に一致するものを返す（大文字小文字を区別しない）。"""
    result: list[Path] = []
    for item in paths:
        path = Path(item)
        if _suffix(path) in AUDIO_VIDEO_EXTENSIONS:
            result.append(path)
    return result


def assert_no_audio_files(paths: Iterable[Path | str]) -> None:
    """音声・動画が1件でもあれば ``AudioContentError`` を送出する。"""
    found = find_audio_files(list(paths))
    if found:
        raise AudioContentError(
            "audio/video content detected: " + ", ".join(str(p) for p in found)
        )


# push 先リポジトリ名の形式（``owner/repo``）。
_REPO_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+$")


def check_note_destination(notes_repo: str, *, github) -> bool:
    """push 先が非公開リポジトリであることを機械確認する。

    ``github``（``repo_info(repo) -> dict | None`` を持つクライアント）
    でリポジトリ情報を取得し、可視性が ``private`` のときだけ True。
    形式が不正・取得できない・private でない場合は False
    （fail-closed）。公開リポジトリへ要約を上げないためのガード。
    """
    if not isinstance(notes_repo, str) or not _REPO_PATTERN.match(notes_repo):
        return False
    try:
        info = github.repo_info(notes_repo)
    except Exception:
        return False
    if not info:
        return False
    return str(info.get("visibility", "")).lower() == "private"


def _state_name(session) -> str:
    # セッションから状態を取り出す部分だけが runner 側と異なる。
    # 状態値から名前を得る規則は states.state_name に一本化する。
    return state_name(getattr(session, "state", None))


def vm_copy_deletable(session) -> bool:
    """処理完了かつ ``draft_saved`` 以降の状態で ``draft_saved_at``
    記録済みのときだけ True。

    正本 docs/状態管理.md「削除の条件」の二重条件: 処理完了
    （``processing_completed``）と、要約の生成確認
    （``draft_saved`` 以降の状態で ``draft_saved_at`` が記録されて
    いること）の両方を満たす必要がある。``needs_reprocess`` や
    ``draft_saved`` より前の状態では False。
    """
    if _state_name(session) not in {
        State.DRAFT_SAVED.value,
        State.FINAL_SAVED.value,
        State.DELETABLE.value,
        State.CLOSED.value,
    }:
        return False
    return bool(
        getattr(session, "processing_completed", False)
        and getattr(session, "draft_saved_at", None) is not None
    )


def execute_vm_deletion(store, session_id: str, inbox_dir: Path | str) -> Path:
    """VM側コピーを削除する。実行直前に条件を再確認する。

    ``inbox_dir`` は inbox ルート（配下のセッションフォルダを削除する）。
    """
    session = store.get(session_id)
    if not vm_copy_deletable(session):
        raise DeletionGuardError(
            "vm copy is not deletable: processing not completed " "or draft not saved"
        )
    target = Path(inbox_dir) / session_id
    if target.exists():
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    session.vm_copy_deleted_at = datetime.now(timezone.utc)
    store.save()
    return target


def mac_deletion_eligible(session) -> bool:
    """要約が非公開リポジトリへ push 済みのときだけ True。

    正本 docs/状態管理.md「削除の条件」: 要約の push 完了
    （``final_saved`` 以降の状態で、``note_published_at`` が
    記録されていること）かつ未処理でないこと
    （``needs_reprocess`` でないこと）を満たす必要がある。
    期限（最長7日）は削除の許可条件ではない（未処理は日数に
    関わらず消さない）。
    """
    state = _state_name(session)
    if state == State.NEEDS_REPROCESS.value:
        return False
    if state not in {
        State.FINAL_SAVED.value,
        State.DELETABLE.value,
        State.CLOSED.value,
    }:
        return False
    return getattr(session, "note_published_at", None) is not None


def find_overdue_unprocessed(
    store, now: datetime, days: int = DEFAULT_OVERDUE_DAYS
) -> list:
    """録音から ``days`` を超え ``final_saved`` 未到達のセッションを返す。

    削除用ではなく、可視化・再処理の拾い直し専用。状態は変更しない。
    """
    threshold = timedelta(days=days)
    result = []
    for session in store.all():
        if getattr(session, "final_saved_at", None) is not None:
            continue
        # final_saved 以降の状態（deletable / closed）も到達済みとして除外する。
        if _state_name(session) in {
            State.FINAL_SAVED.value,
            State.DELETABLE.value,
            State.CLOSED.value,
        }:
            continue
        created_at = getattr(session, "created_at", None)
        if created_at is None:
            continue
        if now - created_at > threshold:
            result.append(session)
    return result
