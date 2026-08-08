# Installing yolka

## Prerequisites

| What | Why | Where |
|---|---|---|
| Windows 10 or 11 | target system | — |
| Python 3.10+ | yolka is written in it | [python.org/downloads](https://www.python.org/downloads/) |
| Administrator rights | one UAC prompt for live-disk access | — |
| An APFS disk | a Mac drive or Time Machine backup to read | your external drive |

**No third-party packages are required** — yolka runs on the Python standard
library alone. No `pip install` of anything. No drivers (Paragon, MacDrive
etc.) needed either; if installed, they don't interfere.

Reading a **disk image** (.img/.dd file) requires no elevation at all.

## Step 1. Install Python

1. Download the installer from [python.org/downloads](https://www.python.org/downloads/).
2. **Check "Add python.exe to PATH"** at the bottom of the first screen.
3. Verify in a terminal:

```bash
python --version
```

## Step 2. Get yolka

```bash
git clone https://github.com/USERNAME/yolka.git
cd yolka
```

Or download the ZIP from GitHub (Code → Download ZIP) and open a terminal
in the extracted folder.

Optionally install as a package (adds `yolka` and `yolka-dav` commands):

```bash
pip install .
```

— not required though: everything works via `python -m yolka`.

## Step 3. Find your disk number

Plug in the Mac disk and run:

```bash
wmic diskdrive get index,model,size
```

Find your drive by model name and note its Index — e.g. `1` means
`PhysicalDrive1`.

## Step 4. Start the raw-read server (the one UAC prompt)

In a terminal **run as administrator**:

```bash
python -m yolka.server \\.\PhysicalDrive1
```

Leave the window open (the server exits itself after 3 idle hours). This is
the only place that needs elevation: a read-only proxy serving bytes on
localhost port 47831.

## Step 5a. Mount as a drive letter

In a normal (non-elevated) terminal:

```bash
python -m yolka.dav
```

And in a second terminal:

```bash
net use Y: \\127.0.0.1@8809\DavWWWRoot
```

Drive Y: appears in Explorer — browse and copy files; strictly read-only.

Unmount with `net use Y: /delete`.

## Step 5b. Or skip mounting — use the CLI

```bash
python -m yolka info
python -m yolka ls /
python -m yolka extract "backup/Data/Users/me/Documents/project" "C:\rescued"
```

Paths inside the disk use `/`; quote them if they contain spaces. Emoji and
non-Latin names work fine.

## Troubleshooting

**"The WebClient service is not started" on net use** — start it:
`net start WebClient` (elevated), then retry.

**Large files fail to copy from Y:** — the built-in Windows WebDAV client
caps files at 50 MB (an early-2000s safety valve: the client downloads each
file to a temp folder in full, and big files over slow networks used to hang
Explorer; the number was never revisited).

Option 1 — raise the cap to its maximum (elevated, once):

```bash
reg add "HKLM\SYSTEM\CurrentControlSet\Services\WebClient\Parameters" /v FileSizeLimitInBytes /t REG_DWORD /d 4294967295 /f
net stop WebClient
net start WebClient
```

The new ceiling is 4 GB — a hard limit of the client (the value is a 32-bit
DWORD; it physically cannot hold more).

Option 2 — any size, no system changes:

```bash
python -m yolka extract "path/to/big/file" "C:\rescued"
```

`extract` reads the disk directly, bypassing WebClient — no limits, and faster.

**"no NXSB superblock"** — wrong disk number, or the partition isn't APFS.
Re-check step 3.

**A file errors mentioning lzvn/lzfse** — it uses a compression method rare
for user files; support is on the roadmap.

**FileVault-encrypted volumes** are not supported yet.
