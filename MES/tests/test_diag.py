# -*- coding: utf-8 -*-
"""Test chẩn đoán kết nối MES ↔ PC1 (không cần PyQt)."""
import os
import socket
import sys
import time

import pytest

MES = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HMI = os.path.join(os.path.dirname(MES), "HMI")
sys.path.insert(0, MES)
sys.path.insert(0, HMI)

import mes_diag                                          # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ======================================================================
# tcp_probe
# ======================================================================
def test_tcp_probe_ok_on_open_port():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        ok, code, msg = mes_diag.tcp_probe("127.0.0.1", port, timeout=2)
    finally:
        srv.close()
    assert ok and code == "ok"
    assert str(port) in msg


def test_tcp_probe_refused_on_closed_port():
    ok, code, msg = mes_diag.tcp_probe("127.0.0.1", _free_port(), timeout=2)
    assert not ok
    # Windows thường DROP gói (timeout) thay vì RST (refused) — chấp nhận cả 2
    assert code in ("refused", "timeout")
    advice = mes_diag.TCP_ADVICE[code]
    assert "BẬT CỔNG TCP" in advice, "lời khuyên phải chỉ cách bật cổng TCP"
    if code == "timeout":
        assert "firewall" in advice.lower()


def test_tcp_probe_invalid_host():
    ok, code, _ = mes_diag.tcp_probe("", 502)
    assert not ok and code == "invalid"


def test_tcp_probe_invalid_port():
    ok, code, _ = mes_diag.tcp_probe("127.0.0.1", "abc")
    assert not ok and code == "invalid"


# ======================================================================
# explain_exception
# ======================================================================
def test_exception_11_explains_whitelist():
    why, fix = mes_diag.explain_exception(11)
    assert "TỪ CHỐI IP" in why
    assert "IP cho phép" in fix


def test_modbus_fault_reason_is_actionable():
    fault = mes_diag.ModbusFault(4, 11)
    assert fault.func_code == 4 and fault.code == 11
    assert "IP cho phép" in fault.reason


# ======================================================================
# modbus_probe với PC1 giả lập (gateway thật của HMI)
# ======================================================================
@pytest.fixture()
def gateway():
    from modbus_tcp_gateway import ModbusTcpGateway
    gw = ModbusTcpGateway(host="127.0.0.1", port=_free_port(), device_id=254)
    assert gw.start(), gw.error
    gw.update({"humidity": 47.8, "temp": 26.4, "pump": True, "sensor_ok": True,
               "connected": True, "cfg_valid": True, "seq": 42,
               "rh_on": 40.0, "rh_off": 45.0, "data_age_s": 1, "uptime_s": 100})
    yield gw
    gw.stop()


def test_modbus_probe_reads_real_gateway(gateway):
    ok, code, msg, data = mes_diag.modbus_probe("127.0.0.1", gateway.port, 254)
    assert ok and code == "ok", msg
    assert data["input"][0] == 478          # RH 47.8 %RH
    assert data["input"][1] == 264          # T 26.4 °C
    assert data["holding"][0] == 400        # ngưỡng RH bật 40.0
    assert data["coils"][0] is True         # bơm đang chạy


def test_modbus_probe_reports_whitelist_rejection():
    from modbus_tcp_gateway import ModbusTcpGateway
    gw = ModbusTcpGateway(host="127.0.0.1", port=_free_port(), device_id=254,
                          allowed_ips=["10.99.99.99"])
    assert gw.start(), gw.error
    try:
        ok, code, msg, _ = mes_diag.modbus_probe("127.0.0.1", gw.port, 254)
    finally:
        gw.stop()
    assert not ok
    assert code == "exception:11"
    assert "IP cho phép" in msg


# ======================================================================
# diagnose
# ======================================================================
def test_diagnose_reports_ok_on_healthy_gateway(gateway):
    steps = mes_diag.diagnose("127.0.0.1", gateway.port, 254)
    assert steps and steps[0][0] == "OK"
    assert any("Dữ liệu" in t for _, t in steps)


def test_diagnose_gives_advice_when_port_closed():
    steps = mes_diag.diagnose("127.0.0.1", _free_port(), 254)
    assert steps[0][0] == "ERROR"
    assert any(lv == "WARN" and "Cách khắc phục" in t for lv, t in steps)


def test_diagnose_warns_when_sensor_missing():
    from modbus_tcp_gateway import ModbusTcpGateway
    gw = ModbusTcpGateway(host="127.0.0.1", port=_free_port(), device_id=254)
    assert gw.start(), gw.error
    gw.update({"humidity": 0, "temp": 0, "sensor_ok": False, "connected": False,
               "data_age_s": 65535, "seq": 0})
    try:
        steps = mes_diag.diagnose("127.0.0.1", gw.port, 254)
    finally:
        gw.stop()
    assert any("KHÔNG đọc được cảm biến" in t for _, t in steps)
    assert any("chưa từng nhận được mẫu" in t for _, t in steps)
