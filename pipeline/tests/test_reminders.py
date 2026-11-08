"""リマインド判定（reminders）のテスト。

根拠: pipeline/SPEC.md「P2 追加契約」の ``reminders.py`` 契約と
docs/手順書.md「Google連携（P2）／リマインド」。

- 種別は eve（前日）／day_morning（当日・午前）／day_evening（当日・午後）。
- 面談日が翌日ならスロット不問で eve。当日は午前→day_morning、
  午後→day_evening。それ以外（昨日・明後日など）は何も出さない。
- 送信済みの種別は再び出ない。記録は ``mark_reminded`` で行い
  （check では記録しない二段構え）、レジストリへ永続化される。
- ``unfilled_sections`` は準備セクション（企業研究・想定問答・聞きたいこと）
  のうち本文が空または「（未記入）」のもの、見出し自体が無いものも
  未記入として返す。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
CREATED = datetime(2026, 11, 8, 9, 0, tzinfo=JST)
NOW_MORNING = datetime(2026, 11, 10, 9, 0, tzinfo=JST)
NOW_EVENING = datetime(2026, 11, 10, 18, 0, tzinfo=JST)

FILLED_NOTE = (
    "企業研究\n"
    "ダミー社の事業内容を調べた。\n"
    "想定問答\n"
    "ダミーの回答を用意した。\n"
    "聞きたいこと\n"
    "ダミーの質問を用意した。\n"
)


def _record(**overrides):
    from interview_pipeline.interviews import InterviewRecord

    base = {
        "event_id": "evt-dummy-1",
        "session_id": "2026-11-11_01",
        "date": "2026-11-11",
        "start": datetime(2026, 11, 11, 10, 0, tzinfo=JST),
        "end": datetime(2026, 11, 11, 11, 0, tzinfo=JST),
        "title": "面接（ダミー社・担当者名）",
        "company": "ダミー社",
        "format": "Google Meet",
        "meeting_url": "https://meet.google.com/dummy-example",
        "docs_doc_id": "dummy-doc-1",
        "docs_url": "https://example.com/docs/dummy-doc-1",
        "sheet_row": 1,
        "note_error": None,
        "sheet_error": None,
        "reminders": {},
        "created_at": CREATED,
        "updated_at": CREATED,
    }
    base.update(overrides)
    return InterviewRecord(**base)


def _registry(tmp_path, records, name="interviews.json"):
    from interview_pipeline.interviews import InterviewRegistry

    registry = InterviewRegistry(tmp_path / name)
    for record in records:
        registry.upsert(record)
    registry.save()
    return registry


def _due(registry, now, slot, note_text=""):
    from interview_pipeline.reminders import due_reminders

    record = registry.all()[0]
    # note_texts のキーが event_id / session_id のどちらでも拾えるよう両方入れる。
    note_texts = {record.event_id: note_text, record.session_id: note_text}
    return due_reminders(registry, now, slot, note_texts=note_texts)


def _kinds(reminders):
    return [r.kind for r in reminders]


# ---------------------------------------------------------------------------
# due_reminders: 日付とスロットの判定
# ---------------------------------------------------------------------------


def test_due_eve_for_tomorrow_morning_slot(tmp_path):
    """翌日の面談は、午前スロットで eve が出る。"""
    registry = _registry(tmp_path, [_record(date="2026-11-11")])

    reminders = _due(registry, NOW_MORNING, "morning", FILLED_NOTE)

    assert _kinds(reminders) == ["eve"]
    assert reminders[0].event_id == "evt-dummy-1"
    assert reminders[0].session_id == "2026-11-11_01"
    assert isinstance(reminders[0].text, str) and reminders[0].text


def test_due_eve_for_tomorrow_evening_slot(tmp_path):
    """翌日の面談は、午後スロットでも（未送信なら）eve が出る。"""
    registry = _registry(tmp_path, [_record(date="2026-11-11")])

    reminders = _due(registry, NOW_EVENING, "evening", FILLED_NOTE)

    assert _kinds(reminders) == ["eve"]


def test_due_eve_text_includes_unfilled_sections(tmp_path):
    """eve の本文には準備セクションの未記入項目が含まれる。"""
    registry = _registry(tmp_path, [_record(date="2026-11-11")])
    note = (
        "企業研究\n（未記入）\n"
        "想定問答\nダミーの回答を用意した。\n"
        "聞きたいこと\n（未記入）\n"
    )

    reminders = _due(registry, NOW_MORNING, "morning", note)

    assert _kinds(reminders) == ["eve"]
    assert "企業研究" in reminders[0].text
    assert "聞きたいこと" in reminders[0].text


def test_due_day_morning_for_today_morning_slot(tmp_path):
    """当日の面談は、午前スロットで day_morning が出る。"""
    registry = _registry(
        tmp_path,
        [_record(date="2026-11-10", session_id="2026-11-10_01")],
    )

    reminders = _due(registry, NOW_MORNING, "morning", FILLED_NOTE)

    assert _kinds(reminders) == ["day_morning"]


def test_due_day_evening_for_today_evening_slot(tmp_path):
    """当日の面談は、午後スロットで day_evening が出る。"""
    registry = _registry(
        tmp_path,
        [_record(date="2026-11-10", session_id="2026-11-10_01")],
    )

    reminders = _due(registry, NOW_EVENING, "evening", FILLED_NOTE)

    assert _kinds(reminders) == ["day_evening"]


def test_due_nothing_for_yesterday_and_day_after_tomorrow(tmp_path):
    """昨日・明後日の予定はどのスロットでも出ない。"""
    from interview_pipeline.reminders import due_reminders

    registry = _registry(
        tmp_path,
        [
            _record(
                event_id="evt-dummy-yesterday",
                session_id="2026-11-09_01",
                date="2026-11-09",
            ),
            _record(
                event_id="evt-dummy-later",
                session_id="2026-11-12_01",
                date="2026-11-12",
            ),
        ],
    )

    for slot, now in (("morning", NOW_MORNING), ("evening", NOW_EVENING)):
        assert due_reminders(registry, now, slot, note_texts={}) == []


# ---------------------------------------------------------------------------
# due_reminders: 送信済みの再送防止と mark_reminded
# ---------------------------------------------------------------------------


def test_due_skips_already_sent_kind(tmp_path):
    """送信済みの種別は再び出ない。"""
    registry = _registry(
        tmp_path,
        [
            _record(
                date="2026-11-11",
                reminders={"eve": "2026-11-10T09:00:00+09:00"},
            )
        ],
    )

    assert _due(registry, NOW_MORNING, "morning", FILLED_NOTE) == []
    assert _due(registry, NOW_EVENING, "evening", FILLED_NOTE) == []


def test_due_skips_already_sent_day_morning(tmp_path):
    """当日の種別も、送信済みなら同じスロットで再び出ない。"""
    registry = _registry(
        tmp_path,
        [
            _record(
                date="2026-11-10",
                session_id="2026-11-10_01",
                reminders={"day_morning": "2026-11-10T09:00:00+09:00"},
            )
        ],
    )

    assert _due(registry, NOW_MORNING, "morning", FILLED_NOTE) == []


def test_mark_reminded_persists_and_suppresses(tmp_path):
    """mark_reminded の記録は永続化され、以後その種別は出なくなる。"""
    from interview_pipeline.reminders import mark_reminded

    registry = _registry(tmp_path, [_record(date="2026-11-11")])
    assert _kinds(_due(registry, NOW_MORNING, "morning", FILLED_NOTE)) == ["eve"]

    mark_reminded(
        registry,
        "evt-dummy-1",
        "eve",
        datetime(2026, 11, 10, 9, 5, tzinfo=JST),
    )

    record = registry.get_by_event("evt-dummy-1")
    assert "eve" in record.reminders
    assert record.reminders["eve"]

    reloaded = _registry(tmp_path, [], name="interviews.json")
    reloaded_record = reloaded.get_by_event("evt-dummy-1")
    assert "eve" in reloaded_record.reminders
    assert _due(reloaded, NOW_MORNING, "morning", FILLED_NOTE) == []


# ---------------------------------------------------------------------------
# unfilled_sections
# ---------------------------------------------------------------------------


def test_unfilled_sections_detects_placeholder():
    """「（未記入）」のセクションが検出され、記入済みは含まれない。"""
    from interview_pipeline.reminders import unfilled_sections

    note = (
        "企業研究\n（未記入）\n"
        "想定問答\nダミーの回答を用意した。\n"
        "聞きたいこと\n（未記入）\n"
    )

    result = unfilled_sections(note)
    assert set(result) == {"企業研究", "聞きたいこと"}
    assert "想定問答" not in result


def test_unfilled_sections_all_filled_returns_empty():
    """全セクション記入済みなら空になる。"""
    from interview_pipeline.reminders import unfilled_sections

    assert unfilled_sections(FILLED_NOTE) == []


def test_unfilled_sections_missing_heading_counts_as_unfilled():
    """見出し自体が無いセクションは未記入扱いになる。"""
    from interview_pipeline.reminders import unfilled_sections

    note = "企業研究\n調べた。\n想定問答\n用意した。\n"  # 聞きたいこと が無い

    assert unfilled_sections(note) == ["聞きたいこと"]


def test_unfilled_sections_empty_body_counts_as_unfilled():
    """見出しはあるが本文が空のセクションも未記入扱いになる。"""
    from interview_pipeline.reminders import unfilled_sections

    note = (
        "企業研究\n"
        "想定問答\nダミーの回答を用意した。\n"
        "聞きたいこと\nダミーの質問を用意した。\n"
    )

    assert unfilled_sections(note) == ["企業研究"]
