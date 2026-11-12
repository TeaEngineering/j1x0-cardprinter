# Status, queries and transport

Sources: `jvlLMON.DLL` (language monitor), `GetPrinterData` calls in `jvlRaster.DLL`, `jv.inf`.
Not analysed in depth yet; this file records what the strings and imports already show.

## USB identity

`jv.inf` matches hardware id `USBPRINT\JAVEL_CARD_PRINTERJ1702A` for the J110i
("Javelin J110i Card Printer USB"). The J110m is `...J1B16B`, the J120i `...J1706E`. The VID/PID
are not in the INF and must be read from the device (`system_profiler SPUSBDataType`, or
`lsusb`). **[code]**

The device is a standard USB printer-class device. Reuse the steps from the rollingmill code:
claim the interface, find the bulk-OUT endpoint, then GET_DEVICE_ID and SOFT_RESET. **[infer]**
Bulk-IN for replies has not been confirmed.

## Driver-to-printer queries

`DrvStartPage` (`FUN_18000eee0`) calls `GetPrinterDataW` twice with the *command text itself* as
the value name (`ESC !RIBBON CR`, and a second query beginning `ESC ...`). The monitor presumably
forwards the text to the printer and returns the reply. A reply is terminated by `0x06` (ACK);
the driver strips it, then parses the ribbon id from the text. **[code]**

`jvlLMON.DLL` also contains the format strings `Ribbon=%ld` and `Model=%s`, and in the driver
`jvRIBBON.INI` is read (`\jvRibbon.INI`) to turn the ribbon id into a name. **[code]**

## Monitor request names

The monitor handles request names such as `GetFileVersion`, `IsDOCComplete`, `CheckPortStatus`,
`ISREADY`, `CHECK_STATUS`, `RESET`, `ISHIDREADY` and `HIDDONE`. These look like keys for
`GetPrinterData`/`SetPrinterData` rather than wire commands. **[infer]**

Raw ESC commands seen in the monitor: `V`, `R`, `!RIBBON`, `!RIBLEN`, `MO`, `!FF 0`, `E 1`,
`E 2`, `A 1`, `A 2`, `A 3`, `MS 2`, `MASTER`, `&E*`, `&R`, `MF`, `MI`, `# 1 TF`, and a `GS` form. Their
semantics are unknown.

## Status and error strings

These are the strings the monitor shows. The mapping from printer reply to string has not been
decoded; the order below is the order in the file.

```
OUT OF RIBBON, TEMPERATURE, MECHANICAL ERROR, OUT OF CARD, CARD_IN_ENCODER, CARD_NOT_IN_ENCODER,
HEAD OPEN, REMOVE RIBBON, COMMAND ERROR (repeated), INSERT CARD, CHECK RIBBON, PARAMETERS ERROR,
ENCODING ERROR, READING ERROR, MECHANICAL ERROR, MAGNETIC ERROR, ROTATION ERROR, COVER OPEN,
SEQUENCE_ERROR, ERROR PROX, ERROR CONTACT DATA, ERROR PROX DATA, FLASH ERROR
```

## RFID and smart card

- `ERROR PROX`, `ERROR PROX DATA` and `ISHIDREADY`/`HIDDONE` point to a proximity (HID-style)
  card encoder; `ERROR CONTACT DATA` points to a contact smart-card encoder. **[infer]**
- The monitor loads two helper libraries at run time, `EILMON.dll` (from `jvlLMON.DLL`) and
  `EInt.dll` (from `jvlRaster.DLL`). Neither is in the package that was installed. They are the
  most likely home of the encoder control code. **[infer]**
- The help file's Device Information page says "Smart Card - identifies if a smart card encoder
  is present and type of encoder", so this is a per-unit option.
- Next step: look for the installer's other components, or read the encoder commands (`A n`, `E n`)
  out of the monitor's request handlers.
