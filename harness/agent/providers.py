"""Pluggable coding-agent drivers.

The harness needs exactly one thing from a model: given a tree and a set of
reject hunks, edit files under `patches/` until the stack applies. Which model
does that is a swappable detail, so it lives behind a two-method interface and
is chosen by `HARNESS_AGENT` (codex | claude | none).

`none` is a first-class option, not a stub. With it the harness still detects,
builds, tests, gates and reports -- it just stops at the first broken patch and
opens an issue instead of fixing it. That is the mode to run in while you are
still deciding whether to trust an agent with the patch stack.

Credentials
-----------
Both drivers read their key from the environment and nothing else. The repair
job in the workflow is the only job that gets one, and it is deliberately the
job with no write access to the repository: an agent that can edit patches
cannot also push, comment, or read the sundial credentials.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Protocol

from .._util import Result, log, run


@dataclass
class AgentTurn:
    ok: bool
    output: str
    provider: str


class Provider(Protocol):
    name: str

    def available(self) -> Optional[str]:
        """None if usable, else a human-readable reason it is not."""

    def run(self, prompt: str, *, cwd: Path, timeout: int) -> AgentTurn: ...


class _Base:
    name = "base"
    binary = ""

    def _missing_binary(self) -> Optional[str]:
        if not shutil.which(self.binary):
            return f"{self.binary} is not on PATH"
        return None


class CodexProvider(_Base):
    """OpenAI Codex CLI.

    Auth, in the order it is tried:

      OPENAI_API_KEY   A metered API key. The only mode actually designed for
                       CI: it is revocable, scopeable to a project, and can
                       carry a hard spend cap. Recommended.

      CODEX_AUTH_JSON  The contents of a `~/.codex/auth.json` produced by a
                       ChatGPT sign-in, stored as a secret. This works, but the
                       refresh token rotates, so the stored copy goes stale and
                       the workflow starts failing for reasons that look
                       unrelated; a ChatGPT plan's usage limits are sized for
                       interactive use, not a rebuild loop; and it puts a full
                       account credential in CI. Supported because it is your
                       account and your call -- not the default.
    """

    name = "codex"
    binary = "codex"

    def available(self) -> Optional[str]:
        missing = self._missing_binary()
        if missing:
            return missing
        if os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_AUTH_JSON"):
            return None
        return "neither OPENAI_API_KEY nor CODEX_AUTH_JSON is set"

    def _materialise_auth(self) -> None:
        blob = os.environ.get("CODEX_AUTH_JSON")
        if not blob or os.environ.get("OPENAI_API_KEY"):
            return
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        home.mkdir(parents=True, exist_ok=True)
        auth = home / "auth.json"
        auth.write_text(blob, encoding="utf-8")
        auth.chmod(0o600)
        log("restored codex auth.json from CODEX_AUTH_JSON")

    def run(self, prompt: str, *, cwd: Path, timeout: int) -> AgentTurn:
        self._materialise_auth()
        proc: Result = run(
            [
                self.binary, "exec",
                "--skip-git-repo-check",
                # The sandbox is the harness's own path allowlist plus the
                # forbidden-path check after every turn, not the CLI's.
                "--dangerously-bypass-approvals-and-sandbox",
                prompt,
            ],
            cwd=cwd,
            timeout=timeout,
            tee=True,
            capture=False,
        )
        return AgentTurn(ok=proc.ok, output=proc.combined(), provider=self.name)


class ClaudeProvider(_Base):
    """Anthropic Claude Code CLI, driven non-interactively with `-p`."""

    name = "claude"
    binary = "claude"

    def available(self) -> Optional[str]:
        missing = self._missing_binary()
        if missing:
            return missing
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return None
        return "neither ANTHROPIC_API_KEY nor CLAUDE_CODE_OAUTH_TOKEN is set"

    def run(self, prompt: str, *, cwd: Path, timeout: int) -> AgentTurn:
        proc = run(
            [
                self.binary, "-p", prompt,
                "--permission-mode", "acceptEdits",
                "--allowedTools", "Read,Edit,Write,Grep,Glob,Bash",
            ],
            cwd=cwd,
            timeout=timeout,
            tee=True,
            capture=False,
        )
        return AgentTurn(ok=proc.ok, output=proc.combined(), provider=self.name)


class NoneProvider(_Base):
    """No agent. The harness reports the rejects and stops."""

    name = "none"

    def available(self) -> Optional[str]:
        return None

    def run(self, prompt: str, *, cwd: Path, timeout: int) -> AgentTurn:
        return AgentTurn(
            ok=False,
            output="HARNESS_AGENT=none: patch repair is not attempted in this mode.",
            provider=self.name,
        )


_PROVIDERS = {p.name: p for p in (CodexProvider(), ClaudeProvider(), NoneProvider())}


def get(name: Optional[str] = None) -> Provider:
    name = (name or os.environ.get("HARNESS_AGENT") or "codex").strip().lower()
    if name not in _PROVIDERS:
        raise SystemExit(
            f"unknown agent {name!r}; expected one of {', '.join(sorted(_PROVIDERS))}"
        )
    return _PROVIDERS[name]  # type: ignore[return-value]


def describe() -> List[str]:
    lines = []
    for name, provider in sorted(_PROVIDERS.items()):
        reason = provider.available()
        lines.append(f"  {name:<8} {'ready' if reason is None else 'unavailable: ' + reason}")
    return lines
