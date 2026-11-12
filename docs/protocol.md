# Javelin J110i print protocol (reverse engineered)

Source: static analysis of the NBS Technologies "Javelin Value Class Printers" Windows driver,
v8.00.02.01 (`blobs/`, annotated in the Ghidra project `javelin`), corrected against a USB capture
of that driver printing to a real J110i (`captured-job.md`, which supersedes this file where they
differ: no run-length coding, two `MASTER`, tuning sent, end of card is `ESC MO CR`). Every claim
carries a confidence marker:

| Marker | Meaning |
|---|---|
| **[code]** | Read directly from decompiled code or constant data. |
| **[infer]** | Inferred from code structure, strings or the bundled help file. |
| **[guess]** | Plausible but unconfirmed. See `open-questions.md`. |

## Detail files

| File | Contents |
|---|---|
| `captured-job.md` | The job the real driver and port monitor sent, byte for byte. Start here. |
| `commands.md` | Every ESC command found, with arguments and where the driver emits it. |
| `image-data.md` | Exact encoding of the colour planes, the K bitmap and the overlay bitmap. Includes a reference encoder. |
| `driver-internals.md` | How the Windows driver is structured, the function map for Ghidra, and the private device-state offsets. |
| `status-and-transport.md` | Language monitor, status queries, error strings, smart-card/RFID hints. |
| `open-questions.md` | What must be checked on hardware, in priority order. |

## 1. Stack

```
Windows app --GDI--> jvlRaster.DLL  --WritePrinter--> spooler --> jvlLMON.DLL --> USBPRINT --> printer
                     (DDI driver)                                  (language monitor)
```

- `jvlRaster.DLL` is a kernel-style GDI *DDI* driver (`DrvEnableDriver`, `Eng*` imports). It lets
  GDI draw the page into two off-screen surfaces, then converts those surfaces into the printer
  language in `DrvSendPage`. **[code]**
- `jvlLMON.DLL` is a language monitor. It owns the port, polls status, and answers
  `GetPrinterData` queries by sending the query as a command to the printer. **[code]**
- The printer language is **ESC-prefixed ASCII commands terminated by CR (0x0D)**, with binary
  payloads after a few of them. **[code]**
- For our Python tool the Windows stack disappears. We build the byte stream ourselves and send
  it over libusb to the printer's bulk-OUT endpoint. **[infer]**

## 2. Page geometry

- Image area is **1024 x 640** (the header fields are `0 0 1024 640`, the overlay frame is
  `vL 0 0 1023 639`). **[code]**
- This fits a CR80 card (85.6 x 54 mm) at roughly 300 dpi: 1011 x 638 px, padded a few pixels. **[infer]**
- The print head spans the short edge. The data stream is **1024 head lines of 640 dots**; a
  line is one slice across the card's short edge. **[infer]** (the encoder loops are 0x400 outer
  x 0x280 inner)
- The driver's GDI surfaces are 640 x 1024 (portrait) or 1024 x 640 (landscape). Which way the
  card is mirrored or rotated in the stream is **not yet established**. See `open-questions.md` Q3.

## 3. Panels

The ribbons are dye-sublimation (YMC) plus resin (K) plus clear overlay varnish (O). Ribbon types
are listed in `jvRIBBON.INI` (YMCKO, YMCK, K-only, UV variants, and others).

| Panel | How it is sent | Format |
|---|---|---|
| Y, M, C | planes 0, 1, 2 via `ESC GS p ...` | 5-bit density, three samples per 16-bit word |
| K (dye-style grey) | plane 3 via `ESC GS 3 ...` | luma density (not captured) |
| K (resin) | `ESC G 0 0 10 640 1024 1` | 1 bit per dot, byte-stuffed |
| O (overlay) | `ESC v...` commands | full frame, rectangles, or a 1-bit bitmap |

Details in `image-data.md`. **[code]**

## 4. Anatomy of a single-sided YMCKO job

Derived from `SendJobHeader` and `SendPageData` (case 0 of the ribbon switch, non-duplex). **[code]**
for the commands and their order, **[guess]** for their meaning.

```
ESC MASTER CR                       job start (host-to-printer "master" mode); sent twice, see below
ESC MASTER CR
ESC JOB 0 CR                        0 = non-duplex (duplex flows use 2 or 1)
ESC SXY 1 CR
ESC +$C 0 v CR  ESC +$C 1 v CR  ESC +$C 2 v CR      per-panel head tuning (Y,M,C)
ESC +$L 0 v CR  ESC +$L 1 v CR  ESC +$L 2 v CR
ESC +C v CR                         common tuning
ESC +CV v CR                        tuning for some ribbons
[ESC +DLAMI a b CR]                 laminator, only on laminator-equipped models

ESC $F CR ESC GS 0 32 0 0 1024 640 SP  <Y  payload> CR  ESC IS 0 CR      (ESC $F CR only here)
          ESC GS 1 32 0 0 1024 640 SP  <M  payload> CR  ESC IS 1 CR
          ESC GS 2 32 0 0 1024 640 SP  <C  payload> CR  ESC IS 2 CR

if the K resin surface has any content:
    ESC F CR ESC G 0 0 10 640 1024 1 CR ESC Z  <80-byte lines x 1024> CR
    ESC I 10 CR
else:
    ESC F CR ESC !FF 1 CR

overlay (see commands.md for the modes):
    ESC vF CR [ESC vL ... CR | ESC vG ... ] ESC IV 30 CR     (full)   or   ESC IV 31 CR
```

The capture (`captured-job.md`) confirmed this order, and showed these details:

- **Two `ESC MASTER CR`** in a row (the port monitor's and the driver's), and the tuning commands
  with the value 5.
- **The end of the card is `ESC MO CR`**, written by the monitor after the last data, nothing else.
- The monitor asks the printer `ESC E 2 CR` and waits for an ACK after each plane's data, after
  `ESC IS`, `ESC I 10`, `ESC IV 30` and `ESC MO`, but not during the header or inside a block
  (`status-and-transport.md` section 8).
- The meaning of most single-letter commands (`F`, `I`, `IV`, `IS`, `M?`) is still only from
  position and the help text.

## 5. Magnetic stripe

Track data goes in-band: `ESC & B n <data> CR` for tracks 1, 2 and 3 (max 76, 37 and 104 chars),
then `ESC & E * CR` to encode. **[code]** The document carries the data as `~1...`, `~2...`,
`~3...` tags that `FUN_180002150` parses before printing. **[code]**

## 6. Smart card and RFID

The raster driver has no RFID code. The language monitor does: status strings include
`ERROR PROX`, `ERROR CONTACT DATA`, `ERROR PROX DATA`, and it handles `ISHIDREADY`/`HIDDONE`
queries. It also loads `EILMON.dll`, which is not in the package. **[infer]** See
`status-and-transport.md`. Whether the J110i's encoder is internal or a separate reader depends
on the unit; the help file lists "Smart Card" as an optional device.

## 7. Implications for the Python tool

1. The renderer must produce, from the SVG, **1024 x 640 RGB** for Y/M/C and (optionally) a
   **1024 x 640 1-bit** K mask and overlay mask.
2. Colour handling can be our own. The driver's gain/gamma/sharpen/dither is only a quality
   choice; the printer receives 5-bit densities, so the 16 distinct levels the driver produces
   are a floor, not a hardware limit. **[guess]** (the printer accepts 0..31)
3. The transport is a plain write of the byte stream, preceded by the USB printer-class steps
   used in the rollingmill code (GET_DEVICE_ID, SOFT_RESET).
4. Status polling needs the language-monitor commands in `status-and-transport.md`.
