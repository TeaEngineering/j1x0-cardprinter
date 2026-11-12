import http.client
import io
import json
import threading

import pytest
from PIL import Image

from j1x0_cardprinter import webui
from j1x0_cardprinter.protocol import end_of_card
from j1x0_cardprinter.raster import CairoRasteriser
from j1x0_cardprinter.usb import DeviceSelectionError, J1x0MockUSB
from j1x0_cardprinter.webui import AppError, CardApp, make_server

CARD_SVG = """<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
     width="85.6mm" height="54mm" viewBox="0 0 85.6 54">
  <g inkscape:groupmode="layer" inkscape:label="Design">
    <text x="5" y="10">$firstName$</text>
    <image x="50" y="5" width="30" height="40" xlink:href="$photo$"/>
  </g>
  <g inkscape:groupmode="layer" inkscape:label="K"><text x="5" y="50">ID $cardNo$</text></g>
</svg>"""


class FakeRasteriser:
    """Stands in for Inkscape: a horizontal gradient, so the card has colour and resin dots."""

    def render(self, svg, width, height):
        row = bytes(round(i * 255 / (width - 1)) for i in range(width))
        return Image.frombytes("L", (width, height), row * height).convert("RGB")


class FakeCairo(CairoRasteriser, FakeRasteriser):
    def render(self, svg, width, height):
        return FakeRasteriser.render(self, svg, width, height)


def png_bytes(size=(40, 30), mode="RGB", colour=(200, 50, 50)):
    buf = io.BytesIO()
    Image.new(mode, size, colour).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def designs(tmp_path):
    folder = tmp_path / "designs"
    folder.mkdir()
    (folder / "staff_badge.svg").write_text(CARD_SVG)
    return folder


@pytest.fixture
def link():
    return J1x0MockUSB()


@pytest.fixture
def app(designs, link):
    card_app = CardApp(designs, rasteriser=FakeRasteriser(), link_factory=lambda: link)
    yield card_app
    card_app.close()


VALUES = {"firstName": "Ada", "cardNo": "0042"}


# ── Designs and fields ───────────────────────────────────────────────────────

def test_designs_are_listed_with_readable_titles(app, designs):
    assert app.designs() == {"dir": str(designs), "designs": [{"name": "staff_badge", "title": "Staff badge"}]}


def test_design_info_gives_labelled_fields_with_pictures_last(app):
    info = app.design_info("staff_badge")
    assert info["title"] == "Staff badge"
    # In the SVG the photo comes before cardNo, but the form asks for the text boxes first.
    assert info["fields"] == [
        {"name": "firstName", "label": "First name", "kind": "text"},
        {"name": "cardNo", "label": "Card no", "kind": "text"},
        {"name": "photo", "label": "Photo", "kind": "image"},
    ]


def test_unknown_and_broken_designs_are_user_errors(app, designs):
    with pytest.raises(AppError) as exc:
        app.design_info("nope")
    assert exc.value.status == 404
    (designs / "broken.svg").write_text("<svg><unclosed>")
    with pytest.raises(AppError) as exc:
        app.design_info("broken")
    assert exc.value.status == 422 and "not a valid SVG" in exc.value.message


def test_thumbnails_are_small_png_previews_and_cached(app):
    first = app.thumbnail("staff_badge")
    image = Image.open(io.BytesIO(first))
    assert image.format == "PNG" and max(image.size) <= 480
    assert app.thumbnail("staff_badge") is first


# ── Rendering ────────────────────────────────────────────────────────────────

def test_render_keeps_a_preview_and_a_job(app):
    result = app.render("staff_badge", VALUES)
    assert result["warnings"] == []
    preview = Image.open(io.BytesIO(app.preview(result["id"])))
    assert preview.size == (1024, 640)
    assert app._renders[result["id"]].job.startswith(b"\x1bMASTER\r")


def test_missing_values_render_blank_rather_than_failing(app):
    assert app.render("staff_badge", {})["id"]


def test_only_the_most_recent_renders_are_kept(designs, link):
    small = CardApp(designs, rasteriser=FakeRasteriser(), link_factory=lambda: link, keep_renders=2)
    try:
        ids = [small.render("staff_badge", VALUES)["id"] for _ in range(3)]
        with pytest.raises(AppError):
            small.preview(ids[0])
        assert small.preview(ids[2])
    finally:
        small.close()


def test_cairosvg_renders_warn_about_unsupported_features(designs, link):
    (designs / "shadow.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><filter id="f"/><rect filter="url(#f)"/></svg>')
    app = CardApp(designs, rasteriser=FakeCairo(), link_factory=lambda: link)
    try:
        assert app.render("shadow", {})["warnings"] == ["cairosvg can't draw: filters (blur, drop shadow)"]
    finally:
        app.close()


def test_a_failing_rasteriser_becomes_a_readable_error(designs, link):
    class Boom:
        def render(self, svg, width, height):
            raise RuntimeError("Inkscape not found")

    app = CardApp(designs, rasteriser=Boom(), link_factory=lambda: link)
    try:
        with pytest.raises(AppError) as exc:
            app.render("staff_badge", VALUES)
        assert exc.value.status == 500 and "Inkscape not found" in exc.value.message
    finally:
        app.close()


# ── Uploads ──────────────────────────────────────────────────────────────────

def test_uploaded_photos_are_normalised_and_usable(app):
    upload_id = app.save_upload(png_bytes((3000, 2000)))
    stored = next((app._tmp / "uploads").glob(f"{upload_id}.*"))
    assert max(Image.open(stored).size) <= 1600
    assert app.render("staff_badge", {**VALUES, "photo": upload_id})["id"]


def test_transparent_uploads_keep_their_transparency(app):
    upload_id = app.save_upload(png_bytes((10, 10), "RGBA", (255, 0, 0, 0)))
    assert next((app._tmp / "uploads").glob(f"{upload_id}.*")).suffix == ".png"


def test_non_images_are_rejected(app):
    with pytest.raises(AppError) as exc:
        app.save_upload(b"definitely not a picture")
    assert exc.value.status == 400


def test_unknown_upload_ids_fall_back_to_the_blank_image(app):
    assert app._upload_path("../../etc/passwd") == app._blank
    assert app._upload_path("0123456789abcdef") == app._blank


# ── Status ───────────────────────────────────────────────────────────────────

def test_status_ready_attention_and_missing_printer(designs):
    ready = CardApp(designs, rasteriser=FakeRasteriser(), link_factory=lambda: J1x0MockUSB())
    assert ready.status()["state"] == "ready" and ready.status()["ready"]
    attention = CardApp(designs, rasteriser=FakeRasteriser(), link_factory=lambda: J1x0MockUSB(status=0x00))
    assert attention.status()["state"] == "attention"

    def no_printer():
        raise DeviceSelectionError("no USB printers found")

    missing = CardApp(designs, rasteriser=FakeRasteriser(), link_factory=no_printer)
    state = missing.status()
    assert state["state"] == "no_printer" and not state["ready"] and "USB cable" in state["message"]

    def broken():
        raise OSError("pipe error")

    assert CardApp(designs, rasteriser=FakeRasteriser(), link_factory=broken).status()["state"] == "error"


def test_full_status_includes_the_device_report(app):
    status = app.status(full=True)
    assert status["ready"] and any(line.startswith("model/firmware") for line in status["lines"])


# ── Printing ─────────────────────────────────────────────────────────────────

def test_print_sends_exactly_the_bytes_behind_the_preview(app, link):
    render = app.render("staff_badge", VALUES)
    app.start_print(render["id"])
    app.wait_for_job()
    assert app.job()["state"] == "done"
    assert link.stream == app._renders[render["id"]].job + end_of_card()
    assert "Sent to the printer" in app.job()["message"]


def test_a_printer_that_is_not_ready_blocks_the_job(designs):
    link = J1x0MockUSB(status=0x00)
    app = CardApp(designs, rasteriser=FakeRasteriser(), link_factory=lambda: link)
    try:
        render = app.render("staff_badge", VALUES)
        app.start_print(render["id"])
        app.wait_for_job()
        assert app.job()["state"] == "error" and "not ready" in app.job()["message"]
        assert link.sent == []
        app.start_print(render["id"])                    # asking again changes nothing: there is no override
        app.wait_for_job()
        assert app.job()["state"] == "error" and link.sent == []
    finally:
        app.close()


def test_print_check_skips_the_slow_informational_queries(app, link):
    app.start_print(app.render("staff_badge", VALUES)["id"])
    app.wait_for_job()
    assert len(link.queries) == 3


def test_only_one_job_at_a_time(designs):
    gate = threading.Event()

    class Slow(J1x0MockUSB):
        def write(self, data):
            gate.wait(5)
            return super().write(data)

    slow = Slow()
    app = CardApp(designs, rasteriser=FakeRasteriser(), link_factory=lambda: slow)
    try:
        render = app.render("staff_badge", VALUES)
        app.start_print(render["id"])
        with pytest.raises(AppError) as exc:
            app.start_print(render["id"])
        assert exc.value.status == 409
        assert app.status()["state"] == "printing"
        gate.set()
        app.wait_for_job()
        assert app.job()["state"] == "done" and app.status()["state"] == "ready"
    finally:
        gate.set()
        app.close()


def test_printing_an_unknown_render_is_refused(app):
    with pytest.raises(AppError) as exc:
        app.start_print("deadbeef")
    assert exc.value.status == 404


def test_a_usb_failure_while_sending_is_reported(designs):
    class Failing(J1x0MockUSB):
        def write(self, data):
            raise OSError("Operation timed out")

    app = CardApp(designs, rasteriser=FakeRasteriser(), link_factory=lambda: Failing())
    try:
        app.start_print(app.render("staff_badge", VALUES)["id"])
        app.wait_for_job()
        assert app.job()["state"] == "error" and "Operation timed out" in app.job()["message"]
        assert app.status()["state"] != "printing"                 # the lock is released
    finally:
        app.close()


# ── Uploading a template ─────────────────────────────────────────────────────

TEMPLATE_SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" width="85.6mm" height="54mm" viewBox="0 0 85.6 54">'
                b'<text x="5" y="10">$name$</text></svg>')


def test_an_uploaded_template_is_stored_in_the_designs_folder_and_listed(app, designs):
    result = app.save_design("My Card.svg", TEMPLATE_SVG)
    assert result == {"name": "My Card", "title": "My card", "replaced": False}
    assert (designs / "My Card.svg").read_bytes() == TEMPLATE_SVG
    assert {"name": "My Card", "title": "My card"} in app.designs()["designs"]
    assert app.design_info("My Card")["fields"] == [{"name": "name", "label": "Name", "kind": "text"}]
    assert app.render("My Card", {"name": "Ada"})["id"]                       # and it can be used straight away


def test_uploading_the_same_name_again_replaces_it(app, designs):
    app.save_design("card.svg", TEMPLATE_SVG)
    newer = TEMPLATE_SVG.replace(b"$name$", b"$title$")
    assert app.save_design("card.svg", newer)["replaced"] is True
    assert (designs / "card.svg").read_bytes() == newer
    assert sorted(p.name for p in designs.iterdir()) == ["card.svg", "staff_badge.svg"]      # no temporary files left


@pytest.mark.parametrize("sent, stored", [
    ("../../etc/evil.svg", "evil"),                      # only the file name counts, never a path
    ("C:\\Users\\me\\Card.SVG", "Card"),
    ("weird:name*?.svg", "weird_name"),
    (".hidden.svg", "hidden"),                           # a dot file would not be listed
    ("noextension", "noextension"),
    ("", "design"),
    ("Zoë café.svg", "Zoë café"),
    ("x" * 200 + ".svg", "x" * 80),
])
def test_uploaded_names_are_made_safe(app, designs, sent, stored):
    assert app.save_design(sent, TEMPLATE_SVG)["name"] == stored
    assert (designs / f"{stored}.svg").exists()
    assert all(p.parent == designs for p in designs.rglob("*.svg"))              # nothing outside the folder


@pytest.mark.parametrize("data, why", [
    (b"", "empty"),
    (b"this is not xml", "not a valid SVG"),
    (b"<svg><unclosed>", "not a valid SVG"),
    (b'<html xmlns="http://www.w3.org/1999/xhtml"><body/></html>', "not an SVG"),
    (b"\xff\xfe binary \x00", "not a valid SVG"),
])
def test_a_file_that_is_not_an_svg_is_refused_and_nothing_is_stored(app, designs, data, why):
    with pytest.raises(AppError) as exc:
        app.save_design("bad.svg", data)
    assert exc.value.status == 400 and why in exc.value.message
    assert [p.name for p in designs.iterdir()] == ["staff_badge.svg"]


def test_the_designs_folder_is_created_on_the_first_upload(tmp_path):
    folder = tmp_path / "does" / "not" / "exist"
    card_app = CardApp(folder, rasteriser=FakeRasteriser(), link_factory=lambda: J1x0MockUSB())
    try:
        card_app.save_design("first.svg", TEMPLATE_SVG)
        assert (folder / "first.svg").exists()
    finally:
        card_app.close()


# ── HTTP ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def server(app):
    httpd = make_server(app, port=0)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def call(server, method, path, body=None, token=True, headers=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    all_headers = dict(headers or {})
    if token:
        all_headers["X-J1x0-Token"] = server.token
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        all_headers["Content-Type"] = "application/json"
    if host is None:
        conn.request(method, path, body=body, headers=all_headers)
    else:
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host)
        for key, value in all_headers.items():
            conn.putheader(key, value)
        conn.putheader("Content-Length", str(len(body or b"")))
        conn.endheaders(body)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, response.headers, data


def test_the_page_and_assets_are_served_with_the_token(server):
    status, headers, body = call(server, "GET", "/", token=False)
    assert status == 200 and server.token.encode() in body and b"__TOKEN__" not in body
    for asset, kind in (("/app.js", "javascript"), ("/style.css", "css")):
        status, headers, body = call(server, "GET", asset, token=False)
        assert status == 200 and kind in headers["Content-Type"] and body


def test_posts_need_the_token(server):
    status, _, _ = call(server, "POST", "/api/render", {"design": "staff_badge"}, token=False)
    assert status == 403
    status, _, _ = call(server, "POST", "/api/render", {"design": "staff_badge"},
                        headers={"X-J1x0-Token": "wrong"}, token=False)
    assert status == 403


def test_other_host_names_are_refused(server):
    status, _, _ = call(server, "GET", "/api/designs", host="evil.example")
    assert status == 403
    status, _, _ = call(server, "GET", "/api/designs", host="localhost:1234")
    assert status == 200


def test_the_whole_flow_over_http(server, link):
    status, _, body = call(server, "GET", "/api/designs")
    assert status == 200 and json.loads(body)["designs"][0]["name"] == "staff_badge"

    status, _, body = call(server, "GET", "/api/designs/staff_badge")
    assert [f["name"] for f in json.loads(body)["fields"]] == ["firstName", "cardNo", "photo"]

    status, headers, body = call(server, "GET", "/api/designs/staff_badge/thumb.png")
    assert status == 200 and headers["Content-Type"] == "image/png" and body[:4] == b"\x89PNG"

    status, _, body = call(server, "POST", "/api/upload", png_bytes(), headers={"Content-Type": "image/png"})
    upload_id = json.loads(body)["id"]

    status, _, body = call(server, "POST", "/api/render",
                           {"design": "staff_badge", "values": {**VALUES, "photo": upload_id}})
    render_id = json.loads(body)["id"]
    assert status == 200

    status, headers, body = call(server, "GET", f"/api/preview/{render_id}.png")
    assert status == 200 and Image.open(io.BytesIO(body)).size == (1024, 640)

    status, _, body = call(server, "GET", "/api/status")
    assert json.loads(body)["state"] == "ready"

    status, _, body = call(server, "POST", "/api/print", {"id": render_id})
    assert status == 200 and json.loads(body)["state"] == "sending"
    for _ in range(100):
        _, _, body = call(server, "GET", "/api/job")
        if json.loads(body)["state"] != "sending":
            break
        threading.Event().wait(0.05)
    assert json.loads(body)["state"] == "done"
    assert link.stream.startswith(b"\x1bMASTER\r") and len(link.stream) > 1000


def test_errors_are_json_with_the_right_status(server):
    for method, path, body, expected in (
        ("GET", "/api/designs/nope", None, 404),
        ("GET", "/api/nothing", None, 404),
        ("GET", "/api/preview/abcdef.png", None, 404),
        ("POST", "/api/upload", b"not an image", 400),
        ("POST", "/api/print", {"id": "abcdef"}, 404),
        ("POST", "/api/render", {"design": "nope"}, 404),
    ):
        status, headers, data = call(server, method, path, body)
        assert status == expected, path
        assert "error" in json.loads(data)


def test_oversized_uploads_are_refused(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    conn.putrequest("POST", "/api/upload")
    conn.putheader("X-J1x0-Token", server.token)
    conn.putheader("Content-Length", str(webui.MAX_UPLOAD + 1))
    conn.endheaders()
    assert conn.getresponse().status == 413
    conn.close()


def test_bad_json_is_a_400(server):
    status, _, _ = call(server, "POST", "/api/render", b"{not json", headers={"Content-Type": "application/json"})
    assert status == 400


def test_a_template_can_be_uploaded_over_http(server, designs):
    from urllib.parse import quote
    status, _, body = call(server, "POST", "/api/designs", TEMPLATE_SVG,
                           headers={"X-Filename": quote("Zoë café.svg"), "Content-Type": "image/svg+xml"})
    assert status == 200
    assert json.loads(body) == {"name": "Zoë café", "title": "Zoë café", "replaced": False}
    assert (designs / "Zoë café.svg").exists()
    status, _, body = call(server, "GET", "/api/designs")
    assert "Zoë café" in [d["name"] for d in json.loads(body)["designs"]]


def test_uploading_a_template_needs_the_token(server, designs):
    status, _, _ = call(server, "POST", "/api/designs", TEMPLATE_SVG, token=False, headers={"X-Filename": "a.svg"})
    assert status == 403 and [p.name for p in designs.iterdir()] == ["staff_badge.svg"]


def test_a_bad_or_oversized_template_upload_is_refused_over_http(server, designs, monkeypatch):
    status, _, body = call(server, "POST", "/api/designs", b"not svg", headers={"X-Filename": "a.svg"})
    assert status == 400 and "not a valid SVG" in json.loads(body)["error"]
    monkeypatch.setattr(webui, "MAX_DESIGN", 100)
    status, _, _ = call(server, "POST", "/api/designs", TEMPLATE_SVG, headers={"X-Filename": "big.svg"})
    assert status == 413
    assert [p.name for p in designs.iterdir()] == ["staff_badge.svg"]


def test_the_print_endpoint_ignores_any_attempt_to_override_a_not_ready_printer(designs):
    link = J1x0MockUSB(status=0x00)
    card_app = CardApp(designs, rasteriser=FakeRasteriser(), link_factory=lambda: link)
    httpd = make_server(card_app, port=0)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        _, _, body = call(httpd, "POST", "/api/render", {"design": "staff_badge", "values": VALUES})
        render_id = json.loads(body)["id"]
        status, _, _ = call(httpd, "POST", "/api/print", {"id": render_id, "force": True})
        assert status == 200
        card_app.wait_for_job()
        assert card_app.job()["state"] == "error" and link.sent == []
    finally:
        httpd.shutdown()
        httpd.server_close()
        card_app.close()
