from __future__ import annotations

from main import _auto_sparse_frames_from_scene_summary


def test_auto_frames_three_keyframes() -> None:
    f = _auto_sparse_frames_from_scene_summary(
        {"playback_start": 1.0, "playback_end": 60.0, "frame": 1.0, "fps": 24.0}
    )
    assert f == [1.0, 30.5, 60.0]


def test_auto_frames_single_when_range_tiny() -> None:
    f = _auto_sparse_frames_from_scene_summary(
        {"playback_start": 10.0, "playback_end": 10.02, "frame": 10.0}
    )
    assert f == [10.0]


def test_auto_frames_swap_hi_lo() -> None:
    f = _auto_sparse_frames_from_scene_summary({"playback_start": 48.0, "playback_end": 1.0})
    assert f == [1.0, 24.5, 48.0]


def test_auto_frames_only_current_when_no_playback() -> None:
    f = _auto_sparse_frames_from_scene_summary({"frame": 7.5})
    assert f == [7.5]
