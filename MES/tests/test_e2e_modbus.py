# -*- coding: utf-8 -*-
"""E2E: MES (app Python) ↔ PC1 (Modbus TCP server) — không qua HTTP bridge.

Dựng PC1 giả lập (gateway thật của HMI), cho MES đọc/ghi thật qua
Modbus TCP và kiểm tra lệnh về tới PC1 đúng nội dung.
"""
import os
import socket
import sys
import time

import pytest

MES = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HMI = os.path.join(os.path.dirname(MES), "HMI")
sys.path.insert(0, MES)
sys.path.insert(0, HMI)

pytest.importorskip("PyQt5", reason="chưa cài PyQt5")

from PyQt5.QtWidgets import QApplication                # noqa: E402

PC1_STATE = {
    "humidity": 47.8, "temp": 26.4, "pump": False, "fan": True, "lamp": False,
    "mode": "AUTO", "estop": False, "sensor_ok": True, "connected": True,
    "cfg_valid": True, "uptime_s": 3600, "data_age_s": 1, "seq": 42,
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


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture()
def pc1():
    """PC1 giả lập: gateway Modbus TCP thật của HMI."""
    from modbus_tcp_gateway import ModbusTcpGateway
    commands, configs = [], []
    gw = ModbusTcpGateway(host="127.0.0.1", port=_free_port(), device_id=254,
                          on_command=lambda c: commands.append(c),
                          on_config=lambda c: configs.append(c))
    assert gw.start(), gw.error
    gw.update(dict(PC1_STATE))
    yield gw, commands, configs
    gw.stop()


def test_mes_reads_live_state_from_pc1(app, pc1, tmp_path):
    import mes_app
    gw, commands, configs = pc1
    got = {}
    worker = mes_app.MesWorker("127.0.0.1", gw.port, 254,
                               mes_app.History(str(tmp_path / "h.db")))
    worker.snapshot.connect(lambda d: got.update(d))
    worker.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not got:
            app.processEvents()
            time.sleep(0.05)
        assert got, "MES không đọc được dữ liệu từ PC1"
        s = got["sample"]
        assert s["humidity"] == pytest.approx(47.8)
        assert s["temp"] == pytest.approx(26.4)
        assert s["fan"] is True
        assert s["mode"] == "AUTO"
        assert s["seq"] == 42
    finally:
        worker.stop()
        worker.wait(3000)


def test_mes_control_reaches_pc1(app, pc1, tmp_path):
    """Lệnh bật/tắt tải từ MES phải tới PC1."""
    import mes_app
    gw, commands, configs = pc1
    worker = mes_app.MesWorker("127.0.0.1", gw.port, 254,
                               mes_app.History(str(tmp_path / "h.db")))
    worker.start()
    time.sleep(1.5)
    try:
        worker.write_coil(1, True)          # bật quạt
        deadline = time.time() + 6
        while time.time() < deadline and not commands:
            app.processEvents()
            time.sleep(0.05)
        assert commands, "PC1 không nhận lệnh điều khiển từ MES"
        assert commands[-1].get("fan") is True
        assert commands[-1].get("src") == "127.0.0.1"
    finally:
        worker.stop()
        worker.wait(3000)


def test_mes_threshold_write_reaches_pc1(app, pc1, tmp_path):
    """Ghi ngưỡng từ MES phải tới PC1 (qua FC06)."""
    import mes_app
    gw, commands, configs = pc1
    worker = mes_app.MesWorker("127.0.0.1", gw.port, 254,
                               mes_app.History(str(tmp_path / "h.db")))
    worker.start()
    time.sleep(1.5)
    try:
        worker.write_register(0, 425)        # rh_on = 42.5 %RH
        deadline = time.time() + 6
        while time.time() < deadline and not configs:
            app.processEvents()
            time.sleep(0.05)
        assert configs, "PC1 không nhận lệnh ghi ngưỡng"
        assert configs[-1]["rh_on"] == pytest.approx(42.5)
        assert configs[-1]["rh_off"] == pytest.approx(45.0)
    finally:
        worker.stop()
        worker.wait(3000)
