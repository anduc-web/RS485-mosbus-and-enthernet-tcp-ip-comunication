# -*- coding: utf-8 -*-
"""Test dựng khung Modbus (RTU + ASCII) — đối chiếu với chính pymodbus."""
import os
import sys

import pytest

HMI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HMI)

import modbus_frames as mf                          # noqa: E402


# ======================================================================
# CRC-16 / LRC khớp pymodbus (byte thật trên bus)
# ======================================================================
def test_crc16_matches_pymodbus():
    from pymodbus.framer.rtu import FramerRTU
    for raw in (b"\x01\x04\x00\x00\x00\x02", b"\xff\x03\x00\x64\x00\x01",
                b"\x01\x05\x00\x02\xff\x00", bytes(range(8))):
        assert mf.crc16_modbus(raw) == FramerRTU.compute_CRC(raw)


def test_lrc_matches_pymodbus():
    from pymodbus.framer.ascii import FramerAscii
    for raw in (b"\x01\x04\x00\x00\x00\x02", b"\x11\x03\x00\x6b\x00\x03",
                bytes(range(6))):
        assert mf.lrc_ascii(raw) == FramerAscii.compute_LRC(raw)


# ======================================================================
# Khung đọc
# ======================================================================
def test_read_request_fc04():
    rtu, asc = mf.read_request(1, 4, 0x0000, 2)
    assert rtu[:6] == bytes([0x01, 0x04, 0x00, 0x00, 0x00, 0x02])
    assert len(rtu) == 8                     # + CRC 2 byte
    assert mf.crc16_ok(rtu)
    assert asc.startswith(b":010400000002")
    assert asc.endswith(b"\r\n")
    assert mf.lrc_ok(asc)


def test_read_request_ascii_is_hex_of_same_body():
    rtu, asc = mf.read_request(255, 3, 0x0064, 10)
    body = rtu[:-2]
    assert asc.lstrip(b":").rstrip(b"\r\n")[:len(body) * 2].decode() \
        == body.hex().upper()


def test_read_response_registers():
    rtu, asc = mf.read_response(1, 4, [478, 264])
    assert rtu[:3] == bytes([0x01, 0x04, 0x04])      # byte count = 4
    assert rtu[3:7] == b"\x01\xDE\x01\x08"            # 478, 264
    assert mf.crc16_ok(rtu)
    assert mf.lrc_ok(asc)


def test_read_response_coils_packs_bits():
    rtu, _ = mf.read_response(255, 1, [True, False, True, False, False,
                                        False, False, True])
    assert rtu[2] == 1                                # 1 byte
    assert rtu[3] == 0b10000101                       # bit0, bit2, bit7


def test_exception_response_sets_high_bit():
    rtu, asc = mf.exception_response(1, 4, 2)
    assert rtu[1] == 0x84                             # fc | 0x80
    assert rtu[2] == 2
    assert mf.crc16_ok(rtu)
    assert mf.lrc_ok(asc)


# ======================================================================
# Khung ghi
# ======================================================================
def test_write_coil_request():
    rtu, asc = mf.write_coil_request(255, 2, True)
    assert rtu[:6] == bytes([0xFF, 0x05, 0x00, 0x02, 0xFF, 0x00])
    assert mf.crc16_ok(rtu) and mf.lrc_ok(asc)


def test_write_register_request():
    rtu, asc = mf.write_register_request(255, 0, 400)
    assert rtu[:6] == bytes([0xFF, 0x06, 0x00, 0x00, 0x01, 0x90])
    assert mf.crc16_ok(rtu) and mf.lrc_ok(asc)


# ======================================================================
# Bản ghi đẩy lên ERP
# ======================================================================
def test_make_record_contains_everything():
    rtu, asc = mf.read_request(1, 4, 0, 2)
    rec = mf.make_record("TX", 1, 4, rtu, asc, note="đọc cảm biến")
    assert rec["dir"] == "TX"
    assert rec["slave"] == 1
    assert rec["fc"] == 4
    assert "FC04" in rec["fc_name"]
    assert rec["rtu"].startswith("01 04 00 00 00 02")
    assert rec["ascii"].startswith(":010400000002")
    assert len(rec["crc16"]) == 4
    assert len(rec["lrc"]) == 2
    # crc16 / lrc phải là CHUỖI để JSON hoá và hiển thị đúng trên web ERP
    assert isinstance(rec["crc16"], str)
    assert isinstance(rec["lrc"], str)
    assert rec["lrc"] == "F9"
    assert rec["crc16"] == "71CB"
    assert rec["ok"] is True
    assert rec["note"] == "đọc cảm biến"


def test_fc_name_marks_exception():
    assert "LỖI" in mf.fc_name(0x84)
    assert "FC05" in mf.fc_name(5)


def test_to_hex_and_ascii():
    assert mf.to_hex(b"\x01\xff") == "01 FF"
    assert mf.to_ascii(b"\x41\x42\x00") == "AB."
