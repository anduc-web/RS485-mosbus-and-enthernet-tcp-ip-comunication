# -*- coding: utf-8 -*-
"""Pytest cho yêu cầu MES (Modbus master) trên cổng TCP của HMI.

MES khác client SCADA ở 4 điểm:
  1. Cần "contract" máy-đọc-được để import tag (CSV/JSON).
  2. Cần biết dữ liệu có còn "tươi" không (age) + chống đọc trùng (seq).
  3. Modbus TCP không có xác thực → phải chặn IP lạ (whitelist).
  4. Cần nhận diện thiết bị (FC0x2B) để MES quản lý kho thiết bị.

Chạy:  python -m pytest tests -q   (trong thư mục HMI)
"""
import csv
import io
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbus_tcp_gateway import (  # noqa: E402
    ModbusTcpGateway,
    build_tag_contract,
    contract_to_csv,
    encode_input_registers,
)

STATE = {
    "humidity": 42.5, "temp": 28.3,
    "pump": True, "fan": False, "lamp": True,
    "mode": "MANUAL", "estop": False,
    "sensor_ok": True, "connected": True, "cfg_valid": True,
    "uptime_s": 125, "data_age_s": 2,
    "rh_on": 40.0, "rh_off": 45.0,
    "fan_on": 32.0, "fan_off": 30.0,
    "heat_on": 22.0, "heat_off": 25.0,
    "pump_max_s": 600, "heat_max_s": 1800, "failsafe_cycles": 1,
}


# ------------------------------------------------ 1. contract máy-đọc-được
def test_contract_covers_every_published_tag():
    contract = build_tag_contract()
    names = {t["name"] for t in contract}
    assert "humidity_pct" in names
    assert "temp_c" in names
    assert "pump_state" in names
    assert "estop" in names
    assert "rh_on_pct" in names
    assert "mode" in names


def test_contract_declares_mes_poll_metadata():
    contract = build_tag_contract()
    for tag in contract:
        assert tag["device_id"] == 254
        assert tag["area"] in ("input", "holding", "coil")
        assert "address" in tag
        assert "type" in tag
        assert "scale" in tag
        assert "unit" in tag
        assert "writable" in tag


def test_contract_csv_is_importable_by_mes():
    csv_text = contract_to_csv(build_tag_contract())
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) >= 15
    assert {"name", "device_id", "area", "address", "type", "scale", "unit",
            "writable"} <= set(rows[0].keys())
    hum = next(r for r in rows if r["name"] == "humidity_pct")
    assert hum["scale"] == "0.1"
    assert hum["unit"] == "%RH"
    assert hum["writable"] == "false"


def test_contract_json_is_valid():
    data = json.loads(json.dumps(build_tag_contract()))
    assert isinstance(data, list)
    assert data[0]["name"]


# --------------------------------------------- 2. chất lượng dữ liệu cho MES
def test_input_registers_expose_data_age_and_sequence():
    regs = encode_input_registers(dict(STATE, data_age_s=7, seq=1234))
    assert regs[10] == 7        # data_age_s — dữ liệu còn tươi?
    assert regs[11] == 1234     # seq — đếm số lần cập nhật (chống đọc trùng)


def test_data_age_is_clamped_and_non_negative():
    regs = encode_input_registers(dict(STATE, data_age_s=-5))
    assert regs[10] == 0
    regs = encode_input_registers(dict(STATE, data_age_s=99999))
    assert regs[10] == 65535


def test_flags_mark_stale_data_when_sensor_lost():
    regs = encode_input_registers(dict(STATE, sensor_ok=False))
    assert regs[7] & 0x01 == 0          # bit0 sensor_ok tắt
    stale = encode_input_registers(dict(STATE, data_age_s=30))
    assert stale[7] & 0x08 == 0x08      # bit3 = dữ liệu cũ (>10 s)


# ------------------------------------------------------ 3. whitelist IP
def _free_port():
    import socket
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
        allowed_ips=["127.0.0.1"],
    )
    gw.update(STATE)
    assert gw.start(), gw.error
    time.sleep(0.3)
    gw.events = events
    yield gw
    gw.stop()


def test_allowed_ip_can_poll(gateway):
    from pymodbus.client import ModbusTcpClient
    cli = ModbusTcpClient("127.0.0.1", port=gateway.port, timeout=3)
    assert cli.connect()
    try:
        r = cli.read_input_registers(0, count=3, device_id=254)
        assert not r.isError()
        assert r.registers[0] == 425
    finally:
        cli.close()


def test_blocked_ip_is_rejected(gateway):
    """IP không nằm trong whitelist phải bị từ chối."""
    from pymodbus.client import ModbusTcpClient
    gateway.allowed_ips = ["10.99.99.99"]        # giả vờ IP lạ
    cli = ModbusTcpClient("127.0.0.1", port=gateway.port, timeout=2)
    if cli.connect():
        r = cli.read_input_registers(0, count=3, device_id=254)
        assert r.isError(), "IP ngoài whitelist vẫn đọc được dữ liệu"
    cli.close()


def test_command_records_client_ip_for_audit(gateway):
    """Lệnh ghi phải kèm IP người gửi để MES/HMI audit được."""
    from pymodbus.client import ModbusTcpClient
    cli = ModbusTcpClient("127.0.0.1", port=gateway.port, timeout=3)
    assert cli.connect()
    try:
        cli.write_coil(1, True, device_id=254)
        time.sleep(0.3)
        cmds = [e[1] for e in gateway.events if e[0] == "cmd"]
        assert cmds, "không nhận lệnh"
        assert cmds[-1].get("src") == "127.0.0.1"
    finally:
        cli.close()


# ------------------------------------------------------ 4. nhận diện thiết bị
def test_server_exposes_device_identification(gateway):
    """MES đọc FC0x2B để biết đây là thiết bị nào."""
    from pymodbus.client import ModbusTcpClient
    cli = ModbusTcpClient("127.0.0.1", port=gateway.port, timeout=3)
    assert cli.connect()
    try:
        r = cli.read_device_information(device_id=254)
        assert not r.isError(), r
        info = {k: v for k, v in r.information.items() if v}
        assert any("AGRI" in str(v).upper() or "HMI" in str(v).upper()
                   for v in info.values()), info
    finally:
        cli.close()
