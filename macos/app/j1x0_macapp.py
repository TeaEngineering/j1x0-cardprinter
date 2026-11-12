"""J1x0 Card Printer.app: a thin macOS shell around the j1x0 web UI.

The app starts the same local web page `j1x0` serves, on this computer only, opens it in the browser, and
gives it a Dock icon and a menu (open it again, open the designs folder, quit). Quitting stops the page and
the Inkscape process behind it.

It runs inside the app bundle (see macos/launcher.c), where the native libraries sit in Contents/Frameworks.
`use_bundled_libraries` points ctypes at them, so it MUST run before pyusb or cairocffi is imported. It also
runs from a source checkout (`python macos/app/j1x0_macapp.py`), using whatever the system provides.
"""

from __future__ import annotations

import ctypes.util
import logging
import os
import sys
import threading
from pathlib import Path

CONTENTS = Path(os.environ["J1X0_APP_CONTENTS"]) if os.environ.get("J1X0_APP_CONTENTS") else None
FRAMEWORKS = CONTENTS / "Frameworks" if CONTENTS else None
RESOURCES = CONTENTS / "Resources" if CONTENTS else None
LOG = Path.home() / "Library" / "Logs" / "J1x0 Card Printer" / "card-printer.log"

# ctypes.util.find_library names -> the library file shipped in Contents/Frameworks
BUNDLED = {
    "usb-1.0": "libusb-1.0.0.dylib", "libusb-1.0": "libusb-1.0.0.dylib", "usb": "libusb-1.0.0.dylib",
    "cairo-2": "libcairo.2.dylib", "cairo": "libcairo.2.dylib", "libcairo-2": "libcairo.2.dylib",
}

log = logging.getLogger("j1x0_macapp")


def use_bundled_libraries() -> None:
    """Make pyusb (libusb) and cairocffi (libcairo) load the copies inside the app, and fontconfig use the
    app's font configuration. A no-op outside the bundle."""
    if FRAMEWORKS is None:
        return
    original = ctypes.util.find_library

    def find_library(name: str):
        leaf = BUNDLED.get(name)
        if leaf and (FRAMEWORKS / leaf).exists():
            return str(FRAMEWORKS / leaf)
        return original(name)

    ctypes.util.find_library = find_library
    config = RESOURCES / "fontconfig" / "fonts.conf"
    if config.exists():
        os.environ["FONTCONFIG_FILE"] = str(config)
        os.environ["FONTCONFIG_PATH"] = str(config.parent)


def set_up_logging() -> None:
    """Finder gives an app no terminal, so keep a log file (and send stdout and stderr to it)."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 1_000_000:
        LOG.replace(LOG.with_suffix(".log.old"))
    stream = open(LOG, "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = stream
    logging.basicConfig(stream=stream, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def designs_folder() -> Path:
    """The folder of card designs, made on first run with the example designs in it."""
    from j1x0_cardprinter.library import default_dir

    folder = default_dir()
    if not folder.exists():
        folder.mkdir(parents=True)
        examples = RESOURCES / "examples" if RESOURCES else Path(__file__).resolve().parents[2] / "examples"
        for svg in sorted(examples.glob("*.svg")) if examples.is_dir() else []:
            (folder / svg.name).write_bytes(svg.read_bytes())
    return folder


class Controller:
    """The web page: build it, run it in a thread, stop it."""

    def __init__(self) -> None:
        self.server = None
        self.app = None
        self.url = ""
        self.designs: Path | None = None
        self.renderer = ""

    def start(self) -> None:
        from j1x0_cardprinter.raster import CairoRasteriser, make_rasteriser
        from j1x0_cardprinter.usb import J1x0USB
        from j1x0_cardprinter.webui import DEFAULT_PORT, CardApp, make_server

        rasteriser = make_rasteriser(os.environ.get("J1X0_RENDERER", "auto"))      # the override is for testing and support
        self.renderer = "the built-in renderer" if isinstance(rasteriser, CairoRasteriser) else "Inkscape"
        self.designs = designs_folder()
        self.app = CardApp(self.designs, rasteriser=rasteriser, link_factory=lambda: J1x0USB.discover())
        try:
            self.server = make_server(self.app, "127.0.0.1", DEFAULT_PORT)
        except OSError:                                    # the port is taken: any free one will do
            self.server = make_server(self.app, "127.0.0.1", 0)
        self.url = f"http://localhost:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, name="web", daemon=True).start()
        log.info("serving %s; designs in %s; drawing with %s", self.url, self.designs, self.renderer)

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.app is not None:
            self.app.close()                               # stops Inkscape and removes the temporary files
        log.info("stopped")


def run_app() -> None:
    from AppKit import (NSAlert, NSApplication, NSApplicationActivationPolicyRegular, NSBundle, NSMenu, NSMenuItem,
                        NSWorkspace)
    from Foundation import NSObject, NSURL

    controller = Controller()

    def open_url(url: str) -> None:
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))

    class Delegate(NSObject):
        def applicationDidFinishLaunching_(self, notification):
            try:
                controller.start()
            except Exception as exc:                       # no renderer, port trouble, ...
                log.exception("could not start")
                alert = NSAlert.alloc().init()
                alert.setMessageText_("J1x0 Card Printer could not start")
                alert.setInformativeText_(f"{exc}\n\nThe log is at {LOG}")
                alert.runModal()
                NSApplication.sharedApplication().terminate_(None)
                return
            self.build_menu()
            if not os.environ.get("J1X0_APP_NO_BROWSER"):      # the switch is for automated tests
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                open_url(controller.url)

        def applicationShouldHandleReopen_hasVisibleWindows_(self, application, visible):
            if controller.url:                             # a click on the Dock icon: show the page again
                open_url(controller.url)
            return True

        def applicationWillTerminate_(self, notification):
            controller.stop()

        def openInterface_(self, sender):
            open_url(controller.url)

        def openDesignsFolder_(self, sender):
            NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(str(controller.designs)))

        def showLog_(self, sender):
            NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(str(LOG)))

        def getInkscape_(self, sender):
            open_url("https://inkscape.org/release/")

        def build_menu(self):
            application = NSApplication.sharedApplication()
            name = NSBundle.mainBundle().infoDictionary().get("CFBundleName") or "J1x0 Card Printer"
            menubar, top, menu = NSMenu.alloc().init(), NSMenuItem.alloc().init(), NSMenu.alloc().init()
            menubar.addItem_(top)
            top.setSubmenu_(menu)

            def add(title, action=None, key="", target=self, enabled=True):
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
                item.setTarget_(target)
                item.setEnabled_(enabled)
                menu.addItem_(item)

            add(f"About {name}", "orderFrontStandardAboutPanel:", target=None)
            menu.addItem_(NSMenuItem.separatorItem())
            add(f"Open {name}", "openInterface:", "o")
            add("Open Designs Folder", "openDesignsFolder:", "d")
            add("Show Log", "showLog:")
            menu.addItem_(NSMenuItem.separatorItem())
            add(f"Drawing cards with {controller.renderer}", enabled=False)
            if controller.renderer != "Inkscape":
                add("Get Inkscape for Best Results…", "getInkscape:")
            menu.addItem_(NSMenuItem.separatorItem())
            add(f"Hide {name}", "hide:", "h", target=None)
            add(f"Quit {name}", "terminate:", "q", target=None)
            application.setMainMenu_(menubar)

    application = NSApplication.sharedApplication()
    application.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    delegate = Delegate.alloc().init()
    application.setDelegate_(delegate)
    application.run()


def loaded_libraries() -> list[str]:
    """Every dynamic library loaded into this process, as the dynamic linker reports it."""
    dyld = ctypes.CDLL(None)
    dyld._dyld_get_image_name.restype = ctypes.c_char_p
    dyld._dyld_get_image_name.argtypes = [ctypes.c_uint32]
    return [dyld._dyld_get_image_name(i).decode() for i in range(dyld._dyld_image_count())]


def self_test() -> int:
    """`CardPrinter --self-test`: load and use everything the app depends on, with no window, and report where
    each native library came from. Exit 0 only if all of it works and none of it comes from outside the system and
    the app itself."""
    import io
    import json

    use_bundled_libraries()
    report: dict = {"python": sys.version.split()[0], "in_bundle": CONTENTS is not None}
    problems: list[str] = []

    def check(name, fn):
        try:
            report[name] = fn()
        except Exception as exc:                            # a failed check is a result
            report[name] = f"FAILED: {type(exc).__name__}: {exc}"
            problems.append(name)

    def libusb():
        import usb.backend.libusb1 as backend
        assert backend.get_backend() is not None, "no libusb backend"
        return backend._lib._name

    def cairo():
        import cairosvg
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="85.6mm" height="54mm" viewBox="0 0 85.6 54">'
               b'<rect width="85.6" height="54" fill="#fff"/><rect x="5" y="5" width="30" height="10" fill="#c00"/>'
               b'<text x="5" y="30" font-family="sans-serif" font-size="6">Ada Lovelace 0042</text></svg>')
        from PIL import Image
        png = cairosvg.svg2png(bytestring=svg, output_width=1024, output_height=640, background_color="white")
        image = Image.open(io.BytesIO(png)).convert("RGB")
        text_pixels = sum(1 for p in image.crop((50, 300, 500, 420)).tobytes()[::3] if p < 128)
        assert text_pixels > 100, f"the text did not draw ({text_pixels} dark pixels)"
        return f"{image.size[0]}x{image.size[1]}, {text_pixels} dark pixels of text"

    def pipeline():
        import random
        from j1x0_cardprinter.printer import Printer
        from j1x0_cardprinter.raster import CairoRasteriser
        from j1x0_cardprinter.template import Template
        examples = RESOURCES / "examples" if RESOURCES else Path(__file__).resolve().parents[2] / "examples"
        card = Printer(rasteriser=CairoRasteriser(), seed=1).build(Template.from_file(examples / "test-card.svg"), {})
        assert card.startswith(b"\x1bMASTER\r") and len(card) == 1409256, f"unexpected job of {len(card)} bytes"
        return f"{len(card)} byte job"

    def appkit():
        import AppKit
        return f"AppKit {AppKit.NSApplication.__name__}"

    def inkscape():
        from j1x0_cardprinter.raster import find_inkscape
        return find_inkscape() or "not installed (the built-in renderer will be used)"

    for name, fn in (("libusb", libusb), ("cairo_render", cairo), ("job_pipeline", pipeline), ("appkit", appkit),
                     ("inkscape", inkscape)):
        check(name, fn)

    outside = sorted(p for p in loaded_libraries()
                     if not p.startswith(("/usr/lib/", "/System/", "/Library/Apple/"))
                     and not (CONTENTS and p.startswith(str(CONTENTS))))
    report["libraries_from_outside_the_app_and_system"] = outside
    if outside and CONTENTS:
        problems.append("libraries loaded from outside the app")
    print(json.dumps(report, indent=2))
    return 1 if problems else 0


def main() -> None:
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    use_bundled_libraries()
    set_up_logging()
    log.info("starting (python %s)", sys.version.split()[0])
    try:
        run_app()
    except Exception:
        log.exception("the app crashed")
        raise


if __name__ == "__main__":
    main()
