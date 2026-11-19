# 机械臂工作台

工作台用 Lux3D 生成 GLB，用 lux3D-engine 训练和评估桌面四轴机械臂。支持单件刚性块状物体和双指抓放。视口用原始 GLB 显示物体，物理引擎用尺寸相同的长方体计算碰撞。夹取宽度可在 45–60 毫米之间调整。

## 从全新克隆启动

以下命令使用 Windows PowerShell。准备 Git、64 位 Python 3.13、Node.js、Rustup 和 MinGW-w64 GCC。在一个存放项目的目录中运行以下命令，将示例中的 MinGW-w64 路径换成你的安装路径。

```powershell
git -c core.autocrlf=false clone --branch main https://github.com/songconmaisaix31-design/lux3D-engine.git
git clone --branch main https://github.com/songconmaisaix31-design/lux3d-embodied-bridge.git

$env:Path = "C:\Users\DW\winlibs\mingw64\bin;$env:USERPROFILE\.cargo\bin;$env:Path"
$env:RUSTUP_TOOLCHAIN = 'stable-x86_64-pc-windows-gnu'
rustup toolchain install stable-x86_64-pc-windows-gnu
python -m pip install torch
$env:PYO3_PYTHON = (Get-Command python.exe).Source

Set-Location lux3D-engine
cargo generate-lockfile
cargo build --release --locked -p oriera-agent --features python,urdf --lib --example embodied_sim_collect
$engineDir = (Get-Location).Path
Set-Location ../lux3d-embodied-bridge
```

首次克隆需要生成本地 `Cargo.lock`。工作台用同一个 Python 运行 PyTorch 和引擎扩展。Node.js 使用引擎仓库随附的 `examples/15-embodied-sim/vendor/oriera-contact/` 执行独立评估，浏览器也用这份 WebAssembly 回放。

## 用麻将训练和回放

准备一个麻将 GLB。复跑已验收的资产时，使用 Lux3D 任务 `8421303` 下载的 `majiang.glb`，导入后应显示资产 ID `1b9952482e825bda`。将下面的源文件路径替换为你的文件位置。

```powershell
New-Item -ItemType Directory -Force out/live | Out-Null
Copy-Item -LiteralPath 'C:\path\to\majiang.glb' -Destination out/live/majiang.glb
python bridge.py ingest out/live/majiang.glb --name majiang --out out/live --lux3d
python bridge.py serve --engine $engineDir
```

已有 GLB 可以直接训练。要在页面中生成新模型，在工作台根目录的 `.env` 设置 `AHOLO_API_KEY`，再从「生成」提交一块单独的麻将牌。

1. 打开 [机械臂工作台](http://127.0.0.1:8137/)，确认状态栏显示「lux3D-engine · 就绪」。如端口已被占用，在启动命令中添加 `--port 8150` 并访问对应端口。
2. 从左侧选择 `majiang`，在「场景」中选择「侧放」，宽度设为 50 毫米，点击「准备场景」。
3. 在「训练」中点击「开始训练」，等待示范采集、策略训练和独立评估完成。
4. 在「回放」里确认至少 9/12 集抓放成功、12/12 集无穿透。选中一集，等视口显示「已核对轨迹」后点击「播放」。

## 运行过程

1. 生成一个小型块状模型，或从左侧选择已经生成的 GLB。
2. 在「场景」确认物体朝向和夹取宽度。服务保存该资产的 GLB 副本和场景配置，校验结果通过后可以开始训练。
3. 用冻结的场景采集 48 集仿真示范，按成功集训练行为克隆策略。训练时用一组独立验证种子选取策略检查点。
4. 从磁盘加载检查点，使用 12 个独立评估种子运行。机械臂须将物体抬离台面，并在目标区松爪后等待物体稳定。至少 9 集完成抓放，且 12 集的夹爪、物体和台面都通过逐帧碰撞体检查，才达到验收门槛。评估记录保留各接触对的最大穿透深度，浮点舍入容差为 1 微米。
5. 选择任一评估集回放。新运行直接绘制求解器输出的夹爪碰撞体和物体位姿。浏览器用同一场景、种子和动作驱动 lux3D-engine，并核对结束状态的引擎指纹。场景或动作不一致时，回放会报错。

接触约束在每个物理子步校正重叠，双侧夹持面满足接触条件后建立刚性抓持约束，松爪时解除。历史运行使用各自原有的浏览器仿真模块。

每次运行保存在 `out/app/runs/<id>/`，包含 `scene.json`、`asset.glb`、`policy.pt`、`policy_report.json` 和逐集动作记录。原有 URDF 与末端网格实验可从 `/index.html` 打开。

## 本机验收

2026-11-19 从页面发起 Lux3D 任务 `8420009`，生成绿色积木 `48b910f64ce5c915`。页面自动选中生成物，确认场景后启动训练。运行 `20261119-084327-workbench_green_cube-1198aa09` 采集了 48 集成功示范，独立评估 **12/12** 通过。逐集重放的最终仿真状态与各自的评估记录一致。

蓝色方块 `b1177c167095c643` 的包围盒尺寸不同，独立评估也通过 12/12。长宽比例不同的胡桃木块在侧放后完成了流程，独立评估结果为 5/12，页面显示未达到门槛。

麻将资产 `1b9952482e825bda` 在接触约束引擎上重新采集和训练，运行 `20261119-091818-majiang-5638f478` 使用 42 集成功示范。12 集独立评估全部完成抓放，5,109 帧原始碰撞体中，夹爪对台面、麻将对台面及夹爪对麻将的最大穿透深度均为 0 毫米。浏览器回放的 12 集指纹与评估一致。

引擎目录改为 `C:\Users\DW\repos\lux3D-engine` 后，重新训练的麻将运行 `20261119-093933-majiang-e78b6afe` 使用 42 集成功示范。独立评估 12/12 集完成抓放，5,109 帧均未检测到穿透。新旧两次运行的回放分别核对了 12 集指纹，浏览器播放新运行的第 1 集至结束。
