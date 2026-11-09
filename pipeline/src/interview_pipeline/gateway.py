"""Google へのアクセスの集約（ゲートウェイ）。

外部（Google）へのアクセスはすべてこのモジュールを通す。実装
``CliGoogleGateway`` は接続済みコネクタの正規経路（``hatch_gws_cli``）
を subprocess で呼び、失敗はすべて ``GoogleGatewayError`` に寄せる。
テストは ``GoogleGateway`` プロトコルのフェイク実装で切り分ける
（SPEC「P2 追加契約」）。
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from interview_pipeline.errors import GoogleGatewayError
from interview_pipeline.google_config import GoogleConfig

__all__ = [
    "CalendarEvent",
    "CliGoogleGateway",
    "GoogleGateway",
    "GoogleGatewayError",
    "extract_meeting_url",
]

#: 会議URL（Meet／Zoom／Teams）の検出パターン。
_MEETING_URL_RE = re.compile(
    r"https?://(?:meet\.google\.com|[\w.-]*zoom\.us|teams\.microsoft\.com"
    r"|teams\.live\.com)[^\s\"'<>）)]*"
)


def extract_meeting_url(text: str | None) -> str:
    """テキスト中の最初の会議URLを返す。無ければ空文字列。"""
    if not text:
        return ""
    match = _MEETING_URL_RE.search(text)
    return match.group(0) if match else ""


@dataclass
class CalendarEvent:
    """カレンダー予定の正規化済み表現。"""

    event_id: str
    calendar_id: str
    title: str
    start: datetime
    end: datetime
    organizer_email: str = ""
    organizer_self: bool = False
    location: str = ""
    description: str = ""
    meeting_url: str = ""
    cancelled: bool = False


class GoogleGateway(Protocol):
    """Google 連携の外部アクセス契約（SPEC「P2 追加契約」）。"""

    def list_events(
        self, calendar_id: str, time_min: datetime, time_max: datetime
    ) -> list[CalendarEvent]: ...

    def copy_template(
        self, template_id: str, name: str, folder_id: str | None = None
    ) -> tuple[str, str]: ...

    def replace_note_fields(self, doc_id: str, fields: dict[str, str]) -> None: ...

    def read_note_text(self, doc_id: str) -> str: ...


class CliGoogleGateway:
    """``hatch_gws_cli`` を subprocess で呼ぶ ``GoogleGateway`` 実装。"""

    def __init__(self, config: GoogleConfig) -> None:
        self._config = config

    # -- 基盤 ---------------------------------------------------------

    def _run(
        self,
        *args: str,
        params: dict | None = None,
        body: dict | None = None,
    ) -> dict | list:
        command = [self._config.cli_path, *args]
        if params is not None:
            command += ["--params", json.dumps(params, ensure_ascii=False)]
        if body is not None:
            command += ["--json", json.dumps(body, ensure_ascii=False)]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, check=False
            )
        except OSError as exc:
            raise GoogleGatewayError(f"connector CLI not runnable: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise GoogleGatewayError(
                f"connector CLI failed ({args[0]} {args[1]}): {detail[:200]}"
            )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise GoogleGatewayError("connector CLI returned non-JSON output") from exc

    # -- カレンダー ---------------------------------------------------

    def list_events(
        self, calendar_id: str, time_min: datetime, time_max: datetime
    ) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        page_token: str | None = None
        while True:
            params: dict = {
                "calendarId": calendar_id,
                "timeMin": time_min.isoformat(),
                "timeMax": time_max.isoformat(),
                "singleEvents": True,
                "orderBy": "startTime",
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._run("calendar", "events", "list", params=params)
            items = payload.get("items", []) if isinstance(payload, dict) else []
            events.extend(self._to_event(calendar_id, item) for item in items)
            page_token = (
                payload.get("nextPageToken") if isinstance(payload, dict) else None
            )
            if not page_token:
                return events

    def _to_event(self, calendar_id: str, item: dict) -> CalendarEvent:
        organizer = item.get("organizer") or {}
        description = item.get("description") or ""
        meeting_url = item.get("hangoutLink") or extract_meeting_url(description)
        return CalendarEvent(
            event_id=str(item.get("id", "")),
            calendar_id=calendar_id,
            title=item.get("summary") or "",
            start=self._parse_time(item.get("start") or {}),
            end=self._parse_time(item.get("end") or {}),
            organizer_email=organizer.get("email") or "",
            organizer_self=bool(organizer.get("self")),
            location=item.get("location") or "",
            description=description,
            meeting_url=meeting_url,
            cancelled=item.get("status") == "cancelled",
        )

    def _parse_time(self, value: dict) -> datetime:
        date_time = value.get("dateTime")
        if date_time:
            parsed = datetime.fromisoformat(date_time)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        date_value = value.get("date")
        tz = ZoneInfo(self._config.timezone)
        if date_value:
            day = datetime.fromisoformat(date_value).date()
            return datetime.combine(day, time.min, tzinfo=tz)
        return datetime.now(tz)

    # -- Docs ----------------------------------------------------------

    def copy_template(
        self, template_id: str, name: str, folder_id: str | None = None
    ) -> tuple[str, str]:
        body: dict = {"name": name}
        if folder_id:
            body["parents"] = [folder_id]
        payload = self._run(
            "drive",
            "files",
            "copy",
            params={"fileId": template_id, "ignoreDefaultVisibility": True},
            body=body,
        )
        doc_id = str(payload.get("id", "")) if isinstance(payload, dict) else ""
        if not doc_id:
            raise GoogleGatewayError("template copy returned no document id")
        info = self._run(
            "drive",
            "files",
            "get",
            params={"fileId": doc_id, "fields": "id,webViewLink"},
        )
        url = str(info.get("webViewLink", "")) if isinstance(info, dict) else ""
        return doc_id, url

    def replace_note_fields(self, doc_id: str, fields: dict[str, str]) -> None:
        requests = [
            {
                "replaceAllText": {
                    "containsText": {"text": "{{" + key + "}}", "matchCase": True},
                    "replaceText": value,
                }
            }
            for key, value in fields.items()
        ]
        if not requests:
            return
        self._run(
            "docs",
            "documents",
            "batchUpdate",
            params={"documentId": doc_id},
            body={"requests": requests},
        )

    def read_note_text(self, doc_id: str) -> str:
        payload = self._run("docs", "documents", "get", params={"documentId": doc_id})
        body = payload.get("body", {}) if isinstance(payload, dict) else {}
        parts: list[str] = []
        self._collect_text(body.get("content", []), parts)
        return "".join(parts)

    def _collect_text(self, elements: list, parts: list[str]) -> None:
        for element in elements:
            if not isinstance(element, dict):
                continue
            paragraph = element.get("paragraph")
            if paragraph:
                for run in paragraph.get("elements", []):
                    text_run = run.get("textRun") if isinstance(run, dict) else None
                    if text_run:
                        parts.append(text_run.get("content", ""))
                parts.append("\n")
            table = element.get("table")
            if table:
                for row in table.get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        self._collect_text(cell.get("content", []), parts)
