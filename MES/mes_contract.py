# -*- coding: utf-8 -*-
"""Contract tag Modbus TCP + PROFILE bảng thanh ghi cho MES.

Hệ thống có **2 biến thể HMI** với bảng thanh ghi KHÁC NHAU:

  * ``sht30`` — dự án "kiến trúc mạng": SHTC30, 3 kênh (bơm / quạt / đèn halogen)
  * ``sht20`` — dự án "he_thong_giam_sat_RS485": SHTC20 RS-485 + quạt làm mát

MES **tự nhận diện** biến thể qua `detect_profile()` (xem `mes_app.py`), và
người dùng luôn có thể ép cứng bằng combo box trên thanh trên cùng.

Hai bên phải khớp bảng thanh ghi:
  * PC1 (HMI) → `HMI/modbus_tcp_gateway.py` (hoặc bản tương ứng ở dự án RS-485)
  * PC2 (MES) → file này
"""

import csv
import io
import json

DEVICE_ID = 254

# ----------------------------------------------------------------------
# PROFILE
# ----------------------------------------------------------------------
PROFILE_SHT30 = "sht30"
PROFILE_SHT20 = "sht20"
PROFILE_AUTO = "auto"

#: Ngưỡng theo profile: addr → (min, max). Dùng để chặn giá trị vô lý
#: trước khi ghi xuống PC1.
_LIMITS_SHT30 = {
    0: (0.0, 100.0), 1: (0.0, 100.0),
    2: (-10.0, 60.0), 3: (-10.0, 60.0),
    4: (-10.0, 60.0), 5: (-10.0, 60.0),
    6: (30, 7200), 7: (30, 7200), 8: (1, 999),
}
_LIMITS_SHT20 = {
    0: (0.0, 60.0), 1: (0.0, 60.0), 2: (10.0, 80.0),
    3: (30, 7200), 4: (1, 999),
}

#: Ô nhập ngưỡng trên UI.
#:   ui     — khoá trong `sp_formula`
#:   sample — khoá trong mẫu telemetry đọc từ PC1
#:   addr   — địa chỉ thanh ghi cấu hình (holding)
#:   kind   — 'pct' (%RH) | 'c' (°C) | 'int' (giá trị nguyên)
#:   order  — thứ tự ghi: 'off' trước 'on' để không vi phạm ràng buộc
_FIELDS_SHT30 = [
    {"ui": "rh_on_pct", "sample": "rh_on", "addr": 0, "kind": "pct",
     "label": "RH bật (%RH)", "min": 0.0, "max": 100.0, "step": 0.5},
    {"ui": "rh_off_pct", "sample": "rh_off", "addr": 1, "kind": "pct",
     "label": "RH tắt (%RH)", "min": 0.0, "max": 100.0, "step": 0.5},
    {"ui": "fan_on_c", "sample": "fan_on", "addr": 2, "kind": "c",
     "label": "T bật quạt (°C)", "min": -10.0, "max": 60.0, "step": 0.5},
    {"ui": "fan_off_c", "sample": "fan_off", "addr": 3, "kind": "c",
     "label": "T tắt quạt (°C)", "min": -10.0, "max": 60.0, "step": 0.5},
    {"ui": "heat_on_c", "sample": "heat_on", "addr": 4, "kind": "c",
     "label": "T bật đèn (°C)", "min": -10.0, "max": 60.0, "step": 0.5},
    {"ui": "heat_off_c", "sample": "heat_off", "addr": 5, "kind": "c",
     "label": "T tắt đèn (°C)", "min": -10.0, "max": 60.0, "step": 0.5},
]
_FIELDS_SHT20 = [
    {"ui": "fan_on_c", "sample": "fan_on", "addr": 0, "kind": "c",
     "label": "T bật quạt (°C)", "min": 0.0, "max": 60.0, "step": 0.5},
    {"ui": "fan_off_c", "sample": "fan_off", "addr": 1, "kind": "c",
     "label": "T tắt quạt (°C)", "min": 0.0, "max": 60.0, "step": 0.5},
    {"ui": "alert_temp_c", "sample": "alert_temp", "addr": 2, "kind": "c",
     "label": "T cảnh báo (°C)", "min": 10.0, "max": 80.0, "step": 0.5},
    {"ui": "fan_max_s", "sample": "fan_max_s", "addr": 3, "kind": "int",
     "label": "Watchdog quạt (s)", "min": 30, "max": 7200, "step": 30},
    {"ui": "failsafe_cycles", "sample": "failsafe_cycles", "addr": 4,
     "kind": "int", "label": "Số chu kỳ failsafe", "min": 1, "max": 999,
     "step": 1},
]

PROFILES = {
    PROFILE_SHT30: {
        "label": "SHTC30 · 3 kênh (bơm/quạt/đèn)",
        "ir": {"humidity_pct": 0, "temp_c": 1, "pump": 2, "fan": 3, "lamp": 4,
               "mode": 5, "estop": 6, "flags": 7, "uptime_lo": 8,
               "uptime_hi": 9, "data_age_s": 10, "seq": 11},
        "hr": {"rh_on": 0, "rh_off": 1, "fan_on": 2, "fan_off": 3, "heat_on": 4,
               "heat_off": 5, "pump_max_s": 6, "heat_max_s": 7,
               "failsafe_cycles": 8, "mode": 9},
        "coil": {"pump": 0, "fan": 1, "lamp": 2, "estop": 3},
        "loads": [("Bơm", "pump"), ("Quạt", "fan"), ("Đèn halogen", "lamp")],
        "fields": _FIELDS_SHT30,
        "limits": _LIMITS_SHT30,
    },
    PROFILE_SHT20: {
        "label": "SHTC20 · RS-485 + quạt làm mát",
        "ir": {"temp_c": 0, "humidity_pct": 1, "fan": 2, "mode": 3, "flags": 4,
               "uptime_lo": 5, "uptime_hi": 6, "data_age_s": 7, "seq": 8},
        "hr": {"fan_on": 0, "fan_off": 1, "alert_temp": 2, "fan_max_s": 3,
               "failsafe_cycles": 4, "mode": 5},
        "coil": {"fan": 0, "estop": 1},
        "loads": [("Quạt", "fan")],
        "fields": _FIELDS_SHT20,
        "limits": _LIMITS_SHT20,
    },
}

#: Thứ tự ghi ngưỡng cho từng profile (ngưỡng "tắt" trước, "bật" sau).
#: Nếu ghi sai thứ tự, PC1 từ chối lệnh (vi phạm ràng buộc bật ≥ tắt).
WRITE_ORDER = {
    PROFILE_SHT30: ["rh_off_pct", "rh_on_pct", "fan_off_c", "fan_on_c",
                    "heat_off_c", "heat_on_c"],
    PROFILE_SHT20: ["fan_off_c", "fan_on_c", "alert_temp_c", "fan_max_s",
                    "failsafe_cycles"],
}


def detect_profile(ir):
    """Đoán biến thể HMI từ 12 input registers đầu.

    Dùng 3 dấu hiệu không trùng nhau giữa 2 bảng thanh ghi:
      * IR4 — SHTC30: trạng thái đèn (0/1) · SHTC20: bitfield flags (0..15)
      * IR5/IR6 — SHTC30: mode/estop (0/1) · SHTC20: uptime (số lớn)
      * IR7 — SHTC30: flags (0..15) · SHTC20: tuổi dữ liệu (0..65535)
    """
    if not ir or len(ir) < 8:
        return None
    vals = list(ir) + [0] * max(0, 12 - len(ir))
    if vals[4] > 1:
        return PROFILE_SHT20
    if vals[5] > 1 or vals[6] > 1:
        return PROFILE_SHT20
    if vals[7] > 15:
        return PROFILE_SHT20
    return PROFILE_SHT30


def profile_fields(profile):
    """Danh sách ô nhập ngưỡng của profile."""
    return PROFILES.get(profile, PROFILES[PROFILE_SHT30])["fields"]


def profile_limits(profile):
    """Giới hạn hợp lệ của thanh ghi cấu hình theo profile."""
    return PROFILES.get(profile, PROFILES[PROFILE_SHT30])["limits"]


# ----------------------------------------------------------------------
# Hợp đồng tag (profile SHTC30 — bản chuẩn của dự án "kiến trúc mạng")
# ----------------------------------------------------------------------
#: (area, address, name, type, scale, unit, writable, description)
_TAGS = [
    ("input", 0, "humidity_pct", "int16", 0.1, "%RH", False, "Độ ẩm không khí"),
    ("input", 1, "temp_c", "int16", 0.1, "degC", False, "Nhiệt độ không khí"),
    ("input", 2, "pump_state", "bool", 1, "", False, "Bơm đang chạy"),
    ("input", 3, "fan_state", "bool", 1, "", False, "Quạt đang chạy"),
    ("input", 4, "lamp_state", "bool", 1, "", False, "Đèn halogen đang sáng"),
    ("input", 5, "mode", "enum", 1, "", False, "0=AUTO 1=MANUAL"),
    ("input", 6, "estop", "bool", 1, "", False, "E-STOP đang khóa"),
    ("input", 7, "flags", "bitfield", 1, "", False,
     "b0 sensor_ok · b1 connected · b2 cfg_valid · b3 dữ liệu cũ"),
    ("input", 8, "uptime_s_lo", "uint32", 1, "s", False, "Thời gian chạy (16 bit thấp)"),
    ("input", 9, "uptime_s_hi", "uint32", 1, "s", False, "Thời gian chạy (16 bit cao)"),
    ("input", 10, "data_age_s", "uint16", 1, "s", False,
     "Tuổi dữ liệu cảm biến — >10 s là cũ"),
    ("input", 11, "seq", "uint16", 1, "", False,
     "Bộ đếm số lần cập nhật (chống đọc trùng)"),
    ("holding", 0, "rh_on_pct", "int16", 0.1, "%RH", True, "Ngưỡng bật bơm"),
    ("holding", 1, "rh_off_pct", "int16", 0.1, "%RH", True, "Ngưỡng tắt bơm"),
    ("holding", 2, "fan_on_c", "int16", 0.1, "degC", True, "Ngưỡng bật quạt"),
    ("holding", 3, "fan_off_c", "int16", 0.1, "degC", True, "Ngưỡng tắt quạt"),
    ("holding", 4, "heat_on_c", "int16", 0.1, "degC", True, "Ngưỡng bật đèn"),
    ("holding", 5, "heat_off_c", "int16", 0.1, "degC", True, "Ngưỡng tắt đèn"),
    ("holding", 6, "pump_watchdog_s", "uint16", 1, "s", True, "Watchdog bơm"),
    ("holding", 7, "lamp_watchdog_s", "uint16", 1, "s", True, "Watchdog đèn"),
    ("holding", 8, "failsafe_cycles", "uint16", 1, "", True, "Số chu kỳ failsafe"),
    ("holding", 9, "mode_cmd", "enum", 1, "", True, "Lệnh chế độ 0=AUTO 1=MANUAL"),
    ("coil", 0, "pump_cmd", "bool", 1, "", True, "Bật/tắt bơm"),
    ("coil", 1, "fan_cmd", "bool", 1, "", True, "Bật/tắt quạt"),
    ("coil", 2, "lamp_cmd", "bool", 1, "", True, "Bật/tắt đèn halogen"),
    ("coil", 3, "estop_cmd", "bool", 1, "", True,
     "1 = kích hoạt E-STOP, 0 = giải phóng"),
]

#: Địa chỉ thanh ghi cấu hình (holding) — dùng để ghi từ UI.
HR_ADDRESS = {
    "rh_on_pct": 0, "rh_off_pct": 1,
    "fan_on_c": 2, "fan_off_c": 3,
    "heat_on_c": 4, "heat_off_c": 5,
    "pump_watchdog_s": 6, "lamp_watchdog_s": 7,
    "failsafe_cycles": 8, "mode_cmd": 9,
}

#: Giới hạn hợp lệ của các thanh ghi cấu hình (profile SHTC30).
HR_LIMITS = _LIMITS_SHT30

#: Ánh xạ tên ô nhập (contract) → khoá trong mẫu telemetry đọc từ PC1.
SAMPLE_KEY = {
    "rh_on_pct": "rh_on", "rh_off_pct": "rh_off",
    "fan_on_c": "fan_on", "fan_off_c": "fan_off",
    "heat_on_c": "heat_on", "heat_off_c": "heat_off",
    "pump_watchdog_s": "pump_max_s", "lamp_watchdog_s": "heat_max_s",
    "failsafe_cycles": "failsafe_cycles",
}

#: Địa chỉ coil — dùng để điều khiển từ UI.
COIL_ADDRESS = {"pump_cmd": 0, "fan_cmd": 1, "lamp_cmd": 2, "estop_cmd": 3}

FLD = {
    "humidity": "humidity_pct", "temp": "temp_c",
    "pump": "pump_state", "fan": "fan_state", "lamp": "lamp_state",
}


def build_tag_contract(device_id=DEVICE_ID, profile=PROFILE_SHT30):
    """Danh sách tag theo chuẩn import của MES/SCADA."""
    if profile == PROFILE_SHT20:
        return _build_tag_contract_sht20(device_id)
    return [
        {
            "device_id": device_id,
            "area": area,
            "address": addr,
            "name": name,
            "type": vtype,
            "scale": scale,
            "unit": unit,
            "writable": writable,
            "description": desc,
        }
        for area, addr, name, vtype, scale, unit, writable, desc in _TAGS
    ]


def _build_tag_contract_sht20(device_id=DEVICE_ID):
    """Contract tag cho biến thể SHTC20 RS-485."""
    rows = [
        ("input", 0, "temp_c", "int16", 0.1, "degC", False, "Nhiệt độ không khí"),
        ("input", 1, "humidity_pct", "int16", 0.1, "%RH", False, "Độ ẩm không khí"),
        ("input", 2, "fan_state", "bool", 1, "", False, "Quạt đang chạy"),
        ("input", 3, "mode", "enum", 1, "", False, "0=AUTO 1=MANUAL"),
        ("input", 4, "flags", "bitfield", 1, "", False,
         "b0 sensor_ok · b1 connected · b2 cfg_valid · b3 dữ liệu cũ"),
        ("input", 5, "uptime_s_lo", "uint32", 1, "s", False, "Thời gian chạy (16 bit thấp)"),
        ("input", 6, "uptime_s_hi", "uint32", 1, "s", False, "Thời gian chạy (16 bit cao)"),
        ("input", 7, "data_age_s", "uint16", 1, "s", False,
         "Tuổi dữ liệu cảm biến — >10 s là cũ"),
        ("input", 8, "seq", "uint16", 1, "", False,
         "Bộ đếm số lần cập nhật (chống đọc trùng)"),
        ("holding", 0, "fan_on_c", "int16", 0.1, "degC", True, "Ngưỡng bật quạt"),
        ("holding", 1, "fan_off_c", "int16", 0.1, "degC", True, "Ngưỡng tắt quạt"),
        ("holding", 2, "alert_temp_c", "int16", 0.1, "degC", True, "Ngưỡng cảnh báo"),
        ("holding", 3, "fan_watchdog_s", "uint16", 1, "s", True, "Watchdog quạt"),
        ("holding", 4, "failsafe_cycles", "uint16", 1, "", True, "Số chu kỳ failsafe"),
        ("holding", 5, "mode_cmd", "enum", 1, "", True, "Lệnh chế độ 0=AUTO 1=MANUAL"),
        ("coil", 0, "fan_cmd", "bool", 1, "", True, "Bật/tắt quạt"),
        ("coil", 1, "estop_cmd", "bool", 1, "", True,
         "1 = kích hoạt E-STOP, 0 = giải phóng"),
    ]
    return [
        {"device_id": device_id, "area": a, "address": ad, "name": n,
         "type": t, "scale": sc, "unit": un, "writable": w, "description": d}
        for a, ad, n, t, sc, un, w, d in rows
    ]


def contract_to_csv(contract=None):
    """Contract → CSV (để MES import tag)."""
    contract = contract if contract is not None else build_tag_contract()
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[
        "device_id", "area", "address", "name", "type", "scale", "unit",
        "writable", "description"])
    writer.writeheader()
    for tag in contract:
        writer.writerow({k: ("true" if v is True else "false" if v is False
                             else v) for k, v in tag.items()})
    return buf.getvalue()


def contract_to_json(contract=None):
    """Contract → JSON (để MES import tag)."""
    contract = contract if contract is not None else build_tag_contract()
    return json.dumps(contract, ensure_ascii=False, indent=2)


def signed16(raw):
    """Giải mã thanh ghi 16 bit có dấu."""
    raw = int(raw) & 0xFFFF
    return raw - 0x10000 if raw >= 0x8000 else raw


def scale_value(tag, raw):
    """Giá trị thanh ghi → giá trị kỹ thuật (áp dụng scale của contract)."""
    if tag["type"] == "int16":
        return signed16(raw) * tag["scale"]
    if tag["type"] == "uint16":
        return raw * tag["scale"]
    return raw
