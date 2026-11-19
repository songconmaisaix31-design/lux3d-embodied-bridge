# lux3d-embodied-bridge

[![tests](https://github.com/songconmaisaix31-design/lux3d-embodied-bridge/actions/workflows/tests.yml/badge.svg)](https://github.com/songconmaisaix31-design/lux3d-embodied-bridge/actions/workflows/tests.yml)

**Aholo Lux3D 生成的资产，第一次带上可复核的回执。**
Turn an Aholo Lux3D artifact into a deterministic embodied experiment: `GLB → STL → URDF collision mesh → per-episode fingerprint`.

This repository contains **no code copied from any engine**. It is a bridge: it produces assets and descriptions, then hands a command to an engine checkout you already have.

---

## At a glance · 一眼看懂

| | |
|---|---|
| **Problem<br>问题** | A 3D generation model can hand you a beautiful mesh, and a simulator can hand you a reproducible episode. Nothing in between pins one to the other: the asset is regenerated, the collision geometry changes, and the receipt no longer means anything.<br>3D 生成模型能给你一个好看的网格，仿真器能给你一集可复现的实验。中间那层没人做：资产重新生成一次，碰撞几何就变了，回执也就不再指向任何东西。 |
| **Fix<br>做法** | Hash what Lux3D returned, convert it to the one mesh format the bench actually parses, stamp the asset id into the URDF that the bench consumes, then read the per-episode fingerprints back.<br>把 Lux3D 返回的字节做哈希，转成仿真台真正会解析的网格格式，把资产 id 写进仿真台真正读取的那份 URDF，再把每集指纹读回来。 |
| **Cost<br>代价** | Python 3.10+ standard library only. No pip install, no GPU, no engine fork.<br>只要 Python 3.10+ 标准库；不用装包、不用 GPU、不 fork 引擎。 |
| **Not<br>不是** | Not a renderer, not a physics engine, not a Lux3D client SDK for every endpoint. It is the accounting layer between a generator and a bench.<br>不是渲染器，不是物理引擎，也不是覆盖全部接口的 Lux3D SDK。它就是生成器和仿真台之间的记账层。 |

## The chain · 这条链

```
Aholo Lux3D  ──GLB/OBJ──▶  bridge  ──STL──▶  URDF <collision><mesh>
                              │                        │
                              │ sha256 + bbox + tris   │ package://generated/crate.stl
                              ▼                        ▼
                      assets.lock.json        embodied_sim_collect
                                                       │
                                                       ▼
                                        spec_hash + per-episode fingerprints
```

One sentence: **the asset becomes part of the experiment's identity, not a decoration around it.**
一句话：**资产成为实验身份的一部分，而不是实验外面的一层装饰。**

## Why this exists · 为什么做这个

The Aholo Global 3D AI Hackathon scores 30% on "群核技术栈应用" and requires Lux3D to carry the core 3D content. A project that only shows a generated mesh has nothing to verify. A project that only shows numbers on a laptop cannot prove the numbers came from *that* mesh.

This bridge is the missing seam. It does not try to be a better generator or a better simulator; it makes the hand-off auditable.

群核 Aholo 全球 3D AI 黑客松里，"群核技术栈应用"占 30%，且要求 Lux3D 承担核心 3D 内容。只展示一个生成的网格，没什么可核验；只在笔记本上报一串数字，又证明不了数字来自**那一个**网格。这个桥做的就是中间那道缝。

## Quickstart · 快速开始

Offline, no API key, no engine: the bundled fixture stands in for a Lux3D output.

```bash
python bridge.py demo
```

You should see three steps, a real STL on disk, a URDF whose collision geometry now points at it, and the exact `cargo run` line the bench expects:

```text
[1/3] ingest  fixtures/cube.glb -> STL + asset lock
      crate <- cube.glb (12 tris, 1.000x1.000x1.000 m, asset ca7b4be0429c2804)
[2/3] urdf    fixtures/desk_arm.urdf -> collision mesh
      link tool -> package://generated/crate.stl
[3/3] bench   the command to run against an engine checkout
      cargo run -p oriera-agent --example embodied_sim_collect --features rigid3d,cam,spec,urdf -- ...
```

Run the tests (stdlib `unittest`, 9 cases):

```bash
python -m unittest discover -s tests -v
```

## With a real Lux3D artifact · 用真实的 Lux3D 产物

```bash
cp .env.example .env          # then put your key in .env (it is gitignored)

# 1. generate: real Lux3D task, poll, download, ingest in one shot
python bridge.py generate --mode text-to-3d \
    --prompt "a small wooden shipping crate with metal corner brackets" \
    --version G1-Turbo --style photorealistic --face-count 30000 \
    --name crate --profile global

# 2. or ingest something you already downloaded from the console
python bridge.py ingest out/assets/crate.glb --name crate --lux3d

# 3. write it into the link the bench will collide with
python bridge.py urdf fixtures/desk_arm.urdf --out out/demo/urdf/desk_arm.urdf \
    --link tool --manifest out/meshes/assets.lock.json --asset crate

# 4. print (or run) the bench command
python bridge.py bench --engine /path/to/OriEra --out out/demo --run
```

`--profile global` = `https://api.aholo.ai`, `--profile cn` = `https://api.aholo.cn`.
Auth is `Authorization: <key>` with **no** `Bearer` prefix.

## Commands · 命令

| Command | What it does |
|---|---|
| `generate` | Creates a Lux3D task, polls it, downloads the artifact, ingests it. `--dry-run` sends nothing. |
| `ingest` | `GLB`/`OBJ` → binary STL, plus `assets.lock.json` (sha256, triangles, bbox, asset id). |
| `urdf` | Replaces one link's collision geometry with `package://<pkg>/<mesh>.stl`, stamped with the asset id. |
| `bench` | Builds the `embodied_sim_collect` command; `--run` executes it in your checkout. |
| `verify` | Compares two receipt JSON files and names the first divergent path. |
| `demo` | The offline chain, using the bundled fixture. |

## What is actually verified · 现在真正验证过什么

- `python -m unittest discover -s tests` → **17 tests pass** (`GLB` parsing, deterministic asset ids, STL round-trip, URDF injection, command shaping, receipt diff, Lux3D wire contract).
- `python bridge.py demo` → produces `out/demo/meshes/crate.stl`, `out/demo/urdf/desk_arm.urdf`, `out/demo/meshes/assets.lock.json` on a clean checkout.
- The generated URDF parses back clean (the injector re-parses its own output; a malformed description fails at write time, not at simulation time).
- **A real Lux3D task ran end to end on 2026-11-19**: `text-to-3d`, `G1-Turbo`, `faceCount=30000` → `taskId=8389570`, `status=3` after ~2 min, one `tex_mesh.glb` (9.7 MB) → **21,874 triangles, bbox 0.600 × 0.511 × 0.535 m, asset_id `474c2fd22f0d3f9f`** → injected into `package://generated/crate_lux3d.stl`. Details in [`docs/AHOLO-LUX3D.md`](docs/AHOLO-LUX3D.md).
- **The stamping chain then ran against the real engine on 2026-11-19** (OriEra 0.13.0, Rust 1.100, `rigid3d,cam,spec,urdf`): `desk_arm / 2 DoF`, 0 skipped links, `✓ 确定性复核（full）双跑逐位一致`, 2 episodes · 1400 transitions · 0% success (honest: a 2-DoF arm with a zero `home` cannot reach the table), 1400 real camera PNGs written. Numbers in [`docs/BENCH-COMMAND.md`](docs/BENCH-COMMAND.md) §3.1.

## Honest limits · 边界，写出来不藏着

- **GLB is not the bench's native format.** The upstream bench probes GLB but parses STL into a convex hull. This bridge therefore converts; a GLB handed straight to the bench degrades to a link-segment envelope. That is why the STL step exists, not as a convenience.
- **The Lux3D wire format is pinned to the real gateway, and still overridable.** Base URL, create path, poll path and payload are verified (`api.aholo.ai`, `/global/lux3d/v1/...`, integer statuses `0/1/3/4/6`, download URL in `outputs[n].content` with a ~2 h TTL). `--base-url`, `--create-path`, `--poll-path` and `--payload-json` remain, so a gateway change is a flag, not a rewrite.
- **Convex hulls are conservative.** A crate with a hollow interior collides as its hull. Contact behaviour is stable, geometry is not exact.
- **No Y-up / Z-up conversion is invented here.** Positions are passed through as the file declares them; a rotated asset shows up rotated. The bench has `--urdf-mount-rpy` for installation orientation, and this bridge does not silently apply it for you.
- **Units are trusted as metres**, because glTF and URDF both say metres. A Lux3D export in centimetres will look 100× too big; `assets.lock.json` prints the bounding box so this is visible immediately.

## Repository layout · 仓库结构

```
lux3d-embodied-bridge/
├── bridge.py                 # launcher: python bridge.py <command>
├── src/lux3d_bridge/
│   ├── aholo.py              # Lux3D async task client (create / poll / download)
│   ├── glb.py                # GLB container -> world-space triangles
│   ├── obj.py                # OBJ -> triangles
│   ├── stl.py                # binary STL writer / reader
│   ├── asset.py              # hash + bbox + asset lock
│   ├── urdf.py               # collision mesh injection
│   ├── bench.py              # upstream command builder
│   ├── verify.py             # receipt diff
│   └── cli.py                # subcommands
├── fixtures/                 # cube.glb (generated), desk_arm.urdf
├── tests/
├── docs/                     # AHOLO-LUX3D.md, BENCH-COMMAND.md
└── submission/               # hackathon report / demo script / post draft
```

## Status · 状态

Verified on two machines. Windows 11 / Python 3.13.18: 17 tests pass, the offline demo writes its three artifacts, one real Lux3D generation was carried through to an injected STL, and the injected URDF re-parses. Linux x86_64 / Python 3.12 / Rust 1.100: the whole chain ran against a real engine checkout — the Lux3D GLB became an STL, was injected into the URDF, imported as a 2-DoF `RobotSpec`, collected 2 episodes, and passed `--verify-determinism full` (bit-identical double run). Two upstream CLI details only surface when you actually run it: `--verify-determinism` takes a value (`full|sample|off`), and paths must be absolute because cargo runs with the engine checkout as cwd. This bridge now does both.

## License · 许可

Apache-2.0. No engine code is bundled or copied; the engine remains a separate checkout that you point at.
