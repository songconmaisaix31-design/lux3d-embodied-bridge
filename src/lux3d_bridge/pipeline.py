"""From a generated asset to a training run on disk.

One run is one directory under ``out/app/runs/``:

    <run>/
      run.json                # the request, the command and the outcome
      urdf/desk_arm.urdf      # the description, with this asset in the tool collision slot
      meshes/<asset>.stl      # the exact mesh the bench will collide with
      robot.json              # the engine's parsed RobotSpec (written by the engine)
      eval_report.json        # per-episode seeds/steps/reward + success rate + Wilson CI
      fingerprints.v2.json    # the per-episode receipts
      lerobot-v21/            # dataset rows + camera PNG frames

Nothing is invented here: every field the app shows is read back from those files.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import bench, urdf

RUNS_DIRNAME = os.path.join("out", "app", "runs")
DEFAULT_TASK = "pick_place"
TRAIN_FEATURES = "rigid3d,cam,spec,urdf"

ID_RE = re.compile(r"^[0-9A-Za-z_.:-]{1,80}$")


def runs_root(repo_root: str) -> str:
    return os.path.join(repo_root, RUNS_DIRNAME)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]+", "-", text).strip("-")[:40] or "run"


def new_run_id(asset: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return f"{stamp}-{_slug(asset)}"


def write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def prepare_run(
    repo_root: str,
    asset: Dict[str, Any],
    asset_stl: str,
    task: str = DEFAULT_TASK,
    episodes: int = 2,
    seed: int = 1000,
    engine: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create the run directory: mesh copy + injected URDF + command preview."""
    run_id = run_id or new_run_id(asset.get("name") or "asset")
    if not ID_RE.match(run_id):
        raise ValueError(f"bad run id {run_id!r}")
    run_dir = os.path.join(runs_root(repo_root), run_id)
    mesh_dir = os.path.join(run_dir, "meshes")
    urdf_dir = os.path.join(run_dir, "urdf")
    os.makedirs(mesh_dir, exist_ok=True)
    os.makedirs(urdf_dir, exist_ok=True)

    asset_name = asset.get("name") or "asset"
    stl_name = f"{asset_name}.stl"
    shutil.copyfile(asset_stl, os.path.join(mesh_dir, stl_name))

    urdf_out = os.path.join(urdf_dir, "desk_arm.urdf")
    report = urdf.inject(
        os.path.join(repo_root, "fixtures", "desk_arm.urdf"),
        urdf_out,
        package="generated",
        mesh_file=stl_name,
        link_name="tool",
        asset_id=asset.get("asset_id"),
        asset_name=asset_name,
    )

    paths = bench.paths_for(run_dir, "desk_arm")
    command: List[str] = []
    shell_line = ""
    if engine:
        command = bench.build_command(
            engine_dir=engine,
            urdf_robot=paths["urdf_robot"],
            mesh_base=paths["mesh_base"],
            mesh_package="generated",
            dump_robot_spec=paths["dump_robot_spec"],
            episodes=episodes,
            fingerprints=paths["fingerprints"],
            verify_determinism="full",
            extra=["--task", task, "--seed", str(seed), "--out", run_dir, "--cam"],
        )
        shell_line = bench.render(command)

    record = {
        "id": run_id,
        "created_at": _now(),
        "asset": {
            "name": asset_name,
            "asset_id": asset.get("asset_id"),
            "triangles": asset.get("triangles"),
            "bbox_m": asset.get("bbox_m"),
            "source_sha256": asset.get("source_sha256"),
        },
        "task": task,
        "episodes": episodes,
        "seed": seed,
        "engine": engine,
        "command": command,
        "command_line": shell_line,
        "urdf": os.path.relpath(urdf_out, repo_root).replace(os.sep, "/"),
        "mesh": os.path.relpath(os.path.join(mesh_dir, stl_name), repo_root).replace(os.sep, "/"),
        "urdf_report": report,
        "status": "prepared",
    }
    write_json(os.path.join(run_dir, "run.json"), record)
    return {"id": run_id, "dir": run_dir, "record": record}


def finalize_run(repo_root: str, run_id: str, exit_code: int = 0, elapsed_s: float = 0.0) -> Dict[str, Any]:
    """Read the artifacts the engine wrote back into the run record."""
    run_dir = os.path.join(runs_root(repo_root), run_id)
    record = read_json(os.path.join(run_dir, "run.json")) or {"id": run_id}
    report = read_json(os.path.join(run_dir, "eval_report.json"))
    fingerprints = read_json(os.path.join(run_dir, "fingerprints.v2.json"))
    record["status"] = "done" if exit_code == 0 else "failed"
    record["exit_code"] = exit_code
    record["elapsed_s"] = round(elapsed_s, 1)
    record["finished_at"] = _now()
    if report:
        record["result"] = summarize_report(report)
    if fingerprints:
        record["receipt"] = {
            "spec_hash": fingerprints.get("spec_hash"),
            "protocol": fingerprints.get("protocol_id"),
            "episodes": len((fingerprints.get("fingerprints") or {}).get("episode_fingerprints_v2") or []),
            "transitions": fingerprints.get("transitions"),
        }
    record["dataset"] = dataset_summary(run_dir)
    record["frames"] = frame_tracks(run_dir)
    write_json(os.path.join(run_dir, "run.json"), record)
    return record


def summarize_report(report: Dict[str, Any]) -> Dict[str, Any]:
    runs = report.get("runs") or []
    return {
        "task": report.get("task"),
        "spec_hash": report.get("spec_hash"),
        "gate": report.get("gate"),
        "episodes": report.get("episodes"),
        "successes": report.get("successes"),
        "success_rate": report.get("success_rate"),
        "wilson95": report.get("wilson95"),
        "transitions_steps": report.get("transitions_steps") or report.get("throughput_steps_per_s"),
        "throughput_steps_per_s": report.get("throughput_steps_per_s"),
        "verify_determinism": report.get("verify_determinism"),
        "protocol": (report.get("protocol") or {}).get("id"),
        "runs": runs,
        "failure_phases": report.get("failure_phases"),
    }


def dataset_summary(run_dir: str) -> Dict[str, Any]:
    root = os.path.join(run_dir, "lerobot-v21")
    if not os.path.isdir(root):
        return {}
    info = read_json(os.path.join(root, "meta", "info.json")) or {}
    episodes = os.path.join(root, "meta", "episodes.jsonl")
    episode_count = 0
    if os.path.isfile(episodes):
        with open(episodes, "r", encoding="utf-8") as handle:
            episode_count = sum(1 for line in handle if line.strip())
    return {
        "path": os.path.relpath(root, run_dir).replace(os.sep, "/"),
        "rows": info.get("total_episodes") or info.get("total_frames"),
        "episodes": episode_count,
        "info": {key: info[key] for key in ("codebase_version", "robot_type", "total_episodes", "total_frames", "fps") if key in info},
    }


def frame_tracks(run_dir: str) -> List[Dict[str, Any]]:
    """Camera tracks under the dataset: ``observation.images.<cam>/episode_*/frame_*.png``."""
    root = os.path.join(run_dir, "lerobot-v21")
    tracks: List[Dict[str, Any]] = []
    if not os.path.isdir(root):
        return tracks
    for name in sorted(os.listdir(root)):
        track = os.path.join(root, name)
        if not os.path.isdir(track) or name.startswith("data") or name in ("meta",):
            continue
        episodes = sorted(item for item in os.listdir(track) if item.startswith("episode_"))
        if not episodes:
            continue
        first = os.path.join(track, episodes[0])
        frames = sorted(item for item in os.listdir(first) if item.lower().endswith(".png"))
        tracks.append(
            {
                "track": name,
                "episodes": len(episodes),
                "frames_per_episode": len(frames),
                "first": f"{name}/{episodes[0]}/{frames[0]}" if frames else None,
            }
        )
    return tracks


def list_runs(repo_root: str) -> List[Dict[str, Any]]:
    root = runs_root(repo_root)
    if not os.path.isdir(root):
        return []
    records = []
    for name in sorted(os.listdir(root), reverse=True):
        record = read_json(os.path.join(root, name, "run.json"))
        if record:
            records.append(record)
    return records


def load_run(repo_root: str, run_id: str) -> Optional[Dict[str, Any]]:
    if not ID_RE.match(run_id):
        return None
    record = read_json(os.path.join(runs_root(repo_root), run_id, "run.json"))
    if record and "result" not in record:
        report = read_json(os.path.join(runs_root(repo_root), run_id, "eval_report.json"))
        if report:
            record["result"] = summarize_report(report)
    return record


def frame_path(repo_root: str, run_id: str, track: str, episode: str, name: str) -> Optional[str]:
    """Resolve one camera frame, refusing anything that is not a track PNG."""
    if not ID_RE.match(run_id) or not ID_RE.match(track) or not ID_RE.match(episode):
        return None
    if not re.match(r"^frame_\d{6}\.png$", name):
        return None
    base = os.path.realpath(os.path.join(runs_root(repo_root), run_id))
    target = os.path.realpath(os.path.join(base, "lerobot-v21", track, episode, name))
    if os.path.commonpath([base, target]) != base or not os.path.isfile(target):
        return None
    return target
