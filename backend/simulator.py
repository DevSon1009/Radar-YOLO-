"""Gia lap ESP32 + radar: gui su kien len backend nhu thiet bi that.

Chay:  python simulator.py            (backend phai dang chay o cong 8000)
       python simulator.py --id esp32s3-02 --url http://localhost:8000
Nhan lenh tu web (arm / disarm / ack) bang cach hoi /api/devices/<id>/command.
"""
import argparse
import json
import os
import random
import time
import urllib.request

DEVICE_KEY = os.environ.get("RADAR_DEVICE_KEY", "radar-device-key")
CONFIRM_S = 2.0   # co nguoi lien tuc 2 giay moi bao dong (giong CONFIRM_MS)
CLEAR_S = 3.0     # het nguoi 3 giay thi ve ARMED (giong CLEAR_MS)


def call(url, data=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", "X-Device-Key": DEVICE_KEY},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode() or "{}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", default="esp32s3-01")
    ap.add_argument("--url", default="http://localhost:8000")
    args = ap.parse_args()

    state = "ARMED"
    t_state = time.time()
    person_until = 0.0     # thoi diem nguoi gia lap roi di
    next_person = time.time() + random.uniform(5, 10)
    last_hb = 0.0
    moving = stationary = 0

    def set_state(s):
        nonlocal state, t_state
        state, t_state = s, time.time()
        print(f">>> {s}")
        send()

    def send():
        call(f"{args.url}/api/events", {
            "device_id": args.id, "state": state,
            "moving_energy": moving, "stationary_energy": stationary,
            "radar_ok": False, "source": "simulator",
        })

    print(f"Gia lap thiet bi {args.id} -> {args.url}  (Ctrl+C de dung)")
    send()

    while True:
        now = time.time()

        # Nguoi gia lap xuat hien / roi di ngau nhien
        if now >= next_person and now >= person_until:
            person_until = now + random.uniform(4, 9)
            next_person = person_until + random.uniform(8, 15)
        present = now < person_until
        moving = random.randint(50, 95) if present else random.randint(0, 8)
        stationary = random.randint(30, 70) if present else random.randint(0, 5)

        # Lenh tu web
        try:
            cmd = call(f"{args.url}/api/devices/{args.id}/command").get("cmd")
        except Exception:
            cmd = None
        if cmd == "arm":
            set_state("ARMED")
        elif cmd == "disarm":
            set_state("DISARMED")
        elif cmd == "ack" and state == "ALARMING":
            set_state("ACKNOWLEDGED")

        # May trang thai giong firmware
        if state == "ARMED" and present:
            set_state("DETECTED")
        elif state == "DETECTED":
            if not present:
                set_state("ARMED")
            elif now - t_state >= CONFIRM_S:
                set_state("ALARMING")
        elif state in ("ALARMING", "ACKNOWLEDGED") and not present and now - person_until > CLEAR_S:
            set_state("ARMED")

        # Heartbeat + so lieu dinh ky
        if now - last_hb >= 5:
            last_hb = now
            send()
            call(f"{args.url}/api/heartbeat", {"device_id": args.id, "rssi": random.randint(-70, -45)})

        time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDung gia lap.")
