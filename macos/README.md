# Card Printer for macOS

`build_app.py` builds **Card Printer.app**, a double-click version of `j1x0` for Apple Silicon Macs, and
optionally a signed, notarised `.dmg` to hand to someone. It is not part of the PyPI package.

The app starts the same local web page as `j1x0 ui` (on this computer only), opens it in the browser, and gives
it a Dock icon and a menu: open the page again, open the designs folder (`~/.j1x0`, created on first run with
the two example designs), show the log, quit. Quitting stops the page and Inkscape.

## What is inside

| | |
|---|---|
| `Contents/MacOS/CardPrinter` | a small launcher (`launcher.c`) that starts the bundled Python *in this process* |
| `Contents/Resources/python` | a relocatable CPython 3.13 ([python-build-standalone](https://github.com/astral-sh/python-build-standalone), checksum-verified) with `j1x0-cardprinter`, Pillow, pyusb, **cairosvg + cairocffi**, and PyObjC installed |
| `Contents/Frameworks` | `libusb` and **`libcairo`** with the libraries it needs (pixman, freetype, fontconfig, libpng, X11 client libraries, gettext): 15 libraries, 5 MB, copied from Homebrew with their paths rewritten to point at each other |
| `Contents/Resources/app/j1x0_macapp.py` | the menu, the Dock integration and the web server |
| `Contents/Resources/fontconfig` | the font configuration the bundled cairo uses (system fonts) |

The cairo renderer is `cairosvg`, which uses the **`cairocffi`** binding (not `pycairo`) and the native `libcairo`;
both are bundled, so the app can draw cards on a Mac with no Inkscape. **Inkscape is not bundled.** The app uses it
when it is installed (`/Applications/Inkscape.app`, found without a `PATH`) and its own cairo renderer otherwise,
which cannot draw every SVG feature (filters, masks, wrapped text) and picks different fonts for generic names such
as `sans-serif`; see `../docs/rendering.md`. The menu says which one is in use.

## Building

Needs an Apple Silicon Mac with the Xcode command line tools, and Homebrew with `cairo` and `libusb`
(`brew install cairo libusb`).

    python macos/build_app.py                      # dist/Card Printer.app, ad-hoc signed: runs on this Mac
    python macos/build_app.py --dmg                # and dist/Card-Printer-<version>-arm64.dmg

An ad-hoc build is fine for testing. On another Mac it is blocked by Gatekeeper ("cannot be opened because the
developer cannot be verified"): the person has to right-click the app and choose Open once. For a build that opens
with a double-click anywhere it must be signed with a **Developer ID Application** certificate and notarised.

The build ends with a self-test (below). The exact package versions are in `requirements-lock.txt`; delete it to
build with the latest, and copy the new `dist/Card Printer-requirements.txt` over it.

## Releasing: signing and notarising

Two things are needed once, on the Mac that builds releases:

1. **A "Developer ID Application" certificate** in the login keychain. (An "Apple Development" certificate cannot
   be notarised.) At developer.apple.com: Certificates, IDs & Profiles, Certificates, **+**, *Developer ID
   Application*, upload a certificate signing request made with Keychain Access (Certificate Assistant, Request a
   Certificate From a Certificate Authority), download the `.cer` and double-click it. It needs the Account Holder
   or Admin role. Check with `security find-identity -v -p codesigning`.
2. **Notarisation credentials**, stored in the keychain under a name of your choice. Create an app-specific
   password at appleid.apple.com, then:

       xcrun notarytool store-credentials "j1x0-notary" --apple-id YOU@EXAMPLE.COM --team-id TEAMID --password APP-SPECIFIC-PASSWORD

Then:

    python macos/build_app.py --version 0.1.1 --dmg --notarize j1x0-notary \
        --identity "Developer ID Application: Your Name (TEAMID)"

It signs every binary with the hardened runtime and a secure timestamp, notarises and staples the app, builds the
disk image from it, signs, notarises and staples that too. Check the result with:

    spctl -a -vv -t exec "dist/Card Printer.app"    # accepted, source=Notarized Developer ID
    xcrun stapler validate Card-Printer-0.1.1-arm64.dmg

The bundle identifier defaults to `com.teaengineering.cardprinter` (`--bundle-id` to change it).

## Testing

`CardPrinter --self-test` runs without a window and checks that libusb loads, cairo draws a card with text, the whole
job pipeline works, PyObjC imports, and that **no library is loaded from outside the app and the system** (so nothing
leaks in from Homebrew). Run it with a clean environment, as the build does:

    env -i HOME=$HOME PATH=/usr/bin:/bin "dist/Card Printer.app/Contents/MacOS/CardPrinter" --self-test

For a full run without opening a browser tab, and with a throwaway designs folder or a forced renderer:

    env -i HOME=$HOME PATH=/usr/bin:/bin J1X0_APP_NO_BROWSER=1 J1X0_TEMPLATES=/tmp/designs J1X0_RENDERER=cairosvg \
        "dist/Card Printer.app/Contents/MacOS/CardPrinter"

The log is `~/Library/Logs/Card Printer/card-printer.log`.

## Notes

* **Licences.** The app contains LGPL libraries (cairo, pixman, libusb, gettext) and cairosvg (LGPL-3.0). Each is a
  separate file that can be replaced, and the texts are in `Contents/Resources/licenses`.
* **Hardened runtime.** `entitlements.plist` allows library loading and executable memory, which Python, PyObjC and
  ctypes need. The app is not sandboxed (a sandboxed app needs a USB entitlement and cannot reach `~/.j1x0`).
* **The app never writes into itself** (it is signed): bytecode is compiled at build time and
  `PYTHONDONTWRITEBYTECODE` is set.
* **Intel Macs** are not supported; the launcher and every binary are arm64.
