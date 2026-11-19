# -*- coding: utf-8 -*-
"""Test E2E: khung Modbus do HMI sinh → Firebase (giả lập) → Web ERP hiển thị.

Đây là test HỢP ĐỒNG giữa 2 tầng:
  • dùng đúng code HMI (`modbus_frames.make_record` + `FirebaseSync.push_trace`)
    để tạo và đẩy bản ghi lên Firebase,
  • mở Web ERP bằng trình duyệt thật và kiểm tra bảng "Giao thức Modbus"
    hiển thị đúng khung ASCII / RTU / CRC-16 / LRC.

Firebase được giả lập bằng HTTP server tối giản (REST giống Realtime DB).
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ERP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HMI = os.path.join(os.path.dirname(ERP), "HMI")
sys.path.insert(0, ERP)
sys.path.insert(0, HMI)

sync_playwright = pytest.importorskip("playwright.sync_api",
                                      reason="chưa cài playwright").sync_playwright


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeFirebase(BaseHTTPRequestHandler):
    """REST giả lập: /<path>.json → PUT/POST ghi, GET đọc map {key: value}."""

    store = {}

    def _reply(self, payload, code=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        # Firebase thật có CORS; fake cũng phải có để web ERP gọi được.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._reply(None)

    def _path(self):
        p = self.path.split("?")[0]
        return p[1:] if p.startswith("/") else p

    def do_GET(self):
        path = self._path()
        if not path.endswith(".json"):
            self._reply(None)
            return
        base = path[:-5]
        # Node cha → trả map các con (giống Realtime Database).
        if base in FakeFirebase.store:
            self._reply(FakeFirebase.store[base])
            return
        children = {k[len(base) + 1:]: v for k, v in FakeFirebase.store.items()
                    if k.startswith(base + "/")}
        self._reply(children or None)

    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"null")
        path = self._path()
        FakeFirebase.store[path[:-5] if path.endswith(".json") else path] = body
        self._reply(None)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"null")
        path = self._path()
        key = f"-k{len(FakeFirebase.store) + 1:04d}"
        FakeFirebase.store[f"{path[:-5] if path.endswith('.json') else path}/{key}"] = body
        self._reply({"name": key})

    def do_DELETE(self):
        FakeFirebase.store.pop(self._path()[:-5], None)
        self._reply(None)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def firebase():
    FakeFirebase.store = {}
    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), FakeFirebase)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def site():
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=ERP, **kw)

        def log_message(self, *a):
            pass

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.4)
    yield f"http://127.0.0.1:{port}/"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = None
        for channel in ("msedge", "chrome"):
            try:
                b = p.chromium.launch(channel=channel)
                break
            except Exception:
                b = None
        if b is None:
            pytest.skip("không có Edge/Chrome cho Playwright")
        yield b
        b.close()


def _hmi_doi_duong_khung():
    """Sinh bản ghi khung bằng CHÍNH code HMI (khung RTU + ASCII thật)."""
    import modbus_frames as mf
    from firebase_sync import FirebaseSync

    rtu, asc = mf.read_request(1, 4, 0, 2)
    rec = mf.make_record("TX", 1, 4, rtu, asc, note="đọc 2 ô @0x0000")
    rtu2, asc2 = mf.read_response(1, 4, [0x0117, 0x01DE])
    rec2 = mf.make_record("RX", 1, 4, rtu2, asc2, note="2 giá trị")

    sync = FirebaseSync(device="vuon-1")
    sync._url = ""            # gán sau
    return sync, [rec, rec2]


def test_hmi_day_khung_modbus_len_firebase_va_web_hien_thi(firebase, site, browser):
    sync, records = _hmi_doi_duong_khung()
    sync._url = firebase
    sync.push_trace(records)
    sync._flush_trace()

    # Firebase giả lập phải nhận đúng 2 bản ghi, có trường "ascii"
    stored = [v for k, v in FakeFirebase.store.items()
              if k.startswith("hmi/vuon-1/modbus_trace/")]
    assert len(stored) == 2, stored
    ascii_vals = sorted(r["ascii"] for r in stored)
    assert ascii_vals == [":010400000002F9", ":010404011701DE00"], ascii_vals
    assert stored[0]["crc16"] and stored[0]["lrc"]

    # Web ERP đọc đúng nhánh đó và hiển thị khung ASCII
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    page.on("console", lambda m: errors.append(m.text)
            if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.add_init_script(
        "try{localStorage.setItem('agri_erp_cfg_v1', JSON.stringify("
        + json.dumps({"url": firebase, "secret": "", "device": "vuon-1",
                      "display": "light"})
        + "));}catch(e){}")
    page.goto(site, wait_until="load")
    page.wait_for_timeout(1500)

    page.click('.nav[data-tab="monitor"]')
    page.wait_for_timeout(1200)

    txt = page.eval_on_selector("#traceBody", "el => el.innerText")
    assert ":010400000002F9" in txt, txt[:300]
    assert ":010404011701DE00" in txt, txt[:300]
    assert "01 04 00 00 00 02 71 CB" in txt
    assert "FC04" in txt
    assert "CRC-16 71CB" in txt
    assert "LRC F9" in txt
    assert page.eval_on_selector("#traceCount", "el => Number(el.textContent)") == 2

    assert not errors, f"lỗi console: {errors[:2]}"
    page.close()
