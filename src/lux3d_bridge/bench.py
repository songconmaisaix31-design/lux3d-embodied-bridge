"""Build (and optionally run) the upstream bench command.

This repository does not vendor the engine: the command below points at a
checkout the user already has, and passes only the files this bridge
produced.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import Any, Dict, List, Sequence

DEFAULT_EXAMPLE = "embodied_sim_collect"
DEFAULT_FEATURES = "rigid3d,cam,spec,urdf"


def build_command(
    engine_dir: str,
    urdf_robot: str,
    mesh_base: str,
    mesh_package: str,
    dump_robot_spec: str | None = None,
    episodes: int = 2,
    fingerprints: str | None = None,
    verify_determinism: str | bool = "full",
    extra: Sequence[str] = (),
) -> List[str]:
    """``--verify-determinism`` takes a value in the engine's CLI: full|sample|off."""
    mode = "full" if verify_determinism is True else "off" if verify_determinism is False else str(verify_determinism)
    if mode not in ("full", "sample", "off"):
        raise ValueError(f"verify_determinism must be full|sample|off, got {verify_determinism!r}")
    package_arg = f"{mesh_package}={mesh_base}"
    command: List[str] = [
        "cargo",
        "run",
        "-p",
        "oriera-agent",
        "--example",
        DEFAULT_EXAMPLE,
        "--features",
        DEFAULT_FEATURES,
        "--",
        "--urdf-robot",
        urdf_robot,
        "--mesh-base",
        mesh_base,
        "--mesh-package",
        package_arg,
        "--episodes",
        str(episodes),
    ]
    if dump_robot_spec:
        command += ["--dump-robot-spec", dump_robot_spec]
    if fingerprints:
        command += ["--fingerprints", fingerprints]
    command += ["--verify-determinism", mode]
    command += list(extra)
    return command


def render(command: Sequence[str]) -> str:
    """A copy-pasteable line; quotes only where a shell would need them."""
    parts = []
    for token in command:
        parts.append(f'"{token}"' if (" " in token or "\t" in token) else token)
    return " ".join(parts)


def run(command: Sequence[str], engine_dir: str) -> int:
    if not os.path.isdir(os.path.join(engine_dir, "crates")):
        raise FileNotFoundError(
            f"{engine_dir!r} does not look like an engine checkout (no crates/ directory)"
        )
    env = dict(os.environ)
    env.setdefault("CARGO_TERM_COLOR", "never")
    completed = subprocess.run(command, cwd=engine_dir, env=env, check=False)
    return completed.returncode


def paths_for(out_dir: str, robot_name: str) -> Dict[str, str]:
    return {
        "urdf_robot": os.path.join(out_dir, "urdf", f"{robot_name}.urdf"),
        "mesh_base": os.path.join(out_dir, "meshes"),
        "dump_robot_spec": os.path.join(out_dir, "robot.json"),
        "fingerprints": os.path.join(out_dir, "fingerprints.v2.json"),
    }


def split_extra(raw: Sequence[str]) -> List[str]:
    """Allow `--` passthrough fragments such as `--gate=0.9`."""
    return [token for fragment in raw for token in shlex.split(fragment)]
