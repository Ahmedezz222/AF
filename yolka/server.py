# -*- coding: utf-8 -*-
r"""Сервер чтения диска. Запускается с правами администратора один раз,
дальше yolka работает без повышения прав.

    python -m yolka.server \\.\PhysicalDrive1 [порт]

Только чтение, слушает 127.0.0.1, сам выключается после 3 часов простоя.

Переживает передёргивание диска: если Windows аннулировал ручку (диск
выдернули/воткнули) — сервер находит тот же диск заново по «отпечатку»
первых секторов, даже если номер PhysicalDrive сменился.
"""
import os
import socket
import struct
import sys
import threading
import time

SECTOR = 4096
IDLE_EXIT = 3 * 3600
FINGERPRINT = 4096  # сколько байт с начала диска берём как уникальный отпечаток


def _open_raw(path):
    return open(path, "rb", buffering=0)


def _read_head(f, n):
    f.seek(0)
    return f.read(n)


def main(device, port=47831):
    fh = _open_raw(device)
    identity = _read_head(fh, FINGERPRINT)  # отпечаток нашего диска
    if not identity:
        raise IOError(f"не удалось прочитать диск {device}")
    dev = [fh]
    cur_path = [device]
    lock = threading.Lock()
    last = [time.time()]

    def find_by_identity():
        """Ищет наш диск среди всех PhysicalDrive по отпечатку.
        Возвращает (path, handle) или (None, None)."""
        # сперва пробуем прежний путь — чаще всего номер тот же
        candidates = [cur_path[0]] + [rf"\\.\PhysicalDrive{i}" for i in range(16)]
        for path in candidates:
            try:
                f = _open_raw(path)
                if _read_head(f, FINGERPRINT) == identity:
                    return path, f
                f.close()
            except OSError:
                continue
        return None, None

    def reopen():
        """Лечит битую ручку после передёргивания диска."""
        try:
            dev[0].close()
        except Exception:
            pass
        path, f = find_by_identity()
        if f is None:
            raise OSError("диск не найден (отключён?)")
        dev[0] = f
        cur_path[0] = path

    def read_aligned(offset, size):
        a_start = (offset // SECTOR) * SECTOR
        a_end = ((offset + size + SECTOR - 1) // SECTOR) * SECTOR
        want = a_end - a_start
        with lock:
            # до 4 попыток с переоткрытием — диск после втыкания
            # поднимается не мгновенно
            for attempt in range(4):
                try:
                    dev[0].seek(a_start)
                    data = dev[0].read(want)
                except OSError:
                    data = b""
                if data or want == 0:
                    return data[offset - a_start: offset - a_start + size]
                # пустой ответ на непустой запрос = битая ручка → чиним
                try:
                    reopen()
                except OSError:
                    if attempt < 3:
                        time.sleep(0.5)  # ждём, пока диск вернётся
                    else:
                        break
            return b""

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
                if off == 0 and size == 0xFFFFFFFFFFFFFFFF:
                    # команда выключения (доступна только с localhost)
                    conn.sendall(struct.pack("<Q", 0))
                    os._exit(0)
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
