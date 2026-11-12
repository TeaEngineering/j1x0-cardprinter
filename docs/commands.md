# Command reference

All commands are `ESC` (0x1B) + ASCII, terminated by `CR` (0x0D) unless noted. The constants live
in `jvlRaster.DLL` at `0x180032410`..`0x180032948` (labelled `cmd_*`/`fmt_*` in Ghidra) and
in `jvlLMON.DLL` near file offset `0x1b878`..`0x1c404`.

The help file's Tools page confirms the textual form: "Enter a printer command (without leading
escape character)" and a send-file format of one command per line starting with `MASTER`. **[code]**

Meanings are inferred unless noted. `v` stands for a decimal value; `n`, `p` for panel/track numbers.

## Job and tuning

| Command | Where | Notes |
|---|---|---|
| `MASTER` | `SendJobHeader` | Job start. Sent once, on the first page. |
| `JOB 0` / `JOB 2` / `JOB 1` | `SendJobHeader` | 0 when not duplex. For duplex jobs: 2 when ribbon==0 and side==0, otherwise 1. |
| `SXY 1` | `SendJobHeader` | Unknown; always sent. |
| `+$C p v` | `SendJobHeader` | Per-panel (p = 0..2 for YMC, 3 for K) tuning value, one byte from the settings block. Likely heat or contrast. |
| `+$L p v` | `SendJobHeader` | Second per-panel value. Same panels. |
| `+C v`, `+CH v`, `+CV v` | `SendJobHeader` | Global/horizontal/vertical tuning for some ribbon classes. |
| `+DLAMI 1 0` / `+DLAMI 0 1` | `SendPageData` | Laminator on/off, per side. |
| `V` | `cmd_V` | Probably a version query (`jvlLMON` sends it too). |

## Image data

| Command | Payload |
|---|---|
| `$F` | Precedes a colour/grey plane header. Possibly "start frame". |
| `G S p 32 0 0 1024 640 ` + payload + `CR` | Plane p = 0 (Y), 1 (M), 2 (C), 3 (grey K). The `32` is the number of density levels. The trailing space is part of the format. See `image-data.md`. |
| `I S p` | Sent after plane p (p = 0..2). For plane 3 the trailer is `CR ESC I S 3 CR`. |
| `F` then `G 0 0 10 640 1024 1` then `Z` | Monochrome K resin bitmap. The binary data follows `ESC Z`, then a `CR`. |
| `I 10` | After a K bitmap was sent. |
| `F` + `!FF 1` | Sent instead of a K bitmap when the K surface is blank. |

## Overlay (varnish) panel

| Command | Use |
|---|---|
| `vF` | Start overlay definition. Written when the mode is not "none". |
| `vL x y w h f` | Rectangle. `f` = 1 for the full frame, 0 for keep-out areas. |
| `vG 0 0 10 640 1024 1` + `ESC Z` + data | User bitmap overlay, 1 bit per dot (same shape as the K bitmap). |
| `IV 30` / `IV 31` | End of overlay. 30 for "full" mode, 31 otherwise. |

Fixed keep-out rectangles (hard-coded x y w h, `f` = 0). **[code]** The driver picks one of each
pair with a per-side flag (`psVar6[1]`, set when the image is flipped), so the pairs are two
placements of the same zone, not "front" and "back" of the card.

| Mode | Flag 0 | Flag 1 |
|---|---|---|
| Full frame | `vL 0 0 1023 639 1` | |
| ISO smart-card contacts | `vL 85 197 194 159 0` | `vL 743 289 194 159 0` |
| AFNOR contacts | `vL 85 97 158 160 0` | `vL 768 388 158 160 0` |
| Magstripe | `vL 0 0 1023 230 0` (top strip) | `vL 0 415 1023 230 0` (bottom strip) |

User-selected rectangles use the generic `vL %d %d %d %d %d` format; coordinates are mirrored
(`0x3ff - x`, `0x27f - y`) when the flip flag is set. **[code]**

## Card movement

| Command | Guess |
|---|---|
| `MI` | Move card in (to print position). |
| `MF` | Move forward / feed. |
| `MO` | Move out / eject. Sent alone when the job mode is "encode only". |
| `IS 0`, `IS 1`, `IS 2` | Select/execute panels. Also appear in a block with `MI`/`MF`/`MO` (`0x1800324d0`). |
| `I 10`, `I 20`, `I 30`, `IV`, `IV 30`, `IV 31`, `IH 31`, `I` | Print/advance variants. Exact meaning unknown. |

The firmware-file example from the help (`MASTER`, `MI`, `MO`, `SLAVE`) says `MI` moves the
card to the print position and `MO` ejects it. **[code]** (help text)

## Magnetic stripe

| Command | Meaning |
|---|---|
| `&B 1 <data>` | Track 1, at most 76 characters. |
| `&B 2 <data>` | Track 2, at most 37 characters. |
| `&B 3 <data>` | Track 3, at most 104 characters. |
| `&E *` | Encode all tracks. |
| `&R` | Reset or read. |

Untruncated `&B n %s` forms are also present. **[code]**

## Query commands used by the monitor

Found in `jvlLMON.DLL`; the first group is also sent by `jvlRaster.DLL` through `GetPrinterData`.

| Command | Reply parsing |
|---|---|
| `!RIBBON` | Ribbon type id; the reply ends with 0x06 (ACK). |
| `!RIBLEN` | Ribbon remaining count (guess). |
| `V`, `R`, `A 1..3`, `E 1`, `E 2`, `M ...`, `MS 2`, `!FF 0` | Unknown. |
| `ISREADY`, `CHECK_STATUS`, `RESET`, `ISHIDREADY`, `HIDDONE` | Internal monitor request names rather than wire commands; see `status-and-transport.md`. |
