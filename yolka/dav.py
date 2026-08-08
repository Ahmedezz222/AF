# -*- coding: utf-8 -*-
"""WebDAV-сервер (только чтение) поверх тома APFS.

Windows умеет монтировать WebDAV штатно, без сторонних драйверов:

    python -m yolka.dav              # поднять сервер на 127.0.0.1:8809
    net use Y: \\\\127.0.0.1@8809\\DavWWWRoot   # подключить как диск Y:

или в Проводнике: «Подключить сетевой диск» → http://127.0.0.1:8809/

Нулевые зависимости: только стандартная библиотека.
"""
import argparse
import email.utils
import html
import posixpath
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

from . import open_volume
from .apfs import RawDevice, ServerDevice

DIR = 0o040000
REG = 0o100000


class DavState:
    """Кэш дерева: путь → (fid, inode). WebDAV-клиент долбит PROPFIND очень часто."""

    def __init__(self, volume):
        self.vol = volume
        self.lock = threading.Lock()
        self.dirs = {}     # path -> {name: fid}
        self.inodes = {}   # fid -> inode dict
        self.files = {}    # fid -> bytes (маленький LRU-подобный кэш содержимого)

    def inode(self, fid):
        with self.lock:
            if fid not in self.inodes:
                self.inodes[fid] = self.vol.get_inode(fid)
            return self.inodes[fid]

    def listing(self, path, fid):
        with self.lock:
            if path not in self.dirs:
                self.dirs[path] = self.vol.list_dir(fid)
            return self.dirs[path]

    def resolve(self, path):
        """path ('/a/b') → (fid, inode) либо KeyError."""
        cur = self.vol.ROOT_ID
        walked = ""
        for part in [p for p in path.split("/") if p]:
            entries = self.listing(walked or "/", cur)
            if part not in entries:
                raise KeyError(path)
            cur = entries[part]
            walked = walked + "/" + part
        return cur, self.inode(cur)

    def content(self, fid):
        with self.lock:
            if fid in self.files:
                return self.files[fid]
        data = self.vol.read_file(fid)
        with self.lock:
            if len(data) <= 32 * 1024 * 1024 and len(self.files) < 64:
                self.files[fid] = data
        return data


def http_date(ts):
    try:
        return email.utils.formatdate(ts, usegmt=True)
    except Exception:
        return email.utils.formatdate(0, usegmt=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "yolka-dav/1.0"
    state = None  # заполняется в serve()

    def log_message(self, fmt, *args):
        pass

    # --- служебное ---

    def _path(self):
        p = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        return "/" + posixpath.normpath("/" + p).lstrip("/")

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("DAV", "1, 2")
        self.send_header("MS-Author-Via", "DAV")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    # --- методы WebDAV ---

    def do_OPTIONS(self):
        self._send(200, extra={"Allow": "OPTIONS, GET, HEAD, PROPFIND"})

    def _propfind_entry(self, href, name, ino):
        isdir = ino and (ino["mode"] & 0o170000) == DIR
        size = 0 if isdir else ((ino or {}).get("size") or 0)
        mtime = http_date((ino or {}).get("mtime", 0))
        restype = "<D:collection/>" if isdir else ""
        if isdir and not href.endswith("/"):
            href += "/"
        return (f"<D:response><D:href>{escape(href)}</D:href>"
                f"<D:propstat><D:prop>"
                f"<D:displayname>{escape(name)}</D:displayname>"
                f"<D:resourcetype>{restype}</D:resourcetype>"
                f"<D:getcontentlength>{size}</D:getcontentlength>"
                f"<D:getlastmodified>{mtime}</D:getlastmodified>"
                f"</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
                f"</D:response>")

    def do_PROPFIND(self):
        # тело запроса читаем и игнорируем: отдаём стандартный набор свойств
        ln = int(self.headers.get("Content-Length") or 0)
        if ln:
            self.rfile.read(ln)
        path = self._path()
        depth = self.headers.get("Depth", "1")
        try:
            fid, ino = self.state.resolve(path)
        except KeyError:
            self._send(404)
            return
        quote = urllib.parse.quote
        parts = [self._propfind_entry(quote(path), posixpath.basename(path) or "/", ino)]
        if depth != "0" and (ino["mode"] & 0o170000) == DIR:
            for name, child in sorted(self.state.listing(path, fid).items()):
                cino = self.state.inode(child)
                href = quote(posixpath.join(path, name))
                parts.append(self._propfind_entry(href, name, cino))
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<D:multistatus xmlns:D="DAV:">' + "".join(parts) +
                "</D:multistatus>").encode("utf-8")
        self._send(207, body, 'application/xml; charset="utf-8"')

    def do_GET(self):
        path = self._path()
        try:
            fid, ino = self.state.resolve(path)
        except KeyError:
            self._send(404)
            return
        if (ino["mode"] & 0o170000) == DIR:
            # простенький HTML-листинг: удобно смотреть в браузере
            rows = []
            for name, child in sorted(self.state.listing(path, fid).items()):
                cino = self.state.inode(child)
                isdir = cino and (cino["mode"] & 0o170000) == DIR
                href = urllib.parse.quote(posixpath.join(path, name))
                label = html.escape(name) + ("/" if isdir else "")
                size = "" if isdir else f" — {((cino or {}).get('size') or 0):,} б"
                rows.append(f'<li><a href="{href}">{label}</a>{size}</li>')
            body = (f"<!doctype html><meta charset=utf-8><title>{html.escape(path)}</title>"
                    f"<h3>🌲 {html.escape(path)}</h3><ul>" + "".join(rows) + "</ul>").encode()
            self._send(200, body, "text/html; charset=utf-8")
            return
        try:
            data = self.state.content(fid)
        except Exception as ex:
            self._send(500, str(ex).encode())
            return
        self._send(200, data, "application/octet-stream",
                   extra={"Last-Modified": http_date(ino.get("mtime", 0))})

    do_HEAD = do_GET


def serve(volume, host="127.0.0.1", port=8809):
    Handler.state = DavState(volume)
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"yolka-dav: том {volume.name!r} на http://{host}:{port}/")
    print(f"подключить диском:  net use Y: \\\\{host}@{port}\\DavWWWRoot")
    srv.serve_forever()


def main():
    p = argparse.ArgumentParser(prog="yolka.dav", description="WebDAV поверх APFS (чтение)")
    p.add_argument("--image", help="путь к образу диска")
    p.add_argument("--device", help=r"raw-устройство (нужен администратор)")
    p.add_argument("--raw-port", type=int, default=47831, help="порт yolka-сервера чтения")
    p.add_argument("--port", type=int, default=8809, help="порт WebDAV (по умолчанию 8809)")
    args = p.parse_args()
    if args.image:
        dev = RawDevice(args.image)
    elif args.device:
        dev = RawDevice(args.device)
    else:
        dev = ServerDevice(port=args.raw_port)
    serve(open_volume(dev), port=args.port)


if __name__ == "__main__":
    main()
