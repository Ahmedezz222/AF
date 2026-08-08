# How one spruce tree broke a paid driver

[Читать по-русски →](STORY.ru.md)

## The problem

I was moving from a Mac to Windows. All my data lived on an external drive —
a Time Machine backup formatted in APFS, macOS's native file system. Windows
can't read those out of the box, so I installed Paragon APFS for Windows —
a commercial driver ($49.95 on the official site today), the standard
"just works" solution. And since older Mac disks can be HFS+ instead, Paragon
sells that as a separate product for separate money — so I ended up with two
paid drivers instead of one.

Almost everything opened. Except one folder — and, of course, the one with
the files I needed most. Explorer stumbled, the driver went silent.

The reason was absurd: the folder was named **`🌲 Workspace`** — with a
spruce-tree emoji. The Paragon driver couldn't handle an emoji in the name
(technically, Unicode "surrogate pairs") and simply refused to enter it.
One little picture in a folder name, and a paid driver was helpless while
the data sat locked away.

Yes, by the classic rules of good taste, an emoji in a folder name is a
"don't" — you never know what program the file will pass through later.
I knew that. But macOS supports such names perfectly, and it had all worked
for years — so why not? The little spruce was nice to look at and bothered
no one. Until a driver that wasn't ready for a perfectly valid name ended up
in the chain. Exactly what that rule warns about — and a live proof that the
warning isn't idle.

## The journey

If the driver stumbles on the name, then the disk has to be read **around
the driver** — directly, at the byte level. APFS has an open specification;
its structures can be parsed by hand.

It turned out to be a one-day road, but with a few forks:

1. **Reach the disk.** Windows won't let you read a physical disk without
   administrator rights. So I built a tiny read server that takes the single
   elevation prompt, while everything else runs as a normal user.

2. **Parse APFS by hand.**

3. **Get the data out.** The spruce-tree folder opened. Everything that had
   been locked away was back on the computer.

## The solution

Then it became clear that the rescue operation had produced a tool people
are missing. That's how **yolka** 🌲 was born — a read-only Mac-disk driver
for Windows:

- reads **both** of Apple's file systems — APFS (new) and HFS+ (old);
- mounts the disk as a **drive letter in Explorer** via a mechanism built
  into Windows — you browse a Mac disk with the mouse like any USB stick;
- **pure Python, zero dependencies**, no kernel drivers;
- **read-only** — it cannot damage the disk, by design;
- names with emoji, Cyrillic, anything — the spruce that started it all
  breaks nothing anymore.

Validated on two real disks: an APFS Time Machine backup and a 1 TB HFS+ drive.

The niche turned out to be surprisingly empty: there was no free tool that
reads **both** formats on Windows and mounts them as a drive letter — only
the paid Paragon and MacDrive. Now there is.

The spruce tree that started it all gave the project its name — *yolka*
(ёлка) is Russian for a spruce, the Christmas tree.
