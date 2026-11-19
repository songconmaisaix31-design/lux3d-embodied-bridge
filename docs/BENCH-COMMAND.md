# 把资产交给具身台：命令、输出、坑

这些命令的形状来自引擎自己的 CLI 文档（`docs/URDF-IMPORT.md` 的「全链 CLI」一节）。
本仓库不复制引擎代码，只生成它要读的文件。

## 1. 完整命令

```bash
cd /path/to/lux3D-engine
cargo run -p oriera-agent --example embodied_sim_collect \
  --features rigid3d,cam,spec,urdf -- \
  --urdf-robot  out/demo/urdf/desk_arm.urdf \
  --mesh-base   out/demo/meshes \
  --mesh-package generated=out/demo/meshes \
  --dump-robot-spec out/demo/robot.json \
  --fingerprints    out/demo/fingerprints.v2.json \
  --episodes 2 \
  --verify-determinism full \
  --cam --out /abs/path/to/out/frames
```

`python bridge.py bench --engine /path/to/lux3D-engine --out out/live` 会打印同一行；加 `--run`
就直接执行，加 `--cam` 会额外落相机帧。

> **两个坑，实测踩过**：引擎的 `cargo run` 以 `<engine>` 为工作目录，所有路径必须传
> 绝对路径（本桥现在自动 `abspath`）；`--verify-determinism` 是带值参数，不带值会直接
> 报「需要一个值」。

## 2. 每个参数在做什么

| 参数 | 作用 | 本桥为什么这么填 |
|---|---|---|
| `--urdf-robot` | 用 `RobotSpec` 全链导入替换 preset 机器人（N 轴 + 夹爪 + mesh） | 注入过的 URDF 是唯一带着资产 id 的描述 |
| `--mesh-base` | STL 解析根目录，`<collision><mesh>` 的 STL 会被全量解析成凸包 | 指向 `out/meshes` |
| `--mesh-package` | `package://名称/相对路径` 的包根表 | URDF 里写 `package://generated/crate.stl`，这里给 `generated=<dir>` |
| `--dump-robot-spec` | 导出最终 RobotSpec（含 skipped 与 warnings） | 评审/复核用，别只留在终端 |
| `--fingerprints` | 把每集逐位回执写成金标文件 | 和另一台机器/另一次运行对比 |
| `--episodes` | 采集集数 | 演示 2 集足够；成功率不重要，可复现才重要 |
| `--verify-determinism` | 双跑逐位复核，**必须带值**：`full` \| `sample` \| `off` | 这是「回执」主张的现场证据；`full` 是全量双跑比对 |
| `--cam` + `--out DIR` | 额外把相机帧写成 PNG（`lerobot-v21/videos/...`） | Demo 视频里的「真实运行画面」直接来自这里 |

## 3. 你会看到什么

导入阶段（形状与引擎文档一致，数字随你的资产变化）：

```text
· base 朝向：安装朝向 rpy = [0, 0, 0]（--urdf-mount-rpy …）
· 导入降级/缺省报告（N 条）——全文见 --dump-robot-spec 落盘
  · link tool mesh package://generated/crate.stl → ConvexHull（… 点）
✓ RobotSpec → out/demo/robot.json（desk_arm / 2 DoF，skipped N 条）
```

采集阶段：

```text
spec <preset> · robot desk_arm (2 DoF) · spec_hash <hash> · protocol_hash <hash>
✓ 确定性复核（full）：双跑逐位一致
✓ 采集 2 集 · … transitions · 成功 X 集（X%）· 吞吐 … steps/s
```

## 3.1 2026-11-19 的真实一次（Lux3D 资产 → 引擎）

| 项 | 值 |
|---|---|
| 资产 | `crate_live.glb`（Lux3D `taskId 8390507`）→ `crate_live.stl`，14,902 面，asset `6e593a666736fce0` |
| 导入 | `desk_arm / 2 DoF`，skipped 0 条，警告 1 条（`home` 全零） |
| 复核 | **✓ 确定性复核（full）：双跑逐位一致** |
| 采集 | 2 集 · 1400 transitions · 成功 0 集（0%）· 66 steps/s |
| 落盘 | `robot.json`、`fingerprints.v2.json`（scheme v2）、`eval_report.json`、1400 张相机 PNG |

成功率 0% 是如实结果：2 自由的桌面臂 + `home` 全零的 URDF 零位，够不到台面方块。
回执一致才是这个仓库要证明的东西。

拿到两份回执后，不装 cargo 也能复核：

```bash
python bridge.py verify runA/fingerprints.v2.json runB/fingerprints.v2.json
# MATCH  ...  spec_hash: <hash>
# 或 DIVERGENCE  $.episodes[3].fingerprint: '…' vs '…'
```

## 4. 三个已知坑（引擎文档里记的，别在比赛现场踩）

1. **`gap_closed ≈ 0`**：两指夹爪闭合位相触，方块会被挤飞。用
   `--set robot.gripper.gap_closed=0.046` 覆盖。
2. **`home` 全零 = URDF 零位**：常见为竖直/奇异构型，脚本够不着桌面。用
   `--set "robot.home=[q1,q2,…]"` 覆盖。
3. **base 朝向**：URDF 主轴不朝引擎 +X 时，臂会往反方向伸。用
   `--urdf-mount-rpy 0,0,<yaw>` 修；导入时会打印当前安装朝向。

这三个都是「人话警告 + 给出口」，不会静默改你的模型——和本仓库的态度一致。

## 5. 边界

- 上游引擎对 GLB 只做**探针**（容器校验 + 元信息），STL 才是全量解析并进凸包的格式。
  所以本桥落 STL；直接把 GLB 塞进 `<collision>` 会退化成链段包络，看起来「能跑」但
  碰撞体根本不是你的资产。
- 凸包是保守近似：内凹/镂空的资产碰撞行为稳定，几何不精确。报告书里要写这一句。
- 单位按米处理。Lux3D 若按厘米导出，包围盒会立刻显示 100×，在 `assets.lock.json` 里
  一眼能看出来，不要靠仿真时的怪异行为去猜。
