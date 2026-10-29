"""Tests for room taxonomy helpers."""

from __future__ import annotations

import pytest

from perunchat.rooms import RoomResolver, parse_room_id


@pytest.mark.parametrize("bad", [5, None, [], {}, b"grp:a:b:c"])
def test_parse_room_id_rejects_non_str_with_valueerror(bad: object) -> None:
    with pytest.raises(ValueError):
        parse_room_id(bad)


def test_parse_room_id_error_does_not_echo_input() -> None:
    with pytest.raises(ValueError) as e:
        parse_room_id("SECRET<script>")
    assert "SECRET" not in str(e.value)


def test_public_room_requires_matching_scope() -> None:
    resolver = RoomResolver()
    room = "pub:global:general"
    assert resolver.can_join(room, "agent", ["pub:global:*"]) is True
    assert resolver.can_join(room, "agent", ["pub:other:*"]) is False
    assert resolver.can_join(room, "agent", []) is False
    assert resolver.can_join(room, "agent", None) is True


def test_private_room_requires_explicit_membership() -> None:
    resolver = RoomResolver()
    room = "priv:abc123"
    assert resolver.can_join(room, "alice", ["priv:*"]) is False
    resolver.add_member(room, "alice")
    assert resolver.can_join(room, "alice", []) is True
    assert resolver.can_join(room, "bob", []) is False


def test_group_acl_takes_precedence_over_scope() -> None:
    resolver = RoomResolver()
    room = "grp:perun:demo:chat"
    resolver.set_members(room, ["alice"])
    assert resolver.can_join(room, "alice", ["grp:perun:*"]) is True
    assert resolver.can_join(room, "bob", ["grp:perun:*"]) is False


def test_group_scope_is_fallback_without_acl() -> None:
    resolver = RoomResolver()
    room = "grp:perun:demo:chat"
    assert resolver.can_join(room, "bob", ["grp:perun:*"]) is True
    assert resolver.can_join(room, "bob", ["grp:other:*"]) is False
    assert resolver.can_join(room, "bob", None) is False


def test_unknown_room_scope_is_denied() -> None:
    resolver = RoomResolver()
    with pytest.raises(ValueError):
        resolver.can_join("not-a-room", "alice", ["*"])
