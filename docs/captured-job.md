# A captured job: what the Windows driver and port monitor really send

Reference for everything that was uncertain from the disassembly. Source: a SnoopyPro 0.26g USB
capture (`captures/USBLog5.usblog`, not in the repository) taken in a Windows XP guest (UTM, USB
passthrough) with the vendor driver v8.00.02.01 printing a sample BMP from Paint to a real J110i
(firmware V1.18.25, ribbon type 2 = YMCKO). `scripts/snoopypro_out.py` extracts the bulk-OUT
writes from such a log. `tests/test_capture.py` checks our encoder and builder against it, and is
skipped when the capture is absent.

Result: **`j1x0_cardprinter.protocol.build_job(...) + end_of_card()` is byte-for-byte the job the
Windows driver sent**, once the monitor's `ESC E 2 CR` enquiries are taken out.

## The stream, as writes

```
ESC MASTER CR                 (the port monitor, when it opens the port)
ESC MASTER CR                 (the driver)
ESC JOB 0 CR      ESC SXY 1 CR
ESC +$C 0 5 CR  ESC +$C 1 5 CR  ESC +$C 2 5 CR       head tuning, all 5
ESC +$L 0 5 CR  ESC +$L 1 5 CR  ESC +$L 2 5 CR
ESC +C 5 CR       ESC +CV 5 CR
ESC $F CR                     only before plane 0
ESC GS 0 32 0 0 1024 640 SP
<442368 bytes>  CR            plane 0, in 4096-byte writes; the CR is in the last one
   ESC E 2 CR   (ACK)         enquiry after the data, before ESC IS
ESC IS 0 CR
   ESC E 2 CR   (ACK)
ESC GS 1 32 0 0 1024 640 SP   plane 1: no ESC $F
<442368 bytes>  CR     ESC E 2 CR     ESC IS 1 CR     ESC E 2 CR
ESC GS 2 32 0 0 1024 640 SP
<442368 bytes>  CR     ESC E 2 CR     ESC IS 2 CR     ESC E 2 CR
ESC F CR          ESC G 0 0 10 640 1024 1 CR          ESC Z
<81920 bytes>  CR             the resin bitmap (here all zero)
   ESC E 2 CR
ESC I 10 CR       ESC E 2 CR
ESC vF CR         ESC IV 30 CR                        one enquiry, after the second
   ESC E 2 CR
ESC MO CR         ESC E 2 CR                          the end of the card
```

Before the job the monitor asks its usual questions (`ESC V`, `!RIBBON`, `!RIBLEN`, `!V 15`, `!V 20`,
`%SERIE`, `!NL`, and `ESC SLAVE` / `!V 1`, `!V 2`, `!V 24`, `!V 25` once), each followed by `ESC E 2 CR`.
There are no enquiries during the job header and none inside an image block.

## Colour plane payload

Not run-length coded. `SendPlanesYMC` sets `pdev+300 = 1` on entry, so it always takes
`FUN_180014540` (and `FUN_180015100` for landscape surfaces); `RlePlane_Portrait`, which the first
version of this tool copied, is the dead `pdev+300 == 0` path.

Per head line (1024 lines per plane), 640 samples of `(255 - byte) >> 3`, packed three to a
16-bit **big-endian** word: `0 | s0 (5 bits) | s1 (5) | s2 (5)`.

* 213 words hold samples 0 to 638; a 214th word holds sample 639 alone, in the top field.
* then four zero bytes.
* 214 x 2 + 4 = **432 bytes per line, 442368 per plane**, then one `CR`.

Blank paper is the value 1 in every field, so a blank line is `04 21` x 213, `04 00`, `00 00 00 00`.
(This confirms docs/open-questions Q4.) The `CR` and `ESC` values that occur inside the data do not
matter: the printer reads a fixed length once the `ESC GS` header has been seen.

## Orientation

Decoding the three captured planes back into an image gives an upright card when stream line *i* is
image column *i* and dot *j* is image row *j* (`encode.stream_order`). The printed card came out upright in
portrait, so there is nothing to flip and the tool has no mirroring options. Plane 0 is the blue
channel of the surface, plane 1 green, plane 2 red (so Y, M, C densities), as assumed.

## Not what the disassembly said

* **No run-length coding** (see above).
* **`ESC $F CR` only precedes plane 0.**
* **Two `ESC MASTER CR`.**
* **Tuning commands are sent**, all with the value 5 here.
* **The end of the card is `ESC MO CR` alone**, with no leading `CR` and no `ESC !FF 0 CR`, on a
  YMCKO ribbon. (`EndDocPort` as read in Ghidra had both for panel ribbons.)
* **`ESC E 2` is asked in a few places only**, not after every 4096-byte chunk (section above).
