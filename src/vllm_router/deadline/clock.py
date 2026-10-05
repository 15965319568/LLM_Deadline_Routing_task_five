import time


class MonotonicClock:
    def now_us(self):
        return time.monotonic_ns() // 1000


class ManualClock:
    """Explicit microsecond clock for the CPU integration environment."""
    def __init__(self, now_us=0):
        self.value = int(now_us)

    def now_us(self):
        return self.value

    def advance_to(self, value):
        if value < self.value:
            raise ValueError("A monotonic clock cannot move backwards")
        self.value = int(value)
