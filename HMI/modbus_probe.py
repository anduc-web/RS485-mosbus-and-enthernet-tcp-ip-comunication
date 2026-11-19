# -*- coding: utf-8 -*-
"""Dò bus Modbus RTU (USB–RS485) — dùng cho nút 🔧 CHẨN ĐOÁN BUS của HMI.

Quét baudrate + slave id để tìm:
  · cảm biến SHTC30 (FC04/FC03 input/holding registers)
  · board relay (FC01 coils / FC05 write coil / FC06 write register)

Chạy tay:

    python modbus_probe.py --port COM3 --all
    python modbus_probe.py --port COM3 --baud 9600 --sensor-slave 1
    python modbus_probe.py --port COM3 --baud 9600 --relay-slave 255

Ghi chú: chỉ MỘT tiến trình được giữ cổng COM. Đóng HMI/Arduino IDE/
Hercules/Tera Term trước khi dò (HMI tự nhả cổng khi bấm CHẨN ĐOÁN).

Đầu ra dùng các dấu để app HMI tô màu log:
    ✔ / ★ / "có phản hồi"  → OK
    ✘ / "không mở được"    → lỗi
    ! / "lưu ý"            → cảnh báo
"""

import argparse
import sys
import time

try:
    import serial
    from serial.tools import list_ports
    SERIAL_OK = True
except Exception:                                     # pragma: no cover
    SERIAL_OK = False

try:
    from pymodbus.client import ModbusSerialClient
    PYMODBUS_OK = True
except Exception:                                     # pragma: no cover
    PYMODBUS_OK = False

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BAUDS = (9600, 19200, 38400, 57600, 115200)


def _short(exc, limit=120):
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= limit else text[:limit] + "…"


def list_serial_ports():
    if not SERIAL_OK:
        return []
    out = []
    for p in list_ports.comports():
        desc = (p.description or "").strip()
        out.append((p.device, desc))
    return out


def make_client(port, baud, timeout):
    kw = dict(port=port, baudrate=baud, bytesize=8, parity="N",
              stopbits=1, timeout=timeout)
    try:
        cli = ModbusSerialClient(retries=0, **kw)
    except TypeError:
        cli = ModbusSerialClient(method="rtu", retries=0, **kw)
    return cli


def _call(wrapper, *args, **kwargs):
    """Gọi hàm pymodbus với tên tham số device_id/slave/unit tuỳ bản."""
    for key in ("device_id", "slave", "unit"):
        try:
            return wrapper(*args, **{key: kwargs["slave"]})
        except TypeError:
            continue
    raise RuntimeError("chữ ký pymodbus không hỗ trợ")


def probe_sensor(client, slave):
    """Thử đọc 2 thanh ghi cảm biến bằng FC04 rồi FC03."""
    for fc, fn in (("FC04", client.read_input_registers),
                   ("FC03", client.read_holding_registers)):
        try:
            rr = _call(fn, 0x0000, 2, slave=slave)
        except Exception as exc:
            print(f"    ! {fc} slave {slave}: {_short(exc)}")
            continue
        if rr is None or rr.isError():
            print(f"    ✘ {fc} slave {slave}: không phản hồi ({rr})")
            continue
        raw = list(rr.registers)
        t = (raw[0] - 65536) * 0.1 if raw[0] > 32767 else raw[0] * 0.1
        h = raw[1] * 0.1 if len(raw) > 1 else float("nan")
        print(f"    ✔ {fc} slave {slave}: reg0={raw[0]} reg1={raw[1]} "
              f"→ T≈{t:.1f}°C RH≈{h:.1f}%RH")
        if 0 <= h <= 100 and -40 <= t <= 125:
            print(f"    ★ CÓ PHẢN HỒI cảm biến ở slave {slave} ({fc})")
            return slave, fc
        print("    ! Giá trị ngoài dải SHTC30 (đúng thanh ghi?)")
    return None


def probe_relay(client, slave, channels=4):
    """Thử board relay: đọc coil, ghi coil, ghi register (không đổi tải)."""
    found = []

    # Đọc coil: quirk phần cứng chỉ nhận count = 1 hoặc 8
    for count in (8, 1):
        try:
            rr = _call(client.read_coils, 0, count, slave=slave)
        except Exception as exc:
            print(f"    ! FC01 count={count} slave {slave}: {_short(exc)}")
            continue
        if rr is not None and not rr.isError():
            bits = list(rr.bits or [])
            print(f"    ✔ FC01 count={count} slave {slave}: {bits[:channels]}")
            found.append(f"FC01(count={count})")
            break
        print(f"    ✘ FC01 count={count} slave {slave}: {rr}")

    # Ghi coil giữ giá trị hiện tại (an toàn: không đổi trạng thái tải)
    try:
        cur = False
        rr = _call(client.read_coils, 0, 8, slave=slave)
        if rr is not None and not rr.isError() and rr.bits:
            cur = bool(rr.bits[0])
        wr = _call(client.write_coil, 0, cur, slave=slave)
        if wr is not None and not wr.isError():
            print(f"    ✔ FC05 slave {slave}: ghi coil 0 = {cur} (giữ nguyên)")
            found.append("FC05")
        else:
            print(f"    ✘ FC05 slave {slave}: {wr}")
    except Exception as exc:
        print(f"    ! FC05 slave {slave}: {_short(exc)}")

    # Holding register (một số board dùng FC06 thay FC05)
    try:
        rr = _call(client.read_holding_registers, 0, channels, slave=slave)
        if rr is not None and not rr.isError():
            print(f"    ✔ FC03 slave {slave}: {list(rr.registers)}")
            found.append("FC03")
        else:
            print(f"    ✘ FC03 slave {slave}: {rr}")
    except Exception as exc:
        print(f"    ! FC03 slave {slave}: {_short(exc)}")

    if found:
        print(f"    ★ CÓ PHẢN HỒI relay ở slave {slave} ({', '.join(found)})")
    return found


def main():
    ap = argparse.ArgumentParser(description="Dò bus Modbus RTU")
    ap.add_argument("--port", required=True, help="cổng COM, vd COM3")
    ap.add_argument("--baud", type=int, default=0,
                    help="baudrate; bỏ trống + --all để quét")
    ap.add_argument("--all", action="store_true",
                    help="quét nhiều baudrate và nhiều slave id")
    ap.add_argument("--sensor-slave", type=int, default=1)
    ap.add_argument("--relay-slave", type=int, default=255)
    ap.add_argument("--timeout", type=float, default=0.4)
    args = ap.parse_args()

    if not SERIAL_OK:
        print("✘ Thiếu pyserial → pip install pyserial")
        return 2
    if not PYMODBUS_OK:
        print("✘ Thiếu pymodbus → pip install pymodbus")
        return 2

    ports = list_serial_ports()
    print(f"🔧 DÒ BUS MODBUS RTU · cổng {args.port} · timeout {args.timeout}s")
    if ports:
        print("   Cổng hiện có: " + ", ".join(d for d, _ in ports))
    else:
        print("   ! Không thấy cổng COM nào (cắm lại cáp / cài driver CH340…)")

    bauds = [args.baud] if args.baud else (BAUDS if args.all else [9600])
    slaves = range(1, 9) if args.all else [args.sensor_slave]

    best = None
    for baud in bauds:
        print(f"\n— Baud {baud} —")
        client = make_client(args.port, baud, args.timeout)
        try:
            if not client.connect():
                print(f"  ✘ Không mở được {args.port} @ {baud} "
                      "(cổng đang bị app khác giữ?)")
                return 3
        except Exception as exc:
            print(f"  ✘ Không mở được {args.port}: {_short(exc)}")
            return 3

        try:
            for slave in slaves:
                got = probe_sensor(client, slave)
                if got:
                    best = (baud, got[0], slave)
                    break
        finally:
            try:
                client.close()
            except Exception:
                pass

        if best:
            break

    # Relay: chỉ thử ở baud đã tìm được (hoặc 9600 nếu chưa thấy cảm biến)
    relay_baud = best[0] if best else (args.baud or 9600)
    print(f"\n— Relay @ baud {relay_baud}, slave {args.relay_slave} —")
    client = make_client(args.port, relay_baud, args.timeout)
    try:
        if client.connect():
            probe_relay(client, args.relay_slave)
    except Exception as exc:
        print(f"  ! Lỗi dò relay: {_short(exc)}")
    finally:
        try:
            client.close()
        except Exception:
            pass

    print("\n— KẾT LUẬN —")
    if best:
        baud, fc, slave = best
        print(f"  ★ Cảm biến: baud {baud} · slave {slave} · {fc}")
        print(f"    → đặt cổng {args.port}, baud {baud}, sensor slave {slave} "
              "rồi bấm CONNECT.")
        return 0
    print("  ✘ Không thấy cảm biến nào phản hồi.")
    print("    Kiểm tra: (1) đảo dây A/B, (2) nối GND chung, (3) nguồn module,")
    print("    (4) đúng baud/slave, (5) chỉ 1 app giữ cổng COM.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
