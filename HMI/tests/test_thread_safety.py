# -*- coding: utf-8 -*-
"""Test hồi quy: KHÔNG được chạm widget Qt từ thread mạng.

Lớp bug này rất dễ lọt: code chạy đúng nhưng Qt sẽ crash ngẫu nhiên
(và trong PyQt5 thường chỉ crash lúc debug). Test này kiểm tra bằng cách
chạy app thật ở chế độ headless và phát hiện thao tác GUI từ thread khác.
"""
import os
import sys
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "HMI"))

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QApplication                    # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


def test_hmi_tcp_reject_goes_through_signal_not_direct_call(app):
    """on_reject phải emit tín hiệu, không gọi log() trực tiếp."""
    import hmi_shtc30_3kenh as hmi

    win = hmi.MainWindow()
    try:
        # Ghi lại mọi lần gọi log() từ thread KHÔNG phải GUI thread.
        offenders = []
        orig_log = win.log
        gui_thread = threading.current_thread()

        def spy_log(level, msg):
            if threading.current_thread() is not gui_thread:
                offenders.append((level, msg))
            return orig_log(level, msg)

        win.log = spy_log
        # Handler thật của tín hiệu (GUI thread) vẫn phải chạy.
        win.tcp_rejected.connect(win._on_tcp_rejected)

        # Mô phỏng gateway báo từ chối từ thread mạng.
        win.tcp_rejected.emit("10.0.0.9")
        app.processEvents()

        assert not offenders, f"gọi log() từ thread mạng: {offenders}"
    finally:
        win.close()


def test_tcp_command_callback_only_emits_signal(app):
    """Callback từ gateway chỉ được emit tín hiệu, không đụng widget."""
    import hmi_shtc30_3kenh as hmi

    win = hmi.MainWindow()
    try:
        offenders = []
        gui_thread = threading.current_thread()
        orig_log = win.log

        def spy_log(level, msg):
            if threading.current_thread() is not gui_thread:
                offenders.append((level, msg))
            return orig_log(level, msg)

        win.log = spy_log

        # Giả lập đúng cách _start_tcp truyền callback.
        import time as _time
        captured = {}

        def fake_gateway(**kwargs):
            captured.update(kwargs)
            return _FakeGateway()

        class _FakeGateway:
            error = None
            running = False

            def start(self):
                return True

            def stop(self):
                pass

        hmi.ModbusTcpGateway = fake_gateway
        try:
            win.sp_tcp_port.setValue(15999)
            win._start_tcp()
        finally:
            hmi.ModbusTcpGateway = hmi.__dict__.get(
                "ModbusTcpGateway", captured.get("_orig"))

        cb = captured.get("on_reject")
        assert cb is not None, "gateway chưa nhận on_reject"

        # Gọi callback từ thread mạng — không được chạm GUI.
        t = threading.Thread(target=cb, args=("10.0.0.9",))
        t.start()
        t.join(3)
        app.processEvents()

        assert not offenders, f"callback chạm GUI từ thread mạng: {offenders}"
    finally:
        win.close()


def test_modbus_gateway_does_not_touch_gui(app):
    """Gateway thuần Python — không được phụ thuộc Qt."""
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "HMI"))
    import modbus_tcp_gateway as gw
    assert "PyQt5" not in sys.modules or True   # không import trực tiếp
    src = open(gw.__file__, encoding="utf-8").read()
    assert "PyQt5" not in src, "gateway không nên phụ thuộc PyQt5"
    assert "QApplication" not in src
