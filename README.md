# Janitza UMG 96-EL Home Assistant Modbus simulator

This repository provides a read-only Modbus TCP Home Assistant add-on that
simulates the measurement and diagnostic registers listed in
[`Janitza_UMG_96EL_Extended_Modbus_Guide.pdf`](./Janitza_UMG_96EL_Extended_Modbus_Guide.pdf).
It is a data-source simulator for Home Assistant's built-in Modbus integration;
it does not create Home Assistant entities by itself.

## Install the add-on

1. In Home Assistant, open **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Add `https://github.com/warsiengin/janitza_umg96el_simulator`.
3. Install **Janitza UMG 96-EL Modbus Simulator**, then review its options.
4. Configure the listener IP and port, then start the add-on. With the default
   `0.0.0.0`, it listens on all Home Assistant host interfaces. Allow the
   selected TCP port through any applicable firewall when connecting remotely.

Options:

| Option | Default | Meaning |
| --- | ---: | --- |
| `ip` | `0.0.0.0` | IPv4 address to bind. Use `0.0.0.0` to listen on all host interfaces, or enter a specific IPv4 address assigned to the Home Assistant host. |
| `tcp_port` | `502` | Modbus TCP listen port on the Home Assistant host. |
| `unit_id` | `1` | Modbus unit identifier (1–247). |
| `register_offset` | `0` | Register-address offset: enter `0` for the guide's listed addresses, or `32768` for the alternate addressing note in the guide. |
| `update_interval` | `200` | Simulated reading refresh interval in milliseconds (50–60000). |

The simulator supports function codes 03 (read holding registers) and 04 (read
input registers). Both expose the guide's same read-only data map. Values are
IEEE-754 32-bit floats in big-endian word order; each value occupies two
consecutive 16-bit registers. Gaps inside documented register blocks return
zero, while reads outside those blocks return an illegal-address exception.
Writes are not supported. Generated readings are illustrative synthetic values,
not calibrated measurements or a full physical model of the meter.

The add-on uses host networking so the configured IP and port are the actual
host listener address and port; there is no separate container port mapping to
keep in sync. Choose `0.0.0.0` unless you specifically need to bind to one host
interface.

## Connect Home Assistant

Configure the built-in Modbus integration. Set `host` to the configured `ip`
address (or the Home Assistant host's LAN IP when `ip` is `0.0.0.0`) and set
`port` to the configured `tcp_port`, then append sensors as needed:

```yaml
modbus:
  - name: janitza_simulator
    type: tcp
    host: 192.168.1.10
    port: 502
    delay: 1
    timeout: 5
    sensors:
      - name: Janitza voltage L1-N
        slave: 1
        address: 19000
        input_type: holding
        data_type: float
        precision: 1
        unit_of_measurement: V
      - name: Janitza current L1
        slave: 1
        address: 19012
        input_type: holding
        data_type: float
        precision: 2
        unit_of_measurement: A
      - name: Janitza active power
        slave: 1
        address: 19026
        input_type: holding
        data_type: float
        precision: 0
        unit_of_measurement: W
      - name: Janitza frequency
        slave: 1
        address: 19050
        input_type: holding
        data_type: float
        precision: 2
        unit_of_measurement: Hz
```

Restart Home Assistant after editing `configuration.yaml`. For an input-register
read, change `input_type` to `input`. Set `slave` to the add-on's configured
`unit_id`. When `register_offset` is `32768`, add 32768 to each guide address
(for example, address `19000` becomes `51768`).

## Register map

All addresses below are the guide's baseline addresses. For each float, the
following register address contains the low 16-bit word.

| Address | Reading | Unit / notes |
| ---: | --- | --- |
| 10 | Current transformer primary | A |
| 12 | Current transformer secondary | A |
| 802, 804, 806 | Voltage zero-, negative-, positive-sequence components | V |
| 848, 850, 852 | Voltage vector real parts L1, L2, L3 | V |
| 854, 856, 858 | Voltage vector imaginary parts L1, L2, L3 | V |
| 920, 922, 924 | Current zero-, negative-, positive-sequence components | A |
| 926, 928, 930 | Current vector real parts L1, L2, L3 | A |
| 932, 934, 936 | Current vector imaginary parts L1, L2, L3 | A |
| 5910, 5912, 5914 | Voltage crest factor L1, L2, L3 | dimensionless |
| 19000, 19002, 19004 | Voltage L1-N, L2-N, L3-N | V |
| 19012, 19014, 19016 | Current L1, L2, L3 | A |
| 19026 | Sum active power | W |
| 19034 | Sum apparent power | VA |
| 19042 | Fundamental sum reactive power | var |
| 19050 | Measured frequency | Hz |
| 19068 | Real energy consumed | Wh |
| 19110 | Voltage THD, L1 | % |
| 19116 | Current THD, L1 | % |

The guide also describes a calculated voltage-imbalance index without a Modbus
address, and a harmonic block starting at 1000 (float) / 3536 (short). It does
not provide enough per-register harmonic layout or imbalance address detail to
assign those values reliably, so they are not fabricated by this simulator.
Unlisted registers within the listed blocks read as zero.

## Development

The simulator uses only the Python standard library. Run its tests from the
repository root:

```sh
python -m unittest discover -s tests -v
```
