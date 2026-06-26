"""claude-lms: run Claude Code against local models in LM Studio.

Exposes a normalizing reverse proxy that makes strict chat templates (e.g. Qwen)
accept Claude Code's requests, and a ``cll`` launcher that wires it all together.
"""

__version__ = "0.4.0"

from .proxy import normalize  # noqa: E402,F401  (re-exported for convenience)
