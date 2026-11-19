# -*- coding: utf-8 -*-
"""Cấu hình chung cho test HMI.

Điểm quan trọng: test KHÔNG được ghi đè `hmi_config.json` thật của người dùng
(app HMI lưu cấu hình khi khởi động worker / bật cổng TCP). Mọi test tạo
MainWindow đều trỏ `_settings_path` sang file tạm.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Tách file cấu hình thật khỏi test."""
    pytest.importorskip("PyQt5")
    import hmi_shtc30_3kenh as hmi

    target = tmp_path / "hmi_config_test.json"
    monkeypatch.setattr(hmi.MainWindow, "_settings_path",
                        lambda self: str(target))
    yield target
