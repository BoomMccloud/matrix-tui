"""Unit tests for sandbox utility functions."""

from matrix_agent.sandbox import _container_name


def test_container_name_alphanumeric():
    """Test with simple alphanumeric input and allowed characters."""
    assert _container_name("room123") == "sandbox-room123"
    assert _container_name("Room_456") == "sandbox-Room_456"
    assert _container_name("room.name") == "sandbox-room.name"
    assert _container_name("room-name") == "sandbox-room-name"
    assert _container_name("123.456-789_abc") == "sandbox-123.456-789_abc"


def test_container_name_special_chars():
    """Test with special characters that should be replaced by dashes."""
    # Matrix room IDs usually look like !hash:server.tld
    assert _container_name("!room:example.com") == "sandbox-room-example.com"
    # Multiple special chars in a row should be collapsed if they are adjacent? 
    # Actually re.sub(r"[^a-zA-Z0-9_.-]", "-", chat_id) replaces each char with a dash.
    assert _container_name("abc#$%123") == "sandbox-abc---123"


def test_container_name_stripping():
    """Test that leading/trailing dashes are stripped from the slug."""
    assert _container_name("!!!room!!!") == "sandbox-room"
    assert _container_name("###") == "sandbox-"


def test_container_name_empty():
    """Test with empty string."""
    assert _container_name("") == "sandbox-"


def test_container_name_long():
    """Test with long input."""
    long_id = "a" * 100
    assert _container_name(long_id) == f"sandbox-{long_id}"
