# -*- coding: utf-8 -*-
"""Test nhận diện biến thể HMI (SHTC30 3 kênh ↔ SHTC20 RS-485).

Hệ thống có 2 bảng thanh ghi KHÁC NHAU. MES phải tự nhận diện để luôn
đọc đúng, nếu không sẽ ra số liệu bịa (đọc lệch vị trí).
"""
import os
import sys

import pytest

MES = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, MES)

pytest.importorskip("PyQt5", reason="chưa cài PyQt5")

from mes_contract import (PROFILE_SHT20, PROFILE_SHT30,          # noqa: E402
                          detect_profile)

#: Input registers theo layout SHTC30 (kiến trúc mạng):
#:  RH, T, bơm, quạt, đèn, mode, estop, flags, up_lo, up_hi, age, seq
#:  flags = 11 = 0b1011 → sensor_ok + connected + dữ liệu cũ
IR_SHT30 = [478, 264, 1, 0, 1, 0, 0, 11, 100, 0, 2, 7]
#: Input registers theo layout SHTC20 (RS-485):
#:  T, RH, quạt, mode, flags, up_lo, up_hi, age, seq
#:  flags = 15 = 0b1111 → sensor_ok + connected + cfg_valid + dữ liệu cũ
IR_SHT20 = [264, 478, 1, 1, 15, 972, 0, 65535, 42, 0, 0, 0]


# ======================================================================
# detect_profile
# ======================================================================
def test_detect_sht30_layout():
    assert detect_profile(IR_SHT30) == PROFILE_SHT30


def test_detect_sht20_layout():
    assert detect_profile(IR_SHT20) == PROFILE_SHT20


def test_detect_returns_none_without_data():
    assert detect_profile([]) is None
    assert detect_profile(None) is None


def test_detect_handles_idle_both_variants():
    """Cả 2 biến thể đều có thể đang rảnh (mọi giá trị 0)."""
    assert detect_profile([0] * 12) == PROFILE_SHT30


# ======================================================================
# Worker map đúng theo profile
# ======================================================================
def test_worker_maps_sht30_registers(app, tmp_path):
    import mes_app
    w = mes_app.MesWorker("127.0.0.1", 5099, 254,
                           mes_app.History(str(tmp_path / "h.db")),
                           profile=PROFILE_SHT30)
    hr = [400, 450, 320, 300, 215, 250, 600, 1800, 3, 0]
    s = w._build_sample(IR_SHT30, hr, [True, False, True, False])
    assert s["humidity"] == pytest.approx(47.8)
    assert s["temp"] == pytest.approx(26.4)
    assert s["pump"] is True
    assert s["fan"] is False
    assert s["lamp"] is True
    assert s["mode"] == "AUTO"
    assert s["sensor_ok"] is True          # flags bit0
    assert s["connected"] is True          # flags bit1
    assert s["stale"] is True              # flags bit3
    assert s["cfg_valid"] is False         # flags bit2 (0 trong flags=11)
    assert s["seq"] == 7
    assert s["rh_on"] == pytest.approx(40.0)
    assert s["failsafe_cycles"] == 3


def test_worker_maps_sht20_registers(app, tmp_path):
    import mes_app
    w = mes_app.MesWorker("127.0.0.1", 5099, 254,
                           mes_app.History(str(tmp_path / "h.db")),
                           profile=PROFILE_SHT20)
    hr = [320, 300, 350, 600, 3, 1, 0, 0, 0, 0]
    s = w._build_sample(IR_SHT20, hr, [True, False, False, False])
    assert s["temp"] == pytest.approx(26.4)       # IR0 = T (khác SHTC30!)
    assert s["humidity"] == pytest.approx(47.8)    # IR1 = RH
    assert s["fan"] is True                       # IR2 = quạt
    assert s["mode"] == "MANUAL"                  # IR3 = mode
    assert s["sensor_ok"] is True                 # IR4 = flags
    assert s["data_age_s"] == 65535               # IR7 = tuổi dữ liệu
    assert s["seq"] == 42                         # IR8 = seq
    assert s["uptime_s"] == 972                   # IR5/IR6 = uptime
    assert s["fan_on"] == pytest.approx(32.0)     # HR0
    assert s["alert_temp"] == pytest.approx(35.0)  # HR2
    assert s["fan_max_s"] == 600                  # HR3
    assert s["failsafe_cycles"] == 3              # HR4


def test_worker_autodetects_profile(app, tmp_path):
    import mes_app
    w = mes_app.MesWorker("127.0.0.1", 5099, 254,
                           mes_app.History(str(tmp_path / "h.db")))
    assert w._update_profile(IR_SHT20) == PROFILE_SHT20
    assert w._update_profile(IR_SHT30) == PROFILE_SHT30


def test_forced_profile_not_overwritten(app, tmp_path):
    import mes_app
    w = mes_app.MesWorker("127.0.0.1", 5099, 254,
                           mes_app.History(str(tmp_path / "h.db")),
                           profile=PROFILE_SHT30)
    assert w._update_profile(IR_SHT20) == PROFILE_SHT30


# ======================================================================
# UI: ô nhập ngưỡng đổi theo profile
# ======================================================================
def test_formula_fields_follow_profile(win):
    win._populate_formula(PROFILE_SHT30)
    assert "rh_on_pct" in win.sp_formula
    assert "heat_off_c" in win.sp_formula
    assert "alert_temp_c" not in win.sp_formula

    win._populate_formula(PROFILE_SHT20)
    assert "fan_on_c" in win.sp_formula
    assert "alert_temp_c" in win.sp_formula
    assert "rh_on_pct" not in win.sp_formula


def test_profile_combo_exists(win):
    assert win.cmb_profile.count() == 3
    assert win.cmb_profile.itemData(0) == "auto"


def test_load_buttons_follow_profile(win):
    """Profile SHTC30 có bơm/quạt/đèn; SHTC20 chỉ có quạt."""
    win._populate_formula(PROFILE_SHT30)
    n30 = win._ctrl_row
    win._populate_formula(PROFILE_SHT20)
    assert win._ctrl_row < n30, "SHTC20 có ít tải điều khiển hơn"
