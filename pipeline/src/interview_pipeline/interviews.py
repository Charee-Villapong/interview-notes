"""検知レジストリ・検知判定・候補抽出。

検知レジストリは、録音セッションの状態管理（``SessionStore``）とは
別の永続記録で、カレンダーで検知した面談予定を ``event_id`` で冪等に
保持し、セッションIDで録音セッションと紐付ける。未録音の予定を
``recorded`` として ``SessionStore`` へ登録することはしない
（状態の意味の正本は ``docs/状態管理.md``）。

セッションIDの採番は正本「セッション識別子」の規則（``<日付>_NN``）
に従い、``SessionStore`` の既存セッションとレジストリ内の同日
レコードの両方と衝突しない最小の連番を使う。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from interview_pipeline.gateway import CalendarEvent
from interview_pipeline.google_config import GoogleConfig

__all__ = [
    "InterviewRecord",
    "InterviewRegistry",
    "extract_company",
    "extract_format",
    "is_interview_event",
]

#: ノート未作成時の note_error 値（SPEC P2 決定事項 2）。
ERROR_KIND_DOCS_TEMPLATE_NOT_CONFIGURED = "docs_template_not_configured"

_PAREN_RE = re.compile(r"[（(]([^（）()]+)[）)]")
_COMPANY_SPLIT_RE = re.compile(r"[・、,／/]")


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


@dataclass
class InterviewRecord:
    """検知した面談予定1件の記録。"""

    event_id: str
    session_id: str
    date: str
    start: datetime
    end: datetime
    title: str
    company: str = ""
    format: str = ""
    meeting_url: str = ""
    docs_doc_id: str = ""
    docs_url: str = ""
    note_error: str | None = None
    reminders: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "date": self.date,
            "start": _datetime_to_str(self.start),
            "end": _datetime_to_str(self.end),
            "title": self.title,
            "company": self.company,
            "format": self.format,
            "meeting_url": self.meeting_url,
            "docs_doc_id": self.docs_doc_id,
            "docs_url": self.docs_url,
            "note_error": self.note_error,
            "reminders": dict(self.reminders),
            "created_at": _datetime_to_str(self.created_at),
            "updated_at": _datetime_to_str(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: dict) -> InterviewRecord:
        return cls(
            event_id=data["event_id"],
            session_id=data["session_id"],
            date=data["date"],
            start=datetime.fromisoformat(data["start"]),
            end=datetime.fromisoformat(data["end"]),
            title=data.get("title", ""),
            company=data.get("company", ""),
            format=data.get("format", ""),
            meeting_url=data.get("meeting_url", ""),
            docs_doc_id=data.get("docs_doc_id", ""),
            docs_url=data.get("docs_url", ""),
            note_error=data.get("note_error"),
            reminders=dict(data.get("reminders", {})),
            created_at=_datetime_from_str(data.get("created_at")) or _now(),
            updated_at=_datetime_from_str(data.get("updated_at")) or _now(),
        )


class InterviewRegistry:
    """検知レジストリの永続化ストア（``SessionStore`` と同じ流儀）。

    インスタンス化時に既存ファイルがあれば自動で読み込む。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._records: dict[str, InterviewRecord] = {}
        if self.path.exists():
            self.load()

    # -- 基本操作 -----------------------------------------------------

    def upsert(self, record: InterviewRecord) -> InterviewRecord:
        """レコードを登録する。同一 ``event_id`` なら既存を返す（冪等）。"""
        existing = self._records.get(record.event_id)
        if existing is not None:
            return existing
        self._records[record.event_id] = record
        self.save()
        return record

    def get_by_event(self, event_id: str) -> InterviewRecord | None:
        return self._records.get(event_id)

    def get(self, session_id: str) -> InterviewRecord | None:
        for record in self._records.values():
            if record.session_id == session_id:
                return record
        return None

    def all(self) -> list[InterviewRecord]:
        return list(self._records.values())

    def assign_session_id(self, date: str, store) -> str:
        """``date``（``YYYY-MM-DD``）のセッションIDを採番する。

        ``SessionStore`` の既存セッションとレジストリ内の同日レコード
        の両方を見て、衝突しない最小の連番（``_01`` から）を返す。
        """
        used: set[int] = set()
        prefix = f"{date}_"
        candidates: list[str] = []
        if store is not None:
            candidates.extend(session.session_id for session in store.all())
        candidates.extend(record.session_id for record in self._records.values())
        for session_id in candidates:
            if not session_id.startswith(prefix):
                continue
            suffix = session_id[len(prefix) :]
            if suffix.isdigit():
                used.add(int(suffix))
        seq = 1
        while seq in used:
            seq += 1
        return f"{date}_{seq:02d}"

    # -- 永続化 -------------------------------------------------------

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "interviews": [record.to_dict() for record in self._records.values()]
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self) -> None:
        if not self.path.exists():
            self._records = {}
            return
        text = self.path.read_text(encoding="utf-8").strip()
        if not text:
            self._records = {}
            return
        payload = json.loads(text)
        if isinstance(payload, dict):
            items = payload.get("interviews", [])
        elif isinstance(payload, list):
            items = payload
        else:
            items = []
        self._records = {}
        for item in items:
            record = InterviewRecord.from_dict(item)
            self._records[record.event_id] = record


# ---------------------------------------------------------------------------
# 検知判定と候補抽出
# ---------------------------------------------------------------------------


def _contains(text: str, keyword: str) -> bool:
    return keyword.lower() in text.lower()


def is_interview_event(event: CalendarEvent, config: GoogleConfig) -> str | None:
    """面談予定かどうかを判定し、根拠を返す（対象外は ``None``）。

    - キャンセル済み予定は常に対象外
    - 自己登録予定: タイトルに主キーを含めば ``"primary"``、
      副キーを含めば ``"secondary"``
    - 招待予定: タイトルまたは説明文に招待キーワードを含めば ``"invited"``
    """
    if event.cancelled:
        return None
    title = event.title or ""
    if event.organizer_self:
        if _contains(title, config.primary_keyword):
            return "primary"
        if any(_contains(title, kw) for kw in config.secondary_keywords):
            return "secondary"
        return None
    haystacks = [title, event.description or ""]
    if any(_contains(text, kw) for text in haystacks for kw in config.invited_keywords):
        return "invited"
    return None


def extract_company(event: CalendarEvent) -> str:
    """企業名の候補をタイトルから抽出する（ベストエフォート）。

    括弧内（全角・半角）の先頭要素を候補とする。括弧が無ければ
    推測で埋めず、空文字列を返す。
    """
    match = _PAREN_RE.search(event.title or "")
    if not match:
        return ""
    first = _COMPANY_SPLIT_RE.split(match.group(1))[0].strip()
    return first


def extract_format(event: CalendarEvent) -> str:
    """形式の候補を作る。場所を優先し、無ければ会議URLの種別から作る。"""
    location = (event.location or "").strip()
    if location:
        return location
    haystack = f"{event.meeting_url}\n{event.description or ''}"
    if "meet.google.com" in haystack:
        return "オンライン（Google Meet）"
    if "zoom.us" in haystack:
        return "オンライン（Zoom）"
    if "teams.microsoft.com" in haystack or "teams.live.com" in haystack:
        return "オンライン（Microsoft Teams）"
    if event.meeting_url:
        return "オンライン"
    return ""
