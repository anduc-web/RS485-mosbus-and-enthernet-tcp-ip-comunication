# -*- coding: utf-8 -*-
"""Chẩn đoán kết nối MES ↔ PC1 (Modbus TCP).

Tách riêng khỏi GUI để pytest test được (không cần PyQt), và để dùng
chung cho cả app MES lẫn chế độ CLI `--diagnose`.

Cung cấp:
  • ``tcp_probe``      — thử mở TCP, phân biệt "từ chối" / "timeout" / "sai DNS"
  • ``modbus_probe``   — đọc thử FC04/FC03/FC01, giải mã mã lỗi Modbus
  • ``diagnose``       — chuỗi bước chẩn đoán, kèm lời khuyên hành động
  • ``ModbusFault``    — ngoại lệ mang mã lỗi Modbus (để worker bắt được)
"""

import socket

#: Thời gian chờ mặc định (giây).
DEFAULT_TIMEOUT = 3.0

#: Mã lỗi Modbus → (nguyên nhân, cách khắc phục).
EXC_HELP = {
    1: ("PC1 báo lỗi nội bộ khi xử lý lệnh",
        "xem nhật ký của app HMI"),
    2: ("Sai địa chỉ thanh ghi hoặc sai device id",
        "đảm bảo device id trùng ô 'dev' ở HMI (mặc định 254)"),
    3: ("Số lượng thanh ghi yêu cầu không hợp lệ", "báo lại cho kỹ thuật"),
    4: ("PC1 từ chối lệnh (vi phạm ràng buộc hoặc E-STOP đang khóa)",
        "xem nhật ký HMI để biết lý do cụ thể"),
    5: ("PC1 chưa xử lý xong lệnh", "thử lại sau vài giây"),
    6: ("PC1 đang bận", "thử lại sau vài giây"),
    10: ("Đường đi tới PC1 không khả dụng",
         "kiểm tra lại device id và địa chỉ IP"),
    11: ("PC1 TỪ CHỐI IP của PC2",
         "mở app HMI → khung 🌐 → ô 'IP cho phép' → thêm IP của PC2 rồi "
         "bật lại cổng TCP"),
}

#: Lỗi mạng → lời khuyên.
TCP_ADVICE = {
    "refused": (
        "Không có tiến trình nào lắng nghe ở cổng này. Trên PC1: mở app HMI → "
        "khung 🌐 → bật 'BẬT CỔNG TCP' (hoặc tick 'Tự bật'), và kiểm tra cổng "
        "có đúng không (mặc định 502)."),
    "timeout": (
        "Không có phản hồi. Kiểm tra theo thứ tự:\n"
        "  1) Trên PC1: app HMI → khung 🌐 → nút 'BẬT CỔNG TCP' đang bật chưa?\n"
        "  2) IP đúng chưa? (ô 'Nhập ở PC2' trong khung 🌐 của PC1)\n"
        "  3) Firewall của PC1 — chạy PowerShell (quyền Administrator):\n"
        "     New-NetFirewallRule -DisplayName \"Modbus TCP 502\" "
        "-Direction Inbound -Protocol TCP -LocalPort 502 -Action Allow\n"
        "Lưu ý: Windows thường DROP gói (báo timeout) chứ không báo 'từ chối', "
        "nên timeout KHÔNG chắc chắn là sai IP."),
    "dns": (
        "Không phân giải được tên máy. Hãy nhập IP dạng số, ví dụ "
        "192.168.1.50:502."),
    "unreachable": (
        "Không có đường tới PC1 (khác subnet / chưa cắm mạng). Kiểm tra "
        "Wi-Fi, Ethernet, và IP của cả 2 máy."),
    "connect": "Không kết nối được. Thử chẩn đoán TCP trước.",
    "pymodbus": "Thiếu thư viện pymodbus — cài bằng: pip install pymodbus",
    "error": "Lỗi không xác định.",
}


class ModbusFault(Exception):
    """PC1 trả về mã lỗi Modbus (có mã cụ thể để giải thích được)."""

    def __init__(self, func_code, code):
        self.func_code = int(func_code)
        self.code = int(code)
        super().__init__(f"FC{self.func_code:02X} lỗi {self.code}")

    @property
    def reason(self):
        """Câu giải thích + cách khắc phục (tiếng Việt)."""
        why, fix = explain_exception(self.code)
        return f"{why} — {fix}"


def explain_exception(code):
    """Mã lỗi Modbus → (nguyên nhân, cách khắc phục)."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = 0
    return EXC_HELP.get(code, (f"PC1 trả về mã lỗi Modbus {code}",
                                "xem nhật ký của app HMI"))


def tcp_probe(host, port, timeout=DEFAULT_TIMEOUT):
    """Thử mở TCP tới PC1.

    Trả về ``(ok, code, message)``:
      ok  — code ``'ok'``
      lỗi — ``'refused'`` | ``'timeout'`` | ``'dns'`` | ``'unreachable'``
            | ``'error'`` | ``'invalid'``
    """
    if not host or not str(host).strip():
        return False, "invalid", "Chưa nhập địa chỉ PC1."
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False, "invalid", f"Cổng không hợp lệ: {port!r}"
    try:
        with socket.create_connection((str(host).strip(), port),
                                      timeout=timeout):
            return True, "ok", f"TCP mở được tới {host}:{port}."
    except socket.gaierror as exc:
        return False, "dns", (f"Không phân giải được tên máy '{host}': "
                              f"{exc.strerror or exc}")
    except (socket.timeout, TimeoutError):
        return False, "timeout", (f"Không có phản hồi từ {host}:{port} "
                                  f"trong {timeout:g}s (cổng chưa mở, "
                                  f"firewall chặn, hoặc sai IP).")
    except ConnectionRefusedError:
        return False, "refused", (f"{host}:{port} từ chối kết nối "
                                  f"(không có tiến trình nào lắng nghe).")
    except OSError as exc:
        code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
        if code in (10060, 110):
            return False, "timeout", f"Không có phản hồi từ {host}:{port}."
        if code in (10065, 10051, 113, 101):
            return False, "unreachable", (f"Không có đường tới {host}:{port} "
                                          f"({exc.strerror or exc}).")
        return False, "error", f"{type(exc).__name__}: {exc.strerror or exc}"
    except Exception as exc:                      # pragma: no cover
        return False, "error", f"{type(exc).__name__}: {exc}"


def modbus_probe(host, port, device_id=254, timeout=DEFAULT_TIMEOUT):
    """Đọc thử FC04/FC03/FC01 tới PC1.

    Trả về ``(ok, code, message, data)`` — ``code`` là ``'ok'`` khi thành
    công, hoặc ``'exception:<mã>'`` khi PC1 từ chối, ``'tcp:<mã>'`` khi
    không mở được TCP.
    """
    try:
        from pymodbus.client import ModbusTcpClient
    except Exception as exc:
        return False, "pymodbus", f"Thiếu pymodbus: {exc}", {}

    ok, code, msg = tcp_probe(host, port, timeout)
    if not ok:
        return False, f"tcp:{code}", msg, {}

    cli = ModbusTcpClient(str(host).strip(), port=int(port), timeout=timeout)
    try:
        if not cli.connect():
            return False, "tcp:refused", msg, {}
        checks = [
            ("FC04", lambda: cli.read_input_registers(0, count=12,
                                                       device_id=device_id)),
            ("FC03", lambda: cli.read_holding_registers(0, count=10,
                                                        device_id=device_id)),
            ("FC01", lambda: cli.read_coils(0, count=4, device_id=device_id)),
        ]
        data = {}
        for name, fn in checks:
            res = fn()
            if res.isError():
                exc_code = getattr(res, "exception_code", 0)
                why, fix = explain_exception(exc_code)
                return (False, f"exception:{exc_code}",
                        f"{name} bị từ chối (mã {exc_code}): {why} — {fix}", {})
            if name == "FC04":
                data["input"] = list(res.registers)
            elif name == "FC03":
                data["holding"] = list(res.registers)
            else:
                data["coils"] = list(res.bits)
        return True, "ok", f"Đọc FC04/FC03/FC01 thành công (device {device_id}).", data
    except Exception as exc:
        return False, "error", f"{type(exc).__name__}: {exc}", {}
    finally:
        try:
            cli.close()
        except Exception:
            pass


def diagnose(host, port, device_id=254, timeout=DEFAULT_TIMEOUT):
    """Chẩn đoán đầy đủ — trả về list ``(level, text)``.

    level: ``'OK'`` | ``'WARN'`` | ``'ERROR'``
    """
    steps = []
    ok, code, msg = tcp_probe(host, port, timeout)
    steps.append(("OK" if ok else "ERROR", f"[TCP] {msg}"))
    if not ok:
        advice = TCP_ADVICE.get(code, TCP_ADVICE["error"])
        steps.append(("WARN", f"[Cách khắc phục] {advice}"))
        return steps

    ok, code, msg, data = modbus_probe(host, port, device_id, timeout)
    steps.append(("OK" if ok else "ERROR", f"[Modbus] {msg}"))
    if not ok:
        if code.startswith("tcp:"):
            advice = TCP_ADVICE.get(code[4:], TCP_ADVICE["error"])
            steps.append(("WARN", f"[Cách khắc phục] {advice}"))
        return steps

    ir = data.get("input") or []
    hr = data.get("holding") or []
    flags = ir[7] if len(ir) > 7 else 0
    age = ir[10] if len(ir) > 10 else 0
    sensor_ok = bool(flags & 0x01)
    steps.append((
        "OK",
        f"[Dữ liệu] RH={ir[0] / 10 if len(ir) > 0 else 0:.1f}% "
        f"T={ir[1] / 10 if len(ir) > 1 else 0:.1f}°C "
        f"seq={ir[11] if len(ir) > 11 else 0} · "
        f"ngưỡng RH bật/tắt = {hr[0] / 10 if len(hr) > 0 else 0:.1f}/"
        f"{hr[1] / 10 if len(hr) > 1 else 0:.1f} %RH"))
    if not sensor_ok:
        steps.append((
            "WARN",
            "[Cảm biến] PC1 KHÔNG đọc được cảm biến SHTC30 "
            "(cờ sensor_ok = 0). Link TCP vẫn tốt — kiểm tra COM/serial "
            "trên PC1."))
    if age >= 65535:
        steps.append((
            "WARN",
            "[Dữ liệu] PC1 chưa từng nhận được mẫu cảm biến nào "
            "(data_age = 65535 s)."))
    return steps
