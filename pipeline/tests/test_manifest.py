"""マニフェストのテスト。

根拠: pipeline/SPEC.md の ``manifest.py`` 契約と docs/手順書.md
「転送受信と検証」（マニフェストは session_id とファイルごとの
name / size / sha256 を持ち、系統はファイル名の接頭辞で区別する）。
"""

from __future__ import annotations

import pytest

SID = "2026-11-09_01"
SYSTEM_DATA = b"dummy-system-audio-bytes"
MIC_DATA = b"dummy-mic-audio-bytes"


def test_file_entry_fields(filekit):
    """FileEntry は name / size / sha256 を保持する。"""
    from interview_pipeline.manifest import FileEntry

    entry = FileEntry(
        name="system_track.m4a",
        size=len(SYSTEM_DATA),
        sha256=filekit.sha256_hex(SYSTEM_DATA),
    )
    assert entry.name == "system_track.m4a"
    assert entry.size == len(SYSTEM_DATA)
    assert entry.sha256 == filekit.sha256_hex(SYSTEM_DATA)


def test_manifest_from_dict(filekit):
    """Manifest.from_dict で session_id とファイル一覧が復元できる。"""
    from interview_pipeline.manifest import Manifest

    data = filekit.manifest_dict(
        SID,
        [("system_track.m4a", SYSTEM_DATA), ("mic_track.m4a", MIC_DATA)],
    )
    manifest = Manifest.from_dict(data)

    assert manifest.session_id == SID
    assert [f.name for f in manifest.files] == [
        "system_track.m4a",
        "mic_track.m4a",
    ]
    by_name = {f.name: f for f in manifest.files}
    assert by_name["system_track.m4a"].size == len(SYSTEM_DATA)
    assert by_name["system_track.m4a"].sha256 == filekit.sha256_hex(SYSTEM_DATA)
    assert by_name["mic_track.m4a"].size == len(MIC_DATA)
    assert by_name["mic_track.m4a"].sha256 == filekit.sha256_hex(MIC_DATA)


def test_manifest_load_from_json_file(tmp_path, filekit):
    """Manifest.load で JSON ファイルから読み込める。"""
    from interview_pipeline.manifest import Manifest

    data = filekit.manifest_dict(
        SID,
        [("system_track.m4a", SYSTEM_DATA), ("mic_track.m4a", MIC_DATA)],
    )
    path = filekit.write_manifest(tmp_path / "manifest.json", data)

    manifest = Manifest.load(path)
    assert manifest.session_id == SID
    assert len(manifest.files) == 2


def test_manifest_keeps_system_and_mic_entries_distinguishable(filekit):
    """系統（system / mic）はファイル名で区別できる形で保持される。"""
    from interview_pipeline.manifest import Manifest

    data = filekit.manifest_dict(
        SID,
        [("system_track.m4a", SYSTEM_DATA), ("mic_track.m4a", MIC_DATA)],
    )
    manifest = Manifest.from_dict(data)

    names = [f.name for f in manifest.files]
    assert any(name.startswith("system") for name in names)
    assert any(name.startswith("mic") for name in names)


def test_manifest_from_dict_rejects_missing_files_key():
    """files を欠く dict は受理しない（検証の前提が崩れるため）。"""
    from interview_pipeline.manifest import Manifest

    with pytest.raises(Exception):
        Manifest.from_dict({"session_id": SID})
