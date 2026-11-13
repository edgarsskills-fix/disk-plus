"""Недавние источники восстановления (образы): простой JSON в ~/.local/share/disk-plus."""
from __future__ import annotations

import json
import os

FILE = os.path.expanduser("~/.local/share/disk-plus/recent.json")


def load() -> list[str]:
    try:
        items = json.load(open(FILE))
        return [p for p in items if isinstance(p, str) and os.path.exists(p)][:8]
    except (OSError, ValueError):
        return []


def add(path: str):
    items = [p for p in load() if p != path]
    items.insert(0, path)
    try:
        os.makedirs(os.path.dirname(FILE), exist_ok=True)
        json.dump(items[:8], open(FILE, "w"), ensure_ascii=False)
    except OSError:
        pass
