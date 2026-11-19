# -*- coding: utf-8 -*-
"""Test app MES (PyQt5) — giao diện, đọc/ghi Modbus TCP, lịch sử.

Chạy:  python -m pytest tests -q   (trong thư mục MES)

Fixture `app` / `win` nằm trong tests/conftest.py (dùng chung).
"""
import os
import sys
import time

import pytest

MES = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, MES)
sys.path.insert(0, os.path.join(os.path.dirname(MES), "HMI"))

pytest.importorskip("PyQt5", reason="chưa cài PyQt5")


# ======================================================================
# GIAO DIỆN: 4 trang, sidebar, topbar
# ======================================================================
def test_window_has_four_pages(win):
    assert win.stack.count() == 4, "MES phải có 4 trang"
    assert [t for t, _ in win.PAGES] == [
        "Tổng quan", "Lịch sử cảnh báo", "Quản lý bảo trì",
        "Công thức vận hành"]


def test_sidebar_has_navigation_buttons(win):
    assert len(win._nav) == 4
    labels = [b.text().lower() for b in win._nav]
    assert any("tổng quan" in t for t in labels)
    assert any("lịch sử cảnh báo" in t for t in labels)
    assert any("bảo trì" in t for t in labels)
    assert any("công thức" in t for t in labels)


def test_switching_page_updates_title(win):
    for i in range(4):
        win._show(i)
        assert win.stack.currentIndex() == i
        assert win.lbl_title.text() == win.PAGES[i][0]


def test_topbar_has_host_input_and_connect_button(win):
    assert win.ed_host is not None
    assert ":" in win.ed_host.text(), "ô địa chỉ phải có dạng IP:cổng"
    assert win.btn_conn.text() == "Kết nối"


def test_status_pills_exist(win):
    assert win.lbl_conn.text().startswith("●")
    assert win.lbl_quality is not None
    assert win.lbl_clock is not None


# ======================================================================
# ĐỌC DỮ LIỆU: dữ liệu giả hiển thị đúng
# ======================================================================
def _fake_snapshot(seq=7, age=2, stale=False, rh=45.5, temp=27.25):
    return {
        "sample": {
            "ts": int(time.time()), "humidity": rh, "temp": temp,
            "pump": True, "fan": False, "lamp": True, "mode": "MANUAL",
            "estop": False, "sensor_ok": True, "connected": True,
            "cfg_valid": True, "stale": stale, "data_age_s": age, "seq": seq,
            "uptime_s": 3661, "rh_on": 40.0, "rh_off": 45.0,
            "fan_on": 32.0, "fan_off": 30.0, "heat_on": 22.0,
            "heat_off": 25.0, "pump_max_s": 600, "heat_max_s": 1800,
            "failsafe_cycles": 1,
        },
        "series": [{"ts": int(time.time()) - 60, "temp": 27.0, "rh": 45.0},
                   {"ts": int(time.time()), "temp": 27.5, "rh": 45.5}],
        "ratio": {"run_pct": 62, "run_s": 620, "idle_s": 380, "samples": 10},
        "overheat": [],
        "summary": {"samples": 10, "temp_avg": 27.0, "temp_max": 28.0,
                    "rh_avg": 45.0, "rh_max": 46.0, "run_pct": 62},
        "events": [{"kind": "sensor_lost", "ts": int(time.time()),
                    "level": "WARN", "msg": "Mất dữ liệu cảm biến SHTC30"}],
        "overheat_c": 35.0,
    }


def test_snapshot_updates_values(win):
    win._on_snapshot(_fake_snapshot())
    assert "45.5" in win.stat["rh"].text()
    assert "27.2" in win.stat["temp"].text() or "27.3" in win.stat["temp"].text()
    assert win.stat["pump"].text() == "ON"
    assert win.stat["mode"].text() == "MANUAL"
    assert "62%" in win.lbl_donut.text()
    assert "tươi" in win.lbl_quality.text()


def test_stale_data_is_flagged(win):
    win._on_snapshot(_fake_snapshot(age=30, stale=True))
    assert "CŨ" in win.lbl_quality.text()
    assert "CŨ" in win.mt["age"].text()


def test_overheat_count_shown(win):
    data = _fake_snapshot()
    data["overheat"] = [{"start_ts": int(time.time()) - 60,
                         "end_ts": int(time.time()), "peak_temp": 41.5,
                         "duration_s": 60}]
    win._on_snapshot(data)
    assert win.lbl_oh.text() == "1"
    assert "41.5" in win.lbl_oh_sub.text()


def test_events_table_filled(win):
    win._on_snapshot(_fake_snapshot())
    assert win.tbl_events.rowCount() == 1
    assert "sensor_lost" in (win.tbl_events.item(0, 1).text() or "")


def test_formula_fields_filled_from_snapshot(win):
    win._on_snapshot(_fake_snapshot())
    assert win.sp_formula["rh_on_pct"].value() == 40.0
    assert win.sp_formula["fan_on_c"].value() == 32.0


# ======================================================================
# GHI: điều khiển tải + ghi ngưỡng (qua hàng đợi lệnh của worker)
# ======================================================================
class _FakeWorker:
    def __init__(self):
        self.coils = []
        self.regs = []
        self.overheat = None

    def write_coil(self, coil, on):
        self.coils.append((coil, on))

    def write_register(self, address, value):
        self.regs.append((address, value))

    def set_overheat_c(self, value):
        self.overheat = value


def test_coil_command_goes_to_worker(win):
    fake = _FakeWorker()
    win.worker = fake
    win._send_coil(1, True)
    win._send_coil(3, False)
    assert fake.coils == [(1, True), (3, False)]


def test_formula_write_uses_valid_order(win):
    """Ghi ngưỡng bật/tắt: phải ghi ngưỡng 'tắt' trước để không vi phạm."""
    fake = _FakeWorker()
    win.worker = fake
    win.sp_formula["rh_on_pct"].setValue(50.0)
    win.sp_formula["rh_off_pct"].setValue(55.0)
    win._save_formula()
    assert fake.regs, "phải gửi lệnh ghi xuống PC1"
    addrs = [a for a, _ in fake.regs]
    assert addrs[0] == 1, "ghi rh_off (địa chỉ 1) trước"
    assert addrs[1] == 0, "sau đó ghi rh_on (địa chỉ 0)"
    # giá trị gửi đi là ×10
    assert fake.regs[0] == (1, 550)
    assert fake.regs[1] == (0, 500)


def test_formula_write_rejects_out_of_range(win):
    """Giá trị ngoài khoảng hợp lệ phải bị chặn, không gửi xuống PC1."""
    fake = _FakeWorker()
    win.worker = fake
    sp = win.sp_formula["rh_on_pct"]
    sp.setMaximum(999)                  # giả lập nhập tay giá trị sai
    sp.setValue(120.0)                  # > 100 %RH
    win._save_formula()
    assert not fake.regs, "không được ghi giá trị vô lý xuống PC1"


def test_overheat_threshold_saved_to_worker(win):
    fake = _FakeWorker()
    win.worker = fake
    win.sp_oh.setValue(30.0)
    win._save_overheat()
    assert fake.overheat == 30.0


def test_export_tags_creates_files(win, tmp_path):
    win._export_tags()
    base = os.path.join(MES, "mes_tag_contract")
    assert os.path.exists(base + ".csv")
    assert os.path.exists(base + ".json")


# ======================================================================
# HỒI QUY: trạng thái link MES↔PC1 KHÔNG được suy ra từ cờ cảm biến
# ======================================================================
def test_sensor_flag_false_does_not_show_pc1_offline(win):
    """Cờ 'connected' của PC1 = cảm biến RS485 còn sống, KHÔNG phải link TCP.

    Hồi quy: MES đọc được dữ liệu qua Modbus TCP thì phải hiện "online",
    dù cảm biến RS485 đã mất. Trước đây UI hiện "PC1 offline" → người
    dùng tưởng MES không kết nối được HMI.
    """
    win._on_link(True, "")
    data = _fake_snapshot()
    data["sample"]["connected"] = False        # cảm biến RS485 mất
    data["sample"]["sensor_ok"] = False
    win._on_snapshot(data)
    txt = win.lbl_conn.text().lower()
    assert "offline" not in txt, f"không được báo offline: {win.lbl_conn.text()}"
    assert "online" in txt


def test_link_down_shows_actionable_reason(win):
    win._on_link(False, "PC1 TỪ CHỐI IP — thêm IP PC2 vào ô 'IP cho phép'")
    assert "offline" in win.lbl_conn.text().lower()
    assert "IP cho phép" in win.lbl_msg.text(), "phải hiện lời khuyên cụ thể"


def test_quality_shows_sensor_state_and_no_data(win):
    data = _fake_snapshot(age=65535, stale=True)
    data["sample"]["sensor_ok"] = False
    win._on_snapshot(data)
    q = win.lbl_quality.text()
    assert "MẤT" in q, f"phải báo cảm biến mất: {q}"
    assert "chưa có dữ liệu" in q, f"age=65535 phải hiện chưa có dữ liệu: {q}"


def test_diagnose_button_exists(win):
    assert hasattr(win, "btn_diag"), "cần nút chẩn đoán kết nối"
    assert "chẩn đoán" in win.btn_diag.text().lower()


def test_worker_emits_link_down_reason(app, tmp_path):
    """Worker phải báo lý do cụ thể khi không kết nối được (không chỉ 'lỗi')."""
    import socket
    import mes_app
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed_port = s.getsockname()[1]
    s.close()

    msgs = []
    worker = mes_app.MesWorker("127.0.0.1", closed_port, 254,
                                mes_app.History(str(tmp_path / "h.db")))
    worker.link.connect(lambda up, m: msgs.append((up, m)))
    worker.start()
    try:
        deadline = time.time() + 8
        while time.time() < deadline and not msgs:
            app.processEvents()
            time.sleep(0.05)
    finally:
        worker.stop()
        worker.wait(3000)
    assert msgs, "worker phải phát tín hiệu link"
    assert any(not up and msg for up, msg in msgs), \
        "phải có trạng thái link down kèm lý do"
    assert any(len(msg) > 10 for _, msg in msgs), "lý do phải đủ cụ thể"
