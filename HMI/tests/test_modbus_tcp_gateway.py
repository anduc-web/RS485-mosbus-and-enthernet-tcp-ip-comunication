# -*- coding: utf-8 -*-
"""Pytest cho cổng Modbus TCP (phương án A).

Chạy:  python -m pytest tests -q   (trong thư mục HMI)
"""
import os
import socket
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbus_tcp_gateway import (  # noqa: E402
    ModbusTcpGateway,
    coil_command,
    config_command,
    encode_holding_registers,
    encode_input_registers,
    mode_command,
)

STATE = {
    "humidity": 42.5, "temp": 28.3,
    "pump": True, "fan": False, "lamp": True,
    "mode": "MANUAL", "estop": False,
    "sensor_ok": True, "connected": True, "cfg_valid": True,
    "uptime_s": 125,
    "rh_on": 40.0, "rh_off": 45.0,
    "fan_on": 32.0, "fan_off": 30.0,
    "heat_on": 22.0, "heat_off": 25.0,
    "pump_max_s": 600, "heat_max_s": 1800, "failsafe_cycles": 1,
}


# ----------------------------------------------------------------- đơn vị
def test_input_registers_mirror_live_state():
    regs = encode_input_registers(STATE)
    assert regs[0] == 425          # RH 42.5 %RH  (x10)
    assert regs[1] == 283          # T  28.3 °C   (x10)
    assert regs[2] == 1            # bơm đang bật
    assert regs[3] == 0            # quạt đang tắt
    assert regs[4] == 1            # đèn đang bật
    assert regs[5] == 1            # MANUAL
    assert regs[6] == 0            # không E-STOP
    assert regs[7] & 0x01          # sensor_ok
    assert regs[7] & 0x02          # connected


def test_input_registers_encode_negative_temperature_as_unsigned16():
    regs = encode_input_registers(dict(STATE, temp=-5.5))
    assert regs[1] == (-55 & 0xFFFF)


def test_holding_registers_mirror_config():
    regs = encode_holding_registers(STATE)
    assert regs[0] == 400          # RH_ON 40.0
    assert regs[1] == 450          # RH_OFF 45.0
    assert regs[6] == 600          # watchdog bơm (s)
    assert regs[9] == 1            # MODE lệnh: MANUAL


def test_coil_write_maps_to_control_command():
    assert coil_command(0, True) == {"mode": "MANUAL", "pump": True}
    assert coil_command(1, False) == {"mode": "MANUAL", "fan": False}
    assert coil_command(2, True) == {"mode": "MANUAL", "lamp": True}
    assert coil_command(3, True) == {"estop": True, "mode": "MANUAL"}
    assert coil_command(3, False) == {"estop": False}


def test_unknown_coil_is_ignored():
    assert coil_command(9, True) is None


def test_holding_register_mode_write_maps_to_control_command():
    assert mode_command(0) == {"mode": "AUTO"}
    assert mode_command(1) == {"mode": "MANUAL"}


def test_holding_register_threshold_write_keeps_other_thresholds():
    cmd = config_command(STATE, 0, 425)
    assert cmd["rh_on"] == 42.5
    assert cmd["rh_off"] == 45.0     # giữ nguyên phần còn lại
    assert cmd["fan_on"] == 32.0


# ------------------------------------------------------------- tích hợp
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def gateway():
    events = []
    gw = ModbusTcpGateway(
        host="127.0.0.1", port=_free_port(), device_id=254,
        on_command=lambda c: events.append(("cmd", c)),
        on_config=lambda c: events.append(("cfg", c)),
    )
    gw.update(STATE)
    assert gw.start(), "không khởi động được Modbus TCP server"
    time.sleep(0.3)
    gw.events = events
    yield gw
    gw.stop()


def test_client_reads_live_telemetry(gateway):
    from pymodbus.client import ModbusTcpClient
    cli = ModbusTcpClient("127.0.0.1", port=gateway.port, timeout=3)
    assert cli.connect()
    try:
        r = cli.read_input_registers(0, count=8, device_id=gateway.device_id)
        assert not r.isError()
        assert r.registers[0] == 425
        assert r.registers[2] == 1
        assert r.registers[4] == 1
    finally:
        cli.close()


def test_client_reads_coil_states(gateway):
    from pymodbus.client import ModbusTcpClient
    cli = ModbusTcpClient("127.0.0.1", port=gateway.port, timeout=3)
    assert cli.connect()
    try:
        r = cli.read_coils(0, count=4, device_id=gateway.device_id)
        assert not r.isError()
        assert r.bits[0] is True      # bơm
        assert r.bits[1] is False     # quạt
        assert r.bits[2] is True      # đèn
    finally:
        cli.close()


def test_client_write_coil_emits_control_command(gateway):
    from pymodbus.client import ModbusTcpClient
    cli = ModbusTcpClient("127.0.0.1", port=gateway.port, timeout=3)
    assert cli.connect()
    try:
        r = cli.write_coil(0, False, device_id=gateway.device_id)
        assert not r.isError()
        time.sleep(0.2)
        kinds = [e[0] for e in gateway.events]
        assert "cmd" in kinds
        cmd = gateway.events[[i for i, e in enumerate(gateway.events)
                              if e[0] == "cmd"][0]][1]
        assert cmd["pump"] is False
        assert cmd["mode"] == "MANUAL"
    finally:
        cli.close()


def test_client_write_holding_register_emits_config_command(gateway):
    from pymodbus.client import ModbusTcpClient
    cli = ModbusTcpClient("127.0.0.1", port=gateway.port, timeout=3)
    assert cli.connect()
    try:
        r = cli.write_register(0, 425, device_id=gateway.device_id)
        assert not r.isError()
        time.sleep(0.2)
        cfgs = [e[1] for e in gateway.events if e[0] == "cfg"]
        assert cfgs, "không phát sinh lệnh cấu hình"
        assert cfgs[-1]["rh_on"] == 42.5
        assert cfgs[-1]["rh_off"] == 45.0
    finally:
        cli.close()


def test_gateway_stop_releases_port(gateway):
    from pymodbus.client import ModbusTcpClient
    port = gateway.port
    gateway.stop()
    time.sleep(0.3)
    cli = ModbusTcpClient("127.0.0.1", port=port, timeout=1)
    assert not cli.connect()
    cli.close()


def test_sensor_slave_is_read_only(gateway):
    """Slave 1 (SHTC30 mirror) không được dùng để điều khiển."""
    from pymodbus.client import ModbusTcpClient
    cli = ModbusTcpClient("127.0.0.1", port=gateway.port, timeout=3)
    assert cli.connect()
    try:
        r = cli.write_coil(0, True, device_id=1)
        assert r.isError(), "slave 1 phải từ chối ghi"
        r = cli.write_register(0, 425, device_id=1)
        assert r.isError(), "slave 1 phải từ chối ghi"
        # không phát sinh lệnh điều khiển nào
        assert not [e for e in gateway.events if e[0] == "cmd"]
    finally:
        cli.close()


def test_catch_all_device_id_zero_returns_bridge(gateway):
    """Device id 0 = catch-all → trả về bản thanh ghi của HMI Bridge."""
    from pymodbus.client import ModbusTcpClient
    cli = ModbusTcpClient("127.0.0.1", port=gateway.port, timeout=3)
    assert cli.connect()
    try:
        r = cli.read_input_registers(0, count=3, device_id=0)
        assert not r.isError()
        assert r.registers[0] == 425
    finally:
        cli.close()
