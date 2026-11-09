"""検知同期：カレンダー検知→レジストリ登録→Docsノート。

冪等に動作し、不足分だけを補う。失敗は成功扱いにせず、レジストリの
``note_error``（種別のみ）とイベントログに記録し、次回の同期で
再試行する。1件の失敗で他件の処理は止めない
（silent failure 禁止。正本 ``docs/状態管理.md``「通知・ログ」）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from interview_pipeline.gateway import GoogleGateway
from interview_pipeline.google_config import GoogleConfig
from interview_pipeline.interviews import (
    ERROR_KIND_DOCS_TEMPLATE_NOT_CONFIGURED,
    InterviewRecord,
    InterviewRegistry,
    extract_company,
    extract_format,
    is_interview_event,
)

__all__ = ["SyncReport", "detection_window", "sync_interviews"]

#: イベントログに記録する state／error_kind（内容は記録しない）。
EVENT_STATE_INTERVIEW = "interview_sync"
ERROR_KIND_NOTE_FAILED = "interview_note_failed"


@dataclass
class SyncReport:
    detected: list[str] = field(default_factory=list)
    notes_created: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def detection_window(config: GoogleConfig, now: datetime) -> tuple[datetime, datetime]:
    """検知ウィンドウ（設定タイムゾーンの暦日ベース）を返す。"""
    tz = ZoneInfo(config.timezone)
    local_now = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
    start_day = local_now.date() - timedelta(days=config.window_past_days)
    end_day = local_now.date() + timedelta(days=config.window_future_days)
    return (
        datetime.combine(start_day, time.min, tzinfo=tz),
        datetime.combine(end_day, time.max, tzinfo=tz),
    )


def _note_fields(record: InterviewRecord, tz: ZoneInfo) -> dict[str, str]:
    start = record.start.astimezone(tz)
    end = record.end.astimezone(tz)
    return {
        "日時": f"{record.date} {start:%H:%M}〜{end:%H:%M}",
        "企業": record.company,
        "形式": record.format,
        "会議URL": record.meeting_url,
    }


def _ensure_note(
    record: InterviewRecord,
    gateway: GoogleGateway,
    config: GoogleConfig,
    tz: ZoneInfo,
    report: SyncReport,
    event_log,
) -> None:
    """Docsノートの作成（または基本情報置換の再試行）を行う。"""
    if record.docs_doc_id and record.note_error is None:
        return  # 作成済みで異常なし。再置換はしない
    if not config.docs_template_id:
        record.note_error = ERROR_KIND_DOCS_TEMPLATE_NOT_CONFIGURED
        return
    try:
        created = False
        if not record.docs_doc_id:
            doc_id, url = gateway.copy_template(
                config.docs_template_id,
                f"{record.session_id} 面談ノート",
                config.docs_folder_id,
            )
            record.docs_doc_id = doc_id
            record.docs_url = url
            created = True
        gateway.replace_note_fields(record.docs_doc_id, _note_fields(record, tz))
    except Exception as exc:  # ゲートウェイ失敗は種別のみ記録する
        record.note_error = type(exc).__name__
        if event_log is not None:
            event_log.record(
                record.session_id, EVENT_STATE_INTERVIEW, ERROR_KIND_NOTE_FAILED
            )
        return
    record.note_error = None
    if created:
        report.notes_created.append(record.session_id)


def sync_interviews(
    registry: InterviewRegistry,
    gateway: GoogleGateway,
    config: GoogleConfig,
    *,
    store,
    now: datetime,
    event_log=None,
) -> SyncReport:
    """検知ウィンドウ内を走査し、検知した予定の登録・作成を行う。"""
    tz = ZoneInfo(config.timezone)
    time_min, time_max = detection_window(config, now)
    report = SyncReport()

    for calendar_id in config.calendar_ids:
        try:
            events = gateway.list_events(calendar_id, time_min, time_max)
        except Exception:
            # カレンダー単位の失敗は failed にカレンダーIDを入れて可視化する
            # （SPEC P2 決定事項 9）。他カレンダーの処理は続ける。
            report.failed.append(calendar_id)
            continue
        for event in events:
            if is_interview_event(event, config) is None:
                continue
            record = registry.get_by_event(event.event_id)
            if record is None:
                date_str = event.start.astimezone(tz).date().isoformat()
                record = InterviewRecord(
                    event_id=event.event_id,
                    session_id=registry.assign_session_id(date_str, store),
                    date=date_str,
                    start=event.start,
                    end=event.end,
                    title=event.title,
                    company=extract_company(event),
                    format=extract_format(event),
                    meeting_url=event.meeting_url,
                    created_at=now,
                    updated_at=now,
                )
                registry.upsert(record)
                report.detected.append(record.session_id)
                if event_log is not None:
                    event_log.record(record.session_id, "interview_detected")
            before = record.docs_doc_id
            _ensure_note(record, gateway, config, tz, report, event_log)
            if record.docs_doc_id != before or record.note_error:
                record.updated_at = now
            registry.save()
    return report
