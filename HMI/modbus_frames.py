# -*- coding: utf-8 -*-
"""Dựng khung Modbus để báo cáo "tín hiệu" từng lần đo lên ERP.

Hệ thống có 2 dạng mã hoá trên bus RS-485:

  * **RTU (binary)** — khung ``slave | fc | data | CRC-16`` (2 byte, CRC theo
    chuẩn Modbus: poly 0xA001, init 0xFFFF).
  * **ASCII** — khung ``:`` + hex(slave|fc|data) + ``LRC`` + ``\\r\\n``
    (LRC = bù hai của tổng các byte).

Module này dựng **đúng byte thật** mà PC1 gửi/nhận trên bus, kèm tên mã
hàm (FC01/FC02/FC03/FC04/FC05/FC06/FC15/FC16/FC0x2B) để ERP hiển thị.

Kiểm thử: `HMI/tests/test_modbus_frames.py` — đối chiếu CRC/LRC với chính
pymodbus để chắc chắn khớp byte thật.
"""

import struct
import time

#: Tên mã hàm Modbus.
FC_NAMES = {
    1: "FC01 · Đọc coil",
    2: "FC02 · Đọc discrete input",
    3: "FC03 · Đọc holding register",
    4: "FC04 · Đọc input register",
    5: "FC05 · Ghi 1 coil",
    6: "FC06 · Ghi 1 holding register",
    15: "FC15 · Ghi nhiều coil",
    16: "FC16 · Ghi nhiều holding register",
    43: "FC0x2B · Đọc thông tin thiết bị",
}

#: Mã lỗi Modbus → mô tả.
EXC_NAMES = {
    1: "Illegal function", 2: "Illegal data address",
    3: "Illegal data value", 4: "Device failure",
    5: "Acknowledge", 6: "Device busy",
    8: "Memory parity error", 10: "Gateway path unavailable",
    11: "Gateway target no response",
}


def fc_name(fc):
    """Tên mã hàm (có cả phần lỗi FC|0x80)."""
    fc = int(fc) & 0xFF
    if fc & 0x80:
        base = fc & 0x7F
        return f"{FC_NAMES.get(base, f'FC{base:02X}')} + LỖI"
    return FC_NAMES.get(fc, f"FC{fc:02X}")


def exc_name(code):
    """Mô tả mã lỗi Modbus."""
    return EXC_NAMES.get(int(code), f"Lỗi {code}")


# ----------------------------------------------------------------------
# Kiểm tra
# ----------------------------------------------------------------------
def crc16_modbus(data):
    """CRC-16 chuẩn Modbus (giống hệt pymodbus).

    Giá trị trả về đã SWAP byte (như pymodbus) → đặt vào khung RTU theo
    thứ tự big-endian.
    """
    crc = 0xFFFF
    for byte in bytes(data):
        crc ^= int(byte)
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return ((crc << 8) & 0xFF00) | ((crc >> 8) & 0x00FF)


def lrc_ascii(data):
    """LRC của Modbus ASCII: bù hai của tổng các byte (mod 256)."""
    lrc = sum(int(b) for b in bytes(data)) & 0xFF
    return ((lrc ^ 0xFF) + 1) & 0xFF


def crc16_ok(frame):
    """Kiểm tra CRC-16 của một khung RTU đầy đủ (slave..data + 2 byte CRC)."""
    frame = bytes(frame)
    if len(frame) < 4:
        return False
    body, crc = frame[:-2], int.from_bytes(frame[-2:], "big")
    return crc16_modbus(body) == crc


def lrc_ok(frame):
    """Kiểm tra LRC của một khung ASCII (bỏ ':' và CRLF)."""
    txt = bytes(frame)
    if txt.startswith(b":"):
        txt = txt[1:]
    txt = txt.rstrip(b"\r\n")
    try:
        raw = bytes.fromhex(txt.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return False
    if len(raw) < 3:
        return False
    body, lrc = raw[:-1], raw[-1]
    return lrc_ascii(body) == lrc


# ----------------------------------------------------------------------
# Dựng khung
# ----------------------------------------------------------------------
def build_rtu(slave, fc, payload=b""):
    """Khung RTU: slave + fc + data + CRC-16 (2 byte, big-endian)."""
    body = bytes([int(slave) & 0xFF, int(fc) & 0xFF]) + bytes(payload)
    return body + crc16_modbus(body).to_bytes(2, "big")


def build_ascii(slave, fc, payload=b""):
    """Khung ASCII: ':' + hex(slave+fc+data) + LRC + CRLF."""
    body = bytes([int(slave) & 0xFF, int(fc) & 0xFF]) + bytes(payload)
    lrc = lrc_ascii(body)
    text = ":" + body.hex().upper() + f"{lrc:02X}" + "\r\n"
    return text.encode("ascii")


def read_request(slave, fc, address, count):
    """Yêu cầu đọc (FC01/FC02/FC03/FC04) → (rtu, ascii)."""
    payload = struct.pack(">HH", int(address) & 0xFFFF, int(count) & 0xFFFF)
    return build_rtu(slave, fc, payload), build_ascii(slave, fc, payload)


def write_coil_request(slave, address, on):
    """Yêu cầu ghi 1 coil (FC05) → (rtu, ascii)."""
    payload = struct.pack(">HH", int(address) & 0xFFFF,
                          0xFF00 if on else 0x0000)
    return build_rtu(slave, 5, payload), build_ascii(slave, 5, payload)


def write_register_request(slave, address, value):
    """Yêu cầu ghi 1 holding register (FC06) → (rtu, ascii)."""
    payload = struct.pack(">HH", int(address) & 0xFFFF, int(value) & 0xFFFF)
    return build_rtu(slave, 6, payload), build_ascii(slave, 6, payload)


def read_response(slave, fc, values):
    """Phản hồi đọc (FC01..FC04) → (rtu, ascii).

    ``values`` là list int (holding/input register) hoặc list bool (coil).
    """
    values = list(values or [])
    if int(fc) in (1, 2):
        nbytes = (len(values) + 7) // 8
        packed = bytearray(nbytes)
        for i, bit in enumerate(values):
            if bit:
                packed[i // 8] |= 1 << (i % 8)
        data = bytes([nbytes]) + bytes(packed)
    else:
        data = bytes([len(values) * 2]) + b"".join(
            struct.pack(">H", int(v) & 0xFFFF) for v in values)
    return build_rtu(slave, fc, data), build_ascii(slave, fc, data)


def exception_response(slave, fc, code):
    """Phản hồi lỗi (fc | 0x80) → (rtu, ascii)."""
    data = bytes([int(code) & 0xFF])
    return build_rtu(slave, int(fc) | 0x80, data), \
        build_ascii(slave, int(fc) | 0x80, data)


# ----------------------------------------------------------------------
# Hiển thị
# ----------------------------------------------------------------------
def to_hex(data):
    """Byte → chuỗi hex, cách nhau bởi dấu cách: ``01 04 00 00 00 02``."""
    return " ".join(f"{b:02X}" for b in bytes(data))


def to_ascii(data):
    """Byte → chuỗi ký tự in được (byte không in → ``.``)."""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in bytes(data))


def make_record(direction, slave, fc, rtu=b"", ascii_frame=b"", note="",
                ok=True, ts=None):
    """Tạo 1 bản ghi khung để đẩy lên ERP.

    ``direction``: ``"TX"`` (PC1 → bus) hoặc ``"RX"`` (bus → PC1).
    """
    rtu = bytes(rtu or b"")
    ascii_frame = bytes(ascii_frame or b"")
    crc = ""
    if len(rtu) >= 2:
        crc = rtu[-2:].hex().upper()
    lrc = ""
    if ascii_frame:
        raw = ascii_frame.lstrip(b":").rstrip(b"\r\n")
        if len(raw) >= 2:
            # bytes -> str, nếu không web ERP sẽ hiện "b'F9'" thay vì "F9".
            lrc = raw[-2:].decode("ascii", "replace").upper()
    return {
        "ts": int(ts if ts is not None else time.time() * 1000),
        "dir": direction,
        "slave": int(slave),
        "fc": int(fc) & 0xFF,
        "fc_name": fc_name(fc),
        "rtu": to_hex(rtu),
        "ascii": ascii_frame.decode("ascii", "replace").strip(),
        "crc16": crc,
        "lrc": lrc,
        "ok": bool(ok),
        "note": str(note)[:160],
    }
