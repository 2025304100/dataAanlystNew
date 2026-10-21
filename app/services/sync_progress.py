"""Bound repeated cancellation reads while checking every network boundary fresh."""
from time import monotonic


class CancellationProbe:
    def __init__(self, check, interval=.25):
        self.check = check
        self.interval = interval
        self.checked_at = float("-inf")
        self.cancelled = False

    def refresh(self):
        if not self.cancelled:
            self.cancelled = bool(self.check())
            self.checked_at = monotonic()
        return self.cancelled

    def __call__(self):
        if monotonic() - self.checked_at >= self.interval:
            return self.refresh()
        return self.cancelled


def cancellation_requested(probe, *, fresh=False):
    if probe is None:
        return False
    return bool(getattr(probe, "refresh", probe)() if fresh else probe())


class ProgressCheckpoint:
    def __init__(self, publish, batch_size=100, interval=1):
        self.publish, self.batch_size, self.interval = publish, batch_size, interval
        self.count, self.last, self.updated_at = 0, None, monotonic()

    def __call__(self, *args):
        self.count += 1
        self.last = args
        if self.count >= self.batch_size or monotonic() - self.updated_at >= self.interval:
            self.flush()

    def flush(self):
        if self.last is not None:
            self.publish(*self.last)
            self.count, self.last, self.updated_at = 0, None, monotonic()
