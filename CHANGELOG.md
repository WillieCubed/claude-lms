# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `cll` warms up the selected model in LM Studio before launching Claude. If the model
  isn't already loaded, `cll` sends one minimal request and waits, so Claude's first
  request hits a loaded model instead of a cold start. Best-effort: if the warm-up fails,
  `cll` says so and starts Claude anyway, letting LM Studio load the model on first use.
- Colored status output: errors in red, warnings in yellow, successes in green, and the
  loaded/default flags in `cll models` and `cll doctor` highlighted. Color is only used
  when writing to a terminal and is disabled by the `NO_COLOR` environment variable.

### Changed

- The interactive `--pick` menu is smoother. Long model names are truncated to fit the
  terminal instead of wrapping, only the rows that change are repainted as you move the
  selection, the menu is cleared on exit, and Ctrl-C cancels cleanly.
- `cll doctor` now lists every model LM Studio has loaded (it can hold several at once) and
  marks the loaded ones in the available-models list, instead of showing only one.
- When several models are loaded and nothing else selects one, `cll` shows the picker
  instead of silently guessing which loaded model to use. With exactly one loaded, it still
  launches straight into it.
- Clearer messages: "running but no models" is distinguished from "not reachable", a missing
  saved default explains it is your saved default and points at `cll set-default`, and the
  warm-up prints a short confirmation (with elapsed time) once the model is ready.

## [0.3.0]

### Added

- `cll install-completion --uninstall` removes the completion script and the rc block it
  added. Package uninstallers (`brew`/`pip`/`uv`) don't remove these, since they live in
  your home directory, outside the package.

## [0.2.0]

A model-selection and UX release. (Supersedes a withdrawn 0.1.3, whose features —
mis-numbered as a patch — are folded in here under a proper MINOR bump.)

### Added

- **Subcommands** for standalone actions: `cll models`, `cll list-models`, `cll doctor`,
  `cll set-default <model>`, `cll clear-default`, `cll install-completion`.
- **Saved default model**: `cll set-default` / `cll clear-default`, stored in
  `~/.config/claude-lms/config.json`. `CLL_MODEL` remains a one-off override.
- **`cll models`** — a table of available models with arch, quant, and which is
  loaded/default.
- **Interactive `--pick`** — an arrow-key menu (↑/↓ or j/k, Enter, q to cancel), with
  each model annotated; falls back to a numbered menu when there's no TTY.
- **`cll install-completion`** — turnkey zsh/bash tab-completion: writes the script and
  wires it into your shell rc (idempotent). `cll -m <TAB>` / `cll set-default <TAB>`
  complete model ids.
- A launch hint pointing at `--pick` / `cll set-default` when the model is auto-resolved.

### Changed

- Standalone actions moved from flags (`--models`, `--doctor`, …) to subcommands.
  Launch modifiers (`-m`, `--pick`) remain flags.
- Model resolution order now includes the saved default: `-m/--model` → `--pick` →
  `$CLL_MODEL` → saved default → loaded model → only model → menu.

## [0.1.2]

### Changed

- Performance: the proxy sets `TCP_NODELAY` so streamed tokens are not briefly buffered,
  skips the JSON re-encode of the request body when it carries no stray system message
  (nothing to fold), and `cll` no longer fetches the model list twice at startup. These
  are small wins — the dominant latency is the local model, not the proxy.

### Added

- `docs/reference/architecture.md`: a short explanation of the launcher, the proxy, the
  request flow, the lifecycle, and the performance levers. README gained Performance and
  architecture pointers.

## [0.1.1]

### Fixed

- The proxy no longer writes to the terminal. It runs in-process inside `cll`, sharing
  stderr with the Claude Code TUI; benign client disconnects previously dumped
  `ConnectionResetError` tracebacks to stderr and corrupted the display. Connection
  errors are now swallowed, and any diagnostics go to a log file behind `CLAUDE_LMS_DEBUG`.

### Changed

- Streaming uses `read1`, forwarding tokens as soon as LM Studio emits them (smoother
  output) and without copying each chunk into a combined buffer.
- Distribution is now Homebrew + source installs; PyPI publishing was dropped.

## [0.1.0]

### Added

- `cll` launcher: run Claude Code against a local LM Studio model.
- Normalizing reverse proxy that folds every non-leading `system` message into the
  single leading `system` block, fixing the "System message must be at the beginning"
  chat-template error for Qwen and other strict-template models. Runs on an ephemeral
  per-session port and streams responses through unbuffered.
- Model selection: `-m/--model` (exact id or unique substring), `--pick` menu,
  `$CLL_MODEL` default, current-loaded-model detection.
- `--list-models` and `--doctor` helpers.
- Standalone `claude-lms-proxy` console script.
- Packaging: PyPI metadata, a Homebrew formula, and GitHub Actions CI.

[Unreleased]: https://github.com/WillieCubed/claude-lms/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/WillieCubed/claude-lms/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/WillieCubed/claude-lms/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/WillieCubed/claude-lms/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/WillieCubed/claude-lms/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/WillieCubed/claude-lms/releases/tag/v0.1.0
