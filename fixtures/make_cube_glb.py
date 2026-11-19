#!/usr/bin/env python3
"""Write fixtures/cube.glb from scratch (GLB 2.0, one mesh, 12 triangles).

Used only to make the repository runnable offline: the real pipeline takes
whatever Lux3D returns. Keeping the fixture generated in-repo means the
tests do not depend on a binary blob nobody can audit.
"""

import json
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "cube.glb")

# 8 corners of a 1 m cube centred on the origin
POSITIONS = [
    (-0.5, -0.5, -0.5), (0.5, -0.5, -0.5), (0.5, 0.5, -0.5), (-0.5, 0.5, -0.5),
    (-0.5, -0.5, 0.5), (0.5, -0.5, 0.5), (0.5, 0.5, 0.5), (-0.5, 0.5, 0.5),
]
INDICES = [
    0, 2, 1, 0, 3, 2,   # -Z
    4, 5, 6, 4, 6, 7,   # +Z
    0, 1, 5, 0, 5, 4,   # -Y
    3, 7, 6, 3, 6, 2,   # +Y
    0, 4, 7, 0, 7, 3,   # -X
    1, 2, 6, 1, 6, 5,   # +X
]


def build() -> bytes:
    index_bytes = struct.pack("<36H", *INDICES)
    position_bytes = b"".join(struct.pack("<3f", *p) for p in POSITIONS)

    bin_chunk = index_bytes + position_bytes
    if len(bin_chunk) % 4:
        bin_chunk += b"\x00" * (4 - len(bin_chunk) % 4)

    gltf = {
        "asset": {"version": "2.0", "generator": "lux3d-embodied-bridge fixture"},
        "scene": 0,
        "scenes": [{"name": "cube", "nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "cube"}],
        "meshes": [
            {
                "name": "cube",
                "primitives": [
                    {"attributes": {"POSITION": 1}, "indices": 0, "mode": 4, "material": 0}
                ],
            }
        ],
        "materials": [{"name": "grey", "pbrMetallicRoughness": {"baseColorFactor": [0.6, 0.6, 0.6, 1.0]}}],
        "accessors": [
            {"bufferView": 0, "componentType": 5123, "count": len(INDICES), "type": "SCALAR"},
            {
                "bufferView": 1,
                "componentType": 5126,
                "count": len(POSITIONS),
                "type": "VEC3",
                "min": [-0.5, -0.5, -0.5],
                "max": [0.5, 0.5, 0.5],
            },
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(index_bytes), "target": 34963},
            {
                "buffer": 0,
                "byteOffset": len(index_bytes),
                "byteLength": len(position_bytes),
                "target": 34962,
            },
        ],
        "buffers": [{"byteLength": len(bin_chunk)}],
    }

    json_bytes = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    if len(json_bytes) % 4:
        json_bytes += b" " * (4 - len(json_bytes) % 4)

    total = 12 + 8 + len(json_bytes) + 8 + len(bin_chunk)
    output = bytearray()
    output += b"glTF" + struct.pack("<II", 2, total)
    output += struct.pack("<II", len(json_bytes), 0x4E4F534A) + json_bytes
    output += struct.pack("<II", len(bin_chunk), 0x004E4942) + bin_chunk
    return bytes(output)


if __name__ == "__main__":
    blob = build()
    with open(OUT, "wb") as handle:
        handle.write(blob)
    print(f"wrote {OUT} ({len(blob)} bytes)")
