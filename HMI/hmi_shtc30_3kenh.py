# -*- coding: utf-8 -*-
"""
==============================================================================
 HMI ĐIỀU KHIỂN KHÍ HẬU PHÒNG  ·  SHTC30  +  MODULE RELAY 3 KÊNH
 -----------------------------------------------------------------------------
 Đo NHIỆT ĐỘ & ĐỘ ẨM KHÔNG KHÍ của phòng bằng cảm biến SHTC30 (RS485,
 Modbus RTU), hiển thị THỜI GIAN THỰC và điều khiển 3 tải qua module relay
 4 kênh (dùng 3 kênh, thứ tự trên xuống theo module):

     CH1  →  MÁY BƠM          BẬT khi độ ẩm KHÔNG KHÍ thấp
     CH2  →  QUẠT LÀM MÁT     BẬT khi nhiệt độ CAO
     CH3  →  ĐÈN HALOGEN      BẬT khi nhiệt độ THẤP (ủ ấm)
     CH4  →  dự phòng

 Nguyên tắc điều khiển AUTO (có hysteresis, chống bật/tắt liên tục):
     Bơm  : RH <  RH_BẬT     → BẬT   |  RH ≥ RH_TẮT     → TẮT
     Quạt : T  >  T_QUẠT_BẬT → BẬT   |  T ≤ T_QUẠT_TẮT → TẮT
     Đèn  : T  <  T_ĐÈN_BẬT  → BẬT   |  T ≥ T_ĐÈN_TẮT  → TẮT

 An toàn:
   · Quạt và đèn halogen KHÔNG BAO GIỜ bật cùng lúc (khóa liên động).
   · Mất dữ liệu cảm biến → AUTO khóa, tự tắt cả 3 tải.
   · Watchdog bơm 600 s, đèn halogen 1800 s (chống chạy quá lâu).
   · Khi khởi động và khi ngắt kết nối: ghi TẮT cả 3 kênh relay.

 Thiết bị Modbus:
   · Slave cảm biến (mặc định 1): SHTC30-RS485, FC04 input registers
       2 thanh ghi liên tiếp, mỗi giá trị ×0,1: (nhiệt độ °C, độ ẩm %RH)
       ĐO TRÊN MODULE THẬT: 0x0000 = nhiệt độ, 0x0001 = độ ẩm (swap_th=True).
       Module trả NGƯỢC thứ tự (0x0000 = độ ẩm) → bỏ tick "Đảo thứ tự thanh ghi".
   · Slave relay (mặc định 255): board Bestep/CHINALCTECH 4 kênh
       CH1..CH4 = địa chỉ 0x0000..0x0003
       Hỗ trợ cả Coil FC05 và Holding Register FC06 (tự dò khi sai).
==============================================================================
"""

import sys
import os
import csv
import json
import math
import time
import random
import threading
import traceback
import subprocess
import socket
from datetime import datetime
from collections import deque

# ------------------------- PyQt5 ------------------------------------------------
from PyQt5.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QRectF, QPointF
)
from PyQt5.QtGui import (
    QPainter, QColor, QPen, QBrush, QFont,
    QLinearGradient, QRadialGradient, QPolygonF
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QPushButton, QComboBox, QTextEdit,
    QFrame, QSizePolicy, QMessageBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QDoubleSpinBox, QAbstractItemView, QSpinBox,
    QLineEdit, QCheckBox, QScrollArea
)

# ------------------------- pyqtgraph --------------------------------------------
try:
    import pyqtgraph as pg
    PYQTGRAPH_OK = True
except Exception:
    PYQTGRAPH_OK = False

# ------------------------- pymodbus ---------------------------------------------
PYMODBUS_OK = True
try:
    from pymodbus.client import ModbusSerialClient
    from pymodbus.exceptions import ModbusException
except Exception:
    try:
        from pymodbus.client.sync import ModbusSerialClient
        from pymodbus.exceptions import ModbusException
    except Exception:
        PYMODBUS_OK = False

# ------------------------- pyserial: quét cổng COM thật ------------------------
SERIAL_OK = True
try:
    from serial.tools import list_ports
except Exception:
    list_ports = None
    SERIAL_OK = False

# ------------------------- Khung Modbus (để báo cáo lên ERP) ----------------
# Dựng đúng byte trên bus (RTU + ASCII, CRC-16/LRC) để ERP hiển thị
# "tín hiệu" từng lần đo.
try:
    import modbus_frames
except Exception:                                    # pragma: no cover
    modbus_frames = None

# ------------------------- Modbus TCP (phương án A) ------------------------
# Mở cổng TCP 502 cho PC2 đọc/ghi bằng client Modbus TCP tiêu chuẩn.
TCP_OK = True
try:
    from modbus_tcp_gateway import ModbusTcpGateway
except Exception:
    ModbusTcpGateway = None
    TCP_OK = False

SERIAL_HINT = (
    "Không tìm thấy cổng COM nào.\n"
    "  1. Cắm lại cáp USB-RS485 (rút ra cắm lại).\n"
    "  2. Cài driver: CH340 / CH341, CP210x, FTDI, PL2303.\n"
    "  3. Mở Device Manager > Ports (COM & LPT) xem có cổng mới không.\n"
    "  4. Nếu Windows thấy cổng mà app không thấy: gõ tay tên cổng (vd: COM7)\n"
    "     vào ô Cổng COM rồi nhấn CONNECT."
)

# Từ khóa nhận diện driver USB-RS485 thường gặp (để ưu tiên hiển thị trước)
_USB_HINTS = ("ch340", "ch341", "ch343", "cp210", "ftdi", "ft232", "pl2303",
              "usb", "silabs", "prolific", "rs485", "rs-485", "uart")
_VIRTUAL_HINTS = ("eltima", "virtual", "vsbc", "com0com", "vspd", "bluetooth")


def _windows_registry_ports():
    """Đọc thẳng registry HARDWARE\\DEVICEMAP\\SERIALCOMM (nguồn dự phòng)."""
    out = {}
    if not sys.platform.startswith("win"):
        return out
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
            i = 0
            while True:
                try:
                    name, value, _typ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if isinstance(value, str) and value.upper().startswith("COM"):
                    short = name.split("\\")[-1].replace("\x00", "")
                    out[value.upper()] = (value, f"Windows · {short}")
    except Exception:
        pass
    return out


def scan_serial_ports():
    """Trả về list[(device, description)] của các cổng serial đang có thật."""
    found = {}

    if SERIAL_OK:
        try:
            for p in list_ports.comports():
                dev = (p.device or "").strip()
                if not dev:
                    continue
                desc = (p.description or "").strip()
                if not desc or desc.lower() in ("n/a", "unknown", "n/a (no description)"):
                    desc = (p.hwid or "").strip() or "không rõ tên"
                found[dev.upper()] = (dev, desc)
        except Exception as e:
            print("list_ports.comports() lỗi:", e)

    for k, v in _windows_registry_ports().items():
        found.setdefault(k, v)

    def _rank(item):
        dev, desc = item
        d = desc.lower()
        try:
            num = int("".join(ch for ch in dev if ch.isdigit()) or 999)
        except ValueError:
            num = 999
        if any(h in d for h in _USB_HINTS):
            group = 0
        elif any(h in d for h in _VIRTUAL_HINTS):
            group = 2
        else:
            group = 1
        return (group, num, dev)

    return sorted(found.values(), key=_rank)


def _short_error(exc, limit=150):
    """Rút gọn thông báo lỗi pymodbus cho dễ đọc trên log."""
    s = str(exc).replace("Modbus Error: ", "").strip()
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit] + "…"


# ==============================================================================
# 1. CSV LOGGER
# ==============================================================================
class CsvLogger:
    FIELDS = ["Timestamp", "Humidity(%RH)", "Temp(C)",
              "Pump", "Fan", "Halogen", "Mode", "Note"]

    def __init__(self, path: str):
        self.path = path
        if not os.path.exists(self.path):
            try:
                with open(self.path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(self.FIELDS)
            except Exception as e:
                print("Không tạo được file CSV:", e)

    def write(self, humidity, temp, states, mode, note=""):
        def on(key):
            return "ON" if states.get(key) else "OFF"
        try:
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    f"{humidity:.2f}", f"{temp:.2f}",
                    on("pump"), on("fan"), on("lamp"), mode, note,
                ])
        except Exception as e:
            print("Lỗi ghi CSV:", e)


# ==============================================================================
# 2. WIDGET ĐỒNG HỒ TRÒN (màu theo vùng + vạch ngưỡng)
# ==============================================================================
class CircularGauge(QWidget):
    def __init__(self, title="", unit="%", min_val=0.0, max_val=100.0,
                 parent=None):
        super().__init__(parent)
        self.title = title
        self.unit = unit
        self.min_val = float(min_val)
        self.max_val = float(max_val)
        self.value = None
        self.prev = None
        self.trend = 0.0
        self.ticks = []              # [(value, QColor), ...]
        self.color_fn = None         # callable(value) -> QColor
        self.stale = False
        self.setMinimumSize(170, 160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_value(self, v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return
        if self.value is not None:
            self.trend = v - self.value
            self.prev = self.value
        self.value = max(self.min_val, min(self.max_val, v))
        self.update()

    def set_ticks(self, ticks):
        self.ticks = list(ticks)
        self.update()

    def set_color_fn(self, fn):
        self.color_fn = fn
        self.update()

    def set_stale(self, stale):
        stale = bool(stale)
        if stale != self.stale:
            self.stale = stale
            self.update()

    def _ratio(self, v):
        if self.max_val <= self.min_val:
            return 0.0
        return max(0.0, min(1.0, (v - self.min_val) / (self.max_val - self.min_val)))

    def _arc(self):
        w, h = self.width(), self.height()
        side = min(w - 24, h - 16) - 30
        side = max(40, side)
        return QRectF((w - side) / 2, (h - side) / 2 + 12, side, side)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        rect = self._arc()

        # Nền cung tròn
        pen_bg = QPen(QColor("#1b2a44"), 13)
        pen_bg.setCapStyle(Qt.RoundCap)
        p.setPen(pen_bg)
        p.drawArc(rect, 225 * 16, -270 * 16)

        # Cung giá trị
        if self.value is not None:
            ratio = self._ratio(self.value)
            color = QColor("#4cc9f0")
            if self.color_fn:
                try:
                    color = QColor(self.color_fn(self.value))
                except Exception:
                    pass
            if self.stale:
                color = QColor("#5b6b82")

            glow = QColor(color)
            glow.setAlpha(60)
            pen_glow = QPen(glow, 24)
            pen_glow.setCapStyle(Qt.RoundCap)
            p.setPen(pen_glow)
            p.drawArc(rect, 225 * 16, int(-270 * 16 * ratio))

            pen_val = QPen(color, 13)
            pen_val.setCapStyle(Qt.RoundCap)
            p.setPen(pen_val)
            p.drawArc(rect, 225 * 16, int(-270 * 16 * ratio))

        # Vạch ngưỡng
        cx, cy = rect.center().x(), rect.center().y()
        r = rect.width() / 2
        for val, col in self.ticks:
            if not (self.min_val <= val <= self.max_val):
                continue
            ang = math.radians(225.0 - 270.0 * self._ratio(val))
            x1 = cx + (r - 10) * math.cos(ang)
            y1 = cy - (r - 10) * math.sin(ang)
            x2 = cx + (r + 5) * math.cos(ang)
            y2 = cy - (r + 5) * math.sin(ang)
            p.setPen(QPen(QColor(col), 3))
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        # Tiêu đề (phía trên cung tròn)
        p.setPen(QColor("#8892b0"))
        p.setFont(QFont("Segoe UI", 9, QFont.Bold))
        p.drawText(QRectF(0, 3, w, 20), Qt.AlignCenter, self.title)

        # Giá trị (cỡ chữ co theo kích thước đồng hồ)
        if self.value is None:
            txt, tcol = "--.-", QColor("#5b6b82")
        else:
            txt = f"{self.value:.1f}"
            tcol = QColor("#ecf0f1") if not self.stale else QColor("#5b6b82")
        side = max(60.0, rect.height())
        cy = rect.center().y()
        fs = max(16, min(34, int(side * 0.26)))
        p.setPen(tcol)
        p.setFont(QFont("Segoe UI", fs, QFont.Bold))
        p.drawText(QRectF(rect.left(), cy - side * 0.34,
                          rect.width(), side * 0.34),
                   Qt.AlignCenter, txt)

        # Đơn vị
        p.setPen(QColor("#8892b0"))
        p.setFont(QFont("Segoe UI", 9))
        p.drawText(QRectF(rect.left(), cy + side * 0.02,
                          rect.width(), side * 0.18),
                   Qt.AlignCenter, self.unit)

        # Xu hướng
        if self.value is not None and abs(self.trend) >= 0.05:
            arrow = "▲" if self.trend > 0 else "▼"
            p.setPen(QColor("#8892b0"))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(QRectF(rect.left(), cy + side * 0.22,
                              rect.width(), side * 0.16),
                       Qt.AlignCenter, f"{arrow} {abs(self.trend):.1f}")

        # Min / max
        p.setPen(QColor("#4a5a75"))
        p.setFont(QFont("Consolas", 8))
        p.drawText(QRectF(rect.left() - 6, rect.bottom() - 16, 60, 14),
                   Qt.AlignLeft, f"{self.min_val:.0f}")
        p.drawText(QRectF(rect.right() - 54, rect.bottom() - 16, 60, 14),
                   Qt.AlignRight, f"{self.max_val:.0f}")


# ==============================================================================
# 3. LED INDICATOR
# ==============================================================================
class LEDIndicator(QWidget):
    def __init__(self, size=16, parent=None):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self._on = False

    def set_state(self, on):
        on = bool(on)
        if on != self._on:
            self._on = on
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        color = QColor("#2ecc71") if self._on else QColor("#e74c3c")
        glow = QColor(color)
        glow.setAlpha(90 if self._on else 40)
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(0, 0, self._size, self._size)
        p.setBrush(color)
        m = self._size // 6
        p.drawEllipse(m, m, self._size - 2 * m, self._size - 2 * m)

# ==============================================================================
# 4. DEVICE CARD (trạng thái 1 tải: bơm / quạt / đèn)
# ==============================================================================
class DeviceCard(QFrame):
    def __init__(self, name, icon, accent, parent=None):
        super().__init__(parent)
        self.name = name
        self.accent = QColor(accent)
        self.setObjectName("DeviceCard")
        self.setMinimumHeight(84)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._on = None
        self._pending = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(6)

        self.lbl_icon = QLabel(icon)
        self.lbl_icon.setFixedWidth(32)
        self.lbl_icon.setAlignment(Qt.AlignCenter)
        self.lbl_icon.setStyleSheet(f"color:{accent}; font-size:24px;")
        row.addWidget(self.lbl_icon)

        col = QVBoxLayout()
        col.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.lbl_name = QLabel(name)
        self.lbl_name.setStyleSheet(
            "color:#ecf0f1; font-size:10px; font-weight:700; letter-spacing:0.5px;")
        self.lbl_name.setMinimumWidth(0)
        top.addWidget(self.lbl_name, 1)
        self.lbl_ch = QLabel("CH1")
        self.lbl_ch.setStyleSheet(
            "color:#8892b0; font-size:9px; font-weight:700;"
            " background:#0b1626; border:1px solid #22314a;"
            " border-radius:4px; padding:1px 3px;")
        top.addWidget(self.lbl_ch)
        col.addLayout(top)

        mid = QHBoxLayout()
        mid.setSpacing(6)
        self.led = LEDIndicator(12)
        mid.addWidget(self.led)
        self.lbl_state = QLabel("ĐANG TẮT")
        self.lbl_state.setStyleSheet(
            "color:#6b7a90; font-size:13px; font-weight:700;")
        mid.addWidget(self.lbl_state)
        mid.addStretch()
        self.lbl_runtime = QLabel("")
        self.lbl_runtime.setStyleSheet(
            "color:#8892b0; font-family:Consolas; font-size:9px;")
        mid.addWidget(self.lbl_runtime)
        col.addLayout(mid)

        self.lbl_reason = QLabel("chưa có dữ liệu")
        self.lbl_reason.setStyleSheet("color:#6b7a90; font-size:10px;")
        self.lbl_reason.setToolTip("")
        self._reason_full = "chưa có dữ liệu"

        row.addLayout(col, 1)
        root.addLayout(row)
        root.addWidget(self.lbl_reason)
        self._paint(False, False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_reason()

    def _apply_reason(self):
        fm = self.lbl_reason.fontMetrics()
        avail = max(20, self.lbl_reason.width() - 2)
        txt = fm.elidedText(self._reason_full, Qt.ElideRight, avail)
        if txt != self.lbl_reason.text():
            self.lbl_reason.setText(txt)

    def set_channel(self, ch):
        self.lbl_ch.setText(f"CH{int(ch) + 1}")

    def set_reason(self, text):
        text = text or ""
        self._reason_full = text
        self.lbl_reason.setToolTip(text)
        self._apply_reason()

    def set_state(self, on, pending=False, runtime=""):
        on = bool(on)
        pending = bool(pending)
        self.lbl_runtime.setText(runtime or "")
        if pending == self._pending and on == self._on:
            return
        self._on = on
        self._pending = pending
        if pending:
            self.lbl_state.setText("ĐANG BẬT…" if not on else "ĐANG TẮT…")
            self.lbl_state.setStyleSheet(
                "color:#f39c12; font-size:13px; font-weight:700;")
            self.led.set_state(False)
        elif on:
            self.lbl_state.setText("ĐANG CHẠY")
            self.lbl_state.setStyleSheet(
                "color:#2ecc71; font-size:13px; font-weight:700;")
            self.led.set_state(True)
        else:
            self.lbl_state.setText("ĐANG TẮT")
            self.lbl_state.setStyleSheet(
                "color:#6b7a90; font-size:13px; font-weight:700;")
            self.led.set_state(False)
        self._paint(on, pending)

    def _paint(self, on, pending):
        if pending:
            border = "#f39c12"
            bg = "#141d2c"
        elif on:
            border = self.accent.name()
            bg = "#0f1e2e"
        else:
            border = "#22314a"
            bg = "#0e1726"
        self.setStyleSheet(
            f"QFrame#DeviceCard {{ background-color:{bg};"
            f" border:1px solid {border}; border-radius:12px; }}"
            f"QFrame#DeviceCard QLabel {{ background:transparent; border:none; }}")


# ==============================================================================
# 5. SƠ ĐỒ ĐẤU NỐI TRỰC QUAN (SHTC30 + relay 3 kênh + tải)
# ==============================================================================
class HardwareDiagram(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(264)
        self.setMinimumWidth(500)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.connected = False
        self.humidity = None
        self.temp = None
        self.slaves = (1, 255)
        self.channels = {"pump": 0, "fan": 1, "lamp": 2}
        self.states = {"pump": False, "fan": False, "lamp": False}
        self.devices = [
            ("pump", "MÁY BƠM", "#4cc9f0"),
            ("fan", "QUẠT LÀM MÁT", "#61dafb"),
            ("lamp", "ĐÈN HALOGEN", "#f9a826"),
        ]

        self._phase = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(80)

    def _tick(self):
        self._phase = (self._phase + 1) % 10000
        self.update()

    def set_slaves(self, sensor_slave=None, relay_slave=None):
        if sensor_slave is not None:
            self.slaves = (int(sensor_slave), self.slaves[1])
        if relay_slave is not None:
            self.slaves = (self.slaves[0], int(relay_slave))
        self.update()

    def set_channels(self, channels):
        self.channels = dict(channels)
        self.update()

    def set_connected(self, c):
        self.connected = bool(c)
        self.update()

    def set_values(self, humidity, temp):
        self.humidity = humidity
        self.temp = temp
        self.update()

    def set_states(self, states):
        if states != self.states:
            self.states = dict(states)
            self.update()

    # ------------------------------ helpers ------------------------------
    @staticmethod
    def _shadow(p, rect, radius=8):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 120))
        p.drawRoundedRect(rect.adjusted(2, 4, 2, 4), radius, radius)

    @staticmethod
    def _led(p, cx, cy, on, color, r=4.0):
        p.setPen(Qt.NoPen)
        if on:
            glow = QColor(color)
            glow.setAlpha(150)
            p.setBrush(glow)
            p.drawEllipse(QPointF(cx, cy), r * 2.2, r * 2.2)
        p.setBrush(QColor(color) if on else QColor(color).darker(320))
        p.drawEllipse(QPointF(cx, cy), r, r)

    def _draw_usb(self, p, rect):
        grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
        grad.setColorAt(0.0, QColor("#3a3a3a"))
        grad.setColorAt(0.3, QColor("#6a6a6a"))
        grad.setColorAt(0.5, QColor("#9a9a9a"))
        grad.setColorAt(0.7, QColor("#6a6a6a"))
        grad.setColorAt(1.0, QColor("#2a2a2a"))
        self._shadow(p, rect, 6)
        p.setBrush(QBrush(grad))
        p.setPen(QPen(QColor("#111"), 1.2))
        p.drawRoundedRect(rect, 6, 6)

        p.setPen(QColor("#e6e6e6"))
        p.setFont(QFont("Segoe UI", 7, QFont.Bold))
        p.drawText(QRectF(rect.left(), rect.top() + 5, rect.width(), 12),
                   Qt.AlignCenter, "USB-RS485")
        p.setPen(QColor("#9aa4b0"))
        p.setFont(QFont("Consolas", 5))
        p.drawText(QRectF(rect.left(), rect.top() + 18, rect.width(), 10),
                   Qt.AlignCenter, "FTDI / CH340")

        # TX / RX
        blink = self.connected and (self._phase % 40) < 8
        for i, lbl in enumerate(("TX", "RX")):
            cx = rect.left() + 16 + i * 20
            cy = rect.top() + 40
            on = blink and ((self._phase + i * 7) % 53) < 12
            self._led(p, cx, cy, on, "#2ecc71", 3.0)
            p.setPen(QColor("#b8c0cc"))
            p.setFont(QFont("Consolas", 5, QFont.Bold))
            p.drawText(QPointF(cx + 5, cy + 3), lbl)

        # Cực A/B
        p.setBrush(QColor("#c9a227"))
        p.setPen(QPen(QColor("#6b5410"), 1))
        for i in range(2):
            p.drawRoundedRect(QRectF(rect.right() - 26 + i * 12,
                                     rect.bottom() - 21, 10, 14), 1, 1)
        p.setPen(QColor("#e8f0f8"))
        p.setFont(QFont("Consolas", 5, QFont.Bold))
        p.drawText(QRectF(rect.right() - 26, rect.bottom() - 35, 22, 10),
                   Qt.AlignCenter, "A  B")

    def _draw_sensor(self, p, rect):
        grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
        grad.setColorAt(0.0, QColor("#f2f5f7"))
        grad.setColorAt(0.45, QColor("#cfd8df"))
        grad.setColorAt(1.0, QColor("#93a1ad"))
        self._shadow(p, rect, 7)
        p.setBrush(QBrush(grad))
        p.setPen(QPen(QColor("#5a6672"), 1.2))
        p.drawRoundedRect(rect, 7, 7)

        # Nhãn SHTC30
        lab = QRectF(rect.left() + 8, rect.top() + 7, rect.width() - 16, 20)
        p.setBrush(QColor("#0e6e3d"))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(lab, 3, 3)
        p.setPen(QColor("#d1ffe1"))
        p.setFont(QFont("Segoe UI", 7, QFont.Bold))
        p.drawText(lab, Qt.AlignCenter, "SHTC30")

        # Khe thông gió
        p.setPen(QPen(QColor("#7b8792"), 1))
        for i in range(5):
            y = rect.top() + 34 + i * 4
            p.drawLine(QPointF(rect.left() + 12, y),
                       QPointF(rect.left() + 58, y))

        # Giá trị đo realtime
        h = f"{self.humidity:.1f}" if self.humidity is not None else "--.-"
        t = f"{self.temp:.1f}" if self.temp is not None else "--.-"
        p.setFont(QFont("Consolas", 9, QFont.Bold))
        p.setPen(QColor("#0d3550"))
        p.drawText(QRectF(rect.left() + 64, rect.top() + 34, 80, 16),
                   Qt.AlignLeft | Qt.AlignVCenter, f"{h} %RH")
        p.setPen(QColor("#7a4a00"))
        p.drawText(QRectF(rect.left() + 64, rect.top() + 52, 80, 16),
                   Qt.AlignLeft | Qt.AlignVCenter, f"{t} °C")

        # LED hoạt động
        on = self.connected and (self._phase % 60) < 30
        self._led(p, rect.right() - 13, rect.bottom() - 12, on, "#00ff88", 3.0)

        # Địa chỉ slave (góc dưới bên trái, trong hộp)
        p.setPen(QColor("#5a6672"))
        p.setFont(QFont("Consolas", 5, QFont.Bold))
        p.drawText(QRectF(rect.left() + 8, rect.bottom() - 16,
                          rect.width() - 30, 10),
                   Qt.AlignLeft | Qt.AlignVCenter,
                   f"SLAVE {self.slaves[0]} · 0x{self.slaves[0]:02X}")

    def _draw_relay(self, p, rect):
        grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
        grad.setColorAt(0, QColor("#147a45"))
        grad.setColorAt(1, QColor("#0a4a29"))
        self._shadow(p, rect, 8)
        p.setBrush(QBrush(grad))
        p.setPen(QPen(QColor("#042010"), 1.5))
        p.drawRoundedRect(rect, 8, 8)

        p.setPen(QColor("#d1ffe1"))
        p.setFont(QFont("Segoe UI", 7, QFont.Bold))
        p.drawText(QRectF(rect.left(), rect.top() + 5, rect.width(), 12),
                   Qt.AlignCenter, "RELAY 4 KÊNH · CH4 DỰ PHÒNG")
        p.setPen(QColor("#8fd3aa"))
        p.setFont(QFont("Consolas", 6))
        p.drawText(QRectF(rect.left(), rect.top() + 18, rect.width(), 10),
                   Qt.AlignCenter,
                   f"SLAVE {self.slaves[1]} · 0x{self.slaves[1]:02X}")

        inner = QRectF(rect.left() + 8, rect.top() + 34,
                       rect.width() - 16, rect.height() - 40)
        row_h = inner.height() / 3.0
        for i, (key, name, color) in enumerate(self.devices):
            row = QRectF(inner.left(), inner.top() + i * row_h + 2,
                         inner.width(), row_h - 4)
            p.setBrush(QColor(0, 0, 0, 70))
            p.setPen(QPen(QColor(255, 255, 255, 18), 1))
            p.drawRoundedRect(row, 5, 5)

            on = bool(self.states.get(key, False))
            ch = int(self.channels.get(key, i))
            self._led(p, row.left() + 15, row.center().y(), on,
                      color, 4.2)

            p.setPen(QColor("#d1ffe1"))
            p.setFont(QFont("Consolas", 8, QFont.Bold))
            p.drawText(QRectF(row.left() + 27, row.top(),
                              42, row.height()),
                       Qt.AlignVCenter | Qt.AlignLeft, f"CH{ch + 1}")

            p.setPen(QColor("#8fd3aa"))
            p.setFont(QFont("Segoe UI", 6))
            p.drawText(QRectF(row.left() + 62, row.top(),
                              row.width() - 92, row.height()),
                       Qt.AlignVCenter | Qt.AlignLeft,
                       "ON" if on else "OFF")

            # Cực ra C / NO
            p.setPen(QPen(QColor("#05341b"), 1))
            p.setBrush(QColor("#d4af37"))
            for k in range(2):
                p.drawEllipse(QPointF(row.right() - 19 + k * 12,
                                      row.center().y()), 3.2, 3.2)

    def _draw_wire(self, p, x1, y, x2, on, color):
        base = QColor("#3a4a60") if not on else QColor(color)
        p.setPen(QPen(base, 3))
        p.drawLine(QPointF(x1, y), QPointF(x2, y))
        if on:
            glow = QColor(color)
            glow.setAlpha(70)
            p.setPen(QPen(glow, 8))
            p.drawLine(QPointF(x1, y), QPointF(x2, y))
            # hạt chạy trên dây
            span = max(1.0, x2 - x1)
            off = (self._phase * 2.5) % span
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 200))
            for d in (0.0, 0.4):
                pos = x1 + ((off + d * span) % span)
                p.drawEllipse(QPointF(pos, y), 1.8, 1.8)
        else:
            p.setPen(QPen(QColor(255, 255, 255, 25), 1))
            p.drawLine(QPointF(x1, y - 1.5), QPointF(x2, y - 1.5))

    def _draw_pump_icon(self, p, cx, cy, on, color):
        r = 17
        p.setPen(QPen(QColor(color) if on else QColor("#3a4a60"), 2))
        p.setBrush(QColor("#0b1a2a"))
        p.drawEllipse(QPointF(cx, cy), r, r)

        ang0 = (self._phase * 9) % 360 if on else 0
        p.setPen(QPen(QColor(color) if on else QColor("#3a4a60"), 2))
        for k in range(3):
            a = math.radians(ang0 + k * 120)
            p.drawLine(QPointF(cx, cy),
                       QPointF(cx + math.cos(a) * (r - 4),
                               cy + math.sin(a) * (r - 4)))
        p.setBrush(QColor(color) if on else QColor("#3a4a60"))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx, cy), 3, 3)

        if on:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(90, 190, 255, 190))
            for i in range(3):
                dy = (self._phase * 2 + i * 9) % 26
                p.drawEllipse(QPointF(cx + r + 6 + i * 3, cy + 12 - dy), 1.8, 3.0)

    def _draw_fan_icon(self, p, cx, cy, on, color):
        r = 17
        p.setPen(QPen(QColor(color) if on else QColor("#3a4a60"), 2))
        p.setBrush(QColor("#0b1a2a"))
        p.drawEllipse(QPointF(cx, cy), r, r)

        ang0 = (self._phase * 14) % 360 if on else 0
        p.setBrush(QColor(color) if on else QColor("#3a4a60"))
        p.setPen(Qt.NoPen)
        for k in range(4):
            a = math.radians(ang0 + k * 90)
            bx = cx + math.cos(a) * 9
            by = cy + math.sin(a) * 9
            p.save()
            p.translate(bx, by)
            p.rotate(math.degrees(a) + 35)
            p.drawEllipse(QRectF(-3.5, -6.5, 7, 13))
            p.restore()
        p.setBrush(QColor("#d7e6f5"))
        p.drawEllipse(QPointF(cx, cy), 2.6, 2.6)

    def _draw_lamp_icon(self, p, cx, cy, on, color):
        r = 13
        p.setPen(QPen(QColor(color) if on else QColor("#3a4a60"), 2))
        p.setBrush(QColor("#241a08") if on else QColor("#0b1a2a"))
        p.drawEllipse(QPointF(cx, cy - 5), r, r)
        # đui đèn
        p.setBrush(QColor("#8a8f98"))
        p.setPen(QPen(QColor("#555"), 1))
        p.drawRoundedRect(QRectF(cx - 6, cy + 7, 12, 8), 2, 2)

        # dây tóc
        p.setPen(QPen(QColor("#ffd27a") if on else QColor("#5a4a2a"), 1.4))
        p.drawLine(QPointF(cx - 4, cy + 1), QPointF(cx - 2, cy - 8))
        p.drawLine(QPointF(cx - 2, cy - 8), QPointF(cx + 2, cy - 8))
        p.drawLine(QPointF(cx + 2, cy - 8), QPointF(cx + 4, cy + 1))

        if on:
            p.setPen(Qt.NoPen)
            for k in range(10):
                a = math.radians(k * 36 + self._phase * 1.5)
                rr = 21 + 5 * math.sin(self._phase * 0.3 + k)
                p.setBrush(QColor(255, 190, 60, 70))
                p.drawEllipse(QPointF(cx + math.cos(a) * rr,
                                      cy - 5 + math.sin(a) * rr), 2.2, 2.2)
            # tia nhiệt
            p.setPen(QPen(QColor(255, 170, 60, 130), 1.5))
            for k in range(2):
                x = cx - 8 + k * 14
                dy = (self._phase * 1.6 + k * 10) % 18
                p.drawLine(QPointF(x, cy + 20 + dy),
                           QPointF(x + 2, cy + 26 + dy))

    def _draw_load(self, p, x, cy, key, name, color):
        on = bool(self.states.get(key, False))
        if key == "pump":
            self._draw_pump_icon(p, x + 20, cy, on, color)
        elif key == "fan":
            self._draw_fan_icon(p, x + 20, cy, on, color)
        else:
            self._draw_lamp_icon(p, x + 20, cy, on, color)

        p.setPen(QColor("#ecf0f1" if on else "#8892b0"))
        p.setFont(QFont("Segoe UI", 8, QFont.Bold))
        p.drawText(QRectF(x + 46, cy - 15, 105, 14),
                   Qt.AlignVCenter | Qt.AlignLeft, name)
        p.setPen(QColor(color) if on else QColor("#5b6b82"))
        p.setFont(QFont("Consolas", 8, QFont.Bold))
        p.drawText(QRectF(x + 46, cy + 1, 105, 14),
                   Qt.AlignVCenter | Qt.AlignLeft,
                   ("● BẬT" if on else "○ TẮT"))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        bg = QLinearGradient(0, 0, 0, h)
        bg.setColorAt(0, QColor("#0a1220"))
        bg.setColorAt(1, QColor("#060a14"))
        p.fillRect(self.rect(), QBrush(bg))

        usb = QRectF(10, 34, 100, 80)
        sensor = QRectF(138, 18, 140, 90)
        relay = QRectF(138, 120, 196, h - 132)

        dx = max(0.0, (w - 528.0) / 2.0)
        p.translate(dx, 0)

        self._draw_usb(p, usb)
        self._draw_sensor(p, sensor)
        self._draw_relay(p, relay)

        # Trục RS485 A/B
        trunk_x = 122
        p.setPen(QPen(QColor("#f39c12"), 2))
        p.drawLine(QPointF(usb.right() + 1, usb.center().y()),
                   QPointF(trunk_x, usb.center().y()))
        p.drawLine(QPointF(trunk_x, sensor.center().y()),
                   QPointF(trunk_x, relay.top() + 34))
        p.drawLine(QPointF(trunk_x, sensor.center().y()),
                   QPointF(sensor.left(), sensor.center().y()))
        p.drawLine(QPointF(trunk_x, relay.top() + 34),
                   QPointF(relay.left(), relay.top() + 34))
        p.setPen(QPen(QColor("#f1c40f"), 2))
        p.drawLine(QPointF(usb.right() + 1, usb.center().y() + 3),
                   QPointF(trunk_x + 3, usb.center().y() + 3))
        p.drawLine(QPointF(trunk_x + 3, sensor.center().y() + 3),
                   QPointF(trunk_x + 3, relay.top() + 37))
        p.drawLine(QPointF(trunk_x + 3, sensor.center().y() + 3),
                   QPointF(sensor.left(), sensor.center().y() + 3))
        p.drawLine(QPointF(trunk_x + 3, relay.top() + 37),
                   QPointF(relay.left(), relay.top() + 37))

        p.setPen(QColor("#8892b0"))
        p.setFont(QFont("Consolas", 6))
        p.drawText(QRectF(usb.left(), usb.bottom() + 7, 132, 12),
                   Qt.AlignLeft, "RS485 · 9600 8N1")

        # Tải + dây ra relay
        row_h = (relay.height() - 40) / 3.0
        load_x = relay.right() + 34
        for i, (key, name, color) in enumerate(self.devices):
            cy = relay.top() + 34 + row_h * (i + 0.5)
            self._draw_wire(p, relay.right(), cy, load_x - 8,
                            self.states.get(key, False), color)
            self._draw_load(p, load_x, cy, key, name, color)

# ==============================================================================
# 6. MODBUS WORKER THREAD — đọc SHTC30 + điều khiển 3 kênh relay
# ==============================================================================
class ModbusWorker(QThread):
    data_ready = pyqtSignal(float, float, dict)   # RH, T, {kênh: bật}
    relay_states = pyqtSignal(dict)               # trạng thái relay xác nhận
    relay_failed = pyqtSignal(int, bool)          # kênh, lệnh mong muốn
    log_event = pyqtSignal(str, str)
    connection_changed = pyqtSignal(bool, str)
    link_changed = pyqtSignal(bool)
    sensor_ok = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self._port = "COM5"
        self._baudrate = 9600
        self._timeout = 1.0
        self._poll_interval = 0.5
        self._retries = 1
        self._sensor_slave = 1
        self._relay_slave = 255
        self._fc_coil = True           # True: Coil FC05 · False: Holding FC06
        self._active_low = False
        self._swap_th = True           # True: reg0 = nhiệt độ, reg1 = độ ẩm — ĐO TRÊN MODULE THẬT: reg0=298 (29.8°C), reg1=538 (53.8%RH)
        self._channels = [0, 1, 2]

        self._running = False
        self._client = None
        #: Nhật ký khung Modbus gửi/nhận trên bus (đẩy lên ERP).
        self._trace = []
        self._trace_max = 40
        self._lock = threading.Lock()
        self._pending = {}
        self._want = {0: False, 1: False, 2: False}
        self._actual = {0: False, 1: False, 2: False}
        self._last_vals = (None, None)
        self._sensor_fc = 4            # 4 = input register, 3 = holding
        self._sensor_fc_auto = False
        self._fc_auto_done = False
        self._write_only = False
        self._read_fails = 0
        self._last_write = {}
        self._last_hb = 0.0
        self._sensor_hint = False
        self._sensor_fail_streak = 0
        self._failsafe_cycles = 1

    # ------------------------------------------------------------------
    def configure(self, port, baud, timeout, poll_interval=0.5, retries=1,
                  sensor_slave=1, relay_slave=255, fc_coil=True,
                  active_low=False, swap_th=True, channels=(0, 1, 2),
                  failsafe_cycles=1):
        with self._lock:
            self._port = port
            self._baudrate = int(baud)
            self._timeout = float(timeout)
            self._poll_interval = float(poll_interval)
            self._retries = int(retries)
            self._sensor_slave = int(sensor_slave)
            self._relay_slave = int(relay_slave)
            self._fc_coil = bool(fc_coil)
            self._active_low = bool(active_low)
            self._swap_th = bool(swap_th)
            self._channels = [int(c) for c in channels]
            self._failsafe_cycles = max(1, int(failsafe_cycles or 1))
        self._fc_auto_done = False
        self._write_only = False
        self._read_fails = 0
        self._sensor_fc = 4
        self._sensor_fc_auto = False
        self._sensor_hint = False
        self._pending.clear()

    def set_command(self, channel, on):
        with self._lock:
            self._pending[int(channel)] = bool(on)

    def stop(self):
        self._running = False

    # ------------------------------------------------------------------
    #  Wrapper chịu được khác biệt chữ ký giữa các bản pymodbus
    # ------------------------------------------------------------------
    def get_trace(self):
        """Bản ghi khung Modbus gần đây (để đẩy lên ERP)."""
        with self._lock:
            return list(self._trace)

    def _push_trace(self, direction, slave, fc, rtu=b"", asc=b"", note="",
                    ok=True):
        """Ghi 1 khung Modbus vào nhật ký (bỏ qua nếu có lỗi ngoài dự kiến)."""
        try:
            rec = modbus_frames.make_record(direction, slave, fc, rtu, asc,
                                            note=note, ok=ok)
        except Exception:
            return
        with self._lock:
            self._trace.append(rec)
            del self._trace[:-self._trace_max]

    def _trace_read(self, fc, slave, addr, count, res):
        """Ghi nhật ký yêu cầu đọc + phản hồi."""
        try:
            rtu, asc = modbus_frames.read_request(slave, fc, addr, count)
            self._push_trace("TX", slave, fc, rtu, asc,
                             note=f"đọc {count} ô @0x{addr:04X}")
            if res is None:
                self._push_trace("RX", slave, fc, b"", b"",
                                 note="không có phản hồi", ok=False)
                return
            if res.isError():
                code = getattr(res, "exception_code", 0)
                rtu, asc = modbus_frames.exception_response(slave, fc, code)
                self._push_trace("RX", slave, fc, rtu, asc,
                                 note=modbus_frames.exc_name(code), ok=False)
                return
            values = getattr(res, "bits", None)
            if values is None:
                values = getattr(res, "registers", []) or []
            rtu, asc = modbus_frames.read_response(slave, fc, list(values))
            self._push_trace("RX", slave, fc, rtu, asc,
                             note=f"{len(values)} giá trị")
        except Exception:
            pass

    def _trace_write(self, fc, slave, addr, value, res, note=""):
        """Ghi nhật ký yêu cầu ghi + phản hồi."""
        try:
            if fc == 5:
                rtu, asc = modbus_frames.write_coil_request(slave, addr,
                                                            bool(value))
            else:
                rtu, asc = modbus_frames.write_register_request(slave, addr,
                                                                value)
            self._push_trace("TX", slave, fc, rtu, asc,
                             note=note or f"ghi 0x{addr:04X}")
            if res is not None and res.isError():
                code = getattr(res, "exception_code", 0)
                rtu, asc = modbus_frames.exception_response(slave, fc, code)
                self._push_trace("RX", slave, fc, rtu, asc,
                                 note=modbus_frames.exc_name(code), ok=False)
            elif res is not None:
                rtu, asc = modbus_frames.read_response(slave, fc, [])
                self._push_trace("RX", slave, fc, rtu, asc, note="đã ghi")
        except Exception:
            pass

    def _read_input_regs(self, addr, count, slave):
        res = self._call_modbus([
            lambda: self._client.read_input_registers(addr, count=count, device_id=slave),
            lambda: self._client.read_input_registers(addr, count=count, slave=slave),
            lambda: self._client.read_input_registers(addr, count=count, unit=slave),
        ])
        self._trace_read(4, slave, addr, count, res)
        return res

    def _read_holding_regs(self, addr, count, slave):
        res = self._call_modbus([
            lambda: self._client.read_holding_registers(addr, count=count, device_id=slave),
            lambda: self._client.read_holding_registers(addr, count=count, slave=slave),
            lambda: self._client.read_holding_registers(addr, count=count, unit=slave),
        ])
        self._trace_read(3, slave, addr, count, res)
        return res

    def _write_reg(self, addr, value, slave):
        res = self._call_modbus([
            lambda: self._client.write_register(addr, value, device_id=slave),
            lambda: self._client.write_register(addr, value, slave=slave),
            lambda: self._client.write_register(addr, value, unit=slave),
        ])
        self._trace_write(6, slave, addr, value, res,
                          note=f"ghi holding 0x{addr:04X} = {value}")
        return res

    def _write_coil(self, addr, on, slave):
        res = self._call_modbus([
            lambda: self._client.write_coil(addr, bool(on), device_id=slave),
            lambda: self._client.write_coil(addr, bool(on), slave=slave),
            lambda: self._client.write_coil(addr, bool(on), unit=slave),
        ])
        self._trace_write(5, slave, addr, bool(on), res,
                          note=f"ghi coil 0x{addr:04X} = "
                               f"{'ON' if on else 'OFF'}")
        return res

    def _read_coils(self, addr, count, slave):
        res = self._call_modbus([
            lambda: self._client.read_coils(addr, count=count, device_id=slave),
            lambda: self._client.read_coils(addr, count=count, slave=slave),
            lambda: self._client.read_coils(addr, count=count, unit=slave),
        ])
        self._trace_read(1, slave, addr, count, res)
        return res

    @staticmethod
    def _call_modbus(calls):
        """Thử lần lượt các chữ ký pymodbus cho tới khi không còn TypeError."""
        last = None
        for fn in calls:
            try:
                return fn()
            except TypeError as exc:
                last = exc
        if last is not None:
            raise last
        return None

    def _make_client(self, port, baud, timeout):
        kw = dict(port=port, baudrate=baud, bytesize=8,
                  parity="N", stopbits=1, timeout=timeout)
        try:
            cli = ModbusSerialClient(retries=self._retries, **kw)
        except TypeError:
            try:
                cli = ModbusSerialClient(method="rtu", **kw)
            except TypeError:
                cli = ModbusSerialClient(**kw)
        try:
            cli.set_max_no_responses(1000000)
        except Exception:
            pass
        return cli

    def _ensure_open(self, port, baud, timeout):
        try:
            if self._client is not None and getattr(self._client, "connected", True):
                return True
        except Exception:
            pass
        try:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
            self._client = self._make_client(port, baud, timeout)
            if self._client.connect():
                self.log_event.emit("OK", f"Đã mở lại {port} (auto-reconnect)")
                return True
            self._client = None
        except Exception as e:
            self.log_event.emit("ERROR", f"Auto-reconnect lỗi: {_short_error(e)}")
            self._client = None
        return False

    # ------------------------------------------------------------------
    #  SHTC30
    # ------------------------------------------------------------------
    def _read_sensor_once(self):
        if self._sensor_fc == 3:
            return self._read_holding_regs(0x0000, 2, self._sensor_slave)
        return self._read_input_regs(0x0000, 2, self._sensor_slave)

    def _read_sensor(self):
        rr = self._read_sensor_once()
        if (rr is None or rr.isError()) and not self._sensor_fc_auto:
            if getattr(rr, "exception_code", None) in (1, 2):
                self._sensor_fc_auto = True
                self._sensor_fc = 3 if self._sensor_fc == 4 else 4
                self.log_event.emit(
                    "WARN",
                    "SHTC30 không hỗ trợ FC04 → tự chuyển sang FC03 "
                    "(holding register).")
                rr = self._read_sensor_once()
        if rr is None or rr.isError():
            raise ModbusException(
                f"Slave {self._sensor_slave} (SHTC30) không phản hồi ({rr})")

        raw0, raw1 = rr.registers[0], rr.registers[1]
        raw_h, raw_t = (raw1, raw0) if self._swap_th else (raw0, raw1)
        humidity = raw_h * 0.1
        temp = (raw_t - 65536) * 0.1 if raw_t > 32767 else raw_t * 0.1
        if not (0.0 <= humidity <= 100.0) or not (-40.0 <= temp <= 125.0):
            raise ModbusException(
                f"giá trị ngoài dải SHTC30 (RH={humidity:.1f} %, T={temp:.1f} °C)")
        return humidity, temp

    # ------------------------------------------------------------------
    #  RELAY
    # ------------------------------------------------------------------
    def _relay_value(self, on):
        on = bool(on)
        return (not on) if self._active_low else on

    def _write_channel_once(self, ch, on):
        val = self._relay_value(on)
        if self._fc_coil:
            return self._write_coil(ch, val, self._relay_slave)
        return self._write_reg(ch, 1 if val else 0, self._relay_slave)

    def _write_channel(self, ch, on):
        wr = self._write_channel_once(ch, on)
        if wr is not None and not wr.isError():
            return
        if (getattr(wr, "exception_code", None) in (1, 2)
                and not self._fc_auto_done):
            self._fc_auto_done = True
            self._fc_coil = not self._fc_coil
            self._read_fails = 0
            self._write_only = False
            self.log_event.emit(
                "WARN",
                "Relay không hỗ trợ "
                + ("Holding FC06" if self._fc_coil else "Coil FC05")
                + " → tự chuyển sang "
                + ("Coil FC05" if self._fc_coil else "Holding FC06") + ".")
            wr = self._write_channel_once(ch, on)
            if wr is not None and not wr.isError():
                return
        raise ModbusException(f"ghi CH{ch + 1} thất bại ({wr})")

    def _read_states_once(self):
        count = max(self._channels) + 1
        if self._fc_coil:
            # Quirk phan cung (README): board Bestep 4 kenh CHI tra loi FC01
            # khi count = 1 hoac 8; count 2/4 thi im lang. Neu doc count=3 thi
            # board khong tra loi -> app tuong mat relay va roi vao che do
            # "chi ghi", mat kha nang xac minh tai nao dang dong.
            read_count = 8 if count <= 8 else count
            rr = self._read_coils(0, read_count, self._relay_slave)
            if rr is None or rr.isError():
                err = ModbusException(f"đọc relay thất bại ({rr})")
                err.code = getattr(rr, "exception_code", None)
                raise err
            bits = list(rr.bits or [])
            return {ch: (bool(bits[ch]) if ch < len(bits) else False)
                    != self._active_low for ch in self._channels}

        rr = self._read_holding_regs(0, count, self._relay_slave)
        if rr is None or rr.isError():
            err = ModbusException(f"đọc relay thất bại ({rr})")
            err.code = getattr(rr, "exception_code", None)
            raise err
        regs = list(rr.registers or [])
        return {ch: ((regs[ch] != 0) if ch < len(regs) else False)
                != self._active_low for ch in self._channels}

    def _read_states(self):
        try:
            return self._read_states_once()
        except ModbusException as e:
            if getattr(e, "code", None) in (1, 2) and not self._fc_auto_done:
                self._fc_auto_done = True
                self._fc_coil = not self._fc_coil
                self.log_event.emit(
                    "WARN",
                    "Relay không hỗ trợ kiểu đọc cũ → tự chuyển sang "
                    + ("Coil FC05" if self._fc_coil else "Holding FC06") + ".")
                return self._read_states_once()
            raise

    # ------------------------------------------------------------------
    def run(self):
        if not PYMODBUS_OK:
            self.connection_changed.emit(False,
                                         "Chưa cài pymodbus (pip install pymodbus)")
            return

        self._running = True
        with self._lock:
            port = (self._port or "").strip()
            baud = self._baudrate
            timeout = self._timeout
            channels = list(self._channels)

        if not port:
            self.connection_changed.emit(False, "Chưa chọn cổng COM.")
            return
        if port.startswith("\\\\.\\"):
            port = port[4:]
            with self._lock:
                self._port = port

        for ch in channels:
            self._want[ch] = False
            self._actual[ch] = False

        try:
            self._client = self._make_client(port, baud, timeout)
            if not self._client.connect():
                available = ", ".join(d for d, _ in scan_serial_ports()) or "không có"
                self.log_event.emit(
                    "ERROR",
                    f"Không mở được {port}. Cổng có thể đang bị app khác giữ "
                    f"(Arduino IDE, Hercules, Tera Term...). Cổng hiện có: {available}")
                self.connection_changed.emit(
                    False, f"Không mở được cổng {port} · đang có: {available}")
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
                return
        except Exception as e:
            self.connection_changed.emit(False, f"Lỗi mở cổng: {e}")
            self._client = None
            return

        self.connection_changed.emit(True, f"Đã kết nối {port} @ {baud} 8N1")
        self.log_event.emit("INFO", f"Đã mở cổng {port} @ {baud} 8N1")

        # ---- Khởi động an toàn: tắt cả 3 kênh relay ----
        self.log_event.emit("WARN", "Khởi động an toàn: ghi TẮT cả 3 kênh relay…")
        for ch in channels:
            try:
                self._write_channel(ch, False)
                self._actual[ch] = False
            except Exception as e:
                self.log_event.emit(
                    "WARN", f"Chưa tắt được CH{ch + 1} khi khởi động: {_short_error(e)}")
        self.relay_states.emit(dict(self._actual))

        fail_streak = 0
        link_prev = None
        sensor_prev = None
        self._sensor_fail_streak = 0

        while self._running:
            t_start = time.time()

            if not self._ensure_open(port, baud, timeout):
                time.sleep(1.0)
                continue

            link_ok = False
            relay_ok = False
            errors = []

            # ---- (1) Lệnh từ UI ----
            with self._lock:
                pending = dict(self._pending)
                self._pending.clear()
            for ch, val in pending.items():
                if ch in self._want:
                    self._want[ch] = bool(val)

            # ---- (2) Liên động an toàn: quạt và đèn halogen ----
            if 1 in self._want and 2 in self._want \
                    and self._want[1] and self._want[2]:
                self._want[2] = False
                self.log_event.emit(
                    "WARN", "⚠ Liên động: quạt và đèn halogen không được bật "
                            "cùng lúc → đã tắt đèn halogen.")

            # ---- (3) Đọc SHTC30 ----
            humidity, temp = self._last_vals
            sensor_now = False
            try:
                humidity, temp = self._read_sensor()
                self._last_vals = (humidity, temp)
                sensor_now = True
                link_ok = True
                self._sensor_fail_streak = 0
                self._sensor_hint = False
            except Exception as e:
                self._sensor_fail_streak += 1
                errors.append(f"Slave {self._sensor_slave} (SHTC30): "
                              f"{_short_error(e)}")

            # ---- (4) Ghi relay khi trạng thái mong muốn khác thực tế ----
            now = time.time()
            for ch in channels:
                if self._want[ch] == self._actual[ch]:
                    continue
                if now - self._last_write.get(ch, 0.0) < 1.0:
                    continue
                self._last_write[ch] = now
                try:
                    self._write_channel(ch, self._want[ch])
                    self._actual[ch] = self._want[ch]
                    relay_ok = True
                    link_ok = True
                    self.relay_states.emit(dict(self._actual))
                    self.log_event.emit(
                        "INFO",
                        f"Relay CH{ch + 1}: "
                        f"{'BẬT' if self._want[ch] else 'TẮT'} "
                        f"(slave {self._relay_slave})")
                except Exception as e:
                    errors.append(f"Relay CH{ch + 1}: {_short_error(e)}")
                    self.relay_failed.emit(ch, bool(self._want[ch]))
                    self._want[ch] = self._actual[ch]

            # ---- (5) Đọc trạng thái relay thật (nếu board hỗ trợ) ----
            if not self._write_only:
                try:
                    st = self._read_states()
                    self._read_fails = 0
                    relay_ok = True
                    link_ok = True
                    if any(st.get(ch) != self._actual.get(ch)
                           for ch in channels):
                        self._actual.update(st)
                        self.relay_states.emit(dict(self._actual))
                except Exception as e:
                    self._read_fails += 1
                    if self._read_fails == 3:
                        self._write_only = True
                        self._last_hb = 0.0
                        self.log_event.emit(
                            "WARN",
                            f"Board relay (slave {self._relay_slave}) không trả lời "
                            "khi ĐỌC trạng thái, nhưng vẫn nhận lệnh GHI → "
                            "chuyển sang chế độ CHỈ GHI (vẫn điều khiển bình thường).")
                    elif self._read_fails < 3:
                        errors.append(f"Slave {self._relay_slave} (relay): "
                                      f"{_short_error(e)}")
            else:
                # Chỉ ghi: gửi nhịp 2,5 s/lần để phát hiện mất kết nối
                now = time.time()
                if now - self._last_hb >= 2.5:
                    self._last_hb = now
                    for ch in channels:
                        try:
                            self._write_channel(ch, self._actual.get(ch, False))
                            relay_ok = True
                            link_ok = True
                        except Exception as e:
                            errors.append(f"Relay CH{ch + 1}: {_short_error(e)}")
                            break

            # ---- (6) Cảm biến mất dữ liệu ----
            # failsafe_cycles = so chu ky doc loi LEN TIEP duoc phep truoc khi
            # coi la mat cam bien (khoa AUTO + tat tai).
            sensor_valid = (self._sensor_fail_streak < self._failsafe_cycles)
            if sensor_valid != sensor_prev:
                self.sensor_ok.emit(sensor_valid)
                if not sensor_valid:
                    self.log_event.emit(
                        "WARN",
                        "Mất dữ liệu cảm biến SHTC30 → KHÓA điều khiển AUTO "
                        "và tắt tải để an toàn.")
                else:
                    self.log_event.emit(
                        "OK", "Cảm biến SHTC30 có dữ liệu trở lại → AUTO hoạt động lại.")
                sensor_prev = sensor_valid

            if (not sensor_valid and relay_ok and not self._sensor_hint
                    and self._sensor_fail_streak >= 2):
                self._sensor_hint = True
                self.log_event.emit(
                    "WARN",
                    f"Board relay (slave {self._relay_slave}) vẫn trả lời → "
                    f"bus RS485 + adapter TỐT. Kiểm tra cảm biến (slave "
                    f"{self._sensor_slave}): (1) nguồn module, (2) dây A/B, "
                    f"(3) địa chỉ slave/baudrate.")

            # ---- (7) Báo cáo dữ liệu ----
            if sensor_now:
                self.data_ready.emit(humidity, temp, dict(self._actual))

            if link_ok:
                if fail_streak >= 3:
                    self.log_event.emit("OK", "Bus Modbus đã có phản hồi trở lại.")
                fail_streak = 0
                if link_prev is not True:
                    self.link_changed.emit(True)
                    link_prev = True
            else:
                fail_streak += 1
                if fail_streak == 1:
                    self.log_event.emit(
                        "ERROR", "Không có phản hồi Modbus — " + " | ".join(errors))
                    self.log_event.emit(
                        "WARN",
                        "Kiểm tra: (1) đảo 2 dây A/B, (2) nối GND chung, "
                        "(3) nguồn module RS485, (4) địa chỉ slave, (5) baudrate. "
                        "Có thể chạy 'python modbus_probe.py' để dò tự động.")
                    self.link_changed.emit(False)
                    link_prev = False
                elif fail_streak % 20 == 0:
                    self.log_event.emit(
                        "ERROR",
                        f"Vẫn không có phản hồi Modbus ({fail_streak} chu kỳ liên tiếp).")

            with self._lock:
                poll = self._poll_interval
            dt = time.time() - t_start
            time.sleep(max(0.05, poll - dt))

        try:
            if self._client:
                self._client.close()
        except Exception:
            pass
        self.connection_changed.emit(False, "Đã ngắt kết nối")


# ==============================================================================
# 7. SIMULATOR — mô phỏng phòng kín để demo không cần phần cứng
# ==============================================================================
class SimulatorWorker(QThread):
    data_ready = pyqtSignal(float, float, dict)
    relay_states = pyqtSignal(dict)
    relay_failed = pyqtSignal(int, bool)
    log_event = pyqtSignal(str, str)
    connection_changed = pyqtSignal(bool, str)
    link_changed = pyqtSignal(bool)
    sensor_ok = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self._running = False
        self._lock = threading.Lock()
        self._poll = 0.5
        self._pending = {}
        self._want = {0: False, 1: False, 2: False}
        self._actual = {0: False, 1: False, 2: False}
        self._rh = 38.0
        self._temp = 27.0
        self._t0 = time.time()

    def configure(self, port, baud, timeout, poll_interval=0.5, retries=1,
                  sensor_slave=1, relay_slave=255, fc_coil=True,
                  active_low=False, swap_th=True, channels=(0, 1, 2)):
        with self._lock:
            self._poll = float(poll_interval)

    def set_command(self, channel, on):
        with self._lock:
            self._pending[int(channel)] = bool(on)

    def stop(self):
        self._running = False

    def run(self):
        self._running = True
        self.connection_changed.emit(True, "SIMULATOR MODE (dữ liệu giả lập)")
        self.log_event.emit("WARN", "Đang chạy SIMULATOR — không dùng cổng thật.")
        self.link_changed.emit(True)
        self.sensor_ok.emit(True)
        self.relay_states.emit(dict(self._actual))

        while self._running:
            with self._lock:
                pending = dict(self._pending)
                self._pending.clear()
            changed = False
            for ch, val in pending.items():
                if ch in self._want and self._want[ch] != val:
                    self._want[ch] = val
                    changed = True

            # Liên động quạt / đèn
            if self._want[1] and self._want[2]:
                self._want[2] = False
                self.log_event.emit(
                    "WARN", "[SIM] Liên động: tắt đèn halogen khi quạt đang chạy.")

            if changed:
                self._actual.update(self._want)
                self.relay_states.emit(dict(self._actual))
                for ch, val in self._want.items():
                    self.log_event.emit(
                        "INFO", f"[SIM] Relay CH{ch + 1}: "
                                f"{'BẬT' if val else 'TẮT'}")

            self._t0 += self._poll
            t = time.time() - self._t0

            # Nhiệt độ phòng: dao động 21..33 °C để thấy cả quạt và đèn
            ambient_t = 27.0 + 6.0 * math.sin(t / 45.0)
            ambient_rh = 46.0 - 6.0 * math.sin(t / 60.0 + 1.0)

            self._rh += (ambient_rh - self._rh) * 0.05
            self._temp += (ambient_t - self._temp) * 0.05

            if self._actual.get(0):
                self._rh += 1.1          # bơm phun sương → ẩm tăng
                self._temp -= 0.10
            if self._actual.get(1):
                self._temp -= 0.45       # quạt làm mát
                self._rh -= 0.10
            if self._actual.get(2):
                self._temp += 0.50       # đèn halogen ủ ấm
                self._rh -= 0.15

            self._rh = max(20.0, min(95.0, self._rh))
            self._temp = max(15.0, min(40.0, self._temp))

            hum = self._rh + random.uniform(-0.25, 0.25)
            temp = self._temp + random.uniform(-0.15, 0.15)
            self.data_ready.emit(hum, temp, dict(self._actual))
            time.sleep(self._poll)

        self.connection_changed.emit(False, "Đã ngắt SIMULATOR")


# ==============================================================================
# 8. CHẨN ĐOÁN BUS (chạy modbus_probe.py, nhả cổng COM cho nó dùng)
# ==============================================================================
class ProbeWorker(QThread):
    line_ready = pyqtSignal(str)
    finished_ok = pyqtSignal(int)

    def __init__(self, port, relay_slave=255, parent=None):
        super().__init__(parent)
        self._port = port
        self._relay_slave = int(relay_slave)

    def run(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "modbus_probe.py")
        if not os.path.exists(script):
            self.line_ready.emit("✘ Không tìm thấy modbus_probe.py cạnh app HMI.")
            self.finished_ok.emit(-1)
            return
        cmd = [sys.executable, "-u", script, "--port", self._port, "--all",
               "--relay-slave", str(self._relay_slave)]
        flags = 0
        if sys.platform.startswith("win"):
            flags = 0x08000000          # CREATE_NO_WINDOW
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=flags,
            )
            for line in proc.stdout:
                self.line_ready.emit(line.rstrip())
            proc.wait()
            self.finished_ok.emit(proc.returncode)
        except Exception as e:
            self.line_ready.emit(f"✘ Lỗi chạy chẩn đoán: {e}")
            self.finished_ok.emit(-1)
        finally:
            try:
                if proc and proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass

# ==============================================================================
# 9. GIAO DIỆN CHÍNH
# ==============================================================================
DARK_QSS = """
QMainWindow, QWidget { background-color: #0b1220; color: #ecf0f1;
    font-family: 'Segoe UI', 'Arial'; font-size: 12px; }
QLabel { background: transparent; }
QGroupBox {
    background-color: #101a2e; border: 1px solid #22314a;
    border-radius: 10px; margin-top: 14px; padding-top: 10px;
    font-weight: 600; color: #8892b0;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
QPushButton {
    background-color: #1e90ff; color: white; border: none;
    border-radius: 6px; padding: 7px 10px; font-weight: 600;
}
QPushButton:hover { background-color: #3aa0ff; }
QPushButton:pressed { background-color: #1060d0; }
QPushButton:disabled { background-color: #26324a; color: #5b6b82; }
QPushButton#DangerBtn { background-color: #e74c3c; }
QPushButton#DangerBtn:hover { background-color: #ff6b5a; }
QPushButton#SuccessBtn { background-color: #2ecc71; color:#062812; }
QPushButton#SuccessBtn:hover { background-color: #4ce08a; }
QPushButton#DangerBtn:disabled, QPushButton#SuccessBtn:disabled {
    background-color: #26324a; color: #5b6b82; }
QPushButton#GhostBtn { background-color: #22314a; color:#ecf0f1; }
QPushButton#GhostBtn:hover { background-color: #2f4367; }
QPushButton#ModeOn { background-color: #1e90ff; color:#ffffff; }
QPushButton#ModeOff { background-color: #18243a; color:#8892b0; }
QComboBox, QLineEdit, QDoubleSpinBox, QSpinBox {
    background-color: #0f1a2e; border: 1px solid #22314a;
    border-radius: 6px; padding: 4px 7px; color: #ecf0f1;
}
QComboBox QAbstractItemView {
    background-color: #0f1a2e; color:#ecf0f1;
    selection-background-color: #1e90ff;
}
QCheckBox { color:#c9d1d9; spacing:6px; }
QCheckBox::indicator { width:15px; height:15px; border-radius:4px;
    border:1px solid #38507a; background:#0f1a2e; }
QCheckBox::indicator:checked { background:#1e90ff; border:1px solid #1e90ff; }
QTextEdit {
    background-color: #0a1424; border: 1px solid #22314a;
    border-radius: 6px; color:#c9d1d9;
    font-family: 'Consolas', monospace; font-size: 11px;
}
QTableWidget {
    background-color: #0a1424; gridline-color:#1c2b45;
    border: 1px solid #22314a; border-radius: 6px; color:#c9d1d9;
    font-family:'Consolas', monospace; font-size: 10px;
}
QTableWidget::item:selected { background-color: #1e90ff; color:#ffffff; }
QHeaderView::section {
    background-color: #16213e; color:#8892b0; border: none;
    padding: 5px; font-weight: 600; font-size:10px;
}
QLabel#TitleLbl { font-size: 17px; font-weight: 800; color:#ecf0f1;
    letter-spacing:1px; }
QLabel#SubtitleLbl { font-size: 10px; color:#8892b0; }
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background:#0b1220; width:9px; margin:0; }
QScrollBar::handle:vertical { background:#2a3a5a; border-radius:4px;
    min-height:30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
"""


def detect_lan_ips():
    """Các địa chỉ IPv4 LAN của máy này (bỏ loopback).

    Dùng để hiện cho người dùng biết nhập gì vào PC2 (MES). Cách nhanh và
    chính xác nhất là "connect" socket UDP (KHÔNG gửi gói tin) để hệ điều
    biết đường ra ngoài, sau đó bổ sung các IP lấy được từ tên máy.
    """
    ips = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.2)
        try:
            sock.connect(("10.255.255.255", 1))     # không gửi gói tin
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.append(ip)
        finally:
            sock.close()
    except Exception:
        pass
    if ips:
        return ips          # đã có IP → KHÔNG gọi DNS (có thể chậm, treo GUI)
    # Chỉ khi không có đường ra ngoài mới thử tra tên máy (DNS có thể chậm).
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                      socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


class MainWindow(QMainWindow):
    PUMP_MAX = 600          # s — watchdog bơm (có thể chỉnh từ web)
    HEAT_MAX = 1800         # s — watchdog đèn halogen (có thể chỉnh từ web)
    BLOCK_AFTER_WD = 120    # s — tạm khóa bật lại sau watchdog

    # Firebase chạy ở thread riêng → phát tín hiệu để về GUI thread
    remote_control = pyqtSignal(object)
    remote_config = pyqtSignal(object)
    fb_status_changed = pyqtSignal(str, bool)
    # Modbus TCP cũng chạy ở thread riêng → mọi thao tác lên GUI (log, label)
    # phải đi qua tín hiệu, không gọi trực tiếp từ thread mạng.
    tcp_rejected = pyqtSignal(str)

    DEVICES = (
        ("pump", "MÁY BƠM", "💧", "#4cc9f0"),
        ("fan", "QUẠT LÀM MÁT", "🌀", "#61dafb"),
        ("lamp", "ĐÈN HALOGEN", "💡", "#f9a826"),
    )

    def __init__(self):
        super().__init__()
        self.setWindowTitle(
            "HMI KHÍ HẬU PHÒNG  •  SHTC30  ·  Relay 3 kênh: Bơm / Quạt / Đèn halogen")
        self.resize(1680, 1010)
        self.setMinimumSize(1440, 900)
        self.setStyleSheet(DARK_QSS)

        # ----- Ngưỡng AUTO -----
        self.rh_on = 40.0        # RH < 40 % → BẬT bơm
        self.rh_off = 45.0       # RH ≥ 45 % → TẮT bơm
        self.fan_on = 32.0       # T > 32 °C → BẬT quạt
        self.fan_off = 30.0      # T ≤ 30 °C → TẮT quạt
        self.heat_on = 22.0      # T < 22 °C → BẬT đèn ủ ấm
        self.heat_off = 25.0     # T ≥ 25 °C → TẮT đèn

        self.mode = "AUTO"
        self.connected = False

        # ----- Kênh relay cho từng tải (0..3 = CH1..CH4) -----
        self.relay_map = {"pump": 0, "fan": 1, "lamp": 2}
        self.ch_state = {0: False, 1: False, 2: False, 3: False}
        self.dev_want = {k: False for k, *_ in self.DEVICES}
        self.dev_start = {k: None for k, *_ in self.DEVICES}
        self.block_until = {k: 0.0 for k, *_ in self.DEVICES}
        self.reasons = {k: "chờ dữ liệu" for k, *_ in self.DEVICES}
        self.last_cmd = {k: 0.0 for k, *_ in self.DEVICES}

        self.sensor_ok = False
        self.last_data_ts = None
        self.worker = None
        self._probe = None
        self.use_simulator = False
        self.cfg_valid = True

        # ----- Trạng thái an toàn / Firebase -----
        self.estop = False
        self.pump_max = self.PUMP_MAX
        self.heat_max = self.HEAT_MAX
        self.failsafe_cycles = 1
        self.last_humidity = None
        self.last_temp = None
        self.fb = None
        self.fb_auto = True
        self.fb_state_text = "chưa bật"
        self.fb_state_ok = False
        self.last_control_ts = 0
        self.last_config_ts = 0

        # ----- Modbus TCP (MES đọc/ghi) -----
        self.tcp = None
        self.tcp_auto = True
        self.tcp_seq = 0

        self.hist_t = deque(maxlen=900)
        self.hist_rh = deque(maxlen=900)
        self.hist_temp = deque(maxlen=900)
        self.t0 = time.time()

        self.csv = CsvLogger(os.path.join(os.getcwd(), "log_khi_hau_phong.csv"))
        self._last_ports = []

        self._build_ui()
        self._load_settings()
        QTimer.singleShot(1500, self._auto_start_firebase)
        QTimer.singleShot(2200, self._auto_start_tcp)

        # Cổng COM tự cập nhật
        self._port_timer = QTimer(self)
        self._port_timer.timeout.connect(self._poll_ports)
        self._port_timer.start(2500)

        # Đồng hồ + tuổi dữ liệu + watchdog
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_ui)
        self._clock_timer.start(1000)

        # Ô địa chỉ PC1 cho PC2 (MES): dò lại mỗi 5 s vì IP có thể đổi
        # (đổi Wi-Fi, cắm Ethernet...). Cũng cập nhật khi đổi cổng/device id.
        self._ip_timer = QTimer(self)
        self._ip_timer.timeout.connect(self._refresh_tcp_addr)
        self._ip_timer.start(5000)
        self.sp_tcp_port.valueChanged.connect(self._refresh_tcp_addr)
        self.sp_tcp_devid.valueChanged.connect(self._refresh_tcp_addr)
        QTimer.singleShot(300, self._refresh_tcp_addr)

        self._watchdog_timer = QTimer(self)
        self._watchdog_timer.timeout.connect(self._watchdog_check)
        self._watchdog_timer.start(1000)

        self.log("INFO", "Sẵn sàng. Chọn cổng COM → CONNECT, hoặc bật SIMULATOR để demo.")
        if not SERIAL_OK:
            self.log("ERROR", "Thiếu pyserial → không quét được cổng COM. "
                              "Chạy: pip install pyserial")
        if not PYMODBUS_OK:
            self.log("ERROR", "Thiếu pymodbus → không kết nối Modbus được. "
                              "Chạy: pip install pymodbus")
        self.log("INFO",
                 "Thứ tự relay: CH1 → BƠM · CH2 → QUẠT · CH3 → ĐÈN HALOGEN "
                 "(CH4 dự phòng).")
        self._refresh_cards()

    # ===============================================================
    #  DỰNG GIAO DIỆN
    # ===============================================================
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(9)

        root.addWidget(self._build_topbar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(9)
        root.addLayout(body, 1)

        # ---------------- Cột trái (cấu hình + ngưỡng + thủ công) ----------------
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(6)
        left.addWidget(self._build_connection_group())
        left.addWidget(self._build_threshold_group())
        left.addWidget(self._build_manual_group())
        left.addWidget(self._build_firebase_group())
        left.addWidget(self._build_tcp_group())
        left.addStretch()
        left_w = QWidget()
        left_w.setLayout(left)
        scroll = QScrollArea()
        scroll.setWidget(left_w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(414)
        body.addWidget(scroll)

        # ---------------- Cột giữa (realtime + sơ đồ + đồ thị) ----------------
        center = QVBoxLayout()
        center.setContentsMargins(0, 0, 0, 0)
        center.setSpacing(9)
        center.addWidget(self._build_gauges_group())
        center.addWidget(self._build_device_row())
        center.addWidget(self._build_diagram_group())
        center.addWidget(self._build_plot_group(), 1)
        center_w = QWidget()
        center_w.setLayout(center)
        body.addWidget(center_w, 1)

        # ---------------- Cột phải (bảng + log) ----------------
        right = QVBoxLayout()
        right.setSpacing(9)
        right.addWidget(self._build_log_group(), 1)
        right_w = QWidget()
        right_w.setLayout(right)
        right_w.setFixedWidth(426)
        body.addWidget(right_w)

    def _build_topbar(self):
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setStyleSheet(
            "QFrame#TopBar { background-color:#101a2e;"
            " border:1px solid #22314a; border-radius:10px; }")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 8, 16, 8)
        lay.setSpacing(12)

        col = QVBoxLayout()
        col.setSpacing(1)
        title = QLabel("🌡  HMI KHÍ HẬU PHÒNG")
        title.setObjectName("TitleLbl")
        col.addWidget(title)
        sub = QLabel("SHTC30 · Đo nhiệt độ & độ ẩm không khí thời gian thực · "
                     "Relay: Bơm / Quạt / Đèn halogen")
        sub.setObjectName("SubtitleLbl")
        col.addWidget(sub)
        lay.addLayout(col)
        lay.addStretch()

        self.lbl_live = QLabel("● SHTC30 · chưa có dữ liệu")
        self.lbl_live.setStyleSheet(
            "color:#5b6b82; font-family:Consolas; font-size:11px;"
            " background:#0b1626; border:1px solid #22314a;"
            " border-radius:6px; padding:5px 10px;")
        lay.addWidget(self.lbl_live)

        lay.addSpacing(6)
        self.led = LEDIndicator(16)
        lay.addWidget(self.led)
        self.lbl_status = QLabel("Chưa kết nối")
        self.lbl_status.setFixedWidth(230)
        self.lbl_status.setStyleSheet("color:#8892b0; font-size:11px;")
        lay.addWidget(self.lbl_status)

        self.lbl_estop = QLabel("⛔ E-STOP")
        self.lbl_estop.setStyleSheet(
            "color:#ffffff; background:#e74c3c; border-radius:6px;"
            " padding:5px 10px; font-weight:700; font-size:11px;")
        self.lbl_estop.setToolTip("Dừng khẩn cấp đang kích hoạt — chỉ chạy lại "
                                 "sau khi bấm GIẢI PHÓNG E-STOP.")
        self.lbl_estop.hide()
        lay.addWidget(self.lbl_estop)

        self.lbl_fb_top = QLabel("☁ chưa bật")
        self.lbl_fb_top.setStyleSheet(
            "color:#5b6b82; font-family:Consolas; font-size:11px;"
            " background:#0b1626; border:1px solid #22314a;"
            " border-radius:6px; padding:5px 10px;")
        lay.addWidget(self.lbl_fb_top)

        self.lbl_clock = QLabel(datetime.now().strftime("%H:%M:%S"))
        self.lbl_clock.setStyleSheet(
            "color:#ecf0f1; font-family:Consolas; font-size:16px;"
            " font-weight:700;")
        lay.addWidget(self.lbl_clock)
        return bar

    # ---------------------- Cột trái: kết nối ----------------------
    def _build_connection_group(self):
        g = QGroupBox("⚙  KẾT NỐI MODBUS RTU")
        v = QGridLayout(g)
        v.setContentsMargins(10, 15, 10, 8)
        v.setVerticalSpacing(3)
        v.setHorizontalSpacing(7)

        v.addWidget(QLabel("Cổng COM:"), 0, 0)
        self.cb_port = QComboBox()
        self.cb_port.setEditable(True)
        self.cb_port.setMinimumWidth(130)
        self.cb_port.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cb_port.setMinimumContentsLength(10)
        self.cb_port.setToolTip("Chọn cổng trong danh sách, hoặc gõ tay tên cổng (vd: COM7)")
        v.addWidget(self.cb_port, 0, 1)

        hb = QHBoxLayout()
        hb.setSpacing(4)
        self.btn_scan = QPushButton("🔄")
        self.btn_scan.setObjectName("GhostBtn")
        self.btn_scan.setFixedWidth(34)
        self.btn_scan.setToolTip("Quét lại các cổng serial đang cắm")
        self.btn_scan.clicked.connect(lambda: self._scan_ports(force=True))
        hb.addWidget(self.btn_scan)

        self.btn_auto_detect = QPushButton("🔍")
        self.btn_auto_detect.setObjectName("GhostBtn")
        self.btn_auto_detect.setFixedWidth(34)
        self.btn_auto_detect.setToolTip("Tự dò cổng có thiết bị Modbus trả lời")
        self.btn_auto_detect.clicked.connect(self._auto_detect_port)
        hb.addWidget(self.btn_auto_detect)
        v.addLayout(hb, 0, 2)

        self.lbl_port_hint = QLabel("Đang quét cổng...")
        self.lbl_port_hint.setWordWrap(True)
        self.lbl_port_hint.setStyleSheet("color:#8892b0; font-size:10px;")
        v.addWidget(self.lbl_port_hint, 1, 0, 1, 3)

        v.addWidget(QLabel("Baudrate:"), 2, 0)
        self.cb_baud = QComboBox()
        self.cb_baud.addItems(["9600", "19200", "38400", "57600", "115200"])
        v.addWidget(self.cb_baud, 2, 1)

        v.addWidget(QLabel("Timeout:"), 2, 2)
        self.cb_timeout = QComboBox()
        self.cb_timeout.addItems(["0.5", "1.0", "1.5", "2.0"])
        v.addWidget(self.cb_timeout, 2, 3)

        v.addWidget(QLabel("Slave cảm biến:"), 3, 0)
        self.sp_sensor_slave = QSpinBox()
        self.sp_sensor_slave.setRange(0, 247)
        self.sp_sensor_slave.setValue(1)
        self.sp_sensor_slave.setToolTip(
            "Địa chỉ Modbus của SHTC30 (mặc định 1).\n"
            "FC04: reg 0 = độ ẩm, reg 1 = nhiệt độ (hoặc ngược lại nếu "
            "tick \"Đảo thứ tự thanh ghi\").")
        v.addWidget(self.sp_sensor_slave, 3, 1)

        v.addWidget(QLabel("Slave relay:"), 3, 2)
        self.sp_relay_slave = QSpinBox()
        self.sp_relay_slave.setRange(0, 255)
        self.sp_relay_slave.setValue(255)
        self.sp_relay_slave.setToolTip(
            "Địa chỉ Modbus của board relay.\n"
            "Board Bestep/CHINALCTECH 4 kênh thường ở 255 (0xFF).")
        v.addWidget(self.sp_relay_slave, 3, 3)

        v.addWidget(QLabel("Chu kỳ poll:"), 4, 0)
        self.cb_poll = QComboBox()
        self.cb_poll.addItems(["0.5", "1.0", "2.0", "3.0"])
        self.cb_poll.setCurrentText("0.5")
        v.addWidget(self.cb_poll, 4, 1)

        v.addWidget(QLabel("Thử lại:"), 4, 2)
        self.cb_retries = QComboBox()
        self.cb_retries.addItems(["0", "1", "2", "3"])
        self.cb_retries.setCurrentText("1")
        self.cb_retries.setToolTip("Số lần thử lại mỗi request Modbus.")
        v.addWidget(self.cb_retries, 4, 3)

        v.addWidget(QLabel("Kiểu relay:"), 5, 0)
        self.cb_relay_mode = QComboBox()
        self.cb_relay_mode.addItems(["Coil (FC05)", "Holding Reg (FC06)"])
        self.cb_relay_mode.setToolTip(
            "Board 4 kênh đời mới thường dùng Coil FC05.\n"
            "Nếu sai, app tự thử kiểu còn lại khi relay báo lỗi chức năng.")
        v.addWidget(self.cb_relay_mode, 5, 1, 1, 3)

        self.chk_active_low = QCheckBox("Đảo mức relay (active-LOW)")
        self.chk_active_low.setToolTip(
            "Chỉ tick khi board relay kích mức THẤP (0 = đóng relay).\n"
            "Đa số board Modbus để 1 = đóng relay → để trống.")
        v.addWidget(self.chk_active_low, 6, 0, 1, 4)

        self.chk_swap_th = QCheckBox("Đảo thứ tự thanh ghi (reg0 = nhiệt độ)")
        self.chk_swap_th.setChecked(True)
        self.chk_swap_th.setToolTip(
            "Module SHTC30-RS485 của bạn trả reg0 = nhiệt độ, reg1 = độ ẩm (đã đo xác nhận) → mặc định TICK.\n"
            "Bỏ tick nếu đổi sang module trả chuẩn ngược (reg0 = độ ẩm).")
        v.addWidget(self.chk_swap_th, 7, 0, 1, 4)

        # Kênh relay cho từng tải
        ch_row = QHBoxLayout()
        ch_row.setSpacing(6)
        ch_row.addWidget(QLabel("Kênh:"))
        self.sp_ch_pump = QSpinBox()
        self.sp_ch_fan = QSpinBox()
        self.sp_ch_lamp = QSpinBox()
        for sp, val in ((self.sp_ch_pump, 1), (self.sp_ch_fan, 2),
                        (self.sp_ch_lamp, 3)):
            sp.setRange(1, 4)
            sp.setValue(val)
            sp.setPrefix("CH")
            sp.setFixedWidth(62)
        self.sp_ch_pump.setToolTip("Máy bơm (mặc định CH1)")
        self.sp_ch_fan.setToolTip("Quạt làm mát (mặc định CH2)")
        self.sp_ch_lamp.setToolTip("Đèn halogen ủ ấm (mặc định CH3)")
        ch_row.addWidget(self.sp_ch_pump)
        ch_row.addWidget(self.sp_ch_fan)
        ch_row.addWidget(self.sp_ch_lamp)
        v.addLayout(ch_row, 7, 0, 1, 4)

        self.btn_connect = QPushButton("🔌  CONNECT")
        self.btn_connect.clicked.connect(self.toggle_connection)
        v.addWidget(self.btn_connect, 8, 0, 1, 4)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_sim = QPushButton("🧪 SIMULATOR")
        self.btn_sim.setCheckable(True)
        self.btn_sim.setObjectName("GhostBtn")
        self.btn_sim.setToolTip("Bật để test UI không cần phần cứng thật")
        self.btn_sim.clicked.connect(self._toggle_sim)
        row.addWidget(self.btn_sim)

        self.btn_diag = QPushButton("🔧 CHẨN ĐOÁN")
        self.btn_diag.setObjectName("GhostBtn")
        self.btn_diag.setToolTip(
            "Tự quét RTU 0..255, ASCII và thử ghi relay.\n"
            "App tự ngắt kết nối để nhường cổng COM cho tiến trình chẩn đoán.")
        self.btn_diag.clicked.connect(self._diagnose_bus)
        row.addWidget(self.btn_diag)
        v.addLayout(row, 9, 0, 1, 4)

        self._scan_ports(force=True)
        return g

    # ---------------------- Cột trái: ngưỡng AUTO ----------------------
    @staticmethod
    def _mk_spin(value, lo, hi, suffix, prefix, color):
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setSingleStep(0.5)
        sp.setDecimals(1)
        sp.setSuffix(suffix)
        sp.setPrefix(prefix)
        sp.setValue(value)
        sp.setFixedWidth(120)
        sp.setStyleSheet(
            f"QDoubleSpinBox {{ color:{color}; font-weight:700; font-size:11px; }}")
        return sp

    def _build_threshold_group(self):
        g = QGroupBox("🎯  NGƯỠNG ĐIỀU KHIỂN AUTO")
        v = QVBoxLayout(g)
        v.setContentsMargins(10, 15, 10, 8)
        v.setSpacing(4)

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(0, 1)

        hdr1 = QLabel("BẬT KHI")
        hdr2 = QLabel("TẮT KHI")
        for lb in (hdr1, hdr2):
            lb.setStyleSheet("color:#5b6b82; font-size:9px; font-weight:700;")
        grid.addWidget(hdr1, 0, 1)
        grid.addWidget(hdr2, 0, 2)

        self.sp_rh_on = self._mk_spin(self.rh_on, 0, 100, " %", "RH< ", "#4cc9f0")
        self.sp_rh_off = self._mk_spin(self.rh_off, 0, 100, " %", "RH≥ ", "#2ecc71")
        self.sp_fan_on = self._mk_spin(self.fan_on, -10, 60, " °C", "T> ", "#e74c3c")
        self.sp_fan_off = self._mk_spin(self.fan_off, -10, 60, " °C", "T≤ ", "#f39c12")
        self.sp_heat_on = self._mk_spin(self.heat_on, -10, 60, " °C", "T< ", "#61dafb")
        self.sp_heat_off = self._mk_spin(self.heat_off, -10, 60, " °C", "T≥ ", "#f9a826")

        self.sp_rh_on.setToolTip("Độ ẩm dưới ngưỡng này → BẬT máy bơm.")
        self.sp_rh_off.setToolTip("Độ ẩm đạt ngưỡng này → TẮT máy bơm (hysteresis).")
        self.sp_fan_on.setToolTip("Nhiệt độ vượt ngưỡng này → BẬT quạt làm mát.")
        self.sp_fan_off.setToolTip("Nhiệt độ hạ xuống ngưỡng này → TẮT quạt.")
        self.sp_heat_on.setToolTip("Nhiệt độ xuống dưới ngưỡng này → BẬT đèn halogen ủ ấm.")
        self.sp_heat_off.setToolTip("Nhiệt độ đạt ngưỡng này → TẮT đèn halogen.")

        rows = (
            ("💧 MÁY BƠM", self.sp_rh_on, self.sp_rh_off),
            ("🌀 QUẠT MÁT", self.sp_fan_on, self.sp_fan_off),
            ("💡 ĐÈN HALOGEN", self.sp_heat_on, self.sp_heat_off),
        )
        for i, (name, sp_on, sp_off) in enumerate(rows, start=1):
            lb = QLabel(name)
            lb.setStyleSheet("color:#c9d1d9; font-size:11px; font-weight:600;")
            grid.addWidget(lb, i, 0)
            grid.addWidget(sp_on, i, 1)
            grid.addWidget(sp_off, i, 2)
        v.addLayout(grid)

        hint = QLabel(
            "Hysteresis: bơm bật khi ẩm thấp, chỉ tắt khi ẩm hồi đủ. "
            "Quạt và đèn halogen có khóa liên động (không bật cùng lúc).")
        hint.setStyleSheet("color:#5b6b82; font-size:10px;")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.lbl_warn = QLabel("")
        self.lbl_warn.setStyleSheet("color:#f39c12; font-size:10px;")
        self.lbl_warn.setWordWrap(True)
        v.addWidget(self.lbl_warn)

        for sp in (self.sp_rh_on, self.sp_rh_off, self.sp_fan_on,
                   self.sp_fan_off, self.sp_heat_on, self.sp_heat_off):
            sp.valueChanged.connect(self._on_threshold_changed)

        self._on_threshold_changed()
        return g

    # ---------------------- Cột trái: điều khiển thủ công ----------------------
    def _build_manual_group(self):
        g = QGroupBox("🎛  ĐIỀU KHIỂN")
        v = QVBoxLayout(g)
        v.setContentsMargins(10, 15, 10, 8)
        v.setSpacing(4)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("Chế độ:"))
        self.btn_auto = QPushButton("AUTO")
        self.btn_auto.setCheckable(True)
        self.btn_auto.setChecked(True)
        self.btn_auto.setObjectName("ModeOn")
        self.btn_auto.clicked.connect(lambda: self._set_mode("AUTO"))
        self.btn_man = QPushButton("MANUAL")
        self.btn_man.setCheckable(True)
        self.btn_man.setObjectName("ModeOff")
        self.btn_man.clicked.connect(lambda: self._set_mode("MANUAL"))
        row.addWidget(self.btn_auto, 1)
        row.addWidget(self.btn_man, 1)
        v.addLayout(row)

        self.btn_on = {}
        self.btn_off = {}
        for key, name, icon, color in self.DEVICES:
            row = QHBoxLayout()
            row.setSpacing(5)
            lb = QLabel(f"{icon} {name}")
            lb.setStyleSheet(f"color:{color}; font-size:11px; font-weight:600;")
            lb.setFixedWidth(120)
            row.addWidget(lb)

            b_on = QPushButton("BẬT")
            b_on.setObjectName("SuccessBtn")
            b_on.clicked.connect(lambda _=False, k=key: self._manual(k, True))
            row.addWidget(b_on)

            b_off = QPushButton("TẮT")
            b_off.setObjectName("DangerBtn")
            b_off.clicked.connect(lambda _=False, k=key: self._manual(k, False))
            row.addWidget(b_off)

            self.btn_on[key] = b_on
            self.btn_off[key] = b_off
            v.addLayout(row)

        self.btn_all_off = QPushButton("⛔  TẮT TẤT CẢ TẢI")
        self.btn_all_off.setObjectName("DangerBtn")
        self.btn_all_off.setToolTip("Tắt khẩn cấp cả bơm, quạt và đèn halogen.")
        self.btn_all_off.clicked.connect(self._all_off)
        v.addWidget(self.btn_all_off)

        estop_row = QHBoxLayout()
        estop_row.setSpacing(5)
        self.btn_estop = QPushButton("🛑  E-STOP")
        self.btn_estop.setObjectName("DangerBtn")
        self.btn_estop.setToolTip("Khóa điều khiển, tắt toàn bộ tải và giữ trạng "
                                  "thái dừng khẩn cấp cho tới khi giải phóng.")
        self.btn_estop.clicked.connect(self._estop_now)
        estop_row.addWidget(self.btn_estop)
        self.btn_release_estop = QPushButton("✔  GIẢI PHÓNG")
        self.btn_release_estop.setObjectName("SuccessBtn")
        self.btn_release_estop.setToolTip("Bỏ khóa E-STOP và trở lại chế độ AUTO.")
        self.btn_release_estop.clicked.connect(lambda: self._release_estop())
        estop_row.addWidget(self.btn_release_estop)
        v.addLayout(estop_row)

        self.lbl_wd = QLabel(
            f"🛡 Watchdog: bơm {self.pump_max}s · đèn {self.heat_max}s")
        self.lbl_wd.setStyleSheet("color:#5b6b82; font-size:10px;")
        self.lbl_wd.setWordWrap(True)
        v.addWidget(self.lbl_wd)

        self._update_manual_buttons()
        return g

    # ---------------------- Cột trái: Firebase (ERP) ----------------------
    def _build_firebase_group(self):
        g = QGroupBox("☁  ĐỒNG BỘ FIREBASE · ERP")
        v = QGridLayout(g)
        v.setContentsMargins(10, 15, 10, 8)
        v.setVerticalSpacing(5)
        v.setHorizontalSpacing(7)

        v.addWidget(QLabel("DB URL:"), 0, 0)
        self.ed_fb_url = QLineEdit(
            "https://ktma-f2871-default-rtdb.asia-southeast1.firebasedatabase.app")
        self.ed_fb_url.setPlaceholderText(
            "https://<project>-default-rtdb.firebaseio.com")
        self.ed_fb_url.setToolTip(
            "Firebase Console → Realtime Database → copy URL ở đầu trang.")
        v.addWidget(self.ed_fb_url, 0, 1, 1, 2)

        v.addWidget(QLabel("Secret:"), 1, 0)
        self.ed_fb_auth = QLineEdit()
        self.ed_fb_auth.setEchoMode(QLineEdit.Password)
        self.ed_fb_auth.setPlaceholderText("để trống nếu rules mở")
        self.ed_fb_auth.setToolTip(
            "Chỉ cần khi rules yêu cầu xác thực (Database secret).\n"
            "Không lưu vào file cấu hình.")
        v.addWidget(self.ed_fb_auth, 1, 1, 1, 2)

        v.addWidget(QLabel("Thiết bị:"), 2, 0)
        self.ed_fb_device = QLineEdit("vuon-1")
        self.ed_fb_device.setToolTip(
            "Nhóm dữ liệu /hmi/<mã>/… phải trùng với web ERP.")
        v.addWidget(self.ed_fb_device, 2, 1)

        self.chk_fb_auto = QCheckBox("Tự bật")
        self.chk_fb_auto.setChecked(True)
        self.chk_fb_auto.setToolTip("Tự bật đồng bộ mỗi khi mở HMI.")
        v.addWidget(self.chk_fb_auto, 2, 2)

        row = QHBoxLayout()
        row.setSpacing(5)
        self.btn_fb = QPushButton("☁  BẬT ĐỒNG BỘ")
        self.btn_fb.setObjectName("GhostBtn")
        self.btn_fb.setCheckable(True)
        self.btn_fb.setToolTip(
            "Đẩy telemetry + trạng thái 3 kênh lên Firebase và nhận lệnh / "
            "ngưỡng từ web ERP. Độc lập với kết nối Modbus.")
        self.btn_fb.clicked.connect(self._toggle_firebase)
        row.addWidget(self.btn_fb, 1)
        self.btn_fb_ping = QPushButton("↻")
        self.btn_fb_ping.setObjectName("GhostBtn")
        self.btn_fb_ping.setFixedWidth(34)
        self.btn_fb_ping.setToolTip("Kiểm tra nhanh Firebase và làm mới trạng thái.")
        self.btn_fb_ping.clicked.connect(self._fb_ping)
        row.addWidget(self.btn_fb_ping)
        v.addLayout(row, 3, 0, 1, 3)

        self.lbl_fb = QLabel("chưa bật")
        self.lbl_fb.setStyleSheet("color:#8892b0; font-size:11px;")
        self.lbl_fb.setWordWrap(True)
        v.addWidget(self.lbl_fb, 4, 0, 1, 2)

        self.lbl_fb_stats = QLabel("2.0 s/chu kỳ · -- ms")
        self.lbl_fb_stats.setStyleSheet(
            "color:#5b6b82; font-family:Consolas; font-size:10px;")
        v.addWidget(self.lbl_fb_stats, 4, 2)

        self.remote_control.connect(self._on_remote_control)
        self.remote_config.connect(self._on_remote_config)
        self.fb_status_changed.connect(self._on_fb_status)
        self.tcp_rejected.connect(self._on_tcp_rejected)
        return g

    # ---------------------- Modbus TCP (PC2 đọc/ghi) ----------------------
    def _build_tcp_group(self):
        g = QGroupBox("🌐  MODBUS TCP · PC2 ĐỌC/GHI")
        v = QGridLayout(g)
        v.setContentsMargins(10, 15, 10, 8)
        v.setVerticalSpacing(5)
        v.setHorizontalSpacing(7)

        v.addWidget(QLabel("Nhập ở PC2:"), 0, 0)
        self.ed_tcp_addr = QLineEdit()
        self.ed_tcp_addr.setReadOnly(True)
        self.ed_tcp_addr.setPlaceholderText("đang dò IP...")
        self.ed_tcp_addr.setToolTip(
            "Địa chỉ PC1 (IP của máy này) + cổng Modbus TCP.\n"
            "Gõ đúng chuỗi này vào PC2 (MES) để đọc/ghi.")
        v.addWidget(self.ed_tcp_addr, 0, 1)
        self.btn_tcp_copy = QPushButton("📋")
        self.btn_tcp_copy.setObjectName("GhostBtn")
        self.btn_tcp_copy.setFixedWidth(38)
        self.btn_tcp_copy.setToolTip("Sao chép địa chỉ để dán vào PC2.")
        self.btn_tcp_copy.clicked.connect(self._copy_tcp_addr)
        v.addWidget(self.btn_tcp_copy, 0, 2)
        self.lbl_tcp_devid = QLabel("dev --")
        self.lbl_tcp_devid.setStyleSheet(
            "color:#5b6b82; font-family:Consolas; font-size:9px;")
        self.lbl_tcp_devid.setToolTip("Device ID mà PC2 dùng.")
        v.addWidget(self.lbl_tcp_devid, 0, 3)

        v.addWidget(QLabel("Cổng:"), 1, 0)
        self.sp_tcp_port = QSpinBox()
        self.sp_tcp_port.setRange(1, 65535)
        self.sp_tcp_port.setValue(502)
        self.sp_tcp_port.setToolTip(
            "502 = cổng chuẩn Modbus TCP.\n"
            "Đổi nếu bị chiếm (vd 1502). PC2 phải trỏ cùng cổng.")
        v.addWidget(self.sp_tcp_port, 1, 1)

        v.addWidget(QLabel("Device ID:"), 1, 2)
        self.sp_tcp_devid = QSpinBox()
        self.sp_tcp_devid.setRange(1, 255)
        self.sp_tcp_devid.setValue(254)
        self.sp_tcp_devid.setToolTip(
            "Địa chỉ slave mà PC2 dùng khi đọc/ghi.\n"
            "0 = catch-all, 1 = SHTC30 mirror.")
        v.addWidget(self.sp_tcp_devid, 1, 3)

        self.chk_tcp_auto = QCheckBox("Tự bật")
        self.chk_tcp_auto.setChecked(True)
        self.chk_tcp_auto.setToolTip("Tự mở cổng TCP mỗi khi mở HMI.")
        v.addWidget(self.chk_tcp_auto, 2, 0)

        self.btn_tcp = QPushButton("🌐  BẬT CỔNG TCP")
        self.btn_tcp.setObjectName("GhostBtn")
        self.btn_tcp.setCheckable(True)
        self.btn_tcp.setToolTip(
            "Mở Modbus TCP server để PC2 đọc telemetry và điều khiển.\n"
            "Lệnh từ PC2 đi qua đúng logic an toàn của HMI "
            "(E-STOP, liên động quạt↔đèn, watchdog).")
        self.btn_tcp.clicked.connect(self._toggle_tcp)
        v.addWidget(self.btn_tcp, 2, 1, 1, 3)

        v.addWidget(QLabel("IP cho phép:"), 3, 0)
        self.ed_tcp_allow = QLineEdit()
        self.ed_tcp_allow.setPlaceholderText(
            "vd 192.168.1.50, 10.0.0.0/8 · để trống = cho tất cả")
        self.ed_tcp_allow.setToolTip(
            "Modbus TCP không có xác thực → chỉ cho phép IP của MES.\n"
            "Nhiều IP cách nhau bằng dấu phẩy. Hỗ trợ CIDR.")
        v.addWidget(self.ed_tcp_allow, 3, 1, 1, 3)

        row2 = QHBoxLayout()
        row2.setSpacing(5)
        self.btn_tcp_export = QPushButton("📋  XUẤT TAG CHO MES")
        self.btn_tcp_export.setObjectName("GhostBtn")
        self.btn_tcp_export.setToolTip(
            "Xuất bảng thanh ghi ra CSV/JSON để MES import tag.")
        self.btn_tcp_export.clicked.connect(self._export_tcp_contract)
        row2.addWidget(self.btn_tcp_export, 1)
        v.addLayout(row2, 4, 0, 1, 4)

        self.lbl_tcp = QLabel("chưa bật")
        self.lbl_tcp.setStyleSheet("color:#8892b0; font-size:11px;")
        self.lbl_tcp.setWordWrap(True)
        v.addWidget(self.lbl_tcp, 5, 0, 1, 4)

        self.lbl_tcp_map = QLabel(
            "IR0 RH×10 · IR1 T×10 · IR2-4 bơm/quạt/đèn · IR5 mode · "
            "IR6 E-STOP\n"
            "IR7 flags · IR8-9 uptime · IR10 data_age · IR11 seq\n"
            "HR0-5 ngưỡng · HR6-8 watchdog · HR9 mode\n"
            "Coil0 bơm · Coil1 quạt · Coil2 đèn · Coil3 E-STOP")
        self.lbl_tcp_map.setStyleSheet(
            "color:#5b6b82; font-family:Consolas; font-size:9px;")
        v.addWidget(self.lbl_tcp_map, 6, 0, 1, 4)
        return g

    def _refresh_tcp_addr(self):
        """Cập nhật ô địa chỉ PC1 + cổng TCP (để người dùng nhập ở PC2)."""
        try:
            ips = detect_lan_ips()
        except Exception:
            ips = []
        try:
            port = int(self.sp_tcp_port.value())
            devid = int(self.sp_tcp_devid.value())
        except Exception:
            port, devid = 502, 254
        self.lbl_tcp_devid.setText(f"dev {devid}")
        if ips:
            self.ed_tcp_addr.setText(f"{ips[0]}:{port}")
            extra = ("\nCác IP khác: " + ", ".join(ips[1:])) if len(ips) > 1 else ""
            self.ed_tcp_addr.setToolTip(
                f"Gõ vào PC2 (MES): {ips[0]}:{port}{extra}"
                f"\nDevice ID: {devid}")
        else:
            self.ed_tcp_addr.setText("không lấy được IP")
            self.ed_tcp_addr.setToolTip(
                "Không dò được IP của máy này (kiểm tra Wi-Fi/Ethernet).\n"
                "Có thể thử tên máy: " + socket.gethostname())

    def _copy_tcp_addr(self):
        """Sao chép địa chỉ PC1 cho người dùng dán vào PC2."""
        text = self.ed_tcp_addr.text().strip()
        if not text or text.startswith("không"):
            self.log("WARN", "Chưa lấy được IP để sao chép.")
            return
        try:
            QApplication.clipboard().setText(text)
            self.log("OK", f"Đã sao chép địa chỉ: {text}")
        except Exception as exc:
            self.log("WARN", f"Không sao chép được: {exc}")

    def _toggle_tcp(self):
        if self.btn_tcp.isChecked():
            self._start_tcp()
        else:
            self._stop_tcp()

    def _start_tcp(self):
        if not TCP_OK:
            self.log("ERROR", "Thiếu modbus_tcp_gateway.py — không mở được cổng TCP.")
            self.btn_tcp.setChecked(False)
            return
        self._stop_tcp(quiet=True)
        port = int(self.sp_tcp_port.value())
        devid = int(self.sp_tcp_devid.value())
        allow = [x.strip() for x in self.ed_tcp_allow.text().split(",")
                 if x.strip()]
        self.tcp = ModbusTcpGateway(
            host="0.0.0.0", port=port, device_id=devid,
            allowed_ips=allow,
            on_command=lambda c: self.remote_control.emit(
                dict(c, by="modbus-tcp", ts=int(time.time() * 1000))),
            on_config=lambda c: self.remote_config.emit(
                dict(c, by="modbus-tcp", ts=int(time.time() * 1000))),
            on_reject=lambda ip: self.tcp_rejected.emit(str(ip or "?")),
        )
        if not self.tcp.start():
            err = self.tcp.error or "không rõ"
            self.log("ERROR", f"Modbus TCP không khởi động được: {err}")
            self.tcp = None
            self.btn_tcp.setChecked(False)
            self.lbl_tcp.setText(f"lỗi: {err}")
            self.lbl_tcp.setStyleSheet("color:#e74c3c; font-size:11px;")
            if "chiếm" in err:
                QMessageBox.warning(
                    self, "Cổng đang bị chiếm",
                    f"{err}\n\n"
                    "Có thể app HMI khác đang mở cùng cổng này.\n"
                    "Đóng app cũ, hoặc đổi sang cổng khác (vd 1502) — "
                    "nhớ báo MES dùng cổng mới.")
            return
        self.btn_tcp.setChecked(True)
        self.btn_tcp.setText("🌐  ĐANG MỞ CỔNG TCP")
        self.lbl_tcp.setText(f"đang lắng nghe 0.0.0.0:{port} · device {devid}")
        self.lbl_tcp.setStyleSheet("color:#2ecc71; font-size:11px;")
        allow_txt = (", ".join(allow) if allow else "mọi IP")
        self.log("OK", f"🌐 Modbus TCP đang lắng nghe cổng {port} "
                       f"(device {devid}) · IP cho phép: {allow_txt}")
        self._save_settings()
        self._push_tcp_state()

    def _stop_tcp(self, quiet=False):
        if self.tcp is not None:
            try:
                self.tcp.stop()
            except Exception:
                pass
            self.tcp = None
        if quiet:
            return
        self.btn_tcp.setChecked(False)
        self.btn_tcp.setText("🌐  BẬT CỔNG TCP")
        self.lbl_tcp.setText("đã tắt cổng TCP")
        self.lbl_tcp.setStyleSheet("color:#8892b0; font-size:11px;")
        self.log("INFO", "Đã đóng cổng Modbus TCP.")

    def _auto_start_tcp(self):
        if self.tcp is None and self.tcp_auto:
            self._start_tcp()

    def _on_tcp_rejected(self, ip):
        """IP lạ bị từ chối (tín hiệu từ thread Modbus TCP → GUI thread)."""
        self.log("WARN", f"🌐 Từ chối kết nối Modbus TCP từ IP {ip} "
                         f"(không nằm trong danh sách cho phép).")

    def _push_tcp_state(self):
        """Đẩy trạng thái sống sang Modbus TCP (đọc từ MES)."""
        if self.tcp is None:
            return
        try:
            payload = self._fb_live_payload()
            payload["connected"] = payload.get("modbus_connected", False)
            payload["cfg_valid"] = bool(self.cfg_valid)
            # seq chi tang khi co MAU MOI (xem _on_data) — neu tang moi lan
            # day trang thai thi MES tuong du lieu moi lien tuc va bo qua
            # kiem tra "trung mau".
            payload["seq"] = self.tcp_seq
            age = (time.time() - self.last_data_ts
                   if self.last_data_ts else 65535)
            payload["data_age_s"] = int(min(age, 65535))
            self.tcp.update(payload)
        except Exception:
            pass

    def _export_tcp_contract(self):
        """Xuất bảng thanh ghi cho MES (CSV + JSON)."""
        try:
            from modbus_tcp_gateway import (build_tag_contract,
                                            contract_to_csv, contract_to_json)
        except Exception as e:
            self.log("ERROR", f"Không xuất được contract: {e}")
            return
        devid = int(self.sp_tcp_devid.value())
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "mes_tag_contract")
        try:
            contract = build_tag_contract(devid)
            with open(base + ".csv", "w", encoding="utf-8-sig",
                      newline="") as f:
                f.write(contract_to_csv(contract))
            with open(base + ".json", "w", encoding="utf-8") as f:
                f.write(contract_to_json(contract))
        except Exception as e:
            self.log("ERROR", f"Không ghi được file contract: {e}")
            return
        self.log("OK", f"Đã xuất contract cho MES: {base}.csv / {base}.json "
                       f"({len(contract)} tag).")

    # ---------------------- Cột giữa: đồng hồ realtime ----------------------
    # ---------------------- Firebase (ERP) ----------------------
    def _toggle_firebase(self):
        if self.btn_fb.isChecked():
            self._start_firebase()
        else:
            self._stop_firebase()

    def _auto_start_firebase(self):
        if self.fb is None and self.fb_auto and \
                self.ed_fb_url.text().strip().lower().startswith("http"):
            self._start_firebase()

    def _start_firebase(self):
        url = self.ed_fb_url.text().strip()
        if not url.lower().startswith("http"):
            self.log("ERROR", "Chưa nhập URL Realtime Database "
                              "(dạng https://<project>-default-rtdb.firebaseio.com)")
            self.btn_fb.setChecked(False)
            return
        try:
            from firebase_sync import FirebaseSync
        except Exception as e:
            self.log("ERROR", f"Không nạp được firebase_sync.py: {e}")
            self.btn_fb.setChecked(False)
            return

        device = self.ed_fb_device.text().strip() or "vuon-1"
        self._stop_firebase(quiet=True)
        self.fb = FirebaseSync(
            on_control=lambda c: self.remote_control.emit(c),
            on_config=lambda c: self.remote_config.emit(c),
            on_status=lambda t, ok: self.fb_status_changed.emit(t, ok),
            last_control_ts=self.last_control_ts,
            last_config_ts=self.last_config_ts,
            device=device,
        )
        self.fb.configure(url, self.ed_fb_auth.text().strip(), device, True,
                          interval=2.0, history_interval=20.0)
        self.fb.start()
        self.btn_fb.setChecked(True)
        self.btn_fb.setText("☁  ĐANG ĐỒNG BỘ")
        self.fb_state_text = "đang kết nối…"
        self.fb_state_ok = False
        self.lbl_fb.setText("đang kết nối…")
        self.lbl_fb.setStyleSheet("color:#f39c12; font-size:11px;")
        self._refresh_fb_top()
        self._save_settings()
        self.log("INFO", f"Bật đồng bộ Firebase · thiết bị '{device}'")
        self._push_fb_live()

    def _stop_firebase(self, quiet=False):
        if self.fb is not None:
            try:
                self.fb.stop()
                self.fb.join(2.0)
            except Exception:
                pass
            self.fb = None
        self.fb_state_ok = False
        self.fb_state_text = "đã tắt" if not quiet else self.fb_state_text
        if quiet:
            return
        self.btn_fb.setChecked(False)
        self.btn_fb.setText("☁  BẬT ĐỒNG BỘ")
        self.lbl_fb.setText("đã tắt đồng bộ")
        self.lbl_fb.setStyleSheet("color:#8892b0; font-size:11px;")
        self._refresh_fb_top()
        self.log("INFO", "Đã tắt đồng bộ Firebase.")

    def _fb_ping(self):
        if self.fb is None:
            self.log("WARN", "Chưa bật đồng bộ Firebase.")
            return
        self.fb.push_live(self._fb_live_payload())
        lat = self.fb.latency_ms
        self.log("INFO", f"Firebase: {self.fb_state_text} · "
                         f"{'--' if lat is None else f'{lat:.0f} ms'} · "
                         f"thiết bị {self.fb.device}")
        self._refresh_fb_top()

    def _on_fb_status(self, text, ok):
        self.fb_state_text = str(text)
        self.fb_state_ok = bool(ok)
        self.lbl_fb.setText(self.fb_state_text)
        color = ("#2ecc71" if ok
                 else "#e74c3c" if "lỗi" in self.fb_state_text.lower()
                 else "#f39c12")
        self.lbl_fb.setStyleSheet(f"color:{color}; font-size:11px;")
        self._refresh_fb_top()

    def _refresh_fb_top(self):
        if self.fb is None:
            self.lbl_fb_top.setText("☁ chưa bật")
            self.lbl_fb_top.setStyleSheet(
                "color:#5b6b82; font-family:Consolas; font-size:11px;"
                " background:#0b1626; border:1px solid #22314a;"
                " border-radius:6px; padding:5px 10px;")
            self.lbl_fb_stats.setText("2.0 s/chu kỳ · -- ms")
            return
        lat = self.fb.latency_ms
        lat_txt = "-- ms" if lat is None else f"{lat:.0f} ms"
        color = "#2ecc71" if self.fb_state_ok else (
            "#e74c3c" if "lỗi" in self.fb_state_text.lower() else "#f39c12")
        self.lbl_fb_top.setText(f"☁ {self.fb_state_text} · {lat_txt}")
        self.lbl_fb_top.setStyleSheet(
            f"color:{color}; font-family:Consolas; font-size:11px;"
            " background:#0b1626; border:1px solid #22314a;"
            " border-radius:6px; padding:5px 10px;")
        self.lbl_fb_stats.setText(
            f"{self.fb.interval:.1f} s/chu kỳ · {lat_txt}")

    def _fb_live_payload(self):
        states = {d: self.ch_state.get(self.relay_map[d], False)
                  for d in self.dev_want}
        return {
            "humidity": (None if self.last_humidity is None
                         else round(float(self.last_humidity), 1)),
            "temp": (None if self.last_temp is None
                     else round(float(self.last_temp), 1)),
            "pump": bool(states.get("pump", False)),
            "fan": bool(states.get("fan", False)),
            "lamp": bool(states.get("lamp", False)),
            "mode": self.mode,
            "estop": bool(self.estop),
            "sensor_ok": bool(self.sensor_ok),
            "modbus_connected": bool(self.connected),
            "rh_on": round(self.rh_on, 1),
            "rh_off": round(self.rh_off, 1),
            "fan_on": round(self.fan_on, 1),
            "fan_off": round(self.fan_off, 1),
            "heat_on": round(self.heat_on, 1),
            "heat_off": round(self.heat_off, 1),
            "pump_max_s": int(self.pump_max),
            "heat_max_s": int(self.heat_max),
            "failsafe_cycles": int(self.failsafe_cycles),
            "uptime_s": int(max(0, time.time() - self.t0)),
        }

    def _fb_state_snapshot(self):
        return {
            "mode": self.mode,
            "estop": bool(self.estop),
            "pump": bool(self.ch_state.get(self.relay_map["pump"], False)),
            "fan": bool(self.ch_state.get(self.relay_map["fan"], False)),
            "lamp": bool(self.ch_state.get(self.relay_map["lamp"], False)),
            "connected": bool(self.connected),
            "sensor_ok": bool(self.sensor_ok),
        }

    def _push_fb_live(self):
        self._push_tcp_state()
        if self.fb is None:
            return
        try:
            self.fb.push_live(self._fb_live_payload())
        except Exception:
            pass
        try:
            self.fb.push_trace(self._fb_trace_records())
        except Exception:
            pass

    def _fb_trace_records(self):
        """Bản ghi khung Modbus (RTU + ASCII) gần nhất của worker RS-485.

        Worker mô phỏng (SimulatorWorker) không có get_trace() → trả rỗng.
        """
        getter = getattr(self.worker, "get_trace", None)
        if getter is None:
            return []
        try:
            return list(getter())
        except Exception:
            return []

    # ---------------------- Nhận lệnh / cấu hình từ ERP ----------------------
    def _on_remote_control(self, ctl):
        seq = ctl.get("seq")
        if seq is None:
            seq = ctl.get("ts")
        by = str(ctl.get("by", "web"))
        ts = int(ctl.get("ts") or 0)
        if ts:
            self.last_control_ts = max(self.last_control_ts, ts)
            if self.fb is not None:
                self.fb.note_control_ts(ts)

        ok = True
        notes = []
        try:
            if ctl.get("estop") is True:
                self._apply_estop(f"web ({by})")
                notes.append("E-STOP")
            elif ctl.get("estop") is False and self.estop:
                # An toan: giai phong E-STOP la 1 buoc RIENG. Neu cung 1 lenh
                # vua giai phong vua bat/tat tai thi mot thong diep duy nhat
                # co the vuot qua khoa E-STOP -> bo qua cac lenh tai do.
                target = str(ctl.get("mode") or self.mode or "MANUAL").upper()
                if target not in ("AUTO", "MANUAL"):
                    target = "MANUAL"
                had_load_cmd = any(k in ctl for k in ("pump", "fan", "lamp"))
                ctl = {k: v for k, v in ctl.items()
                       if k not in ("pump", "fan", "lamp")}
                self._release_estop(target, reason=f"web ({by})")
                notes.append("giải phóng E-STOP")
                if had_load_cmd:
                    notes.append("bo qua lenh tai trong cung lenh giai phong")

            if self.estop:
                notes.append("đang khóa E-STOP")
                if ctl.get("mode") == "AUTO":
                    ok = False
            else:
                mode = ctl.get("mode")
                if mode in ("AUTO", "MANUAL") and mode != self.mode:
                    self._set_mode(mode)
                    notes.append(f"chế độ {mode}")
                for dev in ("pump", "fan", "lamp"):
                    if dev in ctl and isinstance(ctl[dev], bool):
                        done = self._send_device(
                            dev, bool(ctl[dev]), f"lệnh web ({by})",
                            source="WEB", force=True)
                        notes.append(
                            f"{self._dev_name(dev)} "
                            f"{'BẬT' if ctl[dev] else 'TẮT'}")
                        if done is False:
                            ok = False
            if not self.connected and (notes or ctl.get("estop") is True):
                ok = False
                notes.append("chưa nối Modbus")
        except Exception as e:
            ok = False
            notes.append(f"lỗi {type(e).__name__}")

        self.last_control_ts = max(self.last_control_ts, ts)
        self._save_settings()
        self._push_fb_live()
        msg = " · ".join(notes) if notes else "đã áp dụng"
        if self.fb is not None:
            self.fb.push_command_ack(seq, ok, msg,
                                     self._fb_state_snapshot(), by=by)
        self.log("OK" if ok else "WARN", f"Remote ({by}) seq={seq}: {msg}")

    def _on_remote_config(self, cf):
        seq = cf.get("seq")
        if seq is None:
            seq = cf.get("ts")
        by = str(cf.get("by", "web"))
        ts = int(cf.get("ts") or 0)

        def pick(key, current):
            try:
                return float(cf[key])
            except (KeyError, TypeError, ValueError):
                return float(current)

        rh_on = max(0.0, min(100.0, pick("rh_on", self.rh_on)))
        rh_off = max(0.0, min(100.0, pick("rh_off", self.rh_off)))
        fan_on = max(-10.0, min(60.0, pick("fan_on", self.fan_on)))
        fan_off = max(-10.0, min(60.0, pick("fan_off", self.fan_off)))
        heat_on = max(-10.0, min(60.0, pick("heat_on", self.heat_on)))
        heat_off = max(-10.0, min(60.0, pick("heat_off", self.heat_off)))
        try:
            pump_max = int(cf.get("pump_max_s", self.pump_max))
        except (TypeError, ValueError):
            pump_max = self.pump_max
        try:
            heat_max = int(cf.get("heat_max_s", self.heat_max))
        except (TypeError, ValueError):
            heat_max = self.heat_max
        try:
            failsafe = int(cf.get("failsafe_cycles", self.failsafe_cycles))
        except (TypeError, ValueError):
            failsafe = self.failsafe_cycles
        pump_max = max(30, min(7200, pump_max))
        heat_max = max(30, min(7200, heat_max))
        failsafe = max(1, min(999, failsafe))

        errs = []
        if rh_on >= rh_off:
            errs.append("RH bật phải nhỏ hơn RH tắt")
        if fan_on <= fan_off:
            errs.append("T quạt bật phải lớn hơn T quạt tắt")
        if heat_on >= heat_off:
            errs.append("T đèn bật phải nhỏ hơn T đèn tắt")
        if heat_off >= fan_off:
            errs.append("T đèn tắt phải nhỏ hơn T quạt tắt")

        if errs:
            msg = "Từ chối cấu hình: " + " · ".join(errs)
            self.log("WARN", msg)
            if self.fb is not None:
                self.fb.push_config_ack(seq, False, msg,
                                        self._fb_config_snapshot(), by=by)
            return

        spins = (self.sp_rh_on, self.sp_rh_off, self.sp_fan_on,
                 self.sp_fan_off, self.sp_heat_on, self.sp_heat_off)
        for sp in spins:
            sp.blockSignals(True)
        try:
            self.sp_rh_on.setValue(rh_on)
            self.sp_rh_off.setValue(rh_off)
            self.sp_fan_on.setValue(fan_on)
            self.sp_fan_off.setValue(fan_off)
            self.sp_heat_on.setValue(heat_on)
            self.sp_heat_off.setValue(heat_off)
        finally:
            for sp in spins:
                sp.blockSignals(False)
            self._on_threshold_changed()

        self.pump_max = pump_max
        self.heat_max = heat_max
        self.failsafe_cycles = failsafe
        self.lbl_wd.setText(
            f"🛡 Watchdog: bơm {self.pump_max}s · đèn {self.heat_max}s")
        self.last_config_ts = max(self.last_config_ts, ts)
        if self.fb is not None:
            self.fb.note_config_ts(ts)
        self._save_settings()
        self._push_fb_live()

        msg = "Đã áp dụng ngưỡng từ web"
        self.log("OK", f"{msg} ({by}) seq={seq}")
        if self.fb is not None:
            self.fb.push_config_ack(seq, True, msg,
                                    self._fb_config_snapshot(), by=by)

    def _fb_config_snapshot(self):
        return {
            "rh_on": round(self.rh_on, 1), "rh_off": round(self.rh_off, 1),
            "fan_on": round(self.fan_on, 1),
            "fan_off": round(self.fan_off, 1),
            "heat_on": round(self.heat_on, 1),
            "heat_off": round(self.heat_off, 1),
            "pump_max_s": int(self.pump_max),
            "heat_max_s": int(self.heat_max),
            "failsafe_cycles": int(self.failsafe_cycles),
        }

    # ---------------------- E-STOP ----------------------
    def _estop_now(self):
        self._apply_estop("thủ công")

    def _apply_estop(self, reason="E-STOP"):
        self.estop = True
        self.log("WARN", f"⛔ E-STOP: {reason} — khóa điều khiển, tắt toàn bộ tải.")
        self._set_mode("MANUAL", force=True)
        for dev in self.dev_want:
            self._send_device(dev, False, f"E-STOP — {reason}",
                              source="ESTOP", force=True)
        self._update_estop_ui()
        self._push_fb_live()

    def _release_estop(self, mode="AUTO", reason="thủ công"):
        if not self.estop:
            return
        self.estop = False
        target = mode if mode in ("AUTO", "MANUAL") else "AUTO"
        self.log("OK", f"✔ Giải phóng E-STOP ({reason}) → {target}")
        self._set_mode(target, force=True)
        self._update_estop_ui()
        self._push_fb_live()

    def _update_estop_ui(self):
        self.lbl_estop.setVisible(self.estop)
        self.btn_estop.setEnabled(not self.estop)
        self.btn_release_estop.setEnabled(self.estop)
        self._update_manual_buttons()

    def _build_gauges_group(self):
        g = QGroupBox("📡  DỮ LIỆU SHTC30 · CẬP NHẬT THỜI GIAN THỰC")
        lay = QHBoxLayout(g)
        lay.setContentsMargins(8, 16, 8, 8)
        lay.setSpacing(8)

        self.gauge_hum = CircularGauge("ĐỘ ẨM KHÔNG KHÍ", "%RH", 0, 100)
        self.gauge_hum.set_color_fn(self._rh_color)
        self.gauge_hum.set_ticks([(self.rh_on, QColor("#4cc9f0")),
                                  (self.rh_off, QColor("#2ecc71"))])
        lay.addWidget(self.gauge_hum, 1)

        self.gauge_temp = CircularGauge("NHIỆT ĐỘ KHÔNG KHÍ", "°C", -10, 60)
        self.gauge_temp.set_color_fn(self._temp_color)
        self.gauge_temp.set_ticks([(self.fan_on, QColor("#e74c3c")),
                                   (self.fan_off, QColor("#f39c12")),
                                   (self.heat_on, QColor("#61dafb")),
                                   (self.heat_off, QColor("#f9a826"))])
        lay.addWidget(self.gauge_temp, 1)

        g.setFixedHeight(208)
        return g

    def _build_device_row(self):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(9)
        self.cards = {}
        for key, name, icon, color in self.DEVICES:
            card = DeviceCard(name, icon, color)
            self.cards[key] = card
            h.addWidget(card, 1)
        w.setFixedHeight(88)
        return w

    def _build_diagram_group(self):
        g = QGroupBox("🔌  SƠ ĐỒ PHẦN CỨNG & TRẠNG THÁI TẢI")
        v = QVBoxLayout(g)
        v.setContentsMargins(8, 16, 8, 8)
        self.diagram = HardwareDiagram()
        v.addWidget(self.diagram)
        return g

    # ---------------------- Cột giữa: đồ thị ----------------------
    def _build_plot_group(self):
        g = QGroupBox("📈  DIỄN BIẾN NHIỆT ĐỘ & ĐỘ ẨM THEO THỜI GIAN")
        v = QVBoxLayout(g)
        v.setContentsMargins(8, 16, 8, 8)
        v.setSpacing(4)

        legend = QLabel(
            '<span style="color:#4cc9f0;">● Độ ẩm</span> &nbsp; '
            '<span style="color:#f9c74f;">● Nhiệt độ</span> &nbsp; '
            '<span style="color:#e74c3c;">┄ Quạt bật</span> &nbsp; '
            '<span style="color:#f39c12;">┄ Quạt tắt</span> &nbsp; '
            '<span style="color:#61dafb;">┄ Đèn bật</span> &nbsp; '
            '<span style="color:#f9a826;">┄ Đèn tắt</span>')
        legend.setStyleSheet("font-size:10px; color:#8892b0;")
        v.addWidget(legend)

        self.plot = None
        self.curve = None
        self.curve_temp = None
        self.vb_temp = None
        self.plot_lines = {}

        if not PYQTGRAPH_OK:
            lbl = QLabel("⚠ Cần cài pyqtgraph để xem đồ thị:  pip install pyqtgraph")
            lbl.setStyleSheet("color:#f39c12; padding:20px;")
            v.addWidget(lbl)
            return g

        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget()
        self.plot.setBackground("#0a1424")
        self.plot.setMinimumHeight(100)
        pi = self.plot.getPlotItem()
        pi.showGrid(x=True, y=True, alpha=0.22)
        pi.setLabel("left", "Độ ẩm (%RH)", color="#4cc9f0")
        pi.setLabel("bottom", "Thời gian (giây)", color="#8892b0")
        pi.setYRange(0, 100, padding=0.02)
        pi.getAxis("left").setTextPen("#8892b0")
        pi.getAxis("bottom").setTextPen("#8892b0")

        self.curve = pi.plot(pen=pg.mkPen("#4cc9f0", width=2))
        self.region = pg.LinearRegionItem(
            values=(self.rh_on, self.rh_off),
            orientation="horizontal",
            brush=pg.mkBrush(76, 201, 240, 18),
            movable=False)
        pi.addItem(self.region)

        # Trục phải: nhiệt độ
        self.vb_temp = pg.ViewBox()
        pi.showAxis("right")
        self.plot.scene().addItem(self.vb_temp)
        pi.getAxis("right").linkToView(self.vb_temp)
        self.vb_temp.setXLink(pi)
        pi.getAxis("right").setLabel("Nhiệt độ (°C)", color="#f9c74f")
        pi.getAxis("right").setTextPen("#8892b0")
        self.vb_temp.setYRange(-10, 60, padding=0)
        self.curve_temp = pg.PlotCurveItem(
            pen=pg.mkPen("#f9c74f", width=2))
        self.vb_temp.addItem(self.curve_temp)

        def add_line(view, key, value, color, style):
            line = pg.InfiniteLine(pos=value, angle=0,
                                   pen=pg.mkPen(color, width=1.4, style=style))
            view.addItem(line)
            self.plot_lines[key] = line
            return line

        add_line(pi, "rh_on", self.rh_on, "#4cc9f0", Qt.DashLine)
        add_line(pi, "rh_off", self.rh_off, "#2ecc71", Qt.DashLine)
        add_line(self.vb_temp, "fan_on", self.fan_on, "#e74c3c", Qt.DotLine)
        add_line(self.vb_temp, "fan_off", self.fan_off, "#f39c12", Qt.DotLine)
        add_line(self.vb_temp, "heat_on", self.heat_on, "#61dafb", Qt.DotLine)
        add_line(self.vb_temp, "heat_off", self.heat_off, "#f9a826", Qt.DotLine)

        def _sync_views():
            self.vb_temp.setGeometry(pi.vb.sceneBoundingRect())
            self.vb_temp.linkedViewChanged(pi.vb, self.vb_temp.XAxis)

        pi.vb.sigResized.connect(_sync_views)
        self._sync_views = _sync_views
        _sync_views()

        v.addWidget(self.plot, 1)
        return g

    # ---------------------- Cột phải: bảng + log ----------------------
    def _build_log_group(self):
        g = QGroupBox("📋  NHẬT KÝ & DỮ LIỆU THỜI GIAN THỰC")
        v = QVBoxLayout(g)
        v.setContentsMargins(10, 16, 10, 10)
        v.setSpacing(7)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Giờ", "RH (%)", "T (°C)", "BƠM", "QUẠT", "ĐÈN"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Stretch)
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.setFixedHeight(205)
        v.addWidget(self.table)

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        v.addWidget(self.txt_log, 1)

        row = QHBoxLayout()
        btn_export = QPushButton("💾  Mở CSV")
        btn_export.setObjectName("GhostBtn")
        btn_export.clicked.connect(self._open_csv)
        btn_clear = QPushButton("🧹  Xóa log")
        btn_clear.setObjectName("GhostBtn")
        btn_clear.clicked.connect(lambda: self.txt_log.clear())
        row.addWidget(btn_export)
        row.addWidget(btn_clear)
        v.addLayout(row)
        return g

    # ===============================================================
    #  MÀU ĐỒNG HỒ
    # ===============================================================
    def _rh_color(self, v):
        if v < self.rh_on:
            return "#4cc9f0"      # khô → cần bơm
        if v < self.rh_off:
            return "#f39c12"      # vùng hysteresis
        return "#2ecc71"          # đủ ẩm

    def _temp_color(self, v):
        if v > self.fan_on:
            return "#e74c3c"      # quá nóng → quạt
        if v > self.fan_off:
            return "#f39c12"      # vùng hysteresis quạt
        if v < self.heat_on:
            return "#3aa0ff"      # quá lạnh → đèn ủ ấm
        if v < self.heat_off:
            return "#61dafb"      # vùng hysteresis đèn
        return "#2ecc71"          # dễ chịu

    # ===============================================================
    #  CẤU HÌNH LƯU / ĐỌC
    # ===============================================================
    def _settings_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "hmi_config.json")

    def _load_settings(self):
        try:
            with open(self._settings_path(), "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            return
        try:
            if str(cfg.get("baud", "")) in [self.cb_baud.itemText(i)
                                            for i in range(self.cb_baud.count())]:
                self.cb_baud.setCurrentText(str(cfg["baud"]))
            if str(cfg.get("timeout", "")) in [self.cb_timeout.itemText(i)
                                               for i in range(self.cb_timeout.count())]:
                self.cb_timeout.setCurrentText(str(cfg["timeout"]))
            if str(cfg.get("poll", "")) in [self.cb_poll.itemText(i)
                                            for i in range(self.cb_poll.count())]:
                self.cb_poll.setCurrentText(str(cfg["poll"]))
            if str(cfg.get("retries", "")) in [self.cb_retries.itemText(i)
                                               for i in range(self.cb_retries.count())]:
                self.cb_retries.setCurrentText(str(cfg["retries"]))
            self.cb_relay_mode.setCurrentIndex(int(cfg.get("relay_mode", 0)) % 2)
            self.chk_active_low.setChecked(bool(cfg.get("active_low", False)))
            self.chk_swap_th.setChecked(bool(cfg.get("swap_th", True)))
            self.sp_sensor_slave.setValue(int(cfg.get("sensor_slave", 1)))
            self.sp_relay_slave.setValue(int(cfg.get("relay_slave", 255)))

            self.sp_ch_pump.setValue(int(cfg.get("ch_pump", 1)))
            self.sp_ch_fan.setValue(int(cfg.get("ch_fan", 2)))
            self.sp_ch_lamp.setValue(int(cfg.get("ch_lamp", 3)))

            self.sp_rh_on.setValue(float(cfg.get("rh_on", self.rh_on)))
            self.sp_rh_off.setValue(float(cfg.get("rh_off", self.rh_off)))
            self.sp_fan_on.setValue(float(cfg.get("fan_on", self.fan_on)))
            self.sp_fan_off.setValue(float(cfg.get("fan_off", self.fan_off)))
            self.sp_heat_on.setValue(float(cfg.get("heat_on", self.heat_on)))
            self.sp_heat_off.setValue(float(cfg.get("heat_off", self.heat_off)))

            port = str(cfg.get("port", "")).strip()
            if port:
                idx = self.cb_port.findData(port)
                if idx >= 0:
                    self.cb_port.setCurrentIndex(idx)

            self.pump_max = max(30, min(7200, int(
                cfg.get("pump_max_s", self.PUMP_MAX))))
            self.heat_max = max(30, min(7200, int(
                cfg.get("heat_max_s", self.HEAT_MAX))))
            self.failsafe_cycles = max(1, min(999, int(
                cfg.get("failsafe_cycles", 1))))
            self.lbl_wd.setText(
                f"🛡 Watchdog: bơm {self.pump_max}s · đèn {self.heat_max}s")
            self.last_control_ts = int(cfg.get("last_control_ts", 0) or 0)
            self.last_config_ts = int(cfg.get("last_config_ts", 0) or 0)

            if str(cfg.get("fb_url", "")).strip():
                self.ed_fb_url.setText(str(cfg["fb_url"]).strip())
            if str(cfg.get("fb_device", "")).strip():
                self.ed_fb_device.setText(str(cfg["fb_device"]).strip())
            self.fb_auto = bool(cfg.get("fb_auto", True))
            self.chk_fb_auto.setChecked(self.fb_auto)

            self.sp_tcp_port.setValue(int(cfg.get("tcp_port", 502)))
            self.sp_tcp_devid.setValue(int(cfg.get("tcp_devid", 254)))
            self.tcp_auto = bool(cfg.get("tcp_auto", True))
            self.chk_tcp_auto.setChecked(self.tcp_auto)
            if str(cfg.get("tcp_allow_ips", "")).strip():
                self.ed_tcp_allow.setText(str(cfg["tcp_allow_ips"]).strip())

            self.estop = bool(cfg.get("estop", False))
            if self.estop:
                self.mode = "MANUAL"
                self.btn_auto.setChecked(False)
                self.btn_man.setChecked(True)
                self.btn_auto.setObjectName("ModeOff")
                self.btn_man.setObjectName("ModeOn")
            self._update_estop_ui()
            self._update_manual_buttons()

            if self.fb_auto and self.ed_fb_url.text().strip().lower().startswith("http"):
                QTimer.singleShot(1200, self._auto_start_firebase)
        except Exception as e:
            self.log("WARN", f"File cấu hình không hợp lệ: {e}")

    def _save_settings(self):
        cfg = {
            "port": self._current_port(),
            "baud": self.cb_baud.currentText(),
            "timeout": self.cb_timeout.currentText(),
            "poll": self.cb_poll.currentText(),
            "retries": self.cb_retries.currentText(),
            "relay_mode": self.cb_relay_mode.currentIndex(),
            "active_low": self.chk_active_low.isChecked(),
            "swap_th": self.chk_swap_th.isChecked(),
            "sensor_slave": self.sp_sensor_slave.value(),
            "relay_slave": self.sp_relay_slave.value(),
            "ch_pump": self.sp_ch_pump.value(),
            "ch_fan": self.sp_ch_fan.value(),
            "ch_lamp": self.sp_ch_lamp.value(),
            "rh_on": self.rh_on, "rh_off": self.rh_off,
            "fan_on": self.fan_on, "fan_off": self.fan_off,
            "heat_on": self.heat_on, "heat_off": self.heat_off,
            "mode": self.mode,
            "estop": self.estop,
            "pump_max_s": self.pump_max,
            "heat_max_s": self.heat_max,
            "failsafe_cycles": self.failsafe_cycles,
            "last_control_ts": self.last_control_ts,
            "last_config_ts": self.last_config_ts,
            "fb_url": self.ed_fb_url.text().strip(),
            "fb_device": self.ed_fb_device.text().strip() or "vuon-1",
            "fb_auto": bool(self.chk_fb_auto.isChecked()),
            "tcp_port": int(self.sp_tcp_port.value()),
            "tcp_devid": int(self.sp_tcp_devid.value()),
            "tcp_auto": bool(self.chk_tcp_auto.isChecked()),
            "tcp_allow_ips": self.ed_tcp_allow.text().strip(),
        }
        try:
            with open(self._settings_path(), "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("Không lưu được cấu hình:", e)

    # ===============================================================
    #  LOG / ĐỒNG HỒ UI
    # ===============================================================
    def log(self, level, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        colors = {"INFO": "#4cc9f0", "OK": "#2ecc71",
                  "WARN": "#f39c12", "ERROR": "#e74c3c"}
        c = colors.get(level, "#c9d1d9")
        if self.fb is not None and level in ("OK", "WARN", "ERROR"):
            try:
                self.fb.push_log(level, msg)
            except Exception:
                pass
        esc = (str(msg).replace("&", "&amp;").replace("<", "&lt;")
               .replace(">", "&gt;"))
        html = (f'<span style="color:#8892b0;">[{ts}]</span> '
                f'<span style="color:{c}; font-weight:600;">[{level}]</span> '
                f'<span style="color:#c9d1d9;">{esc}</span>')
        self.txt_log.append(html)
        self.txt_log.verticalScrollBar().setValue(
            self.txt_log.verticalScrollBar().maximum())

    def _tick_ui(self):
        self.lbl_clock.setText(datetime.now().strftime("%H:%M:%S"))
        self._refresh_fb_top()
        self._push_tcp_state()

        if self.last_data_ts is None:
            self.lbl_live.setText("● SHTC30 · chưa có dữ liệu")
            self.lbl_live.setStyleSheet(
                "color:#5b6b82; font-family:Consolas; font-size:11px;"
                " background:#0b1626; border:1px solid #22314a;"
                " border-radius:6px; padding:5px 10px;")
            self.gauge_hum.set_stale(True)
            self.gauge_temp.set_stale(True)
            return

        age = max(0.0, time.time() - self.last_data_ts)
        if age < 3 and self.connected and self.sensor_ok:
            col = "#2ecc71"
        elif age < 8 and self.connected:
            col = "#f39c12"
        else:
            col = "#e74c3c"
        self.lbl_live.setText(f"● SHTC30 · cập nhật {age:.1f} s trước")
        self.lbl_live.setStyleSheet(
            f"color:{col}; font-family:Consolas; font-size:11px;"
            f" background:#0b1626; border:1px solid {col};"
            f" border-radius:6px; padding:5px 10px;")

        stale = age > 5 or not self.connected or not self.sensor_ok
        self.gauge_hum.set_stale(stale)
        self.gauge_temp.set_stale(stale)

    # ===============================================================
    #  CỔNG COM
    # ===============================================================
    def _current_port(self):
        data = self.cb_port.currentData(Qt.UserRole)
        if data:
            return str(data).strip()
        text = self.cb_port.currentText().strip()
        if not text:
            return ""
        return text.split()[0]

    def _scan_ports(self, force=False):
        prev = self._current_port() if self.cb_port.count() else ""
        ports = scan_serial_ports()
        sig = tuple(ports)
        if not force and sig == self._last_ports:
            return
        changed = (sig != self._last_ports)
        self._last_ports = sig

        self.cb_port.blockSignals(True)
        self.cb_port.clear()
        if not ports:
            self.cb_port.addItem("⚠ Không tìm thấy cổng COM", None)
            self.cb_port.setEditText("")
            self.lbl_port_hint.setText(
                "⚠ Không thấy cổng nào · cắm cáp USB-RS485 / cài driver "
                "CH340-CP210x, hoặc gõ tay tên cổng (vd: COM7)")
        else:
            for dev, desc in ports:
                self.cb_port.addItem(dev, dev)
                self.cb_port.setItemData(self.cb_port.count() - 1,
                                         f"{dev}\n{desc}", Qt.ToolTipRole)
            idx = self.cb_port.findData(prev) if prev else -1
            self.cb_port.setCurrentIndex(idx if idx >= 0 else 0)
            self.lbl_port_hint.setText(
                f"✔ {len(ports)} cổng: " + ", ".join(d for d, _ in ports))
        self.cb_port.blockSignals(False)

        if changed and hasattr(self, "txt_log"):
            if ports:
                self.log("INFO", "Cổng COM: "
                                 + ", ".join(f"{d} ({desc})" for d, desc in ports))
            else:
                self.log("WARN", "Không phát hiện cổng serial nào. "
                                 + SERIAL_HINT.replace("\n", " "))

    def _poll_ports(self):
        if getattr(self, "connected", False):
            return
        try:
            self._scan_ports(force=False)
        except Exception:
            pass

    @staticmethod
    def _probe_port(dev, baud, timeout=0.3):
        """Thử mở 1 cổng và hỏi Modbus xem có thiết bị trả lời không."""
        if not PYMODBUS_OK:
            return None, None
        variants = (
            dict(port=dev, baudrate=baud, bytesize=8, parity="N",
                 stopbits=1, timeout=timeout, retries=0),
            dict(port=dev, baudrate=baud, bytesize=8, parity="N",
                 stopbits=1, timeout=timeout),
            dict(method="rtu", port=dev, baudrate=baud, bytesize=8,
                 parity="N", stopbits=1, timeout=timeout),
        )
        cli = None
        for kw in variants:
            try:
                cli = ModbusSerialClient(**kw)
                break
            except TypeError:
                continue
            except Exception:
                return None, None
        if cli is None:
            return None, None

        try:
            if not cli.connect():
                return None, None
            for unit in (0x01, 0x02, 0xFF):
                for reader in ("read_input_registers", "read_holding_registers",
                               "read_coils"):
                    fn = getattr(cli, reader, None)
                    if fn is None:
                        continue
                    count = 8 if reader == "read_coils" else 2
                    try:
                        rr = fn(0x0000, count=count, device_id=unit)
                    except TypeError:
                        try:
                            rr = fn(0x0000, count=count, slave=unit)
                        except TypeError:
                            rr = fn(0x0000, count=count, unit=unit)
                    except Exception:
                        rr = None
                    if rr is not None and not rr.isError():
                        return unit, rr

            # Board relay rẻ chỉ trả lời lệnh ghi → ghi OFF (an toàn) để dò
            for writer, val in (("write_coil", False), ("write_register", 0)):
                fn = getattr(cli, writer, None)
                if fn is None:
                    continue
                try:
                    try:
                        rr = fn(0x0000, val, device_id=0xFF)
                    except TypeError:
                        rr = fn(0x0000, val, slave=0xFF)
                except Exception:
                    continue
                if rr is not None and not rr.isError():
                    return 0xFF, rr
        except Exception:
            return None, None
        finally:
            try:
                cli.close()
            except Exception:
                pass
        return None, None

    def _auto_detect_port(self):
        if self._probe is not None and self._probe.isRunning():
            self.log("INFO", "Đang chẩn đoán bus — bỏ qua dò cổng.")
            return
        if self.connected:
            self.log("WARN", "Đang kết nối — ngắt kết nối trước khi dò cổng.")
            return
        if not PYMODBUS_OK:
            QMessageBox.warning(self, "Lỗi",
                                "Chưa cài pymodbus.\nChạy: pip install pymodbus")
            return

        self._scan_ports(force=True)
        ports = scan_serial_ports()
        if not ports:
            self.log("WARN", "Không có cổng nào để dò. "
                             + SERIAL_HINT.replace("\n", " "))
            return

        baud = int(self.cb_baud.currentText())
        self.log("INFO", f"Đang dò {len(ports)} cổng @ {baud} 8N1 "
                         f"({', '.join(d for d, _ in ports)}) ...")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.btn_auto_detect.setEnabled(False)
        hit = None
        try:
            for dev, desc in ports:
                QApplication.processEvents()
                self.lbl_port_hint.setText(f"Đang thử {dev} ...")
                unit, _resp = self._probe_port(dev, baud)
                if unit:
                    hit = (dev, desc, unit)
                    break
        finally:
            QApplication.restoreOverrideCursor()
            self.btn_auto_detect.setEnabled(True)

        if hit:
            dev, desc, unit = hit
            self.log("OK", f"Tìm thấy thiết bị tại {dev} · Slave {unit} · {desc}")
            self._scan_ports(force=True)
            idx = self.cb_port.findData(dev)
            if idx >= 0:
                self.cb_port.setCurrentIndex(idx)
            else:
                self.cb_port.setEditText(dev)
            self.lbl_port_hint.setText(
                f"✔ {dev} có thiết bị trả lời (Slave {unit}) — {desc}")
        else:
            self.log("WARN", "Không cổng nào trả lời Modbus. Kiểm tra dây A/B, "
                             "nguồn module RS485, địa chỉ Slave, baudrate.")
            self.lbl_port_hint.setText(
                "✘ Không cổng nào trả lời — kiểm tra dây A/B, nguồn, "
                "địa chỉ Slave, baudrate")

    # ===============================================================
    #  KẾT NỐI
    # ===============================================================
    def toggle_connection(self):
        if not self.connected:
            self._start_worker()
        else:
            self._stop_worker(safe=True)

    def _toggle_sim(self):
        self.use_simulator = self.btn_sim.isChecked()
        if self.use_simulator:
            self.log("WARN", "Bật SIMULATOR (không dùng cổng thật)")
        else:
            self.log("INFO", "Tắt SIMULATOR")

    def _start_worker(self):
        port = self._current_port()

        if not self.use_simulator:
            if not PYMODBUS_OK:
                QMessageBox.warning(self, "Lỗi",
                                    "Chưa cài pymodbus.\nChạy: pip install pymodbus")
                return
            if not port:
                QMessageBox.warning(
                    self, "Chưa chọn cổng COM",
                    "Chưa chọn cổng COM.\n\nNhấn 🔄 để quét lại, hoặc gõ tay "
                    "tên cổng (vd: COM7) vào ô Cổng COM.\n\n" + SERIAL_HINT)
                return

        chans = (self.sp_ch_pump.value() - 1,
                 self.sp_ch_fan.value() - 1,
                 self.sp_ch_lamp.value() - 1)
        if len(set(chans)) != 3:
            QMessageBox.warning(
                self, "Trùng kênh relay",
                "Bơm / Quạt / Đèn đang được gán vào cùng một kênh relay.\n"
                "Mỗi tải phải dùng một kênh riêng (CH1..CH4).")
            return

        self.relay_map = {"pump": chans[0], "fan": chans[1], "lamp": chans[2]}
        self.ch_state = {ch: self.ch_state.get(ch, False) for ch in range(4)}
        for dev in self.dev_want:
            self.dev_want[dev] = False
            self.dev_start[dev] = None
            self.block_until[dev] = 0.0
            self.reasons[dev] = "chờ dữ liệu"
            self.last_cmd[dev] = 0.0

        self.worker = SimulatorWorker() if self.use_simulator else ModbusWorker()

        baud = int(self.cb_baud.currentText())
        timeout = float(self.cb_timeout.currentText())
        poll = float(self.cb_poll.currentText())
        retries = int(self.cb_retries.currentText())
        sensor_slave = self.sp_sensor_slave.value()
        relay_slave = self.sp_relay_slave.value()
        fc_coil = self.cb_relay_mode.currentIndex() == 0

        self.sensor_ok = bool(self.use_simulator)
        self.last_data_ts = None

        self.worker.configure(
            port, baud, timeout, poll, retries=retries,
            sensor_slave=sensor_slave, relay_slave=relay_slave,
            fc_coil=fc_coil, active_low=self.chk_active_low.isChecked(),
            swap_th=self.chk_swap_th.isChecked(),
            channels=chans,
            failsafe_cycles=self.failsafe_cycles)

        self.worker.data_ready.connect(self._on_data)
        self.worker.relay_states.connect(self._on_relay_states)
        self.worker.relay_failed.connect(self._on_relay_failed)
        self.worker.log_event.connect(self.log)
        self.worker.connection_changed.connect(self._on_conn_changed)
        self.worker.link_changed.connect(self._on_link_changed)
        self.worker.sensor_ok.connect(self._on_sensor_ok)

        self.diagram.set_channels(self.relay_map)
        self.diagram.set_slaves(sensor_slave, relay_slave)
        self.diagram.set_states({d: False for d in self.dev_want})

        self._save_settings()
        self._refresh_cards()
        self.worker.start()
        if not self.use_simulator:
            self.log("INFO", f"Đang kết nối {port} @ {baud} 8N1 …")

    def _stop_worker(self, safe=True):
        if self.worker is None:
            return
        if safe and self.connected:
            active = [d for d in self.dev_want
                      if self.ch_state.get(self.relay_map[d], False)]
            if active:
                for dev in active:
                    self._send_device(dev, False, "TẮT AN TOÀN KHI NGẮT",
                                      source="SYSTEM", force=True)
                t_end = time.time() + 2.5
                while time.time() < t_end:
                    QApplication.processEvents()
                    if not any(self.ch_state.get(self.relay_map[d], False)
                               for d in active):
                        break
                    time.sleep(0.05)
        self.worker.stop()
        self.worker.wait(2500)
        self.worker = None
        self._on_conn_changed(False, "Đã ngắt kết nối")
        self.log("INFO", "Đã ngắt kết nối.")

    def _on_conn_changed(self, connected, msg):
        self.connected = bool(connected)
        self.led.set_state(connected)
        self.lbl_status.setText(msg)
        self.lbl_status.setToolTip(msg)
        self.diagram.set_connected(connected)
        self.btn_connect.setText("🔌  DISCONNECT" if connected else "🔌  CONNECT")
        self._update_manual_buttons()
        if connected:
            self.log("OK", msg)
            if self.estop:
                for dev in self.dev_want:
                    self._send_device(dev, False, "E-STOP đang khóa",
                                      source="ESTOP", force=True)
        else:
            self.log("WARN", msg)
        self._push_fb_live()

    def _on_link_changed(self, ok):
        if not self.connected:
            return
        if ok:
            self.led.set_state(True)
            self.lbl_status.setText("Bus OK · SHTC30 + relay đang trao đổi")
        else:
            self.led.set_state(False)
            self.lbl_status.setText("⚠ Cổng mở nhưng KHÔNG có thiết bị trả lời")
        self._push_fb_live()

    # ===============================================================
    #  NHẬN DỮ LIỆU / TRẠNG THÁI
    # ===============================================================
    def _on_relay_states(self, states):
        now = time.time()
        for ch, val in states.items():
            if ch in self.ch_state:
                self.ch_state[ch] = bool(val)
        for dev in self.dev_want:
            ch = self.relay_map[dev]
            actual = self.ch_state.get(ch, False)
            # Không ghi đè lệnh vừa gửi trong 2,5 s (board có thể trả về trễ)
            if now - self.last_cmd.get(dev, 0.0) > 2.5:
                self.dev_want[dev] = actual
            if actual:
                if self.dev_start[dev] is None:
                    self.dev_start[dev] = now
            else:
                self.dev_start[dev] = None
        self._refresh_cards()
        self._update_manual_buttons()
        self._push_fb_live()

    def _on_relay_failed(self, ch, wanted):
        dev = next((d for d, c in self.relay_map.items() if c == ch), None)
        if dev is None:
            return
        self.dev_want[dev] = self.ch_state.get(ch, False)
        self.reasons[dev] = "LỖI: relay không xác nhận lệnh"
        self.log("ERROR",
                 f"Phần cứng KHÔNG nhận lệnh {'BẬT' if wanted else 'TẮT'} "
                 f"CH{ch + 1} ({self._dev_name(dev)}) — kiểm tra nguồn relay "
                 f"và dây RS485.")
        self._refresh_cards()
        self._push_fb_live()

    def _on_sensor_ok(self, ok):
        self.sensor_ok = bool(ok)
        if not ok and self.mode == "AUTO" and self.connected:
            for dev in self.dev_want:
                self._send_device(dev, False,
                                  "AN TOÀN: mất dữ liệu cảm biến",
                                  source="SAFETY", force=True)
        self._refresh_cards()
        self._push_fb_live()

    def _on_data(self, humidity, temp, states):
        self.last_data_ts = time.time()
        self.sensor_ok = True
        # Bo dem mau moi cho MES (tang 1 lan cho moi mau doc duoc).
        self.tcp_seq = (self.tcp_seq + 1) & 0xFFFF
        self.last_humidity = humidity
        self.last_temp = temp

        for ch, val in (states or {}).items():
            if ch in self.ch_state:
                self.ch_state[ch] = bool(val)

        self.gauge_hum.set_value(humidity)
        self.gauge_temp.set_value(temp)
        self.diagram.set_values(humidity, temp)
        self.diagram.set_states(
            {d: self.ch_state.get(self.relay_map[d], False) for d in self.dev_want})

        dev_states = {d: self.ch_state.get(self.relay_map[d], False)
                      for d in self.dev_want}

        # ----- Bảng dữ liệu -----
        row = self.table.rowCount()
        self.table.insertRow(row)
        items = [
            QTableWidgetItem(datetime.now().strftime("%H:%M:%S")),
            QTableWidgetItem(f"{humidity:.1f}"),
            QTableWidgetItem(f"{temp:.1f}"),
        ]
        for i, it in enumerate(items):
            it.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, i, it)
        for i, key in enumerate(("pump", "fan", "lamp"), start=3):
            on = dev_states[key]
            it = QTableWidgetItem("ON" if on else "OFF")
            it.setTextAlignment(Qt.AlignCenter)
            it.setForeground(QColor("#2ecc71") if on else QColor("#e74c3c"))
            self.table.setItem(row, i, it)
        if self.table.rowCount() > 240:
            self.table.removeRow(0)
        self.table.scrollToBottom()

        # ----- Đồ thị -----
        if self.curve is not None:
            t_now = time.time() - self.t0
            self.hist_t.append(t_now)
            self.hist_rh.append(humidity)
            self.hist_temp.append(temp)
            self.curve.setData(list(self.hist_t), list(self.hist_rh))
            if self.curve_temp is not None:
                self.curve_temp.setData(list(self.hist_t), list(self.hist_temp))

        # ----- Điều khiển AUTO -----
        self._auto_cycle(humidity, temp)

        # ----- CSV -----
        note = ""
        if self.mode == "AUTO":
            note = (f"RH_ON={self.rh_on:.1f} RH_OFF={self.rh_off:.1f} "
                    f"FAN_ON={self.fan_on:.1f} FAN_OFF={self.fan_off:.1f} "
                    f"HEAT_ON={self.heat_on:.1f} HEAT_OFF={self.heat_off:.1f}")
        self.csv.write(humidity, temp, dev_states, self.mode, note)

        # ----- Đồng bộ Firebase (ERP) -----
        self._push_fb_live()

    # ===============================================================
    #  LOGIC AUTO
    # ===============================================================
    def _auto_cycle(self, humidity, temp):
        if self.mode != "AUTO" or not self.connected or not self.sensor_ok:
            return
        if not self.cfg_valid or self.estop:
            return

        now = time.time()

        # ----- Bơm theo độ ẩm -----
        if (not self.dev_want["pump"] and humidity < self.rh_on
                and now >= self.block_until["pump"]):
            self._send_device(
                "pump", True,
                f"RH {humidity:.1f}% < {self.rh_on:.1f}%",
                source="AUTO")
        elif self.dev_want["pump"] and humidity >= self.rh_off:
            self._send_device(
                "pump", False,
                f"RH {humidity:.1f}% ≥ {self.rh_off:.1f}%",
                source="AUTO")

        # ----- Quạt theo nhiệt độ -----
        # Nếu đèn halogen đang bật mà phòng đã quá nóng: tắt đèn trước,
        # sau đó mới cho quạt chạy (liên động an toàn).
        if temp > self.fan_on and self.dev_want["lamp"]:
            self._send_device("lamp", False, "ưu tiên làm mát phòng",
                              source="AUTO", force=True)
        if not self.dev_want["fan"] and temp > self.fan_on:
            self._send_device(
                "fan", True,
                f"T {temp:.1f}°C > {self.fan_on:.1f}°C",
                source="AUTO")
        elif self.dev_want["fan"] and temp <= self.fan_off:
            self._send_device(
                "fan", False,
                f"T {temp:.1f}°C ≤ {self.fan_off:.1f}°C",
                source="AUTO")

        # ----- Đèn halogen ủ ấm -----
        if (not self.dev_want["lamp"] and temp < self.heat_on
                and now >= self.block_until["lamp"]):
            self._send_device(
                "lamp", True,
                f"T {temp:.1f}°C < {self.heat_on:.1f}°C",
                source="AUTO")
        elif self.dev_want["lamp"] and temp >= self.heat_off:
            self._send_device(
                "lamp", False,
                f"T {temp:.1f}°C ≥ {self.heat_off:.1f}°C",
                source="AUTO")

        # ----- Liên động an toàn -----
        if self.dev_want["fan"] and self.dev_want["lamp"]:
            self._send_device("lamp", False, "Liên động: quạt đang chạy",
                              source="SAFETY", force=True)

        # ----- Mô tả trạng thái trên card -----
        if self.dev_want["pump"]:
            self.reasons["pump"] = (
                f"RH {humidity:.1f}% · đang tưới (tắt ≥ {self.rh_off:.0f}%)")
        else:
            self.reasons["pump"] = (
                f"RH {humidity:.1f}% · bật khi < {self.rh_on:.0f}%")

        if self.dev_want["fan"]:
            self.reasons["fan"] = (
                f"T {temp:.1f}°C · đang làm mát (tắt ≤ {self.fan_off:.0f}°C)")
        else:
            self.reasons["fan"] = (
                f"T {temp:.1f}°C · bật khi > {self.fan_on:.0f}°C")

        if self.dev_want["lamp"]:
            self.reasons["lamp"] = (
                f"T {temp:.1f}°C · đang ủ ấm (tắt ≥ {self.heat_off:.0f}°C)")
        else:
            self.reasons["lamp"] = (
                f"T {temp:.1f}°C · bật khi < {self.heat_on:.0f}°C")
        self._refresh_cards()

    def _send_device(self, dev, on, reason="", source="AUTO", force=False):
        if dev not in self.dev_want:
            return False
        if not self.worker or not self.connected:
            self.log("WARN", "Chưa kết nối, không thể điều khiển tải.")
            return False

        ch = self.relay_map[dev]
        on = bool(on)
        if on and self.estop:
            self.log("WARN", "E-STOP đang khóa — không bật tải.")
            return False
        if self.dev_want[dev] == on and self.ch_state.get(ch, False) == on:
            return True
        if not force and time.time() - self.last_cmd.get(dev, 0.0) < 2.0:
            return False

        # Liên động quạt ↔ đèn halogen
        if dev == "lamp" and on and self.dev_want.get("fan"):
            self.log("WARN", "Không bật đèn halogen khi quạt đang chạy "
                             "(liên động an toàn).")
            return False
        if dev == "fan" and on and self.dev_want.get("lamp"):
            self.log("WARN", "Hãy tắt đèn halogen trước khi bật quạt "
                             "(liên động an toàn).")
            return False

        self.dev_want[dev] = on
        self.last_cmd[dev] = time.time()
        if reason:
            self.reasons[dev] = reason
        self.worker.set_command(ch, on)
        self.log("INFO",
                 f"[{source}] {'BẬT' if on else 'TẮT'} {self._dev_name(dev)} "
                 f"(CH{ch + 1})" + (f" — {reason}" if reason else ""))
        self._refresh_cards()
        self._update_manual_buttons()
        self._push_fb_live()
        return True

    def _dev_name(self, dev):
        return next((n for k, n, _i, _c in self.DEVICES if k == dev), dev)

    def _refresh_cards(self):
        now = time.time()
        for dev in self.dev_want:
            ch = self.relay_map[dev]
            on = self.ch_state.get(ch, False)
            want = self.dev_want[dev]
            pending = (want != on)
            runtime = ""
            if on and self.dev_start.get(dev):
                runtime = f"{int(max(0, now - self.dev_start[dev]))} s"
            card = self.cards.get(dev)
            if card is None:
                continue
            card.set_channel(ch)
            card.set_state(on, pending, runtime)
            card.set_reason(self.reasons.get(dev, ""))
        self.diagram.set_states(
            {d: self.ch_state.get(self.relay_map[d], False) for d in self.dev_want})

    def _update_manual_buttons(self):
        enable = (self.mode == "MANUAL" and self.connected and not self.estop)
        for dev in self.btn_on:
            on = self.dev_want[dev]
            self.btn_on[dev].setEnabled(enable and not on)
            self.btn_off[dev].setEnabled(enable and on)
        self.btn_all_off.setEnabled(self.connected)
        self.btn_auto.setEnabled(not self.estop)
        self.btn_man.setEnabled(not self.estop)
        self.btn_estop.setEnabled(not self.estop)
        self.btn_release_estop.setEnabled(self.estop)

    def _set_mode(self, mode, force=False):
        if mode == self.mode:
            return
        if self.estop and mode == "AUTO" and not force:
            self.log("WARN", "E-STOP đang khóa — giải phóng trước khi về AUTO.")
            return
        self.mode = mode
        self.btn_auto.setChecked(mode == "AUTO")
        self.btn_man.setChecked(mode == "MANUAL")
        self.btn_auto.setObjectName("ModeOn" if mode == "AUTO" else "ModeOff")
        self.btn_man.setObjectName("ModeOn" if mode == "MANUAL" else "ModeOff")
        for b in (self.btn_auto, self.btn_man):
            b.style().unpolish(b)
            b.style().polish(b)
        if mode == "AUTO":
            for dev in self.reasons:
                self.reasons[dev] = "chờ chu kỳ đo"
        self.log("INFO", f"Chuyển chế độ: {mode}")
        self._update_manual_buttons()
        self._refresh_cards()
        self._push_fb_live()

    def _manual(self, dev, on):
        if self.estop:
            self.log("WARN", "E-STOP đang khóa — giải phóng trước khi điều khiển.")
            return
        if self.mode != "MANUAL":
            self.log("WARN", "Đang ở chế độ AUTO — chuyển sang MANUAL để "
                             "điều khiển thủ công.")
            return
        self._send_device(dev, on, "điều khiển thủ công",
                          source="MANUAL", force=True)

    def _all_off(self):
        if not self.connected:
            self.log("WARN", "Chưa kết nối — không gửi được lệnh tắt.")
            return
        self.log("WARN", "TẮT KHẨN CẤP: gửi lệnh tắt cả 3 tải.")
        for dev in self.dev_want:
            self._send_device(dev, False, "TẮT KHẨN CẤP",
                              source="MANUAL", force=True)

    # ===============================================================
    #  WATCHDOG
    # ===============================================================
    def _watchdog_check(self):
        now = time.time()
        for dev, limit in (("pump", self.pump_max), ("lamp", self.heat_max)):
            ch = self.relay_map[dev]
            if not self.ch_state.get(ch, False):
                continue
            if self.dev_start.get(dev) is None:
                self.dev_start[dev] = now
                continue
            elapsed = now - self.dev_start[dev]
            if elapsed > limit:
                self.block_until[dev] = now + self.BLOCK_AFTER_WD
                self._send_device(
                    dev, False,
                    f"WATCHDOG: quá {limit} s",
                    source="WATCHDOG", force=True)
                self.log("WARN",
                         f"⚠ WATCHDOG: {self._dev_name(dev)} chạy quá {limit} s "
                         f"→ tự ngắt, tạm khóa {self.BLOCK_AFTER_WD} s.")
        self._refresh_cards()
        self._push_fb_live()

    # ===============================================================
    #  NGƯỠNG
    # ===============================================================
    def _on_threshold_changed(self, *_):
        try:
            self.rh_on = float(self.sp_rh_on.value())
            self.rh_off = float(self.sp_rh_off.value())
            self.fan_on = float(self.sp_fan_on.value())
            self.fan_off = float(self.sp_fan_off.value())
            self.heat_on = float(self.sp_heat_on.value())
            self.heat_off = float(self.sp_heat_off.value())
        except Exception:
            return

        msgs = []
        if self.rh_on >= self.rh_off:
            msgs.append("RH bật phải NHỎ HƠN RH tắt")
        if self.fan_on <= self.fan_off:
            msgs.append("T quạt bật phải LỚN HƠN T quạt tắt")
        if self.heat_on >= self.heat_off:
            msgs.append("T đèn bật phải NHỎ HƠN T đèn tắt")
        if self.heat_off >= self.fan_off:
            msgs.append("T đèn tắt phải NHỎ HƠN T quạt tắt (tránh chồng vùng)")
        self.cfg_valid = not msgs
        self.lbl_warn.setText("⚠ " + " · ".join(msgs) if msgs else "")
        self.lbl_warn.setStyleSheet(
            "color:#e74c3c; font-size:10px;" if msgs
            else "color:#f39c12; font-size:10px;")

        gauge_h = getattr(self, "gauge_hum", None)
        if gauge_h is not None:
            gauge_h.set_ticks([(self.rh_on, QColor("#4cc9f0")),
                               (self.rh_off, QColor("#2ecc71"))])
        gauge_t = getattr(self, "gauge_temp", None)
        if gauge_t is not None:
            gauge_t.set_ticks([(self.fan_on, QColor("#e74c3c")),
                               (self.fan_off, QColor("#f39c12")),
                               (self.heat_on, QColor("#61dafb")),
                               (self.heat_off, QColor("#f9a826"))])

        if getattr(self, "plot", None) is not None:
            for key, val in (("rh_on", self.rh_on), ("rh_off", self.rh_off),
                             ("fan_on", self.fan_on), ("fan_off", self.fan_off),
                             ("heat_on", self.heat_on), ("heat_off", self.heat_off)):
                line = getattr(self, "plot_lines", {}).get(key)
                if line is not None:
                    line.setPos(val)
            if getattr(self, "region", None) is not None:
                self.region.setRegion((self.rh_on, self.rh_off))
        self._push_fb_live()

    # ===============================================================
    #  TIỆN ÍCH
    # ===============================================================
    def _diagnose_bus(self):
        if self._probe is not None and self._probe.isRunning():
            return
        port = self._current_port()
        if not port:
            QMessageBox.warning(self, "Thiếu cổng COM",
                                "Chọn cổng COM trước khi chẩn đoán.")
            return

        if self.worker:
            self._stop_worker(safe=True)
            time.sleep(0.3)

        self.btn_diag.setEnabled(False)
        self.btn_connect.setEnabled(False)
        self.log("INFO", f"🔧 Bắt đầu chẩn đoán bus trên {port} "
                         f"(quét 0..255 + ASCII + thử ghi relay, ~2 phút)…")
        self._probe = ProbeWorker(port, self.sp_relay_slave.value())
        self._probe.line_ready.connect(self._on_probe_line)
        self._probe.finished_ok.connect(self._on_probe_done)
        self._probe.start()

    def _on_probe_line(self, line):
        line = (line or "").strip()
        if not line:
            return
        low = line.lower()
        if "✘" in line or "không có byte" in low or "không mở được" in low:
            lvl = "ERROR"
        elif "✔" in line or "★" in line or "có phản hồi" in low:
            lvl = "OK"
        elif "!" in line or "lưu ý" in low:
            lvl = "WARN"
        else:
            lvl = "INFO"
        self.log(lvl, line)

    def _on_probe_done(self, code):
        self.btn_diag.setEnabled(True)
        self.btn_connect.setEnabled(True)
        if code == 0:
            self.log("INFO", "🔧 Chẩn đoán xong. Bấm CONNECT để kết nối lại.")
        else:
            self.log("WARN", f"🔧 Chẩn đoán kết thúc với mã {code}. Xem log trên.")

    def _open_csv(self):
        path = self.csv.path
        if not os.path.exists(path):
            QMessageBox.information(self, "Thông báo", "Chưa có file log CSV.")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}"')
        except Exception as e:
            QMessageBox.warning(self, "Lỗi", f"Không mở được file: {e}")

    def closeEvent(self, event):
        try:
            self._save_settings()
            if self.fb is not None:
                self._stop_firebase(quiet=True)
            if self.tcp is not None:
                self._stop_tcp(quiet=True)
            if self.worker is not None:
                self._stop_worker(safe=True)
            if self._probe is not None and self._probe.isRunning():
                self._probe.terminate()
                self._probe.wait(1500)
        except Exception:
            pass
        event.accept()


# ==============================================================================
# 10. ENTRY POINT
# ==============================================================================
def _install_global_excepthook():
    def _hook(exc_type, exc_value, exc_tb):
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        sys.stderr.write(tb)
        try:
            QMessageBox.critical(None, "Lỗi không mong muốn",
                                 f"{exc_value}\n\nChi tiết:\n{tb[:1500]}")
        except Exception:
            pass
    sys.excepthook = _hook


def main():
    try:
        if not PYMODBUS_OK:
            print("⚠ THIẾU pymodbus (không bắt buộc nếu dùng SIMULATOR).")
            print("  Cài: pip install pymodbus")
        if not PYQTGRAPH_OK:
            print("⚠ THIẾU pyqtgraph (đồ thị sẽ không hiển thị).")
            print("  Cài: pip install pyqtgraph")

        app = QApplication(sys.argv)
        app.setStyle("Fusion")
        _install_global_excepthook()

        w = MainWindow()
        w.show()
        sys.exit(app.exec_())

    except SystemExit:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        print("=" * 70)
        print("❌ LỖI KHỞI ĐỘNG ỨNG DỤNG:")
        print(tb)
        print("=" * 70)
        try:
            if QApplication.instance() is None:
                _app = QApplication(sys.argv)
            QMessageBox.critical(None, "Không khởi động được HMI",
                                 f"{e}\n\n{tb[:1800]}")
        except Exception:
            pass
        try:
            input("\n>>> Nhấn Enter để thoát...")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
