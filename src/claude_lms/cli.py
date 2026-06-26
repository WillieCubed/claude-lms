"""``cll`` -- launch Claude Code against a local model served by LM Studio.

Resolves a model, starts a per-session normalizing proxy on an ephemeral local port
(so concurrent sessions never collide and nothing is left running), then execs the
``claude`` CLI pointed at that proxy. All Claude-bound traffic stays on localhost.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import __version__, completions
from .proxy import make_server

DEFAULT_LMSTUDIO_URL = "http://localhost:1234"


def _eprint(*args) -> None:
    print(*args, file=sys.stderr)


_SGR = {"red": "31", "green": "32", "yellow": "33", "cyan": "36", "dim": "2", "bold": "1"}


def _color_enabled(stream) -> bool:
    """True when ``stream`` is a terminal and the user hasn't set ``NO_COLOR``."""
    # NO_COLOR (https://no-color.org): any value, including empty, disables color.
    return stream.isatty() and os.environ.get("NO_COLOR") is None


def _paint(text: str, *names: str, stream=None) -> str:
    """Wrap ``text`` in ANSI styles, or return it unchanged when color is off.

    Each name is either a key in ``_SGR`` or a raw SGR parameter string (e.g. a 256-color
    ``"38;5;208"``), which passes through unchanged.
    """
    stream = stream or sys.stderr
    if not _color_enabled(stream):
        return text
    codes = ";".join(_SGR.get(name, name) for name in names)
    return f"\x1b[{codes}m{text}\x1b[0m"


# 256-color hues for distinguishing model vendors, chosen to avoid the colors that carry
# meaning elsewhere: cyan (selection), green (loaded), yellow (default), red (errors).
_VENDOR_256 = (33, 141, 170, 208, 178, 211)  # blue, purple, magenta, orange, gold, pink


def _vendor_color(model: str) -> str:
    """A stable 256-color SGR code for a model, keyed on its vendor/family.

    Groups by the leading letters of the namespace (``qwen/qwen3.6`` and
    ``qwen2.5-0.5b`` both map to ``qwen``), so a family shares one color across runs.
    """
    head = model.split("/", 1)[0]
    key = ""
    for char in head:
        if not char.isalpha():
            break
        key += char
    key = (key or head).lower()
    digest = 0
    for char in key:  # polynomial hash: stable across runs and spreads better than a sum
        digest = (digest * 31 + ord(char)) & 0xFFFFFFFF
    return f"38;5;{_VENDOR_256[digest % len(_VENDOR_256)]}"


def _say_error(msg: str) -> None:
    _eprint(_paint(msg, "red"))


def _say_warn(msg: str) -> None:
    _eprint(_paint(msg, "yellow"))


def _say_ok(msg: str) -> None:
    _eprint(_paint(msg, "green"))


def _say_dim(msg: str) -> None:
    _eprint(_paint(msg, "dim"))


def _get_json(url: str, timeout: float = 5.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _post_json(url: str, payload: dict, timeout: float = 600.0):
    """POST a JSON body and return the decoded JSON response.

    Unlike ``_get_json``, errors propagate so callers can tell a failed request from an
    empty body.
    """
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def server_reachable(base_url: str) -> bool:
    """True if LM Studio answers at all, even when it has no models available."""
    return _get_json(base_url.rstrip("/") + "/v1/models") is not None


def no_models_message(base_url: str) -> str:
    """The message to show when no chat models are available.

    Distinguishes a server that is up but empty from one that isn't running, so the user
    knows whether to download a model or start LM Studio.
    """
    if server_reachable(base_url):
        return (
            f"cll: LM Studio is running at {base_url} but has no chat models available.\n"
            "     Download or load a model in LM Studio, then try again."
        )
    return (
        f"cll: LM Studio is not reachable at {base_url}.\n"
        "     Start its local server (e.g. `lms server start`) and make sure a model is available."
    )


# --- persistent config (the cll-managed default model) ----------------------------

def _config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "claude-lms", "config.json")


def load_config() -> dict:
    try:
        with open(_config_path()) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(config: dict) -> None:
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")


def configured_default() -> str | None:
    return load_config().get("default_model")


def effective_default() -> str | None:
    """The default model in force: the ``CLL_MODEL`` env override, else the config."""
    return os.environ.get("CLL_MODEL") or configured_default()


# --- LM Studio queries -------------------------------------------------------------

def list_models(base_url: str) -> list[str]:
    """Return downloadable chat model ids from LM Studio (embeddings excluded)."""
    data = _get_json(base_url.rstrip("/") + "/v1/models")
    if not data:
        return []
    ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
    return [i for i in ids if "embed" not in i.lower()]


def model_details(base_url: str) -> dict:
    """Map model id -> {arch, quant, state, ctx} from LM Studio's ``/api/v0/models``."""
    data = _get_json(base_url.rstrip("/") + "/api/v0/models") or {}
    details = {}
    for model in data.get("data", []):
        mid = model.get("id", "")
        if mid and "embed" not in mid.lower():
            details[mid] = {
                "arch": model.get("arch"),
                "quant": model.get("quantization"),
                "state": model.get("state"),
                "ctx": model.get("max_context_length"),
            }
    return details


def loaded_models(base_url: str) -> list[str]:
    """The ids of every model currently loaded in LM Studio (it can hold several)."""
    return [
        mid for mid, detail in model_details(base_url).items() if detail.get("state") == "loaded"
    ]


def warm_up_model(base_url: str, model: str, notify) -> bool:
    """Ensure ``model`` is loaded in LM Studio before launching Claude.

    Returns ``True`` if the model is resident (already loaded, or loaded by this warmup).
    Best-effort: on error or timeout it reports via ``notify`` and returns ``False``,
    leaving the caller free to launch Claude and let LM Studio load it on the first request.
    """
    if model_details(base_url).get(model, {}).get("state") == "loaded":
        return True
    notify(_paint(f"cll: loading {model} into LM Studio (this can take a moment)...", "yellow"))
    started = time.monotonic()
    try:
        # A single user message needs no normalizing, so we can hit LM Studio directly
        # (the proxy isn't running yet). The response means the model finished loading.
        _post_json(
            base_url + "/v1/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": "ok"}],
                "max_tokens": 1,
                "stream": False,
            },
        )
    except (OSError, ValueError) as exc:  # OSError covers URLError and socket timeouts
        notify(_paint(
            f"cll: could not preload {model} ({exc}); LM Studio will load it on first use.",
            "yellow",
        ))
        return False
    elapsed = time.monotonic() - started
    notify(_paint(f"cll: {model} ready{f' ({elapsed:.0f}s)' if elapsed >= 1 else ''}.", "green"))
    return True


def ensure_lmstudio(base_url: str) -> "list[str] | None":
    """Return available chat models, starting the LM Studio server if needed.

    Returns the model ids (embeddings excluded), or ``None`` if the server is
    unreachable or serves no chat models. Returning the list lets callers avoid a
    second ``/v1/models`` fetch.
    """
    models = list_models(base_url)
    if models:
        return models
    if shutil.which("lms"):
        subprocess.run(
            ["lms", "server", "start"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        models = list_models(base_url)
        if models:
            return models
    return None


# --- model selection ---------------------------------------------------------------

def match_model(requested: str, available: list[str]) -> tuple[str | None, list[str]]:
    """Resolve a possibly-partial model id against available ids.

    Returns ``(resolved, candidates)``: an exact id or unique substring match yields
    ``(id, [])``; an ambiguous substring yields ``(None, matches)``; no match yields
    ``(None, [])``.
    """
    if requested in available:
        return requested, []
    matches = [m for m in available if requested.lower() in m.lower()]
    if len(matches) == 1:
        return matches[0], []
    return None, matches


def _annotate(model: str, details: dict, default: str | None) -> str:
    detail = details.get(model, {})
    bits = [b for b in (detail.get("arch"), detail.get("quant")) if b]
    meta = f"  [{' · '.join(bits)}]" if bits else ""
    marks = []
    if detail.get("state") == "loaded":
        marks.append("loaded")
    if model == default:
        marks.append("default")
    mark = f"  ({', '.join(marks)})" if marks else ""
    return f"{model}{meta}{mark}"


_FALLBACK = object()  # sentinel: interactive picker unavailable -> use the numbered menu
_PICKER_TITLE = "Select a model"
_PICKER_HINT = "  (↑/↓ or j/k · Enter to choose · q to cancel)"
_PICKER_HEADER = _PICKER_TITLE + _PICKER_HINT


def _key_action(key: str, selected: int, count: int) -> tuple[int, str]:
    """Map a key to ``(new_selected, action)``.

    ``action`` is one of ``move``, ``select``, ``cancel``, ``ignore``. Kept pure so the
    navigation logic is unit-testable without a terminal.
    """
    if key in ("\r", "\n"):
        return selected, "select"
    if key in ("q", "\x1b", "\x03"):  # q, Esc, Ctrl-C
        return selected, "cancel"
    if key in ("up", "k"):
        return (selected - 1) % count, "move"
    if key in ("down", "j"):
        return (selected + 1) % count, "move"
    return selected, "ignore"


def _read_key(fd: int) -> str:
    """Read one keypress from ``fd`` (raw/unbuffered); map arrow escapes to up/down.

    Reads via ``os.read`` rather than buffered ``sys.stdin`` so ``select`` and the reads
    see the same bytes — otherwise an arrow's ``\\x1b[B`` gets slurped into Python's
    buffer and the trailing ``[B`` is invisible to ``select``, misreading it as Esc.
    """
    data = os.read(fd, 1)
    if data != b"\x1b":
        return data.decode("utf-8", "ignore")
    # Possible escape sequence: only treat as an arrow if more bytes are actually waiting.
    if not select.select([fd], [], [], 0.05)[0]:
        return "\x1b"
    seq = os.read(fd, 2)
    if len(seq) == 2 and seq[0:1] == b"[":
        return {b"A": "up", b"B": "down"}.get(seq[1:2], "ignore")
    return "\x1b"


def _fit_terminal_line(line: str, columns: int) -> str:
    """Keep a picker row on one terminal line so redraw cursor math stays valid."""
    max_columns = max(columns - 1, 1)
    if len(line) <= max_columns:
        return line
    if max_columns <= 3:
        return line[:max_columns]
    return line[: max_columns - 3] + "..."


# The picker helpers share one convention: between operations the cursor rests on
# the line just below the menu block. _render_menu leaves it there, _rewrite_menu_row
# returns it there, and _clear_menu moves up to the header and erases downward.


def _terminal_columns() -> int:
    return shutil.get_terminal_size(fallback=(80, 24)).columns


def _clear_menu(out, rows: int) -> None:
    """Erase the picker block, leaving the cursor where the header started."""
    if rows <= 0:
        return
    out.write(f"\x1b[{rows}A\r\x1b[J")  # up to the header, then erase to end of display


def _format_menu_row(model: str, details: dict, default: str | None, selected: bool, columns: int) -> str:
    marker = "❯ " if selected else "  "
    plain = f"{marker}{_annotate(model, details, default)}"

    # No color (or NO_COLOR), or the row needs truncating: keep the safe reverse-video
    # rendering, which only ever wraps already-measured plain text.
    if not _color_enabled(sys.stderr) or len(plain) > max(columns - 1, 1):
        line = _fit_terminal_line(plain, columns)
        return f"\x1b[7m{line}\x1b[0m" if selected else line

    # Styled: the plain row fits, so painting segments (same visible width) is safe.
    # Each property gets a consistent color so the menu reads column by column.
    detail = details.get(model, {})
    pointer = _paint("❯ ", "cyan", "bold") if selected else "  "
    name = _paint(model, "cyan", "bold") if selected else _paint(model, _vendor_color(model))

    props = []
    if detail.get("arch"):
        props.append(_paint(detail["arch"], "38;5;109"))  # steel blue
    if detail.get("quant"):
        props.append(_paint(detail["quant"], "38;5;173"))  # copper
    meta = f"{_paint('  [', 'dim')}{_paint(' · ', 'dim').join(props)}{_paint(']', 'dim')}" if props else ""

    marks = []
    if detail.get("state") == "loaded":
        marks.append(_paint("loaded", "green"))
    if model == default:
        marks.append(_paint("default", "yellow"))
    tail = f"{_paint('  (', 'dim')}{_paint(', ', 'dim').join(marks)}{_paint(')', 'dim')}" if marks else ""
    return f"{pointer}{name}{meta}{tail}"


def _rewrite_menu_row(out, row: int, rows: int, body: str) -> None:
    """Rewrite one menu row, preserving the cursor below the menu."""
    up = rows - row
    out.write(f"\x1b[{up}A\r\x1b[2K{body}\x1b[{up}B\r")


def _render_menu(out, models, details, default, selected, redraw) -> int:
    rows = len(models) + 1
    if redraw:
        _clear_menu(out, rows)
    columns = _terminal_columns()
    if _color_enabled(sys.stderr) and len(_PICKER_HEADER) <= max(columns - 1, 1):
        header = _paint(_PICKER_TITLE, "bold") + _paint(_PICKER_HINT, "dim")
    else:
        header = _fit_terminal_line(_PICKER_HEADER, columns)
    out.write(f"\r\x1b[2K{header}\n")
    for index, model in enumerate(models):
        out.write(f"\r\x1b[2K{_format_menu_row(model, details, default, index == selected, columns)}\n")
    out.flush()
    return columns


def _update_menu_selection(out, models, details, default, previous, selected, columns) -> int:
    current_columns = _terminal_columns()
    if current_columns != columns:
        return _render_menu(out, models, details, default, selected, redraw=True)

    rows = len(models) + 1
    _rewrite_menu_row(
        out,
        previous + 1,
        rows,
        _format_menu_row(models[previous], details, default, False, columns),
    )
    _rewrite_menu_row(
        out,
        selected + 1,
        rows,
        _format_menu_row(models[selected], details, default, True, columns),
    )
    out.flush()
    return columns


def _interactive_pick(models, details, default):
    """Arrow-key picker using only the standard library.

    Returns the chosen model, ``None`` if cancelled, or ``_FALLBACK`` if a TTY picker
    can't be set up (e.g. no ``termios``).
    """
    try:
        import termios
        import tty
    except ImportError:
        return _FALLBACK
    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except (termios.error, OSError, ValueError):
        return _FALLBACK
    selected = models.index(default) if default in models else 0
    out = sys.stderr
    out.write("\x1b[?25l")  # hide cursor
    try:
        tty.setcbreak(fd)
        columns = _render_menu(out, models, details, default, selected, redraw=False)
        while True:
            new_selected, action = _key_action(_read_key(fd), selected, len(models))
            if action == "select":
                return models[selected]
            if action == "cancel":
                return None
            if action == "move":
                previous = selected
                selected = new_selected
                columns = _update_menu_selection(
                    out, models, details, default, previous, selected, columns
                )
    except KeyboardInterrupt:
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        _clear_menu(out, len(models) + 1)
        out.write("\x1b[?25h\r")  # restore cursor
        out.flush()


def _numbered_pick(models, details, default):
    """Plain numbered menu — the fallback when there is no interactive terminal."""
    _eprint("Select a model:")
    for index, model in enumerate(models, 1):
        _eprint(f"  {index}) {_annotate(model, details, default)}")
    while True:
        try:
            choice = input("# ").strip()
        except EOFError:
            return None
        if not choice:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(models):
            return models[int(choice) - 1]
        _say_error("Invalid selection.")


def pick_model(
    models: list[str], details: dict | None = None, default: str | None = None
) -> str | None:
    """Choose a model: an arrow-key picker on a TTY, else a numbered menu."""
    if not models:
        return None
    details = details or {}
    if sys.stdin.isatty() and sys.stderr.isatty():
        try:
            result = _interactive_pick(models, details, default)
        except Exception:
            result = _FALLBACK
        if result is not _FALLBACK:
            return result
    return _numbered_pick(models, details, default)


def resolve_model(args, base_url: str, models: list[str]) -> str | None:
    """Pick the model to use, in priority order.

    1. ``-m/--model`` (exact id or unique substring)  2. ``--pick`` menu
    3. ``$CLL_MODEL`` (one-off env override)  4. the persisted default
    (``cll set-default``)  5. the loaded model when exactly one is resident (if several
    are loaded on a TTY, a menu, so we don't guess)  6. the only model, if there is
    exactly one  7. otherwise an interactive menu.
    """
    def menu() -> str | None:
        return pick_model(models, model_details(base_url), effective_default())

    if args.model:
        resolved, candidates = match_model(args.model, models)
        if resolved:
            return resolved
        if candidates:
            _eprint(f"cll: '{args.model}' matches multiple models:")
            for candidate in candidates:
                _eprint(f"  - {candidate}")
            return None
        if models:
            _eprint(f"cll: model '{args.model}' not found in LM Studio.")
            return menu()
        return args.model
    if args.pick:
        return menu()
    if os.environ.get("CLL_MODEL"):
        return os.environ["CLL_MODEL"]
    if configured_default():
        return configured_default()
    loaded = loaded_models(base_url)
    if len(loaded) == 1:
        return loaded[0]
    if len(loaded) > 1 and sys.stdin.isatty():
        # Several models are resident; don't silently guess. Let the user choose
        # (loaded ones are marked in the menu). cll set-default pins one for next time.
        return menu()
    if loaded:
        return loaded[0]
    if len(models) == 1:
        return models[0]
    return menu()


# --- informational subcommands -----------------------------------------------------

def print_models_table(base_url: str) -> int:
    """Print a human-readable table of available models and exit."""
    models = ensure_lmstudio(base_url)
    if models is None:
        _say_error(no_models_message(base_url))
        return 1
    details = model_details(base_url)
    default = effective_default()
    print(f"{'MODEL':36} {'ARCH':12} {'QUANT':9} STATE")
    for model in models:
        detail = details.get(model, {})
        flags = []
        if detail.get("state") == "loaded":
            flags.append(_paint("loaded", "green", stream=sys.stdout))
        if model == default:
            flags.append(_paint("default", "cyan", stream=sys.stdout))
        print(
            f"{model:36} {(detail.get('arch') or '?'):12} "
            f"{(detail.get('quant') or '?'):9} {' '.join(flags)}".rstrip()
        )
    return 0


def run_doctor(base_url: str) -> int:
    """Print an environment checklist and return 0 if everything looks ready."""
    claude = shutil.which("claude")
    lms = shutil.which("lms")
    models = list_models(base_url)
    reachable = bool(models)
    loaded = loaded_models(base_url)
    env_default = os.environ.get("CLL_MODEL")
    cfg_default = configured_default()

    checks = [
        (bool(claude), f"claude CLI            {claude or 'not found — https://claude.com/claude-code'}"),
        (True, f"lms CLI               {lms or 'not found (optional; auto-starts the server)'}"),
        (reachable, f"LM Studio server      {'reachable at ' + base_url if reachable else 'NOT reachable at ' + base_url}"),
    ]
    if loaded:
        label = "loaded models" if len(loaded) > 1 else "loaded model"
        checks.append((True, f"{label:<22}{', '.join(loaded)}"))
    if env_default:
        checks.append((True, f"CLL_MODEL (env)       {env_default}"))
    if cfg_default:
        checks.append((True, f"default (config)      {cfg_default}"))

    for passed, message in checks:
        mark = _paint("✓", "green") if passed else _paint("✗", "red")
        _eprint(f"  {mark} {message}")
    if models:
        loaded_set = set(loaded)
        _eprint(f"  · available models    {len(models)}")
        for model in models:
            tag = _paint("  (loaded)", "green") if model in loaded_set else ""
            _eprint(f"      {model}{tag}")

    ready = all(passed for passed, _ in checks)
    _eprint("")
    if ready:
        _say_ok("cll: ready.")
    else:
        _say_error("cll: not ready — resolve the ✗ items above.")
    return 0 if ready else 1


EPILOG = """\
commands:
  cll                              launch Claude Code on the default model
  cll -m qwen3.6                   launch with a model (exact id or unique substring)
  cll --pick                       launch after choosing from an interactive menu
  cll models                       show a table of available models
  cll list-models                  print bare model ids (for scripts/completion)
  cll set-default <model>          save the default model
  cll clear-default                clear the saved default
  cll doctor                       check your environment
  cll install-completion [shell]   install zsh/bash tab-completion
  cll install-completion --uninstall   remove it again

Unrecognized launch arguments are forwarded to claude, e.g.:
  cll --dangerously-skip-permissions -p "explain this repo"

model resolution order:
  -m/--model · --pick · $CLL_MODEL · saved default · loaded model · only model · menu

environment:
  CLL_MODEL        one-off default-model override (beats the saved default)
  LM_STUDIO_URL    LM Studio base URL (default http://localhost:1234)
  CLL_AUTH_TOKEN   dummy auth token sent to the endpoint (default lm-studio)
  NO_COLOR         set to disable colored output
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cll",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Launch Claude Code against a local LM Studio model. "
        "Unrecognized arguments are forwarded to `claude`.",
        epilog=EPILOG,
    )
    parser.add_argument(
        "-m", "--model", help="model id, exact or unique substring (e.g. qwen3.6)"
    )
    parser.add_argument(
        "--pick", action="store_true", help="choose a model from an interactive menu"
    )
    parser.add_argument(
        "--lmstudio-url",
        default=os.environ.get("LM_STUDIO_URL", DEFAULT_LMSTUDIO_URL),
        help="LM Studio base URL (default: %(default)s)",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


COMMANDS = (
    "models",
    "list-models",
    "doctor",
    "set-default",
    "clear-default",
    "install-completion",
)


def _run_command(command: str, rest: list[str]) -> int:
    """Run a standalone subcommand (no claude launch) and return its exit code."""
    base_url = os.environ.get("LM_STUDIO_URL", DEFAULT_LMSTUDIO_URL).rstrip("/")
    if command == "models":
        return print_models_table(base_url)
    if command == "list-models":
        models = ensure_lmstudio(base_url)
        if models is None:
            _say_error(no_models_message(base_url))
            return 1
        for model in models:
            print(model)
        return 0
    if command == "doctor":
        return run_doctor(base_url)
    if command == "set-default":
        if not rest:
            _say_error("cll: 'set-default' needs a model id, e.g. cll set-default qwen/qwen3.6-27b")
            return 1
        save_config({**load_config(), "default_model": rest[0]})
        _say_ok(f"cll: default model set to '{rest[0]}'  ({_config_path()})")
        return 0
    if command == "clear-default":
        config = load_config()
        config.pop("default_model", None)
        save_config(config)
        _say_ok("cll: saved default cleared")
        return 0
    if command == "install-completion":
        uninstall = any(token in ("--uninstall", "-u") for token in rest)
        shell = next((t for t in rest if not t.startswith("-")), None) or completions.detect_shell()
        if shell not in ("zsh", "bash"):
            _say_error(
                f"cll: couldn't detect a supported shell (got {shell!r}).\n"
                "     Run: cll install-completion zsh   (or bash, plus --uninstall to remove)"
            )
            return 1
        for line in (completions.uninstall if uninstall else completions.install)(shell):
            _eprint(f"cll: {line}")
        if uninstall:
            _say_ok(f"cll: {shell} completion removed — restart your shell (e.g. `exec {shell}`).")
        else:
            _say_ok(f"cll: {shell} completion installed — restart your shell (e.g. `exec {shell}`).")
            _say_dim("cll: (remove later with: cll install-completion --uninstall)")
        return 0
    return 2  # unreachable: command was validated against COMMANDS


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in COMMANDS:
        return _run_command(raw[0], raw[1:])

    parser = build_parser()
    args, claude_args = parser.parse_known_args(raw)
    base_url = args.lmstudio_url.rstrip("/")

    # --- launch claude ---
    if shutil.which("claude") is None:
        _say_error(
            "cll: 'claude' (Claude Code) not found on PATH.\n"
            "     Install it from https://claude.com/claude-code"
        )
        return 127

    models = ensure_lmstudio(base_url)
    if models is None:
        _say_error(no_models_message(base_url))
        return 1

    auto_resolved = not args.model and not args.pick
    model = resolve_model(args, base_url, models)
    if not model:
        _say_error("cll: no model selected.")
        return 1
    if models and model not in models:
        if model == configured_default():
            _say_warn(
                f"cll: your saved default '{model}' isn't available in LM Studio right now.\n"
                "     Pick another below, or run `cll set-default <model>` to change it."
            )
        elif model == os.environ.get("CLL_MODEL"):
            _say_warn(f"cll: CLL_MODEL '{model}' isn't available in LM Studio. Pick another below.")
        else:
            _say_warn(f"cll: model '{model}' isn't available in LM Studio. Pick another below.")
        model = pick_model(models, model_details(base_url), effective_default())
        if not model:
            return 1

    warm_up_model(base_url, model, _eprint)

    upstream = urlsplit(base_url)
    server = make_server(
        "127.0.0.1", 0, upstream.hostname or "localhost", upstream.port or 1234
    )
    host, port = server.server_address
    proxy_url = f"http://{host}:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = proxy_url
    env["ANTHROPIC_AUTH_TOKEN"] = os.environ.get("CLL_AUTH_TOKEN", "lm-studio")
    for var in (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ):
        env[var] = model
    # Don't let a stored cloud key shadow the local endpoint.
    env.pop("ANTHROPIC_API_KEY", None)

    _eprint(
        f"cll: Claude Code -> {_paint(model, 'cyan', 'bold')}  via LM Studio {base_url}  "
        f"{_paint(f'(normalizer {proxy_url})', 'dim')}"
    )
    if auto_resolved and len(models) > 1:
        _say_dim("cll: (cll --pick to choose another · cll set-default to pin one)")

    # Let the child own terminal signals (Ctrl-C); keep the proxy alive until it exits.
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        completed = subprocess.run(["claude", *claude_args], env=env)
        return completed.returncode
    finally:
        signal.signal(signal.SIGINT, previous)
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
