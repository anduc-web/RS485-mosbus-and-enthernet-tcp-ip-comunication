# -*- coding: utf-8 -*-
"""AGRI MES · PC2 — app Python (PyQt5) đọc/ghi PC1 qua Modbus TCP.

    MES (PC2)  ──Modbus TCP──►  PC1 (HMI, mở cổng 502)

Bố cục 4 phần hệ (giống PumpVision MES):
  1. Tổng quan          — biểu đồ 6 giờ, tỉ lệ bơm chạy/nghỉ 7 ngày, đợt quá nhiệt
  2. Lịch sử cảnh báo  — các sự cố phát hiện được
  3. Quản lý bảo trì    — watchdog, failsafe, tuổi dữ liệu, ngưỡng quá nhiệt
  4. Công thức vận hành — đọc/ghi ngưỡng, điều khiển tải, xuất tag

Chạy:
    python mes_app.py --host 192.168.1.50 --port 502
"""

import argparse
import os
import queue
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtCore import QThread, QTimer, Qt, pyqtSignal      # noqa: E402
from PyQt5.QtGui import QColor, QFont, QPainter, QPen         # noqa: E402
from PyQt5.QtWidgets import (                                  # noqa: E402
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSizePolicy, QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

from mes_contract import (                                     # noqa: E402
    COIL_ADDRESS, DEVICE_ID, HR_ADDRESS, HR_LIMITS, PROFILE_AUTO,
    PROFILE_SHT20, PROFILE_SHT30, PROFILES, SAMPLE_KEY, WRITE_ORDER,
    build_tag_contract, contract_to_csv, contract_to_json, detect_profile,
    profile_fields, profile_limits, signed16,
)
from mes_diag import ModbusFault, diagnose                    # noqa: E402
from mes_history import History                               # noqa: E402
from mes_stats import (                                       # noqa: E402
    build_series, detect_events, overheat_events, run_ratio, summarize,
)

POLL_SECONDS = 2.0
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 502
DEFAULT_OVERHEAT_C = 35.0

DARK_QSS = """
QMainWindow, QWidget { background-color:#0b1220; color:#ecf0f1;
    font-family:'Segoe UI','Arial'; font-size:12px; }
QFrame#Sidebar { background-color:#0e1626; border-right:1px solid #22314a; }
QFrame#Topbar { background-color:#101a2e; border-bottom:1px solid #22314a; }
QLabel { background:transparent; }
QLabel#Brand { font-size:16px; font-weight:800; color:#ecf0f1; }
QLabel#BrandSub { font-size:10px; color:#5b6b82; }
QLabel#PageTitle { font-size:19px; font-weight:700; color:#ecf0f1; }
QLabel#CardTitle { font-size:11.5px; font-weight:700; letter-spacing:.6px;
    color:#8b95a5; }
QGroupBox { background-color:#101a2e; border:1px solid #22314a;
    border-radius:10px; margin-top:12px; padding-top:8px;
    font-weight:600; color:#8892b0; }
QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 6px; }
QPushButton { background-color:#1e90ff; color:white; border:none;
    border-radius:6px; padding:6px 12px; font-weight:600; }
QPushButton:hover { background-color:#3aa0ff; }
QPushButton:pressed { background-color:#1060d0; }
QPushButton:disabled { background-color:#26324a; color:#5b6b82; }
QPushButton#Ghost { background-color:#1b2942; color:#c9d1d9; }
QPushButton#Ghost:hover { background-color:#24354f; }
QPushButton#Danger { background-color:#e74c3c; color:white; }
QPushButton#Danger:hover { background-color:#ff6b5a; }
QPushButton#Ok { background-color:#2ecc71; color:#062812; }
QPushButton#Ok:hover { background-color:#4ce08a; }
QPushButton#Nav { background-color:transparent; color:#9fb0c8;
    border:none; border-radius:7px; padding:9px 12px; text-align:left;
    font-size:13px; font-weight:600; }
QPushButton#Nav:hover { background-color:#16233a; color:#ecf0f1; }
QPushButton#Nav:checked { background-color:#16233a; color:#4c9aff; }
QLineEdit, QSpinBox, QDoubleSpinBox { background-color:#0b1524;
    border:1px solid #22314a; border-radius:6px; padding:5px 8px;
    color:#ecf0f1; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border:1px solid #1e90ff; }
QLineEdit:read-only { background-color:#0f1a2e; color:#8ee6ff;
    font-family:Consolas,monospace; }
QTableWidget { background-color:#0a1424; gridline-color:#1c2b45;
    border:1px solid #22314a; border-radius:6px; color:#c9d1d9;
    font-family:Consolas,monospace; font-size:11px; }
QTableWidget::item:selected { background-color:#1e90ff; color:#ffffff; }
QHeaderView::section { background-color:#16213e; color:#8892b0; border:none;
    padding:5px; font-weight:600; font-size:10px; }
QTextEdit { background-color:#0a1424; border:1px solid #22314a;
    border-radius:6px; color:#c9d1d9; font-family:Consolas,monospace;
    font-size:11px; }
QScrollBar:vertical { background:#0b1220; width:9px; margin:0; }
QScrollBar::handle:vertical { background:#2a3a5a; border-radius:4px; }
QScrollBar:horizontal { background:#0b1220; height:9px; margin:0; }
QScrollBar::handle:horizontal { background:#2a3a5a; border-radius:4px; }
"""


# ==========================================================================
# Widget vẽ: biểu đồ xu hướng + donut
# ==========================================================================
class TrendChart(QWidget):
    """Biểu đồ 2 đường: nhiệt độ (°C) và độ ẩm (%RH)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(240)
        # Giãn hết thẻ chứa (nếu không, biểu đồ bị co lại, thừa khoảng trống)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._points = []
        self._t_range = (0.0, 1.0)
        self._rh_range = (0.0, 100.0)

    def set_points(self, points):
        self._points = list(points or [])
        temps = [p["temp"] for p in self._points if p.get("temp") is not None]
        rhs = [p["rh"] for p in self._points if p.get("rh") is not None]
        if temps:
            self._t_range = (min(temps) - 1, max(temps) + 1)
        if rhs:
            self._rh_range = (min(rhs) - 2, max(rhs) + 2)
        self.update()

    def paintEvent(self, _event):
        p = self.painter() if hasattr(self, "painter") else QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("#101a2e"))

        padL, padR, padT, padB = 46, 44, 12, 24
        pw, ph = w - padL - padR, h - padT - padB
        if pw <= 0 or ph <= 0:
            return

        p.setPen(QPen(QColor("#22314a"), 1))
        p.drawRect(padL, padT, pw, ph)

        p.setPen(QPen(QColor("#1c2b45"), 1))
        for i in range(1, 5):
            y = padT + ph * i / 5.0
            p.drawLine(padL, int(y), padL + pw, int(y))

        # nhãn trục: trái = °C, phải = %RH
        f = QFont("Consolas", 8)
        p.setFont(f)
        t0, t1 = self._t_range
        r0, r1 = self._rh_range
        for i in range(6):
            y = padT + ph * i / 5.0
            p.setPen(QColor("#8b95a5"))
            p.drawText(int(padL - 38), int(y + 4), f"{t1 - (t1 - t0) * i / 5.0:5.1f}")
            p.setPen(QColor("#4c9aff"))
            p.drawText(int(padL + pw + 6), int(y + 4),
                       f"{r1 - (r1 - r0) * i / 5.0:5.1f}")

        if len(self._points) < 2:
            p.setPen(QColor("#5b6b82"))
            p.drawText(int(padL + 10), int(padT + ph / 2), "chưa có dữ liệu")
            return

        def line(key, rng, color):
            lo, hi = rng
            p.setPen(QPen(QColor(color), 1.8))
            started = False
            for i, pt in enumerate(self._points):
                v = pt.get(key)
                if v is None:
                    continue
                x = padL + pw * i / (len(self._points) - 1)
                y = padT + ph - (v - lo) / ((hi - lo) or 1) * ph
                if not started:
                    p.drawLine(int(x), int(y), int(x), int(y))
                    started = True
                else:
                    p.drawLine(int(px), int(py), int(x), int(y))
                px, py = x, y

        line("temp", self._t_range, "#f39c12")
        line("rh", self._rh_range, "#4c9aff")

        # mốc thời gian
        p.setPen(QColor("#5b6b82"))
        n_lab = min(5, len(self._points))
        for i in range(n_lab):
            idx = int(i * (len(self._points) - 1) / max(1, n_lab - 1))
            ts = self._points[idx].get("ts")
            if not ts:
                continue
            x = padL + pw * idx / (len(self._points) - 1)
            p.drawText(int(x - 16), int(h - 6),
                       time.strftime("%H:%M", time.localtime(ts)))


class DonutChart(QWidget):
    """Vòng tròn tỉ lệ bơm chạy / nghỉ."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 200)
        self._pct = 0

    def set_pct(self, pct):
        self._pct = max(0.0, min(100.0, float(pct or 0)))
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        side = min(w, h) - 16
        r = side / 2.0 - 18
        cx, cy = w / 2.0, h / 2.0
        p.setPen(QPen(QColor("#1e3a5f"), 22))
        p.drawEllipse(int(cx - r), int(cy - r), int(2 * r), int(2 * r))
        if self._pct > 0:
            p.setPen(QPen(QColor("#4c9aff"), 22))
            p.drawArc(int(cx - r), int(cy - r), int(2 * r), int(2 * r),
                      90 * 16, int(-self._pct / 100.0 * 360 * 16))


# ==========================================================================
# Worker: poll Modbus TCP tới PC1
# ==========================================================================
class MesWorker(QThread):
    """Đọc/ghi Modbus TCP tới PC1 trong thread riêng."""

    snapshot = pyqtSignal(dict)          # trạng thái + số liệu
    logged = pyqtSignal(str, str)        # (level, message)
    command_done = pyqtSignal(bool, str) # kết quả lệnh ghi
    link = pyqtSignal(bool, str)         # (link Modbus TCP còn sống?, lý do)

    def __init__(self, host, port, device_id=DEVICE_ID, history=None,
                 overheat_c=DEFAULT_OVERHEAT_C, profile=PROFILE_AUTO,
                 parent=None):
        super().__init__(parent)
        self._host = host
        self._port = int(port)
        self._device_id = int(device_id)
        self._history = history
        self._overheat_c = float(overheat_c)
        self._forced_profile = (None if profile in (PROFILE_AUTO, None)
                                else profile)
        self._profile = self._forced_profile or PROFILE_SHT30
        self._running = False
        self._cmd_q = queue.Queue()
        self._client = None
        self._prev = None
        self._events = []
        self._seq = 0
        self._linked = False

    # ------------------------------------------------------------------
    def stop(self):
        self._running = False
        self._cmd_q.put(None)

    def _set_link(self, up, reason=""):
        """Báo trạng thái link Modbus TCP tới PC1.

        Tách khỏi cờ `connected` của cảm biến RS485: link TCP còn sống
        KHÔNG đồng nghĩa với cảm biến còn sống.
        """
        changed = (up != self._linked)
        self._linked = up
        if changed or not up:
            self.link.emit(up, reason)

    def write_coil(self, coil, on):
        """Gửi lệnh FC05 (đi qua hàng đợi để không đụng client từ 2 thread)."""
        self._cmd_q.put(("coil", coil, bool(on)))

    def write_register(self, address, value):
        """Gửi lệnh FC06."""
        self._cmd_q.put(("reg", address, int(value)))

    def set_overheat_c(self, value):
        self._overheat_c = float(value)

    # ------------------------------------------------------------------
    def _client_ok(self):
        if self._client is not None:
            return True
        try:
            from pymodbus.client import ModbusTcpClient
        except Exception as exc:
            self.logged.emit("ERROR", f"Thiếu pymodbus: {exc}")
            return False
        self._client = ModbusTcpClient(self._host, port=self._port, timeout=3)
        if not self._client.connect():
            self._client = None
            return False
        self.logged.emit("OK", f"Đã kết nối {self._host}:{self._port} "
                              f"(device {self._device_id})")
        return True

    def _read_all(self):
        c, dev = self._client, self._device_id
        ir = c.read_input_registers(0, count=12, device_id=dev)
        hr = c.read_holding_registers(0, count=10, device_id=dev)
        co = c.read_coils(0, count=4, device_id=dev)
        if ir.isError():
            raise ModbusFault(4, getattr(ir, "exception_code", 0))
        if hr.isError():
            raise ModbusFault(3, getattr(hr, "exception_code", 0))
        if co.isError():
            raise ModbusFault(1, getattr(co, "exception_code", 0))
        return ir.registers, hr.registers, co.bits

    def _drain_commands(self):
        while True:
            try:
                item = self._cmd_q.get_nowait()
            except queue.Empty:
                return
            if item is None:
                return
            kind, addr, value = item
            try:
                if kind == "coil":
                    res = self._client.write_coil(addr, value,
                                                 device_id=self._device_id)
                    what = f"coil {addr} = {'ON' if value else 'OFF'}"
                else:
                    res = self._client.write_register(addr, value,
                                                     device_id=self._device_id)
                    what = f"thanh ghi {addr} = {value}"
                if res.isError():
                    self.command_done.emit(False, f"{what} bị từ chối")
                else:
                    self.command_done.emit(True, f"Đã gửi {what} tới PC1")
            except Exception as exc:
                self.command_done.emit(False, f"Lỗi ghi: {exc}")

    def _build_sample(self, ir, hr, co):
        """Ghép thanh ghi → mẫu telemetry theo profile của PC1."""
        prof = PROFILES[self._profile]
        ir_map = prof["ir"]
        hr_map = prof["hr"]

        def irv(name, default=0):
            addr = ir_map.get(name)
            if addr is None or addr >= len(ir):
                return default
            return ir[addr]

        def hrv(name, default=0):
            addr = hr_map.get(name)
            if addr is None or addr >= len(hr):
                return default
            return hr[addr]

        flags = irv("flags", 0)
        uptime = (irv("uptime_hi", 0) << 16) | irv("uptime_lo", 0)
        mode = irv("mode", 0)
        sample = {
            "ts": int(time.time()),
            "humidity": irv("humidity_pct", 0) / 10.0,
            "temp": signed16(irv("temp_c", 0)) / 10.0,
            "pump": bool(irv("pump", 0)),
            "fan": bool(irv("fan", 0)),
            "lamp": bool(irv("lamp", 0)),
            "mode": "MANUAL" if mode else "AUTO",
            "estop": bool(irv("estop", 0)),
            "sensor_ok": bool(flags & 0x01),
            "connected": bool(flags & 0x02),
            "cfg_valid": bool(flags & 0x04),
            "stale": bool(flags & 0x08),
            "data_age_s": irv("data_age_s", 0), "seq": irv("seq", 0),
            "uptime_s": uptime,
            "rh_on": signed16(hrv("rh_on", 0)) / 10.0,
            "rh_off": signed16(hrv("rh_off", 0)) / 10.0,
            "fan_on": signed16(hrv("fan_on", 0)) / 10.0,
            "fan_off": signed16(hrv("fan_off", 0)) / 10.0,
            "heat_on": signed16(hrv("heat_on", 0)) / 10.0,
            "heat_off": signed16(hrv("heat_off", 0)) / 10.0,
            "alert_temp": signed16(hrv("alert_temp", 0)) / 10.0,
            "pump_max_s": hrv("pump_max_s", 0),
            "heat_max_s": hrv("heat_max_s", 0),
            "fan_max_s": hrv("fan_max_s", 0),
            "failsafe_cycles": hrv("failsafe_cycles", 0),
            "coil_pump": bool(co[0]), "coil_fan": bool(co[1]),
            "coil_lamp": bool(co[2]), "coil_estop": bool(co[3]),
            "profile": self._profile,
        }
        return sample

    def run(self):
        self._running = True
        while self._running:
            try:
                if not self._client_ok():
                    self._set_link(False, self._tcp_reason())
                    self._stop_evt_wait(1.5)
                    continue
                self._drain_commands()
                ir, hr, co = self._read_all()
                self._update_profile(ir)
                self._set_link(True, "")
                sample = self._build_sample(ir, hr, co)
                self._seq += 1
                if self._history is not None:
                    self._history.add(sample)
                new_events = detect_events(self._prev, sample,
                                           overheat_c=self._overheat_c)
                self._prev = sample
                for e in new_events:
                    self._events.insert(0, e)
                    self.logged.emit(e.get("level", "INFO"), e.get("msg", ""))
                del self._events[200:]
                self.snapshot.emit(self._collect(sample))
            except ModbusFault as exc:
                # PC1 từ chối lệnh (IP lạ, sai device id, vi phạm ràng buộc)
                self._client = None
                msg = exc.reason
                self._set_link(False, msg)
                self.logged.emit("ERROR", msg)
                self._stop_evt_wait(1.5)
            except Exception as exc:
                self._client = None
                msg = f"Mất kết nối PC1: {exc}"
                self._set_link(False, msg)
                self.logged.emit("ERROR", msg)
                self._stop_evt_wait(1.5)
            self._stop_evt_wait(POLL_SECONDS)
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass

    def _tcp_reason(self):
        """Lý do không mở được TCP tới PC1 (theo thứ tự kiểm tra)."""
        from mes_diag import tcp_probe
        ok, code, msg = tcp_probe(self._host, self._port, timeout=2.0)
        if ok:
            return f"Không đọc được dữ liệu từ {self._host}:{self._port}."
        return msg

    def _stop_evt_wait(self, seconds):
        """Chờ có thể bị ngắt (dùng Event để thoát tức thì)."""
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(0.05)

    # ------------------------------------------------------------------
    @property
    def profile(self):
        """Profile bảng thanh ghi đang dùng (tự nhận diện hoặc ép cứng)."""
        return self._profile

    def set_profile(self, profile):
        """Ép cứng profile (None / 'auto' = tự nhận diện)."""
        self._forced_profile = (None if profile in (PROFILE_AUTO, None)
                                else profile)
        if self._forced_profile:
            self._profile = self._forced_profile

    def _update_profile(self, ir):
        """Tự nhận diện biến thể HMI từ input registers (nếu chưa ép cứng)."""
        if self._forced_profile:
            return self._profile
        detected = detect_profile(ir)
        if detected and detected != self._profile:
            self._profile = detected
        return self._profile

    # ------------------------------------------------------------------
    def _collect(self, sample):
        """Gom trạng thái + số liệu để GUI vẽ."""
        hist = self._history
        raw = hist.raw(hours=6) if hist else []
        buckets = hist.buckets(days=7) if hist else []
        stats = buckets + raw
        stats.sort(key=lambda s: s["ts"])
        return {
            "sample": sample,
            "series": build_series(raw, hours=6),
            "ratio": run_ratio(stats, days=7),
            "overheat": overheat_events(stats, threshold_c=self._overheat_c,
                                       days=7),
            "summary": summarize(stats, days=7, overheat_c=self._overheat_c),
            "events": list(self._events),
            "overheat_c": self._overheat_c,
        }


# ==========================================================================
# Worker chẩn đoán (chạy nền để không treo UI)
# ==========================================================================
class DiagWorker(QThread):
    """Chạy chẩn đoán kết nối PC1 ngoài thread GUI."""

    done = pyqtSignal(list)          # [(level, text), ...]

    def __init__(self, host, port, device_id=DEVICE_ID, parent=None):
        super().__init__(parent)
        self._host = host
        self._port = int(port)
        self._device_id = int(device_id)

    def run(self):
        try:
            steps = diagnose(self._host, self._port, self._device_id)
        except Exception as exc:
            steps = [("ERROR", f"Lỗi chẩn đoán: {type(exc).__name__}: {exc}")]
        self.done.emit(steps)


# ==========================================================================
# Cửa sổ chính
# ==========================================================================
class MesWindow(QMainWindow):
    PAGES = [
        ("Tổng quan", "Nhìn lại quá khứ"),
        ("Lịch sử cảnh báo", "Truy vết sự cố"),
        ("Quản lý bảo trì", "Nhìn về tương lai"),
        ("Công thức vận hành", "Ghi nhật xuống PC1"),
    ]

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT,
                 device_id=DEVICE_ID, db_path=None):
        super().__init__()
        self.setWindowTitle("AGRI MES · PC2")
        self.resize(1240, 780)
        self.setMinimumSize(1040, 660)
        self.setStyleSheet(DARK_QSS)

        self.device_id = int(device_id)
        self.history = History(db_path)
        self.worker = None
        self._diag = None
        self._last = {}
        self._build_ui(host, port)
        QTimer.singleShot(400, lambda: self._start(host, port))

    # ------------------------------------------------------------ UI
    def _build_ui(self, host, port):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Tạo stack TRƯỚC sidebar: sidebar gọi _show() cần self.stack.
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_overview())
        self.stack.addWidget(self._build_events())
        self.stack.addWidget(self._build_maintenance())
        self.stack.addWidget(self._build_formula())

        root.addWidget(self._build_sidebar())

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        right.addWidget(self._build_topbar(host, port))
        right.addWidget(self.stack, 1)
        wrap = QWidget()
        wrap.setLayout(right)
        root.addWidget(wrap, 1)
        self._show(0)          # sau khi mọi widget (title, stack) đã có

    def _build_sidebar(self):
        bar = QFrame()
        bar.setObjectName("Sidebar")
        bar.setFixedWidth(226)
        v = QVBoxLayout(bar)
        v.setContentsMargins(14, 16, 14, 14)
        v.setSpacing(10)

        row = QHBoxLayout()
        mark = QLabel("AM")
        mark.setStyleSheet("background:#1e90ff; color:white; font-weight:800;"
                           "border-radius:8px; padding:8px;")
        mark.setAlignment(Qt.AlignCenter)
        row.addWidget(mark)
        txt = QVBoxLayout()
        name = QLabel("AGRI MES")
        name.setObjectName("Brand")
        sub = QLabel("PC2 · tầng 3")
        sub.setObjectName("BrandSub")
        txt.addWidget(name)
        txt.addWidget(sub)
        row.addLayout(txt)
        v.addLayout(row)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#22314a;")
        v.addWidget(line)

        user = QHBoxLayout()
        av = QLabel("A")
        av.setStyleSheet("background:#16233a; color:#4c9aff; font-weight:700;"
                        "border-radius:12px; padding:6px;")
        av.setAlignment(Qt.AlignCenter)
        user.addWidget(av)
        utxt = QVBoxLayout()
        un = QLabel("Admin")
        un.setStyleSheet("font-weight:700;")
        ur = QLabel("quản lý ca")
        ur.setStyleSheet("font-size:10px; color:#5b6b82;")
        utxt.addWidget(un)
        utxt.addWidget(ur)
        user.addLayout(utxt)
        v.addLayout(user)

        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        line2.setStyleSheet("color:#22314a;")
        v.addWidget(line2)

        cap = QLabel("PHẦN HỆ")
        cap.setStyleSheet("font-size:10px; font-weight:700; color:#5b6b82;")
        v.addWidget(cap)

        self._nav = []
        for i, (title, desc) in enumerate(self.PAGES):
            btn = QPushButton(f"▸  {title}")
            btn.setObjectName("Nav")
            btn.setCheckable(True)
            btn.setToolTip(desc)
            btn.clicked.connect(lambda _=False, n=i: self._show(n))
            v.addWidget(btn)
            self._nav.append(btn)
        v.addStretch()
        return bar

    def _build_topbar(self, host, port):
        bar = QFrame()
        bar.setObjectName("Topbar")
        bar.setFixedHeight(58)
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 8, 16, 8)
        h.setSpacing(10)
        self.lbl_title = QLabel(self.PAGES[0][0])
        self.lbl_title.setObjectName("PageTitle")
        h.addWidget(self.lbl_title)
        h.addStretch()

        self.lbl_conn = QLabel("● PC1 offline")
        self.lbl_conn.setStyleSheet("color:#e74c3c; font-family:Consolas;"
                                   " font-size:11px;")
        h.addWidget(self.lbl_conn)
        self.lbl_msg = QLabel("")
        self.lbl_msg.setStyleSheet("color:#f39c12; font-size:11px;")
        self.lbl_msg.setMinimumWidth(0)
        self.lbl_msg.setMaximumWidth(420)
        self.lbl_msg.setToolTip("")
        h.addWidget(self.lbl_msg, 1)
        self.lbl_quality = QLabel("—")
        self.lbl_quality.setStyleSheet("color:#8b95a5; font-family:Consolas;"
                                      " font-size:11px;")
        h.addWidget(self.lbl_quality)
        self.lbl_clock = QLabel("--:--:--")
        self.lbl_clock.setStyleSheet("font-family:Consolas; font-size:14px;"
                                     " font-weight:700;")
        h.addWidget(self.lbl_clock)

        self.ed_host = QLineEdit(f"{host}:{port}")
        self.ed_host.setFixedWidth(150)
        self.ed_host.setToolTip("Địa chỉ PC1 mà bạn nhập ở ô 'Nhập ở PC2'.")
        h.addWidget(self.ed_host)
        self.cmb_profile = QComboBox()
        self.cmb_profile.setFixedWidth(185)
        self.cmb_profile.setToolTip(
            "Bảng thanh ghi của PC1. 'Tự động' = MES đoán từ dữ liệu thật.\n"
            "Chọn tay nếu HMI không có cảm biến (mọi giá trị đều 0).")
        self.cmb_profile.addItem("HMI: tự động nhận diện", PROFILE_AUTO)
        self.cmb_profile.addItem("HMI: SHTC30 3 kênh", PROFILE_SHT30)
        self.cmb_profile.addItem("HMI: SHTC20 RS-485", PROFILE_SHT20)
        self.cmb_profile.currentIndexChanged.connect(self._on_profile_changed)
        h.addWidget(self.cmb_profile)
        self.btn_conn = QPushButton("Kết nối")
        self.btn_conn.setObjectName("Ghost")
        self.btn_conn.clicked.connect(self._reconnect)
        h.addWidget(self.btn_conn)
        self.btn_diag = QPushButton("🩺 Chẩn đoán")
        self.btn_diag.setObjectName("Ghost")
        self.btn_diag.setToolTip("Kiểm tra TCP + Modbus tới PC1 và chỉ ra "
                                 "nguyên nhân nếu hỏng.")
        self.btn_diag.clicked.connect(self._run_diagnose)
        h.addWidget(self.btn_diag)

        QTimer.singleShot(1000, self._tick_clock)
        return bar

    # ---------------------------------------------------- trang 1: tổng quan
    def _build_overview(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(10)

        g1 = QGroupBox("NHIỆT ĐỘ / ĐỘ ẨM THEO TỪNG ĐIỂM ĐO · 6 GIỜ GẦN NHẤT")
        v1 = QVBoxLayout(g1)
        self.chart = TrendChart()
        v1.addWidget(self.chart)
        leg = QHBoxLayout()
        l1 = QLabel("▬ Nhiệt độ (°C)")
        l1.setStyleSheet("color:#f39c12; font-size:11px;")
        l2 = QLabel("▬ Độ ẩm (%RH)")
        l2.setStyleSheet("color:#4c9aff; font-size:11px;")
        leg.addWidget(l1)
        leg.addWidget(l2)
        leg.addStretch()
        v1.addLayout(leg)
        row.addWidget(g1, 2)

        g2 = QGroupBox("TỈ LỆ THỜI GIAN BƠM CHẠY / NGHỈ · 7 NGÀY")
        v2 = QVBoxLayout(g2)
        v2.addStretch(1)
        self.donut = DonutChart()
        v2.addWidget(self.donut)
        self.lbl_donut = QLabel("0%")
        self.lbl_donut.setAlignment(Qt.AlignCenter)
        self.lbl_donut.setStyleSheet("font-size:26px; font-weight:700;"
                                     " color:#4c9aff;")
        v2.addWidget(self.lbl_donut)
        self.lbl_donut_sub = QLabel("chưa có dữ liệu")
        self.lbl_donut_sub.setAlignment(Qt.AlignCenter)
        self.lbl_donut_sub.setStyleSheet("font-size:11px; color:#8b95a5;")
        v2.addWidget(self.lbl_donut_sub)
        v2.addStretch(1)
        row.addWidget(g2, 1)
        v.addLayout(row, 1)

        g3 = QGroupBox("SỐ ĐỢT QUÁ NHIỆT TRONG TUẦN (theo ngưỡng quá nhiệt)")
        v3 = QHBoxLayout(g3)
        self.lbl_oh = QLabel("0")
        self.lbl_oh.setStyleSheet("font-size:40px; font-weight:700;"
                                  " color:#2ecc71;")
        v3.addWidget(self.lbl_oh)
        col = QVBoxLayout()
        self.lbl_oh_text = QLabel("chưa đủ dữ liệu")
        self.lbl_oh_sub = QLabel("")
        self.lbl_oh_sub.setStyleSheet("font-size:11px; color:#8b95a5;")
        col.addWidget(self.lbl_oh_text)
        col.addWidget(self.lbl_oh_sub)
        v3.addLayout(col, 1)
        v.addWidget(g3)

        g4 = QGroupBox("TRẠNG THÁI HIỆN TẠI TỪ PC1")
        v4 = QGridLayout(g4)
        self.stat = {}
        for i, (key, label) in enumerate([
                ("rh", "Độ ẩm"), ("temp", "Nhiệt độ"), ("pump", "Bơm"),
                ("fan", "Quạt"), ("lamp", "Đèn"), ("mode", "Chế độ")]):
            cell = QLabel("--")
            cell.setStyleSheet("font-size:15px; font-weight:700;")
            cap = QLabel(label)
            cap.setStyleSheet("font-size:10px; color:#8b95a5;")
            v4.addWidget(cap, i // 3, (i % 3) * 2)
            v4.addWidget(cell, i // 3, (i % 3) * 2 + 1)
            self.stat[key] = cell
        v.addWidget(g4)
        return page

    # ------------------------------------------- trang 2: lịch sử cảnh báo
    def _build_events(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(14, 12, 14, 12)
        g = QGroupBox("LỊCH SỬ CẢNH BÁO · TRUY VẾT SỰ CỐ")
        vg = QVBoxLayout(g)
        self.tbl_events = QTableWidget(0, 4)
        self.tbl_events.setHorizontalHeaderLabels(
            ["Thời điểm", "Loại", "Mức", "Mô tả"])
        self.tbl_events.verticalHeader().setVisible(False)
        self.tbl_events.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_events.setSelectionBehavior(QAbstractItemView.SelectRows)
        hh = self.tbl_events.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        vg.addWidget(self.tbl_events)
        v.addWidget(g)
        return page

    # -------------------------------------------- trang 3: quản lý bảo trì
    def _build_maintenance(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(14, 12, 14, 12)

        g = QGroupBox("TÌNH TRẠNG THIẾT BỊ")
        vg = QGridLayout(g)
        self.mt = {}
        for i, (key, label) in enumerate([
                ("uptime", "Thời gian chạy HMI"), ("age", "Tuổi dữ liệu"),
                ("sensor", "Cảm biến"), ("pump_max", "Watchdog bơm"),
                ("heat_max", "Watchdog đèn"), ("failsafe", "Failsafe")]):
            cell = QLabel("--")
            cell.setStyleSheet("font-size:15px; font-weight:700;")
            cap = QLabel(label)
            cap.setStyleSheet("font-size:10px; color:#8b95a5;")
            vg.addWidget(cap, i // 3, (i % 3) * 2)
            vg.addWidget(cell, i // 3, (i % 3) * 2 + 1)
            self.mt[key] = cell
        v.addWidget(g)

        g2 = QGroupBox("NGƯỠNG QUÁ NHIỆT ĐANG DÙNG (phía MES)")
        v2 = QHBoxLayout(g2)
        v2.addWidget(QLabel("Ngưỡng (°C)"))
        self.sp_oh = QDoubleSpinBox()
        self.sp_oh.setRange(10.0, 80.0)
        self.sp_oh.setDecimals(1)
        self.sp_oh.setSingleStep(0.5)
        self.sp_oh.setValue(DEFAULT_OVERHEAT_C)
        v2.addWidget(self.sp_oh)
        btn = QPushButton("Lưu")
        btn.clicked.connect(self._save_overheat)
        v2.addWidget(btn)
        v2.addStretch()
        self.lbl_oh_save = QLabel("")
        self.lbl_oh_save.setStyleSheet("font-size:11px; color:#8b95a5;")
        v2.addWidget(self.lbl_oh_save)
        v.addWidget(g2)

        g3 = QGroupBox("SỐ LIỆU 7 NGÀY")
        v3 = QGridLayout(g3)
        self.wk = {}
        for i, (key, label) in enumerate([
                ("oh7", "Đợt quá nhiệt"), ("tmax", "Nhiệt độ cao nhất"),
                ("rhmax", "Độ ẩm cao nhất"), ("pump", "Tỉ lệ bơm chạy"),
                ("samples", "Số phút dữ liệu"), ("oldest", "Mẫu mới nhất")]):
            cell = QLabel("--")
            cell.setStyleSheet("font-size:15px; font-weight:700;")
            cap = QLabel(label)
            cap.setStyleSheet("font-size:10px; color:#8b95a5;")
            v3.addWidget(cap, i // 3, (i % 3) * 2)
            v3.addWidget(cell, i // 3, (i % 3) * 2 + 1)
            self.wk[key] = cell
        v.addWidget(g3)
        return page

    # ----------------------------------------- trang 4: công thức vận hành
    def _build_formula(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(14, 12, 14, 12)

        g = QGroupBox("NGƯỠNG ĐIỀU KHIỂN · GHI XUỐNG PC1 (FC06)")
        vg = QGridLayout(g)
        self.sp_formula = {}
        self._formula_grid = vg
        self._formula_row = 0
        row = QHBoxLayout()
        b_save = QPushButton("Ghi xuống PC1")
        b_save.clicked.connect(self._save_formula)
        row.addWidget(b_save)
        b_reload = QPushButton("Nạp lại từ PC1")
        b_reload.setObjectName("Ghost")
        b_reload.clicked.connect(self._reload_formula)
        row.addWidget(b_reload)
        b_tag = QPushButton("Xuất tag cho MES")
        b_tag.setObjectName("Ghost")
        b_tag.clicked.connect(self._export_tags)
        row.addWidget(b_tag)
        row.addStretch()
        self.lbl_formula_note = QLabel("")
        self.lbl_formula_note.setStyleSheet("font-size:11px; color:#8b95a5;")
        row.addWidget(self.lbl_formula_note)
        vg.addLayout(row, 30, 0, 1, 6)
        v.addWidget(g)

        g2 = QGroupBox("ĐIỀU KHIỂN TẢI TRỰC TIẾP (FC05)")
        v2 = QGridLayout(g2)
        self._ctrl_grid = v2
        self._ctrl_row = 0
        self.lbl_ctrl_note = QLabel("")
        self.lbl_ctrl_note.setStyleSheet("font-size:11px; color:#8b95a5;")
        v.addWidget(g2)
        # Dựng ô nhập theo profile mặc định (sẽ dựng lại khi nhận diện xong)
        self._populate_formula(PROFILE_SHT30)
        return page

    def _populate_formula(self, profile):
        """Dựng ô nhập ngưỡng + nút điều khiển theo profile của PC1.

        2 biến thể HMI có bảng thanh ghi khác nhau nên danh sách ô nhập và
        danh sách tải điều khiển cũng khác nhau.
        """
        prof = PROFILES.get(profile, PROFILES[PROFILE_SHT30])

        # -- dọn ô cũ --
        while self._formula_grid.count():
            item = self._formula_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        while self._ctrl_grid.count():
            item = self._ctrl_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self.sp_formula = {}
        self._formula_row = 0
        self._ctrl_row = 0

        # -- ô ngưỡng --
        for f in prof["fields"]:
            cap = QLabel(f["label"])
            cap.setStyleSheet("font-size:11px; color:#8b95a5;")
            if f["kind"] == "int":
                sp = QSpinBox()
                sp.setRange(int(f["min"]), int(f["max"]))
                sp.setSingleStep(int(f["step"]))
            else:
                sp = QDoubleSpinBox()
                sp.setRange(float(f["min"]), float(f["max"]))
                sp.setDecimals(1)
                sp.setSingleStep(float(f["step"]))
            r = self._formula_row
            self._formula_grid.addWidget(cap, r // 3, (r % 3) * 2)
            self._formula_grid.addWidget(sp, r // 3, (r % 3) * 2 + 1)
            self.sp_formula[f["ui"]] = sp
            self._formula_row += 1

        # -- nút điều khiển tải --
        coils = prof["coil"]
        for label, key in prof["loads"]:
            if key not in coils:
                continue
            coil = coils[key]
            self._ctrl_grid.addWidget(QLabel(label), self._ctrl_row, 0)
            on = QPushButton("Bật")
            on.setObjectName("Ok")
            on.clicked.connect(lambda _=False, c=coil: self._send_coil(c, True))
            off = QPushButton("Tắt")
            off.setObjectName("Ghost")
            off.clicked.connect(lambda _=False, c=coil: self._send_coil(c, False))
            self._ctrl_grid.addWidget(on, self._ctrl_row, 1)
            self._ctrl_grid.addWidget(off, self._ctrl_row, 2)
            self._ctrl_row += 1
        if "estop" in coils:
            estop_c = coils["estop"]
            self._ctrl_grid.addWidget(QLabel("E-STOP"), self._ctrl_row, 0)
            estop = QPushButton("Kích hoạt")
            estop.setObjectName("Danger")
            estop.clicked.connect(lambda _=False, c=estop_c:
                                  self._send_coil(c, True))
            rel = QPushButton("Giải phóng")
            rel.setObjectName("Ghost")
            rel.clicked.connect(lambda _=False, c=estop_c:
                                self._send_coil(c, False))
            self._ctrl_grid.addWidget(estop, self._ctrl_row, 1)
            self._ctrl_grid.addWidget(rel, self._ctrl_row, 2)
            self._ctrl_row += 1
        self._ctrl_grid.addWidget(self.lbl_ctrl_note, self._ctrl_row, 0, 1, 3)

    def _on_profile_changed(self):
        """Người dùng đổi profile HMI → dựng lại ô nhập + khởi động lại."""
        profile = self.cmb_profile.currentData()
        self._populate_formula(self._current_profile())
        self._formula_loaded = False
        if self.worker is not None:
            try:
                self.worker.set_profile(profile)
            except Exception:
                pass

    def _current_profile(self):
        """Profile đang dùng (ép cứng nếu người dùng chọn, không thì tự đoán)."""
        forced = self.cmb_profile.currentData()
        if forced and forced != PROFILE_AUTO:
            return forced
        if self.worker is not None:
            try:
                return self.worker.profile
            except Exception:
                pass
        return PROFILE_SHT30

    # ------------------------------------------------------------ logic
    def _show(self, index):
        self.stack.setCurrentIndex(index)
        for i, b in enumerate(self._nav):
            b.setChecked(i == index)
        self.lbl_title.setText(self.PAGES[index][0])

    def _tick_clock(self):
        self.lbl_clock.setText(time.strftime("%H:%M:%S"))
        QTimer.singleShot(1000, self._tick_clock)

    def _host_port(self):
        text = self.ed_host.text().strip()
        if ":" in text:
            host, _, port = text.rpartition(":")
            try:
                return host.strip(), int(port)
            except ValueError:
                return text, DEFAULT_PORT
        return text or DEFAULT_HOST, DEFAULT_PORT

    def _start(self, host, port):
        self._stop_worker()
        profile = self.cmb_profile.currentData() or PROFILE_AUTO
        self.worker = MesWorker(host, port, self.device_id, self.history,
                                profile=profile)
        self.worker.snapshot.connect(self._on_snapshot)
        self.worker.logged.connect(self._on_log)
        self.worker.command_done.connect(self._on_cmd_done)
        self.worker.link.connect(self._on_link)
        self.worker.start()

    def _reconnect(self):
        host, port = self._host_port()
        self._start(host, port)

    def _stop_worker(self):
        if self.worker is not None:
            try:
                self.worker.stop()
                self.worker.wait(2500)
            except Exception:
                pass
            self.worker = None

    def _on_log(self, level, msg):
        """Nhật ký từ worker — hiện ở ô thông báo, KHÔNG ghi đè trạng thái link."""
        self.lbl_msg.setText(msg[:120])
        self.lbl_msg.setToolTip(msg)
        color = {"ERROR": "#e74c3c", "WARN": "#f39c12",
                 "OK": "#2ecc71"}.get(level, "#8b95a5")
        self.lbl_msg.setStyleSheet(f"color:{color}; font-size:11px;")

    def _on_link(self, up, reason=""):
        """Trạng thái link Modbus TCP tới PC1 (độc lập cờ cảm biến RS485)."""
        if up:
            self.lbl_conn.setText("● PC1 online")
            self.lbl_conn.setStyleSheet(
                "color:#2ecc71; font-family:Consolas; font-size:11px;")
            self.lbl_msg.setText("")
            self.lbl_msg.setToolTip("")
        else:
            self.lbl_conn.setText("● PC1 offline")
            self.lbl_conn.setStyleSheet(
                "color:#e74c3c; font-family:Consolas; font-size:11px;")
            if reason:
                self.lbl_msg.setText(reason[:120])
                self.lbl_msg.setToolTip(reason)
                self.lbl_msg.setStyleSheet("color:#e74c3c; font-size:11px;")

    def _run_diagnose(self):
        """Chẩn đoán kết nối PC1 (chạy nền, không treo UI)."""
        host, port = self._host_port()
        if self._diag is not None and self._diag.isRunning():
            return
        self.lbl_msg.setText("Đang chẩn đoán…")
        self._diag = DiagWorker(host, port, self.device_id, self)
        self._diag.done.connect(self._show_diag)
        self._diag.start()

    def _show_diag(self, steps):
        dlg = QMessageBox(self)
        dlg.setWindowTitle("Chẩn đoán kết nối PC1")
        dlg.setIcon(QMessageBox.Information)
        dlg.setText("\n\n".join(f"{lv} · {txt}" for lv, txt in steps))
        dlg.exec_()

    def _on_cmd_done(self, ok, msg):
        self.lbl_formula_note.setText(msg)
        self.lbl_ctrl_note.setText(msg)
        self.lbl_formula_note.setStyleSheet(
            "font-size:11px; color:#2ecc71;" if ok else "font-size:11px; color:#e74c3c;")
        self.lbl_ctrl_note.setStyleSheet(
            "font-size:11px; color:#2ecc71;" if ok else "font-size:11px; color:#e74c3c;")

    # ----------------------------------------------------------- hiển thị
    def _on_snapshot(self, data):
        self._last = data
        s = data["sample"]
        ratio = data["ratio"]
        summary = data["summary"]
        over = data["overheat"]

        # Trạng thái LINK Modbus do tín hiệu `link` quản lý — KHÔNG suy ra
        # từ cờ `connected` (đó là cảm biến RS485, không phải link TCP).
        age = int(s.get("data_age_s", 0) or 0)
        stale = bool(s.get("stale"))
        sensor_ok = bool(s.get("sensor_ok"))
        no_data = age >= 65535
        age_txt = "chưa có dữ liệu" if no_data else f"{age} s"
        sensor_txt = "cảm biến OK" if sensor_ok else "CẢM BIẾN MẤT"
        self.lbl_quality.setText(
            f"{sensor_txt} · dữ liệu {'CŨ' if stale else 'tươi'} "
            f"{age_txt} · seq {s.get('seq', 0)}")
        bad = stale or not sensor_ok
        self.lbl_quality.setStyleSheet(
            "color:#e74c3c; font-family:Consolas; font-size:11px;" if bad
            else "color:#8b95a5; font-family:Consolas; font-size:11px;")

        self.chart.set_points(data["series"])
        self.donut.set_pct(ratio.get("run_pct", 0))
        self.lbl_donut.setText(f"{ratio.get('run_pct', 0)}%")
        self.lbl_donut_sub.setText(
            f"chạy {ratio.get('run_s', 0)} s · nghỉ {ratio.get('idle_s', 0)} s")

        self.lbl_oh.setText(str(len(over)))
        self.lbl_oh.setStyleSheet(
            "font-size:40px; font-weight:700; color:#2ecc71;" if not over
            else "font-size:40px; font-weight:700; color:#e74c3c;")
        if not summary.get("samples"):
            self.lbl_oh_text.setText("chưa đủ dữ liệu")
            self.lbl_oh_sub.setText("Bơm chạy 0% thời gian · 0 phút dữ liệu")
        elif over:
            last = over[-1]
            self.lbl_oh_text.setText("lần quá nhiệt gần nhất")
            self.lbl_oh_sub.setText(
                f"{time.strftime('%d/%m %H:%M', time.localtime(last['start_ts']))}"
                f" · đỉnh {last['peak_temp']:.1f} °C · "
                f"{last['duration_s']} s · ngưỡng {data['overheat_c']} °C")
        else:
            self.lbl_oh_text.setText("không có lần quá nhiệt")
            self.lbl_oh_sub.setText(
                f"Bơm chạy {ratio.get('run_pct', 0)}% thời gian · "
                f"{summary.get('samples', 0)} phút dữ liệu · "
                f"ngưỡng {data['overheat_c']} °C")

        self.stat["rh"].setText(f"{s.get('humidity', 0):.1f} %RH")
        self.stat["temp"].setText(f"{s.get('temp', 0):.1f} °C")
        self.stat["pump"].setText("ON" if s.get("pump") else "OFF")
        self.stat["fan"].setText("ON" if s.get("fan") else "OFF")
        self.stat["lamp"].setText("ON" if s.get("lamp") else "OFF")
        self.stat["mode"].setText(s.get("mode", "--"))

        self.mt["uptime"].setText(self._dur(s.get("uptime_s")))
        self.mt["age"].setText(
            ("chưa có dữ liệu" if no_data else f"{age} s")
            + (" (CŨ)" if stale and not no_data else ""))
        self.mt["sensor"].setText(
            "OK" if s.get("sensor_ok") else "MẤT")
        self.mt["pump_max"].setText(f"{s.get('pump_max_s', 0)} s")
        self.mt["heat_max"].setText(f"{s.get('heat_max_s', 0)} s")
        self.mt["failsafe"].setText(f"{s.get('failsafe_cycles', 0)} chu kỳ")

        self.wk["oh7"].setText(f"{len(over)} lần")
        self.wk["tmax"].setText(f"{summary.get('temp_max', 0):.1f} °C")
        self.wk["rhmax"].setText(f"{summary.get('rh_max', 0):.1f} %RH")
        self.wk["pump"].setText(f"{ratio.get('run_pct', 0)}%")
        self.wk["samples"].setText(f"{summary.get('samples', 0)} phút")
        last_ts = s.get("ts")
        self.wk["oldest"].setText(
            time.strftime("%H:%M:%S", time.localtime(last_ts)) if last_ts else "--")

        # nạp ngưỡng vào ô (chỉ khi chưa sửa tay)
        profile = s.get("profile") or self._current_profile()
        if getattr(self, "_ui_profile", None) != profile:
            self._ui_profile = profile
            self._populate_formula(profile)
            self._formula_loaded = False
            self.lbl_formula_note.setText(
                f"HMI: {PROFILES[profile]['label']}")
        if not getattr(self, "_formula_loaded", False):
            fields = {f["ui"]: f for f in profile_fields(profile)}
            for key, sp in self.sp_formula.items():
                f = fields.get(key)
                if not f:
                    continue
                val = s.get(f["sample"])
                if val is None:
                    continue
                if f["kind"] == "int":
                    sp.setValue(int(val))
                else:
                    sp.setValue(float(val))
            self._formula_loaded = True
        self.sp_oh.blockSignals(True)
        self.sp_oh.setValue(float(data.get("overheat_c", DEFAULT_OVERHEAT_C)))
        self.sp_oh.blockSignals(False)

        self._render_events(data.get("events", []))

    def _dur(self, sec):
        sec = int(max(0, sec or 0))
        h, m = sec // 3600, (sec % 3600) // 60
        return f"{h} giờ {m} phút" if h else f"{m} phút"

    def _render_events(self, events):
        self.tbl_events.setRowCount(len(events))
        for r, e in enumerate(events):
            vals = [time.strftime("%d/%m %H:%M:%S",
                                  time.localtime(e.get("ts", 0))),
                    e.get("kind", ""), e.get("level", ""), e.get("msg", "")]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(str(val))
                if c == 2:
                    color = {"ERROR": "#e74c3c", "WARN": "#f39c12",
                             "OK": "#2ecc71"}.get(val, "#c9d1d9")
                    it.setForeground(QColor(color))
                self.tbl_events.setItem(r, c, it)

    # ------------------------------------------------------------- lệnh ghi
    def _send_coil(self, coil, on):
        if self.worker is None:
            self._on_cmd_done(False, "Chưa kết nối PC1.")
            return
        self.worker.write_coil(coil, on)

    def _save_formula(self):
        if self.worker is None:
            self._on_cmd_done(False, "Chưa kết nối PC1.")
            return
        profile = self._current_profile()
        fields = {f["ui"]: f for f in profile_fields(profile)}
        limits = profile_limits(profile)
        # Thứ tự ghi theo profile: ngưỡng "tắt" TRƯỚC, "bật" SAU — tránh
        # trạng thái tạm thời vi phạm (bật ≥ tắt) bị PC1 từ chối.
        order = [k for k in WRITE_ORDER.get(profile, []) if k in self.sp_formula]
        for key in self.sp_formula:
            if key not in order:
                order.append(key)
        seq = []
        for key in order:
            sp = self.sp_formula.get(key)
            if sp is None:
                continue
            f = fields[key]
            val = sp.value()
            lo, hi = limits.get(f["addr"], (-1e9, 1e9))
            if not (lo <= val <= hi):
                self._on_cmd_done(False, f"{f['label']} = {val} ngoài khoảng "
                                        f"hợp lệ ({lo}..{hi})")
                return
            seq.append((f["addr"], val, f))
        for addr, val, f in seq:
            # giá trị nguyên (watchdog, failsafe) gửi thẳng; ngưỡng °C/%RH ×10
            raw = int(val) if f["kind"] == "int" else int(round(val * 10))
            self.worker.write_register(addr, raw)
        self.lbl_formula_note.setText(
            f"Đang ghi {len(seq)} ngưỡng xuống PC1…")

    def _reload_formula(self):
        self._formula_loaded = False
        self.lbl_formula_note.setText("Đang nạp lại từ PC1…")

    def _save_overheat(self):
        val = self.sp_oh.value()
        if self.worker is not None:
            self.worker.set_overheat_c(val)
        self.lbl_oh_save.setText(f"Đã đặt ngưỡng {val} °C (áp dụng ngay)")

    def _export_tags(self):
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "mes_tag_contract")
        profile = self._current_profile()
        contract = build_tag_contract(self.device_id, profile)
        try:
            with open(base + ".csv", "w", encoding="utf-8-sig", newline="") as f:
                f.write(contract_to_csv(contract))
            with open(base + ".json", "w", encoding="utf-8") as f:
                f.write(contract_to_json(contract))
        except Exception as exc:
            self.lbl_formula_note.setText(f"Lỗi xuất file: {exc}")
            return
        self.lbl_formula_note.setText(f"Đã xuất {len(contract)} tag: "
                                      f"{os.path.basename(base)}.csv/.json")

    # ------------------------------------------------------------- đóng app
    def closeEvent(self, event):
        self._stop_worker()
        try:
            self.history.close()
        except Exception:
            pass
        event.accept()


def main():
    ap = argparse.ArgumentParser(description="AGRI MES · PC2")
    ap.add_argument("--host", default=DEFAULT_HOST, help="IP của PC1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--device", type=int, default=DEVICE_ID)
    ap.add_argument("--db", default=None, help="file SQLite lịch sử")
    ap.add_argument("--diagnose", action="store_true",
                    help="chẩn đoán kết nối PC1 rồi thoát (không mở UI)")
    args = ap.parse_args()

    if args.diagnose:
        steps = diagnose(args.host, args.port, args.device)
        print(f"=== Chẩn đoán MES → {args.host}:{args.port} (device {args.device}) ===")
        for level, text in steps:
            print(f"[{level:5}] {text}")
        return 0 if steps and steps[0][0] == "OK" else 1

    app = QApplication(sys.argv)
    win = MesWindow(args.host, args.port, args.device, args.db)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
