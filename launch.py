"""
launch.py — Portable entry point สำหรับ BMA-Plan
รัน: python launch.py  หรือ  BMA-Plan.exe (หลัง build)
"""
import sys
import threading
import time
import webbrowser
import socket
import uvicorn
from server import app


def _free_port(start: int = 8000) -> int:
    """หา port ที่ว่าง เผื่อ 8000 ถูกใช้อยู่"""
    for p in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def _wait_for_server(port: int, timeout: float = 20.0) -> bool:
    """รอจน server พร้อมรับ request"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _run_server(port: int):
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    port = _free_port()
    url  = f"http://127.0.0.1:{port}"

    print("=" * 40)
    print("  BMA-Plan — PDF Scale Tool")
    print("=" * 40)
    print(f"  กำลังเริ่ม server ที่ {url}")
    print("  ปิดหน้าต่างนี้เพื่อออกจากโปรแกรม")
    print("=" * 40)

    t = threading.Thread(target=_run_server, args=(port,), daemon=True)
    t.start()

    if _wait_for_server(port):
        print(f"  ✅ พร้อมแล้ว — เปิด browser อัตโนมัติ")
        webbrowser.open(url)
    else:
        print("  ❌ ไม่สามารถเริ่ม server ได้")
        sys.exit(1)

    try:
        t.join()
    except KeyboardInterrupt:
        print("\n  ออกจากโปรแกรม")
