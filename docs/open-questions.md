# Open questions

In rough priority order for the first hardware session. "Test" is the cheapest experiment.

## Image data

**Q1. How does the printer frame the colour-plane payload?**
`CR` (0x0D) is a valid density (`v = 13`), and the driver never escapes it. The K bitmap does
escape `0x5B`, so the design is not uniform. *Test:* send a plane whose first value is 13 and see
if the card prints normally.

**Q2. What do the numeric header fields mean?**
`ESC G S p 32 ...` (32 = level count?) and `ESC G 0 0 10 640 1024 1` (10 = ?). Both are literals
in the code. *Test:* change them and compare prints.

**Q3. Orientation and mirroring of the stream.**
The driver walks the surface forwards or backwards (a 180 degree turn) depending on the rotation
flag, and has landscape variants that were not decompiled. We need to know which way up a card
comes out for a given stream order. *Test:* print an asymmetric test pattern (an "F" in one corner).

**Q4. Is blank paper really value `1`?**
By arithmetic, white maps to `0xF0` then to `1`, so `0` is never sent. A sparse plane might also
accept `0`. *Test:* a white card with `0` vs `1`.

**Q5. Overlay bitmap (`vG`) encoding.**
`FUN_180009fc0` is not decompiled. Not needed for full-frame or rectangle overlays.

**Q6. Are 16 levels a hardware limit?**
The driver quantises to 16 levels, but the header says 32. Our tool may be able to send true 5-bit
data. *Test:* a smooth gradient with all 32 values.

## Job control

**Q7. How does a job end?** The firmware-file format on the help page ends with `SLAVE`, and
`MASTER` starts it. The driver emits `MASTER` but never `SLAVE`. The end-of-card sequence
(`MF`, `MO`, `IV`, `IS`) differs across branches of `SendPageData` and `SendMagEncodeSequence`.

**Q8. Meaning of the single-letter commands and tuning values.**
`F`, `I n`, `IV n`, `IH n`, `IS n`, `M?`, `!FF`, `SXY`, `JOB n`, and `+$C`/`+$L`/`+C`/`+CH`/`+CV`.
Their value ranges and defaults are in the settings block at `pdev+0xDA8`; the defaults are set
in `DrvEnablePDEV`/`DrvResetPDEV`.

**Q9. Ribbon id mapping.** `FUN_1800099f0` maps the printer's `!RIBBON` reply to the driver's
internal enum. Needed to pick the right job template for non-YMCKO ribbons.

## Transport and encoding

**Q10. Does the link check work?** Run `j1x0 send` with the printer connected and idle. It is
the cheapest first experiment, and each part of it is a separate question:

- *The 1284 id.* Does `GET_DEVICE_ID` answer, and does it match `javel|j1[012]0`? If not,
  autodetect finds nothing and `--vid`/`--pid` are needed (the output lists what was seen). The
  id string and VID/PID should then be recorded in `status-and-transport.md`.
- *GET_PORT_STATUS.* Is `0x18` what an idle printer returns? (The monitor's rule is bit 5 set, or
  `status & 0x38 == 0x18`.)
- *Bulk-IN replies.* Does a query come back at all? If `V` times out, try without the `ESC E 2 CR`
  suffix (`QUERY_SUFFIX` in `usb.py`) and with `ESC E 1 CR`.
- *Reply shape.* Does `V` return text ending in `0x06`, as the monitor expects? Capture the raw
  bytes and compare with `parse_reply`.
- *`wIndex`.* The class requests use `interface<<8 | alt` for GET_DEVICE_ID and the interface
  number for GET_PORT_STATUS; the other form is tried as a fallback for GET_DEVICE_ID only.

**Q11. What do `ESC E 2`, `ESC A n`, `ISREADY` and `CHECK_STATUS` mean on the wire?**
`ESC E n` follows every query on the Windows side. `ISREADY` and `CHECK_STATUS` are handled by
the monitor (section 5 of `status-and-transport.md`); only the port-status part is understood.

**Q12. Is `ESC MASTER CR` needed once or twice?** The monitor writes it when it opens the port
and the raster driver's job header begins with it as well. `render` emits one copy (the driver's);
`send` adds none.

**Q13. Does `SOFT_RESET` matter?** rollingmill sends it for the MDX. `j1x0 send` does not unless
`--soft-reset` is given, because it can abort a job in progress. The Windows monitor was not seen
issuing one.

**Q14. RFID / smart-card encoder.** Which library or command set controls it, and whether it is
inside the printer or a separate reader. `EILMON.dll` and `EInt.dll` are not in the package.

## Cross-checks to do when hardware is available

1. Capture the real driver's output (any Windows machine with USBPcap) for three cards: blank,
   single solid colour per plane, asymmetric pattern. Diff against our generator.
2. Compare the bytes of a captured YMCKO job against the sequence in `protocol.md` section 4.
