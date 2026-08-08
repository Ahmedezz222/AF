# 🌲 yolka

**Read-only APFS driver for Windows in pure Python — no kernel drivers, no dependencies.**

Mounts Apple File System disks (including Time Machine backups) as a regular
Windows drive letter, using the WebDAV client built into Windows itself.

[Читать по-русски →](README.ru.md)

## The story

A commercial APFS driver refused to open a folder from a Time Machine backup —
it choked on an emoji (`🌲`) in the folder name, a surrogate-pair bug in its
filename handling. The data inside was a Telegram content pipeline that needed
rescuing. So this project reads the file system directly from the raw disk,
bypassing the driver entirely. The spruce that broke everything gave the
project its name.

## What it does

- parses APFS from raw bytes: GPT, checkpoints, object maps, B-trees;
- finds the latest valid checkpoint (survives an unclean unmount);
- mounts the volume as a Windows drive letter via built-in WebDAV — browse
  in Explorer, open files in any app;
- CLI for scripted use: `ls`, `extract`, `info`;
- handles any filename: emoji, Cyrillic, surrogate pairs — the original bug
  that started it all;
- decompresses decmpfs/zlib files;
- **read-only by design** — it cannot damage your disk, period.

## Architecture

```
┌─ Explorer / any app
│      ↓  (drive letter, e.g. Y:)
├─ Windows WebDAV client        ← built into Windows, no install
│      ↓  HTTP on 127.0.0.1
├─ yolka.dav                    ← WebDAV server, ~200 lines, stdlib only
│      ↓
├─ yolka.apfs                   ← APFS parser: checkpoints, omaps, B-trees
│      ↓
├─ yolka.server                 ← raw-read server (one UAC prompt, then
│      ↓                          everything runs unprivileged)
└─ \\.\PhysicalDriveN           ← the actual disk
```

The elevation split matters: raw disk access on Windows requires administrator
rights, but nobody wants to run a file server elevated. `yolka.server` is a
50-line read-only proxy that takes the single UAC prompt; everything else runs
as a normal user.

## Usage

```bash
# 1. one UAC prompt (raw disk access):
python -m yolka.server \\.\PhysicalDrive1

# 2. as a normal user — mount as a drive:
python -m yolka.dav
net use Y: \\127.0.0.1@8809\DavWWWRoot

# ...or use the CLI without mounting:
python -m yolka info
python -m yolka ls "backup/Users/me/Documents"
python -m yolka extract "backup/Users/me/Documents/project" "C:\rescued"
```

Disk images need no elevation at all:

```bash
python -m yolka --image backup.img ls /
python -m yolka.dav --image backup.img
```

## Limitations

- no write support — intentional, forever;
- no FileVault (encrypted volumes);
- no lzvn/lzfse decompression yet (rare for user files);
- reads the current volume state (no snapshot selection yet).

## Roadmap

- [ ] lzfse/lzvn via pyliblzfse
- [ ] snapshot selection
- [ ] WinFsp backend as an alternative to WebDAV
- [ ] GUI: folder tree + extract button

## License

MIT
