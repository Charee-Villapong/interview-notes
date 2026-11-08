"""Google ゲートウェイ（gateway）のテスト。

根拠: pipeline/SPEC.md「P2 追加契約」の ``gateway.py`` 契約。

- 外部（Google）へのアクセスはすべてゲートウェイを通す。
- 実装 ``CliGoogleGateway`` は失敗時に ``GoogleGatewayError`` を送出する。
- テストは実接続しない（存在しない CLI パスで失敗系だけを確認する）。
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest

JST = timezone(timedelta(hours=9))
T_START = datetime(2026, 11, 10, 10, 0, tzinfo=JST)
T_END = datetime(2026, 11, 10, 11, 0, tzinfo=JST)

#: GoogleGatewayError を探す候補モジュール。
_ERROR_SEARCH_MODULES = (
    "interview_pipeline.gateway",
    "interview_pipeline.errors",
    "interview_pipeline.google_config",
    "interview_pipeline",
)


def _gateway_error_type():
    for module_name in _ERROR_SEARCH_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        cls = getattr(module, "GoogleGatewayError", None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            return cls
    raise AssertionError("GoogleGatewayError がパッケージ内に見つかりません")


def _config_with_cli_path(tmp_path, cli_path: str):
    from interview_pipeline.google_config import load_google_config

    data = {
        "cli_path": cli_path,
        "calendar_ids": ["dummy-calendar-1"],
        "sheets_spreadsheet_id": "dummy-spreadsheet-id",
        "docs_template_id": "dummy-template-id",
    }
    path = tmp_path / "google_config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return load_google_config(path)


def test_calendar_event_stores_fields():
    """CalendarEvent は契約どおりのフィールドを保持する。"""
    from interview_pipeline.gateway import CalendarEvent

    event = CalendarEvent(
        event_id="evt-dummy-1",
        calendar_id="dummy-calendar-1",
        title="面接（ダミー社・担当者名）",
        start=T_START,
        end=T_END,
        organizer_email="organizer@example.com",
        organizer_self=True,
        location="ダミー会議室",
        description="ダミーの説明文",
        meeting_url="https://meet.google.com/dummy-example",
        cancelled=False,
    )

    assert event.event_id == "evt-dummy-1"
    assert event.calendar_id == "dummy-calendar-1"
    assert event.title == "面接（ダミー社・担当者名）"
    assert event.start == T_START
    assert event.end == T_END
    assert event.organizer_email == "organizer@example.com"
    assert event.organizer_self is True
    assert event.location == "ダミー会議室"
    assert event.description == "ダミーの説明文"
    assert event.meeting_url == "https://meet.google.com/dummy-example"
    assert event.cancelled is False


def test_cli_gateway_exposes_protocol_methods(tmp_path):
    """CliGoogleGateway はプロトコルの全メソッドを持つ（生成だけでは接続しない）。"""
    from interview_pipeline.gateway import CliGoogleGateway

    gateway = CliGoogleGateway(_config_with_cli_path(tmp_path, "/dummy/cli"))

    for name in (
        "list_events",
        "copy_template",
        "replace_note_fields",
        "read_note_text",
        "append_sheet_row",
        "sheet_row_exists",
    ):
        assert callable(getattr(gateway, name)), name


def test_cli_gateway_list_events_failure_raises_gateway_error(tmp_path):
    """CLI が存在しない場合、list_events は GoogleGatewayError で失敗する。"""
    from interview_pipeline.gateway import CliGoogleGateway

    gateway = CliGoogleGateway(
        _config_with_cli_path(tmp_path, "/nonexistent/dummy-cli-path")
    )
    with pytest.raises(_gateway_error_type()):
        gateway.list_events(
            "dummy-calendar-1",
            datetime(2026, 11, 1, 0, 0, tzinfo=JST),
            datetime(2026, 12, 1, 0, 0, tzinfo=JST),
        )


def test_cli_gateway_append_failure_raises_gateway_error(tmp_path):
    """書き込み系の失敗も GoogleGatewayError で送出される。"""
    from interview_pipeline.gateway import CliGoogleGateway

    gateway = CliGoogleGateway(
        _config_with_cli_path(tmp_path, "/nonexistent/dummy-cli-path")
    )
    with pytest.raises(_gateway_error_type()):
        gateway.append_sheet_row(
            "dummy-spreadsheet-id",
            "面談一覧",
            ["2026-11-10", "ダミー社", "", "", "", "https://example.com/dummy"],
        )
