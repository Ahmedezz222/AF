from .apfs import RawDevice, ServerDevice, Volume, find_apfs_partition
from .hfsplus import HFSVolume, find_hfs_partition

__all__ = ["RawDevice", "ServerDevice", "Volume", "HFSVolume",
           "find_apfs_partition", "find_hfs_partition", "open_volume"]


def open_volume(dev):
    """Определяет формат (APFS или HFS+) и открывает том."""
    try:
        return Volume(dev, find_apfs_partition(dev))
    except Exception as apfs_err:
        try:
            return HFSVolume(dev, find_hfs_partition(dev))
        except Exception as hfs_err:
            raise IOError(
                f"не удалось открыть ни как APFS ({apfs_err}), "
                f"ни как HFS+ ({hfs_err})")
