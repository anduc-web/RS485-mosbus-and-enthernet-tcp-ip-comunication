# -*- coding: utf-8 -*-
"""Test hồi quy cho các bug đã tìm thấy khi audit gateway Modbus TCP."""
import os
import socket
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbus_tcp_gateway import ModbusTcpGateway  # noqa: E402

STATE = {
    "humidity": 42.5, "temp": 28.3, "pump": False, "fan": True, "lamp": False,
    "mode": "AUTO", "estop": False, "sensor_ok": True, "connected": True,
    "cfg_valid": True, "uptime_s": 10, "data_age_s": 1, "seq": 1,
    "rh_on": 40.0, "rh_off": 45.0, "fan_on": 32.0, "fan_off": 30.0,
    "heat_on": 22.0, "heat_off": 25.0, "pump_max_s": 600,
    "heat_max_s": 1800, "failsafe_cycles": 1,
}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ======================================================================
# BUG 1: FC16 (ghi nhiều thanh ghi) làm ghi đè ngược các ngưỡng đã ghi
# ======================================================================
def test_fc16_write_multiple_registers_does_not_clobber():
    """Ghi 2 ngưỡng trong 1 request FC16 → cả hai phải được áp dụng."""
    from pymodbus.client import ModbusTcpClient

    configs = []
    gw = ModbusTcpGateway(host="127.0.0.1", port=_free_port(), device_id=254,
                          on_config=lambda c: configs.append(c))
    gw.update(STATE)
    assert gw.start(), gw.error
    time.sleep(0.3)
    try:
        cli = ModbusTcpClient("127.0.0.1", port=gw.port, timeout=3)
        assert cli.connect()
        # FC16: rh_on = 50.0 (500), rh_off = 55.0 (550)
        r = cli.write_registers(0, [500, 550], device_id=254)
        assert not r.isError(), r
        cli.close()
        time.sleep(0.4)
        assert configs, "HMI không nhận cấu hình nào"
        last = configs[-1]
        assert last["rh_on"] == 50.0, f"rh_on bị ghi đè ngược: {last['rh_on']}"
        assert last["rh_off"] == 55.0, f"rh_off sai: {last['rh_off']}"
    finally:
        gw.stop()


def test_fc16_sequence_of_three_registers():
    """3 ngưỡng liên tiếp trong 1 FC16 → giữ đủ cả 3 giá trị mới."""
    from pymodbus.client import ModbusTcpClient

    configs = []
    gw = ModbusTcpGateway(host="127.0.0.1", port=_free_port(), device_id=254,
                          on_config=lambda c: configs.append(c))
    gw.update(STATE)
    assert gw.start(), gw.error
    time.sleep(0.3)
    try:
        cli = ModbusTcpClient("127.0.0.1", port=gw.port, timeout=3)
        assert cli.connect()
        r = cli.write_registers(0, [500, 550, 340], device_id=254)
        assert not r.isError(), r
        cli.close()
        time.sleep(0.4)
        last = configs[-1]
        assert last["rh_on"] == 50.0
        assert last["rh_off"] == 55.0
        assert last["fan_on"] == 34.0
    finally:
        gw.stop()


# ======================================================================
# BUG 2: device_id 0 hoặc 1 làm server không khởi động được
# ======================================================================
@pytest.mark.parametrize("device_id", [0, 1])
def test_gateway_starts_with_device_id_0_and_1(device_id):
    gw = ModbusTcpGateway(host="127.0.0.1", port=_free_port(),
                          device_id=device_id)
    gw.update(STATE)
    try:
        assert gw.start(), f"device_id={device_id} không khởi động: {gw.error}"
    finally:
        gw.stop()


# ======================================================================
# BUG 3: lỗi trong callback bị nuốt → MES tưởng lệnh thành công
# ======================================================================
def test_callback_error_is_reported_to_client():
    """Nếu HMI xử lý lệnh lỗi, MES phải nhận exception (không phải OK)."""
    from pymodbus.client import ModbusTcpClient

    def boom(_cmd):
        raise RuntimeError("HMI từ chối vì lý do an toàn")

    gw = ModbusTcpGateway(host="127.0.0.1", port=_free_port(), device_id=254,
                          on_command=boom)
    gw.update(STATE)
    assert gw.start(), gw.error
    time.sleep(0.3)
    try:
        cli = ModbusTcpClient("127.0.0.1", port=gw.port, timeout=3)
        assert cli.connect()
        r = cli.write_coil(0, True, device_id=254)
        cli.close()
        assert r.isError(), "MES nhận 'thành công' dù HMI xử lý lỗi"
    finally:
        gw.stop()
