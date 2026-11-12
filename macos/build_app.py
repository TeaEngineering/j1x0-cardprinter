#!/usr/bin/env python3
"""Build "J1x0 Card Printer.app" for Apple Silicon Macs, and optionally a signed, notarised .dmg.

    python macos/build_app.py                                  # a local app: ad-hoc signed, runs on this Mac
    python macos/build_app.py --identity "Developer ID Application: Name (TEAMID)" --dmg --notarize PROFILE

Needs, on the build Mac: Xcode command line tools (clang, codesign, hdiutil), Homebrew with `cairo` and `libusb`
installed, and network access (the Python runtime and the packages are downloaded). See macos/README.md.

What goes in the app:
    Contents/MacOS/CardPrinter        a small launcher (launcher.c) that embeds the Python below
    Contents/Resources/python         a relocatable CPython (python-build-standalone) with the packages installed
    Contents/Resources/app            j1x0_macapp.py: the menu, the Dock icon and the web server
    Contents/Frameworks               libusb, and libcairo with the libraries it needs, with their paths fixed
    Contents/Resources/{examples,fontconfig,licenses}
Inkscape is not bundled: the app uses it when it is installed and its own cairo renderer otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"

APP_NAME = "J1x0 Card Printer"
EXECUTABLE = "CardPrinter"
DEFAULT_BUNDLE_ID = "com.teaengineering.cardprinter"
MIN_MACOS = "13.0"

# A relocatable Python: https://github.com/astral-sh/python-build-standalone (checksum from its SHA256SUMS)
PYTHON_MM = "3.13"
PYTHON_FILE = "cpython-3.13.18+20261110-aarch64-apple-darwin-install_only.tar.gz"
PYTHON_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/20261110/"
              "cpython-3.13.18%2B20261110-aarch64-apple-darwin-install_only.tar.gz")
PYTHON_SHA256 = "1afd3983237c2a10277345f235be730e9cb49a123e0574ed252dbf3a841a24e4"

EXTRA_PACKAGES = ["pyobjc-framework-Cocoa"]               # the menu and Dock integration (j1x0_macapp)
LOCK = HERE / "requirements-lock.txt"                      # exact versions of everything pip installs

# Parts of the Python install the app does not need
PRUNE = [
    "bin", "include", "share", "lib/pkgconfig", "lib/itcl*", "lib/tcl*", "lib/tk*", "lib/thread*", "lib/libtcl*",
    "lib/libtk*", "lib/*.a", f"lib/python{PYTHON_MM}/test", f"lib/python{PYTHON_MM}/idlelib",
    f"lib/python{PYTHON_MM}/tkinter", f"lib/python{PYTHON_MM}/turtledemo", f"lib/python{PYTHON_MM}/turtle.py",
    f"lib/python{PYTHON_MM}/ensurepip", f"lib/python{PYTHON_MM}/config-{PYTHON_MM}-darwin",
    f"lib/python{PYTHON_MM}/lib-dynload/_tkinter*", f"lib/python{PYTHON_MM}/lib-dynload/_test*",
    f"lib/python{PYTHON_MM}/lib-dynload/xxlimited*", f"lib/python{PYTHON_MM}/lib-dynload/_xx*",
]

HOMEBREW = Path(subprocess.run(["brew", "--prefix"], capture_output=True, text=True).stdout.strip() or "/opt/homebrew")
NATIVE_ROOTS = ["libcairo.2.dylib", "libusb-1.0.0.dylib"]


def say(message: str) -> None:
    print(f"==> {message}", flush=True)


def run(cmd, **kwargs):
    kwargs.setdefault("check", True)
    return subprocess.run([str(c) for c in cmd], **kwargs)


def out(cmd, **kwargs) -> str:
    return run(cmd, capture_output=True, text=True, **kwargs).stdout


# ── Python runtime and packages ───────────────────────────────────────────────────────────────────

def fetch_python() -> Path:
    CACHE.mkdir(exist_ok=True)
    path = CACHE / PYTHON_FILE
    if not path.exists():
        say(f"downloading {PYTHON_FILE}")
        with urllib.request.urlopen(PYTHON_URL) as response, open(path, "wb") as f:
            shutil.copyfileobj(response, f)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != PYTHON_SHA256:
        path.unlink()
        sys.exit(f"the downloaded Python does not match its checksum ({digest}); deleted it")
    return path


def build_wheel(version: str, directory: Path) -> Path:
    say(f"building the j1x0-cardprinter wheel ({version})")
    env = dict(os.environ, SETUPTOOLS_SCM_PRETEND_VERSION=version)
    run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", directory, ROOT], env=env)
    return next(directory.glob("j1x0_cardprinter-*.whl"))


def install_python(contents: Path, wheel: Path) -> Path:
    say("installing Python and the packages")
    resources = contents / "Resources"
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fetch_python()) as tar:
            tar.extractall(tmp, filter="data")
        shutil.move(str(Path(tmp) / "python"), resources / "python")
    home = resources / "python"
    python = home / "bin" / f"python{PYTHON_MM}"
    pip = [python, "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check", "-q"]
    if LOCK.exists():
        pip += ["-c", LOCK]
    run(pip + [f"{wheel}[svg]", *EXTRA_PACKAGES])
    frozen = out([python, "-m", "pip", "freeze", "--exclude", "j1x0-cardprinter"])
    (contents.parent.parent / f"{contents.parent.stem}-requirements.txt").write_text(frozen)
    run([python, "-m", "pip", "uninstall", "-y", "-q", "pip", "setuptools", "wheel"], check=False)
    say("pre-compiling Python files (the app never writes into itself: it is signed)")
    run([python, "-m", "compileall", "-q", "-j", "0", "-s", str(contents.parent), "-p", f"/Applications/{APP_NAME}.app",
         home / "lib"], check=False)                       # a few stdlib test files do not compile; they are pruned below
    for pattern in PRUNE:
        for path in home.glob(pattern):
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    for cache in home.rglob("__pycache__"):                # drop the caches of pruned modules
        if not any(cache.iterdir()):
            cache.rmdir()
    return home


# ── Native libraries ──────────────────────────────────────────────────────────────────────────────

def otool_deps(path: Path) -> list[str]:
    return [line.split()[0] for line in out(["otool", "-L", path]).splitlines()[1:]]


def bundle_libraries(frameworks: Path) -> list[Path]:
    """Copy libcairo and libusb with everything of Homebrew's they load into Contents/Frameworks, and rewrite each
    library's references to its neighbours so nothing is loaded from /opt/homebrew."""
    say("bundling libcairo, libusb and their libraries")
    frameworks.mkdir(parents=True, exist_ok=True)
    sources: dict[str, Path] = {}                          # the name a library is referred to by -> its real file

    def locate(reference: str) -> Path | None:
        """The real file behind a Homebrew library reference, or None for a system library."""
        if reference.startswith(str(HOMEBREW)):
            return Path(reference).resolve()
        if reference.startswith(("@rpath/", "@loader_path/")):
            candidate = HOMEBREW / "lib" / Path(reference).name
            return candidate.resolve() if candidate.exists() else None
        return None

    queue = [(name, (HOMEBREW / "lib" / name).resolve()) for name in NATIVE_ROOTS]
    while queue:
        leaf, source = queue.pop()
        if leaf in sources:
            continue
        sources[leaf] = source
        for reference in otool_deps(source)[1:]:           # [0] is the library's own install name
            found = locate(reference)
            if found:
                queue.append((Path(reference).name, found))

    copied = []
    for leaf, source in sources.items():
        target = frameworks / leaf
        shutil.copy2(source, target)
        target.chmod(0o755)
        run(["install_name_tool", "-id", f"@loader_path/{leaf}", target], stderr=subprocess.DEVNULL)
        for reference in otool_deps(target)[1:]:           # [0] is the library's own id
            if locate(reference) is not None:
                run(["install_name_tool", "-change", reference, f"@loader_path/{Path(reference).name}", target],
                    stderr=subprocess.DEVNULL)
        for rpath in re.findall(r"path (\S+) \(offset", out(["otool", "-l", target])):
            run(["install_name_tool", "-delete_rpath", rpath, target], check=False, stderr=subprocess.DEVNULL)
        run(["codesign", "--force", "--sign", "-", target], stderr=subprocess.DEVNULL)   # install_name_tool voids it
        copied.append(target)
    left = [(t.name, d) for t in copied for d in otool_deps(t) if d.startswith(("/opt/", "/usr/local/", str(HOMEBREW)))]
    if left:
        sys.exit(f"these bundled libraries still point outside the app: {left}")
    say(f"   {len(copied)} libraries, {sum(t.stat().st_size for t in copied) / 1e6:.1f} MB")
    return copied


def copy_fontconfig(resources: Path) -> None:
    source = HOMEBREW / "etc" / "fonts"
    target = resources / "fontconfig"
    target.mkdir(parents=True, exist_ok=True)
    text = (source / "fonts.conf").read_text()
    text = text.replace(f"{HOMEBREW}/var/cache/fontconfig", f"~/Library/Caches/{APP_NAME}/fontconfig")
    (target / "fonts.conf").write_text(text)
    (target / "conf.d").mkdir(exist_ok=True)
    for conf in sorted((source / "conf.d").glob("*.conf")):
        shutil.copy2(conf.resolve(), target / "conf.d" / conf.name)


def gather_licences(resources: Path, libraries: list[Path], site_packages: Path) -> None:
    say("collecting licence texts")
    destination = resources / "licenses"
    destination.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", destination / "j1x0-cardprinter-LICENSE.txt")
    for dist in sorted(site_packages.glob("*.dist-info")):
        for found in list(dist.rglob("LICENSE*")) + list(dist.rglob("COPYING*")) + list(dist.rglob("NOTICE*")):
            if found.is_file():
                shutil.copy2(found, destination / f"{dist.name.split('-')[0]}-{found.name}")
    formulae = set()
    for library in libraries:
        match = re.search(r"/Cellar/([^/]+)/", str((HOMEBREW / "lib" / library.name).resolve()))
        if match:
            formulae.add(match.group(1))
    for formula in sorted(formulae):
        for found in (HOMEBREW / "opt" / formula).glob("*"):
            if re.match(r"(LICENSE|COPYING|COPYRIGHT)", found.name, re.I) and found.is_file():
                shutil.copy2(found, destination / f"{formula}-{found.name}")
    (destination / "NOTICE.txt").write_text(
        f"{APP_NAME} contains open source software. The licence texts are in this folder.\n\n"
        "j1x0-cardprinter (MIT), Python (PSF), Pillow, pyusb, cairosvg (LGPL-3.0), cairocffi, cssselect2, tinycss2,\n"
        "PyObjC (MIT), and these native libraries from Homebrew: " + ", ".join(sorted(formulae)) + ".\n"
        "cairo, pixman, libusb and gettext are LGPL or similar: they are separate library files in Contents/Frameworks\n"
        "and cairosvg is plain Python in Contents/Resources/python/lib, so each can be replaced. Their source code is\n"
        "available from their projects and from Homebrew.\n")


# ── The app itself ────────────────────────────────────────────────────────────────────────────────

def make_info_plist(contents: Path, version: str, bundle_id: str) -> None:
    short = re.match(r"\d+(\.\d+){0,2}", version).group(0)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME, "CFBundleIdentifier": bundle_id,
        "CFBundleExecutable": EXECUTABLE, "CFBundleIconFile": "AppIcon", "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": short, "CFBundleVersion": version, "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": MIN_MACOS, "LSArchitecturePriority": ["arm64"], "LSRequiresNativeExecution": True,
        "LSApplicationCategoryType": "public.app-category.graphics-design", "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication", "NSSupportsAutomaticTermination": False,
        "NSHumanReadableCopyright": "Copyright 2026 Tea Engineering Ltd. MIT licence.",
    }))


def build_app(args, out_dir: Path) -> Path:
    app = out_dir / f"{APP_NAME}.app"
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    for sub in ("MacOS", "Resources/app", "Frameworks"):
        (contents / sub).mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        wheel = build_wheel(args.version, Path(tmp))
        home = install_python(contents, wheel)
    resources = contents / "Resources"

    say("compiling the launcher")
    run(["clang", "-arch", "arm64", f"-mmacosx-version-min={MIN_MACOS}", "-O2", "-Wall",
         f'-DPYTHON_LIB="libpython{PYTHON_MM}.dylib"', "-o", contents / "MacOS" / EXECUTABLE, HERE / "launcher.c"])
    shutil.copy2(HERE / "app" / "j1x0_macapp.py", resources / "app")
    (resources / "examples").mkdir()
    for example in ("test-card.svg", "card.svg"):
        shutil.copy2(ROOT / "examples" / example, resources / "examples")
    copy_fontconfig(resources)
    run([sys.executable, HERE / "make_icon.py", resources / "AppIcon.icns"], stdout=subprocess.DEVNULL)
    libraries = bundle_libraries(contents / "Frameworks")
    gather_licences(resources, libraries, home / "lib" / f"python{PYTHON_MM}" / "site-packages")
    make_info_plist(contents, args.version, args.bundle_id)
    return app


# ── Signing, checking, packaging ──────────────────────────────────────────────────────────────────

MACHO_MAGIC = (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xce\xfa\xed\xfe")


def mach_o_files(root: Path) -> list[Path]:
    found = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with open(path, "rb") as f:
                if f.read(4) in MACHO_MAGIC:
                    found.append(path)
    return sorted(found, key=lambda p: (-len(p.parts), str(p)))      # the deepest first


def sign(app: Path, identity: str, entitlements: Path) -> None:
    release = identity != "-"
    say(f"signing with {'ad-hoc identity' if not release else identity!r}{' (hardened runtime)' if release else ''}")
    common = ["codesign", "--force", "--sign", identity]
    if release:
        common += ["--options", "runtime", "--timestamp"]
    files = [p for p in mach_o_files(app) if p != app / "Contents" / "MacOS" / EXECUTABLE]
    for path in files:
        run(common + [path], stderr=subprocess.DEVNULL)
    run(common + ["--entitlements", entitlements, app])
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=1", app])
    say(f"   signed {len(files) + 1} binaries; the signature verifies")


def check_sdks(app: Path) -> None:
    """Apple's notary service rejects a binary built against an SDK older than 10.9, and some PyPI wheels contain one.
    Find them now, not after an upload."""
    say("checking every binary's SDK (Apple's notary service rejects very old ones)")
    old = []
    for path in mach_o_files(app):
        shown = out(["vtool", "-show-build", path])
        sdk = re.search(r"sdk (\d+)\.(\d+)", shown)
        if not sdk or (int(sdk.group(1)), int(sdk.group(2))) < (10, 9):
            old.append(str(path.relative_to(app)))
    if old:
        sys.exit("these binaries are too old for notarisation:\n  " + "\n  ".join(old))


def self_test(app: Path) -> None:
    say("self-test: running the app's own checks with a clean environment")
    env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin"}          # no Homebrew on the PATH
    result = run([app / "Contents" / "MacOS" / EXECUTABLE, "--self-test"], env=env, capture_output=True, text=True,
                 check=False)
    print(result.stdout)
    if result.returncode:
        print(result.stderr)
        sys.exit("the self-test failed")


def make_dmg(app: Path, out_dir: Path, version: str, identity: str) -> Path:
    say("building the disk image")
    dmg = out_dir / f"{APP_NAME.replace(' ', '-')}-{version}-arm64.dmg"
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / APP_NAME
        stage.mkdir()
        shutil.copytree(app, stage / app.name, symlinks=True)
        (stage / "Applications").symlink_to("/Applications")
        dmg.unlink(missing_ok=True)
        run(["hdiutil", "create", "-volname", APP_NAME, "-srcfolder", stage, "-ov", "-format", "UDZO", dmg],
            stdout=subprocess.DEVNULL)
    if identity != "-":
        run(["codesign", "--force", "--sign", identity, "--timestamp", dmg])
    return dmg


def notarise(path: Path, profile: str) -> None:
    say(f"notarising {path.name} (this uploads it to Apple and waits)")
    target = path
    if path.suffix == ".app":
        target = path.with_suffix(".zip")
        run(["ditto", "-c", "-k", "--keepParent", path, target])
    run(["xcrun", "notarytool", "submit", target, "--keychain-profile", profile, "--wait"])
    run(["xcrun", "stapler", "staple", path])
    if target != path:
        target.unlink()


def default_version() -> str:
    described = out(["git", "describe", "--tags", "--always", "--dirty"], cwd=ROOT).strip()
    match = re.match(r"v?(\d+\.\d+\.\d+)(?:-(\d+)-g([0-9a-f]+))?(-dirty)?$", described)
    if not match:
        return "0.0.0"
    base, ahead, sha, dirty = match.groups()
    return base if not ahead and not dirty else f"{base}+{ahead or 0}.g{sha or 'x'}{'.dirty' if dirty else ''}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--identity", default="-", help='codesign identity (default "-": ad hoc, this Mac only)')
    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID)
    parser.add_argument("--version", default=default_version())
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--dmg", action="store_true", help="also build a .dmg")
    parser.add_argument("--notarize", metavar="PROFILE", help="notarytool keychain profile; needs a Developer ID identity")
    parser.add_argument("--skip-self-test", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin" or os.uname().machine != "arm64":
        sys.exit("this builds an Apple Silicon app and must run on an Apple Silicon Mac")
    if args.notarize and not args.identity.startswith("Developer ID Application"):
        sys.exit("notarisation needs a 'Developer ID Application' identity (see macos/README.md)")
    for tool in ("clang", "codesign", "hdiutil", "iconutil", "install_name_tool", "otool"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found: install the Xcode command line tools")
    for name in NATIVE_ROOTS:
        if not (HOMEBREW / "lib" / name).exists():
            sys.exit(f"{name} not found in {HOMEBREW}/lib: `brew install cairo libusb`")
    args.out.mkdir(parents=True, exist_ok=True)

    app = build_app(args, args.out)
    sign(app, args.identity, HERE / "entitlements.plist")
    check_sdks(app)
    if not args.skip_self_test:
        self_test(app)
    if args.notarize:
        notarise(app, args.notarize)
    dmg = make_dmg(app, args.out, args.version, args.identity) if args.dmg else None
    if dmg and args.notarize:
        notarise(dmg, args.notarize)
    size = sum(p.stat().st_size for p in app.rglob("*") if p.is_file()) / 1e6
    say(f"built {app} ({size:.0f} MB)" + (f" and {dmg}" if dmg else ""))


if __name__ == "__main__":
    main()
