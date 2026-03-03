"""Unit tests for sandbox utility functions."""

from matrix_agent.sandbox import _container_name, _strip_ansi


def test_container_name_alphanumeric():
    """Test with simple alphanumeric input."""
    assert _container_name("room123") == "sandbox-room123"
    assert _container_name("Room_456") == "sandbox-Room_456"


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


def test_strip_ansi_plain():
    """Test plain text passthrough."""
    assert _strip_ansi("hello world") == "hello world"
    assert _strip_ansi("123\n456") == "123\n456"


def test_strip_ansi_colors():
    """Test stripping basic ANSI color codes."""
    assert _strip_ansi("\x1b[31mred\x1b[0m") == "red"
    assert _strip_ansi("\x1b[1;32mgreen bold\x1b[0m") == "green bold"
    assert _strip_ansi("\x1b[44;37mwhite on blue\x1b[0m") == "white on blue"


def test_strip_ansi_sequences():
    """Test stripping various ANSI escape sequences (cursor, clear, etc.)."""
    # [H: cursor home, [2J: clear screen
    assert _strip_ansi("\x1b[H\x1b[2JReady") == "Ready"
    # [K: erase in line
    assert _strip_ansi("Loading...\x1b[KDone") == "Loading...Done"
    # [1A: cursor up
    assert _strip_ansi("Line 1\n\x1b[1ALine 2") == "Line 1\nLine 2"


def test_strip_ansi_empty():
    """Test with empty string."""
    assert _strip_ansi("") == ""


def test_strip_ansi_complex():
    """Test complex/nested-ish sequences."""
    text = "Progress: \x1b[32m[====\x1b[31m>\x1b[32m    ]\x1b[0m 50%"
    assert _strip_ansi(text) == "Progress: [====>    ] 50%"
