# Status, queries and transport

Sources: `jvlLMON.DLL` (the Windows language monitor), the `GetPrinterData` calls in
`jvlRaster.DLL`, `jv.inf`. Function names below are the ones annotated in the Ghidra project.
Markers as in `protocol.md`: **[code]** read from code or data, **[infer]**, **[guess]**.

This file is the source for `j1x0_cardprinter/status.py`, `usb.py` and `probe.py`.

## 1. USB identity

`jv.inf` matches hardware id `USBPRINT\JAVEL_CARD_PRINTERJ1702A` for the J110i ("Javelin J110i
Card Printer USB"); the J110m is `...J1B16B`, the J120i `...J1706E`. **[code]** Windows builds
that id from the printer's IEEE-1284 device id, so the id string contains the same text. The
VID/PID are not in the INF.

The device is a standard USB printer-class device (class 7). **[infer]** That gives us two ways in
without knowing the VID/PID:

- enumerate every printer-class interface and read the **GET_DEVICE_ID** string
  (`bmRequestType 0xA1, bRequest 0, wIndex = interface<<8 | alt`), then pick the one that
  mentions `JAVEL` or a `J1x0` model. This is what `list_printers`/`choose` do.
- or pass `--vid`/`--pid` explicitly (`system_profiler SPUSBDataType`, `lsusb`).

The match pattern (`javel|j1[012]0`) is a guess from the INF; the real id string has not been
seen. **[guess]**

## 2. What Windows does when a document starts

`OpenPortSequence` (`jvlLMON.DLL` `0x180005ca0`). **[code]**

1. Reset monitor state and run `ESC V CR` through `ExecPortCommand`.
2. Open the underlying port (the monitor wraps `usbmon`'s OpenPort/WritePort/ReadPort/ClosePort
   through function pointers at `port+0x130..0x148`) and take a lock (`port+0x44 = 1`).
3. **Port status check** (section 3). Fail unless it passes.
4. `QueryRibbonAndModel` (`0x180002320`): `ESC !RIBBON CR` then `ESC V CR`. If the ribbon id is
   0 it shows a message box and re-polls `!RIBBON` once a second until a ribbon is present.
5. For panel (colour) ribbons only, `CheckRibbonRemaining` (`0x180003260`): `ESC !RIBLEN CR`;
   a count of 20, 10 or 5 starts a low-ribbon warning. Mono and special ribbons skip this, as
   the help file says the remaining count is "Unknown" for them. Documents whose name begins
   `ISERIES` also skip it.
6. The monitor itself writes **`ESC MASTER CR`**, then sets the session flag.

There is **no periodic heartbeat** in the paths analysed. The status byte and the queries above
are one-shot checks at open. **[infer]** (not every function in the monitor has been read)

`j1x0 send` with no job file runs steps 3 to 5 (without the `MASTER` write) and stops.

Note step 6: the raster driver's job header also begins with `ESC MASTER CR`
(`SendJobHeader`), so a Windows job probably carries `MASTER` twice. Whether the printer needs
it once or twice is open (`open-questions.md`).

## 3. Port status byte

`GetPortStatus` (`0x180002f30`) calls the wrapped monitor's GetPrinterDataFromPort with
`IOCTL 0x160004` for USB ports. That code is `IOCTL_USBPRINT_GET_LPT_STATUS`, which usbprint.sys
satisfies with the USB printer-class **GET_PORT_STATUS** request (`0xA1, bRequest 1`, one byte).
**[infer]** (the ioctl number is from the code; the mapping to the class request is the usual
behaviour of usbprint.sys.) The function returns `status | 0x100` when the request succeeded.

Bits, as for an LPT status register:

| Bit | Mask | Meaning |
|---|---|---|
| 5 | `0x20` | paper empty. The card printer seems to use it for "needs attention". |
| 4 | `0x10` | selected |
| 3 | `0x08` | not error |

The monitor lets the port open only if the request succeeded **and** (`status & 0x20` or
`(status & 0x38) == 0x18`). **[code]** An idle, healthy printer is therefore expected to read
`0x18`. `PortStatus.ok_to_open` implements exactly this test.

## 4. Query framing

Host queries are the command text passed as a `GetPrinterData` value name; `ExecPortCommand`
(`0x180004e50`) turns them into wire traffic. **[code]**

```
host -> printer   ESC <command> CR  then  ESC E 2 CR          (USB; "ESC E 1 CR" on other ports)
printer -> host   <text> [space] ACK(0x06)                    success
                  NAK(0x15) d d ...                           error: two decimal digits = error id
```

- After writing, the monitor polls ReadPort every 10 ms until any bytes arrive (USB: up to 20
  seconds). Callers then require the **last byte to be `0x06`** and strip a trailing space.
- An error reply starts with `0x15`; the error id is `(b[1] & 15) * 10 + (b[2] & 15)` and is
  looked up in the table below (`LookupErrorMessage`).
- What `ESC E 2` does is not known. **[guess]** "enable response", i.e. it asks the printer to
  send the reply. The client code sends it after every query, so ours does too.

### Queries seen

| Command | Reply | Source |
|---|---|---|
| `ESC V CR` | model/firmware text | `QueryRibbonAndModel` |
| `ESC !RIBBON CR` | ribbon id, decimal | `QueryRibbonAndModel`; ids in `jvRIBBON.INI` |
| `ESC !RIBLEN CR` | remaining count, decimal | `CheckRibbonRemaining` |
| `ESC R CR`, `ESC RESET CR` | forwarded, then the monitor sleeps **10 s** and fakes an ACK | `ExecPortCommand` |
| `ESC A CR`, `ESC A 1..3 CR` | unknown (marks the command as encoder-related) | `ExecPortCommand` |
| `ESC MS 2`, `ESC MO CR` | clear the session flag | `ExecPortCommand` |

### Error ids (the monitor's table at `0x18001d320`) **[code]**

| Id | Message | Id | Message |
|---|---|---|---|
| 1 | OUT OF RIBBON | 35 | CHECK RIBBON |
| 2 | TEMPERATURE | 40 | PARAMETERS ERROR |
| 3, 43 | MECHANICAL ERROR | 41, 49 | ENCODING ERROR |
| 4 | OUT OF CARD | 42 | READING ERROR |
| 5 | CARD_IN_ENCODER | 44, 50 | MAGNETIC ERROR |
| 6 | CARD_NOT_IN_ENCODER | 45 | BLANK TRACK |
| 7, 8 | HEAD OPEN | 47 | ROTATION ERROR |
| 9 | REMOVE RIBBON | 51 | COVER OPEN |
| 16 | INSERT CARD | 53 | NO ACCESS |
| 10-14, 20-22, 30-34 | COMMAND ERROR | 54 | SEQUENCE_ERROR |
| 55 | ERROR PROX | 56 | ERROR CONTACT DATA |
| 57 | ERROR PROX DATA | 60 | FLASH ERROR |

Each entry also carries a second number (a monitor-internal status code); it is not used by the
Python code.

## 5. Monitor-level pseudo-commands (never reach the printer)

`ExecPortCommand` answers these itself. They are an interface between the Windows driver
components and are **not** wire commands. **[code]**

| Request | Reply |
|---|---|
| `GetFileVersion` | `8.00.02.01` (the driver version) |
| `IsDOCComplete` | `DOC_COMPLETE` or `DOC_PENDING` |
| `CheckPortStatus` | `STATUS_SUCCESS` or `STATUS_FAILED` |
| `ISHIDREADY`, `HIDDONE` | one byte, `0x01` (the HID/smart-card encoder hooks) |
| `ISREADY` | one byte; while a session is open it is the only command accepted (others fail `ERROR_BUSY`). It sets the session flag. Polarity of the reply byte is not confirmed. |
| `CHECK_STATUS` | with a session open: `ACK` if the port status is ok and `(status & 0x38) == 0x18`, otherwise `'8'` |
| `PrintFile:`, `sendcmds:`, `SetPrinterName:`, `download:` | text commands from the UI (send a file, send commands, rename, firmware download) |

`SLAVE`, which the help file's send-file format ends with, occurs only in `jvlUI.DLL` and
`jvllmonNw.dll`, never in the monitor or the raster driver. A job from the driver is therefore
not terminated with `SLAVE`; the UI's send-file feature adds it.

## 6. Smart card and RFID

- `ERROR PROX`, `ERROR PROX DATA` (ids 55, 57) and the `ISHIDREADY`/`HIDDONE` hooks point to a
  proximity (HID-style) card encoder; `ERROR CONTACT DATA` (56) to a contact smart-card encoder.
  **[infer]**
- The monitor loads `EILMON.dll` and the raster driver loads `EInt.dll` at run time. Neither is
  in the installed package; they are the most likely home of the encoder control code.
  **[infer]**
- The help file's Device Information page says "Smart Card - identifies if a smart card
  encoder is present and type of encoder", so this is a per-unit option.
- **The encoder type is encoded in the `ESC V` reply.** The Device Information dialog
  (section 7) looks for these substrings in it: `0iB` Contact Encoder, `0iC` Contact Encoder and
  HID iCLASS, `0iD` Contact Encoder and MIFARE, `0iE` Contact Station, `0iF` HID iCLASS,
  `0iH` MIFARE contactless. None found means no smart-card encoder. **[code]** So the two
  contactless (RFID) variants are `0iF` and `0iH`, and the question of whether a given unit has
  RFID can be answered from `ESC V` alone.

## 7. The Device Information dialog

`jvlUI.DLL` `FillDeviceInfoDialog` (`0x180015c90`) fills the dialog described by the help page
`Device Information.htm`. Each field is one query using the framing in section 4; a reply that
does not end in ACK shows as N/A. The control ids come from the dialog template in the DLL's
resources. **[code]**

| Help field | Query | Notes |
|---|---|---|
| Model/Firmware Version | `ESC V CR` | the reply text as is |
| Serial Number | `ESC %SERIE CR` | the printer's own serial, not the USB descriptor's |
| Head Resistance | `ESC !V 15 CR` | the reply text as is |
| Magnetic Encoder | `ESC !V 20 CR` | `1` means Yes, anything else No |
| Smart Card | substrings of the `ESC V` reply | section 6 |
| Ribbon Type | `ESC !RIBBON CR` | id, named through `jvRibbon.INI` |
| Remaining Count | `ESC !RIBLEN CR` | `0` is shown as N/A (mono ribbons) |
| Panels Printed | `ESC !NL CR` | leading `!` skipped, text up to the first space, as an integer |

The UI also issues `ESC !V 1`, `ESC !V 2`, `ESC !V 24` and `ESC !V 25`; those are not part of
this dialog and are not yet identified.

## 8. Implementation in this repository

| Piece | Where |
|---|---|
| Status byte, reply parsing, error and ribbon tables | `j1x0_cardprinter/status.py` |
| Enumeration by IEEE-1284 id, GET_PORT_STATUS, query | `j1x0_cardprinter/usb.py` |
| The link check (`j1x0 send` with no file) | `j1x0_cardprinter/probe.py` |

The link check asks for `V`, `!RIBBON` and `!RIBLEN` (these decide READY), then for the remaining
Device Information fields of section 7 (`%SERIE`, `!V 15`, `!V 20`, `!NL`) and decodes the
smart-card type from the `V` reply. Those four are informational: no answer shows as N/A. The
reply formats of `%SERIE`, `!V 15` and `!NL` are assumed to be plain text ending in ACK; the
Windows dialog suggests `!NL` contains a space (it searches for one) but the exact layout is
unknown.

Everything in these modules that touches the wire, the `ESC E 2` suffix, reading replies from
bulk-IN, the 1284 id match and the exact class-request arguments, is **unverified** until it has
run against a printer.
