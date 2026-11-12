# Javelin J110i print protocol (reverse engineered)

Source: static analysis of the NBS Technologies "Javelin Value Class Printers" Windows driver,
v8.00.02.01 (`blobs/`, annotated in the Ghidra project `javelin`). Nothing here has been verified
against a real printer yet. Every claim carries a confidence marker:

| Marker | Meaning |
|---|---|
| **[code]** | Read directly from decompiled code or constant data. |
| **[infer]** | Inferred from code structure, strings or the bundled help file. |
| **[guess]** | Plausible but unconfirmed. See `open-questions.md`. |

## Detail files

| File | Contents |
|---|---|
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
| Y, M, C | planes 0, 1, 2 via `ESC G S p ...` | 5-bit density, run-length encoded |
| K (dye-style grey) | plane 3 via `ESC G S 3 ...` | luma density, same RLE |
| K (resin) | `ESC G 0 0 10 640 1024 1` | 1 bit per dot, byte-stuffed |
| O (overlay) | `ESC v...` commands | full frame, rectangles, or a 1-bit bitmap |

Details in `image-data.md`. **[code]**

## 4. Anatomy of a single-sided YMCKO job

Derived from `SendJobHeader` and `SendPageData` (case 0 of the ribbon switch, non-duplex). **[code]**
for the commands and their order, **[guess]** for their meaning.

```
ESC MASTER CR                       job start (host-to-printer "master" mode)
ESC JOB 0 CR                        0 = non-duplex (duplex flows use 2 or 1)
ESC SXY 1 CR
ESC +$C 0 v CR  ESC +$C 1 v CR  ESC +$C 2 v CR      per-panel head tuning (Y,M,C)
ESC +$L 0 v CR  ESC +$L 1 v CR  ESC +$L 2 v CR
ESC +C v CR                         common tuning
ESC +CV v CR                        tuning for some ribbons
[ESC +DLAMI a b CR]                 laminator, only on laminator-equipped models

ESC $ F CR ESC G S 0 32 0 0 1024 640 SP  <Y  payload> CR  ESC I S 0 CR
ESC $ F CR ESC G S 1 32 0 0 1024 640 SP  <M  payload> CR  ESC I S 1 CR
ESC $ F CR ESC G S 2 32 0 0 1024 640 SP  <C  payload> CR  ESC I S 2 CR

if the K resin surface has any content:
    ESC F CR ESC G 0 0 10 640 1024 1 CR ESC Z  <80-byte lines x 1024> CR
    ESC I 10 CR
else:
    ESC F CR ESC !FF 1 CR

overlay (see commands.md for the modes):
    ESC vF CR [ESC vL ... CR | ESC vG ... ] ESC IV 30 CR     (full)   or   ESC IV 31 CR
```

What is missing or uncertain:

- The exact end-of-card sequence (feed and eject, `MF`/`MO`) differs per branch and is partly in
  `SendMagEncodeSequence`, which is not fully analysed.
- Whether and how a job is terminated. The firmware-file format on the help page ends with
  `SLAVE`, but the driver code never emits that string.
- The meaning of most single-letter commands (`F`, `I`, `IV`, `IS`, `M?`). Names above are
  from position and the help text only.

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
