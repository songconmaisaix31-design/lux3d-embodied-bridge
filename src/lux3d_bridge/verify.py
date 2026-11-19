"""Receipt verification: two runs must agree, or say exactly where they diverge.

The engine can do this itself with ``--verify-determinism``; this module is
the out-of-tree check, so a reviewer without cargo can audit the JSON
artifacts that were submitted.
"""

from __future__ import annotations

import json
from typing import Any, List, Tuple


def _load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _first_divergence(a: Any, b: Any, path: str = "$") -> Tuple[bool, str]:
    if type(a) is not type(b):
        return False, f"{path}: type {type(a).__name__} vs {type(b).__name__}"
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a:
                return False, f"{path}.{key}: missing on the left"
            if key not in b:
                return False, f"{path}.{key}: missing on the right"
            if a[key] != b[key]:
                same, where = _first_divergence(a[key], b[key], f"{path}.{key}")
                if not same:
                    return False, where
        return True, path
    if isinstance(a, list):
        if len(a) != len(b):
            return False, f"{path}: length {len(a)} vs {len(b)}"
        for index, (left, right) in enumerate(zip(a, b)):
            if left != right:
                same, where = _first_divergence(left, right, f"{path}[{index}]")
                if not same:
                    return False, where
        return True, path
    return (True, path) if a == b else (False, f"{path}: {a!r} vs {b!r}")


def compare(paths: List[str]) -> int:
    if len(paths) != 2:
        raise SystemExit("verify needs exactly two files: <run-a.json> <run-b.json>")
    left, right = _load(paths[0]), _load(paths[1])
    same, where = _first_divergence(left, right)
    if same:
        print(f"MATCH  {paths[0]}  ==  {paths[1]}")
        for key in ("scheme", "spec_hash", "protocol_id"):
            if isinstance(left, dict) and key in left:
                print(f"       {key}: {left[key]}")
        return 0
    print(f"DIVERGENCE  {where}")
    return 1
