"""特性テスト（現状固定）: C-058 ``cli.py`` / ``__main__.py``。

根拠: refactoring-candidates-2026-11-07.md の interview-notes
「テストが薄い箇所」（cli.py＋__main__.py はテストファイル自体が無く、
7コマンド・環境変数未設定時の SystemExit・日時パースの UTC 補完が
無検証）、pipeline/SPEC.md の CLI 契約、docs/状態管理.md
「データ領域と環境変数」。

既存テストで固定済みの範囲（本ファイルでは重複させない）:
test_characterization_layout.py が subprocess 経由で register の
stdout・receive の成功／失敗・run のログ配置を固定済み。

本ファイルが固定するのは残りの CLI 挙動:
- コマンド未指定／未知コマンドの終了コード（argparse の SystemExit 2）
- ``INTERVIEW_DATA_DIR`` 未設定時の SystemExit とメッセージ
- ``INTERVIEW_NOTES_DIR`` は現行の CLI コマンドが参照しないこと
- ``--created-at`` の解釈（省略時は現在時刻 UTC、naive は UTC 補完、
  aware はオフセット保持、日付のみは 00:00 UTC、不正値は ValueError）
- register の冪等性、status の空／複数件出力
- delete-vm-copy / mac-eligible の成功・拒否・終了コード
- receive のマニフェスト欠落・未知セッション時の例外
- run の空ストア／対象外セッション時の出力文字列
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SID = "2026-11-09_01"
SID2 = "2026-11-09_02"
CREATED = "2026-11-09T09:00:00+00:00"


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """データ領域と保存先を一時ディレクトリへ向けた環境を返す。"""
    data_dir = tmp_path / "data"
    notes_dir = tmp_path / "notes"
    data_dir.mkdir(parents=True, exist_ok=True)
    notes_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("INTERVIEW_DATA_DIR", str(data_dir))
    monkeypatch.setenv("INTERVIEW_NOTES_DIR", str(notes_dir))
    return data_dir, notes_dir


def _main(argv):
    from interview_pipeline.cli import main

    return main(argv)


def _sessions_payload(data_dir: Path) -> list[dict]:
    payload = json.loads(
        (data_dir / "state" / "sessions.json").read_text(encoding="utf-8")
    )
    return payload["sessions"] if isinstance(payload, dict) else payload


def _drive_cli_store(data_dir: Path, target: str) -> None:
    """CLI と同じ場所のストアを作り、セッションを目標状態まで進める。"""
    from datetime import datetime, timezone

    from interview_pipeline.states import State
    from interview_pipeline.store import SessionStore

    paths = {
        "draft_saved": [
            "transfer_pending",
            "transferring",
            "received",
            "processing",
            "awaiting_editing",
            "draft_saved",
        ],
        "final_saved": [
            "transfer_pending",
            "transferring",
            "received",
            "processing",
            "awaiting_editing",
            "draft_saved",
            "final_saved",
        ],
    }
    store = SessionStore(data_dir / "state" / "sessions.json")
    store.register(SID, datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc))
    for step in paths[target]:
        store.transition(SID, State(step))


# ---------------------------------------------------------------------------
# 引数・環境変数のエラー
# ---------------------------------------------------------------------------


def test_cli_no_command_exits_with_code_2(cli_env):
    """コマンド未指定は argparse の SystemExit（コード2）。"""
    with pytest.raises(SystemExit) as excinfo:
        _main([])
    assert excinfo.value.code == 2


def test_cli_unknown_command_exits_with_code_2(cli_env):
    """未知のコマンドも SystemExit（コード2）で拒否される。"""
    with pytest.raises(SystemExit) as excinfo:
        _main(["bogus"])
    assert excinfo.value.code == 2


def test_cli_missing_session_argument_exits_with_code_2(cli_env):
    """必須引数（session_id）の欠落も SystemExit（コード2）。"""
    with pytest.raises(SystemExit) as excinfo:
        _main(["register"])
    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["register", SID],
        ["receive", SID],
        ["run"],
        ["status"],
        ["delete-vm-copy", SID],
        ["mac-eligible", SID],
    ],
)
def test_cli_missing_data_dir_env_exits_with_message(cli_env, monkeypatch, argv):
    """INTERVIEW_DATA_DIR 未設定は全コマンド共通で SystemExit になる。

    メッセージ文字列そのものが現行の契約として固定する。
    """
    monkeypatch.delenv("INTERVIEW_DATA_DIR")
    with pytest.raises(SystemExit) as excinfo:
        _main(argv)
    assert excinfo.value.code == "INTERVIEW_DATA_DIR is not set"


def test_cli_commands_do_not_require_notes_dir_env(cli_env, monkeypatch, capsys):
    """現行挙動: INTERVIEW_NOTES_DIR 未設定でも CLI は動作する。

    SPEC は保存先を環境変数で与えると定めるが、現行の cli.py の
    コマンド群は ``_notes_dir()`` を呼ばず、この変数を参照しない。
    """
    monkeypatch.delenv("INTERVIEW_NOTES_DIR")
    assert _main(["register", SID, "--created-at", CREATED]) == 0
    assert _main(["status"]) == 0
    assert f"{SID} recorded" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# register / 日時パース
# ---------------------------------------------------------------------------


def test_cli_register_without_created_at_uses_current_utc(cli_env, capsys):
    """--created-at 省略時は現在時刻（UTC・tz付き）が記録される。"""
    data_dir, _ = cli_env
    assert _main(["register", SID]) == 0
    assert capsys.readouterr().out.strip() == f"{SID} recorded"
    (entry,) = _sessions_payload(data_dir)
    assert entry["state"] == "recorded"
    assert entry["created_at"].endswith("+00:00")


def test_cli_register_naive_datetime_is_completed_as_utc(cli_env):
    """tz なしの --created-at は UTC として補完される。"""
    data_dir, _ = cli_env
    assert _main(["register", SID, "--created-at", "2026-11-09T10:00:00"]) == 0
    (entry,) = _sessions_payload(data_dir)
    assert entry["created_at"] == "2026-11-09T10:00:00+00:00"


def test_cli_register_aware_datetime_keeps_offset(cli_env):
    """オフセット付きの --created-at はそのまま保持される（UTC化しない）。"""
    data_dir, _ = cli_env
    assert _main(["register", SID, "--created-at", "2026-11-09T10:00:00+09:00"]) == 0
    (entry,) = _sessions_payload(data_dir)
    assert entry["created_at"] == "2026-11-09T10:00:00+09:00"


def test_cli_register_date_only_is_midnight_utc(cli_env):
    """日付のみの --created-at はその日の 00:00 UTC になる。"""
    data_dir, _ = cli_env
    assert _main(["register", SID, "--created-at", "2026-11-09"]) == 0
    (entry,) = _sessions_payload(data_dir)
    assert entry["created_at"] == "2026-11-09T00:00:00+00:00"


def test_cli_register_invalid_datetime_raises_value_error(cli_env):
    """解釈できない --created-at は ValueError のまま伝播する。"""
    with pytest.raises(ValueError):
        _main(["register", SID, "--created-at", "not-a-date"])


def test_cli_register_is_idempotent_and_keeps_first_created_at(cli_env, capsys):
    """同じ ID の再 register は既存を返し、created_at は初回値のまま。"""
    data_dir, _ = cli_env
    assert _main(["register", SID, "--created-at", CREATED]) == 0
    assert _main(["register", SID, "--created-at", "2026-11-10T09:00:00"]) == 0
    capsys.readouterr()
    (entry,) = _sessions_payload(data_dir)
    assert entry["created_at"] == CREATED


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_cli_status_with_no_sessions_prints_nothing(cli_env, capsys):
    """セッションが無ければ status は無出力で正常終了する。"""
    assert _main(["status"]) == 0
    assert capsys.readouterr().out == ""


def test_cli_status_lists_sessions_in_registration_order(cli_env, capsys):
    """status は登録順に「<session_id> <state>」を1行ずつ出す。"""
    _main(["register", SID, "--created-at", CREATED])
    _main(["register", SID2, "--created-at", CREATED])
    capsys.readouterr()
    assert _main(["status"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"{SID} recorded",
        f"{SID2} recorded",
    ]


# ---------------------------------------------------------------------------
# mac-eligible
# ---------------------------------------------------------------------------


def test_cli_mac_eligible_exit_codes_follow_eligibility(cli_env, capsys):
    """mac-eligible は eligible で 0 / not-eligible で 1 を返す。"""
    from interview_pipeline.states import State
    from interview_pipeline.store import SessionStore

    data_dir, _ = cli_env
    _drive_cli_store(data_dir, "draft_saved")
    assert _main(["mac-eligible", SID]) == 1
    assert capsys.readouterr().out.strip() == "not-eligible"
    store = SessionStore(data_dir / "state" / "sessions.json")
    store.transition(SID, State.FINAL_SAVED)
    assert _main(["mac-eligible", SID]) == 0
    assert capsys.readouterr().out.strip() == "eligible"


def test_cli_mac_eligible_unknown_session_raises_key_error(cli_env):
    """未知セッションの mac-eligible は KeyError のまま伝播する。"""
    with pytest.raises(KeyError):
        _main(["mac-eligible", SID])


# ---------------------------------------------------------------------------
# delete-vm-copy
# ---------------------------------------------------------------------------


def test_cli_delete_vm_copy_removes_folder_and_reports_path(cli_env, capsys):
    """削除可なら inbox のセッションフォルダが消え、パスが出力される。"""
    data_dir, _ = cli_env
    _drive_cli_store(data_dir, "draft_saved")
    target = data_dir / "inbox" / SID
    target.mkdir(parents=True)
    (target / "system_track.m4a").write_bytes(b"dummy")

    assert _main(["delete-vm-copy", SID]) == 0
    assert capsys.readouterr().out.strip() == f"deleted {target}"
    assert not target.exists()
    (entry,) = _sessions_payload(data_dir)
    assert entry["vm_copy_deleted_at"] is not None


def test_cli_delete_vm_copy_refused_when_guard_not_satisfied(cli_env, exc):
    """ガード未達なら DeletionGuardError で拒否されフォルダは残る。"""
    data_dir, _ = cli_env
    _main(["register", SID, "--created-at", CREATED])
    target = data_dir / "inbox" / SID
    target.mkdir(parents=True)
    kept = target / "system_track.m4a"
    kept.write_bytes(b"dummy")

    with pytest.raises(exc("DeletionGuardError")):
        _main(["delete-vm-copy", SID])
    assert kept.exists()


def test_cli_delete_vm_copy_second_run_still_succeeds(cli_env, capsys):
    """現行挙動: 削除済み（対象フォルダ無し）でも再実行は正常終了する。"""
    data_dir, _ = cli_env
    _drive_cli_store(data_dir, "draft_saved")
    assert _main(["delete-vm-copy", SID]) == 0
    capsys.readouterr()
    assert _main(["delete-vm-copy", SID]) == 0
    assert capsys.readouterr().out.strip() == (f"deleted {data_dir / 'inbox' / SID}")


# ---------------------------------------------------------------------------
# receive / run の入口挙動
# ---------------------------------------------------------------------------


def test_cli_receive_without_manifest_raises_file_not_found(cli_env):
    """マニフェストが無い receive は FileNotFoundError のまま伝播する。"""
    _main(["register", SID, "--created-at", CREATED])
    with pytest.raises(FileNotFoundError):
        _main(["receive", SID])


def test_cli_receive_unknown_session_raises_key_error(cli_env, filekit):
    """マニフェストはあるが未登録セッションの receive は KeyError。"""
    data_dir, _ = cli_env
    staging = data_dir / "staging" / SID
    filekit.write(staging / "system_track.m4a", b"dummy")
    filekit.write_manifest(
        staging / "manifest.json",
        filekit.manifest_dict(SID, [("system_track.m4a", b"dummy")]),
    )
    with pytest.raises(KeyError):
        _main(["receive", SID])


def test_cli_run_with_empty_store_reports_empty_lists(cli_env, capsys):
    """空ストアの run は processed=[] failed=[] skipped=[] と出す。"""
    assert _main(["run"]) == 0
    assert capsys.readouterr().out.strip() == "processed=[] failed=[] skipped=[]"


def test_cli_run_skips_recorded_session(cli_env, capsys):
    """recorded（対象外状態）は run で skipped に列挙される。"""
    _main(["register", SID, "--created-at", CREATED])
    capsys.readouterr()
    assert _main(["run"]) == 0
    assert capsys.readouterr().out.strip() == (
        f"processed=[] failed=[] skipped=['{SID}']"
    )


# ---------------------------------------------------------------------------
# __main__ の入口（subprocess）
# ---------------------------------------------------------------------------


def _subprocess_env(data_dir: Path, notes_dir: Path) -> dict:
    import interview_pipeline

    src_dir = Path(interview_pipeline.__file__).resolve().parent.parent
    env = dict(os.environ)
    env["INTERVIEW_DATA_DIR"] = str(data_dir)
    env["INTERVIEW_NOTES_DIR"] = str(notes_dir)
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{src_dir}{os.pathsep}{pythonpath}" if pythonpath else str(src_dir)
    )
    return env


def test_module_entrypoint_no_args_exits_2(tmp_path):
    """``python -m interview_pipeline`` 無引数は終了コード2。"""
    result = subprocess.run(
        [sys.executable, "-m", "interview_pipeline"],
        capture_output=True,
        text=True,
        env=_subprocess_env(tmp_path / "d", tmp_path / "n"),
        timeout=60,
    )
    assert result.returncode == 2


def test_module_entrypoint_missing_env_exits_1_with_message(tmp_path):
    """環境変数未設定では終了コード1でメッセージが標準エラーに出る。"""
    env = _subprocess_env(tmp_path / "d", tmp_path / "n")
    del env["INTERVIEW_DATA_DIR"]
    result = subprocess.run(
        [sys.executable, "-m", "interview_pipeline", "status"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 1
    assert "INTERVIEW_DATA_DIR is not set" in result.stderr
