"""宛先ガード（check_note_destination）と要約 push（publish_note）のテスト。

根拠: docs/状態管理.md「要約の保存先」と「削除の条件」、
pipeline/SPEC.md の ``guards.py`` / ``publish.py`` 契約。

守るべき2点:
- 公開リポジトリへは上げない（宛先が private であることを push 前に
  機械確認し、確認できなければ push しない）。
- push 済みでない要約は消さない（push 成功をリモート上の内容一致で
  確認できた場合に限って、ローカルコピーを削除する）。
"""

from __future__ import annotations

import pytest

SID = "2026-11-09_01"
REPO = "owner/notes-repo"
NOTE_CONTENT = "# 面談ノート（ダミー）\nダミー本文です。\n"
REMOTE_PATH = f"2026-11-09/{SID}.md"


class FakeGitHub:
    """テスト用の GitHub クライアント（実 API は叩かない）。"""

    def __init__(self, visibility="private", default_branch="main"):
        self.info = {"visibility": visibility, "default_branch": default_branch}
        self.contents: dict[str, bytes] = {}
        self.put_calls: list = []
        self.fail_put = False
        self.corrupt_after_put = False
        self.info_error = False

    def repo_info(self, repo):
        if self.info_error:
            raise RuntimeError("api unavailable (dummy)")
        return self.info

    def content_sha(self, repo, path):
        from interview_pipeline.publish import git_blob_sha

        data = self.contents.get(path)
        return git_blob_sha(data) if data is not None else None

    def put_content(self, repo, path, content, *, message, sha=None):
        self.put_calls.append((path, sha))
        if self.fail_put:
            return False
        self.contents[path] = b"corrupted" if self.corrupt_after_put else content
        return True


def _setup(make_store, drive_to, t0, tmp_path, target="draft_saved"):
    store = make_store()
    store.register(SID, t0)
    drive_to(store, SID, target)
    note = tmp_path / "note.md"
    note.write_text(NOTE_CONTENT, encoding="utf-8")
    return store, note


def _publish(store, note, github, repo=REPO):
    from interview_pipeline.publish import publish_note

    return publish_note(store, SID, note, notes_repo=repo, github=github)


# ---------------------------------------------------------------------------
# check_note_destination
# ---------------------------------------------------------------------------


def test_destination_accepts_private_repo():
    """正常系: 可視性が private なら True。"""
    from interview_pipeline.guards import check_note_destination

    assert check_note_destination(REPO, github=FakeGitHub("private")) is True


@pytest.mark.parametrize("visibility", ["public", "internal", ""])
def test_destination_rejects_non_private_repo(visibility):
    """異常系: private 以外の可視性は False。"""
    from interview_pipeline.guards import check_note_destination

    assert check_note_destination(REPO, github=FakeGitHub(visibility)) is False


def test_destination_rejects_when_info_unavailable():
    """異常系: リポジトリ情報を取得できなければ False（fail-closed）。"""
    from interview_pipeline.guards import check_note_destination

    github = FakeGitHub()
    github.info = None
    assert check_note_destination(REPO, github=github) is False
    github = FakeGitHub()
    github.info_error = True
    assert check_note_destination(REPO, github=github) is False


@pytest.mark.parametrize("repo", ["notes-repo", "owner/", "/repo", "a/b/c", ""])
def test_destination_rejects_malformed_repo_name(repo):
    """異常系: owner/repo 形式でない宛先は False。"""
    from interview_pipeline.guards import check_note_destination

    assert check_note_destination(repo, github=FakeGitHub()) is False


# ---------------------------------------------------------------------------
# publish_note: 正常系
# ---------------------------------------------------------------------------


def test_publish_success_pushes_records_and_deletes_local(
    make_store, drive_to, state_name, t0, tmp_path
):
    """正常系: push→確認→記録・遷移→ローカルコピー削除まで通る。"""
    store, note = _setup(make_store, drive_to, t0, tmp_path)
    github = FakeGitHub()

    url = _publish(store, note, github)

    assert url == f"https://github.com/{REPO}/blob/main/{REMOTE_PATH}"
    assert github.contents[REMOTE_PATH] == NOTE_CONTENT.encode("utf-8")
    session = store.get(SID)
    assert state_name(session.state) == "final_saved"
    assert session.final_saved_at is not None
    assert session.note_published_at is not None
    assert not note.exists()


def test_publish_is_idempotent_when_remote_already_identical(
    make_store, drive_to, state_name, t0, tmp_path
):
    """リモートに同一内容があれば PUT せずに完了できる（再実行安全）。"""
    store, note = _setup(make_store, drive_to, t0, tmp_path)
    github = FakeGitHub()
    github.contents[REMOTE_PATH] = NOTE_CONTENT.encode("utf-8")

    url = _publish(store, note, github)

    assert url.endswith(REMOTE_PATH)
    assert github.put_calls == []
    assert state_name(store.get(SID).state) == "final_saved"
    assert not note.exists()


def test_publish_updates_remote_when_content_differs(
    make_store, drive_to, t0, tmp_path
):
    """リモートに別内容があれば、既存 sha を渡して更新する。"""
    store, note = _setup(make_store, drive_to, t0, tmp_path)
    github = FakeGitHub()
    github.contents[REMOTE_PATH] = b"old content"

    _publish(store, note, github)

    assert github.contents[REMOTE_PATH] == NOTE_CONTENT.encode("utf-8")
    assert len(github.put_calls) == 1
    assert github.put_calls[0][1] is not None  # 既存 sha が渡っている


# ---------------------------------------------------------------------------
# publish_note: 異常系（要約は保持され、状態は draft_saved のまま）
# ---------------------------------------------------------------------------


def test_publish_refused_for_public_repo(make_store, drive_to, exc, t0, tmp_path):
    """公開リポジトリ宛は push 自体を行わず、要約を保持する。"""
    store, note = _setup(make_store, drive_to, t0, tmp_path)
    github = FakeGitHub(visibility="public")

    with pytest.raises(exc("NotePublishError")):
        _publish(store, note, github)

    assert github.put_calls == []
    assert note.exists()
    session = store.get(SID)
    assert session.note_published_at is None


def test_publish_failure_keeps_note_and_state(
    make_store, drive_to, exc, state_name, t0, tmp_path
):
    """push が失敗したら例外。要約は保持され、状態は draft_saved のまま。"""
    store, note = _setup(make_store, drive_to, t0, tmp_path)
    github = FakeGitHub()
    github.fail_put = True

    with pytest.raises(exc("NotePublishError")):
        _publish(store, note, github)

    assert note.exists()
    session = store.get(SID)
    assert state_name(session.state) == "draft_saved"
    assert session.note_published_at is None


def test_publish_verification_mismatch_keeps_note(
    make_store, drive_to, exc, state_name, t0, tmp_path
):
    """push 後の内容確認が一致しなければ完了扱いにしない。"""
    store, note = _setup(make_store, drive_to, t0, tmp_path)
    github = FakeGitHub()
    github.corrupt_after_put = True

    with pytest.raises(exc("NotePublishError")):
        _publish(store, note, github)

    assert note.exists()
    session = store.get(SID)
    assert state_name(session.state) == "draft_saved"
    assert session.note_published_at is None


def test_publish_requires_draft_saved_state(make_store, drive_to, exc, t0, tmp_path):
    """draft_saved 以外の状態からは push できない。"""
    store, note = _setup(make_store, drive_to, t0, tmp_path, target="awaiting_editing")

    with pytest.raises(exc("NotePublishError")):
        _publish(store, note, FakeGitHub())

    assert note.exists()


def test_publish_missing_note_fails(make_store, drive_to, exc, t0, tmp_path):
    """要約ファイルが無ければ例外（何も push しない）。"""
    store, note = _setup(make_store, drive_to, t0, tmp_path)
    note.unlink()
    github = FakeGitHub()

    with pytest.raises(exc("NotePublishError")):
        _publish(store, note, github)

    assert github.put_calls == []


def test_publish_empty_note_fails(make_store, drive_to, exc, t0, tmp_path):
    """空の要約は push しない。"""
    store, note = _setup(make_store, drive_to, t0, tmp_path)
    note.write_text("", encoding="utf-8")
    github = FakeGitHub()

    with pytest.raises(exc("NotePublishError")):
        _publish(store, note, github)

    assert github.put_calls == []
    assert note.exists()
