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

**Q10. VID/PID, endpoints, and whether the printer sends data back over bulk-IN.**
Plug it in and run `system_profiler SPUSBDataType`.

**Q11. Status reply format.** `!RIBBON` is terminated by `0x06`. The status/error code format
and the mapping to the error strings are not decoded.

**Q12. RFID / smart-card encoder.** Which library or command set controls it, and whether it is
inside the printer or a separate reader. `EILMON.dll` and `EInt.dll` are not in the package.

## Cross-checks to do when hardware is available

1. Capture the real driver's output (any Windows machine with USBPcap) for three cards: blank,
   single solid colour per plane, asymmetric pattern. Diff against our generator.
2. Compare the bytes of a captured YMCKO job against the sequence in `protocol.md` section 4.
