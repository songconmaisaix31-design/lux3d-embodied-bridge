"""Binary STL writer / reader.

The upstream bench parses collision meshes as STL (see its URDF-IMPORT
doc); GLB is only probed there, so the bridge lands everything as STL.
"""

from __future__ import annotations

import math
import struct
from typing import List, Tuple

Vec3 = Tuple[float, float, float]
Triangle = Tuple[Vec3, Vec3, Vec3]

DEFAULT_HEADER = b"lux3d-embodied-bridge binary STL"


def _normal(tri: Triangle) -> Vec3:
    a, b, c = tri
    u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    nx = u[1] * v[2] - u[2] * v[1]
    ny = u[2] * v[0] - u[0] * v[2]
    nz = u[0] * v[1] - u[1] * v[0]
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length == 0.0:
        return (0.0, 0.0, 0.0)
    return (nx / length, ny / length, nz / length)


def write_binary_stl(path: str, triangles: List[Triangle], header: bytes = DEFAULT_HEADER) -> None:
    if not triangles:
        raise ValueError("refusing to write an empty STL")
    head = header[:80].ljust(80, b" ")
    with open(path, "wb") as handle:
        handle.write(head)
        handle.write(struct.pack("<I", len(triangles)))
        for tri in triangles:
            handle.write(struct.pack("<3f", *_normal(tri)))
            for vertex in tri:
                handle.write(struct.pack("<3f", *vertex))
            handle.write(struct.pack("<H", 0))


def read_binary_stl(path: str) -> List[Triangle]:
    with open(path, "rb") as handle:
        blob = handle.read()
    if len(blob) < 84:
        raise ValueError(f"{path}: shorter than a binary STL header")
    count = struct.unpack_from("<I", blob, 80)[0]
    expected = 84 + count * 50
    if len(blob) != expected:
        raise ValueError(f"{path}: declared {count} triangles needs {expected} bytes, file has {len(blob)}")
    triangles: List[Triangle] = []
    offset = 84
    for _ in range(count):
        offset += 12  # normal, recomputed on write and not trusted here
        vertices = []
        for _ in range(3):
            vertices.append(struct.unpack_from("<3f", blob, offset))
            offset += 12
        offset += 2
        triangles.append(tuple(vertices))  # type: ignore[arg-type]
    return triangles
