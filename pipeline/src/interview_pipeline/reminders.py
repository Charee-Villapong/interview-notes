"""リマインド判定と定型文生成・送信済み記録。

判定はコードで固定する（SPEC「P2 追加契約」／手順書「リマインド」）:

- 面談日が翌日: ``eve``（前日リマインド。スロット不問・1回のみ）
- 面談日が当日で午前の回: ``day_morning``（準備ノートの最終確認）
- 面談日が当日で午後の回: ``day_evening``（振り返り記入リマインド）
- それ以外は何も出さない

送信済みの記録は ``mark_reminded`` で送信後に行う二段構えとし、
``due_reminders`` 自体は記録しない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta

from interview_pipeline.interviews import InterviewRecord, InterviewRegistry

__all__ = [
    "KIND_DAY_EVENING",
    "KIND_DAY_MORNING",
    "KIND_EVE",
    "PREP_SECTIONS",
    "Reminder",
    "due_reminders",
    "mark_reminded",
    "unfilled_sections",
]

KIND_EVE = "eve"
KIND_DAY_MORNING = "day_morning"
KIND_DAY_EVENING = "day_evening"

#: 準備セクション（テンプレートの見出し。未記入チェックの対象）。
PREP_SECTIONS = ["企業研究", "想定問答", "聞きたいこと"]

#: セクションの区切りとして扱う見出し（準備・振り返り・基本情報）。
_ALL_HEADINGS = PREP_SECTIONS + [
    "聞かれたこと",
    "答え",
    "次の一手",
    "準備",
    "振り返り",
    "基本情報",
]

_UNFILLED_MARKER = "（未記入）"


@dataclass
class Reminder:
    """送信対象のリマインド1件。"""

    event_id: str
    session_id: str
    kind: str
    text: str


def unfilled_sections(note_text: str) -> list[str]:
    """準備セクションのうち未記入のものを返す。

    本文が空、または「（未記入）」のままのセクションと、見出し
    自体が無いセクションを未記入とする。
    """
    lines = (note_text or "").splitlines()
    heading_index: dict[str, int] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped in _ALL_HEADINGS and stripped not in heading_index:
            heading_index[stripped] = index

    unfilled: list[str] = []
    for section in PREP_SECTIONS:
        start = heading_index.get(section)
        if start is None:
            unfilled.append(section)
            continue
        body_lines: list[str] = []
        for line in lines[start + 1 :]:
            if line.strip() in _ALL_HEADINGS:
                break
            body_lines.append(line)
        body = "\n".join(body_lines).strip()
        if not body or _UNFILLED_MARKER in body:
            unfilled.append(section)
    return unfilled


def _time_range(record: InterviewRecord) -> str:
    return f"{record.start:%H:%M}〜{record.end:%H:%M}"


def _label(record: InterviewRecord) -> str:
    return record.company or record.title or record.session_id


def _eve_text(record: InterviewRecord, note_text: str | None) -> str:
    lines = [
        "【面談リマインド】",
        f"明日 {record.date} {_time_range(record)} に{_label(record)}の面談があります"
        f"（{record.format or '形式未定'}）。",
    ]
    if not record.docs_doc_id:
        lines.append(
            "準備ノートがまだ作成されていません（作成に失敗しています）。"
            "ノートなしでも準備だけは進めてください。"
        )
        return "\n".join(lines)
    lines.append(f"準備ノート: {record.docs_url}")
    unfilled = unfilled_sections(note_text or "")
    if unfilled:
        lines.append("未記入の項目: " + "、".join(unfilled))
    else:
        lines.append("準備項目はすべて記入済みです。")
    return "\n".join(lines)


def _day_morning_text(record: InterviewRecord) -> str:
    lines = [
        "【面談リマインド】",
        f"本日 {record.date} {_time_range(record)} に{_label(record)}の面談です"
        f"（{record.format or '形式未定'}）。",
    ]
    if record.docs_url:
        lines.append(f"準備ノートの最終確認をしてください: {record.docs_url}")
    if record.meeting_url:
        lines.append(f"会議URL: {record.meeting_url}")
    return "\n".join(lines)


def _day_evening_text(record: InterviewRecord) -> str:
    lines = [
        "【振り返りリマインド】",
        f"本日の{_label(record)}の面談、お疲れさまでした。",
        "当日中に、短い振り返り（3行）をノートへ記入してください。",
    ]
    if record.docs_url:
        lines.append(f"面談ノート: {record.docs_url}")
    return "\n".join(lines)


def due_reminders(
    registry: InterviewRegistry,
    now: datetime,
    slot: str,
    *,
    note_texts: dict[str, str] | None = None,
) -> list[Reminder]:
    """送信対象のリマインドを判定する（記録はしない）。"""
    if slot not in ("morning", "evening"):
        raise ValueError(f"unknown slot: {slot!r}")
    texts = note_texts or {}
    today = now.date()
    tomorrow = today + timedelta(days=1)

    reminders: list[Reminder] = []
    for record in registry.all():
        try:
            interview_date = date_type.fromisoformat(record.date)
        except ValueError:
            continue
        kind: str | None = None
        if interview_date == tomorrow:
            kind = KIND_EVE
        elif interview_date == today and slot == "morning":
            kind = KIND_DAY_MORNING
        elif interview_date == today and slot == "evening":
            kind = KIND_DAY_EVENING
        if kind is None or kind in record.reminders:
            continue
        if kind == KIND_EVE:
            note_text = texts.get(record.event_id, texts.get(record.session_id))
            text = _eve_text(record, note_text)
        elif kind == KIND_DAY_MORNING:
            text = _day_morning_text(record)
        else:
            text = _day_evening_text(record)
        reminders.append(
            Reminder(
                event_id=record.event_id,
                session_id=record.session_id,
                kind=kind,
                text=text,
            )
        )
    return reminders


def mark_reminded(
    registry: InterviewRegistry, event_id: str, kind: str, at: datetime
) -> InterviewRecord:
    """リマインドの送信済みをレジストリへ記録する（送信後に呼ぶ）。"""
    record = registry.get_by_event(event_id)
    if record is None:
        raise KeyError(f"unknown interview event: {event_id}")
    record.reminders[kind] = at.isoformat()
    record.updated_at = at
    registry.save()
    return record
