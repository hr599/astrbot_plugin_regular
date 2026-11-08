"""Held-out labels. One label in five (fixed by message id) is test-only.

Decide rules and the M5 tuning loop look at the dev side only; the bench reports
both. A rule that merely memorised your labels shows up as a dev/test gap instead
of a fake win (the evidence gate from Harness²). Synthetic fixtures aren't split:
they're written to test specific cases, not sampled from the group.
"""

import hashlib

TEST_EVERY = 5


def is_test(msg_id: str) -> bool:
    return int(hashlib.sha256(msg_id.encode("utf-8")).hexdigest(), 16) % TEST_EVERY == 0
