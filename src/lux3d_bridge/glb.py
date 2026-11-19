"""Minimal glTF 2.0 / GLB reader: container -> triangle soup.

Scope is deliberately narrow: we only need what the simulator needs, i.e.
world-space triangles with a bounding box. Materials, textures, skins,
morph targets and animation are not interpreted, and are reported as
skipped rather than silently dropped.

Supported here:
  * GLB container (12-byte header, JSON chunk + BIN chunk)
  * scene -> node hierarchy, both `matrix` and TRS node transforms
  * mesh primitives in mode 4 (TRIANGLES), indexed or non-indexed
  * accessors: 5120/5121/5122/5123/5125/5126, byteStride honoured

Not supported (raises or counts as skipped, never guesses):
  * external .gltf + side-car .bin, data: URI buffers
  * modes 5/6 (strip/fan), Draco / meshopt compression, quantization ext
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

Vec3 = Tuple[float, float, float]
Triangle = Tuple[Vec3, Vec3, Vec3]

GLB_MAGIC = b"glTF"
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942

_COMPONENT = {
    5120: ("b", 1),
    5121: ("B", 1),
    5122: ("h", 2),
    5123: ("H", 2),
    5125: ("I", 4),
    5126: ("f", 4),
}
_TYPE_COUNT = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}


class GlbError(Exception):
    """Raised with a human-readable reason instead of degrading silently."""


@dataclass
class GlbResult:
    triangles: List[Triangle] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    @property
    def bbox(self) -> Tuple[Vec3, Vec3]:
        return bbox_of(self.triangles)


def bbox_of(triangles: List[Triangle]) -> Tuple[Vec3, Vec3]:
    if not triangles:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for tri in triangles:
        for v in tri:
            for k in range(3):
                if v[k] < lo[k]:
                    lo[k] = v[k]
                if v[k] > hi[k]:
                    hi[k] = v[k]
    return tuple(lo), tuple(hi)  # type: ignore[return-value]


def _identity() -> List[List[float]]:
    return [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _mat_mul(a: List[List[float]], b: List[List[float]]) -> List[List[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _from_gltf_matrix(m: List[float]) -> List[List[float]]:
    # glTF stores column-major; our rows are [ [m0,m4,m8,m12], ... ]
    return [
        [m[0], m[4], m[8], m[12]],
        [m[1], m[5], m[9], m[13]],
        [m[2], m[6], m[10], m[14]],
        [m[3], m[7], m[11], m[15]],
    ]


def _from_trs(t: List[float], q: List[float], s: List[float]) -> List[List[float]]:
    x, y, z, w = q
    rr = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    m = _identity()
    for i in range(3):
        for j in range(3):
            m[i][j] = rr[i][j] * s[j]
        m[i][3] = t[i]
    return m


def _apply(m: List[List[float]], v: Vec3) -> Vec3:
    x, y, z = v
    return (
        m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3],
        m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3],
        m[2][0] * x + m[2][1] * y + m[2][2] * z + m[2][3],
    )


def _read_glb_container(blob: bytes) -> Tuple[Dict[str, Any], bytes]:
    if len(blob) < 12 or blob[:4] != GLB_MAGIC:
        raise GlbError("not a GLB file: missing 'glTF' magic")
    version, total = struct.unpack_from("<II", blob, 4)
    if version != 2:
        raise GlbError(f"unsupported GLB version {version} (only 2 is implemented)")
    if total > len(blob):
        raise GlbError(f"truncated GLB: header says {total} bytes, file has {len(blob)}")
    offset = 12
    gltf = None
    binary = b""
    while offset + 8 <= total:
        chunk_len, chunk_type = struct.unpack_from("<II", blob, offset)
        offset += 8
        chunk = blob[offset:offset + chunk_len]
        offset += chunk_len
        if chunk_type == CHUNK_JSON and gltf is None:
            gltf = json.loads(chunk.decode("utf-8"))
        elif chunk_type == CHUNK_BIN and not binary:
            binary = chunk
    if gltf is None:
        raise GlbError("GLB has no JSON chunk")
    return gltf, binary


def _read_accessor(gltf: Dict[str, Any], binary: bytes, index: int) -> List[Any]:
    accessor = gltf["accessors"][index]
    count = accessor["count"]
    ncomp = _TYPE_COUNT[accessor["type"]]
    fmt, csize = _COMPONENT[accessor["componentType"]]
    bv_index = accessor.get("bufferView")
    if bv_index is None:
        # spec: a sparse/constant accessor; we do not invent values
        raise GlbError(f"accessor {index} has no bufferView (sparse accessors unsupported)")
    view = gltf["bufferViews"][bv_index]
    if view.get("buffer", 0) != 0:
        raise GlbError("only buffer 0 (the GLB BIN chunk) is supported")
    base = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    stride = view.get("byteStride") or ncomp * csize
    need = base + stride * (count - 1) + ncomp * csize
    if need > len(binary):
        raise GlbError(
            f"accessor {index} overruns the BIN chunk: need {need} bytes, have {len(binary)}"
        )
    out: List[Any] = []
    unpack = struct.unpack_from
    for i in range(count):
        off = base + i * stride
        values = unpack("<" + fmt * ncomp, binary, off)
        out.append(values[0] if ncomp == 1 else values)
    return out


def _node_matrix(node: Dict[str, Any]) -> List[List[float]]:
    if "matrix" in node:
        return _from_gltf_matrix(node["matrix"])
    return _from_trs(
        node.get("translation", [0.0, 0.0, 0.0]),
        node.get("rotation", [0.0, 0.0, 0.0, 1.0]),
        node.get("scale", [1.0, 1.0, 1.0]),
    )


def load_glb_triangles(path: str) -> GlbResult:
    with open(path, "rb") as handle:
        blob = handle.read()
    gltf, binary = _read_glb_container(blob)

    triangles: List[Triangle] = []
    skipped: List[str] = []
    primitive_count = 0

    def emit(primitive: Dict[str, Any], world: List[List[float]]) -> None:
        nonlocal primitive_count
        primitive_count += 1
        mode = primitive.get("mode", 4)
        attributes = primitive.get("attributes", {})
        if "POSITION" not in attributes:
            skipped.append("primitive without POSITION")
            return
        if mode != 4:
            skipped.append(f"primitive mode {mode} (only TRIANGLES is implemented)")
            return
        positions = _read_accessor(gltf, binary, attributes["POSITION"])
        if "indices" in primitive:
            indices = _read_accessor(gltf, binary, primitive["indices"])
        else:
            indices = list(range(len(positions)))
        if len(indices) % 3 != 0:
            skipped.append("index count not a multiple of 3")
        for i in range(0, len(indices) - 2, 3):
            tri: List[Vec3] = []
            for k in range(3):
                idx = indices[i + k]
                v = positions[idx]
                tri.append(_apply(world, (v[0], v[1], v[2])))
            triangles.append((tri[0], tri[1], tri[2]))

    def walk(node_index: int, parent: List[List[float]], seen: set) -> None:
        if node_index in seen:
            skipped.append(f"node {node_index} revisited (cycle)")
            return
        seen = seen | {node_index}
        node = gltf["nodes"][node_index]
        world = _mat_mul(parent, _node_matrix(node))
        if "mesh" in node:
            mesh = gltf["meshes"][node["mesh"]]
            for primitive in mesh.get("primitives", []):
                emit(primitive, world)
        for child in node.get("children", []):
            walk(child, world, seen)

    roots: List[int] = []
    scenes = gltf.get("scenes", [])
    if scenes:
        scene_index = gltf.get("scene", 0)
        roots = list(scenes[scene_index].get("nodes", []))
    else:
        parents = {c for n in gltf.get("nodes", []) for c in n.get("children", [])}
        roots = [i for i in range(len(gltf.get("nodes", []))) if i not in parents]

    for root in roots:
        walk(root, _identity(), set())

    stats = {
        "container": "glb",
        "glb_version": 2,
        "nodes": len(gltf.get("nodes", [])),
        "meshes": len(gltf.get("meshes", [])),
        "materials": len(gltf.get("materials", [])),
        "images": len(gltf.get("images", [])),
        "animations": len(gltf.get("animations", [])),
        "skins": len(gltf.get("skins", [])),
        "primitives": primitive_count,
        "triangles": len(triangles),
        "vertices_unindexed": len(triangles) * 3,
        "skipped": skipped,
    }
    if not triangles:
        raise GlbError("GLB produced no triangles: " + ("; ".join(skipped) or "empty scene"))
    return GlbResult(triangles=triangles, stats=stats)
