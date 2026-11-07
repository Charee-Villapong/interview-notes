"""コマンドライン入口。

使い方: ``python -m interview_pipeline <command>``
データ領域は環境変数 ``INTERVIEW_DATA_DIR``、保存先は
``INTERVIEW_NOTES_DIR`` で受け取る（コードに実値を書かない）。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from interview_pipeline.guards import (
    execute_vm_deletion,
    mac_deletion_eligible,
)
from interview_pipeline.layout import (
    EVENTS_FILENAME,
    INBOX_DIRNAME,
    LOG_DIRNAME,
    MANIFEST_FILENAME,
    SESSIONS_FILENAME,
    STAGING_DIRNAME,
    STATE_DIRNAME,
    WORK_DIRNAME,
)
from interview_pipeline.manifest import Manifest
from interview_pipeline.receive import verify_and_promote
from interview_pipeline.runner import EventLog, run_once
from interview_pipeline.states import State
from interview_pipeline.store import SessionStore
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

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
