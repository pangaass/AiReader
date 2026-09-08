from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AgentError(RuntimeError):
    """Raised when a CLI agent fails or returns unusable JSON."""


def find_claude() -> str:
    """Find Claude Code, including the NVM layout used by the research host."""
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    candidates = sorted(Path.home().glob(".nvm/versions/node/*/bin/claude"), reverse=True)
    if candidates:
        return str(candidates[0])
    raise AgentError("Claude Code is not installed; set CLAUDE_BIN or install `claude`.")


def parse_json_payload(value: str | dict[str, Any]) -> dict[str, Any]:
    """Parse either Claude's result envelope or a model JSON response."""
    if isinstance(value, dict):
        if value.get("structured_output") is not None:
            return parse_json_payload(value["structured_output"])
        if value.get("type") == "result" and "result" in value:
            return parse_json_payload(value["result"])
        return value
    text = value.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise AgentError("Agent response did not contain a JSON object")
        candidate = text[start : end + 1]
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            try:
                from json_repair import repair_json

                parsed = repair_json(candidate, return_objects=True)
            except Exception as repair_exc:
                raise AgentError("Agent response contained invalid JSON") from repair_exc
    if not isinstance(parsed, dict):
        raise AgentError("Agent response must be a JSON object")
    if parsed.get("type") == "result" and "result" in parsed:
        return parse_json_payload(parsed["result"])
    return parsed


@dataclass(frozen=True)
class AgentResult:
    data: dict[str, Any]
    envelope: dict[str, Any]

    @property
    def cost_usd(self) -> float:
        return float(self.envelope.get("total_cost_usd") or 0.0)


class ClaudeRunner:
    def __init__(
        self, model: str = "glm-5.3-flash", timeout_seconds: int = 900, effort: str = "low"
    ):
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.effort = effort
        self.binary = find_claude()

    def run(
        self,
        prompt: str,
        cwd: Path,
        *,
        allow_read: bool = True,
        schema: dict[str, Any] | None = None,
    ) -> AgentResult:
        command = [
            self.binary,
            "-p",
            "--model",
            self.model,
            "--output-format",
            "json",
            "--effort",
            self.effort,
            "--max-turns",
            "3",
            "--no-session-persistence",
        ]
        if allow_read:
            # The option is variadic; the = form prevents it from consuming the prompt.
            command += ["--allowedTools=Read"]
        if schema is not None:
            command += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
        env = os.environ.copy()
        node_bin = str(Path(self.binary).parent)
        env["PATH"] = node_bin + os.pathsep + env.get("PATH", "")
        try:
            completed = subprocess.run(
                command + [prompt],
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentError(f"Claude Code timed out after {self.timeout_seconds}s") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout)[-4000:]
            raise AgentError(f"Claude Code exited {completed.returncode}: {detail}")
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AgentError("Claude Code did not return its JSON envelope") from exc
        if envelope.get("is_error"):
            raise AgentError(str(envelope.get("result") or envelope))
        return AgentResult(parse_json_payload(envelope), envelope)
