import math
import socket
import struct
import threading
import unittest
from pathlib import Path

from addons.janitza_umg96el_simulator.simulator import (
    FLOAT_REGISTERS,
    ModbusTCPServer,
    Settings,
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
        self.assertGreater(later["active_energy_import"], first["active_energy_import"])

    def test_tcp_server_handles_a_complete_modbus_request(self) -> None:
        server = ModbusTCPServer(("127.0.0.1", 0), self.settings)
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
        self.assertEqual(settings, Settings(502, 1, 0, 200))


if __name__ == "__main__":
    unittest.main()
