# -*- coding: utf-8 -*-
"""Client Modbus TCP cho PC2 — đọc telemetry + điều khiển HMI (phương án A).

Chạy trên PC2 (cùng LAN với PC1):
    python modbus_tcp_client.py --host 192.168.1.50
    python modbus_tcp_client.py --host 192.168.1.50 --watch
    python modbus_tcp_client.py --host 192.168.1.50 --pump on
    python modbus_tcp_client.py --host 192.168.1.50 --fan off --lamp on
    python modbus_tcp_client.py --host 192.168.1.50 --mode AUTO
    python modbus_tcp_client.py --host 192.168.1.50 --estop
    python modbus_tcp_client.py --host 192.168.1.50 --rh-on 42

Bảng thanh ghi xem README.md (HMI/README.md mục Modbus TCP).
"""

import argparse
import os
import sys
import time

try:
    from pymodbus.client import ModbusTcpClient
except Exception:                                     # pragma: no cover
    print("Thiếu pymodbus → pip install pymodbus")
    raise SystemExit(1)

# Windows: console cp1252 không in được tiếng Việt có dấu.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

IR_RH, IR_T, IR_PUMP, IR_FAN, IR_LAMP, IR_MODE, IR_ESTOP, IR_FLAGS = range(8)
IR_UPTIME_LO, IR_UPTIME_HI = 8, 9
IR_DATA_AGE, IR_SEQ = 10, 11
HR_RH_ON, HR_RH_OFF = 0, 1
HR_MODE = 9
COIL_PUMP, COIL_FAN, COIL_LAMP, COIL_ESTOP = 0, 1, 2, 3


def signed16(raw):
    raw = int(raw) & 0xFFFF
    return raw - 0x10000 if raw >= 0x8000 else raw


def read_snapshot(client, device_id):
    """Đọc 1 lần trạng thái + cấu hình từ PC1."""
    ir = client.read_input_registers(0, count=12, device_id=device_id)
    if ir.isError():
        raise RuntimeError(f"FC04 lỗi: {ir}")
    hr = client.read_holding_registers(0, count=10, device_id=device_id)
    if hr.isError():
        raise RuntimeError(f"FC03 lỗi: {hr}")
    coils = client.read_coils(0, count=4, device_id=device_id)
    if coils.isError():
        raise RuntimeError(f"FC01 lỗi: {coils}")

    r = ir.registers
    h = hr.registers
    uptime = (r[IR_UPTIME_HI] << 16) | r[IR_UPTIME_LO]
    flags = r[IR_FLAGS]
    return {
        "rh": r[IR_RH] / 10.0,
        "temp": signed16(r[IR_T]) / 10.0,
        "pump": bool(r[IR_PUMP]),
        "fan": bool(r[IR_FAN]),
        "lamp": bool(r[IR_LAMP]),
        "mode": "MANUAL" if r[IR_MODE] else "AUTO",
        "estop": bool(r[IR_ESTOP]),
        "sensor_ok": bool(flags & 0x01),
        "connected": bool(flags & 0x02),
        "uptime_s": uptime,
        "rh_on": h[HR_RH_ON] / 10.0,
        "rh_off": h[HR_RH_OFF] / 10.0,
        "data_age_s": r[IR_DATA_AGE],
        "seq": r[IR_SEQ],
        "stale": bool(flags & 0x08),
        "coil_pump": bool(coils.bits[COIL_PUMP]),
        "coil_fan": bool(coils.bits[COIL_FAN]),
        "coil_lamp": bool(coils.bits[COIL_LAMP]),
        "coil_estop": bool(coils.bits[COIL_ESTOP]),
    }


def show(snap):
    print(f"  RH {snap['rh']:.1f} %RH   T {snap['temp']:.1f} °C   "
          f"mode {snap['mode']}"
          + ("   ⛔ E-STOP" if snap["estop"] else ""))
    print(f"  tải: bơm={'ON' if snap['pump'] else 'OFF'} "
          f"quạt={'ON' if snap['fan'] else 'OFF'} "
          f"đèn={'ON' if snap['lamp'] else 'OFF'}")
    print(f"  coil thực tế: bơm={snap['coil_pump']} quạt={snap['coil_fan']} "
          f"đèn={snap['coil_lamp']} estop={snap['coil_estop']}")
    print(f"  ngưỡng RH: bật < {snap['rh_on']:.1f} · tắt ≥ {snap['rh_off']:.1f}   "
          f"chạy {snap['uptime_s']} s   "
          f"{'đang nối' if snap['connected'] else 'HMI mất kết nối RS485'}")
    quality = "CŨ — MES không nên ghi" if snap["stale"] else "tươi"
    print(f"  chất lượng dữ liệu: {quality} · age {snap['data_age_s']} s · "
          f"seq {snap['seq']}")


def parse_bool(text):
    return str(text).strip().lower() in ("1", "on", "true", "yes", "bat", "bật")


def main():
    ap = argparse.ArgumentParser(description="Client Modbus TCP cho PC2")
    ap.add_argument("--host", default="127.0.0.1", help="IP của PC1")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--device", type=int, default=254, help="device id trên PC1")
    ap.add_argument("--watch", action="store_true", help="theo dõi liên tục")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--pump", choices=["on", "off"])
    ap.add_argument("--fan", choices=["on", "off"])
    ap.add_argument("--lamp", choices=["on", "off"])
    ap.add_argument("--mode", choices=["AUTO", "MANUAL"])
    ap.add_argument("--estop", action="store_true", help="kích hoạt E-STOP")
    ap.add_argument("--release", action="store_true", help="giải phóng E-STOP")
    ap.add_argument("--rh-on", type=float, help="đặt ngưỡng RH bật (%RH)")
    ap.add_argument("--rh-off", type=float, help="đặt ngưỡng RH tắt (%RH)")
    ap.add_argument("--export", action="store_true",
                    help="xuất contract tag cho MES (CSV + JSON) rồi thoát")
    args = ap.parse_args()

    if args.export:
        from modbus_tcp_gateway import (build_tag_contract, contract_to_csv,
                                        contract_to_json)
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "mes_tag_contract")
        contract = build_tag_contract(args.device)
        with open(base + ".csv", "w", encoding="utf-8-sig", newline="") as f:
            f.write(contract_to_csv(contract))
        with open(base + ".json", "w", encoding="utf-8") as f:
            f.write(contract_to_json(contract))
        print(f"Đã xuất {len(contract)} tag: {base}.csv / {base}.json")
        return 0

    client = ModbusTcpClient(args.host, port=args.port, timeout=3)
    if not client.connect():
        print(f"Không kết nối được {args.host}:{args.port} — kiểm tra firewall "
              f"và cổng trên PC1.")
        return 1
    print(f"Đã kết nối {args.host}:{args.port} (device {args.device})\n")

    try:
        # ---- Ghi lệnh ----
        if args.pump:
            client.write_coil(COIL_PUMP, parse_bool(args.pump),
                              device_id=args.device)
            print("→ lệnh: BƠM " + args.pump.upper())
        if args.fan:
            client.write_coil(COIL_FAN, parse_bool(args.fan),
                              device_id=args.device)
            print("→ lệnh: QUẠT " + args.fan.upper())
        if args.lamp:
            client.write_coil(COIL_LAMP, parse_bool(args.lamp),
                              device_id=args.device)
            print("→ lệnh: ĐÈN " + args.lamp.upper())
        if args.estop:
            client.write_coil(COIL_ESTOP, True, device_id=args.device)
            print("→ lệnh: E-STOP")
        if args.release:
            client.write_coil(COIL_ESTOP, False, device_id=args.device)
            print("→ lệnh: giải phóng E-STOP")
        if args.mode:
            client.write_register(HR_MODE, 1 if args.mode == "MANUAL" else 0,
                                  device_id=args.device)
            print(f"→ lệnh: MODE {args.mode}")
        if args.rh_on is not None:
            client.write_register(HR_RH_ON, int(round(args.rh_on * 10)),
                                  device_id=args.device)
            print(f"→ lệnh: RH_ON = {args.rh_on} %RH")
        if args.rh_off is not None:
            client.write_register(HR_RH_OFF, int(round(args.rh_off * 10)),
                                  device_id=args.device)
            print(f"→ lệnh: RH_OFF = {args.rh_off} %RH")

        if args.pump or args.fan or args.lamp or args.estop or args.release \
                or args.mode or args.rh_on is not None or args.rh_off is not None:
            time.sleep(0.5)

        # ---- Đọc ----
        if args.watch:
            print("Ctrl+C để dừng.\n")
            try:
                while True:
                    show(read_snapshot(client, args.device))
                    print()
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\nDừng.")
        else:
            show(read_snapshot(client, args.device))
    except RuntimeError as exc:
        print(f"Lỗi: {exc}")
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
