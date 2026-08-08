# -*- coding: utf-8 -*-
"""Минимальный читатель HFS+ (Mac OS Extended, только чтение).

Старший брат APFS: использовался на маках до 2017 года и на многих
внешних дисках и бэкапах Time Machine той эпохи. Всё big-endian,
каталог — один большой B-tree-файл.
"""
import struct
import unicodedata
import zlib
import os

HFS_EPOCH = 2082844800  # 1904-01-01 → 1970-01-01, секунды
ROOT_ID = 2  # kHFSRootFolderID — совпадает с APFS, удобно

HFSPLUS_TYPE_GUID = bytes.fromhex("0053464800000011aa1100306543ecac")


def be16(b, o): return struct.unpack_from(">H", b, o)[0]
def be32(b, o): return struct.unpack_from(">I", b, o)[0]
def be64(b, o): return struct.unpack_from(">Q", b, o)[0]


class Fork:
    """Данные файла: логический размер + список экстентов."""

    def __init__(self, vol, raw, file_id, fork_type=0):
        self.vol = vol
        self.size = be64(raw, 0)
        self.file_id = file_id
        self.fork_type = fork_type
        self.extents = []
        blocks = 0
        for i in range(8):
            start, cnt = be32(raw, 16 + i * 8), be32(raw, 20 + i * 8)
            if cnt:
                self.extents.append((blocks, start, cnt))
                blocks += cnt
        self._complete = blocks * vol.bs >= self.size

    def _all_extents(self):
        if self._complete:
            return self.extents
        exts = list(self.extents)
        blocks = sum(e[2] for e in exts)
        while blocks * self.vol.bs < self.size:
            more = self.vol._overflow_extents(self.file_id, blocks, self.fork_type)
            if not more:
                break
            for start, cnt in more:
                if cnt:
                    exts.append((blocks, start, cnt))
                    blocks += cnt
        return exts

    def read(self, off, size):
        size = max(0, min(size, self.size - off))
        out = bytearray(size)
        for logical_blk, start, cnt in self._all_extents():
            e_off = logical_blk * self.vol.bs
            e_len = cnt * self.vol.bs
            lo = max(off, e_off)
            hi = min(off + size, e_off + e_len)
            if lo < hi:
                data = self.vol.d.read(start * self.vol.bs + (lo - e_off), hi - lo)
                out[lo - off: lo - off + len(data)] = data
        return bytes(out)

    def read_all(self):
        return self.read(0, self.size)


class BTree:
    """B-дерево HFS+ (каталог, экстенты, атрибуты)."""

    def __init__(self, fork):
        self.fork = fork
        hdr = fork.read(0, 512)
        # BTNodeDescriptor(14) + BTHeaderRec
        self.root = be32(hdr, 14 + 2)
        self.node_size = be16(hdr, 14 + 18)

    def node(self, n):
        return self.fork.read(n * self.node_size, self.node_size)

    def records(self, blk):
        """[(key_off, key_len, val_off)] записей узла."""
        nrec = be16(blk, 10)
        out = []
        for i in range(nrec):
            off = be16(blk, self.node_size - 2 * (i + 1))
            klen = be16(blk, off)
            out.append((off + 2, klen, off + 2 + klen + (klen & 1)))
        return out

    def level(self, blk):
        return blk[8]  # kind: -1 leaf, 0 index... сравниваем через height
    # высота: leaf=1


def _cat_key_parent(blk, ko):
    return be32(blk, ko)


def _cat_key_name(blk, ko, klen):
    nlen = be16(blk, ko + 4)
    raw = blk[ko + 6: ko + 6 + nlen * 2]
    return raw.decode("utf-16-be", "replace")


class HFSVolume:
    """Том HFS+/HFSX, открытый на чтение. Интерфейс совместим с apfs.Volume."""

    ROOT_ID = ROOT_ID

    def __init__(self, dev, part_offset=None):
        from .apfs import CachedSlice
        if part_offset is None:
            part_offset = find_hfs_partition(dev)
        self.d = CachedSlice(dev, part_offset)

        vh = self.d.read(1024, 512)
        if vh[0:2] not in (b"H+", b"HX"):
            raise IOError("нет заголовка HFS+ (H+/HX)")
        self.case_sensitive = vh[0:2] == b"HX"
        self.bs = be32(vh, 40)
        self.name = "HFS+"
        self._inodes = {}

        self.extents_tree = BTree(Fork(self, vh[192:272], 3))
        self.catalog = BTree(Fork(self, vh[272:352], 4))
        try:
            self.attributes = BTree(Fork(self, vh[352:432], 8)) \
                if be64(vh, 352) else None
        except Exception:
            self.attributes = None

        # имя тома лежит в thread-записи корня
        try:
            for rec in self._cat_records(ROOT_ID):
                if rec["kind"] == "thread":
                    self.name = rec["name"] or "HFS+"
                    break
        except Exception:
            pass

    # --- обход каталога ---

    def _cat_records(self, parent_id):
        """Все записи каталога с key.parentID == parent_id."""
        bt = self.catalog

        def walk(node_no):
            blk = bt.node(node_no)
            height = blk[9]
            recs = bt.records(blk)
            if height == 1:  # лист
                for (ko, kl, vo) in recs:
                    if _cat_key_parent(blk, ko) != parent_id:
                        continue
                    yield self._parse_leaf(blk, ko, kl, vo)
                return
            n = len(recs)
            for i, (ko, kl, vo) in enumerate(recs):
                lo = _cat_key_parent(blk, ko)
                hi = _cat_key_parent(blk, recs[i + 1][0]) if i + 1 < n else None
                if hi is not None and hi < parent_id:
                    continue
                if lo > parent_id:
                    break
                yield from walk(be32(blk, vo))

        yield from walk(bt.root)

    def _parse_leaf(self, blk, ko, kl, vo):
        name = unicodedata.normalize("NFC", _cat_key_name(blk, ko, kl))
        rtype = be16(blk, vo)
        if rtype == 1:  # папка
            cnid = be32(blk, vo + 8)
            self._inodes[cnid] = {
                "mode": 0o040755, "size": 0, "private_id": cnid,
                "mtime": be32(blk, vo + 16) - HFS_EPOCH,
                "ctime": be32(blk, vo + 12) - HFS_EPOCH,
                "bsd_flags": 0, "compressed": False,
            }
            return {"kind": "dir", "name": name, "cnid": cnid}
        if rtype == 2:  # файл
            cnid = be32(blk, vo + 8)
            mode = be16(blk, vo + 42) or 0o100644
            owner_flags = blk[vo + 41]
            data_fork = Fork(self, blk[vo + 88: vo + 168], cnid, 0)
            rsrc_fork = Fork(self, blk[vo + 168: vo + 248], cnid, 0xFF)
            self._inodes[cnid] = {
                "mode": mode, "size": data_fork.size, "private_id": cnid,
                "mtime": be32(blk, vo + 16) - HFS_EPOCH,
                "ctime": be32(blk, vo + 12) - HFS_EPOCH,
                "bsd_flags": owner_flags, "compressed": bool(owner_flags & 0x20),
                "_data": data_fork, "_rsrc": rsrc_fork,
            }
            return {"kind": "file", "name": name, "cnid": cnid}
        if rtype in (3, 4):  # thread: (родитель, имя) самого каталога/файла
            return {"kind": "thread", "name": unicodedata.normalize(
                "NFC", _cat_key_name(blk, vo + 4, 0) if False else self._thread_name(blk, vo)),
                "cnid": None}
        return {"kind": "other", "name": name, "cnid": None}

    @staticmethod
    def _thread_name(blk, vo):
        nlen = be16(blk, vo + 8)
        return blk[vo + 10: vo + 10 + nlen * 2].decode("utf-16-be", "replace")

    # --- переполнение экстентов ---

    def _overflow_extents(self, file_id, start_block, fork_type=0):
        bt = self.extents_tree

        def key_tuple(blk, ko):
            return (blk[ko], be32(blk, ko + 2), be32(blk, ko + 6))  # forkType, fileID, startBlock

        want = (fork_type, file_id, start_block)

        def walk(node_no):
            blk = bt.node(node_no)
            height = blk[9]
            recs = bt.records(blk)
            if height == 1:
                for (ko, kl, vo) in recs:
                    if key_tuple(blk, ko) == want:
                        return [(be32(blk, vo + i * 8), be32(blk, vo + 4 + i * 8))
                                for i in range(8)]
                return None
            n = len(recs)
            for i, (ko, kl, vo) in enumerate(recs):
                lo = key_tuple(blk, ko)
                hi = key_tuple(blk, recs[i + 1][0]) if i + 1 < n else None
                if hi is not None and hi < want:
                    continue
                if lo > want:
                    break
                r = walk(be32(blk, vo))
                if r:
                    return r
            return None

        return walk(bt.root)

    # --- xattr (для decmpfs) ---

    def get_xattr(self, cnid, name):
        if self.attributes is None:
            return None
        bt = self.attributes
        want_name = name.encode("utf-16-be")

        def key_fields(blk, ko):
            fid = be32(blk, ko + 2)
            nlen = be16(blk, ko + 10)
            aname = blk[ko + 12: ko + 12 + nlen * 2]
            return fid, aname

        def walk(node_no):
            blk = bt.node(node_no)
            height = blk[9]
            recs = bt.records(blk)
            if height == 1:
                for (ko, kl, vo) in recs:
                    fid, aname = key_fields(blk, ko)
                    if fid == cnid and aname == want_name:
                        rtype = be32(blk, vo)
                        if rtype == 0x10:  # inline
                            asize = be32(blk, vo + 12)
                            return blk[vo + 16: vo + 16 + asize]
                return None
            n = len(recs)
            for i, (ko, kl, vo) in enumerate(recs):
                fid_lo, _ = key_fields(blk, ko)
                fid_hi = key_fields(blk, recs[i + 1][0])[0] if i + 1 < n else None
                if fid_hi is not None and fid_hi < cnid:
                    continue
                if fid_lo > cnid:
                    break
                r = walk(be32(blk, vo))
                if r is not None:
                    return r
            return None

        return walk(bt.root)

    # --- интерфейс, совместимый с apfs.Volume ---

    def list_dir(self, dir_id):
        out = {}
        for rec in self._cat_records(dir_id):
            if rec["kind"] in ("dir", "file"):
                out[rec["name"]] = rec["cnid"]
        return out

    def get_inode(self, fid):
        if fid in self._inodes:
            return self._inodes[fid]
        if fid == ROOT_ID:
            return {"mode": 0o040755, "size": 0, "mtime": 0, "ctime": 0,
                    "bsd_flags": 0, "compressed": False, "private_id": fid}
        # найдём через thread-запись: записи с parent==fid содержат thread
        for rec in self._cat_records(fid):
            pass  # _cat_records попутно наполняет кэш файлов/папок ниже fid
        return self._inodes.get(fid)

    def read_file(self, fid):
        ino = self.get_inode(fid)
        if ino is None or "_data" not in ino:
            raise IOError(f"нет файла с cnid {fid}")
        if ino["compressed"]:
            d = self.get_xattr(fid, "com.apple.decmpfs")
            if d is None or d[:4] != b"fpmc":
                raise IOError("файл сжат, но заголовок decmpfs не найден")
            ctype = struct.unpack_from("<I", d, 4)[0]
            payload = d[16:]
            if ctype == 3:  # zlib в самом атрибуте
                if payload[:1] == b"\xff":
                    return payload[1:]
                return zlib.decompress(payload)
            if ctype == 4:  # zlib в ресурсной ветке, кусками по 64К
                return self._read_rsrc_zlib(ino["_rsrc"])
            raise IOError(f"тип сжатия decmpfs {ctype} пока не поддержан")
        return ino["_data"].read_all()

    @staticmethod
    def _read_rsrc_zlib(rsrc_fork):
        raw = rsrc_fork.read_all()
        data_off = be32(raw, 0)
        blk = raw[data_off:]
        nchunks = struct.unpack_from("<I", blk, 4)[0]
        out = []
        for i in range(nchunks):
            off, ln = struct.unpack_from("<II", blk, 8 + i * 8)
            chunk = blk[4 + off: 4 + off + ln]
            out.append(chunk[1:] if chunk[:1] == b"\xff" else zlib.decompress(chunk))
        return b"".join(out)

    def resolve_path(self, path):
        cur = ROOT_ID
        for part in [p for p in path.replace("\\", "/").split("/") if p]:
            part = unicodedata.normalize("NFC", part)
            entries = self.list_dir(cur)
            if part not in entries:
                raise IOError(f"не найдено: {part!r} (в каталоге: {sorted(entries)[:15]}…)")
            cur = entries[part]
        return cur

    def extract(self, fid, dest, log=print):
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
                log(f"  ! {name}: нет записи")
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


def find_hfs_partition(dev):
    """Ищет HFS+-раздел: сам том или GPT."""
    vh = dev.read(1024, 2)
    if vh in (b"H+", b"HX"):
        return 0
    hdr = dev.read(512, 512)
    if hdr[:8] != b"EFI PART":
        raise IOError("не найден ни том HFS+, ни GPT")
    part_lba = struct.unpack_from("<q", hdr, 72)[0]
    nparts = struct.unpack_from("<I", hdr, 80)[0]
    entsz = struct.unpack_from("<I", hdr, 84)[0]
    table = dev.read(part_lba * 512, nparts * entsz)
    for i in range(nparts):
        e = table[i * entsz:(i + 1) * entsz]
        if e[:16] == HFSPLUS_TYPE_GUID:
            first = struct.unpack_from("<q", e, 32)[0]
            off = first * 512
            if dev.read(off + 1024, 2) in (b"H+", b"HX"):
                return off
    raise IOError("HFS+-раздел в GPT не найден")
