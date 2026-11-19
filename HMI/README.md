# HMI SCADA – SHTC30 · Nhiệt độ & độ ẩm không khí + điều khiển máy bơm

Ứng dụng **PyQt5** chạy trên PC (Modbus Master), giao tiếp với bus **Modbus RTU
(USB–RS485)** và đồng bộ lên **Firebase Realtime Database** cho web ERP.

```
PC (Master) ──USB–RS485──┬── Slave 1: SHTC30-RS485  (0x01, 9600 8N1)
                          └── Slave 2: Relay 1/4 kênh (mặc định 255, tuỳ board)
```

## 1. Cài đặt & chạy

```powershell
pip install pyserial pymodbus pyqtgraph
python hmi_shtc30_3kenh.py
```

- `pyqtgraph` không bắt buộc: thiếu thì app vẫn chạy, chỉ ẩn đồ thị.
- Khi mở app: chọn cổng COM → **CONNECT**.
- Không có phần cứng: tick **🧪 SIMULATOR MODE** rồi bấm CONNECT.

## 2. Địa chỉ Modbus

| Thiết bị | Slave | Hàm | Thanh ghi |
|---|---|---|---|
| SHTC30-RS485 | `0x01` (1) | `0x04` Read Input Registers | `0x0000` = nhiệt độ ×0,1 °C · `0x0001` = độ ẩm ×0,1 %RH |
| Relay 1 kênh | `0x02` (2) | `0x06` Write Single Register | `0x0000`: `1` = bật bơm, `0` = tắt |
| Relay 4 kênh (thực tế) | `0xFF` (255) | `0x05` Write Coil | coil `0` = kênh 1 … coil `3` = kênh 4 |
| Relay 4 kênh — đọc lại | `0xFF` (255) | `0x01` Read Coils | byte bit: bit0 = kênh 1 đang đóng |

> **Đã kiểm tra trên phần cứng thật (COM3 · 9600 8N1):** board Bestep 4 kênh trả
> lời `FC05` khi ghi và `FC01` khi đọc (`FF 01 01 00 60 60` = cả 4 relay OFF).
> Board **không** trả lời `FC03/FC04` — nên đừng dùng FC03 để dò nó.
> Lưu ý quirk: `FC01` chỉ chấp nhận `count = 1` hoặc `8`, count 2/4 thì im lặng.
> App đọc bằng `count = 1` nên chạy đúng, không cần chế độ "chỉ ghi".

## 3. Logic AUTO (theo báo cáo)

```
Bơm (theo độ ẩm):  BẬT khi RH < RH_ON  (40 %RH)
                       TẮT khi RH ≥ RH_OFF (45 %RH)
Quạt (theo nhiệt độ): BẬT khi T > FAN_ON  (32 °C)
                       TẮT khi T ≤ FAN_OFF (30 °C)
Đèn halogen ủ ấm:  BẬT khi T < HEAT_ON  (22 °C)
                       TẮT khi T ≥ HEAT_OFF (25 °C)
```

- Hysteresis **5 %RH** và **2 °C** → chống relay bật/tắt liên tục.
- **Watchdog 600 s**: bơm chạy quá 10 phút thì tự ngắt.
- Nếu cảm biến mất dữ liệu → **khóa AUTO** (không tự bật bơm).
- 4 ngưỡng sửa được trực tiếp trên UI; nếu nhập sai (RH bật ≥ RH tắt hoặc
  T bật ≤ T tắt) app hiện cảnh báo và tạm khóa điều khiển AUTO.
- Lệnh bơm được gửi **trước** khi đọc cảm biến, nên cảm biến lỗi không chặn
  việc bật/tắt bơm.

## 4. Giao diện

- Sơ đồ phần cứng mô phỏng: USB–RS485 → SHTC30 + relay → máy bơm, có LED và
  giá trị đo trực tiếp.
- 2 đồng hồ tròn: **độ ẩm không khí** (%RH) và **nhiệt độ** (°C), kèm vạch ngưỡng.
- Đồ thị 2 trục: độ ẩm (trái) + nhiệt độ (phải), 600 điểm gần nhất.
- Bảng dữ liệu 4 cột + nhật ký sự kiện màu theo mức INFO/OK/WARN/ERROR.
- Nút **BẬT/TẮT BƠM** thủ công (chỉ ở chế độ MANUAL).
- **CHẨN ĐOÁN BUS RS485**: tự quét slave/baud, nhường cổng COM cho tiến trình dò.

## 5. Ghi log

File `log_khi_hau_phong.csv` (cùng thư mục chạy app):

```
Timestamp, Humidity(%RH), Temp(C), PumpState, Mode, Note
2026-10-24 16:38:50, 47.80, 27.90, OFF, AUTO, RH_ON=40.0 RH_OFF=45.0 T_ON=35.0 T_OFF=33.0
```

Nút **💾 Mở / Export CSV** mở file bằng Excel.

## 6. Đồng bộ Firebase (web ERP)
Khung **☁ ĐỒNG BỘ FIREBASE (ERP)** ở cột trái:

1. `Realtime DB URL` đã điền sẵn project `ktma-f2871`.
2. `Mã thiết bị`: `vuon-1` (phải trùng với web ERP).
3. Bấm **☁ BẬT ĐỒNG BỘ**. Tick **Tự bật** để lần mở sau tự kết nối.

Dữ liệu đẩy 2 giây/lần, độc lập với Modbus:

```json
hmi/vuon-1/live = {
  "humidity": 47.8, "temp": 27.9, "pump": false, "fan": true, "lamp": false,
  "mode": "AUTO", "estop": false, "sensor_ok": true, "modbus_connected": true,
  "rh_on": 40, "rh_off": 45, "fan_on": 32, "fan_off": 30,
  "heat_on": 22, "heat_off": 25, "pump_max_s": 600, "heat_max_s": 1800,
  "failsafe_cycles": 1, "uptime_s": 1830, "ts": 1792834508112
}
```

Ngoài `live`, mỗi chu kỳ HMI ghi thêm:

| Nhánh | Nội dung |
|---|---|
| `status` | heartbeat: online, version `hmi-2.0`, mode, estop, uptime, `latency_ms` |
| `command_ack` | `{ seq, ok, message, applied }` sau khi thực thi lệnh web |
| `config_ack` | `{ seq, ok, message, applied }` sau khi áp dụng ngưỡng |
| `logs/<id>` | log INFO/OK/WARN/ERROR/REMOTE, đẩy gần realtime |
| `modbus_trace/<id>` | khung Modbus của từng lần đo: `dir`, `slave`, `fc`, `fc_name`, `rtu`, `ascii`, `crc16`, `lrc`, `ok`, `note` (giữ 300 khung) |
| `history/<id>` | mẫu telemetry 20 s, tự dọn còn 900 mẫu |

Web ERP ghi `hmi/<device>/control` và `hmi/<device>/config`; HMI đọc mỗi ~2 giây,
chống replay bằng `seq` + mốc thời gian đã lưu trong `hmi_config.json`, tự chuyển
MANUAL khi nhận lệnh thiết bị và trả ACK đúng `seq` để web đối chiếu. Cấu hình
ngưỡng/watchdog cũng đi qua `config` và được kiểm tra ràng buộc trước khi áp dụng.
Chi tiết + rules Firebase: xem `ERP_HMI/README.md`.

Thử nhanh không cần mở app:

```powershell
python firebase_sync.py https://ktma-f2871-default-rtdb.asia-southeast1.firebasedatabase.app
```

## 7. Modbus TCP — PC2 đọc/ghi (phương án A)

PC1 (máy này) mở thêm cổng **Modbus TCP** để PC2 — SCADA/ERP server — đọc
telemetry và điều khiển tải bằng client Modbus TCP tiêu chuẩn.

Trong app: khung **🌐 MODBUS TCP · PC2 ĐỌC/GHI** ở cột trái → chọn cổng
(mặc định 502) và Device ID (mặc định 254) → **BẬT CỔNG TCP**.

Ô **"Nhập ở PC2"** trong cùng khung hiện sẵn `IP:cổng` để điền vào PC2
(vd `192.168.1.50:502`) — nút 📋 sao chép nhanh vào clipboard, không cần tự
tìm IP. Ô này tự làm mới mỗi 5 giây (IP có thể đổi khi đổi Wi-Fi /
cắm Ethernet) và cập nhật ngay khi đổi cổng hoặc Device ID.

Firewall (chạy 1 lần, quyền Admin trên PC1):

```powershell
New-NetFirewallRule -DisplayName "Modbus TCP 502" -Direction Inbound `
  -Protocol TCP -LocalPort 502 -Action Allow
```

Client mẫu cho PC2:

```powershell
python modbus_tcp_client.py --host <IP_PC1>            # đọc 1 lần
python modbus_tcp_client.py --host <IP_PC1> --watch    # theo dõi liên tục
python modbus_tcp_client.py --host <IP_PC1> --fan on   # bật quạt
python modbus_tcp_client.py --host <IP_PC1> --estop    # E-STOP
```

Bảng thanh ghi đầy đủ: xem `../README.md` mục 2.3. Tóm tắt (device 254):

| Nhóm | Địa chỉ | Nội dung |
|---|---|---|
| FC04 input reg | 0–9 | RH×10, T×10, bơm/quạt/đèn, mode, E-STOP, flags, uptime |
| FC04 input reg | 10–11 | data_age_s (tuổi dữ liệu), seq (chống đọc trùng) |
| FC03 hold reg | 0–9 | 6 ngưỡng ×10, watchdog bơm/đèn, failsafe, mode lệnh |
| FC01/FC05 coil | 0–3 | bơm, quạt, đèn, E-STOP (đọc = trạng thái thật) |
| Device | 254 / 0 / 1 | HMI Bridge / catch-all / SHTC30 mirror (chỉ đọc) |

### Dành cho MES (Modbus master)

| Nhu cầu MES | Cách dùng |
|---|---|
| Import tag | Nút **📋 XUẤT TAG CHO MES** → `mes_tag_contract.csv` / `.json` (26 tag: device_id, area, address, name, type, scale, unit, writable) |
| Chất lượng dữ liệu | IR10 `data_age_s` + FLAGS b3 (stale khi >10 s) + IR11 `seq` |
| Bảo mật | Ô **IP cho phép** trong khung 🌐 (hỗ trợ CIDR). IP lạ nhận exception 0x0B. Lệnh ghi kèm `src` = IP người gửi để audit |
| Nhận diện thiết bị | FC0x2B → Vendor `AGRI`, ProductName `AGRI HMI SCADA` |

Tool cho đội MES (chạy trên PC2) — app Python (PyQt5), nói thẳng
Modbus TCP với PC1:

```powershell
cd ..\MES
python mes_app.py --host <IP_PC1> --port 502
```

App MES có 4 phần hệ: **Tổng quan** (biểu đồ nhiệt độ/độ ẩm 6 giờ, tỉ lệ
bơm chạy/nghỉ 7 ngày, số đợt quá nhiệt tuần) · **Lịch sử cảnh báo** ·
**Quản lý bảo trì** (watchdog, failsafe, tuổi dữ liệu, ngưỡng quá nhiệt) ·
**Công thức vận hành** (ghi 6 ngưỡng điều khiển + điều khiển bơm/quạt/đèn/E-STOP
+ xuất contract tag). Chi tiết: [`../MES/README.md`](../MES/README.md).

> Lệnh từ PC2 **không** ghi thẳng xuống bus RS485 mà đi qua HMI, nên vẫn
> giữ đủ chốt an toàn: E-STOP, liên động quạt↔đèn, khóa AUTO khi mất cảm biến,
> watchdog, và kiểm tra ràng buộc ngưỡng (lệnh sai bị từ chối).

Test:

```powershell
python -m pytest tests -q     # 53 test
```

## 8. File trong thư mục

| File | Mô tả |
|---|---|
| `hmi_shtc30_3kenh.py` | App HMI chính (PyQt5 + Modbus worker) |
| `firebase_sync.py` | Thread đồng bộ Firebase REST (không cần SDK) |
| `modbus_tcp_gateway.py` | Server Modbus TCP cho PC2 + bảng thanh ghi |
| `modbus_tcp_client.py` | Client mẫu chạy trên PC2 (đọc/ghi) |
| `mes_tag_contract.csv` / `.json` | Contract tag xuất sẵn cho MES import (app MES cũng xuất được) |
| `tests/` | 40 test: bảng thanh ghi, round-trip client, MES contract, whitelist IP, an toàn luồng lệnh, hàng đợi Firebase |
| `modbus_probe.py` | Dò slave/baud/FC trên bus RS485 |
| `log_khi_hau_phong.csv` | Log CSV sinh ra khi chạy |

## 9. Lỗi thường gặp

| Hiện tượng | Cách xử lý |
|---|---|
| `Không mở được COMx` | Đóng Arduino IDE/Hercules/Tera Term… chỉ 1 app được giữ cổng |
| Mở cổng được nhưng không có dữ liệu | Đảo dây A/B, nối GND chung, kiểm tra nguồn 24 V, đúng slave/baud; bấm **CHẨN ĐOÁN BUS** |
| Relay bấm mà không đóng | Đổi **kênh relay** hoặc **kiểu ghi FC05/FC06**; board 4 kênh thường là slave 255 + FC05 |
| Giá trị RH/T bất thường | Sai thanh ghi/hệ số: SHTC30 dùng reg 0 = nhiệt độ, reg 1 = độ ẩm, ×0,1 (đã đo xác nhận) |
| Firebase 401/403 | Mở Rules cho `hmi/` và `erp/` (xem `ERP_HMI/README.md`) |
| PC2 không nối được cổng 502 | Mở firewall (xem mục 7), kiểm tra PC1 đã bật cổng, tắt tường lửa/antivirus tạm để khoanh vùng |
| Cổng 502 báo "đang bị chiếm" | Đã có app HMI/MES khác chiếm cổng. Đóng app cũ hoặc đổi sang 1502 (nhớ đổi cả `--port` bên PC2) |

