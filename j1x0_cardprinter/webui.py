"""A small local web UI: pick a design, fill in the fields, check the preview, press Print.

    j1x0            # starts the UI and opens your browser

`CardApp` holds all the behaviour and knows nothing about HTTP, so it is tested directly with a
fake rasteriser and the mock printer. `make_server` wraps it in a stdlib `http.server` with a JSON
API and three static files from `j1x0_cardprinter/web/`. No extra dependencies.

Safety: the server listens on 127.0.0.1 only unless you ask otherwise, rejects requests whose Host
header is not the loopback name (DNS rebinding), and requires a per-run token on every POST
(cross-site requests). Print jobs always send exactly the bytes behind the preview the user saw.
"""

from __future__ import annotations

import io
import json
import logging
import re
import secrets
import shutil
import signal
import tempfile
import threading
import webbrowser
from collections import OrderedDict
from contextlib import AbstractContextManager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image, ImageOps

from .library import Design, discover, humanise
from .printer import Printer
from .probe import check_link, quick_check
from .raster import CairoRasteriser, InkscapeShellRasteriser, Rasteriser, composite, render_card
from .sender import send_job
from .template import MissingField, Template
from .usb import DeviceSelectionError, J1x0USB

log = logging.getLogger(__name__)

DEFAULT_PORT = 8730
MAX_UPLOAD = 12 * 1024 * 1024
MAX_JSON = 1024 * 1024
LinkFactory = Callable[[], AbstractContextManager]


class AppError(Exception):
    """An error with an HTTP status and a message fit to show the user."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Rendered:
    id: str
    design: str
    preview: bytes          # PNG of the card as it should look
    job: bytes              # the exact printer job for this card
    warnings: list[str]


def _png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


class CardApp:
    """Everything the UI does, independent of HTTP."""

    def __init__(
        self,
        designs_dir: Path,
        rasteriser: Rasteriser | None = None,
        link_factory: LinkFactory | None = None,
        keep_renders: int = 8,
    ) -> None:
        self.designs_dir = Path(designs_dir)
        self.rasteriser = rasteriser or InkscapeShellRasteriser()
        self._link_factory = link_factory or (lambda: J1x0USB.discover())
        self._keep = keep_renders
        self._tmp = Path(tempfile.mkdtemp(prefix="j1x0-ui-"))
        (self._tmp / "uploads").mkdir()
        self._blank = self._tmp / "blank.png"
        Image.new("RGBA", (1, 1), (255, 255, 255, 0)).save(self._blank)
        self._lock = threading.Lock()
        self._renders: OrderedDict[str, Rendered] = OrderedDict()
        self._thumbs: dict[str, tuple[float, bytes]] = {}
        self._busy = False
        self._job: dict | None = None
        self._job_thread: threading.Thread | None = None

    def close(self) -> None:
        closer = getattr(self.rasteriser, "close", None)
        if closer:
            closer()
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── Designs ──────────────────────────────────────────────────────────────

    def designs(self) -> dict:
        found = discover(self.designs_dir)
        return {"dir": str(self.designs_dir),
                "designs": [{"name": d.name, "title": d.title} for d in found]}

    def _design(self, name: str) -> Design:
        for design in discover(self.designs_dir):
            if design.name == name:
                return design
        raise AppError(404, f"There is no design called {name!r}.")

    def _template(self, design: Design) -> Template:
        try:
            return design.load()
        except Exception as exc:                       # malformed XML and the like
            raise AppError(422, f"{design.title} is not a valid SVG file ({exc}).") from exc

    def design_info(self, name: str) -> dict:
        design = self._design(name)
        template = self._template(design)
        images = set(template.image_fields())
        fields = [{"name": f, "label": humanise(f), "kind": "image" if f in images else "text"}
                  for f in template.fields()]
        fields.sort(key=lambda f: f["kind"] == "image")     # file pickers after the text boxes
        return {"name": design.name, "title": design.title, "fields": fields}

    def thumbnail(self, name: str) -> bytes:
        """A small preview of the design with its field names as placeholder text."""
        design = self._design(name)
        mtime = design.path.stat().st_mtime
        cached = self._thumbs.get(name)
        if cached and cached[0] == mtime:
            return cached[1]
        template = self._template(design)
        images = set(template.image_fields())
        values = {f: str(self._blank) if f in images else humanise(f) for f in template.fields()}
        card = self._draw(template, values)
        image = composite(card)
        image.thumbnail((480, 300))
        png = _png(image)
        self._thumbs[name] = (mtime, png)
        return png

    # ── Uploads ──────────────────────────────────────────────────────────────

    def save_upload(self, data: bytes) -> str:
        """Store a picture for an image field; returns its id."""
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except Exception as exc:
            raise AppError(400, "That file is not a picture we can read. Use a PNG or JPEG.") from exc
        image = ImageOps.exif_transpose(image)           # phone photos carry a rotation flag
        image.thumbnail((1600, 1600))                    # the card is only 1024 px wide
        upload_id = secrets.token_hex(8)
        target = self._tmp / "uploads" / upload_id
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            image.convert("RGBA").save(target.with_suffix(".png"))
        else:
            image.convert("RGB").save(target.with_suffix(".jpg"), quality=92)
        return upload_id

    def _upload_path(self, upload_id: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{16}", upload_id or ""):
            for path in (self._tmp / "uploads").glob(f"{upload_id}.*"):
                return path
        return self._blank

    # ── Rendering ────────────────────────────────────────────────────────────

    def _draw(self, template: Template, values: dict[str, str]):
        try:
            return render_card(template, values, self.rasteriser)
        except MissingField as exc:
            raise AppError(422, f"The design needs a value for {exc}.") from exc
        except Exception as exc:                         # the rasteriser failed
            raise AppError(500, f"Could not draw the card: {exc}") from exc

    def render(self, design_name: str, values: dict) -> dict:
        """Render the card and keep its job. The id it returns is what Print sends."""
        design = self._design(design_name)
        template = self._template(design)
        images = set(template.image_fields())
        resolved = {}
        for field in template.fields():
            value = str(values.get(field, "") or "")
            resolved[field] = str(self._upload_path(value)) if field in images else value
        card = self._draw(template, resolved)
        printer = Printer(rasteriser=self.rasteriser, seed=secrets.randbits(32))
        warnings = []
        if isinstance(self.rasteriser, CairoRasteriser):
            warnings = [f"cairosvg can't draw: {p}" for p in template.cairosvg_problems()]
        rendered = Rendered(id=secrets.token_hex(8), design=design.name,
                            preview=_png(composite(card)), job=printer.encode(card),
                            warnings=warnings)
        with self._lock:
            self._renders[rendered.id] = rendered
            while len(self._renders) > self._keep:
                self._renders.popitem(last=False)
        return {"id": rendered.id, "warnings": warnings}

    def preview(self, render_id: str) -> bytes:
        with self._lock:
            rendered = self._renders.get(render_id)
        if rendered is None:
            raise AppError(404, "That preview is no longer available. Please render again.")
        return rendered.preview

    # ── Printer ──────────────────────────────────────────────────────────────

    def status(self, full: bool = False) -> dict:
        """Printer state for the status chip. Quick = the USB status byte, or one version query for
        a printer that has no status byte; full = the whole link check (slower)."""
        if self._busy:
            return {"state": "printing", "ready": False, "message": "Sending a card to the printer…"}
        try:
            with self._link_factory() as link:
                if full:
                    report = check_link(link, details=True)
                    return {"state": "ready" if report.ready else "attention", "ready": report.ready,
                            "message": "Printer ready" if report.ready else _attention(report.problems),
                            "lines": report.lines(), "problems": report.problems}
                ready, reason = quick_check(link)
                if ready:
                    return {"state": "ready", "ready": True, "message": "Printer ready"}
                return {"state": "attention", "ready": False,
                        "message": f"The printer is not ready: {reason}."}
        except DeviceSelectionError as exc:
            return _no_printer(str(exc))
        except RuntimeError as exc:
            text = str(exc)
            if "not found" in text.lower() or "no usb" in text.lower():
                return _no_printer(text)
            return {"state": "error", "ready": False, "message": f"Could not talk to the printer: {text}"}
        except Exception as exc:                         # usb.core.USBError and friends
            return {"state": "error", "ready": False, "message": f"Could not talk to the printer: {exc}"}

    def start_print(self, render_id: str, force: bool = False) -> dict:
        """Send the job behind a preview, in the background. One job at a time."""
        with self._lock:
            rendered = self._renders.get(render_id)
            if rendered is None:
                raise AppError(404, "That preview is no longer available. Please render again.")
            if self._busy:
                raise AppError(409, "A card is already being sent. Please wait.")
            self._busy = True
            self._job = {"id": secrets.token_hex(4), "state": "sending",
                         "message": "Checking the printer…"}
            job = self._job
            snapshot = dict(job)                        # what the caller sees: the job has just begun
        self._job_thread = threading.Thread(target=self._run_print, args=(job, rendered, force),
                                            daemon=True)
        self._job_thread.start()
        return snapshot

    def _run_print(self, job: dict, rendered: Rendered, force: bool) -> None:
        try:
            with self._link_factory() as link:
                report = check_link(link, details=False)
                if not report.ready and not force:
                    self._finish(job, "error", _attention(report.problems))
                    return

                def progress(sent: int, total: int) -> None:
                    job["message"] = f"Sending the card to the printer… {sent * 100 // total}%"

                result = send_job(link, rendered.job, progress=progress)
            self._finish(job, "done", f"Sent to the printer ({result.bytes_sent // 1024} KB). "
                                      "The card will print now.")
        except DeviceSelectionError as exc:
            self._finish(job, "error", _no_printer(str(exc))["message"])
        except Exception as exc:
            log.exception("print failed")
            self._finish(job, "error", f"Printing failed: {exc}")

    def _finish(self, job: dict, state: str, message: str) -> None:
        with self._lock:
            job["state"], job["message"] = state, message
            self._busy = False

    def job(self) -> dict | None:
        with self._lock:
            return dict(self._job) if self._job else None

    def wait_for_job(self, timeout: float = 10.0) -> None:
        """For tests: block until the background print finishes."""
        if self._job_thread is not None:
            self._job_thread.join(timeout)


def _attention(problems: list[str]) -> str:
    detail = "; ".join(problems) or "unknown problem"
    return f"The printer is not ready: {detail}."


def _no_printer(detail: str) -> dict:
    return {"state": "no_printer", "ready": False, "detail": detail,
            "message": "No printer found. Check the USB cable and that it is switched on."}


# ── HTTP ─────────────────────────────────────────────────────────────────────

_STATIC = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
           "style.css": "text/css; charset=utf-8"}
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _static(name: str) -> bytes:
    return resources.files(__package__).joinpath("web", name).read_bytes()


def make_server(app: CardApp, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                token: str | None = None) -> ThreadingHTTPServer:
    """Build (but do not start) the HTTP server. `server.token` is the per-run POST token."""
    token = token or secrets.token_urlsafe(16)
    strict_host = host in _LOOPBACK

    class Handler(BaseHTTPRequestHandler):
        server_version = "j1x0"

        def log_message(self, fmt, *args):          # keep the terminal quiet
            log.debug("%s %s", self.address_string(), fmt % args)

        # ── helpers ──
        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _body(self, limit: int) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > limit:
                raise AppError(413, "That is too large.")
            return self.rfile.read(length)

        def _json_body(self) -> dict:
            try:
                data = json.loads(self._body(MAX_JSON) or b"{}")
            except ValueError as exc:
                raise AppError(400, "Bad request.") from exc
            if not isinstance(data, dict):
                raise AppError(400, "Bad request.")
            return data

        def _host_ok(self) -> bool:
            if not strict_host:
                return True
            name = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return name in _LOOPBACK

        # ── dispatch ──
        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            try:
                if not self._host_ok():
                    raise AppError(403, "Forbidden.")
                parsed = urlparse(self.path)
                path = unquote(parsed.path)
                if method == "POST" and self.headers.get("X-J1x0-Token") != token:
                    raise AppError(403, "Forbidden.")
                self._route(method, path, parse_qs(parsed.query))
            except AppError as exc:
                self._json(exc.status, {"error": exc.message})
            except Exception as exc:
                log.exception("unhandled error")
                self._json(500, {"error": f"Something went wrong: {exc}"})

        def _route(self, method: str, path: str, query: dict) -> None:
            if method == "GET":
                if path in ("/", "/index.html"):
                    page = _static("index.html").replace(b"__TOKEN__", token.encode())
                    return self._send(200, page, _STATIC["index.html"])
                name = path.lstrip("/")
                if name in _STATIC:
                    return self._send(200, _static(name), _STATIC[name])
                if path == "/favicon.ico":
                    return self._send(204, b"", "image/x-icon")
                if path == "/api/designs":
                    return self._json(200, app.designs())
                if match := re.fullmatch(r"/api/designs/([^/]+)", path):
                    return self._json(200, app.design_info(match.group(1)))
                if match := re.fullmatch(r"/api/designs/([^/]+)/thumb\.png", path):
                    return self._send(200, app.thumbnail(match.group(1)), "image/png")
                if match := re.fullmatch(r"/api/preview/([0-9a-f]+)\.png", path):
                    return self._send(200, app.preview(match.group(1)), "image/png")
                if path == "/api/status":
                    return self._json(200, app.status(full=query.get("full") == ["1"]))
                if path == "/api/job":
                    return self._json(200, app.job() or {"state": "idle"})
            else:
                if path == "/api/upload":
                    return self._json(200, {"id": app.save_upload(self._body(MAX_UPLOAD))})
                if path == "/api/render":
                    data = self._json_body()
                    return self._json(200, app.render(str(data.get("design", "")), data.get("values") or {}))
                if path == "/api/print":
                    data = self._json_body()
                    return self._json(200, app.start_print(str(data.get("id", "")),
                                                           bool(data.get("force"))))
            raise AppError(404, "Not found.")

    server = ThreadingHTTPServer((host, port), Handler)
    server.token = token                                  # type: ignore[attr-defined]
    server.daemon_threads = True
    return server


def serve(app: CardApp, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          open_browser: bool = True) -> None:
    """Run the UI until interrupted."""
    try:
        server = make_server(app, host, port)
    except OSError:                                       # port taken: take any free one
        server = make_server(app, host, 0)
    shown = "localhost" if host in _LOOPBACK else host
    url = f"http://{shown}:{server.server_address[1]}/"
    print(f"j1x0 is running at {url}   (Ctrl-C to stop)")
    print(f"card designs: {app.designs_dir}")
    if host not in _LOOPBACK:
        print("warning: listening beyond this computer; anyone on the network can print")
    if open_browser:
        webbrowser.open(url)

    def stop(signum, frame):                              # `kill` should clean up like Ctrl-C does
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, stop)
    except ValueError:                                    # not the main thread (tests)
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
