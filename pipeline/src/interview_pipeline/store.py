"""セッション状態の永続化ストア。

SPEC ``store.py`` 契約と決定事項 8・9・10 に従う:
- ``register`` の初期状態は ``recorded`` で冪等。
- ``processing_completed`` は ``awaiting_editing`` への遷移時に立てる。
- ``SessionStore`` はインスタンス化時に既存ファイルがあれば自動で読み込む。
- ``draft_saved`` / ``final_saved`` への遷移時に日時を自動で記録する。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from interview_pipeline.errors import InvalidTransitionError
from interview_pipeline.states import State, assert_transition

__all__ = ["InvalidTransitionError", "Session", "SessionStore"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _datetime_to_str(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _datetime_from_str(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _state_to_str(value: State | None) -> str | None:
    if value is None:
        return None
    return value.value


def _state_from_str(value: str | None) -> State | None:
    if value is None:
        return None
    return State(value)


@dataclass
class Session:
    session_id: str
    state: State
    created_at: datetime
    resume_state: State | None = None
    error_kind: str | None = None
    draft_saved_at: datetime | None = None
    final_saved_at: datetime | None = None
    vm_copy_deleted_at: datetime | None = None
    mac_copy_deleted_at: datetime | None = None
    processing_completed: bool = False

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "state": _state_to_str(self.state),
            "created_at": _datetime_to_str(self.created_at),
            "resume_state": _state_to_str(self.resume_state),
            "error_kind": self.error_kind,
            "draft_saved_at": _datetime_to_str(self.draft_saved_at),
            "final_saved_at": _datetime_to_str(self.final_saved_at),
            "vm_copy_deleted_at": _datetime_to_str(self.vm_copy_deleted_at),
            "mac_copy_deleted_at": _datetime_to_str(self.mac_copy_deleted_at),
            "processing_completed": bool(self.processing_completed),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Session:
        return cls(
            session_id=data["session_id"],
            state=State(data["state"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            resume_state=_state_from_str(data.get("resume_state")),
            error_kind=data.get("error_kind"),
            draft_saved_at=_datetime_from_str(data.get("draft_saved_at")),
            final_saved_at=_datetime_from_str(data.get("final_saved_at")),
            vm_copy_deleted_at=_datetime_from_str(data.get("vm_copy_deleted_at")),
            mac_copy_deleted_at=_datetime_from_str(data.get("mac_copy_deleted_at")),
            processing_completed=bool(data.get("processing_completed", False)),
        )


@dataclass
class SessionStore:
    path: Path
    _sessions: dict[str, Session] = field(default_factory=dict, init=False, repr=False)

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._sessions: dict[str, Session] = {}
        if self.path.exists():
            self.load()

    # -- 基本操作 -----------------------------------------------------

    def register(self, session_id: str, created_at: datetime) -> Session:
        """セッションを登録する。既存なら既存を返す（冪等）。"""
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        session = Session(
            session_id=session_id,
            state=State.RECORDED,
            created_at=created_at,
        )
        self._sessions[session_id] = session
        self.save()
        return session

    def get(self, session_id: str) -> Session:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise KeyError(f"unknown session: {session_id}") from None

    def all(self) -> list[Session]:
        return list(self._sessions.values())

    def transition(
        self,
        session_id: str,
        dst: State | str,
        *,
        error_kind: str | None = None,
        resume_state: State | str | None = None,
    ) -> Session:
        """遷移検証つきで状態を更新し、保存する。"""
        session = self.get(session_id)
        dst_state = dst if isinstance(dst, State) else State(str(dst))
        # needs_reprocess に既にいる状態で再度失敗を記録する場合は、
        # 状態そのものは変えずに error_kind / resume_state だけ更新する。
        if (
            session.state is State.NEEDS_REPROCESS
            and dst_state is State.NEEDS_REPROCESS
        ):
            if error_kind is not None:
                session.error_kind = error_kind
            if resume_state is not None:
                session.resume_state = (
                    resume_state
                    if isinstance(resume_state, State)
                    else State(str(resume_state))
                )
            self.save()
            return session

        assert_transition(session.state, dst_state)

        if dst_state is State.NEEDS_REPROCESS:
            if error_kind is not None:
                session.error_kind = error_kind
            if resume_state is not None:
                session.resume_state = (
                    resume_state
                    if isinstance(resume_state, State)
                    else State(str(resume_state))
                )
        elif session.state is State.NEEDS_REPROCESS:
            # 復帰時は失敗情報をクリアする。
            session.error_kind = None
            session.resume_state = None

        session.state = dst_state

        if dst_state is State.DRAFT_SAVED and session.draft_saved_at is None:
            session.draft_saved_at = _now()
        if dst_state is State.FINAL_SAVED and session.final_saved_at is None:
            session.final_saved_at = _now()
        if dst_state is State.AWAITING_EDITING:
            session.processing_completed = True

        self.save()
        return session

    # -- 永続化 -------------------------------------------------------

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessions": [session.to_dict() for session in self._sessions.values()]
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self) -> None:
        if not self.path.exists():
            self._sessions = {}
            return
        text = self.path.read_text(encoding="utf-8").strip()
        if not text:
            self._sessions = {}
            return
        payload = json.loads(text)
        if isinstance(payload, dict):
            items = payload.get("sessions", [])
        elif isinstance(payload, list):
            items = payload
        else:
            items = []
        self._sessions = {}
        for item in items:
            session = Session.from_dict(item)
            self._sessions[session.session_id] = session
