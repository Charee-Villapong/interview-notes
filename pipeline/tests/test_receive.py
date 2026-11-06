"""受信検証（verify_and_promote）のテスト。

根拠: pipeline/SPEC.md の ``receive.py`` 契約、docs/状態管理.md
「遷移条件」（transferring → received は全ファイルのサイズ＋SHA-256 一致時のみ、
不一致は needs_reprocess / resume: transfer_pending）、docs/手順書.md
「転送受信と検証」。

前提（解釈）: ``staging_dir`` / ``inbox_dir`` は当該セッションの
ディレクトリ（ファイルが直接その下に置かれる）。runner 側の引数が
``staging_root`` / ``inbox_root`` と別名であることからの判断。
"""

from __future__ import annotations

from pathlib import Path

SID = "2026-11-09_01"
SYSTEM_DATA = b"dummy-system-audio-bytes"
MIC_DATA = b"dummy-mic-audio-bytes"
FILES = [("system_track.m4a", SYSTEM_DATA), ("mic_track.m4a", MIC_DATA)]


def _setup_session(make_store, drive_to, t0, tmp_path):
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, "transferring")
    staging_dir = tmp_path / "staging"
    inbox_dir = tmp_path / "inbox"
    staging_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir.mkdir(parents=True, exist_ok=True)
    return store, staging_dir, inbox_dir


def _promote(store, manifest, staging_dir, inbox_dir):
    from interview_pipeline.receive import verify_and_promote

    return verify_and_promote(store, manifest, staging_dir, inbox_dir)


def _inbox_files(inbox_dir: Path) -> list[Path]:
    return sorted(p for p in inbox_dir.rglob("*") if p.is_file())


def test_success_promotes_to_inbox_and_received(
    make_store, drive_to, filekit, state_name, t0, tmp_path
):
    """全ファイル一致した時だけ inbox へ昇格し received になる。"""
    from interview_pipeline.manifest import Manifest

    store, staging_dir, inbox_dir = _setup_session(
        make_store, drive_to, t0, tmp_path
    )
    for name, data in FILES:
        filekit.write(staging_dir / name, data)
    manifest = Manifest.from_dict(filekit.manifest_dict(SID, FILES))

    result = _promote(store, manifest, staging_dir, inbox_dir)

    assert result.ok is True
    assert state_name(store.get(SID).state) == "received"
    promoted = {p.name for p in _inbox_files(inbox_dir)}
    assert promoted == {"system_track.m4a", "mic_track.m4a"}
    # 中身がそのまま移っていること。
    for path in _inbox_files(inbox_dir):
        expected = SYSTEM_DATA if "system" in path.name else MIC_DATA
        assert path.read_bytes() == expected


def test_sha256_mismatch_blocks_promotion(
    make_store, drive_to, filekit, state_name, t0, tmp_path
):
    """SHA-256 不一致なら inbox へ移さず needs_reprocess になる。"""
    from interview_pipeline.manifest import Manifest

    store, staging_dir, inbox_dir = _setup_session(
        make_store, drive_to, t0, tmp_path
    )
    for name, data in FILES:
        filekit.write(staging_dir / name, data)
    data = filekit.manifest_dict(SID, FILES)
    data["files"][0]["sha256"] = "0" * 64  # 実ファイルと一致しない値
    manifest = Manifest.from_dict(data)

    result = _promote(store, manifest, staging_dir, inbox_dir)

    assert result.ok is False
    assert result.reason  # 失敗理由が返る
    assert _inbox_files(inbox_dir) == []
    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.resume_state) == "transfer_pending"
    assert session.error_kind == "transfer_verification_failed"


def test_size_mismatch_blocks_promotion(
    make_store, drive_to, filekit, state_name, t0, tmp_path
):
    """サイズ不一致なら inbox へ移さず needs_reprocess になる。"""
    from interview_pipeline.manifest import Manifest

    store, staging_dir, inbox_dir = _setup_session(
        make_store, drive_to, t0, tmp_path
    )
    for name, data in FILES:
        filekit.write(staging_dir / name, data)
    data = filekit.manifest_dict(SID, FILES)
    data["files"][1]["size"] = len(MIC_DATA) + 10  # 実サイズと異なる
    manifest = Manifest.from_dict(data)

    result = _promote(store, manifest, staging_dir, inbox_dir)

    assert result.ok is False
    assert _inbox_files(inbox_dir) == []
    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.resume_state) == "transfer_pending"
    assert session.error_kind == "transfer_verification_failed"


def test_tampered_content_blocks_promotion(
    make_store, drive_to, filekit, state_name, t0, tmp_path
):
    """ステージング側の内容が化けていれば（サイズ同・中身違い）拒否する。"""
    from interview_pipeline.manifest import Manifest

    store, staging_dir, inbox_dir = _setup_session(
        make_store, drive_to, t0, tmp_path
    )
    # mic 側だけ、同じ長さの別内容を置く（SHA-256 で検出されるはず）。
    tampered = b"X" * len(MIC_DATA)
    filekit.write(staging_dir / "system_track.m4a", SYSTEM_DATA)
    filekit.write(staging_dir / "mic_track.m4a", tampered)
    manifest = Manifest.from_dict(filekit.manifest_dict(SID, FILES))

    result = _promote(store, manifest, staging_dir, inbox_dir)

    assert result.ok is False
    assert _inbox_files(inbox_dir) == []
    assert state_name(store.get(SID).state) == "needs_reprocess"


def test_missing_file_blocks_promotion(
    make_store, drive_to, filekit, state_name, t0, tmp_path
):
    """マニフェストにあるファイルが欠落していれば拒否する。"""
    from interview_pipeline.manifest import Manifest

    store, staging_dir, inbox_dir = _setup_session(
        make_store, drive_to, t0, tmp_path
    )
    filekit.write(staging_dir / "system_track.m4a", SYSTEM_DATA)
    # mic_track.m4a は置かない（欠落）。
    manifest = Manifest.from_dict(filekit.manifest_dict(SID, FILES))

    result = _promote(store, manifest, staging_dir, inbox_dir)

    assert result.ok is False
    assert _inbox_files(inbox_dir) == []
    session = store.get(SID)
    assert state_name(session.state) == "needs_reprocess"
    assert state_name(session.resume_state) == "transfer_pending"
    assert session.error_kind == "transfer_verification_failed"


def test_one_bad_file_blocks_all_files(
    make_store, drive_to, filekit, state_name, t0, tmp_path
):
    """1件でも不一致なら、正常なファイルも inbox へ移さない（全-or-無）。"""
    from interview_pipeline.manifest import Manifest

    store, staging_dir, inbox_dir = _setup_session(
        make_store, drive_to, t0, tmp_path
    )
    for name, data in FILES:
        filekit.write(staging_dir / name, data)
    data = filekit.manifest_dict(SID, FILES)
    data["files"][1]["sha256"] = "f" * 64
    manifest = Manifest.from_dict(data)

    result = _promote(store, manifest, staging_dir, inbox_dir)

    assert result.ok is False
    assert _inbox_files(inbox_dir) == [], "一部だけ昇格してはいけない"


def test_receive_is_idempotent_after_received(
    make_store, drive_to, filekit, state_name, t0, tmp_path
):
    """received 以降のセッションへの再呼び出しは冪等に ok=True。"""
    from interview_pipeline.manifest import Manifest

    store, staging_dir, inbox_dir = _setup_session(
        make_store, drive_to, t0, tmp_path
    )
    for name, data in FILES:
        filekit.write(staging_dir / name, data)
    manifest = Manifest.from_dict(filekit.manifest_dict(SID, FILES))

    first = _promote(store, manifest, staging_dir, inbox_dir)
    assert first.ok is True
    second = _promote(store, manifest, staging_dir, inbox_dir)

    assert second.ok is True
    assert state_name(store.get(SID).state) == "received"
    assert len(store.all()) == 1, "二重にセッションレコードができてはいけない"


def test_reverification_after_failure_creates_no_duplicate(
    make_store, drive_to, filekit, state_name, t0, tmp_path
):
    """失敗後の再受信・再検証でもレコードは1件のまま received になれる。"""
    from interview_pipeline.manifest import Manifest

    store, staging_dir, inbox_dir = _setup_session(
        make_store, drive_to, t0, tmp_path
    )
    for name, data in FILES:
        filekit.write(staging_dir / name, data)
    bad = filekit.manifest_dict(SID, FILES)
    bad["files"][0]["sha256"] = "0" * 64
    failed = _promote(
        store, Manifest.from_dict(bad), staging_dir, inbox_dir
    )
    assert failed.ok is False
    assert state_name(store.get(SID).state) == "needs_reprocess"

    # 正しいマニフェスト＋ファイルで再検証する。
    good = Manifest.from_dict(filekit.manifest_dict(SID, FILES))
    retried = _promote(store, good, staging_dir, inbox_dir)

    assert retried.ok is True
    assert state_name(store.get(SID).state) == "received"
    assert len(store.all()) == 1
    assert {p.name for p in _inbox_files(inbox_dir)} == {
        "system_track.m4a",
        "mic_track.m4a",
    }
