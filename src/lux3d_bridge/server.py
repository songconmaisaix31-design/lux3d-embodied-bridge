"""Local HTTP bridge for the WebGL viewer.

One process does three jobs, all of them on 127.0.0.1 by default:

1. **serves the page** in ``web/`` (static files, no build step);
2. **serves the artifacts** the page draws - the Lux3D ``.glb`` and the
   converted ``.stl`` - plus the robot description and the receipts;
3. **runs one Lux3D generation at a time** on request, so the viewer's
   "generate" button is a real API call rather than a mock-up.

The API is deliberately boring JSON: every response carries ``ok``; errors
carry ``error``. Nothing here retries a business error, and a failed job keeps
its message instead of degrading into an empty asset.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from . import aholo, asset, bench, pipeline, robot, verify, workbench

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "web")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_ASSET_DIRS = ("out/live", "out/demo/meshes", "out/webgen/meshes")
GEN_DIR = "out/webgen"
MAX_BODY = 64 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
FACE_RANGE = (10000, 300000)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".glb": "model/gltf-binary",
    ".stl": "model/stl",
    ".wasm": "application/wasm",
    ".txt": "text/plain; charset=utf-8",
}


class ApiError(Exception):
    """A request-level failure that becomes a JSON error, never a stack trace."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ── artifacts on disk ────────────────────────────────────────────────────────


class Catalog:
    """The assets a reviewer can load: every ``assets.lock.json`` under the repo.

    A lock entry names its source GLB by basename, but a real Lux3D download is
    named after its task (``lux3d_task8389570_0.glb``). Resolution therefore
    falls back to the task id recorded in ``provenance`` and finally to a
    sha256 match, so identity - not the file name - decides what is served.
    """

    def __init__(self, root: str, dirs: List[str]) -> None:
        self.root = root
        self.dirs = dirs
        self._hashes: Dict[Tuple[str, float, int], str] = {}

    def _abs(self, relative: str) -> str:
        return os.path.join(self.root, relative)

    def _sha256(self, path: str) -> str:
        stat = os.stat(path)
        key = (path, stat.st_mtime, stat.st_size)
        cached = self._hashes.get(key)
        if cached is None:
            cached = asset.sha256_file(path)
            self._hashes[key] = cached
        return cached

    def _candidate_dirs(self, directory: str) -> List[str]:
        """Where a lock entry's source GLB may actually live.

        The CLI downloads to ``out/assets`` and some runs keep the raw task file
        next to the lock; the fixture sits in ``fixtures/``. All three are
        legitimate places for the bytes the manifest hashed.
        """
        parent = os.path.dirname(directory)
        dirs = [directory, os.path.join(directory, "assets"), os.path.join(parent, "assets"), parent, os.path.join(self.root, "fixtures")]
        seen: List[str] = []
        for candidate in dirs:
            if os.path.isdir(candidate) and candidate not in seen:
                seen.append(candidate)
        return seen

    def _resolve_glb(self, directory: str, entry: Dict[str, Any]) -> Optional[str]:
        candidates = self._candidate_dirs(directory)
        source = entry.get("source_file")
        if source:
            for candidate in candidates:
                direct = os.path.join(candidate, source)
                if os.path.isfile(direct):
                    return direct
        task_id = (entry.get("provenance") or {}).get("task_id")
        if task_id:
            for candidate in candidates:
                for name in sorted(os.listdir(candidate)):
                    if str(task_id) in name and name.lower().endswith(".glb"):
                        return os.path.join(candidate, name)
        wanted = entry.get("source_sha256")
        if wanted:
            for candidate in candidates:
                for name in sorted(os.listdir(candidate)):
                    if not name.lower().endswith(".glb"):
                        continue
                    path = os.path.join(candidate, name)
                    if self._sha256(path) == wanted:
                        return path
        return None

    def entries(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        for relative in self.dirs:
            directory = self._abs(relative)
            manifest_path = os.path.join(directory, "assets.lock.json")
            if not os.path.isfile(manifest_path):
                continue
            try:
                with open(manifest_path, "r", encoding="utf-8") as handle:
                    manifest = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                records.append({"name": os.path.basename(directory), "dir": relative, "error": str(exc)})
                continue
            for name, entry in sorted(manifest.get("assets", {}).items()):
                record = dict(entry)
                record["dir"] = relative
                record["manifest"] = os.path.join(relative, "assets.lock.json").replace(os.sep, "/")
                glb = self._resolve_glb(directory, entry)
                stl = os.path.join(directory, entry.get("stl_file") or "")
                record["has_glb"] = bool(glb)
                record["glb_file"] = os.path.basename(glb) if glb else None
                record["has_stl"] = bool(entry.get("stl_file")) and os.path.isfile(stl)
                record["glb_bytes"] = os.path.getsize(glb) if glb else 0
                record["stl_bytes"] = os.path.getsize(stl) if record["has_stl"] else 0
                records.append(record)
        return records

    def find(self, name: str) -> Optional[Dict[str, Any]]:
        for record in self.entries():
            if record.get("name") == name or record.get("asset_id") == name:
                return record
        return None

    def path_for(self, record: Dict[str, Any], kind: str) -> Optional[str]:
        directory = self._abs(record["dir"])
        if kind == "glb":
            return self._resolve_glb(directory, record)
        if kind == "stl":
            candidate = os.path.join(directory, record.get("stl_file") or "")
            return candidate if os.path.isfile(candidate) else None
        return None


def receipts(root: str, dirs: List[str]) -> Dict[str, Any]:
    """Every receipt file we can find, plus the first divergent path between a pair."""
    items: List[Dict[str, Any]] = []
    for relative in dirs:
        directory = os.path.join(root, relative)
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if "fingerprint" not in name.lower() or not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                items.append({"file": f"{relative}/{name}", "error": str(exc)})
                continue
            fingerprints = payload.get("fingerprints") or {}
            items.append(
                {
                    "file": f"{relative}/{name}",
                    "scheme": payload.get("scheme"),
                    "spec_hash": payload.get("spec_hash"),
                    "protocol_id": payload.get("protocol_id"),
                    "protocol_parameter_hash": payload.get("protocol_parameter_hash"),
                    "engine_commit": (payload.get("engine_commit") or "")[:12],
                    "successes": payload.get("successes"),
                    "success_rate": payload.get("success_rate"),
                    "transitions": payload.get("transitions"),
                    "episodes": len(fingerprints.get("episode_fingerprints_v2") or []),
                    "command": payload.get("command"),
                }
            )
    pairs: List[Dict[str, Any]] = []
    for left in items:
        for right in items:
            if left is right or left.get("error") or right.get("error"):
                continue
            if left["file"] >= right["file"]:
                continue
            if left.get("spec_hash") != right.get("spec_hash"):
                continue
            same, where = verify.compare_files(
                os.path.join(root, left["file"]), os.path.join(root, right["file"])
            )
            pairs.append({"left": left["file"], "right": right["file"], "match": same, "first_difference": None if same else where})
    return {"receipts": items, "pairs": pairs}


# ── generation jobs ─────────────────────────────────────────────────────────


class Job:
    def __init__(self, payload: Dict[str, Any], config: Dict[str, Any]) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.payload = payload
        self.config = config
        self.kind = config.get("kind", "generate")
        self.state = "queued"          # queued | running | success | failed
        self.task_id: Optional[str] = None
        self.task_status: Any = None   # the Lux3D integer, verbatim
        self.status_name = "queued"
        self.message = ""
        self.log: List[str] = []
        self.asset: Optional[Dict[str, Any]] = None
        self.result: Optional[Dict[str, Any]] = None
        self.progress: Optional[Dict[str, Any]] = None
        self.cancel_requested = False
        self.run_id: Optional[str] = config.get("run_id")
        self.command: List[str] = []
        self.payload_json: Optional[Dict[str, Any]] = None
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.dry_run = bool(payload.get("dry_run"))
        self.process: Any = None       # subprocess handle, for cancel

    def touch(self) -> None:
        self.updated_at = time.time()

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "dry_run": self.dry_run,
            "task_id": self.task_id,
            "task_status": self.task_status,
            "status_name": self.status_name,
            "message": self.message,
            "log": self.log[-60:],
            "payload": self.payload,
            "payload_json": self.payload_json,
            "asset": self.asset,
            "result": self.result,
            "progress": self.progress,
            "run_id": self.run_id,
            "command": self.command,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class JobRunner(threading.Thread):
    """Runs one Lux3D task, recording every status transition for the page."""

    def __init__(self, job: Job, root: str) -> None:
        super().__init__(daemon=True)
        self.job = job
        self.root = root

    def _log(self, text: str) -> None:
        for line in text.splitlines():
            if line.strip():
                self.job.log.append(line.strip())
        self.job.touch()

    def run(self) -> None:  # noqa: C901 - the sequence is the point
        job, config = self.job, self.job.config
        payload = config["payload"]
        job.state = "running"
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                if job.dry_run:
                    aholo.create_task(
                        config["profile"], config["key"], payload, config["mode"],
                        base_url=config["base_url"], create_path=config["create_path"],
                        dry_run=True,
                    )
                    job.payload_json = payload
                    job.status_name = "dry-run"
                    job.state = "success"
                    job.message = "dry-run: request body printed, nothing was sent"
                    return
                task_id = aholo.create_task(
                    config["profile"], config["key"], payload, config["mode"],
                    base_url=config["base_url"], create_path=config["create_path"],
                )
                job.task_id = task_id
                job.status_name = "init"
                url = self._poll(task_id)
                path = aholo.download(url, config["assets_out"], config["name"], overwrite=True)
                job.log.append(f"downloaded {os.path.basename(path)}")
                entry = asset.ingest(
                    path,
                    config["name"],
                    config["mesh_out"],
                    manifest_path=os.path.join(config["mesh_out"], "assets.lock.json"),
                    provenance={
                        "tool": "Aholo Lux3D",
                        "profile": config["profile"],
                        "mode": config["mode"],
                        "version": payload.get("version"),
                        "task_id": task_id,
                        "source_url": url,
                        "via": "webgl-viewer",
                    },
                )
                job.asset = entry
                job.state = "success"
                job.message = asset.describe(entry)
        except aholo.AholoError as exc:
            job.state = "failed"
            job.message = str(exc)
        except (OSError, ValueError) as exc:
            job.state = "failed"
            job.message = f"{type(exc).__name__}: {exc}"
        finally:
            self._log(buffer.getvalue())

    def _poll(self, task_id: str) -> str:
        job, config = self.job, self.job.config
        deadline = time.time() + config["max_wait"]
        wanted = aholo.FORMAT_EXT.get(config["format"], ".glb")
        last_name = None
        while time.time() < deadline:
            task = aholo.fetch_task(
                config["profile"], config["key"], task_id,
                base_url=config["base_url"], poll_path=config["poll_path"],
            )
            status = aholo.status_of(task)
            job.task_status = status
            job.status_name = aholo.status_name(status)
            if job.status_name != last_name:
                job.log.append(f"status: {job.status_name}")
                last_name = job.status_name
            job.touch()
            if isinstance(status, int) and status in aholo.SUCCESS_STATES:
                url = aholo.pick_output(aholo.output_urls(task), wanted)
                if not url:
                    raise aholo.AholoError(f"task {task_id} succeeded without outputs")
                return url
            if isinstance(status, int) and status in aholo.FAILURE_STATES:
                reason = aholo.error_of(task)
                raise aholo.AholoError(f"task {task_id} ended {job.status_name}: {reason or 'no reason given'}")
            time.sleep(config["interval"])
        raise aholo.AholoError(f"task {task_id} produced no artifact within {config['max_wait']}s")


class TrainingRunner(threading.Thread):
    """Runs the engine's ``embodied_sim_collect`` against a prepared run dir.

    The engine is a separate checkout; it writes its own artifacts. This runner
    only streams its stdout into the job log and reads the artifacts back
    through :mod:`lux3d_bridge.pipeline` when the process exits.
    """

    def __init__(self, job: Job, root: str) -> None:
        super().__init__(daemon=True)
        self.job = job
        self.root = root

    def run(self) -> None:
        job = self.job
        config = job.config
        engine = config["engine"]
        command = config["command"]
        job.state = "running"
        job.command = command
        started = time.time()
        try:
            job.log.append("$ " + bench.render(command))
            job.log.append(f"$ cd {engine}")
            job.touch()
            process = subprocess.Popen(
                command,
                cwd=engine,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env={**os.environ, "CARGO_TERM_COLOR": "never", "PYTHONIOENCODING": "utf-8"},
            )
            job.process = process
            assert process.stdout is not None
            for raw in process.stdout:
                line = raw.rstrip("\r\n")
                if job.kind == "policy" and line.startswith("POLICY "):
                    event = json.loads(line[len("POLICY "):])
                    job.status_name = event.pop("phase")
                    job.progress = event
                    job.touch()
                    continue
                if line.strip():
                    job.log.append(line)
                    if len(job.log) > 400:
                        del job.log[: len(job.log) - 400]
                    job.touch()
            code = process.wait()
        except OSError as exc:
            job.state = "failed"
            job.message = f"cannot start the engine: {exc}"
            self._finalize(started, 1)
            return
        except Exception as exc:  # noqa: BLE001 - a runner thread must never die silently
            job.state = "failed"
            job.message = f"{type(exc).__name__}: {exc}"
            job.log.append(job.message)
            if job.kind == "policy":
                self._finalize(started, 1)
            return
        finally:
            job.process = None

        try:
            record = self._finalize(started, code)
        except Exception as exc:  # noqa: BLE001 - reading artifacts must not hang the job
            job.state = "failed"
            job.message = f"cannot read the run artifacts back: {exc}"
            return
        if code == 0:
            if job.kind == "policy" and not (record or {}).get("result"):
                job.state = "failed"
                job.message = "训练进程结束，但未写出独立评估结果。"
                return
            job.state = "success"
            summary = (record or {}).get("result") or {}
            if job.kind == "policy":
                job.status_name = "completed"
                job.message = f"独立评估完成 · {summary['successes']}/{summary['episodes']} 集抓放成功"
                return
            job.message = (
                f"{summary.get('successes')}/{summary.get('episodes')} episodes · "
                f"{summary.get('protocol') or 'protocol'} · spec {(summary.get('spec_hash') or '?')[:12]}"
            )
        else:
            job.state = "failed"
            if job.kind == "policy" and job.cancel_requested:
                job.status_name = "cancelled"
                job.message = "训练已取消。"
                return
            job.message = f"engine exited {code}; last line: {self._last_line()}"

    def _last_line(self) -> str:
        for line in reversed(self.job.log):
            stripped = line.strip()
            if stripped and not stripped.startswith("$"):
                return stripped[:200]
        return "(no output)"

    def _finalize(self, started: float, code: int) -> Optional[Dict[str, Any]]:
        job = self.job
        if job.kind == "policy":
            directory = Path(job.config["run_dir"])
            record = pipeline.read_json(str(directory / "run.json"))
            if record is None:
                raise ValueError("训练记录无法读取。")
            result = pipeline.read_json(str(directory / "policy_report.json")) if code == 0 else None
            record["status"] = "done" if result is not None else ("cancelled" if job.cancel_requested else "failed")
            record["elapsed_s"] = round(time.time() - started, 2)
            record["exit_code"] = code
            if result is not None:
                record["result"] = result
            elif not job.cancel_requested:
                record["error"] = self._last_line()
            workbench.save_record(directory, record)
            job.result = record
            job.touch()
            return record
        try:
            record = pipeline.finalize_run(self.root, job.run_id, exit_code=code, elapsed_s=time.time() - started)
        except (OSError, ValueError) as exc:
            job.log.append(f"cannot read the run artifacts back: {exc}")
            return None
        job.result = record
        job.touch()
        return record


class Jobs:
    def __init__(self, root: str) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._jobs: Dict[str, Job] = {}

    def submit(self, payload: Dict[str, Any], config: Dict[str, Any]) -> Job:
        job = Job(payload, config)
        runner: threading.Thread
        if job.kind in ("train", "policy"):
            runner = TrainingRunner(job, self.root)
        else:
            runner = JobRunner(job, self.root)
        with self._lock:
            self._jobs[job.id] = job
        runner.start()
        return job

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is not None and job.kind == "policy" and job.state in ("queued", "running"):
            job.cancel_requested = True
            (Path(job.config["run_dir"]) / "cancel.requested").touch()
            job.log.append("已请求取消训练。")
            return True
        process = getattr(job, "process", None) if job else None
        if process is None:
            return False
        process.terminate()
        job.log.append("cancel requested · 已请求终止")
        return True

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def listing(self) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [job.to_json() for job in sorted(jobs, key=lambda item: item.created_at, reverse=True)]


# ── the app ─────────────────────────────────────────────────────────────────


class App:
    def __init__(
        self,
        root: str = REPO_ROOT,
        asset_dirs: Optional[List[str]] = None,
        profile: str = "global",
        key: str = "",
        urdf_path: str = "fixtures/desk_arm.urdf",
        interval: int = 10,
        max_wait: int = 1800,
        base_url: Optional[str] = None,
        create_path: Optional[str] = None,
        poll_path: Optional[str] = None,
        engine: Optional[str] = None,
    ) -> None:
        aholo.load_env_file(os.path.join(root, ".env"))
        self.root = root
        self.asset_dirs = list(asset_dirs or DEFAULT_ASSET_DIRS)
        self.profile = profile
        self.key = key or os.environ.get("AHOLO_API_KEY", "")
        self.urdf_path = urdf_path
        self.interval = interval
        self.max_wait = max_wait
        self.base_url = base_url
        self.create_path = create_path
        self.poll_path = poll_path
        engine_path = engine or os.environ.get("ORIERA_ENGINE")
        self.engine = os.path.abspath(engine_path) if engine_path else ""
        self.catalog = Catalog(root, self.asset_dirs)
        self.jobs = Jobs(root)
        self._policy_lock = threading.Lock()

    # -- config / robot ----------------------------------------------------

    def engine_status(self) -> Dict[str, Any]:
        path = self.engine
        ok = bool(path) and os.path.isdir(os.path.join(path, "crates"))
        return {
            "path": path or None,
            "ok": ok,
            "reason": None
            if ok
            else ("no engine path: pass --engine or set ORIERA_ENGINE" if not path else f"{path} has no crates/ directory"),
        }

    def config(self) -> Dict[str, Any]:
        conf = aholo.PROFILES[self.profile]
        return {
            "ok": True,
            "profile": self.profile,
            "has_key": bool(self.key),
            "auth": "Authorization: <key> (no Bearer)",
            "gateway": self.base_url or conf["base"],
            "mode": "text-to-3d",
            "versions": ["G1", "G1-Turbo"],
            "styles": list(aholo.STYLES),
            "face_range": list(FACE_RANGE),
            "urdf": self.urdf_path,
            "asset_dirs": self.asset_dirs,
            "gen_dir": GEN_DIR,
            "engine": self.engine_status(),
            "workbench": workbench.readiness(self.engine),
            "task_default": pipeline.DEFAULT_TASK,
            "tasks": ["pick_place", "stack", "push"],
            "runs_dir": pipeline.RUNS_DIRNAME.replace(os.sep, "/"),
        }

    def robot_model(self, requested: Optional[str]) -> Dict[str, Any]:
        path = requested or self.urdf_path
        absolute = self._inside(path)
        model = robot.parse_urdf(absolute)
        model["ok"] = True
        model["summary"] = robot.summary(model)
        return model

    def _inside(self, relative: str) -> str:
        """Resolve a repo-relative path and refuse anything outside the repo."""
        target = os.path.realpath(os.path.join(self.root, relative))
        root = os.path.realpath(self.root)
        if os.path.commonpath([root, target]) != root:
            raise ApiError(f"path {relative!r} escapes the repository", 403)
        return target

    # -- catalog -----------------------------------------------------------

    def assets(self) -> Dict[str, Any]:
        listing = self.catalog.entries()
        found = receipts(self.root, self.asset_dirs)
        return {"ok": True, "assets": listing, "receipts": found["receipts"], "pairs": found["pairs"]}

    def asset_bytes(self, name: str, kind: str) -> Tuple[str, str]:
        record = self.catalog.find(name)
        if record is None:
            raise ApiError(f"no asset named {name!r}", 404)
        path = self.catalog.path_for(record, kind)
        if path is None:
            raise ApiError(f"asset {name!r} has no {kind.upper()} on disk", 404)
        return path, CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")

    # -- training ----------------------------------------------------------

    def prepare(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Materialize one run dir (mesh + injected URDF) without running anything."""
        name = (body.get("asset") or "").strip()
        record = self.catalog.find(name)
        if record is None:
            raise ApiError(f"no asset named {name!r}", 404)
        if not record.get("has_stl"):
            raise ApiError(f"asset {name!r} has no STL; ingest it first", 409)
        task = body.get("task") or pipeline.DEFAULT_TASK
        episodes = int(body.get("episodes") or 2)
        seed = int(body.get("seed") or 1000)
        try:
            prepared = pipeline.prepare_run(
                self.root,
                record,
                self.catalog.path_for(record, "stl") or "",
                task=task,
                episodes=episodes,
                seed=seed,
                engine=self.engine or None,
            )
        except (OSError, ValueError) as exc:
            raise ApiError(f"cannot prepare the run: {exc}", 500) from exc
        return {
            "ok": True,
            "run": prepared["record"],
            "dir": pipeline.relpath(prepared["dir"], self.root),
            "engine": self.engine_status(),
        }

    def train(self, body: Dict[str, Any]) -> Dict[str, Any]:
        status = self.engine_status()
        if not status["ok"]:
            raise ApiError(
                f"no engine checkout configured ({status['reason']}). "
                "Run `python bridge.py serve --engine /path/to/OriEra`, or start it with ORIERA_ENGINE set.",
                409,
            )
        name = (body.get("asset") or "").strip()
        record = self.catalog.find(name)
        if record is None:
            raise ApiError(f"no asset named {name!r}", 404)
        if not record.get("has_stl"):
            raise ApiError(f"asset {name!r} has no STL; ingest it first", 409)
        task = body.get("task") or pipeline.DEFAULT_TASK
        if task not in ("pick_place", "stack", "push"):
            raise ApiError("task must be pick_place, stack or push")
        episodes = int(body.get("episodes") or 2)
        if not 1 <= episodes <= 16:
            raise ApiError("episodes must be inside 1..16")
        seed = int(body.get("seed") or 1000)
        verify = body.get("verify") or "full"
        if verify not in ("full", "sample", "off"):
            raise ApiError("verify must be full, sample or off")

        prepared = pipeline.prepare_run(
            self.root,
            record,
            self.catalog.path_for(record, "stl") or "",
            task=task,
            episodes=episodes,
            seed=seed,
            engine=self.engine,
        )
        run_dir = prepared["dir"]
        paths = bench.paths_for(run_dir, "desk_arm")
        command = bench.build_command(
            engine_dir=self.engine,
            urdf_robot=paths["urdf_robot"],
            mesh_base=paths["mesh_base"],
            mesh_package="generated",
            dump_robot_spec=paths["dump_robot_spec"],
            episodes=episodes,
            fingerprints=paths["fingerprints"],
            verify_determinism=verify,
            extra=["--task", task, "--seed", str(seed), "--out", run_dir, "--cam"],
        )
        pipeline.write_json(os.path.join(run_dir, "run.json"), {**prepared["record"], "command": command, "command_line": bench.render(command)})

        job = self.jobs.submit(
            body,
            {
                "kind": "train",
                "engine": self.engine,
                "command": command,
                "run_id": prepared["id"],
                "run_dir": run_dir,
            },
        )
        job.run_id = prepared["id"]
        return {"ok": True, "job": job.to_json(), "run_id": prepared["id"]}

    def runs(self) -> Dict[str, Any]:
        return {"ok": True, "runs": pipeline.list_runs(self.root), "engine": self.engine_status()}

    def workbench_scene(self, body: Dict[str, Any]) -> Dict[str, Any]:
        readiness = workbench.readiness(self.engine)
        if not readiness["ready"]:
            raise ApiError(readiness["reason"], 409)
        asset_id = body.get("asset_id")
        if not isinstance(asset_id, str) or not re.fullmatch(r"[a-f0-9]{16}", asset_id):
            raise ApiError("请选择一个已生成的模型。")
        entry = self.catalog.find(asset_id)
        source = self.catalog.path_for(entry, "glb") if entry else None
        if entry is None or source is None:
            raise ApiError("当前模型的 GLB 文件不存在。", 404)
        try:
            width = float(body.get("width_mm", 50))
            orientation = body.get("orientation", "upright")
            if not isinstance(orientation, str):
                raise ValueError("请选择有效的物体朝向。")
            return {"ok": True, "run": workbench.prepare(self.root, self.engine, entry, source, width, orientation)}
        except (TypeError, ValueError) as exc:
            raise ApiError(str(exc), 422) from exc
        except subprocess.TimeoutExpired as exc:
            raise ApiError("场景校验超时。", 504) from exc

    def workbench_train(self, body: Dict[str, Any]) -> Dict[str, Any]:
        readiness = workbench.readiness(self.engine)
        if not readiness["ready"]:
            raise ApiError(readiness["reason"], 409)
        run_id = body.get("run_id")
        if not isinstance(run_id, str):
            raise ApiError("请先确认场景。")
        with self._policy_lock:
            record = pipeline.load_run(self.root, run_id)
            if not record or record.get("kind") != "policy" or record["id"] != run_id:
                raise ApiError("找不到已确认的场景。", 404)
            if record["status"] != "prepared":
                raise ApiError("此场景已提交。重新确认场景可发起另一次训练。", 409)
            if record["scene"].get("kernel") != "contact":
                raise ApiError("请重新确认场景，以启用当前引擎的接触约束。", 409)
            active = self.jobs.listing()
            if any(job["kind"] == "policy" and job["state"] in ("queued", "running") for job in active):
                raise ApiError("已有训练正在运行。", 409)
            directory, command = workbench.run_command(self.root, self.engine, run_id)
            record["status"] = "running"
            workbench.save_record(directory, record)
            job = self.jobs.submit(body, {
                "kind": "policy", "engine": self.engine, "command": command,
                "run_id": run_id, "run_dir": str(directory),
            })
        return {"ok": True, "job": job.to_json(), "run_id": run_id}

    def run(self, run_id: str) -> Dict[str, Any]:
        record = pipeline.load_run(self.root, run_id)
        if record is None:
            raise ApiError("no such run", 404)
        return {"ok": True, "run": record}

    def frame(self, run_id: str, track: str, episode: str, name: str) -> Tuple[str, str]:
        path = pipeline.frame_path(self.root, run_id, track, episode, name)
        if path is None:
            raise ApiError("no such frame", 404)
        return path, "image/png"

    # -- generation --------------------------------------------------------

    def generate(self, body: Dict[str, Any]) -> Dict[str, Any]:
        mode = body.get("mode") or "text-to-3d"
        if mode not in aholo.MODES:
            raise ApiError(f"mode must be one of {aholo.MODES}")
        prompt = (body.get("prompt") or "").strip()
        if mode == "text-to-3d" and not prompt:
            raise ApiError("prompt is required for text-to-3d")
        version = body.get("version") or "G1-Turbo"
        if version not in ("G1", "G1-Turbo"):
            raise ApiError("version must be G1 or G1-Turbo")
        face_count = int(body.get("face_count") or 20000)
        if not FACE_RANGE[0] <= face_count <= FACE_RANGE[1]:
            raise ApiError(f"face_count must be inside {FACE_RANGE[0]}..{FACE_RANGE[1]}")
        style = body.get("style") or None
        if style is not None and style not in aholo.STYLES:
            raise ApiError(f"style must be one of {list(aholo.STYLES)}")
        name = (body.get("name") or "lux3d").strip()
        if not NAME_RE.match(name):
            raise ApiError("name must match [A-Za-z0-9][A-Za-z0-9_-]{0,39}")
        dry_run = bool(body.get("dry_run"))
        if not dry_run and not self.key:
            raise ApiError("no AHOLO_API_KEY: put it in .env, or tick dry-run", 409)

        gen_root = os.path.join(self.root, GEN_DIR)
        config = {
            "profile": self.profile,
            "key": self.key,
            "mode": mode,
            "name": name,
            "interval": max(0, int(body.get("interval") or self.interval)),
            "max_wait": max(30, int(body.get("max_wait") or self.max_wait)),
            "base_url": self.base_url,
            "create_path": self.create_path,
            "poll_path": self.poll_path,
            "assets_out": os.path.join(gen_root, "assets"),
            "mesh_out": os.path.join(gen_root, "meshes"),
            "format": body.get("format") or "glb",
            "payload": aholo.default_payload(
                prompt, body.get("image_url"), version, face_count,
                body.get("format") or "glb", bool(body.get("pbr", True)), style,
            ),
        }
        job = self.jobs.submit(body, config)
        return {"ok": True, "job": job.to_json()}


# ── HTTP plumbing ───────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    server_version = "lux3d-bridge/0.3"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # no noisy default log
        return

    # -- responses ---------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "no-cache")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Dict[str, Any], status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status)

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path == "/healthz":
                return self._send(200, b"ok\n", "text/plain; charset=utf-8")
            if path == "/api/config":
                return self._json(self.app.config())
            if path == "/api/robot":
                return self._json(self.app.robot_model((query.get("path") or [None])[0]))
            if path == "/api/assets":
                return self._json(self.app.assets())
            if path.startswith("/api/asset/"):
                name = path[len("/api/asset/"):]
                kind = (query.get("kind") or ["glb"])[0].lower()
                if kind not in ("glb", "stl"):
                    raise ApiError("kind must be glb or stl")
                file_path, content_type = self.app.asset_bytes(name, kind)
                with open(file_path, "rb") as handle:
                    return self._send(200, handle.read(), content_type)
            if path == "/api/jobs":
                return self._json({"ok": True, "jobs": self.app.jobs.listing()})
            if path.startswith("/api/jobs/"):
                job = self.app.jobs.get(path[len("/api/jobs/"):])
                if job is None:
                    raise ApiError("no such job", 404)
                return self._json({"ok": True, "job": job.to_json()})
            if path == "/api/runs":
                return self._json(self.app.runs())
            if path.startswith("/api/workbench/kernel/"):
                name = path.removeprefix("/api/workbench/kernel/")
                kernel = workbench.KERNEL
                if name.startswith("contact/"):
                    kernel = workbench.CONTACT_KERNEL
                    name = name.removeprefix("contact/")
                if name not in workbench.KERNEL_FILES or not self.app.engine:
                    raise ApiError("找不到浏览器仿真模块。", 404)
                source = Path(self.app.engine) / kernel / name
                if not source.is_file():
                    raise ApiError("找不到浏览器仿真模块。", 404)
                return self._send(200, source.read_bytes(), CONTENT_TYPES[source.suffix])
            if path.startswith("/api/workbench/runs/"):
                parts = path.removeprefix("/api/workbench/runs/").split("/")
                try:
                    if len(parts) == 2 and parts[1] == "asset":
                        source = workbench.run_artifact(self.app.root, parts[0], parts[1])
                        return self._send(200, source.read_bytes(), CONTENT_TYPES[".glb"])
                    if len(parts) == 3 and parts[1] == "replay":
                        source = workbench.run_artifact(self.app.root, parts[0], parts[1], parts[2])
                        return self._json({"ok": True, "replay": json.loads(source.read_text(encoding="utf-8"))})
                except FileNotFoundError as exc:
                    raise ApiError("找不到这次运行的对应产物。", 404) from exc
                raise ApiError("找不到此路径。", 404)
            if path.startswith("/api/runs/"):
                parts = path[len("/api/runs/"):].split("/")
                if len(parts) == 1:
                    return self._json(self.app.run(parts[0]))
                if len(parts) == 5 and parts[1] == "frame":
                    file_path, content_type = self.app.frame(parts[0], parts[2], parts[3], parts[4])
                    with open(file_path, "rb") as handle:
                        return self._send(200, handle.read(), content_type)
                raise ApiError(f"no route {path}", 404)
            return self._static(path)
        except ApiError as exc:
            return self._error(str(exc), exc.status)
        except robot.UrdfError as exc:
            return self._error(str(exc), 422)
        except (OSError, ValueError) as exc:
            return self._error(f"{type(exc).__name__}: {exc}", 500)

    def do_HEAD(self) -> None:  # noqa: N802
        return self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/generate":
                return self._json(self.app.generate(self._body()), 202)
            if parsed.path == "/api/prepare":
                return self._json(self.app.prepare(self._body()))
            if parsed.path == "/api/train":
                return self._json(self.app.train(self._body()), 202)
            if parsed.path == "/api/workbench/scene":
                return self._json(self.app.workbench_scene(self._body()), 201)
            if parsed.path == "/api/workbench/train":
                return self._json(self.app.workbench_train(self._body()), 202)
            if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/cancel"):
                job_id = parsed.path[len("/api/jobs/"): -len("/cancel")]
                if not self.app.jobs.cancel(job_id):
                    raise ApiError("no running process for this job", 409)
                return self._json({"ok": True, "cancelled": job_id})
            raise ApiError(f"no POST route {parsed.path}", 404)
        except ApiError as exc:
            return self._error(str(exc), exc.status)
        except (OSError, ValueError) as exc:
            return self._error(f"{type(exc).__name__}: {exc}", 500)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError("request body too large", 413)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise ApiError(f"body is not JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise ApiError("body must be a JSON object")
        return body

    # -- static files ------------------------------------------------------

    def _static(self, path: str) -> None:
        relative = "workbench.html" if path in ("", "/") else path.lstrip("/")
        target = os.path.realpath(os.path.join(WEB_DIR, relative))
        web_root = os.path.realpath(WEB_DIR)
        if os.path.commonpath([web_root, target]) != web_root or not os.path.isfile(target):
            raise ApiError(f"not found: {path}", 404)
        content_type = CONTENT_TYPES.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
        with open(target, "rb") as handle:
            self._send(200, handle.read(), content_type)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], app: App) -> None:
        super().__init__(address, Handler)
        self.app = app


def serve(app: App, host: str = "127.0.0.1", port: int = 8137) -> None:
    server = Server((host, port), app)
    print(f"lux3d bridge viewer  http://{host}:{server.server_address[1]}/")
    print(f"  assets   {', '.join(app.asset_dirs)}")
    print(f"  gateway  {app.config()['gateway']}  key {'present' if app.key else 'missing'}")
    status = app.engine_status()
    print(f"  engine   {status['path'] or '(not set — Train is disabled)'}{'' if status['ok'] or not status['path'] else ' (no crates/)'}")
    print("  Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
