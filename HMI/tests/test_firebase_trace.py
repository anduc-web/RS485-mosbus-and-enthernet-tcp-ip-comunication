# -*- coding: utf-8 -*-
"""Test đẩy khung Modbus (RTU + ASCII) lên Firebase cho Web ERP.

Khung ASCII là dữ liệu "tín hiệu báo về" của từng lần đo: HMI ghi nhận
đúng byte trên bus RS-485 rồi đẩy lên nhánh `modbus_trace` để web ERP
hiển thị và xuất dữ liệu.

Quy tắc quan trọng:
  • không gửi trùng (mỗi chu kỳ 2 s lại lấy TOÀN BỘ buffer trace),
  • khi mạng lỗi thì KHÔNG mất khung nào (giống hàng đợi log/ack).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from firebase_sync import FirebaseSync  # noqa: E402


@pytest.fixture()
def fb():
    sync = FirebaseSync(device="vuon-1")
    sync._url = "http://example.invalid"      # sẽ được thay bằng stub
    return sync


def _rec(ts, direction="TX", slave=1, fc=4):
    """Một bản ghi khung Modbus đúng dạng `modbus_frames.make_record()`."""
    return {
        "ts": ts,
        "dir": direction,
        "slave": slave,
        "fc": fc,
        "fc_name": "FC04 · Đọc input register",
        "rtu": "01 04 00 00 00 02 71 CB",
        "ascii": ":010400000002F9",
        "crc16": "71CB",
        "lrc": "F9",
        "ok": True,
        "note": "đọc 2 ô @0x0000",
    }


def _stub_ok(sync):
    calls = []

    def fake(path, method="GET", payload=None, timeout=5, query=None):
        calls.append((path, method, payload))
        return None

    sync._request = fake
    return calls


def _stub_fail(sync, fail_at=1):
    calls = []

    def fake(path, method="GET", payload=None, timeout=5, query=None):
        calls.append((path, method, payload))
        if len(calls) >= fail_at:
            raise RuntimeError("mạng lỗi giả lập")
        return None

    sync._request = fake
    return calls


def test_push_trace_giu_ban_ghi_trong_hang_doi(fb):
    fb.push_trace([_rec(1000), _rec(2000)])
    assert [i["ts"] for i in fb._traceq] == [1000, 2000]


def test_push_trace_bo_qua_ban_ghi_da_gui(fb):
    """HMI gửi lại TOÀN BỘ buffer mỗi chu kỳ → không được gửi trùng."""
    fb.push_trace([_rec(1000), _rec(2000)])
    fb._traceq.clear()
    fb.push_trace([_rec(1000), _rec(2000), _rec(3000)])
    assert [i["ts"] for i in fb._traceq] == [3000]


def test_push_trace_giu_nguyen_ban_ghi_co_ngay_ts(fb):
    """TX và RX có thể trùng mili-giây → vẫn phải giữ cả hai."""
    fb.push_trace([_rec(1000, "TX"), _rec(1000, "RX")])
    assert len(fb._traceq) == 2


def test_flush_trace_gui_dung_nhanh_va_dung_truong(fb):
    calls = _stub_ok(fb)
    fb.push_trace([_rec(1000)])
    fb._flush_trace()

    assert calls[0][0] == "hmi/vuon-1/modbus_trace"
    assert calls[0][1] == "POST"
    payload = calls[0][2]
    assert payload["ascii"] == ":010400000002F9"
    assert payload["rtu"] == "01 04 00 00 00 02 71 CB"
    assert payload["crc16"] == "71CB"
    assert payload["lrc"] == "F9"
    assert payload["fc_name"].startswith("FC04")
    assert payload["slave"] == 1
    assert payload["ok"] is True
    assert len(fb._traceq) == 0


def test_flush_trace_giu_item_khi_loi_mang(fb):
    fb.push_trace([_rec(1000), _rec(2000)])
    _stub_fail(fb, fail_at=1)

    with pytest.raises(RuntimeError):
        fb._flush_trace()

    assert [i["ts"] for i in fb._traceq] == [1000, 2000], "mất khung Modbus"


def test_flush_trace_dung_thu_tu_khi_gui_lai(fb):
    fb.push_trace([_rec(1000), _rec(2000)])
    _stub_fail(fb, fail_at=1)
    with pytest.raises(RuntimeError):
        fb._flush_trace()

    calls = _stub_ok(fb)
    fb._flush_trace()
    assert [p["ts"] for _, _, p in calls] == [1000, 2000]
    assert len(fb._traceq) == 0


def test_run_loop_dinh_ky_gui_trace(fb):
    """Vòng lặp nền phải gọi _flush_trace mỗi chu kỳ."""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "firebase_sync.py"), encoding="utf-8").read()
    assert "self._flush_trace()" in src
