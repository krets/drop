"""Grandfather-father-son retention, decided from upload times alone (UTC), never from file names."""
from datetime import datetime


def select_keep(files: list[tuple[str, datetime]], daily: int, weekly: int, monthly: int, yearly: int) -> set[str]:
    """Ids to keep out of (id, uploaded_at) pairs.

    Each period keeps one file per calendar period, for the newest `n` periods that have any file:
    the last upload of each day, and the first upload of each week, month and year (so a monthly
    or yearly keeper stays the same file as time passes). The newest file is always kept.
    """
    files = sorted(files, key=lambda f: f[1])
    if not files:
        return set()
    keep = {files[-1][0]}

    def pick(key, n: int, latest: bool) -> None:
        buckets: dict[tuple, str] = {}
        for fid, ts in files:
            k = key(ts)
            if latest:
                buckets[k] = fid
            else:
                buckets.setdefault(k, fid)
        keep.update(buckets[k] for k in sorted(buckets, reverse=True)[:n])

    pick(lambda t: (t.year, t.month, t.day), daily, latest=True)
    pick(lambda t: tuple(t.isocalendar())[:2], weekly, latest=False)
    pick(lambda t: (t.year, t.month), monthly, latest=False)
    pick(lambda t: (t.year,), yearly, latest=False)
    return keep
