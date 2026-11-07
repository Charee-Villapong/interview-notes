"""特性テスト（現状固定）: C-051 receive の開始状態別分岐と遷移回避パターン。

根拠: refactoring-candidates-2026-11-07.md の interview-notes 候補2
（「既に needs_reprocess なら遷移せず直接更新」パターンの4重実装。
receive の再失敗分岐そのものを狙ったテストは確認できず）と
「テストが薄い箇所」（receive.py の開始状態別分岐・多段遷移・
received 以後の冪等が薄い）、pipeline/SPEC.md の ``receive.py`` 契約。

既存テストで固定済みの範囲（本ファイルでは重複させない）:
test_receive.py が transferring 開始の成功・SHA／サイズ／欠落の失敗・
received 後の冪等・失敗後の再検証を固定済み。

本ファイルが固定するのは:
- recorded / transfer_pending 開始の成功（多段遷移で received へ）
- recorded 開始の検証失敗
- received 以後の各状態での冪等（不正マニフェストでも検証自体を行わず
  ok=True、staging・inbox・状態はすべて不変）
- needs_reprocess での再失敗（error_kind / resume_state の直接上書き）
- needs_reprocess からの成功（失敗情報のクリア）
- 失敗理由（reason）文字列の現行値
"""

from __future__ import annotations

import pytest

SID = "2026-11-09_01"
SYSTEM_DATA = b"dummy-system-audio-bytes"
MIC_DATA = b"dummy-mic-audio-bytes"
FILES = [("system_track.m4a", SYSTEM_DATA), ("mic_track.m4a", MIC_DATA)]


def _manifest(filekit, bad_sha=False):
    from interview_pipeline.manifest import Manifest

    data = filekit.manifest_dict(SID, FILES)
    if bad_sha:
        data["files"][0]["sha256"] = "0" * 64
    return Manifest.from_dict(data)


def _dirs(tmp_path, label=""):
    staging_dir = tmp_path / f"staging{label}"
    inbox_dir = tmp_path / f"inbox{label}"
    staging_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir.mkdir(parents=True, exist_ok=True)
    return staging_dir, inbox_dir


def _promote(store, manifest, staging_dir, inbox_dir):
    from interview_pipeline.receive import verify_and_promote

    return verify_and_promote(store, manifest, staging_dir, inbox_dir)


def _put_files(filekit, staging_dir):
    for name, data in FILES:
        filekit.write(staging_dir / name, data)


# ---------------------------------------------------------------------------
# 開始状態別の成功経路（多段遷移）
# ---------------------------------------------------------------------------


def test_receive_success_from_recorded(make_store, filekit, state_name, t0, tmp_path):
    """recorded 開始でも検証成功で received まで一気に進む。"""
    store = make_store()
    store.register(SID, t0)
    staging_dir, inbox_dir = _dirs(tmp_path)
    _put_files(filekit, staging_dir)

    result = _promote(store, _manifest(filekit), staging_dir, inbox_dir)

    assert result.ok is True
    assert state_name(store.get(SID).state) == "received"
    assert {p.name for p in inbox_dir.iterdir()} == {
        "system_track.m4a",
        "mic_track.m4a",
    }


def test_receive_success_from_transfer_pending(
    make_store, drive_to, filekit, state_name, t0, tmp_path
):
    """transfer_pending 開始でも検証成功で received になる。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "transfer_pending")
    staging_dir, inbox_dir = _dirs(tmp_path)
    _put_files(filekit, staging_dir)

    result = _promote(store, _manifest(filekit), staging_dir, inbox_dir)

    assert result.ok is True
    assert state_name(store.get(SID).state) == "received"


def test_receive_failure_from_recorded(make_store, filekit, state_name, t0, tmp_path):
    """recorded 開始の検証失敗も needs_reprocess（resume: transfer_pending）。"""
    store = make_store()
    store.register(SID, t0)
    staging_dir, inbox_dir = _dirs(tmp_path)
    _put_files(filekit, staging_dir)

    result = _promote(store, _manifest(filekit, bad_sha=True), staging_dir, inbox_dir)

    assert result.ok is False
    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.resume_state) == "transfer_pending"
    assert session.error_kind == "transfer_verification_failed"
    assert list(inbox_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# received 以後の冪等（検証自体を行わない）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "processing",
        "awaiting_editing",
        "draft_saved",
        "awaiting_review",
        "final_saved",
        "deletable",
        "closed",
    ],
)
def test_receive_idempotent_for_states_after_received(
    make_store, drive_to, filekit, state_name, t0, tmp_path, target
):
    """received 以後の状態では、不正マニフェストでも検証せず ok=True。

    状態は変わらず、staging のファイルは残り、inbox へは何も移らない
    （＝冪等判定が検証より先に行われる現行挙動）。
    """
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, target)
    staging_dir, inbox_dir = _dirs(tmp_path)
    _put_files(filekit, staging_dir)

    result = _promote(store, _manifest(filekit, bad_sha=True), staging_dir, inbox_dir)

    assert result.ok is True
    assert state_name(store.get(SID).state) == target
    assert {p.name for p in staging_dir.iterdir()} == {
        "system_track.m4a",
        "mic_track.m4a",
    }
    assert list(inbox_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# needs_reprocess の再失敗・再成功（候補2の直接更新パターン）
# ---------------------------------------------------------------------------


def _needs_reprocess_store(make_store, drive_to, state_of, t0):
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "received")
    store.transition(
        SID,
        state_of("needs_reprocess"),
        error_kind="processing_failed",
        resume_state=state_of("received"),
    )
    return store


def test_receive_refailure_in_needs_reprocess_overwrites_failure_info(
    make_store, drive_to, state_of, filekit, state_name, t0, tmp_path
):
    """既に needs_reprocess の再失敗では遷移せず失敗情報だけ上書きされる。

    error_kind は元の processing_failed から transfer_verification_failed
    へ、resume_state は received から transfer_pending へ書き換わる。
    """
    store = _needs_reprocess_store(make_store, drive_to, state_of, t0)
    staging_dir, inbox_dir = _dirs(tmp_path)
    _put_files(filekit, staging_dir)

    result = _promote(store, _manifest(filekit, bad_sha=True), staging_dir, inbox_dir)

    assert result.ok is False
    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert session.error_kind == "transfer_verification_failed"
    assert state_name(session.resume_state) == "transfer_pending"
    assert list(inbox_dir.iterdir()) == []


def test_receive_success_from_needs_reprocess_clears_failure_info(
    make_store, drive_to, state_of, filekit, state_name, t0, tmp_path
):
    """needs_reprocess からの検証成功で received になり失敗情報は消える。"""
    store = _needs_reprocess_store(make_store, drive_to, state_of, t0)
    staging_dir, inbox_dir = _dirs(tmp_path)
    _put_files(filekit, staging_dir)

    result = _promote(store, _manifest(filekit), staging_dir, inbox_dir)

    assert result.ok is True
    session = store.get(SID)
    assert state_name(session.state) == "received"
    assert session.error_kind is None
    assert session.resume_state is None


# ---------------------------------------------------------------------------
# 失敗理由（reason）の現行文字列
# ---------------------------------------------------------------------------


def test_receive_reason_for_missing_file(make_store, drive_to, filekit, t0, tmp_path):
    """欠落時の reason は "missing file: <name>" の形。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "transferring")
    staging_dir, inbox_dir = _dirs(tmp_path)
    filekit.write(staging_dir / "system_track.m4a", SYSTEM_DATA)

    result = _promote(store, _manifest(filekit), staging_dir, inbox_dir)

    assert result.ok is False
    assert result.reason == "missing file: mic_track.m4a"


def test_receive_reason_for_size_mismatch(make_store, drive_to, filekit, t0, tmp_path):
    """サイズ不一致時の reason は "size mismatch: <name>" の形。"""
    from interview_pipeline.manifest import Manifest

    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "transferring")
    staging_dir, inbox_dir = _dirs(tmp_path)
    _put_files(filekit, staging_dir)
    data = filekit.manifest_dict(SID, FILES)
    data["files"][0]["size"] = len(SYSTEM_DATA) + 5

    result = _promote(store, Manifest.from_dict(data), staging_dir, inbox_dir)

    assert result.ok is False
    assert result.reason == "size mismatch: system_track.m4a"


def test_receive_reason_for_sha256_mismatch(
    make_store, drive_to, filekit, t0, tmp_path
):
    """SHA-256 不一致時の reason は "sha256 mismatch: <name>" の形。"""
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "transferring")
    staging_dir, inbox_dir = _dirs(tmp_path)
    _put_files(filekit, staging_dir)

    result = _promote(store, _manifest(filekit, bad_sha=True), staging_dir, inbox_dir)

    assert result.ok is False
    assert result.reason == "sha256 mismatch: system_track.m4a"
