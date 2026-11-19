"""Render a real Lux3D GLB (and the STL converted from it) to PNG stills.

Usage:
    python3 render_turntable.py <model.glb> <out_dir> [--name crate]

Needs numpy + matplotlib (both present on the cloud host). The repo itself stays
stdlib-only; this script is a demo utility, not part of the package.
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_ROOT = os.environ.get("BRIDGE_ROOT", os.path.join(os.path.dirname(HERE), "lux3d-embodied-bridge"))
sys.path.insert(0, os.path.join(BRIDGE_ROOT, "src"))

from lux3d_bridge import glb, stl  # noqa: E402

VIEWS = {
    "front": (18, -90),
    "iso": (24, -55),
    "side": (12, -5),
    "top": (72, -70),
}


def _unit(triangles: np.ndarray) -> np.ndarray:
    lo, hi = triangles.min(axis=(0, 1)), triangles.max(axis=(0, 1))
    centre = (lo + hi) / 2.0
    scale = float((hi - lo).max()) or 1.0
    return (triangles - centre) / scale * 1.6


def _shade(triangles: np.ndarray) -> np.ndarray:
    """Flat lambert-ish shading so the mesh reads as a solid, not a wireframe."""
    edge1 = triangles[:, 1] - triangles[:, 0]
    edge2 = triangles[:, 2] - triangles[:, 0]
    normals = np.cross(edge1, edge2)
    lengths = np.linalg.norm(normals, axis=1)
    lengths[lengths == 0] = 1.0
    normals /= lengths[:, None]
    light = np.array([0.4, 0.75, 0.55])
    light /= np.linalg.norm(light)
    intensity = np.abs(normals @ light)
    base = np.array([0.72, 0.58, 0.38])  # warm wood tone
    return np.clip(base[None, :] * (0.35 + 0.75 * intensity[:, None]), 0, 1)


def render(triangles: np.ndarray, out_png: str, title: str, view: str,
           elev: float | None = None, azim: float | None = None, dpi: int = 200) -> None:
    tris = _unit(triangles)
    colours = _shade(tris)
    if elev is None or azim is None:
        elev, azim = VIEWS[view]

    fig = plt.figure(figsize=(8, 6.4), dpi=dpi, facecolor="#0d1014")
    ax = fig.add_subplot(111, projection="3d")
    ax.set_facecolor("#0d1014")
    ax.add_collection3d(
        Poly3DCollection(tris, facecolors=colours, edgecolors=(0.05, 0.05, 0.05, 0.25),
                         linewidths=0.15, shade=False)
    )
    ax.view_init(elev=elev, azim=azim)
    ax.set_xlim(-0.9, 0.9)
    ax.set_ylim(-0.9, 0.9)
    ax.set_zlim(-0.9, 0.9)
    ax.set_box_aspect((1, 1, 1))
    ax.set_axis_off()
    fig.text(0.06, 0.94, title, color="#e8e6e1", fontsize=15, family="DejaVu Sans", weight="bold")
    fig.text(0.06, 0.05, f"{len(triangles):,} triangles · view: {view}", color="#7d8590",
             fontsize=10, family="DejaVu Sans Mono")
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
    fig.savefig(out_png, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"wrote {out_png}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model")
    parser.add_argument("out_dir")
    parser.add_argument("--name", default="model")
    parser.add_argument("--views", default="front,iso,side,top")
    parser.add_argument("--turntable", type=int, default=0,
                        help="render N frames rotating 360deg into <out_dir>/frames")
    parser.add_argument("--dpi", type=int, default=200)
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    if args.model.lower().endswith(".glb"):
        loaded = glb.load_glb_triangles(args.model)
        triangles = np.asarray(loaded.triangles, dtype=float)
        label = os.path.basename(args.model)
    elif args.model.lower().endswith(".stl"):
        triangles = np.asarray(stl.read_binary_stl(args.model), dtype=float)
        label = os.path.basename(args.model)
    else:
        raise SystemExit("only .glb / .stl")

    print(f"{label}: {len(triangles):,} triangles")

    if args.turntable:
        frames_dir = os.path.join(args.out_dir, "frames")
        os.makedirs(frames_dir, exist_ok=True)
        n = args.turntable
        for i in range(n):
            azim = -180 + 360.0 * i / n
            render(triangles, os.path.join(frames_dir, f"tt_{i:02d}.png"),
                   f"{args.name} — {label}", "custom", elev=18, azim=azim, dpi=args.dpi)
        return 0

    for view in args.views.split(","):
        render(triangles, os.path.join(args.out_dir, f"{args.name}_{view}.png"),
               f"{args.name} — {label}", view.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
