"""コマンドライン入口。

使い方: ``python -m interview_pipeline <command>``
データ領域は環境変数 ``INTERVIEW_DATA_DIR``、保存先は
``INTERVIEW_NOTES_DIR`` で受け取る（コードに実値を書かない）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from interview_pipeline.gateway import CliGoogleGateway
from interview_pipeline.google_config import load_google_config
from interview_pipeline.guards import (
    execute_vm_deletion,
    mac_deletion_eligible,
)
from interview_pipeline.interviews import InterviewRegistry, is_interview_event
from interview_pipeline.layout import (
    EVENTS_FILENAME,
    INBOX_DIRNAME,
    INTERVIEWS_FILENAME,
    LOG_DIRNAME,
    MANIFEST_FILENAME,
    SESSIONS_FILENAME,
    STAGING_DIRNAME,
    STATE_DIRNAME,
    WORK_DIRNAME,
)
from interview_pipeline.manifest import Manifest
from interview_pipeline.receive import verify_and_promote
from interview_pipeline.reminders import due_reminders, mark_reminded
from interview_pipeline.runner import EventLog, run_once
from interview_pipeline.states import State
from interview_pipeline.store import SessionStore
from interview_pipeline.sync import detection_window, sync_interviews
from interview_pipeline.transcribe import UnavailableTranscriber


def _data_dir() -> Path:
    value = os.environ.get("INTERVIEW_DATA_DIR")
    if not value:
        raise SystemExit("INTERVIEW_DATA_DIR is not set")
    return Path(value)


def _notes_dir() -> Path:
    value = os.environ.get("INTERVIEW_NOTES_DIR")
    if not value:
        raise SystemExit("INTERVIEW_NOTES_DIR is not set")
    return Path(value)


def _store(data_dir: Path) -> SessionStore:
    return SessionStore(data_dir / STATE_DIRNAME / SESSIONS_FILENAME)


def _google_config():
    value = os.environ.get("INTERVIEW_GOOGLE_CONFIG")
    if not value:
        raise SystemExit("INTERVIEW_GOOGLE_CONFIG is not set")
    return load_google_config(value)


def _build_gateway(config):
    """ゲートウェイ生成の集約点（テストでの差し替え点。SPEC P2契約）。"""
    return CliGoogleGateway(config)


def _registry(data_dir: Path) -> InterviewRegistry:
    return InterviewRegistry(data_dir / STATE_DIRNAME / INTERVIEWS_FILENAME)


def _now_for_mark() -> datetime:
    """送信済み記録用の現在時刻（設定タイムゾーン優先、無ければ UTC）。"""
    value = os.environ.get("INTERVIEW_GOOGLE_CONFIG")
    if value:
        try:
            return datetime.now(ZoneInfo(load_google_config(value).timezone))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _parse_datetime(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="interview_pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_register = subparsers.add_parser("register")
    p_register.add_argument("session_id")
    p_register.add_argument("--created-at", default=None)

    p_receive = subparsers.add_parser("receive")
    p_receive.add_argument("session_id")

    subparsers.add_parser("run")

    subparsers.add_parser("status")

    p_final = subparsers.add_parser("mark-final")
    p_final.add_argument("session_id")

    p_delete = subparsers.add_parser("delete-vm-copy")
    p_delete.add_argument("session_id")

    p_mac = subparsers.add_parser("mac-eligible")
    p_mac.add_argument("session_id")

    subparsers.add_parser("detect")

    subparsers.add_parser("sync")

    subparsers.add_parser("interviews")

    p_check = subparsers.add_parser("remind-check")
    p_check.add_argument("--slot", choices=["morning", "evening"], required=True)
    p_check.add_argument("--dry-run", action="store_true")

    p_mark = subparsers.add_parser("remind-mark")
    p_mark.add_argument("event_id")
    p_mark.add_argument("kind")

    args = parser.parse_args(argv)
    data_dir = _data_dir()
    store = _store(data_dir)

    if args.command == "register":
        session = store.register(args.session_id, _parse_datetime(args.created_at))
        print(f"{session.session_id} {session.state.value}")
        return 0

    if args.command == "receive":
        staging_dir = data_dir / STAGING_DIRNAME / args.session_id
        manifest = Manifest.load(staging_dir / MANIFEST_FILENAME)
        result = verify_and_promote(
            store, manifest, staging_dir, data_dir / INBOX_DIRNAME / args.session_id
        )
        print("ok" if result.ok else f"failed: {result.reason}")
        return 0 if result.ok else 1

    if args.command == "run":
        report = run_once(
            store,
            staging_root=data_dir / STAGING_DIRNAME,
            inbox_root=data_dir / INBOX_DIRNAME,
            work_root=data_dir / WORK_DIRNAME,
            transcriber=UnavailableTranscriber(),
            event_log=EventLog(data_dir / LOG_DIRNAME / EVENTS_FILENAME),
        )
        print(
            f"processed={report.processed} failed={report.failed} "
            f"skipped={report.skipped}"
        )
        return 0

    if args.command == "status":
        for session in store.all():
            print(f"{session.session_id} {session.state.value}")
        return 0

    if args.command == "mark-final":
        store.transition(args.session_id, State.FINAL_SAVED)
        print(f"{args.session_id} final_saved")
        return 0

    if args.command == "delete-vm-copy":
        deleted = execute_vm_deletion(store, args.session_id, data_dir / INBOX_DIRNAME)
        print(f"deleted {deleted}")
        return 0

    if args.command == "mac-eligible":
        session = store.get(args.session_id)
        eligible = mac_deletion_eligible(session)
        print("eligible" if eligible else "not-eligible")
        return 0 if eligible else 1

    if args.command == "detect":
        config = _google_config()
        gateway = _build_gateway(config)
        now = datetime.now(ZoneInfo(config.timezone))
        time_min, time_max = detection_window(config, now)
        for calendar_id in config.calendar_ids:
            for event in gateway.list_events(calendar_id, time_min, time_max):
                basis = is_interview_event(event, config)
                if basis is not None:
                    print(
                        f"{event.start.date().isoformat()} {basis} "
                        f"{event.event_id} {event.title}"
                    )
        return 0

    if args.command == "sync":
        config = _google_config()
        gateway = _build_gateway(config)
        now = datetime.now(ZoneInfo(config.timezone))
        report = sync_interviews(
            _registry(data_dir),
            gateway,
            config,
            store=store,
            now=now,
            event_log=EventLog(data_dir / LOG_DIRNAME / EVENTS_FILENAME),
        )
        print(
            f"detected={report.detected} notes_created={report.notes_created} "
            f"rows_added={report.rows_added} failed={report.failed}"
        )
        return 0 if not report.failed else 1

    if args.command == "interviews":
        for record in _registry(data_dir).all():
            note = "ok" if record.docs_doc_id else "pending"
            print(
                f"{record.session_id} {record.date} note={note} "
                f"sheet_row={record.sheet_row}"
            )
        return 0

    if args.command == "remind-check":
        config = _google_config()
        gateway = _build_gateway(config)
        registry = _registry(data_dir)
        now = datetime.now(ZoneInfo(config.timezone))
        tomorrow = (now.date() + timedelta(days=1)).isoformat()
        note_texts: dict[str, str] = {}
        for record in registry.all():
            if record.date == tomorrow and record.docs_doc_id:
                try:
                    note_texts[record.event_id] = gateway.read_note_text(
                        record.docs_doc_id
                    )
                except Exception:
                    note_texts[record.event_id] = ""
        due = due_reminders(registry, now, args.slot, note_texts=note_texts)
        print(
            json.dumps(
                [
                    {
                        "event_id": item.event_id,
                        "session_id": item.session_id,
                        "kind": item.kind,
                        "text": item.text,
                    }
                    for item in due
                ],
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "remind-mark":
        record = mark_reminded(
            _registry(data_dir), args.event_id, args.kind, _now_for_mark()
        )
        print(f"marked {record.event_id} {args.kind}")
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
