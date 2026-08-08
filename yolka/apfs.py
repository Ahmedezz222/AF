# -*- coding: utf-8 -*-
"""Минимальный читатель APFS (только чтение).

Работает поверх любого источника байтов: файла-образа, raw-устройства
(нужны права администратора) или сервера чтения (yolka.server).
Проверен на бэкапе Time Machine, который драйвер Paragon APFS
отказался открывать из-за эмодзи в имени папки.
"""
import io
import socket
import struct
import os
import zlib

BS = 4096  # размер блока APFS (читается из суперблока при открытии)
APFS_TYPE_GUID = bytes.fromhex("ef57347c0000aa11aa1100306543ecac")


def u16(b, o): return struct.unpack_from("<H", b, o)[0]
def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def u64(b, o): return struct.unpack_from("<Q", b, o)[0]
def i64(b, o): return struct.unpack_from("<q", b, o)[0]


class RawDevice:
    """Прямое чтение файла/устройства с выравниванием по секторам."""

    SECTOR = 4096

    def __init__(self, path):
        self.f = open(path, "rb", buffering=0)

    def read(self, off, size):
        a_start = (off // self.SECTOR) * self.SECTOR
        a_end = ((off + size + self.SECTOR - 1) // self.SECTOR) * self.SECTOR
        self.f.seek(a_start)
        data = self.f.read(a_end - a_start)
        return data[off - a_start: off - a_start + size]


class ServerDevice:
    """Клиент к yolka.server (когда прав администратора нет у текущего процесса)."""

    def __init__(self, host="127.0.0.1", port=47831):
        self.s = socket.create_connection((host, port))

    def read(self, off, size):
        self.s.sendall(struct.pack("<QQ", off, size))
        n = struct.unpack("<Q", self._recv(8))[0]
        return self._recv(n)

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            p = self.s.recv(n - len(buf))
            if not p:
                raise IOError("сервер закрыл соединение")
            buf += p
        return buf


class CachedSlice:
    """Срез устройства от начала раздела + кэш мелких чтений."""

    def __init__(self, dev, base):
        self.dev, self.base = dev, base
        self.cache = {}

    def read(self, off, size):
        key = (off, size)
        if key in self.cache:
            return self.cache[key]
        data = self.dev.read(self.base + off, size)
        if size <= BS * 4 and len(self.cache) < 8192:
            self.cache[key] = data
        return data

    def block(self, bno):
        return self.read(bno * BS, BS)


def find_apfs_partition(dev):
    """Ищет APFS-раздел в GPT. Возвращает смещение в байтах (или 0, если это уже раздел)."""
    head = dev.read(0, BS)
    if head[32:36] == b"NXSB":
        return 0
    hdr = dev.read(512, 512)
    if hdr[:8] != b"EFI PART":
        raise IOError("не найден ни APFS-суперблок, ни GPT")
    part_lba = struct.unpack_from("<q", hdr, 72)[0]
    nparts = struct.unpack_from("<I", hdr, 80)[0]
    entsz = struct.unpack_from("<I", hdr, 84)[0]
    table = dev.read(part_lba * 512, nparts * entsz)
    for i in range(nparts):
        e = table[i * entsz:(i + 1) * entsz]
        if e[:16] == APFS_TYPE_GUID:
            first = struct.unpack_from("<q", e, 32)[0]
            return first * 512
    raise IOError("APFS-раздел в GPT не найден")


def fletcher_ok(blk):
    sum1 = sum2 = 0
    for (w,) in struct.iter_unpack("<I", b"\x00" * 8 + blk[8:]):
        sum1 = (sum1 + w) % 0xFFFFFFFF
        sum2 = (sum2 + sum1) % 0xFFFFFFFF
    ck_low = 0xFFFFFFFF - ((sum1 + sum2) % 0xFFFFFFFF)
    ck_high = 0xFFFFFFFF - ((sum1 + ck_low) % 0xFFFFFFFF)
    return blk[:8] == struct.pack("<II", ck_low, ck_high)


BTNODE_ROOT, BTNODE_LEAF, BTNODE_FIXED = 1, 2, 4
ROOT_DIR_ID = 2

J_INODE, J_XATTR, J_FILE_EXTENT, J_DREC = 3, 4, 8, 9


class Volume:
    """Один том APFS-контейнера, открытый на последней контрольной точке."""

    def __init__(self, dev, part_offset=None):
        if part_offset is None:
            part_offset = find_apfs_partition(dev)
        self.d = CachedSlice(dev, part_offset)

        b0 = self.d.block(0)
        if b0[32:36] != b"NXSB":
            raise IOError("нет суперблока NXSB — это не APFS-раздел")
        global BS
        BS = u32(b0, 0x24)

        # свежайшая валидная контрольная точка
        xp_desc_blocks = u32(b0, 0x68) & 0x7FFFFFFF
        xp_desc_base = i64(b0, 0x70)
        self.xid, nxsb = u64(b0, 0x10), b0
        for i in range(xp_desc_blocks):
            blk = self.d.block(xp_desc_base + i)
            if blk[32:36] == b"NXSB" and fletcher_ok(blk):
                x = u64(blk, 0x10)
                if x >= self.xid:
                    self.xid, nxsb = x, blk

        self.nx_omap_oid = u64(nxsb, 0xA0)
        fs_oid = next((u64(nxsb, 0xB8 + i * 8) for i in range(100)
                       if u64(nxsb, 0xB8 + i * 8)), 0)
        if fs_oid == 0:
            # берём первый оид из омапа контейнера
            om = self.d.block(self.nx_omap_oid)
            root = self.d.block(u64(om, 0x30))
            _, _, entries = self._node(root)
            fs_oid = u64(root, entries[0][0])

        apsb_paddr = self._omap_lookup(self.nx_omap_oid, fs_oid)
        apsb = self.d.block(apsb_paddr)
        if apsb[32:36] != b"APSB":
            raise IOError("не найден суперблок тома APSB")
        self.vol_omap_oid = u64(apsb, 0x80)
        self.root_tree_oid = u64(apsb, 0x88)
        self.name = apsb[0x2C0:0x2C0 + 256].split(b"\x00")[0].decode("utf-8", "replace")

    # --- b-деревья ---

    def _node(self, blk):
        flags, level, nkeys = u16(blk, 0x20), u16(blk, 0x22), u32(blk, 0x24)
        t_off, t_len = u16(blk, 0x28), u16(blk, 0x2A)
        toc = 0x38 + t_off
        keybase = 0x38 + t_off + t_len
        valend = BS - (0x28 if flags & BTNODE_ROOT else 0)
        entries = []
        for i in range(nkeys):
            if flags & BTNODE_FIXED:
                ko, vo = u16(blk, toc + i * 4), u16(blk, toc + i * 4 + 2)
                entries.append((keybase + ko, None,
                                valend - vo if vo != 0xFFFF else None, None))
            else:
                ko, kl, vo, vl = (u16(blk, toc + i * 8), u16(blk, toc + i * 8 + 2),
                                  u16(blk, toc + i * 8 + 4), u16(blk, toc + i * 8 + 6))
                entries.append((keybase + ko, kl,
                                valend - vo if vo != 0xFFFF else None, vl))
        return flags, level, entries

    def _omap_lookup(self, omap_oid, want_oid):
        om = self.d.block(omap_oid)
        bno = u64(om, 0x30)
        while True:
            blk = self.d.block(bno)
            _, level, entries = self._node(blk)
            best = None
            for (ko, _kl, vo, _vl) in entries:
                oid, xid = u64(blk, ko), u64(blk, ko + 8)
                if oid < want_oid or (oid == want_oid and xid <= self.xid):
                    best = (oid, xid, vo)
                else:
                    break
            if best is None:
                return None
            if level == 0:
                oid, _xid, vo = best
                if oid != want_oid:
                    return None
                return u64(blk, vo + 8)
            bno = u64(blk, best[2])

    def _fs_node(self, oid):
        paddr = self._omap_lookup(self.vol_omap_oid, oid)
        if paddr is None:
            raise IOError(f"омап не знает fs-оид {oid:#x}")
        return self.d.block(paddr)

    @staticmethod
    def _key_head(blk, ko):
        v = u64(blk, ko)
        return v & 0x0FFFFFFFFFFFFFFF, v >> 60

    def _fs_range(self, obj_id, jtype):
        """Все листовые записи с данным (obj_id, jtype)."""
        def walk(oid):
            blk = self._fs_node(oid)
            _, level, entries = self._node(blk)
            if level == 0:
                for (ko, kl, vo, vl) in entries:
                    oid_, t = self._key_head(blk, ko)
                    if oid_ == obj_id and t == jtype:
                        yield (blk, ko, kl, vo, vl)
                return
            n = len(entries)
            want = (obj_id, jtype)
            for i, (ko, _kl, vo, _vl) in enumerate(entries):
                lo = self._key_head(blk, ko)
                hi = self._key_head(blk, entries[i + 1][0]) if i + 1 < n else None
                if hi is not None and hi < want:  # граница включительно!
                    continue
                if lo > want:
                    break
                yield from walk(u64(blk, vo))
        yield from walk(self.root_tree_oid)

    # --- файловые операции ---

    def list_dir(self, dir_id):
        out = {}
        for (blk, ko, _kl, vo, _vl) in self._fs_range(dir_id, J_DREC):
            nl = u32(blk, ko + 8) & 0x3FF
            name = blk[ko + 12: ko + 12 + nl - 1].decode("utf-8", "replace")
            out[name] = u64(blk, vo)
        return out

    def get_inode(self, fid):
        for (blk, _ko, _kl, vo, vl) in self._fs_range(fid, J_INODE):
            v = blk[vo: vo + (vl or 92)]
            size = None
            if vl and vl > 92:
                xf = v[92:]
                if len(xf) >= 4:
                    xnum = u16(xf, 0)
                    dpos = 4 + xnum * 4
                    for i in range(xnum):
                        xtype, xsz = xf[4 + i * 4], u16(xf, 6 + i * 4)
                        if xtype == 8:  # INO_EXT_TYPE_DSTREAM
                            size = u64(xf, dpos)
                            break
                        dpos += (xsz + 7) & ~7
            return {"parent": u64(v, 0), "private_id": u64(v, 8),
                    "mode": u16(v, 80), "bsd_flags": u32(v, 68), "size": size,
                    "ctime": u64(v, 16) / 1e9, "mtime": u64(v, 24) / 1e9}
        return None

    def _read_stream(self, stream_id, size):
        exts = []
        for (blk, ko, _kl, vo, _vl) in self._fs_range(stream_id, J_FILE_EXTENT):
            logical = u64(blk, ko + 8)
            ln = u64(blk, vo) & 0x00FFFFFFFFFFFFFF
            phys = u64(blk, vo + 8)
            exts.append((logical, ln, phys))
        exts.sort()
        out = bytearray(size)
        step = 8 * 1024 * 1024
        for logical, ln, phys in exts:
            want = min(ln, max(0, size - logical))
            pos = 0
            while pos < want:
                chunk = self.d.read(phys * BS + pos, min(step, want - pos))
                out[logical + pos: logical + pos + len(chunk)] = chunk
                pos += len(chunk)
        return bytes(out)

    def get_xattr(self, fid, name):
        for (blk, ko, _kl, vo, _vl) in self._fs_range(fid, J_XATTR):
            nl = u16(blk, ko + 8)
            if blk[ko + 10: ko + 10 + nl - 1].decode("utf-8", "replace") != name:
                continue
            flags, xdlen = u16(blk, vo), u16(blk, vo + 2)
            if flags & 2:  # данные прямо в записи
                return blk[vo + 4: vo + 4 + xdlen]
            if flags & 1:  # отдельный поток
                xid_ = u64(blk, vo + 4)
                dsize = u64(blk, vo + 12 + 8)
                return self._read_stream(xid_, dsize)
        return None

    def read_file(self, fid):
        ino = self.get_inode(fid)
        if ino is None:
            raise IOError(f"нет инода {fid}")
        if ino["bsd_flags"] & 0x20:  # UF_COMPRESSED → decmpfs
            d = self.get_xattr(fid, "com.apple.decmpfs")
            if d is None or d[:4] != b"fpmc":
                raise IOError("файл сжат, но заголовок decmpfs не найден")
            ctype = u32(d, 4)
            payload = d[16:]
            if ctype in (3, 4):  # zlib
                if payload[:1] == b"\xff":
                    return payload[1:]
                return zlib.decompress(payload)
            raise IOError(f"тип сжатия decmpfs {ctype} пока не поддержан (lzvn/lzfse)")
        size = ino["size"] or 0
        return self._read_stream(ino["private_id"], size) if size else b""

    def resolve_path(self, path):
        cur = ROOT_DIR_ID
        for part in [p for p in path.replace("\\", "/").split("/") if p]:
            entries = self.list_dir(cur)
            if part not in entries:
                raise IOError(f"не найдено: {part!r} (в каталоге: {sorted(entries)[:15]}…)")
            cur = entries[part]
        return cur

    def extract(self, fid, dest, log=print):
        """Рекурсивно извлекает каталог (или один файл) в dest."""
        ino = self.get_inode(fid)
        if ino and (ino["mode"] & 0o170000) == 0o100000:
            data = self.read_file(fid)
            with open(dest, "wb") as w:
                w.write(data)
            log(f"  f {dest} ({len(data)} б)")
            return
        os.makedirs(dest, exist_ok=True)
        for name, child in sorted(self.list_dir(fid).items()):
            cino = self.get_inode(child)
            out = os.path.join(dest, name)
            if cino is None:
                log(f"  ! {name}: нет инода")
                continue
            ftype = cino["mode"] & 0o170000
            if ftype == 0o040000:
                self.extract(child, out, log)
            elif ftype == 0o100000:
                try:
                    data = self.read_file(child)
                    with open(out, "wb") as w:
                        w.write(data)
                    log(f"  f {out} ({len(data)} б)")
                except Exception as ex:
                    log(f"  ! {out}: {ex}")
            else:
                log(f"  s {name} (режим {oct(cino['mode'])}, пропущен)")
