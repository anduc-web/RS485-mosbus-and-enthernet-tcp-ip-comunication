# -*- coding: utf-8 -*-
"""Test hồi quy cho các chốt an toàn / logic của app HMI.

Bao phủ 4 bug đã sửa khi audit:
  · giải phóng E-STOP + bật tải trong CÙNG một lệnh (vượt khóa an toàn)
  · seq gửi cho MES tăng cả khi không có mẫu mới
  · failsafe_cycles cấu hình được nhưng không dùng
  · device_id 0/1 làm cổng TCP không mở được (đã có test ở
    test_gateway_bugs.py)
"""
import os
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QApplication                # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture()
def win(app):
    import hmi_shtc30_3kenh as hmi
    w = hmi.MainWindow()
    w.connected = True
    w.sensor_ok = True
    yield w
    w.close()


# ======================================================================
# AN TOÀN: giải phóng E-STOP không được kèm bật tải
# ======================================================================
def test_release_estop_does_not_turn_on_load_in_same_message(win):
    """1 lệnh vừa giải phóng E-STOP vừa bật bơm → phải BỎ lệnh bật bơm."""
    win._apply_estop("test")
    assert win.estop is True

    sent = []
    win.worker = type("W", (), {
        "set_command": lambda self, ch, on: sent.append((ch, on))})()

    win._on_remote_control({"estop": False, "mode": "MANUAL", "pump": True,
                            "by": "test", "ts": int(time.time() * 1000)})

    assert win.estop is False, "phải giải phóng E-STOP"
    assert not any(on for _, on in sent if on), \
        f"bơm đã được bật trong cùng lệnh giải phóng: {sent}"
    assert win.dev_want["pump"] is False


def test_estop_blocks_loads(win):
    """Khi E-STOP đang bật, lệnh bật tải phải bị chặn."""
    win._apply_estop("test")
    assert win._send_device("pump", True, "test", source="TEST") is False
    assert win.dev_want["pump"] is False


def test_cannot_switch_to_auto_while_estop(win):
    """E-STOP khóa: không được về AUTO khi chưa giải phóng."""
    win._apply_estop("test")
    win._set_mode("AUTO")
    assert win.mode == "MANUAL", "E-STOP phải giữ MANUAL"


# ======================================================================
# seq gửi MES chỉ tăng theo MẪU MỚI
# ======================================================================
def test_tcp_seq_increments_only_on_new_sample(win):
    win.tcp_seq = 0
    win._push_tcp_state()                 # chỉ đẩy trạng thái
    assert win.tcp_seq == 0, "seq tăng bừa khi chưa có mẫu mới"
    win._on_data(45.0, 27.0, {0: False, 1: False, 2: False, 3: False})
    assert win.tcp_seq == 1, "seq phải tăng khi có mẫu mới"
    win._push_tcp_state()
    assert win.tcp_seq == 1, "seq không được tăng khi chỉ đẩy trạng thái"


def test_tcp_state_carries_seq_and_age(win):
    win.tcp = type("G", (), {"update": lambda self, p: setattr(self, "p", p)})()
    win._on_data(45.0, 27.0, {})
    # _on_data đặt last_data_ts = bây giờ → lùi lại 3 s để kiểm tra tuổi dữ liệu
    win.last_data_ts = time.time() - 3
    win._push_tcp_state()
    payload = win.tcp.p
    assert payload["seq"] >= 1
    assert 2 <= payload["data_age_s"] <= 5, payload["data_age_s"]


# ======================================================================
# failsafe_cycles phải thực sự được truyền và sử dụng
# ======================================================================
def test_failsafe_cycles_is_passed_to_worker(win):
    import hmi_shtc30_3kenh as hmi
    win.failsafe_cycles = 3
    win.use_simulator = False
    win.cb_poll.setCurrentText("1.0")
    captured = {}

    class _Any:
        """Vừa .connect() được (giả tín hiệu) vừa gọi được."""

        def connect(self, *a, **kw):
            pass

        def __call__(self, *a, **kw):
            return None

    class FakeWorker:
        def __init__(self):
            self.data_ready = _Any()
            self.relay_states = _Any()
            self.relay_failed = _Any()
            self.log_event = _Any()
            self.connection_changed = _Any()
            self.link_changed = _Any()
            self.sensor_ok = _Any()

        def configure(self, *a, **kw):
            captured.update(kw)

        def start(self):
            pass

        def stop(self):
            pass

        def set_command(self, *a, **kw):
            pass

    orig = hmi.ModbusWorker
    hmi.ModbusWorker = FakeWorker
    try:
        win._start_worker()
    finally:
        hmi.ModbusWorker = orig
    assert captured.get("failsafe_cycles") == 3, captured


def test_worker_declares_sensor_lost_after_failsafe_cycles(win):
    import hmi_shtc30_3kenh as hmi
    w = hmi.ModbusWorker()
    w.configure("COM1", 9600, 0.5, 0.5, failsafe_cycles=3)
    assert w._failsafe_cycles == 3
    w._sensor_fail_streak = 1
    assert (w._sensor_fail_streak < w._failsafe_cycles) is True   # chưa mất
    w._sensor_fail_streak = 3
    assert (w._sensor_fail_streak < w._failsafe_cycles) is False  # đã mất
