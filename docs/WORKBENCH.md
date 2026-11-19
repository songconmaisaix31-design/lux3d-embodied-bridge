# 机械臂工作台

工作台用 Lux3D 生成 GLB，用 OriEra 训练和评估桌面四轴机械臂。支持单件刚性块状物体和双指抓放。视口用原始 GLB 显示物体，物理引擎用尺寸相同的长方体计算碰撞。夹取宽度可在 45–60 毫米之间调整。

## 启动

在 OriEra 目录中编译采集程序和 Python 扩展。Windows 的 GNU 工具链需要在 PATH 中加入 MinGW-w64 和 Cargo，并设置 `RUSTUP_TOOLCHAIN=stable-x86_64-pc-windows-gnu`。让 PyO3 使用运行服务的 Python，例如在 PowerShell 中设置 `$env:PYO3_PYTHON = (Get-Command python.exe).Source`。

```powershell
cargo build --release --locked -p oriera-agent --features python,urdf --lib --example embodied_sim_collect
```

工作台使用引擎目录 `examples/15-embodied-sim/vendor/oriera/` 随附的浏览器仿真模块。Node.js 负责用同一模块执行独立评估。在本仓库配置 `.env` 的 `AHOLO_API_KEY`，安装 PyTorch 并启动服务。浏览器打开 [机械臂工作台](http://127.0.0.1:8137/)。

```powershell
python -m pip install torch
python bridge.py serve --engine C:\path\to\3D-engine
```

## 运行过程

1. 生成一个小型块状模型，或从左侧选择已经生成的 GLB。
2. 在「场景」确认物体朝向和夹取宽度。服务保存该资产的 GLB 副本和场景配置，校验结果通过后可以开始训练。
3. 用冻结的场景采集 48 集仿真示范，按成功集训练行为克隆策略。训练时用一组独立验证种子选取策略检查点。
4. 从磁盘加载检查点，使用 12 个独立评估种子运行。机械臂须将物体抬离台面，并在目标区松爪后等待物体稳定。至少 9 集完成才达到验收门槛。
5. 选择任一评估集回放。浏览器用同一场景、种子和动作驱动 OriEra，并核对结束状态的引擎指纹。场景或动作不一致时，回放会报错。视口修正积木、夹爪和台面的显示接触，核对使用引擎原始状态。

每次运行保存在 `out/app/runs/<id>/`，包含 `scene.json`、`asset.glb`、`policy.pt`、`policy_report.json` 和逐集动作记录。原有 URDF 与末端网格实验可从 `/index.html` 打开。

## 本机验收

2026-11-19 从页面发起 Lux3D 任务 `8420009`，生成绿色积木 `48b910f64ce5c915`。页面自动选中生成物，确认场景后启动训练。运行 `20261119-084327-workbench_green_cube-1198aa09` 采集了 48 集成功示范，独立评估 **12/12** 通过。逐集重放的最终仿真状态与各自的评估记录一致。

蓝色方块 `b1177c167095c643` 的包围盒尺寸不同，独立评估也通过 12/12。长宽比例不同的胡桃木块在侧放后完成了流程，独立评估结果为 5/12，页面显示未达到门槛。
