"""保存・削除ガード。

削除は実行直前にコードが条件を再確認し、満たさなければ例外で拒否する
（状態管理 M2）。未処理は日数に関わらず絶対に消さない（M1）。
"""

from __future__ import annotations

import shutil
import subprocess
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
    "check_note_storage",
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


# ノート保存ガードの git 実行タイムアウト（秒）。確認が終わらない場合は
# 「確認できない」として False に倒す（fail-closed）。
_GIT_TIMEOUT_SECONDS = 10


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess | None:
    """git を実行する。実行自体に失敗したら None（確認できない扱い）。"""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def check_note_storage(destination: Path | str) -> bool:
    """保存先が「公開され得ない格納場所」であることを git で機械確認する。

    次のすべてを満たすときだけ True を返す。1つでも満たさない・
    確認できない場合は False（fail-closed）:

    - 保存先パスが git 作業コピーの内側にある
    - 保存先ファイルが ``git check-ignore`` で ignore 済みと判定される
    - 格納フォルダ（保存先の2階層上）配下で追跡済みのファイルが
      ``README.md`` のみである（``git ls-files`` で確認）

    git が実行できない・作業コピーでない場合も確認できないため False。
    保存先の日付フォルダは保存時に作られるため、未存在でも判定できる
    （git コマンドは既存の祖先ディレクトリから実行する）。
    """
    dest = Path(destination)
    if shutil.which("git") is None:
        return False

    # git コマンドの実行起点として、保存先の最も近い既存の祖先を探す。
    anchor = dest.parent
    while not anchor.is_dir():
        if anchor.parent == anchor:
            return False
        anchor = anchor.parent

    top = _run_git(["rev-parse", "--show-toplevel"], anchor)
    if top is None or top.returncode != 0:
        return False
    root = Path(top.stdout.strip()).resolve()

    resolved = dest.resolve()
    if root not in resolved.parents:
        return False
    rel = resolved.relative_to(root).as_posix()

    ignored = _run_git(["check-ignore", "-q", "--", rel], root)
    if ignored is None or ignored.returncode != 0:
        return False

    notes_rel = Path(rel).parent.parent.as_posix()
    tracked = _run_git(["ls-files", "--full-name", "--", notes_rel], root)
    if tracked is None or tracked.returncode != 0:
        return False
    entries = {line.strip() for line in tracked.stdout.splitlines() if line.strip()}
    return entries <= {f"{notes_rel}/README.md"}


def _state_name(session) -> str:
    # セッションから状態を取り出す部分だけが runner 側と異なる。
    # 状態値から名前を得る規則は states.state_name に一本化する。
    return state_name(getattr(session, "state", None))


def vm_copy_deletable(session) -> bool:
    """処理完了かつ ``draft_saved`` 以降の状態で ``draft_saved_at``
    記録済みのときだけ True。

    正本 docs/状態管理.md「削除の条件」の二重条件: 処理完了
    （``processing_completed``）と、GitHub への下書き保存確認
    （``draft_saved`` 以降の状態で ``draft_saved_at`` が記録されて
    いること）の両方を満たす必要がある。``needs_reprocess`` や
    ``draft_saved`` より前の状態では False。
    """
    if _state_name(session) not in {
        State.DRAFT_SAVED.value,
        State.AWAITING_REVIEW.value,
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
    """``final_saved`` 以降の状態で、かつ ``needs_reprocess``
    でないときだけ True。

    正本 docs/状態管理.md「削除の条件」: 確定版が保存済み
    （``final_saved`` 以降の状態）かつ未処理でないこと
    （``needs_reprocess`` でないこと）を満たす必要がある。
    ``final_saved_at`` の記録の有無は条件ではない。期限
    （最長7日）は削除の許可条件ではない（未処理は日数に
    関わらず消さない）。
    """
    state = _state_name(session)
    if state == State.NEEDS_REPROCESS.value:
        return False
    return state in {
        State.FINAL_SAVED.value,
        State.DELETABLE.value,
        State.CLOSED.value,
    }


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
