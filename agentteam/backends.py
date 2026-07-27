"""Vendor-neutral agent backends.

One interface: ``run_agent(prompt, backend=..., model=..., effort=..., cwd=...) -> AgentResult``.

The point of this module is interchangeability: a job's spec picks the backend, and nothing
else in the system knows or cares which model is behind it. Add a backend here, and every
recipe can use it. ``mock`` lets you exercise the whole engine offline, with no spend.

Timeouts (see docs/BACKLOG.md #4): spend is controlled by the token budget + round count
(lossless, checked between rounds), NOT by killing a call. A wall-clock ``timeout`` is OFF by
default and exists only as an explicit override. The default backstop is an **idle** timeout:
a call is killed only if it produces NO stdout AND writes NO files for ``idle_timeout`` seconds
(i.e. it's genuinely hung, not just slow). On any kill the streamed output is salvaged so the
agent's latest report is recovered, not thrown away.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

DEFAULT_IDLE_TIMEOUT = 1800  # 30 min of total silence (no stdout AND no file writes) -> hung

# Per-backend model/effort defaults. These live here (not in the CLI) because a *role* may
# switch backend mid-job (see staffing.py), and it then needs that backend's default model --
# the job-level model name belongs to the other CLI's vocabulary.
DEFAULTS = {
    "claude": {"model": "claude-opus-4-8[1m]", "effort": "high"},
    "codex": {"model": "gpt-5.6-sol", "effort": "xhigh"},
    "mock": {"model": None, "effort": None},
}


@dataclass
class AgentResult:
    text: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    backend: str = ""
    ok: bool = True
    error: str = ""
    note: str = ""  # non-fatal note, e.g. "idle-timeout (salvaged partial output)"

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def run_agent(prompt, *, backend, model=None, effort=None, cwd,
              timeout=None, idle_timeout=DEFAULT_IDLE_TIMEOUT) -> AgentResult:
    """Run one agent turn and return its final text plus usage.

    ``timeout``      hard wall-clock ceiling in seconds, or None to disable (default).
    ``idle_timeout`` kill only after this many seconds of no stdout AND no file writes; 0 disables.
    """
    if backend == "mock":
        return _run_mock(prompt, cwd=cwd)
    if backend == "claude":
        return _run_claude(prompt, model=model, effort=effort, cwd=cwd,
                           timeout=timeout, idle_timeout=idle_timeout)
    if backend == "codex":
        return _run_codex(prompt, model=model, effort=effort, cwd=cwd,
                          timeout=timeout, idle_timeout=idle_timeout)
    raise ValueError(f"unknown backend: {backend!r} (known: claude, codex, mock)")


# --- streaming runner with an idle/hard-timeout watchdog ---------------------

def _newest_mtime(path) -> float:
    newest = 0.0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                newest = max(newest, os.stat(os.path.join(root, name)).st_mtime)
            except OSError:
                pass
    return newest


def _terminate(proc):
    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    except OSError:
        pass


def _stream_run(cmd, *, cwd, timeout, idle_timeout):
    """Run cmd, streaming stdout+stderr. Kill if idle (no output AND no file writes in cwd for
    idle_timeout s) or if the hard timeout is exceeded. Returns (stdout, stderr, killed) where
    killed is None | 'idle' | 'hard'. File writes count as progress, so a working call that
    reads/runs/writes is never killed by the idle watchdog."""
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, bufsize=1)
    out, err = [], []
    last = [time.monotonic()]
    lock = threading.Lock()

    def _read(stream, buf):
        for line in iter(stream.readline, ""):
            buf.append(line)
            with lock:
                last[0] = time.monotonic()
        stream.close()

    threads = [threading.Thread(target=_read, args=(proc.stdout, out), daemon=True),
               threading.Thread(target=_read, args=(proc.stderr, err), daemon=True)]
    for t in threads:
        t.start()

    start = time.monotonic()
    last_fs = _newest_mtime(cwd)
    killed = None
    while proc.poll() is None:
        time.sleep(3)
        now = time.monotonic()
        fs = _newest_mtime(cwd)
        if fs > last_fs:                 # the agent wrote a file -> that's progress
            last_fs = fs
            with lock:
                last[0] = now
        with lock:
            idle = now - last[0]
        if timeout and (now - start) > timeout:
            killed = "hard"
            _terminate(proc)
            break
        if idle_timeout and idle > idle_timeout:
            killed = "idle"
            _terminate(proc)
            break
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    for t in threads:
        t.join(timeout=5)
    return "".join(out), "".join(err), killed


# --- Claude Code CLI ---------------------------------------------------------

def _run_claude(prompt, *, model, effort, cwd, timeout, idle_timeout) -> AgentResult:
    if not shutil.which("claude"):
        return AgentResult(backend="claude", ok=False, error="`claude` CLI not found on PATH")
    cmd = ["claude"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    cmd += ["--output-format", "json", "-p", prompt, "--dangerously-skip-permissions"]
    stdout, stderr, killed = _stream_run(cmd, cwd=cwd, timeout=timeout, idle_timeout=idle_timeout)

    text, cost, itok, otok = "", 0.0, 0, 0
    try:
        data = json.loads(stdout)
        text = data.get("result") or data.get("text") or ""
        cost = float(data.get("total_cost_usd") or 0.0)
        usage = data.get("usage") or {}
        itok = int(usage.get("input_tokens") or 0)
        otok = int(usage.get("output_tokens") or 0)
    except (json.JSONDecodeError, ValueError, TypeError):
        text = stdout.strip()  # fall back to raw stdout (e.g. killed mid-run)
    return _finalize("claude", text, itok, otok, killed, stderr, cost_usd=cost)


# --- OpenAI Codex CLI --------------------------------------------------------

def _run_codex(prompt, *, model, effort, cwd, timeout, idle_timeout) -> AgentResult:
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
    stdout, stderr, killed = _stream_run(cmd, cwd=cwd, timeout=timeout, idle_timeout=idle_timeout)

    text = ""
    if os.path.exists(last_path):
        with open(last_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read().strip()  # written only on normal completion
        _safe_unlink(last_path)
    if not text:
        text = _codex_last_message(stdout)  # salvage the latest streamed message on a kill
    itok, otok = _codex_usage_from_jsonl(stdout)
    return _finalize("codex", text, itok, otok, killed, stderr)


def _finalize(backend, text, itok, otok, killed, stderr, cost_usd=0.0) -> AgentResult:
    if killed:
        note = f"{killed}-timeout" + (" (salvaged partial output)" if text else " (no output salvaged)")
        return AgentResult(text=text, cost_usd=cost_usd, input_tokens=itok, output_tokens=otok,
                           backend=backend, ok=bool(text), error=("" if text else note), note=note)
    if not text:
        err = (stderr or "").strip()[-2000:] or "no output"
        return AgentResult(backend=backend, ok=False, input_tokens=itok, output_tokens=otok,
                           error=err)
    return AgentResult(text=text, cost_usd=cost_usd, input_tokens=itok, output_tokens=otok,
                       backend=backend)


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


def _codex_last_message(stdout) -> str:
    """Recover the most recent agent message from a (possibly partial) codex JSONL stream."""
    latest = ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = ev.get("msg") if isinstance(ev.get("msg"), dict) else ev
        if msg.get("type") in ("agent_message", "message") and msg.get("message"):
            latest = str(msg.get("message"))
        elif msg.get("type") == "agent_message" and msg.get("text"):
            latest = str(msg.get("text"))
    return latest.strip()


def _safe_unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# --- Mock (offline engine smoke-test) ---------------------------------------

def _run_mock(prompt, *, cwd) -> AgentResult:
    """Deterministic stand-in so the engine/lifecycle can be exercised with zero spend."""
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
