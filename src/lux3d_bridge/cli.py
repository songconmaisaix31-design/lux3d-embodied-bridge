"""Command line entry point.

Every subcommand is deliberately small and printable: the goal is that a
reviewer can see, in one screen, what was generated, what was hashed and
what the simulator was told.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

from . import aholo, asset, bench, robot, server, urdf, verify

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES = os.path.join(REPO_ROOT, "fixtures")


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _manifest_entry(manifest_path: str, name: str) -> Dict[str, Any]:
    manifest = _read_json(manifest_path)
    entry = manifest.get("assets", {}).get(name)
    if entry is None:
        known = ", ".join(manifest.get("assets", {})) or "none"
        raise SystemExit(f"asset {name!r} not in {manifest_path}; known assets: {known}")
    return entry


def cmd_generate(args: argparse.Namespace) -> int:
    aholo.load_env_file(os.path.join(REPO_ROOT, ".env"))
    key = args.key or os.environ.get("AHOLO_API_KEY")
    if not key and not args.dry_run:
        raise SystemExit(
            "missing API key: put AHOLO_API_KEY in .env, export it, pass --key, "
            "or use --dry-run to print the request"
        )
    image = args.image_url
    if image and os.path.exists(image):
        image = aholo.data_url(image)
    payload = aholo.default_payload(
        args.prompt, image, args.version, args.face_count, args.format,
        not args.no_pbr, args.style,
    )
    if args.payload_json:
        with open(args.payload_json, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    task_id = aholo.create_task(
        args.profile,
        key or "",
        payload,
        args.mode,
        base_url=args.base_url,
        create_path=args.create_path,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        return 0
    url = aholo.wait_for_artifact(
        args.profile,
        key or "",
        task_id,
        base_url=args.base_url,
        poll_path=args.poll_path,
        interval=args.interval,
        max_wait=args.max_wait,
        wanted_ext=aholo.FORMAT_EXT.get(args.format, ".glb"),
    )
    path = aholo.download(url, args.out, args.name, overwrite=args.overwrite)
    if not args.no_ingest:
        entry = asset.ingest(
            path,
            args.name,
            args.mesh_dir,
            manifest_path=os.path.join(args.mesh_dir, "assets.lock.json"),
            provenance={"tool": "Aholo Lux3D", "profile": args.profile, "mode": args.mode,
                        "version": args.version, "task_id": task_id, "source_url": url},
        )
        print("  " + asset.describe(entry))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    entry = asset.ingest(
        args.source,
        args.name,
        args.out,
        manifest_path=os.path.join(args.out, "assets.lock.json"),
        provenance={"tool": "Aholo Lux3D"} if args.lux3d else None,
    )
    print(asset.describe(entry))
    skipped = entry.get("skipped") or []
    if skipped:
        print(f"  skipped: {'; '.join(skipped)}")
    print(f"  stl: {os.path.join(os.path.abspath(args.out), entry['stl_file'])}")
    return 0


def cmd_urdf(args: argparse.Namespace) -> int:
    asset_id = None
    asset_name = args.asset
    if args.manifest and args.asset:
        entry = _manifest_entry(args.manifest, args.asset)
        asset_id = entry["asset_id"]
        mesh_file = entry["stl_file"]
    else:
        if not args.mesh:
            raise SystemExit("either --mesh <file.stl> or --manifest <lock.json> --asset <name>")
        mesh_file = os.path.basename(args.mesh)
    report = urdf.inject(
        args.urdf,
        args.out,
        package=args.package,
        mesh_file=mesh_file,
        link_name=args.link,
        asset_id=asset_id,
        asset_name=asset_name,
    )
    print(f"link {report['link']}: geometry -> {report['mesh_uri']}")
    if report["replaced"]:
        print(f"  replaced: {', '.join(report['replaced'])}")
    print(f"  wrote {report['out']}")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    # The engine runs with cwd=<engine checkout>, so every path we hand it must be absolute.
    out_dir = os.path.abspath(args.out)
    paths = bench.paths_for(out_dir, args.robot_name)
    verify = "off" if args.skip_determinism else args.verify
    extra = bench.split_extra(args.extra)
    if args.cam:
        extra += ["--cam", "--out", os.path.join(out_dir, "frames")]
    command = bench.build_command(
        engine_dir=args.engine,
        urdf_robot=paths["urdf_robot"],
        mesh_base=paths["mesh_base"],
        mesh_package=args.package,
        dump_robot_spec=paths["dump_robot_spec"],
        episodes=args.episodes,
        fingerprints=None if args.no_fingerprints else paths["fingerprints"],
        verify_determinism=verify,
        extra=extra,
    )
    print("cd " + args.engine)
    print(bench.render(command))
    if args.run:
        return bench.run(command, args.engine)
    print("\n(add --run to execute it; the engine checkout is not touched)")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    return verify.compare(args.paths)


def cmd_demo(args: argparse.Namespace) -> int:
    """Offline end-to-end: fixture GLB -> STL -> URDF -> bench command."""
    out = os.path.abspath(args.out or os.path.join(REPO_ROOT, "out", "demo"))
    mesh_dir = os.path.join(out, "meshes")
    manifest = os.path.join(mesh_dir, "assets.lock.json")

    print("[1/3] ingest  fixtures/cube.glb -> STL + asset lock")
    entry = asset.ingest(
        os.path.join(FIXTURES, "cube.glb"),
        "crate",
        mesh_dir,
        manifest_path=manifest,
        provenance={"tool": "fixture", "note": "stand-in for a Lux3D output"},
    )
    print("      " + asset.describe(entry))

    print("[2/3] urdf    fixtures/desk_arm.urdf -> collision mesh")
    urdf_out = os.path.join(out, "urdf", "desk_arm.urdf")
    report = urdf.inject(
        os.path.join(FIXTURES, "desk_arm.urdf"),
        urdf_out,
        package="generated",
        mesh_file=entry["stl_file"],
        link_name=args.link,
        asset_id=entry["asset_id"],
        asset_name="crate",
    )
    print(f"      link {report['link']} -> {report['mesh_uri']}")

    print("[3/3] bench   the command to run against an engine checkout")
    engine = args.engine or os.environ.get("ORIERA_ENGINE") or "<path-to-lux3D-engine-checkout>"
    paths = bench.paths_for(out, "desk_arm")
    command = bench.build_command(
        engine_dir=engine,
        urdf_robot=paths["urdf_robot"],
        mesh_base=paths["mesh_base"],
        mesh_package="generated",
        dump_robot_spec=paths["dump_robot_spec"],
        episodes=2,
        fingerprints=paths["fingerprints"],
        verify_determinism=True,
    )
    print("      " + bench.render(command))
    print(f"\nartifacts in {out}")
    print("nothing was downloaded and no engine code was copied: this is the offline path.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Serve the WebGL viewer, the artifacts and one Lux3D job at a time."""
    aholo.load_env_file(os.path.join(REPO_ROOT, ".env"))
    key = args.key or os.environ.get("AHOLO_API_KEY", "")
    app = server.App(
        root=REPO_ROOT,
        asset_dirs=args.assets or None,
        profile=args.profile,
        key=key,
        urdf_path=args.urdf,
        interval=args.interval,
        max_wait=args.max_wait,
        base_url=args.base_url,
        create_path=args.create_path,
        poll_path=args.poll_path,
        engine=args.engine or os.environ.get("ORIERA_ENGINE"),
    )
    if args.print_config:
        print(json.dumps(app.config(), indent=2, ensure_ascii=False))
        return 0
    try:
        server.serve(app, host=args.host, port=args.port)
    except OSError as exc:
        raise SystemExit(f"cannot listen on {args.host}:{args.port}: {exc}") from exc
    return 0


def cmd_robot(args: argparse.Namespace) -> int:
    """Print the parsed robot model the viewer draws (useful for reviews)."""
    model = robot.parse_urdf(os.path.abspath(args.urdf))
    print(robot.summary(model))
    for warning in model["warnings"]:
        print(f"  warning: {warning}")
    if args.json:
        print(json.dumps(model, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bridge",
        description="Aholo Lux3D -> deterministic embodied bench, without forking the engine.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="create a Lux3D task, poll it, download and ingest")
    gen.add_argument("--prompt", default="", help="text prompt for text-to-3D")
    gen.add_argument("--image-url", default=None, help="reference image URL or local path for img-to-3D")
    gen.add_argument("--mode", choices=aholo.MODES, default="text-to-3d")
    gen.add_argument("--profile", choices=sorted(aholo.PROFILES), default="global",
                     help="global = api.aholo.ai, cn = api.aholo.cn")
    gen.add_argument("--version", default="G1", choices=["G1", "G1-Turbo"])
    gen.add_argument("--style", default=None, choices=list(aholo.STYLES),
                     help="text-to-3D style; default is the service default (photorealistic)")
    gen.add_argument("--face-count", type=int, default=200000)
    gen.add_argument("--format", default="glb", choices=["glb", "zip", "ply"])
    gen.add_argument("--no-pbr", action="store_true")
    gen.add_argument("--payload-json", default=None, help="override the request body")
    gen.add_argument("--base-url", default=None)
    gen.add_argument("--create-path", default=None)
    gen.add_argument("--poll-path", default=None)
    gen.add_argument("--interval", type=int, default=10)
    gen.add_argument("--max-wait", type=int, default=1800)
    gen.add_argument("--key", default=None)
    gen.add_argument("--name", required=True)
    gen.add_argument("--out", default=os.path.join(REPO_ROOT, "out", "assets"))
    gen.add_argument("--mesh-dir", default=os.path.join(REPO_ROOT, "out", "meshes"))
    gen.add_argument("--no-ingest", action="store_true")
    gen.add_argument("--overwrite", action="store_true")
    gen.add_argument("--dry-run", action="store_true")
    gen.set_defaults(func=cmd_generate)

    ing = sub.add_parser("ingest", help="GLB/OBJ -> STL + assets.lock.json")
    ing.add_argument("source")
    ing.add_argument("--name", required=True)
    ing.add_argument("--out", default=os.path.join(REPO_ROOT, "out", "meshes"))
    ing.add_argument("--lux3d", action="store_true", help="stamp provenance: Aholo Lux3D")
    ing.set_defaults(func=cmd_ingest)

    ur = sub.add_parser("urdf", help="write a package:// collision mesh into a URDF link")
    ur.add_argument("urdf")
    ur.add_argument("--out", required=True)
    ur.add_argument("--link", default=None, help="link name (default: last link)")
    ur.add_argument("--package", default="generated")
    ur.add_argument("--mesh", default=None)
    ur.add_argument("--manifest", default=None)
    ur.add_argument("--asset", default=None, help="asset name in the manifest")
    ur.set_defaults(func=cmd_urdf)

    bn = sub.add_parser("bench", help="print or run the upstream embodied_sim_collect command")
    bn.add_argument("--engine", required=True, help="path to a lux3D-engine checkout")
    bn.add_argument("--out", default=os.path.join(REPO_ROOT, "out", "demo"))
    bn.add_argument("--robot-name", default="desk_arm")
    bn.add_argument("--package", default="generated")
    bn.add_argument("--episodes", type=int, default=2)
    bn.add_argument("--no-fingerprints", action="store_true")
    bn.add_argument("--verify", default="full", choices=["full", "sample", "off"],
                    help="--verify-determinism value the engine's CLI expects")
    bn.add_argument("--skip-determinism", action="store_true", help="alias for --verify off")
    bn.add_argument("--cam", action="store_true", help="also render camera PNG frames (--cam)")
    bn.add_argument("--run", action="store_true")
    bn.add_argument("--extra", action="append", default=[], help="extra flags, one string each")
    bn.set_defaults(func=cmd_bench)

    ver = sub.add_parser("verify", help="compare two receipt JSON files")
    ver.add_argument("paths", nargs="+")
    ver.set_defaults(func=cmd_verify)

    demo = sub.add_parser("demo", help="offline chain over the bundled fixture")
    demo.add_argument("--out", default=None)
    demo.add_argument("--engine", default=None)
    demo.add_argument("--link", default="tool")
    demo.set_defaults(func=cmd_demo)

    srv = sub.add_parser("serve", help="serve the WebGL viewer and its API on localhost")
    srv.add_argument("--host", default="127.0.0.1", help="bind address; localhost by default")
    srv.add_argument("--port", type=int, default=8137)
    srv.add_argument("--profile", choices=sorted(aholo.PROFILES), default="global")
    srv.add_argument("--urdf", default=os.path.join("fixtures", "desk_arm.urdf"),
                     help="robot description the viewer draws (repo-relative)")
    srv.add_argument("--assets", action="append", default=None,
                     help="asset directory with assets.lock.json (repeatable; default out/live + out/demo/meshes)")
    srv.add_argument("--interval", type=int, default=10, help="poll interval seconds")
    srv.add_argument("--max-wait", type=int, default=1800)
    srv.add_argument("--key", default=None)
    srv.add_argument("--base-url", default=None)
    srv.add_argument("--create-path", default=None)
    srv.add_argument("--poll-path", default=None)
    srv.add_argument("--print-config", action="store_true", help="print the served config and exit")
    srv.add_argument("--engine", default=None,
                     help="path to a lux3D-engine checkout; enables the Train step (or set ORIERA_ENGINE)")
    srv.set_defaults(func=cmd_serve)

    rb = sub.add_parser("robot", help="parse a URDF and describe what the viewer draws")
    rb.add_argument("urdf", nargs="?", default=os.path.join(FIXTURES, "desk_arm.urdf"))
    rb.add_argument("--json", action="store_true")
    rb.set_defaults(func=cmd_robot)

    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
