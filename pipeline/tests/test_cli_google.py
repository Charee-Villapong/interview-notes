"""CLI 追加コマンド（P2: remind-check / remind-mark）のテスト。

根拠: pipeline/SPEC.md「P2 追加契約」の CLI 追加契約。

- ``remind-check --slot morning|evening`` は送信対象を JSON で表示し、
  記録はしない（送信後に ``remind-mark`` で記録する二段構え）。
- ``remind-mark <event_id> <kind>`` は送信済みをレジストリへ記録する。
- ゲートウェイは ``cli._build_gateway`` の差し替えでフェイクにする
  （SPEC が定めるテストでの差し替え点）。実接続はしない。
- レジストリは ``$INTERVIEW_DATA_DIR/state/interviews.json``。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone

import pytest

JST = timezone(timedelta(hours=9))

FILLED_NOTE = (
    "企業研究\nダミー社の事業内容を調べた。\n"
    "想定問答\nダミーの回答を用意した。\n"
    "聞きたいこと\nダミーの質問を用意した。\n"
)


class FakeGateway:
    """CLI テスト用のゲートウェイフェイク。ノート本文だけを返す。"""

    def __init__(self, note_text=FILLED_NOTE):
        self.note_text = note_text

    def list_events(self, calendar_id, time_min, time_max):
        return []

    def copy_template(self, template_id, name, folder_id=None):
        return "dummy-doc-1", "https://example.com/docs/dummy-doc-1"

    def replace_note_fields(self, doc_id, fields):
        return None

    def read_note_text(self, doc_id):
        return self.note_text


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """データ領域と Google 設定を一時ディレクトリへ向けた環境を返す。"""
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    config = {
        "cli_path": "/dummy/path/hatch_gws_cli",
        "calendar_ids": ["dummy-calendar-1"],
        "timezone": "Asia/Tokyo",
        "primary_keyword": "面接",
        "secondary_keywords": ["面談"],
        "invited_keywords": ["面接", "面談"],
        "docs_template_id": "dummy-template-id",
        "window_past_days": 7,
        "window_future_days": 30,
    }
    config_path = tmp_path / "google_config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("INTERVIEW_DATA_DIR", str(data_dir))
    monkeypatch.setenv("INTERVIEW_GOOGLE_CONFIG", str(config_path))
    return data_dir


def _seed_today_record(data_dir):
    """当日の面談レコードを1件、レジストリへ種まきする。"""
    from interview_pipeline.interviews import InterviewRecord, InterviewRegistry

    today = date.today()
    session_id = f"{today.isoformat()}_01"
    start = datetime.combine(today, time(10, 0), tzinfo=JST)
    registry = InterviewRegistry(data_dir / "state" / "interviews.json")
    registry.upsert(
        InterviewRecord(
            event_id="evt-dummy-cli-1",
            session_id=session_id,
            date=today.isoformat(),
            start=start,
            end=start + timedelta(hours=1),
            title="面接（ダミー社・担当者名）",
            company="ダミー社",
            format="Google Meet",
            meeting_url="https://meet.google.com/dummy-example",
            docs_doc_id="dummy-doc-1",
            docs_url="https://example.com/docs/dummy-doc-1",
            note_error=None,
            reminders={},
            created_at=start,
            updated_at=start,
        )
    )
    registry.save()
    return session_id


def _install_fake_gateway(monkeypatch):
    import interview_pipeline.cli as cli

    fake = FakeGateway()
    monkeypatch.setattr(cli, "_build_gateway", lambda config: fake)
    return cli


def _reminders_in_registry(data_dir):
    from interview_pipeline.interviews import InterviewRegistry

    registry = InterviewRegistry(data_dir / "state" / "interviews.json")
    return registry.get_by_event("evt-dummy-cli-1").reminders


def test_remind_check_outputs_json_and_records_nothing(cli_env, monkeypatch, capsys):
    """remind-check は対象を JSON で出し、レジストリへ記録しない。"""
    session_id = _seed_today_record(cli_env)
    cli = _install_fake_gateway(monkeypatch)

    assert cli.main(["remind-check", "--slot", "morning"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert len(payload) == 1
    item = payload[0]
    assert item["event_id"] == "evt-dummy-cli-1"
    assert item["session_id"] == session_id
    assert item["kind"] == "day_morning"
    assert isinstance(item["text"], str) and item["text"]

    # check の段階では送信済みとして記録されていない。
    assert _reminders_in_registry(cli_env) == {}


def test_remind_check_with_no_due_outputs_empty_json_list(cli_env, monkeypatch, capsys):
    """対象が無ければ空の JSON リストを出す。"""
    cli = _install_fake_gateway(monkeypatch)

    assert cli.main(["remind-check", "--slot", "morning"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_remind_mark_records_sent_reminder(cli_env, monkeypatch, capsys):
    """remind-mark で送信済みが記録され、以後 remind-check に出なくなる。"""
    _seed_today_record(cli_env)
    cli = _install_fake_gateway(monkeypatch)

    assert cli.main(["remind-mark", "evt-dummy-cli-1", "day_morning"]) == 0
    capsys.readouterr()

    reminders = _reminders_in_registry(cli_env)
    assert "day_morning" in reminders
    assert reminders["day_morning"]

    assert cli.main(["remind-check", "--slot", "morning"]) == 0
    assert json.loads(capsys.readouterr().out) == []
