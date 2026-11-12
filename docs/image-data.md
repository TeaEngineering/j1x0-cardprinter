# Image data encoding

Everything here comes from `SendPlanesYMC`, `SendPlane3Luma`, `SendMonoBitmapK`,
`PrepareColourSurface`, the plane packers and `PackMonoBitmap_*` in `jvlRaster.DLL`, checked against
a USB capture of the real driver (`captured-job.md`). Markers as in `protocol.md`.

## 1. Source surfaces

GDI draws into two surfaces owned by the driver. **[code]**

| Surface | Format | Used for |
|---|---|---|
| colour (`pdev+0x10`) | 24 bpp DIB, byte order B, G, R. Cleared to `0xFF` (white) at the start of every page. | Y, M, C and grey-K planes |
| mono (`pdev+0x18`) | 1 bpp, `0xFF` = white | K resin panel (and a mask for the black-panel logic) |

Size is 640 x 1024 when `pdev+0x84 == 1` (portrait), otherwise 1024 x 640.

## 2. Colour planes (Y, M, C)

Plane p reads byte `p` of every pixel: plane 0 = B, 1 = G, 2 = R. Inverting those gives
yellow, magenta and cyan density, so planes 0, 1, 2 = **Y, M, C**. **[code]** for the byte
offsets. The colour names are confirmed: the captured planes decode to a natural-looking card with
plane 0 as the blue channel, and a card printed from planes built this way has the right colours.

Per sample:

```
v = (255 - byte) >> 3          # 0..31, higher = more dye
```

The header field `32` after the plane number is probably the level count. **[infer]** It is sent as is
and the printer accepts it; the values on the wire are odd numbers 1 to 31 (`open-questions.md` Q2, Q6).

### Pre-processing (`PrepareColourSurface`, applied in place, always)

1. If any intensity or contrast setting is non-zero: per-channel brightness add
   (`+= intensity*255/100`) then a power-law contrast curve. **[code]** The constants used in
   the `pow()` calls are 255.5 (`DAT_180033a38`) and 255.0 (`DAT_180033a40`), read from the binary.
   This tool does not reproduce this stage; brightness and contrast are left to the artwork.
2. 3 x 3 sharpen on interior pixels. (This tool leaves it off by default; `render --sharpening 1` is this step, 2 to 5 scale it up. It leaves a thin halo at hard edges, e.g. between gradient steps.) With `c` the centre sample, `N4` the sum of the four
   edge neighbours and `D4` the sum of the four diagonal neighbours:

   ```
   t   = (12*c - 2*N4 - D4) * 5 + 8
   t   = t / 16                      # divide, rounding toward zero
   t   = clamp(t, -70, +70)
   v   = c + t
   ```
3. Quantise to 16 levels with **stochastic rounding** (`rand()`):

   ```
   if v <= 0:    out = 0
   elif v >= 255: out = 0xF0
   else:
       q = v // 17
       if rand() % 17 < v % 17: q += 1
       out = q << 4
   ```

   Border pixels skip step 2 but still get step 3.

Because of step 3 the DIB byte is one of `0, 16, ... 240`, so after `(255 - byte) >> 3`
the stream carries only the odd values `31, 29, ... 3, 1`. **Blank paper is `1`, never `0`.**
**[code]** (derived by hand from the arithmetic; not observed on the wire)

### Plane payload (packed, not run-length coded)

**Corrected by the capture** (`captured-job.md`): an earlier version of this file, and of the tool,
described a run-length code taken from `RlePlane_Portrait`. That function is never used for a normal
job (`SendPlanesYMC` sets `pdev+300 = 1` and takes `FUN_180014540`).

Per head line, 640 samples packed three to a 16-bit big-endian word, `0 | s0 | s1 | s2` (5 bits
each). 213 words carry samples 0 to 638, a 214th carries sample 639 in its top field, then four
zero bytes: 432 bytes a line, 442368 bytes a plane, followed by one `CR`. Blank is `04 21` words.
`encode.pack_plane` / `unpack_plane` implement it and round-trip the captured planes exactly.

Because the length is fixed, the payload needs no escaping: a `CR` or `ESC` byte inside it is data
(this settles Q1).

### Sample order

The outer loop is 1024 lines, the inner loop 640 samples. For the portrait surface, walking
forward is "start at the first pixel, step +3 bytes along the row, step +stride per line". When
the rotation flag (`pdev+0x112`) is 0 the driver instead starts at the *last* pixel of the
last row and walks backwards (a 180 degree turn). The landscape encoders (`FUN_1800153a0`,
`FUN_180015100`) are the transposed equivalents and have not been decompiled. **[code]**/**[infer]**
Only the portrait path was captured, and it is what this tool emits. The resulting stream order is
settled by the capture and by a printed card (`captured-job.md`, Orientation); the landscape encoders
are not needed.

## 3. Grey K plane (plane 3)

Same pre-processing as the colour planes, but each sample is the inverted luma. **Not captured and not
used by any ribbon tried so far** (a YMCKO ribbon takes the resin bitmap instead), so this section is
disassembly only:

```
v = ( (255-G)*59 + (255-R)*30 + (255-B)*11 ) // 100  >> 3
```

(`RleLuma_Landscape` at `0x180015970`, with a flipped twin at `0x180015680`; the portrait
versions are `FUN_180014db0` and `FUN_180014ac0`.) Header
`ESC $ F CR ESC G S 3 32 0 0 1024 640 `, trailer `CR ESC I S 3 CR`. **[code]**

## 4. K resin bitmap

Used for the black resin panel when the K surface has any non-white byte. **[code]** Header, framing and
bit meaning are confirmed by prints made by this tool (text, markers and line blocks come out right);
the byte stuffing and the line order need the qualifications below.

```
ESC F CR ESC G 0 0 10 640 1024 1 CR ESC Z      header (a fixed string, with 10 filled in)
<1024 lines x 80 bytes>                         binary
CR
```

- Each line is 640 dots packed into 80 bytes, **MSB first**.
- Every byte is **inverted** before output, so a set bit means "put a dot here" (the DIB uses
  0 for black, 1 for white).
- Any output byte equal to `0x5B` (`[`) is **written twice** (byte stuffing). **[code]** only: no
  bitmap sent so far contained that byte, so it is unconfirmed on the wire (`open-questions.md` Q17).
- The loop reads the 1 bpp surface as 128 byte columns x 8 bit positions = 1024 lines. With the
  flip flag set it walks the other way (180 degree rotation).
- **The lines go out in the reverse order to the colour planes** (measured from a printed card; the
  capture's K bitmap was all zeros). See `captured-job.md`.

What `10` means in the header is unknown. **[guess]** a heat/energy value, as it is a literal in
the code. It is sent as is and the printer accepts it.

Before packing the driver may run a monochrome conversion (`FUN_180004230`: diffusion, halftone
or barcode, per the Printing Preferences help page) and a rotation (`FUN_180005e50`). For our tool
we can produce the 1-bit mask directly.

## 5. Overlay bitmap (`vG`)

`SendVarnishBitmapFile` loads a user `.bmp`, stretches it to 1024 x 640 at 1 bpp, encodes it with
`FUN_180009fc0` into a buffer, then sends:

```
ESC vG 0 0 10 640 1024 1 CR ESC Z        (fmt at 0x180032948)
<encoded data> CR
```

The encoding of `FUN_180009fc0` is **not yet analysed**; it is probably the same packing as the
K bitmap but that is a guess. Rectangle-based overlay modes (`vL`) avoid this path entirely and
are enough for the common cases.
