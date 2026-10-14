"""AI service: YOLO nhan dien nguoi/vat the, gui so nguoi len backend va phat video MJPEG.

Cai:   python -m pip install ultralytics opencv-python
Chay tu thu muc goc: python backend\\ai_service.py --show
Nguon video khac:    python backend\\ai_service.py --source video.mp4
Video noi bo:        http://localhost:8001/stream
Dashboard dien thoai xem qua backend da dang nhap.
"""
import argparse
import json
import os
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

DEVICE_KEY = ""
latest_jpeg = None
lock = threading.Lock()


def load_device_config():
    secrets_path = Path(__file__).resolve().parent.parent / "firmware" / "include" / "secrets.h"
    settings = {}
    if secrets_path.is_file():
        contents = secrets_path.read_text(encoding="utf-8")
        for name in ("DEVICE_ID", "DEVICE_KEY"):
            match = re.search(
                rf'^\s*#define\s+{name}\s+"([^"]+)"',
                contents,
                re.MULTILINE,
            )
            if match:
                settings[name] = match.group(1)

    device_key = os.environ.get("RADAR_DEVICE_KEY") or settings.get("DEVICE_KEY")
    device_id = os.environ.get("RADAR_DEVICE_ID") or settings.get("DEVICE_ID")
    if not device_key:
        raise RuntimeError(
            "Thiếu khóa thiết bị: đặt RADAR_DEVICE_KEY hoặc cấu hình DEVICE_KEY "
            "trong firmware/include/secrets.h."
        )
    if not device_id:
        raise RuntimeError(
            "Thiếu mã thiết bị: đặt RADAR_DEVICE_ID hoặc cấu hình DEVICE_ID "
            "trong firmware/include/secrets.h."
        )
    return device_id, device_key


class StreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.path.startswith("/stream"):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                with lock:
                    jpg = latest_jpeg
                if jpg is not None:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n")
                time.sleep(0.07)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *args):
        pass


def get(url):
    req = urllib.request.Request(url, headers={"X-Device-Key": DEVICE_KEY})
    return json.loads(urllib.request.urlopen(req, timeout=2).read().decode())


def open_cap(src):
    cap = cv2.VideoCapture(src, cv2.CAP_DSHOW) if isinstance(src, int) else cv2.VideoCapture(src)
    return cap if cap.isOpened() else None


def off_frame(text):
    img = np.full((360, 640, 3), 30, dtype=np.uint8)
    cv2.putText(img, text, (150, 190), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (200, 200, 200), 2)
    return img


def publish(frame):
    global latest_jpeg
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
    if ok:
        with lock:
            latest_jpeg = buf.tobytes()


def post(url, data):
    req = urllib.request.Request(
        url, data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json", "X-Device-Key": DEVICE_KEY}, method="POST")
    urllib.request.urlopen(req, timeout=2).read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="0", help="so webcam (0,1..) hoac duong dan video/RTSP")
    ap.add_argument("--device", help="thiet bi radar di cung camera nay (mac dinh doc tu secrets.h)")
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--model", default="yolov8n.pt")
    ap.add_argument("--conf", type=float, default=0.5, help="nguong tin cay (0-1)")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--show", action="store_true", help="mo cua so xem truc tiep")
    args = ap.parse_args()

    global DEVICE_KEY
    default_device_id, DEVICE_KEY = load_device_config()
    args.device = args.device or default_device_id

    print("Dang tai mo hinh YOLO (lan dau se tu tai file ~6MB)...")
    model = YOLO(args.model)
    src = int(args.source) if args.source.isdigit() else args.source

    server = ThreadingHTTPServer(("0.0.0.0", args.port), StreamHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Video truc tiep: http://localhost:{args.port}/stream")
    print(f"Gui ket qua cho thiet bi {args.device} -> {args.url}  (Ctrl+C de dung)")
    publish(off_frame("Dang khoi dong..."))

    cap, enabled, last_cfg = None, True, 0.0
    last_config_error = None
    last_count, last_send = -1, 0.0
    while True:
        now = time.time()

        # Hoi backend: camera dang bat hay tat (bam nut tren web)
        if now - last_cfg >= 1.5:
            last_cfg = now
            try:
                enabled = bool(get(f"{args.url}/api/camera/{args.device}/config")["enabled"])
                last_config_error = None
            except Exception as error:
                message = str(error)
                if message != last_config_error:
                    print(f"Khong doc duoc cau hinh camera tu backend: {message}")
                    last_config_error = message

        if not enabled:
            if cap is not None or last_count != -2:
                if cap is not None:
                    cap.release()      # nha webcam that su (den camera tat)
                    cap = None
                last_count = -2
                publish(off_frame("Camera da tat"))
                if args.show:
                    cv2.destroyAllWindows()
                print("Camera TAT (theo lenh tu web)")
            time.sleep(0.3)
            continue

        if cap is None:
            cap = open_cap(src)
            if cap is None:
                print("Khong mo duoc camera/video:", args.source, "- thu lai sau 2 giay")
                publish(off_frame("Khong mo duoc camera"))
                time.sleep(2)
                continue
            last_count = -1
            print("Camera BAT")

        ok, frame = cap.read()
        if not ok:
            if isinstance(src, str):          # het video file thi phat lai
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            time.sleep(0.2)
            continue

        res = model(frame, conf=args.conf, verbose=False)[0]
        person_count = 0
        object_count = 0
        for b in res.boxes:
            class_id = int(b.cls[0])
            label = model.names[class_id]
            if class_id == 0:
                person_count += 1
                color = (0, 200, 0)
            else:
                object_count += 1
                color = (0, 165, 255)
            x1, y1, x2, y2 = map(int, b.xyxy[0])
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            cv2.putText(frame, f"{label} {float(b.conf[0]):.2f}", (x1, max(y1 - 6, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        cv2.putText(
            frame,
            f"Nguoi: {person_count} | Vat the: {object_count}",
            (10, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (0, 0, 255),
            2,
        )
        publish(frame)

        if person_count != last_count or now - last_send >= 2.0:    # doi so nguoi hoac moi 2 giay
            try:
                post(f"{args.url}/api/camera", {"device_id": args.device, "persons": person_count})
                last_count, last_send = person_count, now
            except Exception as e:
                print("Khong gui duoc len backend:", e)
                last_send = now

        if args.show:
            cv2.imshow("YOLO camera (nhan q de thoat)", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    if cap is not None:
        cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDung AI service.")