# -*- coding: utf-8 -*-
"""Cổng Modbus TCP — phương án A cho kiến trúc mạng.

    PC1 (máy HMI, chủ bus RS485)  ──►  Modbus TCP server :502  ──►  PC2 (SCADA/ERP)

PC1 giữ vai trò RTU↔TCP: bus RS485 vẫn nói Modbus RTU tới SHTC30 + relay
(như hiện tại), đồng thời mở một cổng Modbus TCP để PC2 đọc/ghi bằng
client Modbus TCP tiêu chuẩn (pymodbus, SCADA, PLC...).

Bảng thanh ghi (đơn vị địa chỉ 0-based, giá trị ×10 cho số thực):

  Input registers (FC04) — trạng thái sống, đọc:
    0  RH            %RH ×10
    1  T             °C  ×10 (signed)
    2  BƠM           0/1
    3  QUẠT          0/1
    4  ĐÈN           0/1
    5  MODE          0=AUTO 1=MANUAL
    6  E-STOP        0/1
    7  FLAGS         b0 sensor_ok · b1 connected · b2 cfg_valid
    8  UPTIME_S      giây (16 bit thấp)
    9  UPTIME_S      giây (16 bit cao)

  Holding registers (FC03 đọc / FC06 ghi) — cấu hình:
    0  RH_ON   %RH ×10      4  HEAT_ON  °C ×10
    1  RH_OFF  %RH ×10      5  HEAT_OFF °C ×10
    2  FAN_ON  °C  ×10      6  watchdog bơm (s)
    3  FAN_OFF °C  ×10      7  watchdog đèn (s)
                              8  failsafe_cycles
                              9  MODE lệnh: 0=AUTO 1=MANUAL (ghi để đổi chế độ)

  Coils (FC01 đọc / FC05 ghi) — trạng thái + lệnh:
    0  BƠM        đọc: trạng thái thật   ghi: bật/tắt
    1  QUẠT       đọc: trạng thái thật   ghi: bật/tắt
    2  ĐÈN        đọc: trạng thái thật   ghi: bật/tắt
    3  E-STOP     đọc: trạng thái thật   ghi 1=kích hoạt, 0=giải phóng

  Thiết bị (device id):
    254  HMI Bridge (cầu nối ERP) — mặc định
    0    catch-all → trả về HMI Bridge
    1    SHTC30 mirror — đúng địa chỉ thanh ghi như RTU (reg0=T×10, reg1=RH×10)

Mọi lệnh ghi từ PC2 KHÔNG đi thẳng xuống bus RS485: chúng được đẩy về
HMI qua callback on_command / on_config để HMI áp dụng đúng logic an toàn
(E-STOP, liên động quạt↔đèn, watchdog, khóa AUTO khi mất cảm biến).
"""

import asyncio
import csv
import io
import ipaddress
import json
import socket
import threading
import time
from contextvars import ContextVar
from functools import partial

try:
    from pymodbus.constants import ExcCodes
    from pymodbus.pdu.device import ModbusDeviceIdentification
    from pymodbus.pdu.exceptionresponse import ExceptionResponse
    from pymodbus.server import ModbusTcpServer
    from pymodbus.server.requesthandler import ServerRequestHandler
    from pymodbus.simulator import DataType, SimData, SimDevice
    PYMODBUS_OK = True
except Exception:                                    # pragma: no cover
    ExcCodes = None
    ExceptionResponse = None
    ModbusDeviceIdentification = None
    ModbusTcpServer = None
    ServerRequestHandler = None
    DataType = SimData = SimDevice = None
    PYMODBUS_OK = False


# --------------------------------------------------------------------------
# Bảng thanh ghi
# --------------------------------------------------------------------------
IR_RH, IR_T, IR_PUMP, IR_FAN, IR_LAMP, IR_MODE, IR_ESTOP, IR_FLAGS = range(8)
IR_UPTIME_LO, IR_UPTIME_HI = 8, 9
IR_DATA_AGE, IR_SEQ = 10, 11

HR_RH_ON, HR_RH_OFF, HR_FAN_ON, HR_FAN_OFF = 0, 1, 2, 3
HR_HEAT_ON, HR_HEAT_OFF = 4, 5
HR_PUMP_MAX, HR_HEAT_MAX, HR_FAILSAFE = 6, 7, 8
HR_MODE = 9

COIL_PUMP, COIL_FAN, COIL_LAMP, COIL_ESTOP = 0, 1, 2, 3

N_IR = 16
N_HR = 16
N_COIL = 16

# Ngưỡng hợp lệ của từng thanh ghi cấu hình (dùng để chặn ghi rác).
HR_LIMITS = {
    HR_RH_ON: (0.0, 100.0), HR_RH_OFF: (0.0, 100.0),
    HR_FAN_ON: (-10.0, 60.0), HR_FAN_OFF: (-10.0, 60.0),
    HR_HEAT_ON: (-10.0, 60.0), HR_HEAT_OFF: (-10.0, 60.0),
    HR_PUMP_MAX: (30, 7200), HR_HEAT_MAX: (30, 7200),
    HR_FAILSAFE: (1, 999),
}

# Dữ liệu cũ hơn ngưỡng này (giây) → báo cho MES biết là không nên dùng.
STALE_AFTER_S = 10


# --------------------------------------------------------------------------
# Contract cho MES (import tag được, không phải đọc README)
# --------------------------------------------------------------------------
def build_tag_contract(device_id=254):
    """Danh sách tag theo chuẩn MES/SCADA import (CSV/JSON).

    Mỗi tag: device_id, area, address, name, type, scale, unit, writable.
    `scale` = hệ số nhân để ra giá trị kỹ thuật (vd 0.1 → reg 425 = 42.5).
    """
    t = [
        # ---- input registers (FC04) ----
        ("input", IR_RH, "humidity_pct", "int16", 0.1, "%RH", False,
         "Độ ẩm không khí"),
        ("input", IR_T, "temp_c", "int16", 0.1, "degC", False,
         "Nhiệt độ không khí"),
        ("input", IR_PUMP, "pump_state", "bool", 1, "", False,
         "Bơm đang chạy"),
        ("input", IR_FAN, "fan_state", "bool", 1, "", False,
         "Quạt đang chạy"),
        ("input", IR_LAMP, "lamp_state", "bool", 1, "", False,
         "Đèn halogen đang sáng"),
        ("input", IR_MODE, "mode", "enum", 1, "", False,
         "0=AUTO 1=MANUAL"),
        ("input", IR_ESTOP, "estop", "bool", 1, "", False,
         "E-STOP đang khóa"),
        ("input", IR_FLAGS, "flags", "bitfield", 1, "", False,
         "b0 sensor_ok · b1 connected · b2 cfg_valid · b3 dữ liệu cũ"),
        ("input", IR_UPTIME_LO, "uptime_s_lo", "uint32", 1, "s", False,
         "Thời gian chạy (16 bit thấp)"),
        ("input", IR_UPTIME_HI, "uptime_s_hi", "uint32", 1, "s", False,
         "Thời gian chạy (16 bit cao)"),
        ("input", IR_DATA_AGE, "data_age_s", "uint16", 1, "s", False,
         "Tuổi dữ liệu cảm biến — >10 s là cũ"),
        ("input", IR_SEQ, "seq", "uint16", 1, "", False,
         "Bộ đếm số lần cập nhật (chống đọc trùng)"),
        # ---- holding registers (FC03/FC06) ----
        ("holding", HR_RH_ON, "rh_on_pct", "int16", 0.1, "%RH", True,
         "Ngưỡng bật bơm"),
        ("holding", HR_RH_OFF, "rh_off_pct", "int16", 0.1, "%RH", True,
         "Ngưỡng tắt bơm"),
        ("holding", HR_FAN_ON, "fan_on_c", "int16", 0.1, "degC", True,
         "Ngưỡng bật quạt"),
        ("holding", HR_FAN_OFF, "fan_off_c", "int16", 0.1, "degC", True,
         "Ngưỡng tắt quạt"),
        ("holding", HR_HEAT_ON, "heat_on_c", "int16", 0.1, "degC", True,
         "Ngưỡng bật đèn sưởi"),
        ("holding", HR_HEAT_OFF, "heat_off_c", "int16", 0.1, "degC", True,
         "Ngưỡng tắt đèn sưởi"),
        ("holding", HR_PUMP_MAX, "pump_watchdog_s", "uint16", 1, "s", True,
         "Watchdog bơm"),
        ("holding", HR_HEAT_MAX, "lamp_watchdog_s", "uint16", 1, "s", True,
         "Watchdog đèn"),
        ("holding", HR_FAILSAFE, "failsafe_cycles", "uint16", 1, "", True,
         "Số chu kỳ failsafe"),
        ("holding", HR_MODE, "mode_cmd", "enum", 1, "", True,
         "Lệnh chế độ: 0=AUTO 1=MANUAL"),
        # ---- coils (FC01/FC05) ----
        ("coil", COIL_PUMP, "pump_cmd", "bool", 1, "", True,
         "Bật/tắt bơm"),
        ("coil", COIL_FAN, "fan_cmd", "bool", 1, "", True,
         "Bật/tắt quạt"),
        ("coil", COIL_LAMP, "lamp_cmd", "bool", 1, "", True,
         "Bật/tắt đèn halogen"),
        ("coil", COIL_ESTOP, "estop_cmd", "bool", 1, "", True,
         "1 = kích hoạt E-STOP, 0 = giải phóng"),
    ]
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
        for area, addr, name, vtype, scale, unit, writable, desc in t
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


def _port_busy(host, port):
    """Cổng đang bị chiếm bởi tiến trình khác không?

    Dùng bind thật KHÔNG bật SO_REUSEADDR → Windows sẽ báo lỗi nếu cổng
    đã có socket khác đang listen. (pymodbus bật SO_REUSEADDR nên nếu ta
    không kiểm tra trước, tiến trình thứ 2 "bind thành công" nhưng không
    bao giờ nhận kết nối → MES đọc nhầm server.)
    """
    for family, addr in ((socket.AF_INET, (host or "0.0.0.0", port)),):
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET,
                                    socket.SO_EXCLUSIVEADDRUSE, 1)
                except OSError:
                    pass
            sock.bind(addr)
            return False          # bind được → cổng trống
        except OSError:
            return True           # bind lỗi → cổng đã bị chiếm
        finally:
            sock.close()
    return False



def _signed16(value):
    """Đóng gói số có dấu 16 bit (Modbus không có số âm không dấu)."""
    return int(value) & 0xFFFF


def _unpack16(raw):
    """Giải mã thanh ghi 16 bit có dấu."""
    raw = int(raw) & 0xFFFF
    return raw - 0x10000 if raw >= 0x8000 else raw


def encode_input_registers(state):
    """Trạng thái sống → input registers (FC04)."""
    state = state or {}
    flags = 0
    if state.get("sensor_ok"):
        flags |= 0x01
    if state.get("connected"):
        flags |= 0x02
    if state.get("cfg_valid", True):
        flags |= 0x04
    age = max(0, int(state.get("data_age_s") or 0))
    if age > STALE_AFTER_S:
        flags |= 0x08                    # dữ liệu cũ → MES không nên dùng
    uptime = max(0, int(state.get("uptime_s") or 0))
    regs = [0] * N_IR
    regs[IR_RH] = int(round(float(state.get("humidity") or 0.0) * 10)) & 0xFFFF
    regs[IR_T] = _signed16(round(float(state.get("temp") or 0.0) * 10))
    regs[IR_PUMP] = 1 if state.get("pump") else 0
    regs[IR_FAN] = 1 if state.get("fan") else 0
    regs[IR_LAMP] = 1 if state.get("lamp") else 0
    regs[IR_MODE] = 1 if str(state.get("mode", "AUTO")).upper() == "MANUAL" else 0
    regs[IR_ESTOP] = 1 if state.get("estop") else 0
    regs[IR_FLAGS] = flags
    regs[IR_UPTIME_LO] = uptime & 0xFFFF
    regs[IR_UPTIME_HI] = (uptime >> 16) & 0xFFFF
    regs[IR_DATA_AGE] = min(age, 0xFFFF)
    regs[IR_SEQ] = int(state.get("seq") or 0) & 0xFFFF
    return regs


def encode_holding_registers(state):
    """Cấu hình hiện tại → holding registers (FC03)."""
    state = state or {}
    regs = [0] * N_HR
    regs[HR_RH_ON] = int(round(float(state.get("rh_on") or 0.0) * 10)) & 0xFFFF
    regs[HR_RH_OFF] = int(round(float(state.get("rh_off") or 0.0) * 10)) & 0xFFFF
    regs[HR_FAN_ON] = _signed16(round(float(state.get("fan_on") or 0.0) * 10))
    regs[HR_FAN_OFF] = _signed16(round(float(state.get("fan_off") or 0.0) * 10))
    regs[HR_HEAT_ON] = _signed16(round(float(state.get("heat_on") or 0.0) * 10))
    regs[HR_HEAT_OFF] = _signed16(round(float(state.get("heat_off") or 0.0) * 10))
    regs[HR_PUMP_MAX] = int(state.get("pump_max_s") or 0) & 0xFFFF
    regs[HR_HEAT_MAX] = int(state.get("heat_max_s") or 0) & 0xFFFF
    regs[HR_FAILSAFE] = int(state.get("failsafe_cycles") or 0) & 0xFFFF
    regs[HR_MODE] = 1 if str(state.get("mode", "AUTO")).upper() == "MANUAL" else 0
    return regs


def encode_sensor_registers(state):
    """SHTC30 mirror — đúng thứ tự thanh ghi như RTU (reg0=T, reg1=RH)."""
    state = state or {}
    regs = [0] * N_IR
    regs[0] = _signed16(round(float(state.get("temp") or 0.0) * 10))
    regs[1] = int(round(float(state.get("humidity") or 0.0) * 10)) & 0xFFFF
    return regs


def coil_command(address, on):
    """Coil ghi (FC05) → lệnh điều khiển cho HMI."""
    on = bool(on)
    if address == COIL_PUMP:
        return {"mode": "MANUAL", "pump": on}
    if address == COIL_FAN:
        return {"mode": "MANUAL", "fan": on}
    if address == COIL_LAMP:
        return {"mode": "MANUAL", "lamp": on}
    if address == COIL_ESTOP:
        return {"estop": True, "mode": "MANUAL"} if on else {"estop": False}
    return None


def mode_command(raw):
    """Holding register MODE (FC06) → lệnh đổi chế độ."""
    return {"mode": "MANUAL" if int(raw) else "AUTO"}


def config_command(state, address, raw):
    """Holding register cấu hình (FC06) → lệnh cấu hình cho HMI.

    Giữ nguyên các ngưỡng còn lại, chỉ thay giá trị tại `address`.
    """
    state = state or {}
    limits = HR_LIMITS.get(address)
    if limits is None:
        return None
    lo, hi = limits
    if address in (HR_PUMP_MAX, HR_HEAT_MAX, HR_FAILSAFE):
        value = int(raw)
    else:
        value = _unpack16(raw) / 10.0
    if not (lo <= value <= hi):
        return None
    cfg = {
        "rh_on": float(state.get("rh_on") or 0.0),
        "rh_off": float(state.get("rh_off") or 0.0),
        "fan_on": float(state.get("fan_on") or 0.0),
        "fan_off": float(state.get("fan_off") or 0.0),
        "heat_on": float(state.get("heat_on") or 0.0),
        "heat_off": float(state.get("heat_off") or 0.0),
        "pump_max_s": int(state.get("pump_max_s") or 0),
        "heat_max_s": int(state.get("heat_max_s") or 0),
        "failsafe_cycles": int(state.get("failsafe_cycles") or 0),
    }
    keys = {HR_RH_ON: "rh_on", HR_RH_OFF: "rh_off", HR_FAN_ON: "fan_on",
            HR_FAN_OFF: "fan_off", HR_HEAT_ON: "heat_on", HR_HEAT_OFF: "heat_off",
            HR_PUMP_MAX: "pump_max_s", HR_HEAT_MAX: "heat_max_s",
            HR_FAILSAFE: "failsafe_cycles"}
    cfg[keys[address]] = value
    return cfg


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Bảo mật cho MES: whitelist IP + audit + nhận diện thiết bị
# --------------------------------------------------------------------------
#: IP của client hiện tại (do handler đặt trước khi xử lý request).
PEER_IP: ContextVar = ContextVar("peer_ip", default=None)

#: Danh sách IP bị từ chối (audit cho MES/HMI).
REJECTED_LOG = []


def _ip_allowed(ip, allowed):
    """IP có nằm trong whitelist không (hỗ trợ CIDR)."""
    if not allowed:
        return True                       # không cấu hình → cho tất cả
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for rule in allowed:
        rule = str(rule).strip()
        if not rule:
            continue
        try:
            if "/" in rule:
                if addr in ipaddress.ip_network(rule, strict=False):
                    return True
            elif addr == ipaddress.ip_address(rule):
                return True
        except ValueError:
            continue
    return False


class _WhitelistHandler(ServerRequestHandler):
    """Handler chặn IP không có trong whitelist + ghi nhận IP client."""

    async def handle_request(self):
        peer = None
        transport = getattr(self, "transport", None)
        if transport is not None:
            try:
                peer = transport.get_extra_info("peername")
            except Exception:
                peer = None
        ip = peer[0] if peer else None
        gateway = getattr(self.server, "_gateway", None)
        if gateway is not None and not gateway.is_allowed(ip):
            gateway.note_rejected(ip)
            # Trả lỗi chuẩn Modbus thay vì im lặng: MES nhận được
            # "không phục vụ" ngay thay vì chờ timeout.
            resp = ExceptionResponse(
                self.last_pdu.function_code if self.last_pdu else 0,
                ExcCodes.GATEWAY_NO_RESPONSE,
                device_id=self.last_pdu.dev_id if self.last_pdu else 1,
                transaction=self.last_pdu.transaction_id
                if self.last_pdu else 0)
            if self.last_pdu is not None:
                resp.transaction_id = self.last_pdu.transaction_id
                resp.dev_id = self.last_pdu.dev_id
            self.server_send(resp, self.last_addr)
            return
        PEER_IP.set(ip)
        await super().handle_request()


class _MesTcpServer(ModbusTcpServer):
    """ModbusTcpServer + handler whitelist + device identity cho MES."""

    def callback_new_connection(self):
        return _WhitelistHandler(self, self.trace_packet, self.trace_pdu,
                                 self.trace_connect)

class ModbusTcpGateway:
    """Modbus TCP server chạy trong thread riêng, đọc/ghi qua callback.

    - `update(state)` đẩy trạng thái sống lên server (gọi từ thread HMI).
    - `on_command(dict)` nhận lệnh điều khiển từ PC2.
    - `on_config(dict)` nhận lệnh cấu hình từ PC2.
    """

    def __init__(self, host="0.0.0.0", port=502, device_id=254,
                 on_command=None, on_config=None, allowed_ips=None,
                 on_reject=None):
        self.host = host
        self.port = int(port)
        self.device_id = int(device_id)
        self.on_command = on_command
        self.on_config = on_config
        #: Danh sách IP (hoặc CIDR) được phép kết nối. Rỗng = cho tất cả.
        self.allowed_ips = list(allowed_ips or [])
        self.on_reject = on_reject

        self._lock = threading.Lock()
        self._state = {}
        self._thread = None
        self._loop = None
        self._server = None
        self._error = None
        self._running = False

    # ------------------------------------------------------------------
    def is_allowed(self, ip):
        """IP có được phép kết nối không."""
        return _ip_allowed(ip, self.allowed_ips)

    def note_rejected(self, ip):
        """Ghi nhận IP bị chặn (audit)."""
        item = (ip, time.time())
        REJECTED_LOG.append(item)
        del REJECTED_LOG[:-200]
        if self.on_reject is not None:
            try:
                self.on_reject(ip)
            except Exception:
                pass

    # ------------------------------------------------------------------
    @property
    def running(self):
        return self._running

    @property
    def error(self):
        return self._error

    def update(self, state):
        """Cập nhật trạng thái sống (thread-safe)."""
        with self._lock:
            self._state = dict(state or {})

    def _snapshot(self):
        with self._lock:
            return dict(self._state)

    # ------------------------------------------------------------------
    def start(self):
        """Khởi động server. Trả True nếu lắng nghe thành công."""
        if not PYMODBUS_OK:
            self._error = "thiếu pymodbus (pip install pymodbus)"
            return False
        if self._thread is not None:
            return self._running
        self._error = None
        if _port_busy(self.host, self.port):
            self._error = (f"cổng {self.port} đang bị chiếm bởi tiến trình "
                           f"khác (đã có app HMI/MES khác chạy?). "
                           f"Đổi cổng khác hoặc đóng tiến trình cũ.")
            return False
        ready = threading.Event()

        def runner():
            try:
                asyncio.run(self._serve(ready))
            except Exception as exc:                  # pragma: no cover
                self._error = f"{type(exc).__name__}: {exc}"
                ready.set()

        self._thread = threading.Thread(target=runner, daemon=True,
                                        name="modbus-tcp-server")
        self._thread.start()
        ready.wait(5.0)
        return self._running and self._error is None

    def stop(self):
        """Dừng server và giải phóng cổng."""
        server, loop = self._server, self._loop
        if server is not None and loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(server.shutdown(), loop)
            except Exception:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
        self._thread = None
        self._server = None
        self._loop = None
        self._running = False

    async def _serve(self, ready):
        self._loop = asyncio.get_running_loop()
        # Id nguoi dung chon duoc uu tien; 0 = catch-all, 1 = SHTC30 mirror.
        # Neu chon 0 hoac 1 thi KHONG duoc tao trung id — pymodbus se tu choi
        # khoi dong server.
        devices = []
        taken = set()
        for dev_id, kind in ((self.device_id, "bridge"), (0, "bridge"),
                             (1, "sensor")):
            if dev_id in taken:
                continue
            taken.add(dev_id)
            devices.append(self._make_device(dev_id, kind))
        identity = ModbusDeviceIdentification(info={
            0x00: "AGRI",                     # VendorName
            0x01: "AGRI-ESP-1",              # ProductCode
            0x02: "1.0",                     # MajorMinorRevision
            0x03: "http://192.168.1.50",     # VendorUrl
            0x04: "AGRI HMI SCADA",          # ProductName
            0x05: "SHTC30+RELAY",            # ModelName
            0x06: "AGRI ERP Modbus TCP",     # UserApplicationName
        }) if ModbusDeviceIdentification is not None else None
        self._server = _MesTcpServer(devices, address=(self.host, self.port),
                                     identity=identity)
        self._server._gateway = self
        # serve_forever() tự gọi listen(); gọi trực tiếp để biết chính xác
        # cổng đã bind hay chưa rồi chờ `serving` (shutdown() set_result).
        if not await self._server.listen():
            self._error = (f"không lắng nghe được {self.host}:{self.port} "
                           f"— cổng đang bị chiếm?")
            ready.set()
            return
        self._running = True
        ready.set()
        await self._server.serving

    # ------------------------------------------------------------------
    def _make_device(self, device_id, kind):
        action = partial(self._action, kind)
        simdata = (
            [SimData(address=0, count=N_COIL, values=False,
                     datatype=DataType.BITS)],
            [SimData(address=0, count=N_COIL, values=False,
                     datatype=DataType.BITS)],
            [SimData(address=0, count=N_HR, values=0,
                     datatype=DataType.REGISTERS)],
            [SimData(address=0, count=N_IR, values=0,
                     datatype=DataType.REGISTERS)],
        )
        return SimDevice(id=device_id, simdata=simdata, action=action)

    # ------------------------------------------------------------------
    async def _action(self, kind, func_code, start_address, address, count,
                      current_registers, set_values):
        """Callback của pymodbus: đọc → nạp trạng thái sống, ghi → phát lệnh."""
        state = self._snapshot()

        # ---- Ghi (set_values khác None) ----
        if set_values is not None:
            if kind != "bridge":
                # Slave 1 (SHTC30 mirror) là thiết bị chỉ đọc — không cho
                # đổi cấu hình/điều khiển qua nó.
                return ExcCodes.ILLEGAL_ADDRESS
            if func_code == 5 and len(set_values) >= 1:          # FC05
                cmd = coil_command(address, set_values[0])
                if cmd:
                    self._emit_command(cmd)
                return None
            if func_code == 6 and len(set_values) >= 1:          # FC06
                self._emit_holding_write(address, set_values[0], state)
                return None
            if func_code == 15:                                  # FC15
                for offset, value in enumerate(set_values):
                    cmd = coil_command(address + offset, value)
                    if cmd:
                        self._emit_command(cmd)
                return None
            if func_code == 16:                                  # FC16
                # Phai CONG DON trang thai: config_command() tra ve day du
                # 9 truong, nen dung lai `state` cu cho moi offset thi cac
                # nguong da ghi truoc do se bi ghi de nguoc ve gia tri cu.
                working = dict(state)
                for offset, value in enumerate(set_values):
                    working = self._emit_holding_write(
                        address + offset, value, working)
                return None
            return None

        # ---- Đọc ----
        if func_code in (1, 2):                                  # FC01/FC02
            self._inject_bits(kind, current_registers, start_address,
                              address, count, state)
            return None
        if func_code == 3:                                       # FC03
            regs = (encode_holding_registers(state) if kind == "bridge"
                    else encode_sensor_registers(state))
            self._inject_regs(current_registers, regs)
            return None
        if func_code == 4:                                       # FC04
            regs = (encode_input_registers(state) if kind == "bridge"
                    else encode_sensor_registers(state))
            self._inject_regs(current_registers, regs)
            return None
        return None

    # ------------------------------------------------------------------
    def _inject_bits(self, kind, current_registers, start_address, address,
                     count, state):
        """Nạp trạng thái tải vào thanh ghi kiểu bit (coil / discrete input)."""
        if kind == "sensor":
            return
        packed = 0
        if state.get("pump"):
            packed |= 1 << COIL_PUMP
        if state.get("fan"):
            packed |= 1 << COIL_FAN
        if state.get("lamp"):
            packed |= 1 << COIL_LAMP
        if state.get("estop"):
            packed |= 1 << COIL_ESTOP
        reg_index = (address // 16) - start_address
        if 0 <= reg_index < len(current_registers):
            current_registers[reg_index] = packed

    @staticmethod
    def _inject_regs(current_registers, regs):
        limit = min(len(current_registers), len(regs))
        current_registers[:limit] = regs[:limit]

    # ------------------------------------------------------------------
    def _emit_command(self, cmd):
        """Phat lenh dieu khien toi HMI.

        KHONG nuot loi: neu HMI xu ly loi thi de exception noi len de pymodbus
        tra ve ExceptionResponse (DEVICE_FAILURE) cho MES — MES phai biet lenh
        bi tu choi, khong duoc tuong la thanh cong.
        """
        if self.on_command is None:
            return
        payload = dict(cmd)
        src = PEER_IP.get()
        if src:
            payload["src"] = src
        self.on_command(payload)

    def _emit_holding_write(self, address, raw, state):
        """Ghi 1 thanh ghi cau hinh; tra ve `state` da cap nhat.

        Tra ve state moi de FC16 (ghi nhieu thanh ghi trong 1 request) cong
        don duoc, thay vi ghi de nguoc cac nguong vua ghi.
        """
        if address == HR_MODE:
            self._emit_command(mode_command(raw))
            return state
        cfg = config_command(state, address, raw)
        if cfg is None:
            return state
        if self.on_config is None:
            return state
        payload = dict(cfg)
        src = PEER_IP.get()
        if src:
            payload["src"] = src
        self.on_config(payload)
        updated = dict(state)
        updated.update(cfg)
        return updated


if __name__ == "__main__":                                       # pragma: no cover
    import time

    gw = ModbusTcpGateway(host="127.0.0.1", port=502,
                          on_command=lambda c: print("[CMD]", c),
                          on_config=lambda c: print("[CFG]", c))
    if not gw.start():
        raise SystemExit(f"không khởi động được: {gw.error}")
    print(f"Modbus TCP đang lắng nghe {gw.host}:{gw.port} (device "
          f"{gw.device_id}) — Ctrl+C để dừng")
    state = {"humidity": 42.5, "temp": 28.3, "pump": True, "fan": False,
             "lamp": False, "mode": "AUTO", "estop": False,
             "sensor_ok": True, "connected": True, "cfg_valid": True,
             "uptime_s": 0, "rh_on": 40.0, "rh_off": 45.0,
             "fan_on": 32.0, "fan_off": 30.0, "heat_on": 22.0,
             "heat_off": 25.0, "pump_max_s": 600, "heat_max_s": 1800,
             "failsafe_cycles": 1}
    try:
        while True:
            state["uptime_s"] = int(time.time())
            gw.update(state)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        gw.stop()
