"""Tests for state.json persistence."""

import json
import os
import uuid

import pytest

ROOM_ID = "!test-room:matrix.org"


def read_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def write_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def get_room_messages(state: dict) -> list[str]:
    history = state.get("history", {}).get(ROOM_ID, [])
    return [m.get("content", "") for m in history]


@pytest.fixture
def state_path(tmp_path):
    return str(tmp_path / "state.json")


def test_random_message_not_in_empty_state(state_path):
    """A fresh random string must not exist in an empty state file."""
    random_msg = str(uuid.uuid4())
    state = read_state(state_path)
    assert random_msg not in get_room_messages(state)


def test_random_message_persisted_after_write(state_path):
    """After writing a random string to state.json, it must be readable."""
    random_msg = str(uuid.uuid4())

    # Confirm not there yet
    state = read_state(state_path)
    assert random_msg not in get_room_messages(state)

    # Write it
    state.setdefault("history", {})[ROOM_ID] = [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": random_msg},
    ]
    write_state(state_path, state)

    # Read back — must be present
    state2 = read_state(state_path)
    assert random_msg in get_room_messages(state2)
