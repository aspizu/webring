from __future__ import annotations

import time


def seconds_since_epoch() -> int:
    return int(time.time())
