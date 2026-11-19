# -*- coding: utf-8 -*-
"""Test: phát hiện xung đột cổng (2 tiến trình cùng bind 1 cổng).

Windows dùng SO_REUSEADDR → socket thứ 2 bind "thành công" nhưng không bao
giờ nhận kết nối. Nếu không phát hiện, MES sẽ đọc nhầm server (rất nguy
hiểm: nhận dữ liệu demo thay vì dữ liệu thật).
"""
import os
import socket
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbus_tcp_gateway import ModbusTcpGateway  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_second_gateway_on_same_port_fails_loudly():
    """Gateway thứ 2 trên cùng cổng phải báo lỗi, không im lặng."""
    port = _free_port()
    gw1 = ModbusTcpGateway(host="0.0.0.0", port=port, device_id=254)
    assert gw1.start(), gw1.error
    try:
        gw2 = ModbusTcpGateway(host="0.0.0.0", port=port, device_id=254)
        ok = gw2.start()
        assert not ok, "phải từ chối khi cổng đã bị chiếm"
        assert gw2.error and "cổng" in gw2.error.lower(), gw2.error
    finally:
        gw1.stop()


def test_port_conflict_error_names_the_offending_port():
    port = _free_port()
    gw1 = ModbusTcpGateway(host="0.0.0.0", port=port, device_id=254)
    assert gw1.start(), gw1.error
    try:
        gw2 = ModbusTcpGateway(host="0.0.0.0", port=port, device_id=254)
        gw2.start()
        assert str(port) in (gw2.error or ""), gw2.error
    finally:
        gw1.stop()


def test_gateway_reports_which_port_it_listens_on():
    port = _free_port()
    gw = ModbusTcpGateway(host="127.0.0.1", port=port, device_id=254)
    assert gw.start(), gw.error
    try:
        assert gw.port == port
    finally:
        gw.stop()


def test_free_port_is_reusable_after_stop():
    """Sau khi stop, cổng phải giải phóng để mở lại được."""
    port = _free_port()
    gw = ModbusTcpGateway(host="0.0.0.0", port=port, device_id=254)
    assert gw.start(), gw.error
    gw.stop()
    time.sleep(0.3)
    gw2 = ModbusTcpGateway(host="0.0.0.0", port=port, device_id=254)
    assert gw2.start(), gw2.error
    gw2.stop()
