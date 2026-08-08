from .apfs import RawDevice, ServerDevice, Volume, find_apfs_partition
from .hfsplus import HFSVolume, find_hfs_partition

__all__ = ["RawDevice", "ServerDevice", "Volume", "HFSVolume",
           "find_apfs_partition", "find_hfs_partition",
           "open_volume", "list_volumes"]


def open_volume(dev, vol=0):
    """Определяет формат (APFS или HFS+) и открывает том №vol."""
    try:
        return Volume(dev, find_apfs_partition(dev), vol_index=vol)
    except Exception as apfs_err:
        try:
            v = HFSVolume(dev, find_hfs_partition(dev))
            if vol:
                raise IOError(f"тома №{vol} нет: на HFS+ том один (№0)")
            return v
        except Exception as hfs_err:
            raise IOError(
                f"не удалось открыть ни как APFS ({apfs_err}), "
                f"ни как HFS+ ({hfs_err})")


def list_volumes(dev):
    """Имена всех томов на диске: [(номер, имя, формат)]."""
    try:
        off = find_apfs_partition(dev)
        first = Volume(dev, off, 0)
        out = [(0, first.name, "APFS")]
        for i in range(1, first.volume_count):
            try:
                out.append((i, Volume(dev, off, i).name, "APFS"))
            except Exception as ex:
                out.append((i, f"<не открылся: {ex}>", "APFS"))
        return out
    except Exception:
        pass
    v = HFSVolume(dev, find_hfs_partition(dev))
    return [(0, v.name, "HFS+")]
