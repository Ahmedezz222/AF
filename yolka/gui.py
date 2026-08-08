# -*- coding: utf-8 -*-
"""Графический интерфейс yolka: список дисков, «Подключить» / «Отключить».

    python -m yolka.gui

Всё в одном окне: выбираете маковский диск, жмёте «Подключить» — yolka
запускает сервер чтения (одно окно UAC), поднимает WebDAV и вешает диск
на свободную букву. «Отключить» снимает букву и гасит всё за собой.
"""
import ctypes
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from . import open_volume
from .apfs import ServerDevice
from .dav import Handler, DavState, ThreadingHTTPServer, _set_explorer_label

RAW_PORT_BASE = 47831
DAV_PORT_BASE = 8809


def _system_disk_indexes():
    """Номера дисков, на которых живёт Windows (C:) — их не показываем."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Partition -DriveLetter C | Select-Object DiskNumber | ConvertTo-Json"],
            capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW).stdout
        data = json.loads(out or "{}")
        if isinstance(data, dict):
            data = [data]
        return {d["DiskNumber"] for d in data}
    except Exception:
        return set()


def list_disks():
    """[(index, model, size_gb)] физических дисков, кроме системного."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_DiskDrive | Select-Object Index,Model,Size | ConvertTo-Json"],
        capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW).stdout
    data = json.loads(out or "[]")
    if isinstance(data, dict):
        data = [data]
    skip = _system_disk_indexes()
    return sorted((d["Index"], d.get("Model") or "?",
                   round((d.get("Size") or 0) / 1e9))
                  for d in data if d["Index"] not in skip)


def free_letter():
    for ch in "YXWVUTSRQPONMLKJIHG":
        if not os.path.exists(f"{ch}:\\"):
            return ch + ":"
    raise IOError("нет свободных букв дисков")


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def server_reads_ok(port):
    """Проверяет, что сервер на порту реально читает диск,
    а не держит битую ручку (после передёргивания винта)."""
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.sendall(struct.pack("<QQ", 0, 512))
        n = struct.unpack("<Q", s.recv(8))[0]
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                break
            buf += chunk
        s.close()
        return len(buf) == 512
    except OSError:
        return False


def stop_raw_server(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
            s.sendall(struct.pack("<QQ", 0, 0xFFFFFFFFFFFFFFFF))
            s.recv(8)
    except OSError:
        pass


class Mount:
    def __init__(self, disk_index):
        self.disk = disk_index
        self.raw_port = RAW_PORT_BASE + disk_index
        self.dav_port = DAV_PORT_BASE + disk_index
        self.letter = None
        self.volume = None
        self.httpd = None

    def start(self, log):
        # зомби после передёргивания диска: порт занят, но чтение мёртвое
        if port_open(self.raw_port) and not server_reads_ok(self.raw_port):
            log("старый сервер не читает диск — перезапускаю…")
            stop_raw_server(self.raw_port)
            for _ in range(20):
                if not port_open(self.raw_port):
                    break
                time.sleep(0.2)
        if not port_open(self.raw_port):
            log("запускаю сервер чтения (окно UAC — нажмите «Да»)…")
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable.replace("python.exe", "pythonw.exe"),
                f'-m yolka.server \\\\.\\PhysicalDrive{self.disk} {self.raw_port}',
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 0)
            if rc <= 32:
                raise IOError("запуск сервера отклонён (UAC)")
            for _ in range(120):
                if port_open(self.raw_port):
                    break
                time.sleep(0.5)
            else:
                raise IOError("сервер чтения не поднялся")

        log("открываю файловую систему…")
        self.volume = open_volume(ServerDevice(port=self.raw_port))

        log(f"поднимаю WebDAV на порту {self.dav_port}…")
        handler = type("H", (Handler,), {"state": DavState(self.volume)})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.dav_port), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        _set_explorer_label("127.0.0.1", self.dav_port, f"{self.volume.name} (yolka)")

        self.letter = free_letter()
        log(f"вешаю букву {self.letter}…")
        r = subprocess.run(["net", "use", self.letter,
                            f"\\\\127.0.0.1@{self.dav_port}\\DavWWWRoot"],
                           capture_output=True, text=True, timeout=60,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode != 0:
            raise IOError(f"net use: {r.stderr.strip() or r.stdout.strip()}")
        log(f"готово: {self.volume.name} → {self.letter}")

    def stop(self, log):
        if self.letter:
            subprocess.run(["net", "use", self.letter, "/delete", "/y"],
                           capture_output=True, text=True, timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            log(f"буква {self.letter} снята")
            self.letter = None
        if self.httpd:
            self.httpd.shutdown()
            self.httpd = None
        stop_raw_server(self.raw_port)
        log("серверы остановлены")


class App:
    def __init__(self, root):
        self.root = root
        root.title("🌲 yolka — маковские диски в Windows")
        root.geometry("640x360")
        self.mounts = {}  # disk_index -> Mount

        bar = ttk.Frame(root, padding=6)
        bar.pack(fill="x")
        ttk.Button(bar, text="Обновить список", command=self.refresh).pack(side="left")
        self.btn_mount = ttk.Button(bar, text="Подключить", command=self.mount)
        self.btn_mount.pack(side="left", padx=6)
        self.btn_umount = ttk.Button(bar, text="Отключить", command=self.unmount)
        self.btn_umount.pack(side="left")

        cols = ("model", "size", "status")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", height=8)
        for col, txt, w in (("model", "Диск", 320), ("size", "ГБ", 70),
                            ("status", "Состояние", 200)):
            self.tree.heading(col, text=txt)
            self.tree.column(col, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=6)

        self.status = tk.StringVar(value="выберите диск и нажмите «Подключить»")
        ttk.Label(root, textvariable=self.status, padding=6).pack(fill="x")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh()

    def log(self, msg):
        self.status.set(msg)
        self.root.update_idletasks()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for idx, model, size in list_disks():
            m = self.mounts.get(idx)
            state = f"{m.volume.name} → {m.letter}" if m and m.letter else "—"
            self.tree.insert("", "end", iid=str(idx),
                             values=(f"[{idx}] {model}", size, state))

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("yolka", "Сначала выберите диск в списке.")
            return None
        return int(sel[0])

    def mount(self):
        idx = self._selected()
        if idx is None:
            return
        if idx in self.mounts and self.mounts[idx].letter:
            self.log("этот диск уже подключён")
            return
        m = Mount(idx)
        try:
            m.start(self.log)
            self.mounts[idx] = m
        except Exception as ex:
            self.log(f"не получилось: {ex}")
            try:
                m.stop(lambda s: None)
            except Exception:
                pass
        self.refresh()

    def unmount(self):
        idx = self._selected()
        if idx is None:
            return
        m = self.mounts.get(idx)
        if not m:
            self.log("этот диск не подключался через это окно")
            return
        m.stop(self.log)
        del self.mounts[idx]
        self.refresh()

    def on_close(self):
        for m in list(self.mounts.values()):
            try:
                m.stop(lambda s: None)
            except Exception:
                pass
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
