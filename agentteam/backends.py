"""Vendor-neutral agent backends.

One interface: ``run_agent(prompt, backend=..., model=..., effort=..., cwd=...) -> AgentResult``.

The point of this module is interchangeability: a job's spec picks the backend, and nothing
else in the system knows or cares which model is behind it. Add a backend here, and every
recipe can use it. ``mock`` lets you exercise the whole engine offline, with no spend.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass


@dataclass
class AgentResult:
    text: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    backend: str = ""
    ok: bool = True
    error: str = ""

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def run_agent(prompt, *, backend, model=None, effort=None, cwd, timeout=None) -> AgentResult:
    """Run one agent turn and return its final text plus usage.

    The agent runs with ``cwd`` as its working directory and (for real backends) full file
    access there -- it does the work directly on disk; we read back its report text.
    """
    if backend == "mock":
        return _run_mock(prompt, cwd=cwd)
    if backend == "claude":
        return _run_claude(prompt, model=model, effort=effort, cwd=cwd, timeout=timeout)
    if backend == "codex":
        return _run_codex(prompt, model=model, effort=effort, cwd=cwd, timeout=timeout)
    raise ValueError(f"unknown backend: {backend!r} (known: claude, codex, mock)")


# --- Claude Code CLI ---------------------------------------------------------

def _run_claude(prompt, *, model, effort, cwd, timeout) -> AgentResult:
    if not shutil.which("claude"):
        return AgentResult(backend="claude", ok=False, error="`claude` CLI not found on PATH")
    cmd = ["claude"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    cmd += ["--output-format", "json", "-p", prompt, "--dangerously-skip-permissions"]
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return AgentResult(backend="claude", ok=False, error=f"timed out after {timeout}s")
    if proc.returncode != 0 and not proc.stdout.strip():
        return AgentResult(backend="claude", ok=False, error=(proc.stderr or "nonzero exit").strip()[:2000])
    text, cost, itok, otok = "", 0.0, 0, 0
    try:
        data = json.loads(proc.stdout)
        text = data.get("result") or data.get("text") or ""
        cost = float(data.get("total_cost_usd") or 0.0)
        usage = data.get("usage") or {}
        itok = int(usage.get("input_tokens") or 0)
        otok = int(usage.get("output_tokens") or 0)
    except (json.JSONDecodeError, ValueError, TypeError):
        text = proc.stdout.strip()  # fall back to raw stdout
    return AgentResult(text=text, cost_usd=cost, input_tokens=itok, output_tokens=otok, backend="claude")


# --- OpenAI Codex CLI --------------------------------------------------------

def _run_codex(prompt, *, model, effort, cwd, timeout) -> AgentResult:
    if not shutil.which("codex"):
        return AgentResult(backend="codex", ok=False, error="`codex` CLI not found on PATH")
    fd, last_path = tempfile.mkstemp(suffix=".txt", prefix="codex_last_")
    os.close(fd)
    cmd = ["codex", "exec", "--skip-git-repo-check"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["-c", f'model_reasoning_effort="{effort}"']  # quoted so codex parses it as TOML
    cmd += ["--sandbox", "danger-full-access", "-c", 'approval_policy="never"',
            "--json", "--output-last-message", last_path, prompt]
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        _safe_unlink(last_path)
        return AgentResult(backend="codex", ok=False, error=f"timed out after {timeout}s")
    text = ""
    if os.path.exists(last_path):
        with open(last_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read().strip()
        _safe_unlink(last_path)
    itok, otok = _codex_usage_from_jsonl(proc.stdout)
    if not text and proc.returncode != 0:
        return AgentResult(backend="codex", ok=False, error=(proc.stderr or "nonzero exit").strip()[:2000])
    return AgentResult(text=text, input_tokens=itok, output_tokens=otok, backend="codex")


def _codex_usage_from_jsonl(stdout) -> tuple[int, int]:
    itok = otok = 0
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = ev.get("usage") or (ev.get("msg") or {}).get("usage") or {}
        if isinstance(usage, dict):
            itok = int(usage.get("input_tokens") or itok)
            otok = int(usage.get("output_tokens") or otok)
    return itok, otok


def _safe_unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# --- Mock (offline engine smoke-test) ---------------------------------------

def _run_mock(prompt, *, cwd) -> AgentResult:
    """Deterministic stand-in so the engine/lifecycle can be exercised with zero spend.

    It writes a marker into work/ (proving file access works the same way real agents use it)
    and echoes the first lines of the prompt so round-to-round context threading is visible.
    """
    role = "agent"
    for line in prompt.splitlines():
        if line.startswith("# ROLE:"):
            role = line.split(":", 1)[1].strip()
            break
    work = os.path.join(cwd, "work")
    try:
        os.makedirs(work, exist_ok=True)
        with open(os.path.join(work, "mock_activity.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{role} ran\n")
    except OSError:
        pass
    head = "\n".join(prompt.strip().splitlines()[:3])
    text = f"[mock {role}] acknowledged. Prompt head:\n{head}"
    return AgentResult(text=text, backend="mock", output_tokens=max(1, len(prompt) // 4))
