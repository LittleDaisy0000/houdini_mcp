"""Session lock merge + clear."""

from runtime.session_lock import clear_session_lock, get_session_lock, set_session_lock, update_session_lock


def setup_function() -> None:
    clear_session_lock()


def test_update_preserves_unspecified_fields() -> None:
    update_session_lock(locked_parent_path="/obj/geo1", note="user ok", effect_tier="v0")
    update_session_lock(note="only note")
    s = get_session_lock()
    assert s["locked_parent_path"] == "/obj/geo1"
    assert s["note"] == "only note"
    assert s["effect_tier"] == "v0"


def test_empty_string_clears_field() -> None:
    update_session_lock(locked_parent_path="/obj/a", note="x")
    update_session_lock(locked_parent_path="")
    s = get_session_lock()
    assert s["locked_parent_path"] is None
    assert s["note"] == "x"


def test_clear_all_via_set() -> None:
    set_session_lock(locked_parent_path="/obj/geo1", note="n", effect_tier="v1")
    clear_session_lock()
    assert get_session_lock() == {"locked_parent_path": None, "note": None, "effect_tier": None}
