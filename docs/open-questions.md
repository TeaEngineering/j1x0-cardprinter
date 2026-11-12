# Open questions

What is still unconfirmed, and what has been settled. "Test" is the cheapest experiment. Evidence comes
from three places: the USB capture of the Windows driver printing a card (`captured-job.md`), prints made
by this tool on a J110i (firmware V1.18.25, YMCKO ribbon), and the disassembly.

## Still open

**Q2. What do the numeric header fields mean?** `ESC GS p 32 ...` (32 = level count?) and
`ESC G 0 0 10 640 1024 1` (10 = ?). The literals are accepted as sent, but nothing has been changed to see
what they do. *Test:* change them one at a time and compare prints.

**Q5. Overlay bitmap (`vG`) encoding.** `FUN_180009fc0` is not decompiled. The full-frame overlay used
by every print so far (`ESC vF`, `ESC IV 30`) does not need it, and neither do rectangle overlays.

**Q6. Are 16 levels a hardware limit?** The driver quantises to 16 levels (every sample on the wire is
odd, 1 to 31), but the header says 32. *Test:* print a gradient with all 32 values and look for the
extra steps.

**Q8. Meaning of the single-letter commands and the tuning values.** `SXY`, `F`, `I n`, `IV n`, `IH n`,
`IS n`, `M?`, `!FF`, and `+$C`/`+$L`/`+C`/`+CH`/`+CV`. All the tuning values were 5 in the capture (the
driver's default), and our jobs send 5. Value ranges and their defaults are in the settings block at
`pdev+0xDA8`. *Test:* vary one tuning value per card and compare density.

**Q9. Ribbon id mapping.** `FUN_1800099f0` maps the printer's `!RIBBON` reply to the driver's internal
enum. Only id 2 (YMCKO, the standard flow, `SendPageData` case 0) has been seen. Needed for other
ribbons: K-only, UV, mono ribbons and so on, whose jobs will differ.

**Q11. What do `ESC A n`, `ESC R`, `ESC M...`, `ISREADY` and `CHECK_STATUS` mean on the wire?** `ISREADY`
and `CHECK_STATUS` are handled inside the monitor (section 5 of `status-and-transport.md`). `ESC A n` is
encoder-related and untested on this unit, which has no encoder.

**Q14. RFID / smart-card encoder.** Which library or command set controls it, and whether it is inside
the printer or a separate reader. `EILMON.dll` and `EInt.dll` are not in the package, and this unit has
no encoder (`ESC V` has no smart-card code). The encoder type is read from the `ESC V` reply
(`status-and-transport.md` section 6).

**Q15. Does the printer answer `GET_PORT_STATUS` and `GET_DEVICE_ID` from a cold start?** The first
session on a freshly powered printer saw both time out. The Windows capture shows both answered
(`0x18`; a 48-byte 1284 id), and so did a test from the Mac after the printer had been used from
Windows. The requests are identical in the two cases (`A1 01 0000 0000`, 1 byte; `A1 00 0000 0000`,
up to 1009 bytes). *Test:* power-cycle the printer, touch nothing else, and send both from a bare
`pyusb` script with a 3 s timeout. If both answer, drop `NO_STATUS_REQUEST` and read the 1284 id so the
link check can use the real status byte as Windows does.

**Q16. Other models and ribbons.** J100i, J110m, J120i and every ribbon but YMCKO are untried: the
link check's strings, reply formats, the 1284 id match and the job template may all differ.

**Q17. Resin (K) byte stuffing.** `0x5B` is written twice in the K bitmap (`PackMonoBitmap`), but no
byte of that value has occurred in a bitmap sent so far, so the rule is unconfirmed on the wire. *Test:*
a K pattern whose line contains the bits `01011011`, then check the print is not shifted.

**Q18. Other job shapes.** Duplex (`JOB 1/2`), the magnetic stripe commands (`&B`, `&E`), the
laminator (`+DLAMI`), grey K plane 3 (`ESC GS 3 ...`), print-and-encode, and `ESC !FF 1` (a job with no
resin) have never been sent or captured.

## Answered

**Q1. How does the printer frame the colour-plane payload?** By length. The payload is a fixed 442368
bytes (432 per line, 1024 lines), read once the `ESC GS` header has been seen, so a `CR` or `ESC` byte
inside it is data (`captured-job.md`).

**Q3. Orientation and mirroring of the stream.** Colour planes: stream line *i* is image column *i* and
dot *j* is image row *j* (`encode.stream_order`); the card prints upright. The resin (K) bitmap's lines go
out in the reverse order to the colour planes, measured from a printed card. There are no flip options.

**Q4. Is blank paper really value `1`?** Yes: a blank line is `04 21` x 213 words (the capture).

**Q7. How does a job end?** `ESC MO CR` after the last data, for a YMCKO ribbon, and the printer is
asked `ESC E 2 CR` after each plane's data, after the commands that follow image data and after the
final `ESC MO`. This tool's output prints and the printer's panel counter rises by 5 per card.

**Q10. Does the link check work?** Yes, on a J110i (V1.18.25): every query answers in the framing the
Windows monitor uses, including the `ESC E 2 CR` suffix, which is required. See Q15 for the status byte
and 1284 id.

**Q12. Is `ESC MASTER CR` needed once or twice?** The capture shows it twice in a row (the monitor's
and the driver's), so the job header sends both.

**Q13. Does `SOFT_RESET` matter?** The Windows monitor sends none: the only class requests in the
capture are `GET_DEVICE_ID` and `GET_PORT_STATUS`. `j1x0 send` sends none unless `--soft-reset` is given.

**Q19. The print process.** The printer works on each panel while the job is still arriving: it feeds,
prints and rewinds the card, then takes the next plane, and meanwhile it stops reading USB and does not
answer `ESC E 2`. The first ACK after `ESC IS 0` took about 7 s, and in one run writes crawled at about
10 KB/s for 13 s. The host must wait through that (`status-and-transport.md` section 8). Exact timings per
panel have not been measured.

## Cross-checks still worth doing

1. Capture the Windows driver printing a card with *resin content* (black text), to compare its K
   packing with ours byte for byte. The only capture so far has an all-zero K bitmap, so the K line
   order comes from a printed card, not from the wire.
2. Capture the Windows driver with a changed tuning value, to see which command carries it (Q8).
