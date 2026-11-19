# Aholo Lux3D：从产物到可入账的资产

本文件只记录**这个桥需要知道的部分**，并以 2026-11-19 的公开文档为准。字段名在不同入口
之间并不统一，所以所有参数都可覆盖——不要靠猜。

## 1. 产物形态

Lux3D 的导出格式随管线与任务参数变化，公开文档列出的是：

| 格式 | 用途 | 本桥的处理 |
|---|---|---|
| `GLB` | 标准网格 + PBR 材质，网页/引擎通用 | 直接解析（`glb.py`） |
| `ZIP` | 打包好的 OBJ / 贴图，或单 GLB | 自动解包取第一个 GLB/OBJ/PLY |
| `OBJ` | 传统网格，常在 ZIP 里 | 直接解析（`obj.py`） |
| `PLY` | 3DGS 点/高斯，给 Viewer 用 | **不转 STL**：它不是面片，进不了碰撞体 |
| `FBX` / `USDZ` | 影视/DCC 管线 | 不在本桥范围，报错并点名格式 |

关键参数（Lux3D 生成侧）：

- `version`：`G1` / `G1-Turbo`（Turbo 更快，`enablePbr` 控制材质）。
- `faceCount`：10,000 – 300,000，默认 200,000；对 PLY 无效。
- `outputFormat`：`zip` / `glb` / `ply` 为基础输出，其余格式按任务可选。

> 判断依据：碰撞体需要**封闭面片**。3DGS 的 PLY 是渲染表达，不是几何表达；把它塞进
> `<collision>` 只会得到一个没有意义的包络。

## 2. 两个入口

| profile | 基址 | 鉴权头 | 说明 |
|---|---|---|---|
| `labs` | `https://labs.aholo3d.com` | `Authorization: <key>`（**不加 Bearer**） | 网关风格，路径形如 `/global/lux3d/v1/generate/{mode}/task/create` |
| `lux3d` | `https://api.lux3d.com` | `Authorization: Bearer <key>` | 生成接口风格，`POST /v1/generations` |

`{mode}` 取 `text-to-3d` 或 `img-to-3d`。

```bash
# 打印请求体，不发送任何东西
python bridge.py generate --mode text-to-3d --prompt "a ribbed wooden crate" --name crate --dry-run

# 真跑：key 从环境变量取，不要写进命令行历史
export AHOLO_API_KEY=...
python bridge.py generate --mode img-to-3d --image-url https://.../ref.png --name crate --profile labs
```

认证与路径都能改：

```bash
python bridge.py generate ... \
  --base-url https://labs.aholo3d.com \
  --create-path /global/lux3d/v1/generate/img-to-3d/task/create \
  --poll-path   /global/lux3d/v1/generate/tasks/{task_id}
```

`--payload-json body.json` 直接替换整个请求体。当控制台显示的字段与默认不一致时，用它，
不要改代码。

## 3. 默认请求体

```json
{
  "version": "G1",
  "outputFormat": ["glb"],
  "faceCount": 200000,
  "enablePbr": true,
  "prompt": "a ribbed wooden crate",
  "img": "https://.../ref.png"
}
```

`prompt` 与 `img` 二选一或同时给（多视图上传用 `imgs`，需要 `G1` 的 Add More Views）。
若服务端只认 `image_url` / `output_format`，用 `--payload-json` 覆盖。

## 4. 轮询与下载

- 创建返回 `202` 与 `task_id`；本桥用深度优先搜索在响应里找 `task_id` / `taskId` / `id`，
  找不到就报错，不静默重试。
- 轮询直到状态落在 `success / succeeded / completed / done / finished / ready`，或
  `failed / error / cancelled / canceled / timeout`（后者直接失败）。
- 产物 URL 从响应里递归找第一个 `.glb/.obj/.zip/.ply/.usdz/.fbx` 链接；`ZIP` 会自动解包。
- 默认 `--interval 10`、`--max-wait 1800`。生成大网格时把 `--max-wait` 拉长，别靠重跑。

## 5. 控制台手动导出（最稳的退路）

截止时间紧张时，不要和 API 字段名较劲：

1. 在 Aholo 控制台跑一次生成，导出 **GLB**。
2. `python bridge.py ingest path/to/model.glb --name crate --lux3d`
3. 生成 + 注入 + 打印命令，三步和 API 路径完全一样。

这条路径没有轮询、没有字段名争议，报告书里也照样能写「资产由 Lux3D 生成，sha256 为 …」。

## 6. 资产账本

`assets.lock.json` 每次 ingest 都会更新：

```json
{
  "schema": "lux3d-asset-lock/v1",
  "assets": {
    "crate": {
      "asset_id": "ca7b4be0429c2804",
      "source_sha256": "…", "stl_sha256": "…",
      "triangles": 12,
      "bbox_m": {"min": [-0.5, -0.5, -0.5], "max": [0.5, 0.5, 0.5], "size": [1.0, 1.0, 1.0]},
      "provenance": {"tool": "Aholo Lux3D", "task_id": "…", "source_url": "…"}
    }
  }
}
```

`asset_id` 只由「源字节哈希 + STL 哈希 + 三角数 + 包围盒 + 管线版本」决定，同一份输入永远
得到同一个 id。这是回执能对得上资产的前提。

> 注意：不要提交 `AHOLO_API_KEY`。仓库里没有任何地方会写盘保存 key。
