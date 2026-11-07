"""状態と遷移の定義。

正本は ``docs/状態管理.md`` の「状態一覧」「遷移条件」。
SPEC 決定事項 1・2:
- ``closed`` は終端状態とし、外向きの遷移を持たない。
- ``needs_reprocess`` からの復帰は ``resume_state`` への遷移のみとし、
  ``TRANSITIONS`` では ``needs_reprocess -> {transfer_pending, received,
  awaiting_editing}`` を許可する。
"""

from __future__ import annotations

from enum import Enum

from interview_pipeline.errors import InvalidTransitionError

__all__ = [
    "ERROR_KIND_AUDIO_CONTENT_DETECTED",
    "ERROR_KIND_PROCESSING_FAILED",
    "ERROR_KIND_STT_NOT_CONFIGURED",
    "ERROR_KIND_TRANSFER_VERIFICATION_FAILED",
    "InvalidTransitionError",
    "State",
    "TRANSITIONS",
    "assert_transition",
    "can_transition",
]

# error_kind の契約値（SPEC の公開インターフェースで固定された文字列）。
# セッションとイベントログの両方に同じ値が記録されるため、リテラルを
# 散在させずここに集約する。値そのものは契約なので変更しない。
ERROR_KIND_TRANSFER_VERIFICATION_FAILED = "transfer_verification_failed"
ERROR_KIND_STT_NOT_CONFIGURED = "stt_not_configured"
ERROR_KIND_PROCESSING_FAILED = "processing_failed"
ERROR_KIND_AUDIO_CONTENT_DETECTED = "audio_content_detected"


class State(str, Enum):
    RECORDED = "recorded"
    TRANSFER_PENDING = "transfer_pending"
    TRANSFERRING = "transferring"
    RECEIVED = "received"
    PROCESSING = "processing"
    AWAITING_EDITING = "awaiting_editing"
    DRAFT_SAVED = "draft_saved"
    AWAITING_REVIEW = "awaiting_review"
    FINAL_SAVED = "final_saved"
    DELETABLE = "deletable"
    CLOSED = "closed"
    NEEDS_REPROCESS = "needs_reprocess"


TRANSITIONS: dict[State, set[State]] = {
    State.RECORDED: {State.TRANSFER_PENDING, State.NEEDS_REPROCESS},
    State.TRANSFER_PENDING: {State.TRANSFERRING, State.NEEDS_REPROCESS},
    State.TRANSFERRING: {State.RECEIVED, State.NEEDS_REPROCESS},
    State.RECEIVED: {State.PROCESSING, State.NEEDS_REPROCESS},
    State.PROCESSING: {State.AWAITING_EDITING, State.NEEDS_REPROCESS},
    State.AWAITING_EDITING: {State.DRAFT_SAVED, State.NEEDS_REPROCESS},
    State.DRAFT_SAVED: {State.AWAITING_REVIEW, State.NEEDS_REPROCESS},
    State.AWAITING_REVIEW: {State.FINAL_SAVED, State.NEEDS_REPROCESS},
    State.FINAL_SAVED: {State.DELETABLE, State.NEEDS_REPROCESS},
    State.DELETABLE: {State.CLOSED, State.NEEDS_REPROCESS},
    State.CLOSED: set(),
    State.NEEDS_REPROCESS: {
        State.TRANSFER_PENDING,
        State.RECEIVED,
        State.AWAITING_EDITING,
    },
}


def _coerce(state: State | str) -> State:
    if isinstance(state, State):
        return state
    return State(str(state))


def can_transition(src: State | str, dst: State | str) -> bool:
    """``src`` から ``dst`` への遷移が許可されているかを返す。"""
    try:
        src_state = _coerce(src)
        dst_state = _coerce(dst)
    except ValueError:
        return False
    return dst_state in TRANSITIONS.get(src_state, set())


def assert_transition(src: State | str, dst: State | str) -> None:
    """許可されていない遷移なら ``InvalidTransitionError`` を送出する。"""
    if not can_transition(src, dst):
        raise InvalidTransitionError(f"transition not allowed: {src!r} -> {dst!r}")
