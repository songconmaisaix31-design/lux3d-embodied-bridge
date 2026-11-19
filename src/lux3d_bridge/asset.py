"""Asset ingest: Lux3D output -> STL + a deterministic asset lock entry.

An asset entry is the identity of one generated object: the hash of the
bytes Lux3D returned, the hash of the STL we hand to the simulator, the
triangle count and the bounding box. The engine's receipts are only
meaningful if the geometry behind them is pinned, which is what this
manifest is for.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from .glb import bbox_of, load_glb_triangles
from .obj import load_obj_triangles
from .stl import write_binary_stl

PIPELINE_VERSION = "0.1.0"
MANIFEST_SCHEMA = "lux3d-asset-lock/v1"
BBOX_DIGITS = 6  # micrometre resolution; also what keeps asset_id stable


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _load_geometry(source: str) -> Tuple[List[Any], Dict[str, Any]]:
    ext = os.path.splitext(source)[1].lower()
    if ext in (".glb", ".gltf"):
        result = load_glb_triangles(source)
        return result.triangles, result.stats
    if ext == ".obj":
        return load_obj_triangles(source)
    raise ValueError(f"unsupported source format {ext!r}: Lux3D exports GLB/OBJ, convert the rest first")


def _bbox_mm(triangles: List[Any]) -> Dict[str, List[float]]:
    lo, hi = bbox_of(triangles)
    return {
        "min": [round(v, BBOX_DIGITS) for v in lo],
        "max": [round(v, BBOX_DIGITS) for v in hi],
        "size": [round(hi[i] - lo[i], BBOX_DIGITS) for i in range(3)],
    }


def ingest(
    source: str,
    name: str,
    out_dir: str,
    manifest_path: str | None = None,
    provenance: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    """Convert one Lux3D artifact to STL and record its lock entry."""
    if not os.path.isfile(source):
        raise FileNotFoundError(f"no such asset: {source}")
    triangles, stats = _load_geometry(source)

    os.makedirs(out_dir, exist_ok=True)
    stl_name = f"{name}.stl"
    stl_path = os.path.join(out_dir, stl_name)
    write_binary_stl(stl_path, triangles)

    entry: Dict[str, Any] = {
        "name": name,
        "source_file": os.path.basename(source),
        "source_sha256": sha256_file(source),
        "stl_file": stl_name,
        "stl_sha256": sha256_file(stl_path),
        "triangles": len(triangles),
        "bbox_m": _bbox_mm(triangles),
        "pipeline_version": PIPELINE_VERSION,
        "converter": stats.get("container"),
        "source_stats": {k: v for k, v in stats.items() if k != "skipped"},
        "skipped": stats.get("skipped", []),
    }
    if provenance:
        entry["provenance"] = provenance
    entry["ingested_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry["asset_id"] = hashlib.sha256(
        _canonical(
            {
                "source_sha256": entry["source_sha256"],
                "stl_sha256": entry["stl_sha256"],
                "triangles": entry["triangles"],
                "bbox_m": entry["bbox_m"],
                "pipeline_version": PIPELINE_VERSION,
            }
        ).encode("utf-8")
    ).hexdigest()[:16]

    manifest_path = manifest_path or os.path.join(out_dir, "assets.lock.json")
    manifest = {"schema": MANIFEST_SCHEMA, "assets": {}}
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if loaded.get("schema") != MANIFEST_SCHEMA:
            raise ValueError(
                f"{manifest_path} has schema {loaded.get('schema')!r}, expected {MANIFEST_SCHEMA!r}"
            )
        manifest = loaded
    manifest["assets"][name] = entry
    manifest["assets"] = dict(sorted(manifest["assets"].items()))
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")

    return entry


def describe(entry: Dict[str, Any]) -> str:
    size = entry["bbox_m"]["size"]
    return (
        f"{entry['name']} <- {entry['source_file']} "
        f"({entry['triangles']} tris, {size[0]:.3f}x{size[1]:.3f}x{size[2]:.3f} m, "
        f"asset {entry['asset_id']})"
    )
