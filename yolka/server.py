# -*- coding: utf-8 -*-
"""Сервер чтения диска. Запускается с правами администратора один раз,
дальше yolka работает без повышения прав.

    python -m yolka.server \\.\PhysicalDrive1 [порт]

Только чтение, слушает 127.0.0.1, сам выключается после 3 часов простоя.
"""
import os
import socket
import struct
import sys
import threading
import time

SECTOR = 4096
IDLE_EXIT = 3 * 3600


def main(device, port=47831):
    dev = open(device, "rb", buffering=0)
    lock = threading.Lock()
    last = [time.time()]

    def read_aligned(offset, size):
        a_start = (offset // SECTOR) * SECTOR
        a_end = ((offset + size + SECTOR - 1) // SECTOR) * SECTOR
        with lock:
            dev.seek(a_start)
            data = dev.read(a_end - a_start)
        return data[offset - a_start: offset - a_start + size]

    def handle(conn):
        try:
            while True:
                hdr = b""
                while len(hdr) < 16:
                    part = conn.recv(16 - len(hdr))
                    if not part:
                        return
                    hdr += part
                off, size = struct.unpack("<QQ", hdr)
                last[0] = time.time()
                if size > 64 * 1024 * 1024:
                    conn.sendall(struct.pack("<Q", 0))
                    continue
                try:
                    data = read_aligned(off, size)
                except Exception:
                    data = b""
                conn.sendall(struct.pack("<Q", len(data)) + data)
        finally:
            conn.close()

    def reaper():
        while True:
            time.sleep(60)
            if time.time() - last[0] > IDLE_EXIT:
                os._exit(0)

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    srv.settimeout(30)
    threading.Thread(target=reaper, daemon=True).start()
    print(f"yolka-server: {device} на 127.0.0.1:{port}")
    while True:
        try:
            conn, _ = srv.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
        except socket.timeout:
            continue


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 47831)
