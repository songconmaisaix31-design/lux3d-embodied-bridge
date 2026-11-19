"""Wavefront OBJ reader (v / f only) -> triangle soup.

Lux3D can return OBJ inside a ZIP; unpacking is the caller's job, this module
only needs the geometry. Faces with more than three vertices are fan
triangulated, negative (relative) indices are resolved.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

Vec3 = Tuple[float, float, float]
Triangle = Tuple[Vec3, Vec3, Vec3]


class ObjError(Exception):
    """Raised instead of returning an empty mesh with no explanation."""


def load_obj_triangles(path: str) -> Tuple[List[Triangle], Dict[str, Any]]:
    positions: List[Vec3] = []
    triangles: List[Triangle] = []
    faces = 0
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line_no, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line or line[0] == "#":
                continue
            parts = line.split()
            if parts[0] == "v":
                if len(parts) < 4:
                    raise ObjError(f"line {line_no}: vertex needs 3 coordinates")
                positions.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif parts[0] == "f":
                faces += 1
                corners = []
                for token in parts[1:]:
                    index = int(token.split("/")[0])
                    if index < 0:
                        index = len(positions) + index + 1
                    if not 1 <= index <= len(positions):
                        raise ObjError(
                            f"line {line_no}: face index {token} outside 1..{len(positions)}"
                        )
                    corners.append(positions[index - 1])
                if len(corners) < 3:
                    continue
                for i in range(1, len(corners) - 1):
                    triangles.append((corners[0], corners[i], corners[i + 1]))

    if not triangles:
        raise ObjError("OBJ produced no triangles (no 'f' lines with >= 3 vertices)")
    stats = {
        "container": "obj",
        "vertices": len(positions),
        "faces": faces,
        "triangles": len(triangles),
        "skipped": [],
    }
    return triangles, stats
