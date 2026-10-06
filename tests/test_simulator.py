import http.client
import errno
import json
import math
import socket
import struct
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from addons.janitza_umg96el_simulator.simulator import (
    DashboardHTTPServer,
    FLOAT_REGISTERS,
    ModbusTCPServer,
    SimulatorState,
    Settings,
    _create_servers,
    load_settings,
    make_register_map,
    process_pdu,
    sample_readings,
)


def _recv_exactly(connection: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise AssertionError("Connection closed before the full Modbus response arrived")
        data.extend(chunk)
    return bytes(data)


class RegisterMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            ip="127.0.0.1",
            tcp_port=502,
            unit_id=1,
            register_offset=0,
            update_interval=200,
        )
        self.readings = sample_readings(10.0)
        self.registers = make_register_map(self.readings)

    def test_every_guide_register_is_encoded_as_big_endian_float(self) -> None:
        self.assertEqual(len(FLOAT_REGISTERS), len(self.readings))
        for address, name in FLOAT_REGISTERS.items():
            encoded = struct.pack(
                ">HH",
                self.registers[address],
                self.registers[address + 1],
            )
            self.assertTrue(
                math.isclose(
                    struct.unpack(">f", encoded)[0],
                    self.readings[name],
                    rel_tol=1e-6,
                ),
                name,
            )

    def test_offset_moves_addresses_by_32768(self) -> None:
        shifted = make_register_map(self.readings, 32768)
        self.assertEqual(shifted[19000 + 32768], self.registers[19000])
        self.assertNotIn(19000, shifted)

    def test_fc03_and_fc04_return_same_float_words(self) -> None:
        for function in (3, 4):
            response = process_pdu(
                struct.pack(">BHH", function, 19000, 2),
                self.registers,
                self.settings,
            )
            self.assertEqual(response[0], function)
            self.assertEqual(response[1], 4)
            self.assertTrue(
                math.isclose(
                    struct.unpack(">f", response[2:])[0],
                    self.readings["voltage_l1_n"],
                    rel_tol=1e-6,
                )
            )

    def test_documented_unlisted_words_are_zero(self) -> None:
        response = process_pdu(
            struct.pack(">BHH", 3, 19006, 2),
            self.registers,
            self.settings,
        )
        self.assertEqual(response, b"\x03\x04\x00\x00\x00\x00")

    def test_invalid_function_count_and_address_return_exceptions(self) -> None:
        self.assertEqual(process_pdu(b"\x06\x00\x01\x00\x02", self.registers, self.settings), b"\x86\x01")
        self.assertEqual(process_pdu(b"\x03\x4a\x38\x00\x00", self.registers, self.settings), b"\x83\x03")
        self.assertEqual(process_pdu(b"\x03\x00\x64\x00\x01", self.registers, self.settings), b"\x83\x02")
        self.assertEqual(process_pdu(b"\x03\x00\x01", self.registers, self.settings), b"\x83\x03")

    def test_sample_changes_but_stays_plausible(self) -> None:
        first = sample_readings(0)
        later = sample_readings(10)
        self.assertNotEqual(first["current_l1"], later["current_l1"])
        self.assertTrue(math.isclose(first["frequency"], 50.0))
        self.assertNotEqual(first["voltage_l1_n"], later["voltage_l1_n"])

    def test_power_setpoint_drives_consistent_three_phase_readings(self) -> None:
        elapsed = 12.0
        for power in (0.0, 2780.0, 10000.0):
            readings = sample_readings(elapsed, power)
            phase_real_power = sum(
                readings[f"voltage_{phase}_real"]
                * readings[f"current_{phase}_real"]
                + readings[f"voltage_{phase}_imag"]
                * readings[f"current_{phase}_imag"]
                for phase in ("l1", "l2", "l3")
            )
            self.assertTrue(
                math.isclose(readings["active_power_total"], power, abs_tol=1e-9)
            )
            self.assertTrue(math.isclose(phase_real_power, power, rel_tol=1e-9, abs_tol=1e-9))
            self.assertTrue(
                math.isclose(
                    readings["apparent_power_total"] ** 2,
                    power**2 + readings["reactive_power_total"] ** 2,
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                )
            )
            self.assertGreaterEqual(readings["current_l1"], 0.0)
        self.assertEqual(sample_readings(elapsed, 0)["current_l1"], 0.0)
        self.assertGreater(
            sample_readings(elapsed, 10000)["current_l1"],
            sample_readings(elapsed, 1000)["current_l1"],
        )

    def test_tcp_server_handles_a_complete_modbus_request(self) -> None:
        server = ModbusTCPServer((self.settings.ip, 0), self.settings)
        server.update(self.registers)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.create_connection(server.server_address, timeout=2) as client:
                request = struct.pack(">HHHB", 0x1234, 0, 6, 1) + struct.pack(
                    ">BHH", 3, 19000, 2
                )
                client.sendall(request)
                header = _recv_exactly(client, 7)
                transaction_id, protocol_id, length, unit_id = struct.unpack(
                    ">HHHB", header
                )
                response_pdu = _recv_exactly(client, length - 1)
                self.assertEqual(
                    (transaction_id, protocol_id, unit_id),
                    (0x1234, 0, 1),
                )
                self.assertEqual(response_pdu[0], 3)
                self.assertTrue(
                    math.isclose(
                        struct.unpack(">f", response_pdu[2:])[0],
                        self.readings["voltage_l1_n"],
                        rel_tol=1e-6,
                    )
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class SettingsTests(unittest.TestCase):
    def test_defaults_load_without_options_file(self) -> None:
        settings = load_settings(Path("a-file-that-does-not-exist.json"))
        self.assertEqual(settings, Settings("0.0.0.0", 502, 1, 0, 200))

    def test_loads_home_assistant_single_item_list_option(self) -> None:
        for offset in (0, 32768, "0", "32768", [0], ["0"]):
            with self.subTest(offset=offset), tempfile.TemporaryDirectory() as directory:
                options_path = Path(directory) / "options.json"
                options_path.write_text(
                    json.dumps(
                        {
                            "tcp_port": 502,
                            "ip": "127.0.0.1",
                            "unit_id": 1,
                            "register_offset": offset,
                            "update_interval": 200,
                        }
                    ),
                    encoding="utf-8",
                )
                settings = load_settings(options_path)
            expected = offset[0] if isinstance(offset, list) else offset
            self.assertEqual(settings.register_offset, int(expected))

    def test_loads_scalar_register_offset_for_existing_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            options_path = Path(directory) / "options.json"
            options_path.write_text(
                json.dumps(
                    {
                        "tcp_port": 502,
                        "ip": "127.0.0.1",
                        "unit_id": 1,
                        "register_offset": 0,
                        "update_interval": 200,
                    }
                ),
                encoding="utf-8",
            )
            settings = load_settings(options_path)
        self.assertEqual(settings.register_offset, 0)

    def test_rejects_invalid_register_offset_list(self) -> None:
        for offset in ([], [0, 32768], [1], True, "1", "invalid", None, 1.5):
            with self.subTest(offset=offset), tempfile.TemporaryDirectory() as directory:
                options_path = Path(directory) / "options.json"
                options_path.write_text(
                    json.dumps(
                        {
                            "tcp_port": 502,
                            "ip": "127.0.0.1",
                            "unit_id": 1,
                            "register_offset": offset,
                            "update_interval": 200,
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    load_settings(options_path)

    def test_rejects_invalid_ip_option(self) -> None:
        for address in ("", "localhost", "192.0.2.1/24", "2001:db8::1", 123):
            with self.subTest(address=address), tempfile.TemporaryDirectory() as directory:
                options_path = Path(directory) / "options.json"
                options_path.write_text(
                    json.dumps(
                        {
                            "ip": address,
                            "tcp_port": 502,
                            "unit_id": 1,
                            "register_offset": 0,
                            "update_interval": 200,
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    load_settings(options_path)

    def test_rejects_modbus_port_reserved_for_sidebar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            options_path = Path(directory) / "options.json"
            options_path.write_text(
                json.dumps(
                    {
                        "ip": "127.0.0.1",
                        "tcp_port": 8099,
                        "unit_id": 1,
                        "register_offset": 0,
                        "update_interval": 200,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "reserved for the sidebar"):
                load_settings(options_path)


class SimulatorStateTests(unittest.TestCase):
    def test_setpoint_changes_readings_and_energy_integrates_power(self) -> None:
        now = [1000.0]
        state = SimulatorState(clock=lambda: now[0])
        initial = state.snapshot()
        self.assertEqual(initial["total_real_power_w"], 2780.0)

        now[0] += 3600
        state.set_total_real_power(1000)
        changed = state.snapshot()
        self.assertEqual(changed["readings"]["active_power_total"], 1000.0)
        self.assertGreater(changed["readings"]["current_l1"], 0)
        self.assertAlmostEqual(
            changed["readings"]["active_energy_import"],
            initial["readings"]["active_energy_import"] + 2780,
        )

        now[0] += 1800
        accumulated = state.snapshot()
        self.assertAlmostEqual(
            accumulated["readings"]["active_energy_import"],
            changed["readings"]["active_energy_import"] + 500,
        )

    def test_setpoint_rejects_invalid_power_values(self) -> None:
        state = SimulatorState()
        for value in (-1, 10001, float("nan"), float("inf"), True, "1000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                state.set_total_real_power(value)

    def test_dashboard_requires_ingress_user_and_applies_setpoint(self) -> None:
        state = SimulatorState()
        server = DashboardHTTPServer(("127.0.0.1", 0), state)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            connection.request("GET", "/api/status")
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()

            connection.request(
                "GET",
                "/",
                headers={"X-Remote-User-Id": "test-user"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(
                b"Janitza UMG 96-EL",
                response.read(),
            )

            connection.request(
                "POST",
                "/api/power",
                body=json.dumps({"total_real_power_w": 4200}),
                headers={
                    "Content-Type": "application/json",
                    "X-Remote-User-Id": "test-user",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            result = json.loads(response.read())
            self.assertEqual(result["total_real_power_w"], 4200)
            self.assertEqual(result["readings"]["active_power_total"], 4200)
            registers = make_register_map(result["readings"])
            encoded_power = struct.pack(">HH", registers[19026], registers[19027])
            self.assertEqual(struct.unpack(">f", encoded_power)[0], 4200)

            connection.request(
                "POST",
                "/api/power",
                body=json.dumps({"total_real_power_w": 20000}),
                headers={
                    "Content-Type": "application/json",
                    "X-Remote-User-Id": "test-user",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class ServerStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            ip="127.0.0.1",
            tcp_port=503,
            unit_id=1,
            register_offset=0,
            update_interval=200,
        )

    def test_modbus_port_conflict_reports_the_endpoint(self) -> None:
        with mock.patch(
            "addons.janitza_umg96el_simulator.simulator.ModbusTCPServer",
            side_effect=OSError(errno.EADDRINUSE, "occupied"),
        ):
            with self.assertRaises(OSError) as raised:
                _create_servers(self.settings, SimulatorState())
        self.assertEqual(raised.exception.errno, errno.EADDRINUSE)
        self.assertIn("already in use", str(raised.exception))
        self.assertIn("choose a different tcp_port", str(raised.exception))

    def test_dashboard_port_conflict_releases_modbus_socket(self) -> None:
        created_servers: list[ModbusTCPServer] = []
        real_modbus_server = ModbusTCPServer

        def create_modbus_server(
            address: tuple[str, int],
            settings: Settings,
        ) -> ModbusTCPServer:
            server = real_modbus_server(address, settings)
            created_servers.append(server)
            return server

        with (
            mock.patch(
                "addons.janitza_umg96el_simulator.simulator.ModbusTCPServer",
                side_effect=create_modbus_server,
            ),
            mock.patch(
                "addons.janitza_umg96el_simulator.simulator.DashboardHTTPServer",
                side_effect=OSError(errno.EADDRINUSE, "occupied"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "Dashboard address .* already in use"):
                settings = Settings(
                    ip=self.settings.ip,
                    tcp_port=0,
                    unit_id=self.settings.unit_id,
                    register_offset=self.settings.register_offset,
                    update_interval=self.settings.update_interval,
                )
                _create_servers(settings, SimulatorState())

        self.assertEqual(len(created_servers), 1)
        with socket.socket() as available:
            available.bind(created_servers[0].server_address)


if __name__ == "__main__":
    unittest.main()
