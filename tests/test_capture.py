"""Check our encoder and job builder against a USB capture of the Windows driver printing a card.

The capture (captures/USBLog5.usblog, a SnoopyPro log) is not in the repository; these tests are
skipped without it. See docs/captured-job.md.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "captures" / "USBLog5.usblog"
pytestmark = pytest.mark.skipif(not CAPTURE.exists(), reason="no USB capture of the Windows driver")

sys.path.insert(0, str(ROOT / "scripts"))

from snoopypro_out import writes                                    # noqa: E402

from j1x0_cardprinter.encode import LINE_BYTES, LINES, pack_plane, unpack_plane   # noqa: E402
from j1x0_cardprinter.jobparse import parse_job                      # noqa: E402
from j1x0_cardprinter.protocol import build_job, end_of_card         # noqa: E402


@pytest.fixture(scope="module")
def captured():
    sent = [payload for _, payload in writes(CAPTURE.read_bytes())]
    first = next(i for i, payload in enumerate(sent) if payload == b"\x1bMASTER\r")
    return sent[first:]


def test_the_captured_job_is_what_our_builder_produces(captured):
    job = b"".join(p for p in captured if p != b"\x1bE 2\r")            # without the monitor's enquiries
    planes = [job[m.end():m.end() + LINES * LINE_BYTES] for m in re.finditer(rb"\x1bGS \d 32 0 0 1024 640 ", job)]
    resin = job[job.index(b"\x1bZ") + 2:][:81920]
    assert build_job(planes, resin) + end_of_card() == job


def test_the_captured_planes_survive_unpack_and_pack(captured):
    job = b"".join(captured)
    for m in re.finditer(rb"\x1bGS \d 32 0 0 1024 640 ", job):
        payload = job[m.end():m.end() + LINES * LINE_BYTES]
        assert pack_plane(unpack_plane(payload)) == payload


def test_the_captured_job_parses_cleanly(captured):
    job = b"".join(p for p in captured if p != b"\x1bE 2\r")
    assert all(s.ok for s in parse_job(job))


def test_the_enquiries_come_where_the_sender_puts_them(captured):
    """ESC E 2 follows each plane's data, the commands after image data (not vF) and the final MO."""
    seen = []
    for before, payload in zip(captured, captured[1:]):
        if payload == b"\x1bE 2\r" and not before.startswith(b"\x1b!") and before not in (b"\x1bV\r", b"\x1b%SERIE\r"):
            seen.append(before[:8] if len(before) > 8 else before)
    names = [x[:6] for x in seen]
    assert names.count(b"\x1bIS 0\r") == 1 and names[-1] == b"\x1bMO\r"
    assert b"\x1bvF\r" not in seen
