# -*- coding: utf-8 -*-
"""Test UI: bảng khung Modbus RTU/ASCII + xuất dữ liệu trên Web ERP.

Kiểm tra bằng trình duyệt thật (Playwright + Edge/Chrome), chế độ DEMO nên
không cần Firebase. Trang "Giám sát" phải hiện bảng khung Modbus (RTU + ASCII)
và nút xuất CSV phải tải được file có cột "Khung ASCII".
"""
import os
import socket
import sys
import threading
import time

import pytest

ERP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ERP)

sync_playwright = pytest.importorskip("playwright.sync_api",
                                      reason="chưa cài playwright").sync_playwright


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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
    time.sleep(0.5)
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


def _open_demo(page, site):
    errors = []
    page.on("console", lambda m: errors.append(m.text)
            if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.add_init_script(
        "try{localStorage.setItem('agri_erp_cfg_v1', JSON.stringify("
        "{url:'',secret:'',device:'vuon-1',display:'light'}));}catch(e){}")
    page.goto(site, wait_until="load")
    page.wait_for_timeout(1500)
    return errors


def test_trang_giam_sat_hien_bang_khung_modbus(site, browser):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = _open_demo(page, site)

    page.click('.nav[data-tab="monitor"]')
    page.wait_for_timeout(600)

    # Bảng khung Modbus phải có trong trang Giám sát
    assert page.eval_on_selector("#traceBody", "el => !!el")
    rows = page.eval_on_selector_all("#traceBody tr", "els => els.length")
    assert rows > 0, "bảng khung Modbus rỗng"

    # Khung ASCII đúng byte thật phải xuất hiện
    txt = page.eval_on_selector("#traceBody", "el => el.innerText")
    assert ":010400000002F9" in txt, txt[:200]
    assert ":010404011701DE00" in txt
    # Khung RTU
    assert "01 04 00 00 00 02 71 CB" in txt
    # CRC-16 / LRC
    assert "CRC-16 71CB" in txt
    assert "LRC F9" in txt
    # Mã hàm có tên
    assert "FC04" in txt

    # Bộ đếm khung + số mã hàm
    assert page.eval_on_selector("#traceCount", "el => Number(el.textContent) > 0")
    assert page.eval_on_selector("#traceFns", "el => Number(el.textContent) > 0")

    assert not errors, f"lỗi console: {errors[:2]}"
    page.close()


def test_xuat_csv_khung_modbus(site, browser):
    page = browser.new_page(viewport={"width": 1440, "height": 900},
                            accept_downloads=True)
    errors = _open_demo(page, site)

    page.click('.nav[data-tab="monitor"]')
    page.wait_for_timeout(500)

    with page.expect_download() as dl_info:
        page.click("#btnTraceExport")
    dl = dl_info.value
    assert dl.suggested_filename.startswith("modbus-trace-")
    path = dl.path()
    data = open(path, "r", encoding="utf-8-sig").read()
    assert "Khung ASCII" in data
    assert ":010400000002F9" in data
    assert "CRC-16" in data and "LRC" in data

    # Trang Báo cáo cũng có nút xuất khung Modbus
    page.click('.nav[data-tab="reports"]')
    page.wait_for_timeout(400)
    assert page.eval_on_selector("#btnExpTrace", "el => !!el")
    with page.expect_download() as dl2_info:
        page.click("#btnExpTrace")
    assert dl2_info.value.suggested_filename.startswith("modbus-trace-")

    assert not errors, f"lỗi console: {errors[:2]}"
    page.close()


def test_json_toan_bo_co_khung_modbus(site, browser):
    page = browser.new_page(viewport={"width": 1440, "height": 900},
                            accept_downloads=True)
    errors = _open_demo(page, site)

    page.click('.nav[data-tab="reports"]')
    page.wait_for_timeout(500)
    with page.expect_download() as dl_info:
        page.click("#btnExpAll")
    data = open(dl_info.value.path(), "r", encoding="utf-8").read()
    assert '"modbus_trace"' in data
    assert ":010400000002F9" in data

    assert not errors, f"lỗi console: {errors[:2]}"
    page.close()
