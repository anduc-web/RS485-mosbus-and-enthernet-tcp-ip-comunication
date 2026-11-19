# -*- coding: utf-8 -*-
"""Logic thống kê cho MES (thuần, không I/O — dễ test).

Các hàm ở đây biến danh sách mẫu telemetry (mỗi mẫu là 1 dict) thành số
liệu mà app MES hiển thị:
  • build_series      → chuỗi điểm đo (biểu đồ nhiệt độ / độ ẩm)
  • run_ratio        → tỉ lệ thời gian bơm chạy / nghỉ
  • overheat_events  → các đợt quá nhiệt
  • detect_events    → sự cố phát sinh giữa 2 mẫu liên tiếp
  • summarize        → gộp tất cả cho API

Mẫu hỗ trợ 2 dạng (cùng một hàm xử lý được):

  * **mẫu thô** (đo trực tiếp, chu kỳ 2 s):
        {"ts": 1700000000, "temp": 26.4, "rh": 47.8, "pump": False, ...}
  * **mẫu tổng hợp theo phút** (bucket, giữ 30 ngày):
        {"ts": 1700000000, "dt": 60, "temp": 26.4, "temp_max": 27.1,
         "rh": 47.8, "rh_max": 49.0, "pump": True, "pump_seconds": 60, ...}

  Với bucket, `dt` = số giây bucket phủ, `pump_seconds` = số giây bơm chạy
  trong bucket → tính tỉ lệ thời gian chính xác (không tính theo số mẫu).

Nguyên tắc chung: một "đợt quá nhiệt" chỉ kết thúc khi có mẫu MÁT xen giữa.
Ranh giới giữa bucket và mẫu thô KHÔNG làm ngắt đợt — nên có thể ghép
bucket (cũ) với mẫu thô (mới) vào cùng một chuỗi mà vẫn đếm đúng số đợt.
"""

# Khoảng thời gian mặc định
DEFAULT_HOURS = 6
DEFAULT_DAYS = 7

#: Khoang trong toi da giua 2 mau trong cung 1 dot qua nhiet (giay).
#: Lon hon nguong nay = thiet bi mat ket noi -> tach thanh dot moi,
#: khong duoc coi ca khoang offline la mot dot nong keo dai.
MAX_GAP_S = 300


def _sorted(samples):
    return sorted(samples or [], key=lambda s: s.get("ts", 0))


def _in_window(samples, now, days=None, hours=None):
    """Lọc mẫu nằm trong cửa sổ thời gian (mới nhất nhất)."""
    if not samples:
        return []
    if days is not None:
        cutoff = now - days * 86400
    elif hours is not None:
        cutoff = now - hours * 3600
    else:
        return list(samples)
    return [s for s in samples if s.get("ts", 0) >= cutoff]


def _dt_of(sample, next_sample, prev_sample):
    """Số giây mà một mẫu đại diện.

    Ưu tiên `dt` (bucket) → nếu không có, dùng khoảng cách tới mẫu kế tiếp;
    mẫu cuối dùng khoảng cách về mẫu trước.
    """
    dt = sample.get("dt")
    if dt is not None:
        return max(0.0, float(dt))
    if next_sample is not None:
        return max(0.0, float(next_sample["ts"]) - float(sample["ts"]))
    if prev_sample is not None:
        return max(0.0, float(sample["ts"]) - float(prev_sample["ts"]))
    return 0.0


def _pump_seconds(sample, dt):
    """Số giây bơm chạy mà mẫu này đại diện."""
    if sample.get("pump_seconds") is not None:
        return max(0.0, float(sample["pump_seconds"]))
    return dt if sample.get("pump") else 0.0


def _hot_value(sample):
    """Nhiệt độ dùng để so ngưỡng (bucket lấy đỉnh trong phút)."""
    value = sample.get("temp_max")
    if value is None:
        value = sample.get("temp")
    return value


def _rh_value(sample):
    """Độ ẩm dùng để thống kê (bucket lấy đỉnh trong phút)."""
    value = sample.get("rh_max")
    if value is None:
        value = sample.get("rh")
    return value


def build_series(samples, hours=DEFAULT_HOURS, now=None, max_points=600):
    """Chuỗi điểm đo trong `hours` giờ gần nhất, giảm mẫu nếu quá nhiều."""
    import time as _time

    if now is None:
        now = _time.time()
    window = _in_window(_sorted(samples), now, hours=hours)
    if len(window) <= max_points:
        return [{"ts": s["ts"], "temp": s.get("temp"), "rh": s.get("rh"),
                 "pump": bool(s.get("pump"))} for s in window]

    step = len(window) / float(max_points)
    picked = []
    for i in range(max_points):
        picked.append(window[int(i * step)])
    return [{"ts": s["ts"], "temp": s.get("temp"), "rh": s.get("rh"),
             "pump": bool(s.get("pump"))} for s in picked]


def run_ratio(samples, days=DEFAULT_DAYS, now=None):
    """Tỉ lệ thời gian bơm chạy / nghỉ trong `days` ngày.

    Tính theo THỜI GIAN thực (dùng dt / pump_seconds của bucket) nên chính
    xác kể cả khi mẫu thô bị lấy không đều.
    """
    import time as _time

    if now is None:
        now = _time.time()
    window = _sorted(_in_window(samples, now, days=days))
    run_s = idle_s = 0.0
    for i, s in enumerate(window):
        nxt = window[i + 1] if i + 1 < len(window) else None
        prev = window[i - 1] if i > 0 else None
        dt = _dt_of(s, nxt, prev)
        ps = _pump_seconds(s, dt)
        run_s += ps
        idle_s += max(0.0, dt - ps)
    total = run_s + idle_s
    pct = int(round(run_s * 100.0 / total)) if total else 0
    return {
        "run_s": int(round(run_s)),
        "idle_s": int(round(idle_s)),
        "total_s": int(round(total)),
        "run_pct": pct,
        "idle_pct": 100 - pct,
        "samples": len(window),
    }


def overheat_events(samples, threshold_c=35.0, days=DEFAULT_DAYS, now=None):
    """Các đợt quá nhiệt (nhiệt độ > ngưỡng) trong `days` ngày.

    Một đợt kết thúc khi gặp mẫu MÁT xen giữa. Thời lượng = khoảng cách
    từ mẫu nóng đầu đến mẫu nóng cuối.
    """
    import time as _time

    if now is None:
        now = _time.time()
    window = _sorted(_in_window(samples, now, days=days))

    events = []
    cur = None

    def _close(ev):
        ev["duration_s"] = int(max(0, ev["end_ts"] - ev["start_ts"]))
        events.append(ev)

    for s in window:
        temp = _hot_value(s)
        hot = temp is not None and temp > threshold_c
        # Khoang trong lon = mat ket noi: khong biet o giua nong hay mat,
        # nen dot phai tach ra thay vi keo dai qua ca khoang offline.
        gap_too_big = (cur is not None
                       and s["ts"] - cur["end_ts"] > MAX_GAP_S)
        if cur is not None and (not hot or gap_too_big):
            _close(cur)
            cur = None
        if hot:
            if cur is None:
                cur = {"start_ts": s["ts"], "end_ts": s["ts"],
                       "peak_temp": float(temp)}
            else:
                cur["end_ts"] = s["ts"]
                cur["peak_temp"] = max(cur["peak_temp"], float(temp))
    if cur is not None:
        _close(cur)
    return events


def detect_events(prev, cur, overheat_c=35.0, now=None):
    """Sự cố phát sinh giữa mẫu `prev` và mẫu `cur`."""
    import time as _time

    if now is None:
        now = _time.time()
    if cur is None:
        return []
    events = []
    ts = cur.get("ts", now)

    if cur.get("estop") and not (prev or {}).get("estop"):
        events.append({"kind": "estop", "ts": ts, "level": "ERROR",
                       "msg": "E-STOP được kích hoạt"})

    if prev is not None:
        if prev.get("sensor_ok") and not cur.get("sensor_ok"):
            events.append({"kind": "sensor_lost", "ts": ts, "level": "WARN",
                           "msg": "Mất dữ liệu cảm biến SHTC30"})
        elif not prev.get("sensor_ok") and cur.get("sensor_ok"):
            events.append({"kind": "link_ok", "ts": ts, "level": "OK",
                           "msg": "Cảm biến có dữ liệu trở lại"})
        pt = _hot_value(prev)
        ct = _hot_value(cur)
        if (pt is not None and ct is not None and pt <= overheat_c
                and ct > overheat_c):
            events.append({"kind": "overheat", "ts": ts, "level": "WARN",
                           "msg": f"Nhiệt độ vượt ngưỡng {overheat_c:g} °C "
                                  f"({ct:.1f} °C)"})
    return events


def _weighted_avg(window, key):
    """Trung bình theo THỜI GIAN (trọng số dt) — chính xác hơn trung bình
    theo số mẫu khi mẫu thô bị lấy không đều."""
    acc = wsum = 0.0
    for i, s in enumerate(window):
        nxt = window[i + 1] if i + 1 < len(window) else None
        prev = window[i - 1] if i > 0 else None
        dt = _dt_of(s, nxt, prev)
        value = s.get(key)
        if value is None:
            continue
        acc += float(value) * dt
        wsum += dt
    return (acc / wsum) if wsum else None


def _peak(window, key, biggest=True):
    values = [s[key] for s in window if s.get(key) is not None]
    if not values:
        return None
    return max(values) if biggest else min(values)


def summarize(samples, days=DEFAULT_DAYS, now=None, overheat_c=35.0):
    """Gộp mọi số liệu MES cần hiển thị."""
    import time as _time

    if now is None:
        now = _time.time()
    ordered = _sorted(samples)
    window = _in_window(ordered, now, days=days)

    ratio = run_ratio(window, days=days, now=now)
    over = overheat_events(window, threshold_c=overheat_c, days=days, now=now)

    temp_avg = _weighted_avg(window, "temp")
    rh_avg = _weighted_avg(window, "rh")
    temp_max = _peak(window, "temp_max") or _peak(window, "temp")
    temp_min = _peak(window, "temp_min", biggest=False) \
        if any(s.get("temp_min") is not None for s in window) \
        else _peak(window, "temp", biggest=False)
    rh_max = _peak(window, "rh_max") or _peak(window, "rh")
    rh_min = _peak(window, "rh_min", biggest=False) \
        if any(s.get("rh_min") is not None for s in window) \
        else _peak(window, "rh", biggest=False)

    return {
        "samples": len(window),
        "temp_avg": round(temp_avg, 1) if temp_avg is not None else 0.0,
        "temp_max": round(temp_max, 1) if temp_max is not None else 0.0,
        "temp_min": round(temp_min, 1) if temp_min is not None else 0.0,
        "rh_avg": round(rh_avg, 1) if rh_avg is not None else 0.0,
        "rh_max": round(rh_max, 1) if rh_max is not None else 0.0,
        "rh_min": round(rh_min, 1) if rh_min is not None else 0.0,
        "run_pct": ratio["run_pct"],
        "run_s": ratio["run_s"],
        "idle_s": ratio["idle_s"],
        "overheat_count": len(over),
        "last": ordered[-1] if ordered else None,
    }
