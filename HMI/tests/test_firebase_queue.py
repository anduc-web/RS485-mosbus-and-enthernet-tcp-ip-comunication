# -*- coding: utf-8 -*-
"""Test hồi quy: hàng đợi Firebase không được MẤT item khi gửi lỗi.

Bug cũ: khi 1 lần PUT lỗi, chỉ item đang lỗi được đẩy lại, các item SAU đó
đã bị xoá khỏi hàng đợi và biến mất → mất command_ack / log.
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


def _stub(sync, fail_at):
    """Thay _request bằng stub lỗi ở lần gọi thứ `fail_at`."""
    calls = []

    def fake(path, method="GET", payload=None, timeout=5, query=None):
        calls.append((path, payload))
        if len(calls) == fail_at:
            raise RuntimeError("mạng lỗi giả lập")
        return None

    sync._request = fake
    return calls


def test_flush_acks_keeps_all_items_after_failure(fb):
    for i in range(1, 4):
        fb.push_command_ack(i, True, f"lệnh {i}")
    calls = _stub(fb, fail_at=2)

    with pytest.raises(RuntimeError):
        fb._flush_acks()

    # item 2 và 3 phải còn lại, đúng thứ tự
    left = [payload["seq"] for _, payload in list(fb._ackq)]
    assert left == [2, 3], f"mất ack: {left}"

    # gửi lại thành công → đủ cả 2, đúng thứ tự
    calls2 = _stub(fb, fail_at=999)
    fb._flush_acks()
    assert [p["seq"] for _, p in calls2] == [2, 3]
    assert len(fb._ackq) == 0


def test_flush_logs_keeps_all_items_after_failure(fb):
    fb.push_log("INFO", "log 1")
    fb.push_log("WARN", "log 2")
    fb.push_log("ERROR", "log 3")
    calls = _stub(fb, fail_at=1)

    with pytest.raises(RuntimeError):
        fb._flush_logs()

    left = [item["msg"] for item in list(fb._logq)]
    assert left == ["log 1", "log 2", "log 3"], f"mất log: {left}"

    calls2 = _stub(fb, fail_at=999)
    fb._flush_logs()
    assert [p["msg"] for _, p in calls2] == ["log 1", "log 2", "log 3"]
    assert len(fb._logq) == 0


def test_flush_stops_at_first_failure(fb):
    """Sau khi lỗi, KHÔNG gửi tiếp các item sau (giữ đúng thứ tự)."""
    for i in range(1, 5):
        fb.push_command_ack(i, True, f"lệnh {i}")
    calls = _stub(fb, fail_at=2)
    with pytest.raises(RuntimeError):
        fb._flush_acks()
    assert len(calls) == 2, "phải dừng ngay khi gặp lỗi"
