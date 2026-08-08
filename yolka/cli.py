# -*- coding: utf-8 -*-
"""Командная строка yolka.

Примеры:
    # через сервер (сервер уже запущен от администратора):
    python -m yolka ls /
    python -m yolka ls "2026-08-07-101512.previous/Data/Users/lelya"
    python -m yolka extract "путь/на/диске" "C:\\куда\\положить"

    # напрямую из образа или устройства (если у процесса есть права):
    python -m yolka --image D:\\backup.img ls /
    python -m yolka --device \\\\.\\PhysicalDrive1 ls /
"""
import argparse
import sys

from . import open_volume, list_volumes
from .apfs import RawDevice, ServerDevice


def make_device(args):
    if args.image:
        return RawDevice(args.image)
    if args.device:
        return RawDevice(args.device)
    return ServerDevice(port=args.port)


def main(argv=None):
    p = argparse.ArgumentParser(prog="yolka", description="Читатель APFS (только чтение)")
    p.add_argument("--image", help="путь к образу диска")
    p.add_argument("--device", help=r"raw-устройство, например \\.\PhysicalDrive1 (нужен администратор)")
    p.add_argument("--port", type=int, default=47831, help="порт yolka-сервера (по умолчанию 47831)")
    p.add_argument("--vol", type=int, default=0, help="номер тома APFS (список: команда volumes)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_ls = sub.add_parser("ls", help="показать каталог")
    p_ls.add_argument("path", nargs="?", default="/")

    p_ex = sub.add_parser("extract", help="извлечь каталог или файл")
    p_ex.add_argument("src")
    p_ex.add_argument("dst")

    p_info = sub.add_parser("info", help="информация о томе")
    sub.add_parser("volumes", help="список томов на диске")

    args = p.parse_args(argv)
    dev = make_device(args)

    if args.cmd == "volumes":
        for i, name, kind in list_volumes(dev):
            print(f"{i}\t{name}\t({kind})")
        return

    vol = open_volume(dev, vol=args.vol)

    if args.cmd == "info":
        kind = "APFS" if hasattr(vol, "xid") else "HFS+"
        extra = f", контрольная точка xid={vol.xid}" if hasattr(vol, "xid") else ""
        print(f"том: {vol.name!r} ({kind}{extra})")
    elif args.cmd == "ls":
        fid = vol.resolve_path(args.path)
        for name, child in sorted(vol.list_dir(fid).items()):
            ino = vol.get_inode(child)
            mark = "/" if ino and (ino["mode"] & 0o170000) == 0o040000 else ""
            size = "" if mark else f"\t{(ino or {}).get('size') or 0}"
            print(f"{name}{mark}{size}")
    elif args.cmd == "extract":
        fid = vol.resolve_path(args.src)
        vol.extract(fid, args.dst)
        print("готово")


if __name__ == "__main__":
    main()
