"""仕様テスト: Mac側コピー削除完了の申告手段（CLI拡張）。

根拠:
- docs/状態管理.md「削除の条件」「遷移条件」: Mac側コピーの削除可否は
  要約の push 済み（``final_saved`` 以降・``note_published_at`` 記録済み）
  かつ未処理でないこと。``deletable`` → ``closed`` の条件は
  「VM側・Mac側の両コピーの削除完了を記録した」こと。
- docs/P3計画.md「削除ガードとの連携」: VM 側の ``mac_copy_deleted_at``
  を記録し ``closed`` へ進めるための申告手段が現行 CLI に無い、が
  本テストの対象（実装されれば解消する）。

受け入れ条件:
- ``record_mac_deletion`` は実行直前に ``mac_deletion_eligible`` を
  再確認し、満たさなければ ``DeletionGuardError`` で拒否して
  状態・記録を一切変えない。
- 受け付けたら ``mac_copy_deleted_at`` を記録し、``final_saved`` から
  ``deletable`` へ進める。両コピーの削除完了が揃えば ``closed`` へ
  進める（削除の順序によらない）。
- 申告は冪等。再申告は初回の記録時刻を保持して成功する。
- CLI ``report-mac-deleted`` は成功時に ``<session_id> <state>`` を
  出力して終了コード 0。拒否時は ``DeletionGuardError`` が伝播する
  （``delete-vm-copy`` と同じ）。
"""

from __future__ import annotations

import pytest

SID = "2026-11-09_01"


def _published_store(make_store, drive_to, t0, target="final_saved"):
    """push 済み（note_published_at 記録済み）のセッションを持つストア。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, target)
    session = store.get(SID)
    session.note_published_at = t0
    store.save()
    return store


# ---------------------------------------------------------------------------
# record_mac_deletion: 受け付けと状態の前進
# ---------------------------------------------------------------------------


def test_record_mac_deletion_records_timestamp_and_advances_to_deletable(
    make_store, drive_to, state_name, t0
):
    """final_saved からの申告で時刻が記録され deletable へ進む。"""
    from interview_pipeline.guards import record_mac_deletion

    store = _published_store(make_store, drive_to, t0)
    session = record_mac_deletion(store, SID)

    assert session.mac_copy_deleted_at is not None
    assert state_name(session.state) == "deletable"


def test_record_mac_deletion_persists_across_reload(
    make_store, drive_to, state_name, t0
):
    """記録と状態は永続化され、ストア再読み込みでも残る。"""
    from interview_pipeline.guards import record_mac_deletion

    store = _published_store(make_store, drive_to, t0)
    record_mac_deletion(store, SID)

    reloaded = make_store()
    session = reloaded.get(SID)
    assert session.mac_copy_deleted_at is not None
    assert state_name(session.state) == "deletable"


def test_record_mac_deletion_closes_when_vm_copy_already_deleted(
    make_store, drive_to, state_name, t0
):
    """VM側の削除が先に記録済みなら、申告で closed まで進む。"""
    from interview_pipeline.guards import record_mac_deletion

    store = _published_store(make_store, drive_to, t0)
    session = store.get(SID)
    session.vm_copy_deleted_at = t0
    store.save()

    session = record_mac_deletion(store, SID)

    assert session.mac_copy_deleted_at is not None
    assert state_name(session.state) == "closed"


def test_record_mac_deletion_from_deletable_closes_when_both_recorded(
    make_store, drive_to, state_name, t0
):
    """deletable で両コピーの削除完了が揃えば closed へ進む。"""
    from interview_pipeline.guards import record_mac_deletion

    store = _published_store(make_store, drive_to, t0, target="deletable")
    session = store.get(SID)
    session.vm_copy_deleted_at = t0
    store.save()

    session = record_mac_deletion(store, SID)

    assert state_name(session.state) == "closed"


def test_record_mac_deletion_from_deletable_stays_when_vm_copy_remains(
    make_store, drive_to, state_name, t0
):
    """VM側コピーが残っていれば deletable のまま（closed にしない）。"""
    from interview_pipeline.guards import record_mac_deletion

    store = _published_store(make_store, drive_to, t0, target="deletable")

    session = record_mac_deletion(store, SID)

    assert session.mac_copy_deleted_at is not None
    assert session.vm_copy_deleted_at is None
    assert state_name(session.state) == "deletable"


# ---------------------------------------------------------------------------
# record_mac_deletion: 拒否（fail-closed）
# ---------------------------------------------------------------------------


def test_record_mac_deletion_refused_before_final_saved(
    make_store, drive_to, exc, state_name, t0
):
    """draft_saved（push 未完了）では申告を受け付けない。"""
    from interview_pipeline.guards import record_mac_deletion

    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "draft_saved")

    with pytest.raises(exc("DeletionGuardError")):
        record_mac_deletion(store, SID)

    session = store.get(SID)
    assert session.mac_copy_deleted_at is None
    assert state_name(session.state) == "draft_saved"


def test_record_mac_deletion_refused_without_note_published_at(
    make_store, drive_to, exc, state_name, t0
):
    """final_saved でも note_published_at 未記録なら受け付けない。"""
    from interview_pipeline.guards import record_mac_deletion

    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "final_saved")

    with pytest.raises(exc("DeletionGuardError")):
        record_mac_deletion(store, SID)

    session = store.get(SID)
    assert session.mac_copy_deleted_at is None
    assert state_name(session.state) == "final_saved"


def test_record_mac_deletion_refused_when_needs_reprocess(
    make_store, exc, state_name, t0
):
    """未処理（needs_reprocess）のセッションは日数に関わらず対象外。"""
    from interview_pipeline.guards import record_mac_deletion

    store = make_store()
    store.register(SID, t0)
    store.transition(SID, "needs_reprocess", error_kind="processing_failed")
    session = store.get(SID)
    session.note_published_at = t0
    store.save()

    with pytest.raises(exc("DeletionGuardError")):
        record_mac_deletion(store, SID)

    session = store.get(SID)
    assert session.mac_copy_deleted_at is None
    assert state_name(session.state) == "needs_reprocess"


def test_record_mac_deletion_refused_in_recorded_state(make_store, exc, t0):
    """recorded では受け付けない。"""
    from interview_pipeline.guards import record_mac_deletion

    store = make_store()
    store.register(SID, t0)

    with pytest.raises(exc("DeletionGuardError")):
        record_mac_deletion(store, SID)

    assert store.get(SID).mac_copy_deleted_at is None


def test_record_mac_deletion_unknown_session_raises_key_error(make_store):
    """未知セッションは KeyError（既存コマンドと同じ）。"""
    from interview_pipeline.guards import record_mac_deletion

    store = make_store()
    with pytest.raises(KeyError):
        record_mac_deletion(store, SID)


# ---------------------------------------------------------------------------
# record_mac_deletion: 冪等性
# ---------------------------------------------------------------------------


def test_record_mac_deletion_is_idempotent_and_keeps_first_timestamp(
    make_store, drive_to, state_name, t0
):
    """再申告は成功し、初回の記録時刻を保持する。"""
    from interview_pipeline.guards import record_mac_deletion

    store = _published_store(make_store, drive_to, t0)
    first = record_mac_deletion(store, SID)
    first_timestamp = first.mac_copy_deleted_at

    second = record_mac_deletion(store, SID)

    assert second.mac_copy_deleted_at == first_timestamp
    assert state_name(second.state) == "deletable"


# ---------------------------------------------------------------------------
# execute_vm_deletion との対: 削除の順序によらず closed へ進む
# ---------------------------------------------------------------------------


def test_execute_vm_deletion_closes_when_mac_deletion_already_recorded(
    make_store, drive_to, filekit, state_name, t0, tmp_path
):
    """Mac側が先に削除済みなら、VM側の削除記録で closed へ進む。"""
    from interview_pipeline.guards import (
        execute_vm_deletion,
        record_mac_deletion,
    )

    store = _published_store(make_store, drive_to, t0, target="deletable")
    record_mac_deletion(store, SID)
    inbox_root = tmp_path / "inbox"
    filekit.write(inbox_root / SID / "system_track.m4a", b"dummy")

    execute_vm_deletion(store, SID, inbox_root)

    session = store.get(SID)
    assert session.vm_copy_deleted_at is not None
    assert state_name(session.state) == "closed"


def test_execute_vm_deletion_does_not_close_without_mac_deletion(
    make_store, drive_to, filekit, state_name, t0, tmp_path
):
    """Mac側の削除が未記録なら、VM側を消しても deletable のまま。"""
    from interview_pipeline.guards import execute_vm_deletion

    store = _published_store(make_store, drive_to, t0, target="deletable")
    inbox_root = tmp_path / "inbox"
    filekit.write(inbox_root / SID / "system_track.m4a", b"dummy")

    execute_vm_deletion(store, SID, inbox_root)

    session = store.get(SID)
    assert session.vm_copy_deleted_at is not None
    assert session.mac_copy_deleted_at is None
    assert state_name(session.state) == "deletable"


# ---------------------------------------------------------------------------
# CLI: report-mac-deleted
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """データ領域を一時ディレクトリへ向けた CLI 用環境。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("INTERVIEW_DATA_DIR", str(data_dir))
    return data_dir


def _cli_store(data_dir):
    from interview_pipeline.layout import SESSIONS_FILENAME, STATE_DIRNAME
    from interview_pipeline.store import SessionStore

    return SessionStore(data_dir / STATE_DIRNAME / SESSIONS_FILENAME)


def _prepare_cli_session(data_dir, drive_to, t0, target="final_saved"):
    store = _cli_store(data_dir)
    store.register(SID, t0)
    drive_to(store, SID, target)
    session = store.get(SID)
    session.note_published_at = t0
    store.save()
    return store


def test_cli_report_mac_deleted_success_prints_state(cli_env, drive_to, capsys, t0):
    """申告できれば終了コード 0 で「<session_id> <state>」を出す。"""
    from interview_pipeline.cli import main

    _prepare_cli_session(cli_env, drive_to, t0)

    assert main(["report-mac-deleted", SID]) == 0
    assert capsys.readouterr().out.strip() == f"{SID} deletable"
    session = _cli_store(cli_env).get(SID)
    assert session.mac_copy_deleted_at is not None


def test_cli_report_mac_deleted_closes_when_both_deleted(cli_env, drive_to, capsys, t0):
    """両コピーの削除完了が揃えば CLI の結果も closed になる。"""
    from interview_pipeline.cli import main

    store = _prepare_cli_session(cli_env, drive_to, t0)
    session = store.get(SID)
    session.vm_copy_deleted_at = t0
    store.save()

    assert main(["report-mac-deleted", SID]) == 0
    assert capsys.readouterr().out.strip() == f"{SID} closed"


def test_cli_report_mac_deleted_refused_raises(cli_env, drive_to, exc, t0):
    """ガード未達なら DeletionGuardError が伝播する（delete-vm-copy と同じ）。"""
    from interview_pipeline.cli import main

    store = _cli_store(cli_env)
    store.register(SID, t0)
    drive_to(store, SID, "draft_saved")

    with pytest.raises(exc("DeletionGuardError")):
        main(["report-mac-deleted", SID])

    assert _cli_store(cli_env).get(SID).mac_copy_deleted_at is None


def test_cli_report_mac_deleted_unknown_session_raises_key_error(cli_env):
    """未知セッションは KeyError が伝播する（mac-eligible と同じ）。"""
    from interview_pipeline.cli import main

    with pytest.raises(KeyError):
        main(["report-mac-deleted", SID])
