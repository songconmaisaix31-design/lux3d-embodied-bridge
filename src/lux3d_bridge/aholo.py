"""Aholo Lux3D client: create an async task, poll it, download the artifact.

Field names differ between the two documented surfaces, so every part of the
request is overridable:

  * profile ``labs``  -> https://labs.aholo3d.com/global/lux3d/v1/...  (raw key)
  * profile ``lux3d`` -> https://api.lux3d.com/v1/generations           (Bearer key)

Pass ``--payload-json`` (or ``--base-url`` / ``--create-path``) when the
console shows a different shape; nothing here guesses on your behalf.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import zipfile
from typing import Any, Dict, Iterable, Optional

PROFILES: Dict[str, Dict[str, str]] = {
    "labs": {
        "base": "https://labs.aholo3d.com",
        "create": "/global/lux3d/v1/generate/{mode}/task/create",
        "poll": "/global/lux3d/v1/generate/tasks/{task_id}",
        "auth": "raw",
    },
    "lux3d": {
        "base": "https://api.lux3d.com",
        "create": "/v1/generations",
        "poll": "/v1/generations/{task_id}",
        "auth": "bearer",
    },
}

SUCCESS_STATES = {"success", "succeeded", "completed", "done", "finished", "ready"}
FAILURE_STATES = {"failed", "error", "cancelled", "canceled", "timeout"}
MODES = ("text-to-3d", "img-to-3d")

_ARTIFACT_RE = re.compile(r"https?://[^\s\"']+\.(glb|obj|zip|ply|usdz|fbx)", re.IGNORECASE)


class AholoError(Exception):
    """A failure that must be reported, not retried forever."""


def _post(url: str, payload: Dict[str, Any], key: str, auth: str, timeout: int) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", key if auth == "raw" else f"Bearer {key}")
    request.add_header("User-Agent", "lux3d-embodied-bridge/0.1")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise AholoError(f"POST {url} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise AholoError(f"POST {url} unreachable: {exc.reason}") from exc


def _get(url: str, key: str, auth: str, timeout: int) -> Dict[str, Any]:
    request = urllib.request.Request(url)
    request.add_header("Authorization", key if auth == "raw" else f"Bearer {key}")
    request.add_header("User-Agent", "lux3d-embodied-bridge/0.1")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise AholoError(f"GET {url} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise AholoError(f"GET {url} unreachable: {exc.reason}") from exc


def _find(node: Any, keys: Iterable[str]) -> Optional[Any]:
    """Depth-first search for the first value under one of ``keys``."""
    if isinstance(node, dict):
        for wanted in keys:
            if wanted in node and node[wanted] not in (None, ""):
                return node[wanted]
        for value in node.values():
            found = _find(value, keys)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find(value, keys)
            if found is not None:
                return found
    return None


def _find_url(node: Any) -> Optional[str]:
    if isinstance(node, str):
        match = _ARTIFACT_RE.search(node)
        return match.group(0) if match else None
    if isinstance(node, dict):
        for key in ("model_url", "modelUrl", "output_url", "outputUrl", "result_url", "url", "artifact"):
            value = node.get(key)
            if isinstance(value, str):
                match = _ARTIFACT_RE.search(value)
                if match:
                    return match.group(0)
        for value in node.values():
            found = _find_url(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_url(value)
            if found:
                return found
    return None


def default_payload(
    prompt: str,
    image_url: str | None,
    version: str,
    face_count: int,
    output_format: str,
    pbr: bool,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "version": version,
        "outputFormat": [output_format],
        "faceCount": face_count,
        "enablePbr": pbr,
    }
    if prompt:
        payload["prompt"] = prompt
    if image_url:
        payload["img"] = image_url
    return payload


def create_task(
    profile: str,
    key: str,
    payload: Dict[str, Any],
    mode: str,
    base_url: str | None = None,
    create_path: str | None = None,
    timeout: int = 60,
    dry_run: bool = False,
) -> str:
    if mode not in MODES:
        raise AholoError(f"mode must be one of {MODES}")
    conf = PROFILES.get(profile)
    if conf is None:
        raise AholoError(f"unknown profile {profile!r}; choose from {sorted(PROFILES)}")
    url = (base_url or conf["base"]) + (create_path or conf["create"].format(mode=mode))
    if dry_run:
        print(f"DRY-RUN POST {url}")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return "dry-run-task"
    response = _post(url, payload, key, conf["auth"], timeout)
    task_id = _find(response, ("task_id", "taskId", "id"))
    if not task_id:
        raise AholoError(f"no task id in response: {json.dumps(response)[:400]}")
    print(f"task {task_id} created via {url}")
    return str(task_id)


def wait_for_artifact(
    profile: str,
    key: str,
    task_id: str,
    base_url: str | None = None,
    poll_path: str | None = None,
    interval: int = 10,
    max_wait: int = 1800,
) -> str:
    conf = PROFILES[profile]
    url_template = (base_url or conf["base"]) + (poll_path or conf["poll"])
    deadline = time.time() + max_wait
    last_status = None
    while time.time() < deadline:
        payload = _get(url_template.format(task_id=task_id), key, conf["auth"], 60)
        status = str(_find(payload, ("status", "state", "task_status", "taskStatus")) or "").lower()
        if status != last_status:
            print(f"  status: {status or 'unknown'}")
            last_status = status
        artifact = _find_url(payload)
        if artifact and (status in SUCCESS_STATES or not status):
            print(f"  artifact: {artifact}")
            return artifact
        if status in FAILURE_STATES:
            raise AholoError(f"task {task_id} ended in {status}: {json.dumps(payload)[:400]}")
        time.sleep(interval)
    raise AholoError(f"task {task_id} produced no artifact within {max_wait}s")


def download(url: str, out_dir: str, name: str, overwrite: bool = True) -> str:
    os.makedirs(out_dir, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "lux3d-embodied-bridge/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        blob = response.read()
    ext = os.path.splitext(url.split("?")[0])[1].lower() or ".bin"
    target = os.path.join(out_dir, f"{name}{ext}")
    if os.path.exists(target) and not overwrite:
        raise AholoError(f"{target} exists; pass --overwrite to replace it")
    with open(target, "wb") as handle:
        handle.write(blob)
    print(f"  saved {target} ({len(blob)} bytes)")
    if ext == ".zip":
        inner = _extract_first_mesh(target, name, out_dir)
        if inner:
            return inner
    return target


def _extract_first_mesh(archive: str, name: str, out_dir: str) -> Optional[str]:
    wanted = (".glb", ".obj", ".ply")
    with zipfile.ZipFile(archive) as bundle:
        candidates = [n for n in bundle.namelist() if n.lower().endswith(wanted)]
        if not candidates:
            print(f"  {archive} has no GLB/OBJ/PLY; leaving the archive in place")
            return None
        chosen = sorted(candidates, key=lambda n: (not n.lower().endswith(".glb"), len(n)))[0]
        data = bundle.read(chosen)
    ext = os.path.splitext(chosen)[1].lower()
    target = os.path.join(out_dir, f"{name}{ext}")
    with open(target, "wb") as handle:
        handle.write(data)
    print(f"  extracted {chosen} -> {target} ({len(data)} bytes)")
    return target
