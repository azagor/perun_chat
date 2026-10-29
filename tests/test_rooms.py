"""Tests for room taxonomy helpers."""

from __future__ import annotations

import pytest

from perunchat.rooms import parse_room_id


@pytest.mark.parametrize("bad", [5, None, [], {}, b"grp:a:b:c"])
def test_parse_room_id_rejects_non_str_with_valueerror(bad: object) -> None:
    with pytest.raises(ValueError):
        parse_room_id(bad)


def test_parse_room_id_error_does_not_echo_input() -> None:
    with pytest.raises(ValueError) as e:
        parse_room_id("SECRET<script>")
    assert "SECRET" not in str(e.value)
