"""Tests for CLI helpers that don't touch the network."""
import io
import os

from claude_lms import cli
from claude_lms.cli import match_model

AVAILABLE = [
    "qwen/qwen3.6-35b-a3b",
    "qwen/qwen3.5-35b-a3b",
    "openai/gpt-oss-20b",
    "mistralai/magistral-small-2509",
]


def test_exact_id_matches():
    assert match_model("openai/gpt-oss-20b", AVAILABLE) == ("openai/gpt-oss-20b", [])


def test_unique_substring_resolves_to_full_id():
    assert match_model("qwen3.6", AVAILABLE) == ("qwen/qwen3.6-35b-a3b", [])


def test_case_insensitive_substring():
    assert match_model("GPT-OSS", AVAILABLE) == ("openai/gpt-oss-20b", [])


def test_ambiguous_substring_returns_candidates():
    resolved, candidates = match_model("qwen", AVAILABLE)
    assert resolved is None
    assert candidates == ["qwen/qwen3.6-35b-a3b", "qwen/qwen3.5-35b-a3b"]


def test_no_match_returns_nothing():
    assert match_model("llama", AVAILABLE) == (None, [])


def test_empty_available_is_no_match():
    assert match_model("qwen3.6", []) == (None, [])


def test_set_and_clear_default_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("CLL_MODEL", raising=False)
    assert cli.configured_default() is None
    assert cli.main(["set-default", "qwen/qwen3.6-27b"]) == 0
    assert cli.configured_default() == "qwen/qwen3.6-27b"
    assert cli.effective_default() == "qwen/qwen3.6-27b"
    assert cli.main(["clear-default"]) == 0
    assert cli.configured_default() is None


def test_env_overrides_configured_default(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cli.save_config({"default_model": "config-model"})
    monkeypatch.setenv("CLL_MODEL", "env-model")
    assert cli.effective_default() == "env-model"
    monkeypatch.delenv("CLL_MODEL", raising=False)
    assert cli.effective_default() == "config-model"


def _read_key_from(data: bytes) -> str:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, data)
    os.close(write_fd)
    try:
        return cli._read_key(read_fd)
    finally:
        os.close(read_fd)


def test_read_key_maps_arrow_escapes():
    # The bug that motivated reading via os.read: arrows must not be misread as Esc.
    assert _read_key_from(b"\x1b[A") == "up"
    assert _read_key_from(b"\x1b[B") == "down"


def test_read_key_plain_and_lone_escape():
    assert _read_key_from(b"q") == "q"
    assert _read_key_from(b"\r") == "\r"
    assert _read_key_from(b"\x1b") == "\x1b"


def test_key_action_navigation_and_wrap():
    assert cli._key_action("down", 0, 3) == (1, "move")
    assert cli._key_action("up", 0, 3) == (2, "move")  # wraps to the end
    assert cli._key_action("j", 2, 3) == (0, "move")  # wraps to the start
    assert cli._key_action("k", 0, 3) == (2, "move")
    assert cli._key_action("\r", 1, 3) == (1, "select")
    assert cli._key_action("q", 1, 3) == (1, "cancel")
    assert cli._key_action("\x1b", 1, 3) == (1, "cancel")
    assert cli._key_action("x", 1, 3) == (1, "ignore")


def test_fit_terminal_line_leaves_room_to_avoid_wrapping():
    assert cli._fit_terminal_line("abcdef", 10) == "abcdef"
    assert cli._fit_terminal_line("abcdefghij", 10) == "abcdef..."
    assert len(cli._fit_terminal_line("abcdefghij", 10)) == 9


def test_clear_menu_erases_rows_and_returns_to_start():
    out = io.StringIO()
    cli._clear_menu(out, 3)

    assert out.getvalue() == "\x1b[3A\r\x1b[2K\x1b[1B\r\x1b[2K\x1b[1B\r\x1b[2K\x1b[2A"


def test_rewrite_menu_row_preserves_cursor_below_menu():
    out = io.StringIO()
    cli._rewrite_menu_row(out, row=2, rows=5, body="row body")

    assert out.getvalue() == "\x1b[3A\r\x1b[2Krow body\x1b[3B\r"
