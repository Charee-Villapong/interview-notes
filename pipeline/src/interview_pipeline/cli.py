"""コマンドライン入口。

使い方: ``python -m interview_pipeline <command>``
データ領域は環境変数 ``INTERVIEW_DATA_DIR`` で受け取る
（コードに実値を書かない）。
STT のモデル名は ``INTERVIEW_STT_MODEL`` で受け取る（未設定なら
エンジン未接続として扱う）。
転送プル（``pull``）の接続情報は ``INTERVIEW_MAC_SSH_COMMAND`` と
``INTERVIEW_MAC_APP_DIR`` で受け取る。
要約の push 先は ``INTERVIEW_NOTES_REPO``（``owner/repo`` 形式の
非公開リポジトリ）で受け取る。gh コマンドのパスは
``INTERVIEW_GH_COMMAND`` で上書きできる（未設定なら ``gh``）。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from interview_pipeline.errors import MacFetchError, NotePublishError
from interview_pipeline.guards import (
    execute_vm_deletion,
    mac_deletion_eligible,
)
from interview_pipeline.layout import (
    EVENTS_FILENAME,
    INBOX_DIRNAME,
    LOG_DIRNAME,
    MANIFEST_FILENAME,
    NOTE_FILENAME,
    SESSIONS_FILENAME,
    STAGING_DIRNAME,
    STATE_DIRNAME,
    WORK_DIRNAME,
)
from interview_pipeline.manifest import Manifest
from interview_pipeline.publish import GhGitHubClient, publish_note
from interview_pipeline.pull import SshMacSource, pull_pending
from interview_pipeline.receive import verify_and_promote
from interview_pipeline.runner import EventLog, run_once
from interview_pipeline.saving import save_note
from interview_pipeline.states import ERROR_KIND_NOTE_PUBLISH_FAILED, State
from interview_pipeline.store import SessionStore
from interview_pipeline.transcribe import (
    FasterWhisperTranscriber,
    UnavailableTranscriber,
)


def _data_dir() -> Path:
    value = os.environ.get("INTERVIEW_DATA_DIR")
    if not value:
        raise SystemExit("INTERVIEW_DATA_DIR is not set")
    return Path(value)


def _notes_repo() -> str:
    value = os.environ.get("INTERVIEW_NOTES_REPO")
    if not value:
        raise SystemExit("INTERVIEW_NOTES_REPO is not set")
    return value


def _build_github_client():
    """GitHub クライアント生成の集約点（テストでの差し替え点）。"""
    return GhGitHubClient(command=os.environ.get("INTERVIEW_GH_COMMAND") or "gh")


def _store(data_dir: Path) -> SessionStore:
    return SessionStore(data_dir / STATE_DIRNAME / SESSIONS_FILENAME)


def _build_transcriber():
    """STTエンジン生成の集約点（テストでの差し替え点。SPEC V1契約）。

    環境変数 ``INTERVIEW_STT_MODEL`` が設定されていればそのモデル名で
    実エンジンを返し、未設定・空なら未接続エンジンを返す。
    """
    model_name = os.environ.get("INTERVIEW_STT_MODEL")
    if not model_name:
        return UnavailableTranscriber()
    return FasterWhisperTranscriber(model_name=model_name)


def _build_mac_source():
    """Mac ソース生成の集約点（テストでの差し替え点。SPEC PULL契約）。

    環境変数 ``INTERVIEW_MAC_SSH_COMMAND``（VM→Mac の SSH 実行
    コマンドへのパス）と ``INTERVIEW_MAC_APP_DIR``（Mac 側アプリ
    データ領域のパス）で接続情報を与える。いずれか未設定・空なら
    明確なエラーで終了する（黙って成功扱いにしない）。
    """
    ssh_command = os.environ.get("INTERVIEW_MAC_SSH_COMMAND")
    if not ssh_command:
        raise SystemExit("INTERVIEW_MAC_SSH_COMMAND is not set")
    app_dir = os.environ.get("INTERVIEW_MAC_APP_DIR")
    if not app_dir:
        raise SystemExit("INTERVIEW_MAC_APP_DIR is not set")
    return SshMacSource(ssh_command, app_dir)


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

    subparsers.add_parser("pull")

    subparsers.add_parser("run")

    subparsers.add_parser("status")

    p_save = subparsers.add_parser("save-note")
    p_save.add_argument("session_id")

    p_publish = subparsers.add_parser("publish-note")
    p_publish.add_argument("session_id")

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

    if args.command == "pull":
        source = _build_mac_source()
        try:
            report = pull_pending(
                store,
                source,
                staging_root=data_dir / STAGING_DIRNAME,
                inbox_root=data_dir / INBOX_DIRNAME,
                event_log=EventLog(data_dir / LOG_DIRNAME / EVENTS_FILENAME),
            )
        except MacFetchError:
            # セッション記録自体を取得できない全体失敗。種別のみを出す。
            print("failed: mac_fetch_failed")
            return 1
        print(
            f"pulled={report.pulled} failed={report.failed} "
            f"skipped={report.skipped}"
        )
        return 0 if not report.failed else 1

    if args.command == "run":
        report = run_once(
            store,
            staging_root=data_dir / STAGING_DIRNAME,
            inbox_root=data_dir / INBOX_DIRNAME,
            work_root=data_dir / WORK_DIRNAME,
            transcriber=_build_transcriber(),
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

    if args.command == "save-note":
        note_path = data_dir / WORK_DIRNAME / args.session_id / NOTE_FILENAME
        saved = save_note(store, args.session_id, note_path)
        print(f"saved {saved}")
        return 0

    if args.command == "publish-note":
        note_path = data_dir / WORK_DIRNAME / args.session_id / NOTE_FILENAME
        try:
            url = publish_note(
                store,
                args.session_id,
                note_path,
                notes_repo=_notes_repo(),
                github=_build_github_client(),
            )
        except NotePublishError:
            # 失敗はイベントとして記録し、種別のみを出す（silent failure 禁止）。
            EventLog(data_dir / LOG_DIRNAME / EVENTS_FILENAME).record(
                args.session_id,
                State.DRAFT_SAVED,
                error_kind=ERROR_KIND_NOTE_PUBLISH_FAILED,
            )
            print("failed: note_publish_failed")
            return 1
        print(url)
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
