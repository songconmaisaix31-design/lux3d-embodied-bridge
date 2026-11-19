from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from . import asset, pipeline
from .workbench_config import EVAL_EPISODES, EVAL_SEED

PRESET = Path("crates/oriera-embodied/specs/official/tabletop-4dof-pick.json")
KERNEL = Path("examples/15-embodied-sim/vendor/oriera")
CONTACT_KERNEL = KERNEL.with_name("oriera-contact")
KERNEL_FILES = {"oriera_wasm.js", "oriera_wasm_bg.wasm"}
TURN = math.pi / 2
ORIENTATIONS = {
    "upright": ((0, 1, 2), (0, 0, 0)),
    "horizontal": ((2, 1, 0), (0, TURN, 0)),
    "side": ((0, 2, 1), (TURN, 0, 0)),
    "end": ((2, 0, 1), (0, TURN, TURN)),
}


def engine_files(engine: str) -> tuple[Path, Path]:
    target = Path(engine) / "target" / "release"
    if os.name == "nt":
        return target / "examples/embodied_sim_collect.exe", target / "oriera_agent.dll"
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    return target / "examples/embodied_sim_collect", target / f"liboriera_agent{suffix}"


def readiness(engine: str) -> dict:
    if not engine:
        return {"ready": False, "reason": "启动服务时需要指定 OriEra 引擎目录。"}
    collector, module = engine_files(engine)
    kernel = Path(engine) / CONTACT_KERNEL
    if not collector.is_file() or not module.is_file():
        return {"ready": False, "reason": "需要先编译 OriEra 的 release 采集程序和 Python 扩展。"}
    if not all((kernel / name).is_file() for name in KERNEL_FILES):
        return {"ready": False, "reason": "OriEra 目录缺少带接触约束的浏览器仿真模块。"}
    if shutil.which("node") is None:
        return {"ready": False, "reason": "独立评估需要 Node.js。"}
    if importlib.util.find_spec("torch") is None:
        return {"ready": False, "reason": "服务所用的 Python 环境需要安装 PyTorch。"}
    return {"ready": True, "reason": None}


def scene_for(preset: dict, entry: dict, width_mm: float, orientation: str) -> dict:
    bbox = entry["bbox_m"]
    lo, hi, size = (bbox[key] for key in ("min", "max", "size"))
    if not all(math.isfinite(n) and n > 0 for n in size):
        raise ValueError("模型在三个轴上都需要有效的尺寸。")
    if not math.isfinite(width_mm) or not 45 <= width_mm <= 60:
        raise ValueError("夹取宽度需要在 45 至 60 毫米之间。")
    if orientation not in ORIENTATIONS:
        raise ValueError("请选择有效的物体朝向。")
    axes, rotation = ORIENTATIONS[orientation]
    scale = width_mm / (1000 * size[axes[2]])
    dims = [round(size[axis] * scale, 8) for axis in axes]
    if any(not 0.018 <= n <= 0.082 for n in dims):
        raise ValueError("缩放后每个轴需在 18 至 82 毫米之间。请调整朝向或夹取宽度后重试。")
    half = [n / 2 for n in dims]
    pivot = [(low + high) / 2 for low, high in zip(lo, hi)]
    prop = preset["scene"]["props"][0]
    table = next(item for item in preset["scene"]["fixtures"] if item["name"] == "table")
    surface = table["center"][1] + table["half"][1]
    prop.update({
        "name": f"asset_{entry['asset_id']}",
        "shape": {"kind": "cuboid", "half": half},
        "placement": {"range": {"x": [0.172, 0.188], "y": surface + half[1] + 0.008, "z": [-0.027, -0.013]}},
    })
    gripper = preset["robot"]["gripper"]
    gripper.update({
        "finger_x": 0.116,
        "finger_half": [0.016, 0.033, 0.004],
        "gap_open": max(gripper["gap_open"], dims[2] + 0.024),
        "gap_closed": dims[2] + 0.0082,
        "gap_rate": 0.0012,
        "friction": 2.2,
    })
    preset["physics"].update({
        "substeps": 4, "solver_iterations": 10,
        "contact_softness_hz": 140.0, "contact_margin_m": 0.0012,
    })
    preset["randomization"]["enabled"] = False
    preset["task"]["id"] = "pick_place@v2"
    preset["task"]["params"].update({
        "goal": {"range": {"x": [0.279, 0.291], "y": surface, "z": [0.045, 0.057]}},
        "goal_xz_tol": 0.055,
        "goal_y_min": surface + half[1] - 0.009,
        "goal_y_max": surface + half[1] + 0.022,
        "min_separation": 0.10,
    })
    preset["meta"] = {"name": prop["name"]}
    return {
        "spec": preset, "scale": scale, "pivot_m": pivot, "size_m": dims,
        "surface_m": surface, "asset_rotation": rotation, "kernel": "contact",
    }


def prepare(root: str, engine: str, entry: dict, source: str, width_mm: float, orientation: str) -> dict:
    with (Path(engine) / PRESET).open(encoding="utf-8") as handle:
        scene = scene_for(json.load(handle), entry, width_mm, orientation)
    if asset.sha256_file(source) != entry["source_sha256"]:
        raise ValueError("资产文件已变化。请刷新资产列表后重试。")
    run_id = f"{pipeline.new_run_id(entry['name'])}-{uuid.uuid4().hex[:8]}"
    directory = Path(pipeline.runs_root(root)) / run_id
    directory.mkdir(parents=True)
    shutil.copyfile(source, directory / "asset.glb")
    if asset.sha256_file(str(directory / "asset.glb")) != entry["source_sha256"]:
        raise ValueError("复制期间资产文件已变化。请重新准备场景。")
    pipeline.write_json(str(directory / "scene.json"), scene["spec"])
    validator = subprocess.run(
        [str(engine_files(engine)[0]), "--validate", str(directory / "scene.json")],
        cwd=engine, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=15,
    )
    if validator.returncode != 0:
        raise ValueError((validator.stderr or validator.stdout).strip())
    scene["glb_url"] = f"/api/workbench/runs/{run_id}/asset"
    record = {
        "id": run_id,
        "kind": "policy",
        "created_at": pipeline._now(),
        "asset": {k: entry[k] for k in ("name", "asset_id", "source_sha256", "triangles")},
        "task": scene["spec"]["task"]["id"],
        "scene": scene,
        "status": "prepared",
    }
    pipeline.write_json(str(directory / "run.json"), record)
    return record


def run_command(root: str, engine: str, run_id: str) -> tuple[Path, list[str]]:
    directory = Path(pipeline.runs_root(root)) / run_id
    script = Path(__file__).with_name("train_policy.py")
    collector, native = engine_files(engine)
    return directory, [
        sys.executable, "-u", str(script), "--run-dir", str(directory),
        "--collector", str(collector), "--native", str(native),
        "--kernel", str(Path(engine) / CONTACT_KERNEL),
    ]


def run_artifact(root: str, run_id: str, name: str, seed: str | None = None) -> Path:
    record = pipeline.load_run(root, run_id)
    if not record or record.get("kind") != "policy" or record["id"] != run_id:
        raise FileNotFoundError("没有这条训练记录。")
    directory = Path(pipeline.runs_root(root)) / run_id
    if name == "asset":
        return directory / "asset.glb"
    if name == "replay" and seed and seed.isascii() and seed.isdecimal() and len(seed) == 8:
        value = int(seed)
        if EVAL_SEED <= value < EVAL_SEED + EVAL_EPISODES and record["status"] == "done":
            return directory / "replay" / f"{value}.json"
    raise FileNotFoundError("该评估轨迹不存在。")


def save_record(directory: Path, record: dict) -> None:
    staged = directory / "run.tmp"
    pipeline.write_json(str(staged), record)
    staged.replace(directory / "run.json")
