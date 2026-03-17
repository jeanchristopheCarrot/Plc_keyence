#!/usr/bin/env python3
import argparse
import json
import re
import socket
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional


ROOT_DIR = Path(__file__).parent
STATIC_DIR = ROOT_DIR / "static"


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
        with self._lock:
            return self._registers.get(register, 0)

    def write(self, register: str, value: int) -> None:
        with self._lock:
            self._registers[register] = value

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(sorted(self._registers.items()))


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


class RequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT_DIR), **kwargs)

    def do_GET(self) -> None:
        if self.path == "/":
            self.path = "/static/index.html"
            return super().do_GET()

        if self.path == "/api/simulator/registers":
            return self._send_json(HTTPStatus.OK, {"registers": SIMULATOR.snapshot()})

        return super().do_GET()

    def do_POST(self) -> None:
        if self.path == "/api/read":
            return self._handle_read()
        if self.path == "/api/write":
            return self._handle_write()
        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})

    def _handle_read(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return

        register = str(payload.get("register", "")).strip()
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

        register = str(payload.get("register", "")).strip()
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
