"""Aholo Lux3D client: create an async task, poll it, download the artifact.

Wire format verified against the official SDK (``manycore-aholo-sdk-lux3d``):

  * gateway ``https://api.aholo.ai`` (global) or ``https://api.aholo.cn`` (cn)
  * header ``Authorization: <key>`` - never ``Bearer``
  * create ``POST /global/lux3d/v1/generate/{mode}/task/create``
  * poll   ``GET  /global/lux3d/v1/generate/task/get?taskid=<id>``
  * envelope ``{"c": "0", "m": "...", "d": <payload>}``; ``c != "0"`` is a biz error
  * task status is an **integer**: ``0`` init, ``1`` running, ``3`` success,
    ``4`` failed, ``6`` canceled - not a string
  * the download URL is ``d.outputs[n].content`` (~2 h TTL after success)

Everything stays overridable (``--base-url`` / ``--create-path`` / ``--poll-path``
/ ``--payload-json``) so a gateway change is a flag, not a rewrite.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Any, Dict, Iterable, Optional, Sequence

PROFILES: Dict[str, Dict[str, str]] = {
    "global": {
        "base": "https://api.aholo.ai",
        "create": "/global/lux3d/v1/generate/{mode}/task/create",
        "poll": "/global/lux3d/v1/generate/task/get?taskid={task_id}",
        "auth": "raw",
    },
    "cn": {
        "base": "https://api.aholo.cn",
        "create": "/lux3d/v1/generate/{mode}/task/create",
        "poll": "/lux3d/v1/generate/task/get?taskid={task_id}",
        "auth": "raw",
    },
}

STATUS_NAMES = {0: "init", 1: "running", 3: "success", 4: "failed", 6: "canceled"}
SUCCESS_STATES = {3}
FAILURE_STATES = {4, 6}
MODES = ("text-to-3d", "img-to-3d")
STYLES = ("photorealistic", "cartoon", "anime", "hand_painted", "cyberpunk", "fantasy", "glass")

FORMAT_EXT = {"glb": ".glb", "zip": ".zip", "ply": ".ply", "obj_zip": ".zip", "usdz": ".usdz"}


class AholoError(Exception):
    """A failure that must be reported, not retried forever."""


def load_env_file(path: str) -> None:
    """Read ``KEY=value`` lines without overwriting variables already set."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def status_name(value: Any) -> str:
    """Integer Lux3D status -> readable name; unknown values pass through."""
    try:
        return STATUS_NAMES.get(int(value), f"status-{value}")
    except (TypeError, ValueError):
        return str(value).lower()


def _wrap_error(exc: urllib.error.HTTPError, where: str) -> AholoError:
    detail = exc.read().decode("utf-8", errors="replace")[:400]
    hint = ""
    if exc.code == 401:
        hint = " (key rejected: send Authorization: <key> with no Bearer prefix)"
    return AholoError(f"{where} -> HTTP {exc.code}{hint}: {detail}")


def _request(method: str, url: str, key: str, auth: str, timeout: int,
             payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", key if auth == "raw" else f"Bearer {key}")
    request.add_header("User-Agent", "lux3d-embodied-bridge/0.2")
    where = f"{method} {url}"
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise _wrap_error(exc, where) from exc
    except urllib.error.URLError as exc:
        raise AholoError(f"{where} unreachable: {exc.reason}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AholoError(f"{where} returned non-JSON: {text[:200]}") from exc


def unwrap(body: Dict[str, Any]) -> Any:
    """Strip the ``{"c","m","d"}`` envelope; tolerate endpoints that skip it."""
    if isinstance(body, dict) and "c" in body:
        if str(body.get("c")) != "0":
            message = body.get("m") or body.get("message") or "unknown business error"
            raise AholoError(f"business error c={body.get('c')}: {message}")
        if body.get("d") is None:
            raise AholoError("gateway returned c=0 with an empty payload")
        return body["d"]
    return body


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


def output_urls(task: Any) -> Sequence[str]:
    """``outputs[n].content`` is the documented download URL."""
    if not isinstance(task, dict):
        return []
    urls = []
    for item in task.get("outputs") or []:
        if isinstance(item, dict):
            content = item.get("content") or item.get("url")
            if isinstance(content, str) and content.startswith("http"):
                urls.append(content)
        elif isinstance(item, str) and item.startswith("http"):
            urls.append(item)
    return urls


def pick_output(urls: Sequence[str], wanted_ext: str) -> Optional[str]:
    for url in urls:
        path = urllib.parse.urlparse(url).path.lower()
        if path.endswith(wanted_ext):
            return url
    return urls[0] if urls else None


def default_payload(
    prompt: str,
    image_url: str | None,
    version: str,
    face_count: int,
    output_format: str,
    pbr: bool,
    style: str | None = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "version": version,
        "outputFormat": [output_format],
        "faceCount": face_count,
        "enablePbr": pbr,
    }
    if prompt:
        payload["prompt"] = prompt
    if style:
        payload["style"] = style
    if image_url:
        payload["img"] = image_url
    return payload


def data_url(path: str) -> str:
    """Inline a local image as ``data:<mime>;base64,...`` (no upload needed)."""
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as handle:
        blob = handle.read()
    return f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"


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
        shown = dict(payload)
        if isinstance(shown.get("img"), str) and shown["img"].startswith("data:"):
            shown["img"] = f"<data-url {len(shown['img'])} bytes>"
        print(json.dumps(shown, indent=2, sort_keys=True))
        return "dry-run-task"
    response = unwrap(_request("POST", url, key, conf["auth"], timeout, payload))
    # The create endpoint returns the id bare in ``d`` (e.g. ``{"c":"0","d":8389873}``),
    # while other deployments wrap it in an object - accept both.
    if isinstance(response, (int, str)) and not isinstance(response, bool):
        task_id: Any = response
    else:
        task_id = _find(response, ("taskId", "task_id", "id"))
    if not task_id:
        raise AholoError(f"no task id in response: {json.dumps(response)[:400]}")
    print(f"task {task_id} created via {url}")
    return str(task_id)


def fetch_task(
    profile: str,
    key: str,
    task_id: str,
    base_url: str | None = None,
    poll_path: str | None = None,
) -> Dict[str, Any]:
    conf = PROFILES[profile]
    template = (base_url or conf["base"]) + (poll_path or conf["poll"])
    return unwrap(_request("GET", template.format(task_id=task_id), key, conf["auth"], 60))


def wait_for_artifact(
    profile: str,
    key: str,
    task_id: str,
    base_url: str | None = None,
    poll_path: str | None = None,
    interval: int = 10,
    max_wait: int = 1800,
    wanted_ext: str = ".glb",
) -> str:
    deadline = time.time() + max_wait
    last = None
    while time.time() < deadline:
        task = fetch_task(profile, key, task_id, base_url=base_url, poll_path=poll_path)
        status = _find(task, ("status", "state", "taskStatus")) if isinstance(task, dict) else None
        name = status_name(status)
        if name != last:
            print(f"  status: {name}")
            last = name
        if isinstance(status, int) and status in SUCCESS_STATES:
            url = pick_output(output_urls(task), wanted_ext)
            if not url:
                raise AholoError(f"task {task_id} succeeded without outputs: {json.dumps(task)[:400]}")
            print(f"  artifact: {url}")
            return url
        if isinstance(status, int) and status in FAILURE_STATES:
            reason = (task.get("message") or task.get("msg") or task.get("error") or "") if isinstance(task, dict) else ""
            raise AholoError(f"task {task_id} ended {name}: {reason or json.dumps(task)[:400]}")
        time.sleep(interval)
    raise AholoError(f"task {task_id} produced no artifact within {max_wait}s")


def download(url: str, out_dir: str, name: str, overwrite: bool = True) -> str:
    os.makedirs(out_dir, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "lux3d-embodied-bridge/0.2"})
    with urllib.request.urlopen(request, timeout=180) as response:
        blob = response.read()
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower() or ".bin"
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
