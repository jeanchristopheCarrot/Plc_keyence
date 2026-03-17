#!/usr/bin/env python3
import argparse
import csv
import io
import json
import re
import socket
import threading
import zipfile
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import load_workbook


ROOT_DIR = Path(__file__).parent
STATIC_DIR = ROOT_DIR / "static"
MAX_UPLOAD_BYTES = 120 * 1024 * 1024
REGISTER_PATTERN = re.compile(r"^(DM|R)\d+$")
EVENT_RANGE_PATTERN = re.compile(r"(\d{4,6})\s*[-–]\s*(\d{4,6})")

OUTPUT_SIGNAL_NAMES = {
    0: "Output Signal #1 (00608 pins 3&4)",
    1: "Output Signal #2 (00609 pins 5&6)",
    2: "Output Signal #3 (00610 pins 7&8)",
    3: "Output Signal #4 (00611 pins 9&10)",
    4: "Output Signal #5 (00612 pins 11&12)",
    5: "Output Signal #6 (00613 pins 13&14)",
    6: "Output Signal #7 (00614 pins 15&16)",
    7: "Output Signal #8 (00615 pins 17&18)",
    8: "Output Signal #9 (00700 pins 19&20)",
    9: "Output Signal #10 (00701 pins 21&22)",
    10: "Output Signal #11 (00702 pins 23&24)",
}


def decode_escapes(value: str) -> str:
    return (
        value.replace("\\r", "\r")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\0", "\0")
    )


def parse_first_int(response: str) -> Optional[int]:
    match = re.search(r"[-+]?\d+", response)
    if not match:
        return None
    return int(match.group(0))


@dataclass
class TcpConnectionConfig:
    host: str
    port: int
    timeout_seconds: float


@dataclass
class ProtocolConfig:
    read_template: str
    write_template: str
    terminator: str
    encoding: str


class SimulatorStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._registers: Dict[str, int] = {
            "DM0": 0,
            "DM1": 100,
            "DM2": 200,
            "R0": 0,
            "R1": 1,
            "R2": 0,
        }

    def read(self, register: str) -> int:
        register = register.strip().upper()
        with self._lock:
            return self._registers.get(register, 0)

    def write(self, register: str, value: int) -> None:
        register = register.strip().upper()
        with self._lock:
            self._registers[register] = value

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(sorted(self._registers.items()))

    def load_registers(self, registers: Dict[str, int]) -> int:
        sanitized = {}
        for register, value in registers.items():
            key = register.strip().upper()
            if not REGISTER_PATTERN.match(key):
                continue
            sanitized[key] = int(value)

        with self._lock:
            self._registers.update(sanitized)
        return len(sanitized)


@dataclass
class EventDefinition:
    name: str
    start: int
    end: int


class EventDefinitionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._definitions: List[EventDefinition] = [
            EventDefinition(
                name="Capture Canister - removal",
                start=23480,
                end=23489,
            )
        ]

    def get_all(self) -> List[EventDefinition]:
        with self._lock:
            return list(self._definitions)

    def replace(self, new_definitions: List[EventDefinition]) -> None:
        with self._lock:
            self._definitions = list(new_definitions)


class PlcTcpClient:
    def __init__(self, conn: TcpConnectionConfig, protocol: ProtocolConfig) -> None:
        self.conn = conn
        self.protocol = protocol

    def read(self, register: str) -> Dict[str, Any]:
        command = self.protocol.read_template.format(register=register)
        response = self._send(command)
        return {
            "command": command,
            "rawResponse": response,
            "parsedValue": parse_first_int(response),
        }

    def write(self, register: str, value: int) -> Dict[str, Any]:
        command = self.protocol.write_template.format(register=register, value=value)
        response = self._send(command)
        return {
            "command": command,
            "rawResponse": response,
        }

    def _send(self, command: str) -> str:
        payload = command + self.protocol.terminator
        encoded = payload.encode(self.protocol.encoding)

        with socket.create_connection(
            (self.conn.host, self.conn.port), timeout=self.conn.timeout_seconds
        ) as plc_socket:
            plc_socket.sendall(encoded)
            plc_socket.settimeout(self.conn.timeout_seconds)
            chunks = []
            while True:
                try:
                    data = plc_socket.recv(4096)
                except socket.timeout:
                    break
                if not data:
                    break
                chunks.append(data)
                if len(data) < 4096:
                    break

        if not chunks:
            return ""
        return b"".join(chunks).decode(self.protocol.encoding, errors="replace")


SIMULATOR = SimulatorStore()
EVENT_DEFINITIONS = EventDefinitionStore()


def parse_numeric_value(raw_value: str) -> Optional[int]:
    value = raw_value.strip()
    if not value or value == "-":
        return None

    if value.startswith("$"):
        try:
            return int(value[1:], 16)
        except ValueError:
            return None

    try:
        return int(value)
    except ValueError:
        try:
            return int(float(value))
        except ValueError:
            return None


def decode_csv_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Could not decode CSV content")


def parse_plc_device_csv(raw_csv: bytes) -> Tuple[Dict[str, int], Dict[str, int]]:
    content = decode_csv_bytes(raw_csv)
    reader = csv.reader(io.StringIO(content))

    registers: Dict[str, int] = {}
    matched_rows = 0
    loaded_rows = 0
    skipped_value_rows = 0

    for row in reader:
        if len(row) < 3:
            continue

        device = row[1].strip().upper()
        if not REGISTER_PATTERN.match(device):
            continue

        matched_rows += 1
        parsed_value = parse_numeric_value(row[2])
        if parsed_value is None:
            skipped_value_rows += 1
            continue

        registers[device] = parsed_value
        loaded_rows += 1

    return registers, {
        "matchedRows": matched_rows,
        "loadedRows": loaded_rows,
        "skippedRows": skipped_value_rows,
    }


def parse_uploaded_register_file(
    filename: str, payload: bytes
) -> Tuple[Dict[str, int], Dict[str, Any]]:
    suffix = Path(filename).suffix.lower()
    source_file = filename
    csv_bytes = payload

    if suffix == ".zip" or payload.startswith(b"PK"):
        with zipfile.ZipFile(io.BytesIO(payload), mode="r") as zip_file:
            names = zip_file.namelist()
            preferred = [name for name in names if name.lower().endswith("plcdevicevalue.csv")]
            if preferred:
                source_file = preferred[0]
            else:
                candidates = [name for name in names if name.lower().endswith(".csv")]
                if not candidates:
                    raise ValueError("ZIP does not contain a CSV file")
                source_file = candidates[0]
            csv_bytes = zip_file.read(source_file)
    elif suffix != ".csv":
        raise ValueError("Only .zip and .csv files are supported")

    registers, stats = parse_plc_device_csv(csv_bytes)
    metadata: Dict[str, Any] = {
        "sourceFile": source_file,
        "fileName": filename,
        **stats,
    }
    return registers, metadata


def is_register_address_candidate(value: int) -> bool:
    return 1000 <= value <= 999999


def parse_event_definitions_from_csv(raw_bytes: bytes) -> List[EventDefinition]:
    content = decode_csv_bytes(raw_bytes)
    reader = csv.reader(io.StringIO(content))
    rows = list(reader)

    # Prefer explicit Event Type sheet exports where columns are fixed:
    # Row | Column | Register | Value | Decimal | EventTypeCode | EventTypeName | ...
    if rows:
        header = [str(cell).strip() for cell in rows[0]]
        normalized = [cell.lower() for cell in header]
        if "register" in normalized and "eventtypename" in normalized:
            register_idx = normalized.index("register")
            row_label_idx = normalized.index("row") if "row" in normalized else 0
            event_name_idx = normalized.index("eventtypename")

            grouped: Dict[Tuple[str, str], List[int]] = {}
            for row in rows[1:]:
                if len(row) <= max(register_idx, event_name_idx):
                    continue
                register_raw = str(row[register_idx]).strip()
                event_name = str(row[event_name_idx]).strip()
                row_label = (
                    str(row[row_label_idx]).strip() if len(row) > row_label_idx else ""
                )
                if not register_raw.isdigit():
                    continue
                register = int(register_raw)
                if not event_name or "event type - step" not in row_label.lower():
                    continue

                key = (event_name, row_label)
                grouped.setdefault(key, []).append(register)

            if grouped:
                parsed_from_schema = [
                    EventDefinition(
                        name=f"{event_name} ({row_label})",
                        start=min(registers),
                        end=max(registers),
                    )
                    for (event_name, row_label), registers in grouped.items()
                ]
                parsed_from_schema.sort(
                    key=lambda item: (item.start, item.end, item.name.lower())
                )
                return parsed_from_schema

    parsed: List[EventDefinition] = []
    seen = set()

    for row in rows:
        cells = [str(cell).strip() for cell in row if str(cell).strip()]
        if not cells:
            continue

        joined = " | ".join(cells)
        match = EVENT_RANGE_PATTERN.search(joined)
        start = None
        end = None
        if match:
            start = int(match.group(1))
            end = int(match.group(2))
        else:
            nums = []
            for cell in cells:
                if re.fullmatch(r"\d{4,6}", cell):
                    nums.append(int(cell))
            if len(nums) >= 2:
                start, end = nums[0], nums[1]

        if start is None or end is None:
            continue
        if start > end:
            start, end = end, start
        if not is_register_address_candidate(start) or not is_register_address_candidate(
            end
        ):
            continue
        if end - start > 400:
            continue

        name = next(
            (
                cell
                for cell in cells
                if re.search(r"[A-Za-z]", cell) and not EVENT_RANGE_PATTERN.search(cell)
            ),
            f"Event {start}-{end}",
        )
        key = (name, start, end)
        if key in seen:
            continue
        seen.add(key)
        parsed.append(EventDefinition(name=name, start=start, end=end))

    parsed.sort(key=lambda item: (item.start, item.end, item.name.lower()))
    return parsed


def parse_event_definitions_from_xlsx(raw_bytes: bytes) -> List[EventDefinition]:
    workbook = load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)

    grouped: Dict[Tuple[str, str], List[int]] = {}
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows(values_only=True):
            values = list(row)
            if len(values) <= 10:
                continue

            register = values[2]
            row_label = values[4]
            event_name = values[10]

            if not isinstance(register, int):
                continue
            if not isinstance(row_label, str) or "event type - step" not in row_label.lower():
                continue
            if not isinstance(event_name, str) or not event_name.strip():
                continue

            key = (event_name.strip(), row_label.strip())
            grouped.setdefault(key, []).append(register)

    if grouped:
        parsed_from_schema = [
            EventDefinition(
                name=f"{event_name} ({row_label})",
                start=min(registers),
                end=max(registers),
            )
            for (event_name, row_label), registers in grouped.items()
        ]
        parsed_from_schema.sort(key=lambda item: (item.start, item.end, item.name.lower()))
        return parsed_from_schema

    parsed: List[EventDefinition] = []
    seen = set()

    for sheet in workbook.worksheets:
        for row in sheet.iter_rows(values_only=True):
            cells = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
            if not cells:
                continue

            joined = " | ".join(cells)
            match = EVENT_RANGE_PATTERN.search(joined)
            start = None
            end = None
            if match:
                start = int(match.group(1))
                end = int(match.group(2))
            else:
                nums = []
                for cell in cells:
                    if re.fullmatch(r"\d{4,6}", cell):
                        nums.append(int(cell))
                if len(nums) >= 2:
                    start, end = nums[0], nums[1]

            if start is None or end is None:
                continue
            if start > end:
                start, end = end, start
            if not is_register_address_candidate(start) or not is_register_address_candidate(
                end
            ):
                continue
            if end - start > 400:
                continue

            name = next(
                (
                    cell
                    for cell in cells
                    if re.search(r"[A-Za-z]", cell)
                    and not EVENT_RANGE_PATTERN.search(cell)
                ),
                f"Event {start}-{end}",
            )
            key = (name, start, end)
            if key in seen:
                continue
            seen.add(key)
            parsed.append(EventDefinition(name=name, start=start, end=end))

    parsed.sort(key=lambda item: (item.start, item.end, item.name.lower()))
    return parsed


def parse_uploaded_event_list_file(filename: str, payload: bytes) -> List[EventDefinition]:
    suffix = Path(filename).suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return parse_event_definitions_from_xlsx(payload)
    if suffix == ".csv":
        return parse_event_definitions_from_csv(payload)
    raise ValueError("Only .xlsx, .xlsm, and .csv files are supported for event list")


def decode_active_bits(words: List[int]) -> List[int]:
    active = []
    for word_index, word in enumerate(words):
        value = int(word) & 0xFFFF
        for bit in range(16):
            if value & (1 << bit):
                active.append(word_index * 16 + bit)
    return active


def decode_event_definition(definition: EventDefinition, registers: Dict[str, int]) -> Dict[str, Any]:
    words = [int(registers.get(f"DM{addr}", 0)) for addr in range(definition.start, definition.end + 1)]
    event_type_value = words[0] if words else 0
    output_words = words[1:5] if len(words) > 1 else []
    input_words = words[5:9] if len(words) > 5 else []
    control_words = words[9:] if len(words) > 9 else []

    output_bits = decode_active_bits(output_words)
    input_bits = decode_active_bits(input_words)
    named_outputs = [
        OUTPUT_SIGNAL_NAMES[bit] for bit in output_bits if bit in OUTPUT_SIGNAL_NAMES
    ]

    return {
        "name": definition.name,
        "start": definition.start,
        "end": definition.end,
        "wordCount": len(words),
        "eventTypeValue": event_type_value,
        "rawWords": words,
        "outputWords": output_words,
        "inputWords": input_words,
        "controlWords": control_words,
        "activeOutputBits": output_bits,
        "activeInputBits": input_bits,
        "namedActiveOutputs": named_outputs,
    }


class RequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT_DIR), **kwargs)

    def do_GET(self) -> None:
        if self.path == "/":
            self.path = "/static/index.html"
            return super().do_GET()

        if self.path == "/api/simulator/registers":
            return self._send_json(HTTPStatus.OK, {"registers": SIMULATOR.snapshot()})
        if self.path == "/api/event-definitions":
            return self._handle_get_event_definitions()

        return super().do_GET()

    def do_POST(self) -> None:
        if self.path == "/api/read":
            return self._handle_read()
        if self.path == "/api/write":
            return self._handle_write()
        if self.path == "/api/upload-register-file":
            return self._handle_upload_register_file()
        if self.path == "/api/upload-event-list":
            return self._handle_upload_event_list()
        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})

    def _handle_read(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return

        register = str(payload.get("register", "")).strip().upper()
        if not register:
            return self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "register is required"}
            )

        mode = str(payload.get("mode", "simulator")).strip().lower()
        if mode == "simulator":
            value = SIMULATOR.read(register)
            return self._send_json(
                HTTPStatus.OK,
                {"mode": "simulator", "register": register, "value": value},
            )

        if mode == "tcp":
            try:
                client = self._build_tcp_client(payload)
                result = client.read(register)
            except Exception as exc:  # pragma: no cover - endpoint safety
                return self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"TCP read failed: {exc}"},
                )

            return self._send_json(
                HTTPStatus.OK,
                {
                    "mode": "tcp",
                    "register": register,
                    "value": result["parsedValue"],
                    "rawResponse": result["rawResponse"],
                    "command": result["command"],
                },
            )

        return self._send_json(
            HTTPStatus.BAD_REQUEST, {"error": f"Unsupported mode: {mode}"}
        )

    def _handle_write(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return

        register = str(payload.get("register", "")).strip().upper()
        if not register:
            return self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "register is required"}
            )

        try:
            value = int(payload.get("value"))
        except (TypeError, ValueError):
            return self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "value is required and must be an integer"},
            )

        mode = str(payload.get("mode", "simulator")).strip().lower()
        if mode == "simulator":
            SIMULATOR.write(register, value)
            return self._send_json(
                HTTPStatus.OK,
                {"mode": "simulator", "register": register, "value": value},
            )

        if mode == "tcp":
            try:
                client = self._build_tcp_client(payload)
                result = client.write(register, value)
            except Exception as exc:  # pragma: no cover - endpoint safety
                return self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": f"TCP write failed: {exc}"},
                )

            return self._send_json(
                HTTPStatus.OK,
                {
                    "mode": "tcp",
                    "register": register,
                    "value": value,
                    "rawResponse": result["rawResponse"],
                    "command": result["command"],
                },
            )

        return self._send_json(
            HTTPStatus.BAD_REQUEST, {"error": f"Unsupported mode: {mode}"}
        )

    def _build_tcp_client(self, payload: Dict[str, Any]) -> PlcTcpClient:
        connection = payload.get("connection") or {}
        protocol = payload.get("protocol") or {}

        host = str(connection.get("host", "")).strip()
        if not host:
            raise ValueError("connection.host is required for TCP mode")

        port = int(connection.get("port", 8501))
        timeout_seconds = float(connection.get("timeoutSeconds", 1.5))
        if timeout_seconds <= 0:
            raise ValueError("connection.timeoutSeconds must be positive")

        read_template = str(protocol.get("readTemplate", "RD {register}"))
        write_template = str(protocol.get("writeTemplate", "WR {register} {value}"))
        terminator = decode_escapes(str(protocol.get("terminator", "\\r\\n")))
        encoding = str(protocol.get("encoding", "ascii"))

        client = PlcTcpClient(
            conn=TcpConnectionConfig(
                host=host,
                port=port,
                timeout_seconds=timeout_seconds,
            ),
            protocol=ProtocolConfig(
                read_template=read_template,
                write_template=write_template,
                terminator=terminator,
                encoding=encoding,
            ),
        )
        return client

    def _handle_upload_register_file(self) -> None:
        content_len_header = self.headers.get("Content-Length")
        if not content_len_header:
            return self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "Missing Content-Length"}
            )

        try:
            content_length = int(content_len_header)
        except ValueError:
            return self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"}
            )

        if content_length <= 0:
            return self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "Uploaded file is empty"}
            )

        if content_length > MAX_UPLOAD_BYTES:
            return self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": (
                        f"File is too large ({content_length} bytes). "
                        f"Max supported size is {MAX_UPLOAD_BYTES} bytes."
                    )
                },
            )

        raw_payload = self.rfile.read(content_length)
        filename = Path(self.headers.get("X-Filename", "upload.bin")).name

        try:
            registers, metadata = parse_uploaded_register_file(filename, raw_payload)
            loaded_count = SIMULATOR.load_registers(registers)
        except Exception as exc:
            return self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": f"Unable to import register file: {exc}"},
            )

        return self._send_json(
            HTTPStatus.OK,
            {
                "message": "Simulator registers imported successfully",
                "loadedRegisters": loaded_count,
                "sourceFile": metadata["sourceFile"],
                "matchedRows": metadata["matchedRows"],
                "loadedRows": metadata["loadedRows"],
                "skippedRows": metadata["skippedRows"],
            },
        )

    def _handle_upload_event_list(self) -> None:
        content_len_header = self.headers.get("Content-Length")
        if not content_len_header:
            return self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "Missing Content-Length"}
            )

        try:
            content_length = int(content_len_header)
        except ValueError:
            return self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"}
            )

        if content_length <= 0:
            return self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "Uploaded file is empty"}
            )
        if content_length > MAX_UPLOAD_BYTES:
            return self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": (
                        f"File is too large ({content_length} bytes). "
                        f"Max supported size is {MAX_UPLOAD_BYTES} bytes."
                    )
                },
            )

        raw_payload = self.rfile.read(content_length)
        filename = Path(self.headers.get("X-Filename", "event-list.bin")).name

        try:
            definitions = parse_uploaded_event_list_file(filename, raw_payload)
            if not definitions:
                raise ValueError("No event/register ranges found in file")
            EVENT_DEFINITIONS.replace(definitions)
        except Exception as exc:
            return self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": f"Unable to import event list file: {exc}"},
            )

        return self._send_json(
            HTTPStatus.OK,
            {
                "message": "Event list imported successfully",
                "loadedEvents": len(definitions),
                "firstEvent": {
                    "name": definitions[0].name,
                    "start": definitions[0].start,
                    "end": definitions[0].end,
                },
            },
        )

    def _handle_get_event_definitions(self) -> None:
        definitions = EVENT_DEFINITIONS.get_all()
        registers = SIMULATOR.snapshot()
        decoded = [decode_event_definition(defn, registers) for defn in definitions]
        return self._send_json(
            HTTPStatus.OK,
            {
                "events": decoded,
                "notes": [
                    "Decoding uses DM[start..end] words.",
                    "eventTypeValue=first word, outputs=words[1..4], inputs=words[5..8].",
                    "Load simulator registers from PlcDeviceValue first for accurate values.",
                ],
            },
        )

    def _read_json_body(self) -> Optional[Dict[str, Any]]:
        content_len = self.headers.get("Content-Length")
        if not content_len:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing Content-Length"})
            return None
        try:
            body = self.rfile.read(int(content_len))
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Expected JSON object")
            return payload
        except Exception as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": f"Invalid JSON body: {exc}"}
            )
            return None

    def _send_json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def run_server(port: int) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), RequestHandler)
    print(f"PLC UI available at http://127.0.0.1:{port}")
    server.serve_forever()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Keyence PLC register UI server")
    parser.add_argument("--port", type=int, default=8080, help="HTTP server port")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_server(args.port)
