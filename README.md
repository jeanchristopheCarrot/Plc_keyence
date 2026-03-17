# Keyence PLC Register UI

Simple web UI to read and write PLC registers in:

- **Simulator mode** (in-memory registers for testing)
- **TCP/IP mode** (raw socket commands to a PLC)

## Run

```bash
python3 -m pip install -r requirements.txt
python3 app.py --port 8080
```

Open:

```text
http://127.0.0.1:8080
```

## Features

- Read register value
- Write register value
- Switch between Simulator and TCP/IP mode
- Configure TCP connection:
  - Host
  - Port
  - Timeout
  - Encoding
- Configure protocol templates:
  - Read template (default: `RD {register}`)
  - Write template (default: `WR {register} {value}`)
  - Terminator (default: `\r\n`)
- Upload a Keyence ZIP/CSV file and preload simulator registers
- Upload an EventTypeList file and decode event type/output/input words
- Operation log for diagnostics

## Load simulator registers from file

In the UI, use **Load Simulator Registers from File** and upload one of:

- a Keyence project ZIP that contains `PlcDeviceValue.csv`
- a standalone `PlcDeviceValue.csv`

The app imports `DM*` and `R*` values into simulator memory so your simulator
reads/writes start from your actual project values.

## Event type decoder

The UI includes an **Event Type Decoder** section:

- Upload `Registers_RevM_EventTypeList_WithAlarmText` (`.xlsx`, `.xlsm`, or `.csv`)
- Sequences are read from **column K (Sequence)**.
- Each EventTrigger block is read from one **C..L row** (10 DM words).
- UI provides:
  - Sequence dropdown (column K values)
  - EventTrigger block dropdown (per sequence)
- Sequence decode table with columns:
  - EventType
  - Outputs ON
  - Inputs ON
  - AlarmCode / AlarmText
- It decodes each selected block from simulator DM values into:
  - event type value (first word)
  - output words (`words[1..4]`)
  - input words (`words[5..8]`)
  - active output/input bits

By default, if `Registers_RevM_EventTypeList_WithAlarmText.xlsx` exists in the
project root, it is auto-loaded on startup.

If no event list file has been uploaded/found, fallback includes:

- `Capture Canister - removal` (`DM23480`-`DM23489`)

## Notes for Keyence PLC communication

Different Keyence PLC families and project settings can use different command formats.
Use the **Command Templates** section to match your PLC protocol syntax.

The server sends exactly:

- `readTemplate` formatted with `{register}`
- `writeTemplate` formatted with `{register}` and `{value}`
- plus the configured terminator

Returned TCP responses are shown raw in the operation log, and the app attempts to parse the first integer value automatically on read.