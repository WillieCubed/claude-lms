"""Tests for CLI helpers that don't touch the network."""
import io
import os
import urllib.error

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

    assert out.getvalue() == "\x1b[3A\r\x1b[J"


def test_rewrite_menu_row_preserves_cursor_below_menu():
    out = io.StringIO()
    cli._rewrite_menu_row(out, row=2, rows=5, body="row body")

    assert out.getvalue() == "\x1b[3A\r\x1b[2Krow body\x1b[3B\r"


def test_warm_up_model_skips_post_when_already_loaded(monkeypatch):
    monkeypatch.setattr(cli, "model_details", lambda url: {"m": {"state": "loaded"}})
    posts = []
    monkeypatch.setattr(cli, "_post_json", lambda *a, **k: posts.append(a))
    notes = []

    assert cli.warm_up_model("http://lm", "m", notes.append) is True
    assert posts == []
    assert notes == []


def test_warm_up_model_posts_warmup_when_not_loaded(monkeypatch):
    monkeypatch.setattr(cli, "model_details", lambda url: {"m": {"state": "not-loaded"}})
    calls = []

    def fake_post(url, payload, *args, **kwargs):
        calls.append((url, payload))
        return {}

    monkeypatch.setattr(cli, "_post_json", fake_post)
    notes = []

    assert cli.warm_up_model("http://lm", "m", notes.append) is True
    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "http://lm/v1/chat/completions"
    assert payload["model"] == "m"
    assert payload["max_tokens"] == 1
    assert any("loading m" in note for note in notes)
    assert any("m ready" in note for note in notes)


def test_warm_up_model_warns_and_continues_on_failure(monkeypatch):
    monkeypatch.setattr(cli, "model_details", lambda url: {})

    def boom(*args, **kwargs):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(cli, "_post_json", boom)
    notes = []

    assert cli.warm_up_model("http://lm", "m", notes.append) is False
    assert any("could not preload m" in note for note in notes)


class _Stream:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def test_color_enabled_requires_tty_and_unset_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert cli._color_enabled(_Stream(tty=True)) is True
    assert cli._color_enabled(_Stream(tty=False)) is False


def test_color_enabled_respects_no_color_even_when_empty(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "")  # presence alone disables, per the spec
    assert cli._color_enabled(_Stream(tty=True)) is False


def test_paint_is_a_noop_without_color(monkeypatch):
    monkeypatch.setattr(cli, "_color_enabled", lambda stream: False)
    assert cli._paint("hi", "red", "bold") == "hi"


def test_paint_wraps_with_sgr_codes_when_enabled(monkeypatch):
    monkeypatch.setattr(cli, "_color_enabled", lambda stream: True)
    assert cli._paint("hi", "red", "bold") == "\x1b[31;1mhi\x1b[0m"


def test_format_menu_row_uses_reverse_video_without_color(monkeypatch):
    monkeypatch.setattr(cli, "_color_enabled", lambda stream: False)
    row = cli._format_menu_row("qwen", {}, None, selected=True, columns=80)
    assert row.startswith("\x1b[7m") and row.endswith("\x1b[0m")
    assert "❯ qwen" in row


def test_format_menu_row_colors_segments_when_enabled(monkeypatch):
    monkeypatch.setattr(cli, "_color_enabled", lambda stream: True)
    details = {"qwen": {"arch": "qwen3", "quant": "6bit", "state": "loaded"}}
    selected = cli._format_menu_row("qwen", details, "qwen", selected=True, columns=80)
    assert "\x1b[36;1m❯ \x1b[0m" in selected  # cyan+bold pointer
    assert "\x1b[32mloaded\x1b[0m" in selected  # green loaded mark
    assert "\x1b[36mdefault\x1b[0m" in selected  # cyan default mark
    # No reverse-video block in the colored path.
    assert "\x1b[7m" not in selected


def test_format_menu_row_falls_back_to_plain_when_too_narrow(monkeypatch):
    monkeypatch.setattr(cli, "_color_enabled", lambda stream: True)
    # A 10-column terminal forces truncation, so the safe plain path runs.
    row = cli._format_menu_row("a-very-long-model-name", {}, None, selected=False, columns=10)
    assert row.endswith("...")
    assert "\x1b[36m" not in row


def test_loaded_models_returns_every_loaded_id(monkeypatch):
    monkeypatch.setattr(
        cli,
        "model_details",
        lambda url: {
            "a": {"state": "loaded"},
            "b": {"state": "not-loaded"},
            "c": {"state": "loaded"},
        },
    )
    assert cli.loaded_models("http://lm") == ["a", "c"]


def test_loaded_models_empty_when_none_loaded(monkeypatch):
    monkeypatch.setattr(cli, "model_details", lambda url: {"a": {"state": "not-loaded"}})
    assert cli.loaded_models("http://lm") == []


def test_server_reachable_reflects_get_json(monkeypatch):
    monkeypatch.setattr(cli, "_get_json", lambda url, **k: {"data": []})
    assert cli.server_reachable("http://lm") is True
    monkeypatch.setattr(cli, "_get_json", lambda url, **k: None)
    assert cli.server_reachable("http://lm") is False


def test_no_models_message_distinguishes_empty_from_down(monkeypatch):
    monkeypatch.setattr(cli, "server_reachable", lambda url: True)
    assert "has no chat models" in cli.no_models_message("http://lm")
    monkeypatch.setattr(cli, "server_reachable", lambda url: False)
    assert "not reachable" in cli.no_models_message("http://lm")


def test_doctor_lists_every_loaded_model(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli, "list_models", lambda url: ["a", "b", "c"])
    monkeypatch.setattr(
        cli,
        "model_details",
        lambda url: {
            "a": {"state": "loaded"},
            "b": {"state": "not-loaded"},
            "c": {"state": "loaded"},
        },
    )
    monkeypatch.setattr(cli, "configured_default", lambda: None)
    monkeypatch.delenv("CLL_MODEL", raising=False)

    cli.run_doctor("http://lm")
    err = capsys.readouterr().err
    assert "loaded models" in err
    assert "a, c" in err
    assert "a  (loaded)" in err and "c  (loaded)" in err
