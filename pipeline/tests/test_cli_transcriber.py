"""CLI の STT エンジン生成（`_build_transcriber`）と run 配線のテスト。

根拠: pipeline/SPEC.md「V1 追加契約（STTエンジン接続）」。
- 環境変数 `INTERVIEW_STT_MODEL` 未設定・空なら `UnavailableTranscriber`
- 設定されていれば、そのモデル名の `FasterWhisperTranscriber`
  （生成時点ではモデルを読み込まないため、実エンジンなしでも生成できる）
- `run` は `_build_transcriber()` の戻り値で処理する
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

SID = "2026-11-09_01"


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """データ領域を一時ディレクトリへ向け、STTモデル指定は外した環境。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("INTERVIEW_DATA_DIR", str(data_dir))
    monkeypatch.delenv("INTERVIEW_STT_MODEL", raising=False)
    return data_dir


def test_build_transcriber_unset_env_returns_unavailable(cli_env):
    """INTERVIEW_STT_MODEL 未設定なら未接続エンジン（従来どおり）。"""
    from interview_pipeline.cli import _build_transcriber
    from interview_pipeline.transcribe import UnavailableTranscriber

    assert isinstance(_build_transcriber(), UnavailableTranscriber)


def test_build_transcriber_empty_env_returns_unavailable(cli_env, monkeypatch):
    """INTERVIEW_STT_MODEL が空文字列でも未接続エンジン。"""
    from interview_pipeline.cli import _build_transcriber
    from interview_pipeline.transcribe import UnavailableTranscriber

    monkeypatch.setenv("INTERVIEW_STT_MODEL", "")
    assert isinstance(_build_transcriber(), UnavailableTranscriber)


def test_build_transcriber_set_env_returns_faster_whisper(cli_env, monkeypatch):
    """INTERVIEW_STT_MODEL があれば、その名前の実エンジンを返す。"""
    from interview_pipeline.cli import _build_transcriber
    from interview_pipeline.transcribe import FasterWhisperTranscriber

    monkeypatch.setenv("INTERVIEW_STT_MODEL", "dummy-model")
    transcriber = _build_transcriber()
    assert isinstance(transcriber, FasterWhisperTranscriber)
    assert transcriber.model_name == "dummy-model"


def _drive_to_received(data_dir: Path) -> None:
    """CLI と同じ場所のストアに、received 状態のセッションを用意する。"""
    from interview_pipeline.states import State
    from interview_pipeline.store import SessionStore

    store = SessionStore(data_dir / "state" / "sessions.json")
    store.register(SID, datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc))
    for step in ("transfer_pending", "transferring", "received"):
        store.transition(SID, State(step))


def test_run_uses_built_transcriber(cli_env, monkeypatch, capsys, make_transcriber):
    """run は _build_transcriber() の戻り値で文字起こしし、成果物を生成する。"""
    import interview_pipeline.cli as cli

    _drive_to_received(cli_env)
    inbox_dir = cli_env / "inbox" / SID
    inbox_dir.mkdir(parents=True, exist_ok=True)
    (inbox_dir / "system_track.m4a").write_bytes(b"dummy-audio")
    (inbox_dir / "mic_track.m4a").write_bytes(b"dummy-audio")

    monkeypatch.setattr(cli, "_build_transcriber", lambda: make_transcriber())

    assert cli.main(["run"]) == 0
    out = capsys.readouterr().out
    assert f"processed=['{SID}']" in out

    transcript = (cli_env / "work" / SID / "transcript.md").read_text(encoding="utf-8")
    assert "B: SYSTEM-DUMMY-TEXT" in transcript
    assert "A: MIC-DUMMY-TEXT" in transcript

    payload = json.loads(
        (cli_env / "state" / "sessions.json").read_text(encoding="utf-8")
    )
    sessions = payload["sessions"] if isinstance(payload, dict) else payload
    state = {s["session_id"]: s["state"] for s in sessions}[SID]
    assert state == "awaiting_editing"
