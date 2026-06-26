# Architecture

`claude-lms` is two small pieces with one job: let Claude Code talk to a local model in
LM Studio that would otherwise reject its requests.

## The pieces

- **`cll`** (`src/claude_lms/cli.py`) — the launcher. Resolves which model to use, warms it
  up in LM Studio, starts the proxy, and runs the `claude` CLI pointed at it.
- **The normalizing proxy** (`src/claude_lms/proxy.py`) — a tiny reverse proxy between
  Claude Code and LM Studio that rewrites each request so any chat template accepts it.

## Request flow

```
Claude Code (claude CLI)
   │   Anthropic /v1/messages         (ANTHROPIC_BASE_URL points at the proxy)
   ▼
claude-lms proxy                       runs in-process inside cll, on an
   │   fold stray system messages      ephemeral 127.0.0.1 port
   ▼
LM Studio /v1/messages                 (its native Anthropic-compatible endpoint)
   │
   ▼
local model (e.g. qwen3.6)
```

Responses stream straight back the other way, token by token.

## What the proxy changes

Claude Code sends a top-level `system` field **and**, often, a second `system`-role
message inside `messages` (for example, a `SessionStart` hook's output). The rendered
order becomes `system → user → system`. Strict chat templates — notably Qwen3.5/3.6 —
reject any system message that isn't first.

The proxy folds every non-leading system message into the single leading `system` block,
so the order becomes `system → user`, which every template accepts. Nothing else is
altered, and the response is streamed through unbuffered. See the
[README](../../README.md#why-this-exists) for the exact error and root cause.

## Lifecycle

Before starting the proxy, `cll` warms up the chosen model: if it isn't already loaded, a
single minimal request asks LM Studio to load it, so the first real request doesn't pay the
cold-start load. The warm-up is best-effort; if it fails, `cll` notes it and continues.

`cll` then starts the proxy on an ephemeral port for the session only, runs `claude` as a
child process, and tears the proxy down on exit. Nothing is left running, and concurrent
`cll` sessions get their own ports, so they never collide. The proxy runs in-process and
shares the terminal with the Claude Code TUI, so it never writes to stderr — diagnostics
go to a log file behind the `CLAUDE_LMS_DEBUG` environment variable.

## Performance

The proxy is thin: per request it parses the body, folds stray system messages only when
present, and forwards — single-digit milliseconds. It streams responses with
`TCP_NODELAY` and `read1`, so tokens appear the instant LM Studio emits them.

Latency you actually feel is almost entirely the **local model** generating tokens
(reasoning/thinking), not the proxy. To speed things up:

- use a lighter model — `cll -m gpt-oss-20b`;
- lower Claude Code's thinking effort;
- load the model with a smaller context — `lms load <model> -c 65536`;
- enable Flash Attention / KV-cache quantization and full GPU offload in LM Studio.

## Files

| Path | Role |
| --- | --- |
| `src/claude_lms/cli.py` | the `cll` launcher, subcommands, model resolution, and config |
| `src/claude_lms/proxy.py` | `normalize()`, the HTTP handler, and the standalone `claude-lms-proxy` entry point |
| `src/claude_lms/completions.py` | embedded zsh/bash completion scripts + the `cll install-completion` installer |
| `tests/` | `normalize()`, CLI model matching/keys, config, completion install, proxy resilience + forwarding |

`cll` has two modes: **launch** (flags like `-m`/`--pick`, with unknown args forwarded to
`claude`) and **subcommands** (`models`, `list-models`, `doctor`, `set-default`,
`clear-default`, `install-completion` — standalone actions that exit). The persistent
default model is stored in `~/.config/claude-lms/config.json` (written by
`cll set-default`); the `CLL_MODEL` environment variable is a one-off override.
