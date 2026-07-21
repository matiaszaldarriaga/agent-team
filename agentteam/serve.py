"""Optional local server for the HTML monitor + inject box.

`job serve` starts it. It lists jobs, serves each job's view.html and deliverables, and accepts
`POST /inject?job=<id>` (from the view's text box) which appends to that job's inbox. Entirely
local, read-mostly, no dependencies -- the demoted successor to the old Flask dashboard.
"""

from __future__ import annotations

import html
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import jobs as jobs_mod
from .render import DEFAULT_PORT


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/inject":
            return self._send(404, "not found", "text/plain")
        qs = urllib.parse.parse_qs(parsed.query)
        job_id = (qs.get("job") or [""])[0]
        length = int(self.headers.get("Content-Length", 0))
        text = self.rfile.read(length).decode("utf-8", "replace").strip()
        job = jobs_mod.Job(job_id)
        if not job.exists() or not text:
            return self._send(400, "bad job or empty direction", "text/plain")
        job.say(text)
        self._send(200, "queued", "text/plain")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        if path in ("/", "/index.html"):
            return self._send(200, _index())
        # serve files from under jobs_root
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(jobs_mod.jobs_root(), rel))
        if not full.startswith(os.path.normpath(jobs_mod.jobs_root())):
            return self._send(403, "forbidden", "text/plain")
        if os.path.isdir(full):
            full = os.path.join(full, "view.html")
        if not os.path.isfile(full):
            return self._send(404, "not found", "text/plain")
        ctype = _ctype(full)
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)


def _ctype(path):
    ext = os.path.splitext(path)[1].lower()
    return {".html": "text/html; charset=utf-8", ".json": "application/json",
            ".tex": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
            ".pdf": "application/pdf", ".diff": "text/plain; charset=utf-8",
            ".py": "text/plain; charset=utf-8"}.get(ext, "application/octet-stream")


def _index():
    rows = []
    for job in jobs_mod.list_jobs():
        try:
            spec = job.load_spec()
        except OSError:
            continue
        rows.append(
            f'<tr><td><a href="/{html.escape(job.id)}/view.html">{html.escape(job.id)}</a></td>'
            f'<td>{spec.get("status","")}</td>'
            f'<td>{spec.get("round",0)}/{spec.get("rounds",0)}</td>'
            f'<td>${spec.get("cost_usd",0.0):.3f}</td></tr>')
    body = "\n".join(rows) or '<tr><td colspan="4">no jobs</td></tr>'
    return ("<!doctype html><meta charset=utf-8><title>agent-team jobs</title>"
            "<meta http-equiv=refresh content=5>"
            "<style>body{font:15px system-ui;max-width:820px;margin:2em auto;padding:0 1em}"
            "table{width:100%;border-collapse:collapse}td{padding:6px 8px;border-bottom:1px solid #ccc3}"
            "a{color:#2563eb}</style>"
            "<h2>agent-team jobs</h2><table>"
            "<tr><td><b>job</b></td><td><b>status</b></td><td><b>round</b></td><td><b>cost</b></td></tr>"
            + body + "</table>")


def serve(port=DEFAULT_PORT):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"agent-team monitor: http://127.0.0.1:{port}/  (Ctrl-C to stop)")
    print(f"serving jobs under: {jobs_mod.jobs_root()}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
