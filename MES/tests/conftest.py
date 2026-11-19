# -*- coding: utf-8 -*-
"""Fixture dùng chung cho test MES (app PyQt5)."""
import os
import sys

import pytest

MES = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HMI = os.path.join(os.path.dirname(MES), "HMI")
if MES not in sys.path:
    sys.path.insert(0, MES)
if HMI not in sys.path:
    sys.path.insert(0, HMI)

pytest.importorskip("PyQt5", reason="chưa cài PyQt5")

from PyQt5.QtWidgets import QApplication                # noqa: E402


@pytest.fixture(scope="session")
def app():
    """Một QApplication dùng chung cho toàn phiên test."""
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture()
def win(app, tmp_path):
    """Cửa sổ MES trỏ tới cổng không có gì (để test không đụng PC1 thật)."""
    import mes_app
    w = mes_app.MesWindow("127.0.0.1", 5099, 254, str(tmp_path / "h.db"))
    yield w
    w.close()
