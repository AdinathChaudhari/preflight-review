"""Token estimation without a tokenizer dependency."""

from __future__ import annotations


def estimate_tokens(text: str) -> int:
    """Rough token count: UTF-8 bytes // 4, at least 1.

    This is a heuristic (about 4 bytes per token for English prose and code),
    not a real tokenizer. It is deterministic and good enough for budgeting.
    """
    return max(1, len(text.encode("utf-8")) // 4)
