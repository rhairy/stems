#!/usr/bin/env python3
"""stemsd: background services for `stems`: a results web server and a job worker.

Intended use: run locally on your own Mac and open it only from that Mac (http://localhost:8765).
The web server listens on 127.0.0.1 and refuses any non-loopback client.

    stemsd web        Serve the stems folder over HTTP: browse/play results, download a
                      folder as .zip, and submit jobs (web form or JSON API).
    stemsd worker     Process queued jobs one at a time by running `stems`.
    stemsd submit URL Queue a job from the command line and print its id.
    stemsd gpu-check  Report whether PyTorch can use the Apple GPU (MPS) in this context.
    stemsd check-access  Check that the output folder is readable and writable.

Both long-running commands run under launchd as login agents for your user (see daemon/).

Job API (bot-agnostic; any local script or bot can use it):
    POST /api/jobs        {"url": "...", "two_stems": false, "mp3": false, "name": null}
                          -> 202 {"id": "...", "status": "pending", "status_url": "..."}
    GET  /api/jobs/<id>   -> {"id", "status": pending|running|done|failed, "link", "zip", "files", "error"}
    GET  /api/jobs        -> recent jobs, newest first
    GET  /api/health      -> {"ok": true}

Config (environment variables, set in the launchd plists):
    STEMS_OUT          Output root (default ~/Music/Stems). The queue lives in $STEMS_OUT/.queue.
    STEMS_PORT         Port (default 8765).
    STEMS_TOKEN        Optional. If set, the job API requires "Authorization: Bearer <token>".
    STEMS_PUBLIC_URL, STEMS_HOST, STEMS_ALLOW
                       Opt-in only, not part of the intended setup: base URL for links, bind address
                       and allowed client networks if you ever expose it beyond localhost.

Only use this on audio you have the rights to use.
"""
import html
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OUT = Path(os.environ.get("STEMS_OUT", Path.home() / "Music" / "Stems")).expanduser()
QUEUE = OUT / ".queue"
STATES = ("pending", "running", "done", "failed")
HOST = os.environ.get("STEMS_HOST", "127.0.0.1")  # localhost only by default
PORT = int(os.environ.get("STEMS_PORT", "8765"))
TOKEN = os.environ.get("STEMS_TOKEN", "")
# Second line of defense behind the 127.0.0.1 bind: only loopback clients are served.
ALLOW = [ipaddress.ip_network(n.strip()) for n in os.environ.get(
    "STEMS_ALLOW", "127.0.0.0/8,::1/128").split(",") if n.strip()]
AUDIO_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4"}
STEM_ORDER = ["vocals", "instrumental", "drums", "bass", "guitar", "piano", "other"]


def public_url() -> str:
    return os.environ.get("STEMS_PUBLIC_URL", "").rstrip("/") or f"http://localhost:{PORT}"


# ---------------------------------------------------------------- queue

def ensure_queue() -> None:
    for s in (*STATES, "tmp"):
        (QUEUE / s).mkdir(parents=True, exist_ok=True)


def find_job(job_id: str) -> tuple[str, dict] | None:
    if not job_id.replace("-", "").isalnum():
        return None
    for s in STATES:
        f = QUEUE / s / f"{job_id}.json"
        if f.exists():
            try:
                return s, json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                return None  # mid-move; caller can retry
    return None


def write_job(state: str, job: dict) -> None:
    tmp = QUEUE / state / f".{job['id']}.tmp"
    tmp.write_text(json.dumps(job, indent=2))
    tmp.replace(QUEUE / state / f"{job['id']}.json")


def submit(url: str, two_stems: bool = False, mp3: bool = False, name: str | None = None) -> dict:
    ensure_queue()
    job = {"id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6], "url": url, "two_stems": bool(two_stems),
           "mp3": bool(mp3), "name": name or None, "status": "pending", "created": time.time()}
    write_job("pending", job)
    return job


def job_view(state: str, job: dict) -> dict:
    v = {k: job.get(k) for k in ("id", "url", "name", "created", "started", "finished", "error", "files")}
    v["status"] = state
    folder = job.get("folder")
    if state == "done" and folder:
        base = f"{public_url()}/{urllib.parse.quote(folder)}"
        v["link"], v["zip"] = base + "/", base + ".zip"
    return v


def recent_jobs(limit: int = 50) -> list[dict]:
    jobs = []
    for s in STATES:
        for f in (QUEUE / s).glob("[!.]*.json"):
            try:
                jobs.append(job_view(s, json.loads(f.read_text())))
            except (OSError, json.JSONDecodeError):
                pass
    return sorted(jobs, key=lambda j: j.get("created") or 0, reverse=True)[:limit]


# ---------------------------------------------------------------- worker

def worker() -> None:
    check_access()
    ensure_queue()
    stems_bin = Path(sys.executable).with_name("stems")
    log(f"worker: output {OUT}, using {stems_bin}")
    gpu_check(quiet=False)
    for f in (QUEUE / "running").glob("[!.]*.json"):  # interrupted by a restart: retry
        f.replace(QUEUE / "pending" / f.name)
    while True:
        pending = sorted((QUEUE / "pending").glob("[!.]*.json"))
        if not pending:
            time.sleep(2)
            continue
        f = pending[0]
        try:
            job = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            f.replace(QUEUE / "failed" / f.name)
            continue
        job["started"] = time.time()
        write_job("running", job)
        f.unlink(missing_ok=True)
        log(f"job {job['id']}: start {job['url']}")
        result_file = QUEUE / "tmp" / f"{job['id']}.result.json"
        cmd = [str(stems_bin), job["url"], "--out", str(OUT), "--no-open", "--result-json", str(result_file)]
        if job.get("two_stems"):
            cmd.append("--two-stems")
        if job.get("mp3"):
            cmd.append("--mp3")
        if job.get("name"):
            cmd += ["--name", job["name"]]
        rc = subprocess.run(cmd).returncode
        if rc != 0 and os.environ.get("STEMS_FORCE_CPU") != "1":
            log(f"job {job['id']}: failed (exit {rc}), retrying on CPU")
            rc = subprocess.run(cmd + ["--cpu"]).returncode
        job["finished"] = time.time()
        if rc == 0 and result_file.exists():
            res = json.loads(result_file.read_text())
            job.update(folder=res["name"], files=res["files"], title=res.get("title"))
            write_job("done", job)
            log(f"job {job['id']}: done -> {public_url()}/{urllib.parse.quote(res['name'])}/")
        else:
            job["error"] = f"stems exited with code {rc}; see the worker log"
            write_job("failed", job)
            log(f"job {job['id']}: failed")
        result_file.unlink(missing_ok=True)
        (QUEUE / "running" / f"{job['id']}.json").unlink(missing_ok=True)


def gpu_check(quiet: bool = False) -> bool:
    try:
        import torch

        ok = torch.backends.mps.is_available()
        if ok:
            x = torch.ones(256, 256, device="mps")
            ok = float((x @ x).sum().cpu()) == 256.0 ** 3
        msg = "GPU (MPS) works" if ok else "GPU (MPS) NOT available, will run on CPU"
    except Exception as e:  # noqa: BLE001
        ok, msg = False, f"GPU (MPS) check failed: {e!r}; will run on CPU"
    log(f"gpu-check: {msg}")
    return ok


def check_access() -> None:
    """Fail loudly if the output folder isn't usable, then let launchd retry."""
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        os.listdir(OUT)
        probe = OUT / ".stemsd-write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        log(f"ERROR: can't read/write {OUT}: {e}. Fix the folder's permissions or set STEMS_OUT; retrying in 60s.")
        time.sleep(60)
        sys.exit(1)


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


# ---------------------------------------------------------------- web

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
:root{{--bg:#fafafa;--fg:#1b1b1b;--muted:#6b6b6b;--card:#fff;--line:#e3e3e3;--accent:#2a62d4}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151515;--fg:#eee;--muted:#9a9a9a;--card:#1f1f1f;--line:#333;--accent:#7aa2ff}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,sans-serif;margin:0;padding:24px 16px}}
main{{max-width:760px;margin:auto}} a{{color:var(--accent)}} h1{{font-size:22px;margin:0 0 16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:10px 0}}
.row{{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}}
.muted{{color:var(--muted);font-size:13px}} audio{{width:100%;margin-top:6px}}
input[type=url]{{flex:1;min-width:0;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}}
button{{padding:8px 14px;border-radius:6px;border:0;background:var(--accent);color:#fff;cursor:pointer}}
form{{display:flex;gap:8px;flex-wrap:wrap;align-items:center}} label{{font-size:13px}}
</style></head><body><main>{body}</main></body></html>"""


def esc(s) -> str:
    return html.escape(str(s), quote=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "stemsd"

    def log_message(self, fmt, *args):
        log(f"web: {self.client_address[0]} {fmt % args}")

    # --- helpers
    def allowed(self) -> bool:
        try:
            ip = ipaddress.ip_address(self.client_address[0])
            if getattr(ip, "ipv4_mapped", None):
                ip = ip.ipv4_mapped
        except ValueError:
            return False
        return any(ip in n for n in ALLOW)

    def send(self, code: int, body: bytes, ctype: str, headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, code: int, obj):
        self.send(code, json.dumps(obj, indent=2).encode(), "application/json")

    def send_page(self, title: str, body: str, code: int = 200):
        self.send(code, PAGE.format(title=esc(title), body=body).encode(), "text/html; charset=utf-8")

    def token_ok(self) -> bool:
        return not TOKEN or self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def safe_path(self, rel: str) -> Path | None:
        p = (OUT / rel).resolve()
        root = OUT.resolve()
        if p != root and root not in p.parents:
            return None
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            return None
        return p

    # --- routing
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.allowed():
            return self.send(403, b"Forbidden\n", "text/plain")
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path == "/api/health":
            return self.send_json(200, {"ok": True})
        if path == "/api/jobs":
            return self.send_json(200, recent_jobs())
        if path.startswith("/api/jobs/"):
            found = find_job(path.rsplit("/", 1)[-1])
            return self.send_json(200, job_view(*found)) if found else self.send_json(404, {"error": "no such job"})
        if path == "/":
            return self.index()
        rel = path.strip("/")
        if rel.endswith(".zip") and "/" not in rel:
            d = self.safe_path(rel[:-4])
            return self.zip_folder(d) if d and d.is_dir() else self.send(404, b"Not found\n", "text/plain")
        p = self.safe_path(rel)
        if p is None or not p.exists():
            return self.send(404, b"Not found\n", "text/plain")
        if p.is_dir():
            if not path.endswith("/"):
                return self.send(301, b"", "text/plain", {"Location": urllib.parse.quote(path) + "/"})
            return self.folder_page(p)
        return self.send_file(p)

    def do_POST(self):
        if not self.allowed():
            return self.send(403, b"Forbidden\n", "text/plain")
        path = urllib.parse.urlsplit(self.path).path
        length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
        raw = self.rfile.read(length)
        if path == "/api/jobs":
            if not self.token_ok():
                return self.send_json(401, {"error": "bad or missing token"})
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self.send_json(400, {"error": "body must be JSON"})
            url = str(data.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                return self.send_json(400, {"error": "url must be an http(s) link"})
            job = submit(url, data.get("two_stems"), data.get("mp3"), data.get("name"))
            return self.send_json(202, {**job_view("pending", job), "status_url": f"{public_url()}/api/jobs/{job['id']}"})
        if path == "/submit":  # the web form; same network restriction, no token (browser use)
            form = urllib.parse.parse_qs(raw.decode())
            url = form.get("url", [""])[0].strip()
            if url.startswith(("http://", "https://")):
                submit(url, "two_stems" in form, "mp3" in form)
            return self.send(303, b"", "text/plain", {"Location": "/"})
        self.send(404, b"Not found\n", "text/plain")

    # --- pages
    def index(self):
        jobs = [j for j in recent_jobs(20) if j["status"] in ("pending", "running", "failed")]
        folders = sorted((d for d in OUT.iterdir() if d.is_dir() and not d.name.startswith(".")),
                         key=lambda d: d.stat().st_mtime, reverse=True) if OUT.exists() else []
        parts = ["<h1>Stems</h1>",
                 '<div class="card"><form method="post" action="/submit">'
                 '<input type="url" name="url" placeholder="YouTube link" required>'
                 '<label><input type="checkbox" name="two_stems"> vocals only</label>'
                 '<label><input type="checkbox" name="mp3"> mp3</label>'
                 '<button>Separate</button></form></div>']
        for j in jobs:
            parts.append(f'<div class="card row"><span>{esc(j["url"])}</span>'
                         f'<span class="muted">{esc(j["status"])}{": " + esc(j["error"]) if j.get("error") else ""}'
                         f'</span></div>')
        if jobs and any(j["status"] in ("pending", "running") for j in jobs):
            parts.append('<script>setTimeout(()=>location.reload(),10000)</script>')
        for d in folders:
            q = urllib.parse.quote(d.name)
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.stat().st_mtime))
            parts.append(f'<div class="card row"><a href="/{q}/">{esc(d.name)}</a>'
                         f'<span class="muted">{when} · <a href="/{q}.zip">zip</a></span></div>')
        if not folders:
            parts.append('<p class="muted">No results yet.</p>')
        self.send_page("Stems", "".join(parts))

    def folder_page(self, d: Path):
        files = [f for f in d.iterdir() if f.is_file() and not f.name.startswith(".")]
        rank = {n: i for i, n in enumerate(STEM_ORDER)}
        files.sort(key=lambda f: (rank.get(f.stem, -1 if f.stem == d.name else 99), f.name))
        q = urllib.parse.quote(d.name)
        parts = [f'<p><a href="/">&larr; all results</a></p><h1>{esc(d.name)}</h1>',
                 f'<p><a href="/{q}.zip">Download all as .zip</a></p>']
        for f in files:
            fq = urllib.parse.quote(f.name)
            size = f"{f.stat().st_size / 1e6:.1f} MB"
            player = f'<audio controls preload="none" src="/{q}/{fq}"></audio>' if f.suffix in AUDIO_TYPES else ""
            label = "full mix" if f.stem == d.name else f.stem
            parts.append(f'<div class="card"><div class="row"><strong>{esc(label)}</strong>'
                         f'<span class="muted">{size} · <a href="/{q}/{fq}" download>download</a></span></div>'
                         f'{player}</div>')
        self.send_page(d.name, "".join(parts))

    def send_file(self, p: Path):
        size = p.stat().st_size
        ctype = AUDIO_TYPES.get(p.suffix.lower(), "application/octet-stream")
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):  # Safari's <audio> needs range requests
            a, _, b = rng[6:].split(",")[0].partition("-")
            try:
                if a:
                    start, end = int(a), int(b) if b else size - 1
                else:
                    start, end = max(size - int(b), 0), size - 1
            except ValueError:
                start, end = 0, size - 1
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with p.open("rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def zip_folder(self, d: Path):
        # Streamed, uncompressed (audio doesn't compress); no Content-Length, so close when done.
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{urllib.parse.quote(d.name)}.zip"')
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        with zipfile.ZipFile(_Unseekable(self.wfile), "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for f in sorted(d.iterdir()):
                if f.is_file() and not f.name.startswith("."):
                    z.write(f, f"{d.name}/{f.name}")


class _Unseekable:
    """Make zipfile treat the socket as a non-seekable stream."""

    def __init__(self, w):
        self.w, self.n = w, 0

    def write(self, b):
        self.w.write(b)
        self.n += len(b)
        return len(b)

    def tell(self):
        return self.n

    def flush(self):
        self.w.flush()


def web() -> None:
    check_access()
    ensure_queue()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    log(f"web: serving {OUT} on {HOST}:{PORT}; links use {public_url()}; allowed: {', '.join(map(str, ALLOW))}")
    srv.serve_forever()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "web":
        web()
    elif cmd == "worker":
        worker()
    elif cmd == "check-access":
        check_access()
        print(f"OK: can read and write {OUT}")
    elif cmd == "gpu-check":
        sys.exit(0 if gpu_check() else 1)
    elif cmd == "submit" and len(sys.argv) > 2:
        job = submit(sys.argv[2], "--two-stems" in sys.argv, "--mp3" in sys.argv)
        print(job["id"])
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
