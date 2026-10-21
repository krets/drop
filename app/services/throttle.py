import time
from collections import defaultdict, deque


class Throttle:
    """In-memory failure limiter: `limit` failures per `window` seconds per key."""

    def __init__(self, limit: int = 5, window: int = 900):
        self.limit, self.window = limit, window
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[float]:
        q = self._fails[key]
        while q and q[0] < time.monotonic() - self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        return len(self._prune(key)) >= self.limit

    def fail(self, key: str) -> None:
        self._prune(key).append(time.monotonic())

    def clear(self) -> None:
        self._fails.clear()


unlock_throttle = Throttle()
