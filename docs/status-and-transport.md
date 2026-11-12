# Status, queries and transport

Sources: `jvlLMON.DLL` (the Windows language monitor), the `GetPrinterData` calls in
`jvlRaster.DLL`, `jv.inf`. Function names below are the ones annotated in the Ghidra project.
Markers as in `protocol.md`: **[code]** read from code or data, **[infer]**, **[guess]**, and now
**[hw]** observed on a real printer.

This file is the source for `j1x0_cardprinter/status.py`, `usb.py` and `probe.py`.

## 0. Observed on a real J110i

First contact with hardware (a J110i, firmware V1.18.25, on macOS with libusb). **[hw]**

**The device.** `0a5f:005c`, USB 1.1 full speed, self-powered, bcdDevice 1.4, one configuration with
one interface: class 7 (printer), subclass 1, protocol 2 (bidirectional), with bulk-IN `0x81` and
bulk-OUT `0x02`, both 64 bytes. The vendor id `0a5f` is Zebra's; the printer's own strings say
`JAVELIN` (manufacturer) and `J110i` (product).

> **Correction.** The rows below that say `GET_DEVICE_ID` and `GET_PORT_STATUS` time out describe the
> first session on a freshly powered printer. A later USB capture of the Windows driver shows the
> printer **answering both**: `GET_PORT_STATUS` returned `0x18` (all 16 requests) and `GET_DEVICE_ID`
> returned 48 bytes, `MFG:JAVELIN CARD PRINTER;CMD:none;MDL:J110i  ;`. They are the same requests
> as ours (`A1 01 0000 0000`, 1 byte; `A1 00 0000 0000`, buffer up to 1009 bytes), and the same two
> requests answered instantly from the Mac once the printer had been used from Windows. Why they
> timed out the first time is unknown (`open-questions.md` Q15), so the code still does not rely on
> them.

**What answers and what does not.**

| Request | Result |
|---|---|
| Standard requests (device descriptor, GET_STATUS, configuration) | answered |
| String descriptor, asked with an explicit language id `0x0409` | answered, **except index 3** (the USB serial number), which times out |
| String descriptor 0 (the language list) | **times out**, so pyusb's `get_string` fails; ask for `0x0409` directly |
| Printer-class `GET_DEVICE_ID` (every form tried: lengths 2 to 1024, `wIndex` `0`, `0x0100`, interface claimed or not) | **timed out** on first contact; answered later (see the correction above) |
| Printer-class `GET_PORT_STATUS` | **timed out** on first contact; answered `0x18` later |
| Bulk-OUT writes, bulk-IN reads | work |

**The query channel is confirmed.** Writing `ESC V CR` and then `ESC E 2 CR` (two separate bulk
writes) returned `J110i V1.18.25` followed by `0x06`. Writing `ESC V CR` alone returned nothing in
3 seconds. So the `ESC E 2 CR` suffix is required, and replies are text ending in ACK, exactly as
section 4 says.

**Replies to the Device Information queries** (this unit):

| Query | Reply |
|---|---|
| `ESC V CR` | `J110i V1.18.25` (no smart-card code, so no encoder fitted) |
| `ESC %SERIE CR` | `J11000xxxx` |
| `ESC !V 15 CR` (head resistance) | `2890` |
| `ESC !V 20 CR` (magnetic encoder) | `0` |
| `ESC !RIBBON CR` | `2` (YMCKO) |
| `ESC !RIBLEN CR` | `132` |
| `ESC !NL CR` (panels printed) | `00012992 00000028`: the panels-printed count, then a second number (28) that is not identified |

**Consequences for the code.** The printer is identified by its strings and its vid/pid
(`KNOWN_PRINTERS` in `usb.py`), not by a 1284 id. The status byte is not used for readiness on this
model (`NO_STATUS_REQUEST`, cautious because of the first-contact timeouts); a version query answers
instead (`probe.quick_check`). A missing status byte is not a failure: the link check shows `port status: unavailable` and carries on. Everything is reached
in about 0.1 s. Once Q15 is settled the code can ask for the status byte, as Windows does.

## 1. USB identity

`jv.inf` matches hardware id `USBPRINT\JAVEL_CARD_PRINTERJ1702A` for the J110i ("Javelin J110i
Card Printer USB"); the J110m is `...J1B16B`, the J120i `...J1706E`. **[code]** Windows evidently
builds that id from the device's strings, since the printer has no 1284 id (section 0).

The device is a standard USB printer-class device (class 7). **[hw]** `j1x0` finds it without a
VID/PID by listing every printer-class interface and picking the one that is a known model or whose
strings or 1284 id mention `JAVEL` or a `J1x0` model (`list_printers`/`choose`). Pass
`--vid`/`--pid` to override (`system_profiler SPUSBDataType`, `lsusb`). Other models in the family
may behave differently: they are matched by their strings, and a model that does answer
`GET_DEVICE_ID` or `GET_PORT_STATUS` will be asked for them.

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

There is no periodic heartbeat in the monitor paths analysed: the status byte and the queries above are
one-shot checks at open. **[infer]** (not every function in the monitor has been read.) The USB capture
does show the Windows status UI polling `ESC V CR` + `ESC E 2 CR` about once a second while idle, so a
heartbeat exists at the application level, from `jvlAsyncUIServer`/`jvlUI` rather than the monitor.

`j1x0 send` with no job file runs steps 3 to 5 (without the `MASTER` write) and stops.

Note step 6: the raster driver's job header also begins with `ESC MASTER CR`
(`SendJobHeader`), so a Windows job carries `MASTER` twice. **[capture]** Confirmed: two in a row. Our
job header sends both.

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
`0x18`, and the capture confirms it: all 16 replies in the capture were `0x18`. `PortStatus.ok_to_open`
implements exactly this test.

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
- What `ESC E 2` does internally is not known, but its behaviour is: the printer answers a query only
  after it, and during a job it is the "ready for more?" handshake (one ACK as the reply).
  **[hw]** **[capture]** (The guess was "enable response".) Our code sends it after every query.

### Queries seen

| Command | Reply | Source |
|---|---|---|
| `ESC V CR` | model/firmware text | `QueryRibbonAndModel` |
| `ESC !RIBBON CR` | ribbon id, decimal | `QueryRibbonAndModel`; ids in `jvRIBBON.INI` |
| `ESC !RIBLEN CR` | remaining count, decimal | `CheckRibbonRemaining` |
| `ESC R CR`, `ESC RESET CR` | forwarded, then the monitor sleeps **10 s** and fakes an ACK | `ExecPortCommand` |
| `ESC A CR`, `ESC A 1..3 CR` | unknown (marks the command as encoder-related); never seen on the wire | `ExecPortCommand` |
| `ESC SLAVE CR` | the printer answers an ACK alone; sent once by the Windows UI at start-up, not in a job | **[capture]** |
| `ESC !V 1`, `!V 2`, `!V 24`, `!V 25` | `2 `, `20 `, `18 `, `20 ` on the test unit; meaning unknown | **[capture]** |
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
this dialog and are still not identified. The capture recorded their replies on the test unit
(`2 `, `20 `, `18 `, `20 `) but not what they mean (`open-questions.md` Q11).

## 8. Sending a job: acknowledgements and the end of the card

What the Windows port monitor does between the spooler and the printer. **[code]** Written up after a
J110i, sent a whole job in one go, stopped on the display "DOWNLOADING DATA", printed nothing and then
ignored every query until it was switched off. **[hw]**

**WritePort** (`WritePortChunked`, `jvlLMON.DLL` `0x180009140`). For a USB port it writes **at most 4096
bytes per call** (`0x1000`) and reports how many it wrote; the spooler calls again for the rest.

**What the capture showed** (`captured-job.md`, a real print from Windows). The monitor does not ask after every
chunk. It asks `ESC E 2 CR` and gets a single `0x06` (ACK):

* after each colour plane's data (before its `ESC IS p`) and after the `ESC IS p`,
* after the resin bitmap, `ESC I 10`, and `ESC IV 30` (not after `ESC vF`),
* after the final `ESC MO CR`,

and never during the job header or inside an image block. (`WaitForPrinterAck`, `jvlLMON.DLL`
`0x180006f20`, sleeps 50 ms, then reads the USB status if it can, and otherwise writes `ESC E 2 CR` and
reads the answer, up to 5 tries; a NAK is decoded to an error number.) A J110i does not answer the
status request, which is why the enquiries are needed at all.

**The end of the card.** The capture ends with `ESC MO CR` alone (move the card out), with no `CR` before it and no
`ESC !FF 0 CR` after it, on a YMCKO ribbon. The Ghidra reading of `EndDocPort` (`0x180006700`: sleep 2 s, then
`SendEndOfCardCommands`) had `CR`, `ESC MO CR`, `ESC !FF 0 CR` for panel ribbons; the capture is what the real
monitor did, so `protocol.end_of_card` follows it.

> **History.** The first hardware attempts left the printer on "DOWNLOADING DATA" and failed to get an
> acknowledgement after a plane. That was not a pacing problem: the planes were in the wrong format
> (run-length coded instead of packed, `image-data.md`), so the printer was still waiting for the rest of
> each one. Several pacing variants were tried and removed; there is only the monitor's behaviour now.

If a run goes wrong, `--log FILE` keeps a flushed, full-detail trail (stdout alone does not hold
the protocol log: it goes to stderr), and if a USB call blocks for 2 minutes a stack dump is written to it. (The printer prints each panel while it receives
the job and stops reading USB for tens of seconds between panels; `J1x0USB.write` waits for it, up to 5 minutes
without a byte moving, and `sender` waits up to 3 minutes for each ACK.)

**Implementation.** `sender.send_job` does the above; `j1x0 send -v` logs each command as it goes (with the
image blocks checked against the sizes their headers imply, `jobparse.py`), `-vv` also logs each
acknowledgement and every USB transfer. `--raw` writes the file as it is, for experiments.

## 9. Implementation in this repository

| Piece | Where |
|---|---|
| Status byte, reply parsing, error and ribbon tables | `j1x0_cardprinter/status.py` |
| Enumeration by IEEE-1284 id, GET_PORT_STATUS, query | `j1x0_cardprinter/usb.py` |
| The link check (`j1x0 send` with no file) | `j1x0_cardprinter/probe.py` |

The link check asks for `V`, `!RIBBON` and `!RIBLEN` (these decide READY), then for the remaining
Device Information fields of section 7 (`%SERIE`, `!V 15`, `!V 20`, `!NL`) and decodes the
smart-card type from the `V` reply. Those four are informational: no answer shows as N/A. The
reply formats of `%SERIE`, `!V 15` and `!NL` are plain text ending in ACK **[hw]**; `!NL` is
`<panels printed> <second number>` (for example `00013012 00000028`), and the second number has never
changed.

The `ESC E 2` suffix, reading replies from bulk-IN, and the strings-and-vid/pid match are verified on a
J110i **[hw]**. Not yet verified: the 1284 id match and `GET_PORT_STATUS` from a cold start
(`open-questions.md` Q15), and any other model.
