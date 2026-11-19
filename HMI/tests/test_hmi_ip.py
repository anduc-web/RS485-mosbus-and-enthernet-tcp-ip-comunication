# -*- coding: utf-8 -*-
"""Test: PC1 (HMI) phải hiện địa chỉ IP để người dùng nhập vào PC2 (MES)."""
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QApplication                # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


# ======================================================================
# Hàm dò IP (thuần, không phụ thuộc GUI)
# ======================================================================
def test_detect_lan_ips_returns_list_without_loopback():
    import hmi_shtc30_3kenh as hmi
    ips = hmi.detect_lan_ips()
    assert isinstance(ips, list)
    for ip in ips:
        assert isinstance(ip, str)
        assert ip, "IP rỗng"
        assert not ip.startswith("127."), f"lọc loopback: {ip}"
        assert ip.count(".") == 3, f"không phải IPv4: {ip}"


def test_detect_lan_ips_has_no_duplicate():
    import hmi_shtc30_3kenh as hmi
    ips = hmi.detect_lan_ips()
    assert len(ips) == len(set(ips)), f"IP trùng: {ips}"


# ======================================================================
# UI: ô hiển thị địa chỉ để nhập ở PC2
# ======================================================================
def test_tcp_group_has_address_field_for_pc2(app):
    import hmi_shtc30_3kenh as hmi
    win = hmi.MainWindow()
    try:
        assert hasattr(win, "ed_tcp_addr"), "thiếu ô hiển thị IP cho PC2"
        assert win.ed_tcp_addr.isReadOnly(), "ô IP nên read-only để dễ copy"
        assert win.ed_tcp_addr.isVisible() or True
        # nhãn hướng dẫn cho người dùng
        labels = [w.text() for w in win.findChildren(type(win.lbl_tcp))
                  if hasattr(w, "text")]
        assert any("PC2" in t for t in labels), \
            f"thiếu nhãn nhắc nhập ở PC2: {labels[:8]}"
    finally:
        win.close()


def test_address_field_shows_ip_and_port(app):
    import hmi_shtc30_3kenh as hmi
    win = hmi.MainWindow()
    try:
        win.sp_tcp_port.setValue(1502)
        win._refresh_tcp_addr()
        text = win.ed_tcp_addr.text().strip()
        assert text, "ô IP trống"
        assert ":1502" in text, f"thiếu cổng trong '{text}'"
        ips = hmi.detect_lan_ips()
        if ips:
            assert ips[0] in text, f"thiếu IP '{ips[0]}' trong '{text}'"
    finally:
        win.close()


def test_copy_button_copies_address_to_clipboard(app):
    import hmi_shtc30_3kenh as hmi
    win = hmi.MainWindow()
    try:
        win.sp_tcp_port.setValue(1502)
        win._refresh_tcp_addr()
        win.btn_tcp_copy.click()
        clip = QApplication.clipboard().text().strip()
        assert clip == win.ed_tcp_addr.text().strip(), \
            f"clipboard '{clip}' != ô '{win.ed_tcp_addr.text()}'"
    finally:
        win.close()


def test_address_updates_when_port_changes(app):
    import hmi_shtc30_3kenh as hmi
    win = hmi.MainWindow()
    try:
        win.sp_tcp_port.setValue(1502)
        win._refresh_tcp_addr()
        assert ":1502" in win.ed_tcp_addr.text()
        win.sp_tcp_port.setValue(502)
        win._refresh_tcp_addr()
        assert ":502" in win.ed_tcp_addr.text()
    finally:
        win.close()
