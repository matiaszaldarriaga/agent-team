"""Render a job's self-contained view.html -- the monitor + inject surface.

This is deliberately a single static file you open in a browser. It auto-refreshes, shows the
verified state, and links to the real deliverable (the tex/pdf/notebook you actually read).
The inject box posts to the optional `job serve` endpoint; you can equally run `job say`.
"""

from __future__ import annotations

import html
import os
from datetime import datetime

from . import TEMPLATES_DIR, staffing

DEFAULT_PORT = 8757


def render(job) -> None:
    spec = job.load_spec()
    state = job.load_state()
    with open(os.path.join(TEMPLATES_DIR, "view.html.tmpl"), encoding="utf-8") as fh:
        tmpl = fh.read()

    checks = state.get("checks") or {}
    if not checks:
        checks_txt, checks_cls = "not configured", "muted"
    elif checks.get("passed"):
        checks_txt, checks_cls = "PASSED", "ok"
    else:
        checks_txt, checks_cls = "FAILED — " + (checks.get("detail", "")[:200]), "bad"

    rows = []
    for c in reversed(state.get("claims", [])[-40:]):
        st = c.get("status", "unclear")
        rows.append(
            f'<tr><td class="r">{c.get("round","")}</td>'
            f'<td><span class="badge {st}">{st}</span></td>'
            f'<td>{html.escape(c.get("text",""))}</td></tr>')
    claims_rows = "\n".join(rows) or '<tr><td colspan="3" class="muted">no claims yet</td></tr>'

    last_verifier = ""
    if state.get("rounds_log"):
        last_verifier = state["rounds_log"][-1].get("verifier", "")

    values = {
        "ID": job.id,
        "TYPE": spec.get("type", ""),
        "KIND": spec.get("kind", ""),
        "STATUS": spec.get("status", ""),
        "STATUS_CLASS": _status_class(spec.get("status", "")),
        "ROUND": str(spec.get("round", 0)),
        "ROUNDS": str(spec.get("rounds", 0)),
        "COST": f'{spec.get("cost_usd", 0.0):.3f}',
        "TOKENS": f'{spec.get("tokens", 0):,}',
        "BUDGET": f'{spec.get("budget_tokens", 0):,}',
        "BACKEND": spec.get("backend", ""),
        "MODEL": spec.get("model") or "—",
        "EFFORT": spec.get("effort") or "—",
        "ROLES_BLOCK": _roles_block(spec),
        "INTENT": html.escape(spec.get("intent", "")),
        "PLAN": html.escape(state.get("plan", "") or "(no plan yet)"),
        "CLAIMS_ROWS": claims_rows,
        "CHECKS": html.escape(checks_txt),
        "CHECKS_CLASS": checks_cls,
        "VERIFIER": html.escape(last_verifier or "(no verifier output yet)"),
        "DELIVERABLE_PATH": html.escape(spec.get("deliverable", {}).get("path", "")),
        "PORT": str(DEFAULT_PORT),
        "UPDATED": _updated_str(spec, state),
    }
    out = tmpl
    for key, val in values.items():
        out = out.replace("{{" + key + "}}", val)
    with open(job.view_path, "w", encoding="utf-8") as fh:
        fh.write(out)


def _roles_block(spec):
    """The staffing table -- shown only when the team isn't uniform, because then the single
    backend/model/effort tiles above no longer say what produced a given claim."""
    if not spec.get("roles"):
        return ""
    rows = []
    for row in staffing.table(spec):
        count = f' <span class="muted">x{row["n"]}</span>' if row["n"] > 1 else ""
        when = row["when"]
        when_cell = ("" if when == "every"
                     else f'<td>{html.escape(when)}</td>')
        rows.append(
            f'<tr><td>{html.escape(row["role"])}{count}</td>'
            f'<td>{html.escape(row["backend"] or "—")}</td>'
            f'<td class="dim">{html.escape(row["model"] or "—")}</td>'
            f'<td>{html.escape(row["effort"] or "—")}</td>'
            + (when_cell or '<td class="dim">every round</td>') + "</tr>")
    return ("<h2>Staffing</h2>\n<table class=\"roles\"><thead><tr>"
            "<th>role</th><th>backend</th><th>model</th><th>effort</th><th>runs</th>"
            "</tr></thead><tbody>\n" + "\n".join(rows) + "\n</tbody></table>")


def _updated_str(spec, state):
    """Render time, plus the live phase when the job is running (folded into one placeholder,
    so no template change is needed)."""
    stamp = datetime.now().strftime("%H:%M:%S")
    phase = state.get("phase")
    if spec.get("status") == "running" and phase:
        return f"{stamp}  ·  {phase}"
    return stamp


def _status_class(status):
    return {
        "running": "run", "done": "ok", "frozen": "ok",
        "stopped": "warn", "abandoned": "bad", "created": "muted",
    }.get(status, "muted")
