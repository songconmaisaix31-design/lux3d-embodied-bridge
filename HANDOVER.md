# 交接文档 · lux3d-embodied-bridge

> 写给下一个接手的人：这个项目在哪、现在能跑到什么程度、哪些地方会咬人、下一步该动哪里。
> 最后更新：2026-11-19 · commit `169a140` · 全部结论都在本机实测过。

---

## 0. 一分钟定位

**项目目录**

```
C:\Users\DW\Doubao\chats\2026-11-19\new-chat\lux3d-embodied-bridge
```

**远端**：<https://github.com/songconmaisaix31-design/lux3d-embodied-bridge>（分支 `main`）

**它是什么**：Aholo Lux3D（文生 3D）与具身仿真之间的那一层——把一个生成出来的网格，
变成一次**可复核**的具身实验。现在的形态是一个本地应用：浏览器里三步走完
**生成 → 训练 → 回执**。

**它还依赖两个外部目录（不在本仓库里，也不 vendor）**

| 依赖 | 本机路径 | 说明 |
|---|---|---|
| 引擎 checkout | `C:\Users\DW\repos\3D-engine` | OriEra 0.13.0（= `OriEra-engine/engine@49d8592` 的完整副本，本机 HEAD `615cab4` 只是 README 提交）。**不可复制进本仓库**，用 `--engine` 指过去 |
| MinGW-w64 GCC | `C:\Users\DW\winlibs\mingw64\bin` | 本机没有 MSVC 链接器，用 gnu 工具链 + 这份 gcc（见 §4） |

**仓库体积**：不含 `.git` 约 63 MB，其中 `out/` 占 57.7 MB（生成的 GLB/STL/数据集，**不进 git**）；
提交内容约 5 MB（主要是 vendored three.js 1.5 MB 和截图）。

---

## 1. 五分钟接手

```powershell
cd C:\Users\DW\Doubao\chats\2026-11-19\new-chat\lux3d-embodied-bridge

# 1) 离线自检：不花钱、不需要引擎
python bridge.py demo
python -m unittest discover -s tests        # 45 条

# 2) 起应用（本机 gnu 工具链 + 引擎）
$env:Path = "C:\Users\DW\winlibs\mingw64\bin;$env:USERPROFILE\.cargo\bin;$env:Path"
$env:RUSTUP_TOOLCHAIN = "stable-x86_64-pc-windows-gnu"
python bridge.py serve --engine C:\Users\DW\repos\3D-engine
#    → http://127.0.0.1:8137/
```

打开页面后应该看到：左边三张资产卡（`crate_live` / `crate` / `crate_web`）、中间 URDF 解析出来的
2 自由度桌面臂 + 生成的木箱、右边「生成 / 训练 / 回执」三个页签。
如果 `out/` 是空的（新克隆），左栏会是空的——先跑 `python bridge.py demo` 或到页面里生成一次。

**验证环境是否真的通**：点「训练」→「开始训练」。日志里应出现
`✓ 确定性复核（full）：双跑逐位一致`，30 秒左右结束，成功率大概率是 **0%**——这是对的，见 §6。

---

## 2. 架构（三层，没有前端构建）

```
┌─ 浏览器  web/（无构建：importmap + ES modules）
│    index.html   三步流程骨架
│    js/main.js   状态机：生成 → 准备 → 训练 → 回执；轮询 job；渲染结果
│    js/arm.js    URDF 模型 → three.js 层级 + FK + 关节轴标记
│    js/scene.js  渲染器 / 灯光 / 桌面 / 相机取景
│    js/stl.js    binary + ASCII STL → BufferGeometry
│    js/api.js    对本地 API 的薄封装
│    vendor/      three.js r169（MIT，vendored，见 web/vendor/README.md）
│
├─ HTTP / JSON（只监听 127.0.0.1）
│
├─ src/lux3d_bridge/server.py   ThreadingHTTPServer
│    · 静态页 + 资产字节
│    · JobRunner       = Lux3D 生成（create → poll → download → ingest）
│    · TrainingRunner  = 引擎子进程（stdout 流进 job.log，结束时读产物）
│    · /api/{config,robot,assets,asset,generate,prepare,train,jobs,runs}
│
├─ src/lux3d_bridge/*.py
│    aholo.py    Lux3D 客户端（裸 Authorization、整数状态机、2h 下载 TTL）
│    glb.py / obj.py / stl.py   网格读写（GLB 容器 → 三角面 → 二进制 STL）
│    asset.py    资产入账：sha256 + 三角数 + bbox → asset_id
│    urdf.py     collision mesh 注入（tool 链接指向生成的 STL）
│    robot.py    URDF → 页面模型（links / joints / 限位 / 几何，非法即报错）
│    bench.py    引擎命令拼装 + 运行
│    pipeline.py run 目录、命令、eval_report 解析、run 列表、帧路径校验
│    verify.py   两份回执逐位对拍，报第一处分歧路径
│    cli.py      generate / ingest / urdf / bench / verify / demo / serve / robot
│
└─ 外部引擎（subprocess，cwd = 引擎 checkout）
     cargo run -p oriera-agent --example embodied_sim_collect
       --features rigid3d,cam,spec,urdf -- <引擎参数>
     产物：eval_report.json / fingerprints.v2.json / robot.json /
           lerobot-v21/（JSONL + 相机 PNG）/ run_manifest.json
```

**数据流一句话**：prompt → Lux3D GLB → 入账（asset_id）→ STL → 注入 URDF 的 tool 碰撞位 →
引擎按 URDF 跑采集 → 每集指纹 + eval_report → 页面把两份文件读回来。

---

## 3. 文件地图

| 路径 | 作用 | 改动频率 |
|---|---|---|
| `bridge.py` | 启动器，把 `src/` 塞进 `sys.path`，`python bridge.py <cmd>` | 几乎不动 |
| `src/lux3d_bridge/aholo.py` | Lux3D 协议：网关、裸 key、信封、整数状态、下载 | 低（网关变了才动） |
| `src/lux3d_bridge/asset.py` | **身份的核心**：`asset_id = sha256(source_sha256, stl_sha256, tris, bbox, pipeline_version)[:16]` | 低，改了等于换身份 |
| `src/lux3d_bridge/pipeline.py` | 一次训练 run 的全部磁盘约定（见 §5） | 中 |
| `src/lux3d_bridge/server.py` | API + 两个 runner + 静态服务 | 高 |
| `src/lux3d_bridge/robot.py` | URDF → JSON（只支持本仓库用到的子集，越界就报错） | 低 |
| `web/js/main.js` | 页面状态机（生成/训练/回执、轮询、结果渲染） | 高 |
| `web/js/{arm,scene,stl}.js` | 3D 相关 | 低 |
| `tests/test_pipeline.py` | 原始 17 条：GLB/STL/URDF 注入/命令/回执/线格式 | 稳定 |
| `tests/test_viewer.py` | 21 条：URDF 模型、API 路由、dry-run job、HTTP 层 | 中 |
| `tests/test_app_flow.py` | 7 条：run 准备、训练 job 生命周期、结果解析、帧路径安全 | 中 |
| `docs/APP.md` | **应用的主文档**：三步、训练命令逐参数、API 表、边界 | 跟着改动走 |
| `docs/AHOLO-API.md` | Lux3D 实测协议（踩过的坑都在这） | 低 |
| `docs/BENCH-COMMAND.md` | 把资产交给引擎的命令与两个实测坑 | 低 |
| `docs/PRODUCT.md` | 产品目标、度量、路线图 | 低 |
| `submission/` | 黑客松报告 / 演示脚本 / 小红书稿 | 低 |
| `demo/shots/` | 真实运行截图与短视频（README 引用） | 低 |

---

## 4. 环境（本机实际版本）

| 东西 | 版本 / 路径 | 备注 |
|---|---|---|
| Python | 3.13.13 | 只用标准库，无 pip 依赖 |
| Node | 24.16.0 | 只在手工跑 `node --test` 时用到，不是运行依赖 |
| Rust | 1.100.0，工具链 `stable-x86_64-pc-windows-gnu` | 引擎要求 ≥ 1.87 |
| GCC | MinGW-w64 16.2.0（winlibs，msvcrt）`C:\Users\DW\winlibs\mingw64\bin` | 没有 MSVC，见下 |
| 引擎 | `C:\Users\DW\repos\3D-engine`（OriEra 0.13.0） | 首次编译约 2m55s |
| 浏览器 | Edge 157（含 SwiftShader，可无头截图） | 只影响截图，不影响功能 |

**为什么用 gnu 而不是 msvc**：这台机器没有 Visual Studio / MSVC 链接器，`cargo` 默认 msvc 工具链
会 `link.exe not found`。装了 `stable-x86_64-pc-windows-gnu` + winlibs GCC 后即可编译运行。
关键两步：

```powershell
$env:Path = "C:\Users\DW\winlibs\mingw64\bin;$env:USERPROFILE\.cargo\bin;$env:Path"
$env:RUSTUP_TOOLCHAIN = "stable-x86_64-pc-windows-gnu"
```

`RUSTUP_TOOLCHAIN` 只对被 serve 起来的子进程生效，不改全局默认（全局仍是 msvc）。
**换机器/换 shell 必须重设这两个变量，否则「训练」会以 `linker link.exe not found` 失败。**

---

## 5. 磁盘约定：`out/` 里有什么

`out/` 整个在 `.gitignore` 里（约 57.7 MB），删了不影响代码，但会丢本地证据。

```
out/
├── live/                      # 第一次真实链路（2026-11-19，Linux 跑的 + 本机回执）
│   ├── assets.lock.json       # 资产台账
│   ├── crate_live.glb/.stl    # 那份 10.3 MB 的 Lux3D 产物与转换结果
│   ├── fingerprints.v2.json   # 回执（本机可复核）
│   └── fingerprints.linux.json# 另一台机器的回执（用于 MATCH 演示）
├── demo/meshes/               # bridge.py demo 的离线产物（cube）
├── webgen/                    # 页面「生成」写这里
│   ├── assets/<name>.glb
│   └── meshes/<name>.stl + assets.lock.json
└── app/runs/<run-id>/         # 页面「训练」写这里，一次 run 一个目录
    ├── run.json               # 请求 + 完整命令 + 结果摘要 + 状态
    ├── urdf/desk_arm.urdf     # tool 碰撞位 = 这次生成的 STL
    ├── meshes/<name>.stl
    ├── robot.json             # 引擎解析出的 RobotSpec
    ├── eval_report.json       # 每集 seed/steps/reward + 成功率 + Wilson
    ├── fingerprints.v2.json   # 每集回执
    └── lerobot-v21/           # 数据集：data/ + meta/ + observation.images.*/episode_*/frame_*.png
```

- 现有两次 run：`20261119-060737-crate_web`（只准备）、`20261119-061746-crate_web`（真跑完）。
- run 目录可以用 `LUX3D_RUNS_DIR=<绝对路径>` 挪走（测试就是这么隔离的，不会污染 `out/app/runs`）。
- `out/` 是证据，不是源码；清空后重跑 `demo` + 一次生成/训练即可重建。

---

## 6. 已经验证过什么（都可以复跑）

| 事实 | 证据 |
|---|---|
| 45 条离线测试全过 | `python -m unittest discover -s tests` |
| CI 在 ubuntu 3.10/3.13 + windows 3.13 全绿 | GitHub Actions `tests`，commit `169a140` |
| 真实 Lux3D 任务 | `taskId 8396881`，G1-Turbo，faceCount 10000 → `crate_web.glb` 9.5 MB → **7,290 面，bbox 0.600 × 0.464 × 0.547 m，asset `55d204cf9851ca74`** |
| 真实训练 run（页面点出来的） | `20261119-061746-crate_web`：2 集 · 1400 transitions · 0/2 · `✓ 确定性复核（full）：双跑逐位一致` · spec_hash `b84b3add08d4a9c3` · 45 steps/s |
| 引擎构建 | gnu 工具链，2m55s；训练单跑 31s |
| 跨机器回执一致（历史） | `verify` 对 Linux/Windows 两份回执 MATCH（见 README） |

**0% 成功率不是 bug**：2 自由度桌面臂 + `home` 全零，够不到台面。它恰恰证明了回执是诚实的——
一个能报 0% 且两次逐位一致的系统，才有资格报 73%。

---

## 7. 排错速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `linker link.exe not found` | 用了 msvc 工具链 | 设 `RUSTUP_TOOLCHAIN=stable-x86_64-pc-windows-gnu`，PATH 加 `C:\Users\DW\winlibs\mingw64\bin` |
| 页面显示「engine missing」 | serve 没带 `--engine`，也没设 `ORIERA_ENGINE` | `python bridge.py serve --engine C:\Users\DW\repos\3D-engine` |
| 「开始训练」点了没反应/409 | 同上，或资产没有 STL | 先看 `/api/config` 的 `engine.ok` |
| 生成返回 409 `no AHOLO_API_KEY` | `.env` 没 key 且没勾 dry-run | 把 key 写进 `.env`（**不要提交**），或勾 dry-run |
| 生成 401 | 用了 `Authorization: Bearer <key>` | 必须裸 key（`docs/AHOLO-API.md` §2） |
| 下载 403/404 | Lux3D 产物 URL 约 2 小时过期 | 重新生成；URL 不可续期 |
| 训练日志里 `spec_hash` 没变但结果变了 | 你改了引擎或 URDF 的物理字段却没重算 | 正常：spec_hash 只覆盖 spec 内容；回执变了说明物理变了 |
| `python -m unittest` 在 Windows 上莫名失败 | 工作树被 CRLF 污染（有测试按 LF 源码做正则） | `git config core.autocrlf false` 后重新检出 |
| 服务起来了但页面空白 | 直接双击了 `index.html`（file://）而不是走 8137 | 页面依赖本地 API，必须经服务访问 |
| 想清掉所有生成物 | — | 删 `out/webgen`、`out/app/runs`（保留 `out/live` 可保住历史证据） |

---

## 8. 已知边界（接手先读这段）

1. **生成的资产进的是 tool 碰撞位，不是被操作的物体。** 引擎的 `scene.props[]` 只支持
   `cuboid`/`sphere`/`capsule`，没有 mesh prop。所以 Lux3D 的箱子改变的是夹具/工具的接触几何，
   台面上那颗方块仍是预设物。这条是当前最大的产品缺口。
2. **没有 PPO 训练**。跑的是 `embodied_sim_collect`（脚本策略采集 + 回执），不是 `train_ppo.py`。
   后者需要 stable-baselines3 + maturin 构建的 PyO3 wheel，本机没装。
3. **一次一个 job**。服务端能并发，但页面只维护一个进度；Lux3D 的并发上限未公开，不要按无限设计。
4. **取消只是 `terminate()`**。会留下半成品 run 目录，状态标 `failed`，不会被当成成功。
5. **`out/` 不入库**，所以新克隆没有资产、没有 run；README 截图是证据，不是可加载状态。
6. **引擎不 vendor**。换引擎版本等于换实验环境：先跑一次 `verify` 看回执是否还一致。
7. **Windows 首次编译慢**（约 3 分钟），之后增量几秒；不要在训练超时里按首次编译估时间。
8. **three.js 是 vendored 的**（r169，MIT）。升级要同时看 `GLTFLoader` 与 `BufferGeometryUtils` 的版本匹配。

---

## 9. 下一步路线（按性价比排序）

| 优先级 | 事情 | 为什么 | 大概工作量 |
|---|---|---|---|
| P0 | **mesh prop 或 bbox prop** | 让「生成的物体」真的成为被操作对象，补上 §8.1 的缺口 | 引擎侧加 mesh prop 是最正的路（本仓库不 fork）；临时方案：用 bbox 生成 cuboid prop，UI 标注「近似」 |
| P1 | 把 `asset_id` 折进 `spec_hash` | 现在资产身份只在注释/目录名里，换资产时 spec_hash 不变；要让它进哈希才叫「身份进入实验」 | 中（引擎侧 spec 字段 + 本仓库写 spec） |
| P1 | 一次 run 多资产 | 料箱 + 抽屉 + 台面同场景，`assets.lock.json` 升场景级清单 | 中 |
| P2 | 训练曲线 | 目前只有终局指标；把每集 reward 画成图（`eval_report.runs[]` 已有数据） | 小 |
| P2 | PPO 真训练 | 接 `scripts/train_ppo.py` + gym 桥，页面显示训练曲线 | 大（依赖 wheel） |
| P3 | 3DGS/「既看又算」 | `docs/PRODUCT.md` M3；视觉资产与碰撞资产同源不同几何 | 大 |
| P3 | 打包成桌面应用 | 现在 `serve` + 浏览器；Studio 方向 | 大 |

---

## 10. 关键决策记录（别在不知情时推翻）

- **为什么不 fork 引擎**：产品的卖点是「记账层」，不是仿真能力。fork 会让回执失去第三方可信度。
- **为什么 GLB 一定要转 STL**：上游引擎对 GLB 只做探针，碰撞走 STL 凸包。直接喂 GLB 会退化成连杆包络。
- **为什么 `asset_id` 不包含文件名**：文件名会变（`crate_live.glb` vs `lux3d_task8389570_0.glb`），
  身份必须由内容决定；服务端用 sha256 反查文件，不靠名字。
- **为什么 viewer 变成了 app**：只有「看」证明不了任何事；三步里每一步都必须留下一个可复核的文件。
- **为什么页面里没有「假进度条」**：训练进度来自子进程 stdout + `eval_report`，没有数据就显示没有。
- **为什么 `out/` 不入库**：资产是「这一次」的证据，入库会变成「某一次」的装饰；仓库里只留可复现的代码和截图。

---

## 11. 接手清单（第一个小时）

- [ ] `python -m unittest discover -s tests` → 45 绿色
- [ ] `python bridge.py demo` → 三件产物
- [ ] 设好 `RUSTUP_TOOLCHAIN` + PATH，`serve --engine ...` → 页面出图
- [ ] 点一次「生成」（dry-run 先跑，确认请求体对；有 key 再真跑）
- [ ] 点一次「训练」→ 看到 `双跑逐位一致`，打开 `out/app/runs/<run>/eval_report.json` 对一遍页面数字
- [ ] 读 `docs/APP.md` §3（边界）与 §4（API）
- [ ] 决定先做路线图哪一项（建议 P0/P1）

---

## 12. 凭证与安全

- `AHOLO_API_KEY` 只在 `.env`（已 gitignore）。**不要贴进代码、issue、截图。**
- 服务默认只绑 `127.0.0.1`，不要 `--host 0.0.0.0` 暴露到局域网。
- Lux3D 产物 URL 约 2 小时过期；下载后立刻落盘 + 入账，不要「先记 URL 稍后再说」。
- 生成的 GLB 可能很大（本次 9.5 MB），`.gitattributes` 已把 `.rgba` 标成 binary；新增大文件前先想清楚是否该入库。
