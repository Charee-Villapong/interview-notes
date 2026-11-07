"""下書き保存（保存ガード付き）。

保存前に音声非含有チェックと保存先の非公開確認を必須とする。
自動生成 md は冒頭に「下書き（未照合）」を含むこと。
成功時は ``draft_saved`` → ``awaiting_review`` へ進める。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from interview_pipeline.errors import (
    AudioContentError,
    DraftMarkingError,
    RepositoryNotPrivateError,
)
from interview_pipeline.guards import assert_no_audio_files
from interview_pipeline.states import ERROR_KIND_AUDIO_CONTENT_DETECTED, State
from interview_pipeline.store import update_needs_reprocess_info

__all__ = [
    "AudioContentError",
    "DraftMarkingError",
    "RepositoryNotPrivateError",
    "save_draft",
]

DRAFT_MARKING = "下書き（未照合）"

# セッションIDの区切り文字。ID は ``<面談日付>_<連番>`` 形式
# （例: ``2026-11-09_01``、正本 docs/状態管理.md「セッション識別子」）で、
# 日付部分はこの区切りの先頭要素になる。
_SESSION_ID_SEPARATOR = "_"


def _session_date_part(session, session_id: str) -> str:
    """保存先フォルダの日付部分を決める。

    通常は ``created_at`` の日付を使う。``created_at`` が無い場合に限り、
    セッションIDが ``<面談日付>_<連番>`` 形式である前提で、その日付
    部分にフォールバックする（ID 形式が変わるとこの前提も見直すこと）。
    """
    created_at = getattr(session, "created_at", None)
    if created_at is not None and hasattr(created_at, "date"):
        return created_at.date().isoformat()
    return session_id.split(_SESSION_ID_SEPARATOR)[0]


def save_draft(
    store,
    session_id: str,
    draft_path: Path | str,
    notes_dir: Path | str,
    *,
    privacy_checker: Callable[[], bool],
) -> Path:
    draft = Path(draft_path)
    notes = Path(notes_dir)
    session = store.get(session_id)

    # 1. 音声・動画の非含有チェック（失敗時は needs_reprocess にする）。
    try:
        assert_no_audio_files([draft])
    except AudioContentError:
        # 現在状態から needs_reprocess へ移せる場合のみ遷移する。
        try:
            store.transition(
                session_id,
                State.NEEDS_REPROCESS,
                error_kind=ERROR_KIND_AUDIO_CONTENT_DETECTED,
                resume_state=State.AWAITING_EDITING,
            )
        except Exception:
            # 既に needs_reprocess 等で遷移できない場合は情報を直接更新する。
            current = store.get(session_id)
            if getattr(current, "state", None) is State.NEEDS_REPROCESS:
                update_needs_reprocess_info(
                    current,
                    error_kind=ERROR_KIND_AUDIO_CONTENT_DETECTED,
                    resume_state=State.AWAITING_EDITING,
                )
                store.save()
        raise

    # 2. 保存先が非公開であることの確認。
    if not privacy_checker():
        raise RepositoryNotPrivateError("destination repository is not private")

    # 3. 下書き表記の検査（冒頭にあること）。
    content = draft.read_text(encoding="utf-8") if draft.is_file() else ""
    stripped = content.lstrip()
    first_line = stripped.splitlines()[0] if stripped.splitlines() else ""
    if DRAFT_MARKING not in first_line:
        raise DraftMarkingError("draft must start with the draft marking")

    # 4. 配置先を決めて配置する: notes_dir/<日付>/<session_id>.md
    date_part = _session_date_part(session, session_id)
    destination_dir = notes / date_part
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{session_id}.md"
    shutil.copyfile(draft, destination)

    # 5. 状態を draft_saved → awaiting_review へ進める。
    current_state = store.get(session_id).state
    if current_state is not State.DRAFT_SAVED:
        store.transition(session_id, State.DRAFT_SAVED)
    store.transition(session_id, State.AWAITING_REVIEW)
    return destination
