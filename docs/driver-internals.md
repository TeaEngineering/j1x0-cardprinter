# Driver internals and Ghidra map

Program: `jvlRaster.DLL` (x64, image base `0x180000000`) in the Ghidra project `javelin`.
Renames and plate comments added by this analysis are saved in the project.

## Driver entry points

`DrvEnableDriver` (`0x180008d70`) returns a table of 20 `{u32 iFunc, pad, u64 pfn}` entries at
`0x18003b450`. Indices are from `winddi.h`; the last four were confirmed by behaviour.

| iFunc | Name | Address |
|---|---|---|
| 0 | DrvEnablePDEV | `0x180008dd0` |
| 1 | DrvCompletePDEV | `0x180009020` |
| 7 | DrvResetPDEV | `0x180009030` |
| 2 | DrvDisablePDEV | `0x180009070` |
| 3 | DrvEnableSurface | `0x1800090f0` |
| 4 | DrvDisableSurface | `0x1800091c0` |
| 14 | DrvStrokePath | `0x1800065c0` |
| 15 | DrvFillPath | `0x1800067b0` |
| 16 | DrvCombinedOutlineFill (stroke-and-fill) | `0x180006980` |
| 31 | DrvLineTo | `0x180006bb0` |
| 17 | DrvPaint | `0x180006dc0` |
| 18 | DrvBitBlt | `0x180007040` |
| 19 | DrvCopyBits | `0x180007430` |
| 20 | DrvStretchBlt | `0x180007660` |
| 23 | DrvTextOut | `0x180007a00` |
| 24 | DrvEscape | `0x18000a140` |
| 35 | DrvStartDoc | `0x18000a190` |
| 34 | DrvEndDoc | `0x18000a1e0` |
| 33 | DrvStartPage | `0x18000eee0` |
| 32 | DrvSendPage | `0x18000ed80` |

- `DrvEscape` escape `0x13` (`PASSTHROUGH`) calls `WritePrinter(payload + 2, len - 2)`. Raw
  commands can therefore be injected from a Windows app.
- `DrvStartDoc` allocates the `0xA1000` payload buffer (`pdev+0x1060`). `DrvStartPage` queries
  the ribbon and model with `GetPrinterData`, updates the settings block, and clears both
  surfaces to `0xFF`.

## Print-path functions

| Function | Address | Role |
|---|---|---|
| SendJobHeader | `0x180014120` | `MASTER`, `JOB`, `SXY`, head tuning |
| SendPageData | `0x180010bc0` | Orchestrates one card side. Big switch on ribbon enum and duplex state. |
| SendPlanesYMC | `0x18000f540` | Planes 0, 1, 2 |
| SendPlane3Luma | `0x180010520` | Plane 3 |
| SendMonoBitmapK | `0x180010090` | K resin 1 bpp bitmap |
| SendOverlayVarnish | `0x1800108d0` | `vF`/`vL`/`vG` overlay |
| SendVarnishBitmapFile | `0x1800160a0` | `vG` from a user `.bmp` |
| SendMagEncodeSequence | `0x18000a840` | Magstripe and card movement. Not fully analysed. |
| PrepareColourSurface | `0x1800043a0` | Gain, gamma, sharpen, stochastic quantise |
| RlePlane_Portrait | `0x1800147e0` | Colour RLE, portrait. Variants at `0x180014540`, `0x1800153a0`, `0x180015100`. |
| RleLuma_Landscape | `0x180015970` | Grey RLE. Flipped twin `0x180015680`; portrait versions `0x180014db0`, `0x180014ac0`. |
| PackMonoBitmap_Landscape | `0x180015e00` | 1 bpp packing, with stuffing. Portrait twin `0x180015cc0`. |
| FormatCommand | `0x180017ac0` | `sprintf`-style helper for the command strings |

Constants are labelled `cmd_*` and `fmt_*` between `0x180032410` and `0x180032948`.

## Private device state (`pdev`, reached through `param_1 + 0x10`)

Offsets and meanings are inferred from usage and from the help file. **[infer]**

| Offset | Meaning |
|---|---|
| `+0x04` | Document started |
| `+0x08` | Page active |
| `+0x0C` | Page counter (parity chooses front/back) |
| `+0x10`, `+0x18` | Colour surface, mono surface handles |
| `+0x28` | Printer handle for `WritePrinter` |
| `+0x84` | Orientation (1 = portrait) |
| `+0x8E` | Copy/encode count (> 1 triggers the encode sequence) |
| `+0x112` | Rotation setting |
| `+0x114` | Print on both sides |
| `+0x116` | Ribbon enum (driver's own numbering) |
| `+0x11A` | Side selector for duplex flows |
| `+0x120` | Monochrome conversion method (low 2 bits) |
| `+0x122` | "K panel used" flag |
| `+0x124` | Printing mode: 0 print only, 1 encode only (just `MO`), 2 print and encode |
| `+0x12F` | Device option class (6 and 7 select laminator and encoder paths) |
| `+0x168`, `+0x57C` | Overlay options, front and back (mode word, then rectangle values) |
| `+0xDA8`..`+0xDC2` | Head and colour adjustment bytes/words |
| `+0x1060`, `+0x1068` | Payload buffer, current length |
| `+0x106C`, `+0x126C`..`+0x1880` | Magstripe track strings and similar text buffers |
| `+0x136C` | Optional extra string written after the overlay rectangles |
| `+0x1980`, `+0x1982` | "Nothing to print" flags per side |
| `+0x1984`, `+0x1985`, `+0x1986` | RLE state: value, run count, state flag |

## Ribbon enum

The driver's internal ribbon number (`pdev+0x116`) is not the `jvRIBBON.INI` id; it is mapped by
`FUN_1800099f0`. The mapping has not been extracted. The switch in `SendPageData` uses values
0 to 0xE; case 0 is the standard YMCKO flow.
