"""Sorting helpers."""
import re

_PART = re.compile(r"(\d+)")


def natural_key(text):
    return [int(p) if p.isdecimal() else p.lower() for p in _PART.split(text)]


def natural_sorted(items):
    return sorted(items, key=natural_key)
