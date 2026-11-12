# Open questions

In rough priority order for the first hardware session. "Test" is the cheapest experiment.

## Image data

**Q1. How does the printer frame the colour-plane payload?** *Answered by a USB capture.* The
payload is a fixed 442368 bytes (432 per line, 1024 lines), read by length once the `ESC GS` header
has been seen, so a `CR` or `ESC` byte inside it is data (`captured-job.md`).

**Q2. What do the numeric header fields mean?**
`ESC G S p 32 ...` (32 = level count?) and `ESC G 0 0 10 640 1024 1` (10 = ?). Both are literals
in the code. *Test:* change them and compare prints.

**Q3. Orientation and mirroring of the stream.** *Answered.* Decoding the captured planes gives an upright
card for stream line *i* = image column *i* and dot *j* = image row *j* (`encode.stream_order`), and the card
printed upright in portrait. There are no flip options.

**Q4. Is blank paper really value `1`?** *Answered:* yes, a blank line is `04 21` x 213 words (the
capture).

**Q5. Overlay bitmap (`vG`) encoding.**
`FUN_180009fc0` is not decompiled. Not needed for full-frame or rectangle overlays.

**Q6. Are 16 levels a hardware limit?**
The driver quantises to 16 levels, but the header says 32. Our tool may be able to send true 5-bit
data. *Test:* a smooth gradient with all 32 values.

## Job control

**Q7. How does a job end?** *Answered by a USB capture.* The monitor writes `ESC MO CR` after the
last data, and asks `ESC E 2 CR` after each plane's data, after the commands that follow image data
and after the final `ESC MO` (`captured-job.md`). Not yet confirmed on this tool's own output: the
duplex and magstripe branches (`SendMagEncodeSequence`) end differently.

**Q8. Meaning of the single-letter commands and tuning values.**
`F`, `I n`, `IV n`, `IH n`, `IS n`, `M?`, `!FF`, `SXY`, `JOB n`, and `+$C`/`+$L`/`+C`/`+CH`/`+CV`.
Their value ranges and defaults are in the settings block at `pdev+0xDA8`; the defaults are set
in `DrvEnablePDEV`/`DrvResetPDEV`.

**Q9. Ribbon id mapping.** `FUN_1800099f0` maps the printer's `!RIBBON` reply to the driver's
internal enum. Needed to pick the right job template for non-YMCKO ribbons.

## Transport and encoding

**Q10. Does the link check work?** *Answered on a J110i (V1.18.25), see
`status-and-transport.md` section 0.* Yes: the printer is found by its strings and vid/pid
(`0a5f:005c`), and every query answers in the framing the Windows monitor uses, including the
`ESC E 2 CR` suffix, which is required. It has no 1284 id and no status byte (both requests time
out). Still open:

- Whether other models in the family (J100i, J110m, J120i) behave the same.
- What the status byte would say on a printer that does answer it (`0x18` idle, per the monitor).
- How the printer behaves for queries *while* a card is printing (the UI avoids asking then).

**Q11. What do `ESC A n`, `ISREADY` and `CHECK_STATUS` mean on the wire?** (`ESC E 2` is
settled: it must follow a query for the printer to answer.) `ISREADY` and `CHECK_STATUS` are
handled by the monitor (section 5 of `status-and-transport.md`); only the port-status part is
understood, and this printer does not provide one.

**Q12. Is `ESC MASTER CR` needed once or twice?** *Answered:* the capture shows it twice in a row
(the monitor's and the driver's), so the job header now has both.

**Q13. Does `SOFT_RESET` matter?** rollingmill sends it for the MDX. `j1x0 send` does not unless
`--soft-reset` is given, because it can abort a job in progress. The Windows monitor was not seen
issuing one.

**Q14. RFID / smart-card encoder.** Which library or command set controls it, and whether it is
inside the printer or a separate reader. `EILMON.dll` and `EInt.dll` are not in the package.

## Cross-checks to do when hardware is available

1. Capture the real driver's output (any Windows machine with USBPcap) for three cards: blank,
   single solid colour per plane, asymmetric pattern. Diff against our generator.
2. Compare the bytes of a captured YMCKO job against the sequence in `protocol.md` section 4.
