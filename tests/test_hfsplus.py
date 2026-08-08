# -*- coding: utf-8 -*-
"""Тест HFS+-парсера на синтетическом образе, собранном руками.

    python tests/test_hfsplus.py
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from yolka import open_volume  # noqa: E402
from yolka.apfs import RawDevice  # noqa: E402

BS = 4096
HFS_EPOCH = 2082844800


def fork_data(logical_size, extents):
    raw = struct.pack(">QII", logical_size, 0, sum(c for _, c in extents))
    for s, c in (extents + [(0, 0)] * 8)[:8]:
        raw += struct.pack(">II", s, c)
    return raw


def cat_key(parent, name):
    n = name.encode("utf-16-be")
    body = struct.pack(">IH", parent, len(n) // 2) + n
    return struct.pack(">H", len(body)) + body


def folder_rec(cnid, mtime):
    r = struct.pack(">HHII", 1, 0, 0, cnid)                    # type, flags, valence, id
    r += struct.pack(">IIIII", mtime, mtime, mtime, mtime, 0)  # даты
    r += b"\x00" * (88 - len(r))
    return r


def file_rec(cnid, mtime, size, extents, mode=0o100644):
    r = struct.pack(">HHII", 2, 0, 0, cnid)
    r += struct.pack(">IIIII", mtime, mtime, mtime, mtime, 0)
    r += struct.pack(">IIBBHI", 0, 0, 0, 0, mode, 0)           # BSDInfo
    r += b"\x00" * 32                                          # userInfo + finderInfo
    r += struct.pack(">II", 0, 0)                              # encoding, reserved
    r += fork_data(size, extents)                              # data fork
    r += fork_data(0, [])                                      # resource fork
    return r


def thread_rec(parent, name):
    n = name.encode("utf-16-be")
    return struct.pack(">HHIH", 3, 0, parent, len(n) // 2) + n


def btree_node_header(root_node, node_size):
    """Узел 0: BTNodeDescriptor + BTHeaderRec (важные поля)."""
    nd = struct.pack(">IIbBHH", 0, 0, 1, 0, 3, 0)
    hdr = struct.pack(">HIIIIHH", 1, root_node, 3, root_node, root_node, node_size, 520)
    blk = bytearray(node_size)
    blk[:len(nd)] = nd
    blk[14:14 + len(hdr)] = hdr
    return bytes(blk)


def leaf_node(records, node_size):
    """Лист высоты 1 с записями [(key, value)]."""
    nd = struct.pack(">IIbBHH", 0, 0, -1, 1, len(records), 0)
    blk = bytearray(node_size)
    blk[:len(nd)] = nd
    pos = 14
    offsets = []
    for key, val in records:
        offsets.append(pos)
        blk[pos:pos + len(key)] = key
        pos += len(key)
        if pos & 1:
            pos += 1
        blk[pos:pos + len(val)] = val
        pos += len(val)
    offsets.append(pos)  # свободное место
    for i, off in enumerate(offsets):
        struct.pack_into(">H", blk, node_size - 2 * (i + 1), off)
    return bytes(blk)


def build_image(path):
    img = bytearray(30 * BS)

    # том: заголовок на 1024
    vh = bytearray(512)
    vh[0:2] = b"H+"
    struct.pack_into(">H", vh, 2, 4)
    struct.pack_into(">I", vh, 40, BS)
    struct.pack_into(">I", vh, 44, 30)
    vh[192:272] = fork_data(BS, [(12, 1)])       # extents tree
    vh[272:352] = fork_data(2 * BS, [(10, 2)])   # catalog
    img[1024:1024 + 512] = vh

    mt = HFS_EPOCH + 1_700_000_000

    records = [
        (cat_key(2, ""), thread_rec(1, "TestDisk")),                       # имя тома
        (cat_key(2, "docs"), folder_rec(20, mt)),
        (cat_key(2, "hello.txt"), file_rec(21, mt, 6, [(20, 1)])),
        (cat_key(20, "тест 🌲.txt"), file_rec(22, mt, 12, [(21, 1)])),
    ]
    img[10 * BS:11 * BS] = btree_node_header(1, BS)
    img[11 * BS:12 * BS] = leaf_node(records, BS)
    img[12 * BS:13 * BS] = btree_node_header(0, BS)  # пустое дерево экстентов

    img[20 * BS:20 * BS + 6] = b"yolka!"
    img[21 * BS:21 * BS + 12] = "ёлка ok 🌲".encode("utf-8")[:12]

    with open(path, "wb") as f:
        f.write(img)


def main():
    path = os.path.join(tempfile.gettempdir(), "yolka_test_hfs.img")
    build_image(path)
    vol = open_volume(RawDevice(path))

    assert vol.name == "TestDisk", vol.name
    root = vol.list_dir(vol.ROOT_ID)
    assert set(root) == {"docs", "hello.txt"}, root

    assert vol.read_file(root["hello.txt"]) == b"yolka!"

    fid = vol.resolve_path("docs/тест 🌲.txt")
    data = vol.read_file(fid)
    assert data == "ёлка ok 🌲".encode("utf-8")[:12], data

    ino = vol.get_inode(root["hello.txt"])
    assert ino["size"] == 6 and (ino["mode"] & 0o170000) == 0o100000

    print("HFS+: все проверки пройдены ✔")
    vol.d.dev.f.close()
    os.remove(path)


if __name__ == "__main__":
    main()
