"""要約の登録（push 待ちへの遷移）。

生成済みの要約 md を「push 待ち」として登録し、状態を ``draft_saved``
へ進める。要約の実体は作業領域に一時保持し、非公開リポジトリへの
push は ``publish`` 側が担う。push 成功が確認できるまでは、ローカル
コピーを削除しない。

登録前に音声非含有チェックを必須とする。音声だった場合は
``needs_reprocess``（resume: ``awaiting_editing``）にして送出する。
要約ファイルが無い・空の場合は ``NoteContentError`` を送出し、
状態は変えない。
"""

from __future__ import annotations

from pathlib import Path

from interview_pipeline.errors import AudioContentError, NoteContentError
from interview_pipeline.guards import assert_no_audio_files
from interview_pipeline.states import ERROR_KIND_AUDIO_CONTENT_DETECTED, State
from interview_pipeline.store import update_needs_reprocess_info

__all__ = [
    "AudioContentError",
    "NoteContentError",
    "save_note",
]


def save_note(store, session_id: str, note_path: Path | str) -> Path:
    note = Path(note_path)

    # 1. 音声・動画の非含有チェック（失敗時は needs_reprocess にする）。
    try:
        assert_no_audio_files([note])
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

    # 2. 要約ファイルの存在と中身の確認（無い・空なら登録しない）。
    content = note.read_text(encoding="utf-8") if note.is_file() else ""
    if not content.strip():
        raise NoteContentError("note file is missing or empty")

    # 3. 状態を draft_saved へ進める（push 待ち）。
    current_state = store.get(session_id).state
    if current_state is not State.DRAFT_SAVED:
        store.transition(session_id, State.DRAFT_SAVED)
    return note
