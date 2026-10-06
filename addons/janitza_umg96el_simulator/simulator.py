"""Read-only Modbus TCP simulator for the Janitza UMG 96-EL guide map."""

from __future__ import annotations

import http.server
import ipaddress
import json
import logging
import math
import os
import socket
import socketserver
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

LOGGER = logging.getLogger("janitza_simulator")
DEFAULTS = {
    "ip": "0.0.0.0",
    "tcp_port": 502,
    "unit_id": 1,
    "register_offset": 0,
    "update_interval": 200,
}

# The guide lists these as 32-bit IEEE 754 floats, with the high word first.
FLOAT_REGISTERS = {
    10: "ct_primary",
    12: "ct_secondary",
    802: "voltage_zero_sequence",
    804: "voltage_negative_sequence",
    806: "voltage_positive_sequence",
    848: "voltage_l1_real",
    850: "voltage_l2_real",
    852: "voltage_l3_real",
    854: "voltage_l1_imag",
    856: "voltage_l2_imag",
    858: "voltage_l3_imag",
    920: "current_zero_sequence",
    922: "current_negative_sequence",
    924: "current_positive_sequence",
    926: "current_l1_real",
    928: "current_l2_real",
    930: "current_l3_real",
    932: "current_l1_imag",
    934: "current_l2_imag",
    936: "current_l3_imag",
    5910: "voltage_l1_crest_factor",
    5912: "voltage_l2_crest_factor",
    5914: "voltage_l3_crest_factor",
    19000: "voltage_l1_n",
    19002: "voltage_l2_n",
    19004: "voltage_l3_n",
    19012: "current_l1",
    19014: "current_l2",
    19016: "current_l3",
    19026: "active_power_total",
    19034: "apparent_power_total",
    19042: "reactive_power_total",
    19050: "frequency",
    19068: "active_energy_import",
    19110: "voltage_l1_thd",
    19116: "current_l1_thd",
}

# Unlisted addresses inside these documented blocks read as zero; addresses
# outside the blocks receive the standard illegal-data-address exception.
REGISTER_BLOCKS = ((0, 12), (802, 936), (5910, 5915), (19000, 19117))
MAX_READ_REGISTERS = 125
OPTIONS_PATH = Path("/data/options.json")
DASHBOARD_PATH = Path(__file__).parent / "www" / "index.html"
MAX_ACTIVE_POWER_W = 10000.0
DEFAULT_ACTIVE_POWER_W = 2780.0
INITIAL_ENERGY_WH = 1_250_000.0
INGRESS_PORT = 8099


@dataclass(frozen=True)
class Settings:
    ip: str
    tcp_port: int
    unit_id: int
    register_offset: int
    update_interval: int


def load_settings(options_path: Path = OPTIONS_PATH) -> Settings:
    """Read and validate add-on options, allowing defaults for local runs."""
    options = DEFAULTS.copy()
    if options_path.exists():
        with options_path.open(encoding="utf-8") as options_file:
            loaded = json.load(options_file)
        if not isinstance(loaded, dict):
            raise ValueError("Add-on options must be a JSON object")
        options.update(loaded)

    settings = Settings(
        ip=_ipv4_address(options["ip"]),
        tcp_port=_bounded_int(options["tcp_port"], "tcp_port", 1, 65535),
        unit_id=_bounded_int(options["unit_id"], "unit_id", 1, 247),
        register_offset=_register_offset(options["register_offset"]),
        update_interval=_bounded_int(
            options["update_interval"], "update_interval", 50, 60000
        ),
    )
    if settings.tcp_port == INGRESS_PORT:
        raise ValueError(
            f"tcp_port cannot use {INGRESS_PORT}; that port is reserved for the sidebar"
        )
    if max(end for _, end in REGISTER_BLOCKS) + settings.register_offset > 65535:
        raise ValueError("register_offset moves a register beyond Modbus address 65535")
    return settings


def _ipv4_address(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("ip must be a valid IPv4 address")
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as error:
        raise ValueError("ip must be a valid IPv4 address") from error


def _register_offset(value: object) -> int:
    if isinstance(value, list):
        if len(value) != 1:
            raise ValueError("register_offset must contain exactly one value")
        value = value[0]
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError as error:
            raise ValueError(
                "register_offset must be 0 or 32768"
            ) from error
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            "register_offset must be 0 or 32768 (as an integer, numeric string, "
            "or one-item list)"
        )
    if value not in (0, 32768):
        raise ValueError("register_offset must be either 0 or 32768")
    return value


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def sample_readings(
    elapsed_seconds: float,
    total_real_power_w: float = DEFAULT_ACTIVE_POWER_W,
    active_energy_import_wh: float = INITIAL_ENERGY_WH,
) -> dict[str, float]:
    """Calculate interdependent three-phase readings for the selected load."""
    phase = elapsed_seconds * (2.0 * math.pi / 45.0)
    load_ratio = total_real_power_w / MAX_ACTIVE_POWER_W
    voltage = 230.0 - 2.0 * load_ratio + 0.45 * math.sin(phase)
    voltage_phases = (
        voltage,
        voltage * (0.997 + 0.0003 * math.sin(phase)),
        voltage * (1.003 - 0.0003 * math.sin(phase)),
    )
    power_shares = (
        1.0 / 3.0 + 0.003 * math.sin(phase),
        1.0 / 3.0 + 0.003 * math.sin(phase - 2.0 * math.pi / 3.0),
        1.0 / 3.0 + 0.003 * math.sin(phase + 2.0 * math.pi / 3.0),
    )
    phase_powers = tuple(total_real_power_w * share for share in power_shares)
    power_factor = 0.94 - 0.015 * load_ratio
    angle = math.acos(power_factor)
    currents = tuple(
        watts / (volts * power_factor)
        for watts, volts in zip(phase_powers, voltage_phases)
    )
    reactive_power = total_real_power_w * math.tan(angle)
    apparent_power = total_real_power_w / power_factor
    phase_angles = (0.0, -2.0 * math.pi / 3.0, 2.0 * math.pi / 3.0)

    values = {
        "ct_primary": 100.0,
        "ct_secondary": 5.0,
        "voltage_l1_n": voltage_phases[0],
        "voltage_l2_n": voltage_phases[1],
        "voltage_l3_n": voltage_phases[2],
        "current_l1": currents[0],
        "current_l2": currents[1],
        "current_l3": currents[2],
        "active_power_total": total_real_power_w,
        "apparent_power_total": apparent_power,
        "reactive_power_total": reactive_power,
        "frequency": 50.0 + 0.03 * math.sin(phase / 2.0),
        "active_energy_import": active_energy_import_wh,
        "voltage_l1_thd": 1.8 + 0.7 * load_ratio + 0.1 * math.sin(phase),
        "current_l1_thd": 4.0 + 5.0 * load_ratio + 0.4 * math.sin(phase),
        "voltage_l1_crest_factor": 1.40 + 0.02 * load_ratio,
        "voltage_l2_crest_factor": 1.39 + 0.02 * load_ratio,
        "voltage_l3_crest_factor": 1.41 + 0.02 * load_ratio,
        "voltage_zero_sequence": 0.08 + 0.05 * math.sin(phase),
        "voltage_negative_sequence": 0.15 + 0.05 * abs(math.sin(phase)),
        "voltage_positive_sequence": sum(voltage_phases) / 3.0,
        "current_zero_sequence": abs(currents[0] - currents[1]) * 0.08,
        "current_negative_sequence": (max(currents) - min(currents)) * 0.2,
        "current_positive_sequence": sum(currents) / 3.0,
    }
    for index, label in enumerate(("l1", "l2", "l3")):
        voltage_magnitude = voltage_phases[index]
        current_magnitude = currents[index]
        voltage_phase = phase_angles[index]
        current_phase = voltage_phase - angle
        values[f"voltage_{label}_real"] = voltage_magnitude * math.cos(voltage_phase)
        values[f"voltage_{label}_imag"] = voltage_magnitude * math.sin(voltage_phase)
        values[f"current_{label}_real"] = current_magnitude * math.cos(current_phase)
        values[f"current_{label}_imag"] = current_magnitude * math.sin(current_phase)
    return values


class SimulatorState:
    """Thread-safe adjustable load and integrated import-energy state."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started_at = clock()
        self._last_updated = self._started_at
        self._total_real_power_w = DEFAULT_ACTIVE_POWER_W
        self._active_energy_import_wh = INITIAL_ENERGY_WH
        self._lock = threading.Lock()

    def _update_energy(self, now: float) -> None:
        elapsed = max(now - self._last_updated, 0.0)
        self._active_energy_import_wh += self._total_real_power_w * elapsed / 3600.0
        self._last_updated = now

    def set_total_real_power(self, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("total_real_power_w must be a number")
        power = float(value)
        if not math.isfinite(power) or not 0.0 <= power <= MAX_ACTIVE_POWER_W:
            raise ValueError(
                f"total_real_power_w must be between 0 and {MAX_ACTIVE_POWER_W:g} W"
            )
        with self._lock:
            self._update_energy(self._clock())
            self._total_real_power_w = power
        return power

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            now = self._clock()
            self._update_energy(now)
            power = self._total_real_power_w
            energy = self._active_energy_import_wh
        readings = sample_readings(now - self._started_at, power, energy)
        return {
            "total_real_power_w": power,
            "readings": readings,
        }


def make_register_map(
    readings: dict[str, float], register_offset: int = 0
) -> dict[int, int]:
    """Encode guide values into 16-bit Modbus words (big-endian float order)."""
    registers: dict[int, int] = {}
    for address, name in FLOAT_REGISTERS.items():
        encoded = struct.pack(">f", readings[name])
        high_word, low_word = struct.unpack(">HH", encoded)
        registers[address + register_offset] = high_word
        registers[address + register_offset + 1] = low_word
    return registers


def _is_in_register_block(address: int, count: int, offset: int) -> bool:
    end_address = address + count - 1
    return any(
        address >= start + offset and end_address <= end + offset
        for start, end in REGISTER_BLOCKS
    )


def process_pdu(
    pdu: bytes,
    registers: dict[int, int],
    settings: Settings,
) -> bytes:
    """Process one Modbus application PDU and return its response PDU."""
    if not pdu:
        return b""
    function = pdu[0]
    if function not in (3, 4):
        return bytes((function | 0x80, 1))
    if len(pdu) != 5:
        return bytes((function | 0x80, 3))

    address, count = struct.unpack(">HH", pdu[1:])
    if count < 1 or count > MAX_READ_REGISTERS:
        return bytes((function | 0x80, 3))
    if not _is_in_register_block(address, count, settings.register_offset):
        return bytes((function | 0x80, 2))

    payload = b"".join(
        struct.pack(">H", registers.get(register, 0))
        for register in range(address, address + count)
    )
    return bytes((function, len(payload))) + payload


class ModbusTCPHandler(socketserver.BaseRequestHandler):
    """Handle multiple Modbus TCP requests on a persistent client connection."""

    def handle(self) -> None:
        while True:
            header = _recv_exactly(self.request, 7)
            if header is None:
                return
            transaction_id, protocol_id, length, unit_id = struct.unpack(
                ">HHHB", header
            )
            if protocol_id != 0 or length < 2 or length > 254:
                LOGGER.warning(
                    "Ignoring malformed Modbus TCP header from %s",
                    self.client_address,
                )
                return
            pdu = _recv_exactly(self.request, length - 1)
            if pdu is None:
                return
            if unit_id != self.server.settings.unit_id:
                LOGGER.debug(
                    "Ignoring unit ID %s from %s",
                    unit_id,
                    self.client_address,
                )
                continue

            registers = self.server.snapshot()
            response_pdu = process_pdu(pdu, registers, self.server.settings)
            response = struct.pack(
                ">HHHB",
                transaction_id,
                0,
                len(response_pdu) + 1,
                unit_id,
            ) + response_pdu
            self.request.sendall(response)


class ModbusTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], settings: Settings) -> None:
        self.settings = settings
        self._registers: dict[int, int] = {}
        self._register_lock = threading.Lock()
        super().__init__(address, ModbusTCPHandler)

    def snapshot(self) -> dict[int, int]:
        with self._register_lock:
            return self._registers.copy()

    def update(self, registers: dict[int, int]) -> None:
        with self._register_lock:
            self._registers = registers


def _recv_exactly(connection: socket.socket, size: int) -> bytes | None:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


class DashboardHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        simulator_state: SimulatorState,
    ) -> None:
        self.simulator_state = simulator_state
        super().__init__(address, DashboardRequestHandler)


class DashboardRequestHandler(http.server.BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def _authorized(self) -> bool:
        if self.headers.get("X-Remote-User-Id", "").strip():
            return True
        self._send_json(403, {"error": "Open the dashboard through Home Assistant."})
        return False

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._authorized():
            return
        if self.path in ("/", "/index.html"):
            try:
                page = DASHBOARD_PATH.read_bytes()
            except OSError:
                LOGGER.exception("Unable to read the simulator dashboard")
                self._send_json(500, {"error": "Dashboard file is unavailable."})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; connect-src 'self'; "
                "frame-ancestors 'self'",
            )
            self.end_headers()
            self.wfile.write(page)
            return
        if self.path == "/api/status":
            self._send_json(200, self.server.simulator_state.snapshot())
            return
        self._send_json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        if not self._authorized():
            return
        if self.path != "/api/power":
            self._send_json(404, {"error": "Not found."})
            return
        content_length = self.headers.get("Content-Length", "")
        if not content_length.isascii() or not content_length.isdecimal():
            self._send_json(400, {"error": "A valid Content-Length is required."})
            return
        body_size = int(content_length)
        if body_size > 1024:
            self._send_json(413, {"error": "Request body is too large."})
            return
        try:
            payload = json.loads(self.rfile.read(body_size))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "Request body must be valid JSON."})
            return
        if not isinstance(payload, dict) or "total_real_power_w" not in payload:
            self._send_json(
                400,
                {"error": "Provide total_real_power_w in the request body."},
            )
            return
        try:
            power = self.server.simulator_state.set_total_real_power(
                payload["total_real_power_w"]
            )
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(
            200,
            {
                "total_real_power_w": power,
                "readings": self.server.simulator_state.snapshot()["readings"],
            },
        )

    def log_message(self, format_string: str, *args: object) -> None:
        LOGGER.info("%s - %s", self.address_string(), format_string % args)


def run() -> None:
    settings = load_settings()
    simulator_state = SimulatorState()
    server = ModbusTCPServer((settings.ip, settings.tcp_port), settings)
    dashboard_server = DashboardHTTPServer(
        (settings.ip, INGRESS_PORT),
        simulator_state,
    )

    def update_readings() -> None:
        while True:
            readings = simulator_state.snapshot()["readings"]
            server.update(make_register_map(readings, settings.register_offset))
            time.sleep(settings.update_interval / 1000.0)

    dashboard_thread = threading.Thread(
        target=dashboard_server.serve_forever,
        name="dashboard",
        daemon=True,
    )
    dashboard_thread.start()
    updater = threading.Thread(
        target=update_readings,
        name="register-updater",
        daemon=True,
    )
    updater.start()
    LOGGER.info(
        "Serving Modbus TCP on %s:%s (unit ID %s, register offset %s); dashboard on port %s",
        settings.ip,
        settings.tcp_port,
        settings.unit_id,
        settings.register_offset,
        INGRESS_PORT,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Stopping Modbus TCP simulator")
    finally:
        server.shutdown()
        server.server_close()
        dashboard_server.shutdown()
        dashboard_server.server_close()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        run()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        LOGGER.error("Unable to start Janitza Modbus simulator: %s", error)
        raise


if __name__ == "__main__":
    main()
