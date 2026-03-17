# Keyence PLC Register UI

Simple web UI to read and write PLC registers in:

- **Simulator mode** (in-memory registers for testing)
- **TCP/IP mode** (raw socket commands to a PLC)

## Run

```bash
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
- Operation log for diagnostics

## Notes for Keyence PLC communication

Different Keyence PLC families and project settings can use different command formats.
Use the **Command Templates** section to match your PLC protocol syntax.

The server sends exactly:

- `readTemplate` formatted with `{register}`
- `writeTemplate` formatted with `{register}` and `{value}`
- plus the configured terminator

Returned TCP responses are shown raw in the operation log, and the app attempts to parse the first integer value automatically on read.