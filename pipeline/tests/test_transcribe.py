"""文字起こしと話者合成のテスト。

根拠: pipeline/SPEC.md の ``transcribe.py`` 契約、docs/手順書.md
「処理ラン」（話者A＝マイク系統＝本人、話者B＝システム系統＝先方、
時系列マージ）。
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _segment(start, end, text):
    from interview_pipeline.transcribe import Segment

    return Segment(start=start, end=end, text=text)


def test_segment_fields():
    """Segment は start / end / text を保持する。"""
    segment = _segment(1.5, 2.5, "ダミー発話")
    assert segment.start == 1.5
    assert segment.end == 2.5
    assert segment.text == "ダミー発話"


def test_merge_tracks_labels_speakers():
    """システム系統＝話者 "B"、マイク系統＝話者 "A" とラベルされる。"""
    from interview_pipeline.transcribe import merge_tracks

    system_segments = [_segment(0.0, 1.0, "system-utterance")]
    mic_segments = [_segment(2.0, 3.0, "mic-utterance")]

    merged = merge_tracks(system_segments, mic_segments)

    labels = {segment.text: label for label, segment in merged}
    assert labels["system-utterance"] == "B"
    assert labels["mic-utterance"] == "A"


def test_merge_tracks_orders_by_start_time():
    """合成結果は時系列（開始時刻）順になる。"""
    from interview_pipeline.transcribe import merge_tracks

    system_segments = [
        _segment(0.0, 1.0, "sys-1"),
        _segment(4.0, 5.0, "sys-2"),
    ]
    mic_segments = [
        _segment(2.0, 3.0, "mic-1"),
        _segment(6.0, 7.0, "mic-2"),
    ]

    merged = merge_tracks(system_segments, mic_segments)

    assert [(label, seg.text) for label, seg in merged] == [
        ("B", "sys-1"),
        ("A", "mic-1"),
        ("B", "sys-2"),
        ("A", "mic-2"),
    ]
    starts = [seg.start for _, seg in merged]
    assert starts == sorted(starts)


def test_merge_tracks_sorts_within_a_track_too():
    """入力側の順序が乱れていても開始時刻順に整列される。"""
    from interview_pipeline.transcribe import merge_tracks

    system_segments = [
        _segment(5.0, 6.0, "sys-late"),
        _segment(1.0, 2.0, "sys-early"),
    ]
    merged = merge_tracks(system_segments, [])
    assert [seg.text for _, seg in merged] == ["sys-early", "sys-late"]


def test_merge_tracks_with_empty_inputs():
    """片方または両方が空でも合成できる。"""
    from interview_pipeline.transcribe import merge_tracks

    assert merge_tracks([], []) == []

    only_mic = merge_tracks([], [_segment(0.0, 1.0, "mic-only")])
    assert [(label, seg.text) for label, seg in only_mic] == [("A", "mic-only")]

    only_system = merge_tracks([_segment(0.0, 1.0, "sys-only")], [])
    assert [(label, seg.text) for label, seg in only_system] == [("B", "sys-only")]


def test_unavailable_transcriber_always_raises(exc, tmp_path):
    """STT未接続（UnavailableTranscriber）は常に EngineNotConfiguredError。"""
    from interview_pipeline.transcribe import UnavailableTranscriber

    audio = Path(tmp_path) / "system_track.m4a"
    audio.write_bytes(b"dummy-audio")
    transcriber = UnavailableTranscriber()
    with pytest.raises(exc("EngineNotConfiguredError")):
        transcriber.transcribe(audio)


def test_unavailable_transcriber_raises_regardless_of_file(exc, tmp_path):
    """存在しないファイルを渡しても「未接続」が理由で失敗する。"""
    from interview_pipeline.transcribe import UnavailableTranscriber

    transcriber = UnavailableTranscriber()
    with pytest.raises(exc("EngineNotConfiguredError")):
        transcriber.transcribe(Path(tmp_path) / "missing.m4a")
