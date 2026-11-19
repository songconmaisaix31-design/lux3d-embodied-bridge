# Aholo Lux3D：从产物到可入账的资产

本文件只记录**这个桥需要知道的部分**。第 2–5 节的端点、鉴权、状态枚举和字段名，
已于 2026-11-19 用真实请求核对（并对照官方 SDK `manycore-aholo-sdk-lux3d` 的源码），
不是从网页示例里猜的。

## 1. 产物形态

| 格式 | 用途 | 本桥的处理 |
|---|---|---|
| `GLB` | 标准网格 + PBR 材质 | 直接解析（`glb.py`） |
| `ZIP` | 打包的 OBJ / 贴图，或单 GLB | 自动解包取第一个 GLB/OBJ/PLY |
| `OBJ` | 传统网格，常在 ZIP 里 | 直接解析（`obj.py`） |
| `PLY` | 3DGS 高斯，给 Viewer 用 | **不转 STL**：不是面片，进不了碰撞体 |
| `FBX` / `USDZ` | 影视/DCC 管线 | 不在本桥范围，报错并点名格式 |

> 判断依据：碰撞体需要**封闭面片**。3DGS 的 PLY 是渲染表达，不是几何表达；塞进
> `<collision>` 只会得到一个没有意义的包络。

## 2. 真实网关与鉴权（已验证）

| profile | 基址 | 鉴权头 | 说明 |
|---|---|---|---|
| `global`（默认） | `https://api.aholo.ai` | `Authorization: <key>` | 国际区，路径前缀 `/global` |
| `cn` | `https://api.aholo.cn` | `Authorization: <key>` | 国内区，路径前缀 `/lux3d/v1` |

**不要加 `Bearer`**。加了的请求会直接 401；这是最常见的失败原因。

已验证的端点（`global` 口径）：

| 动作 | 方法 | 路径 |
|---|---|---|
| 文本生 3D | POST | `/global/lux3d/v1/generate/text-to-3d/task/create` |
| 图生 3D | POST | `/global/lux3d/v1/generate/img-to-3d/task/create` |
| 查任务 | GET | `/global/lux3d/v1/generate/task/get?taskid=<id>` |
| 任务列表 | GET | `/global/lux3d/v1/generate/task/list?page=1&pagesize=20&status=3` |

响应是命令信封：`{"c": "0", "m": "...", "d": <payload>}`。`c != "0"` 是业务错误，
不是 HTTP 错误，所以只看 HTTP 状态码会漏掉失败。

## 3. 请求体

```json
{
  "prompt": "a small wooden shipping crate, single object, centered",
  "version": "G1-Turbo",
  "style": "photorealistic",
  "outputFormat": ["glb"],
  "faceCount": 30000,
  "enablePbr": true
}
```

- `version` **必填**：`G1` / `G1-Turbo`。
- `faceCount` 范围 10,000 – 300,000，默认 200,000；对 PLY 无效。
- `outputFormat` 支持 `zip` / `glb` / `ply`；`enablePbr` 影响 G1-Turbo 的 ZIP/GLB 材质。
- 文本生 3D 的 `style`：`photorealistic`（默认）/ `cartoon` / `anime` /
  `hand_painted` / `cyberpunk` / `fantasy` / `glass`。
- 图生 3D 用 `img`（URL 或 `data:<mime>;base64,...`）或 `imgs`（1–32 张）；
  `--image-url` 传本地路径时，本桥自动转成 data URL，**不需要先上传对象存储**。

```bash
# 只打印请求体，不发请求
python bridge.py generate --prompt "a ribbed wooden crate" --name crate --dry-run

# 真跑（key 放在 .env 或环境变量里，别写进命令行历史）
python bridge.py generate --prompt "a ribbed wooden crate" --name crate \
  --version G1-Turbo --face-count 30000 --format glb
```

## 4. 轮询与下载（已验证）

- `status` 是**整数**，不是字符串：`0` init、`1` running、`3` success、
  `4` failed、`6` canceled。字符串枚举属于 World API，别混用。
- 轮询建议 10–15 秒一次；本桥默认 `--interval 10`、`--max-wait 1800`。
- 成功后，下载地址在 `d.outputs[n].content`，**有效期约 2 小时**。过期就只能重新生成，
  所以 `generate` 会立刻落盘，`ingest` 用的是本地字节。
- 创建成功即返回 `taskId`（数字）。`outputs` 在任务完成前是空的。

## 5. 实测记录（2026-11-19，可复现）

| 项 | 值 |
|---|---|
| taskId | `8389570` |
| 请求 | `text-to-3d`, `G1-Turbo`, `faceCount=30000`, `style=photorealistic` |
| prompt | "a small wooden shipping crate with metal corner brackets, single object, centered" |
| 结果 | `status=3`，约 2 分 15 秒（13 次轮询） |
| 产物 | `tex_mesh.glb`，9,756,632 字节 |
| 转 STL 后 | 21,874 三角面，包围盒 `0.600 × 0.511 × 0.535 m` |
| asset_id | `474c2fd22f0d3f9f` |
| 注入 URDF | `package://generated/crate_lux3d.stl`，`link tool` |

关键点：这个 GLB 有 21,874 面，而引擎的 `ConvexHull` 只吃 STL，所以「GLB → STL」
不是可选的装饰步骤，是让生成物真正进到物理管线里的唯一通道。

## 6. 控制台手动导出（退路）

时间紧、或网关字段又变了时：

1. 控制台跑一次生成，导出 **GLB**。
2. `python bridge.py ingest path/to/model.glb --name crate --lux3d`
3. 后续注入、生成命令与 API 路径完全一致——`assets.lock.json` 里的 `asset_id` 也一样。

## 7. 资产账本

`assets.lock.json` 每次 ingest 都会更新：

```json
{
  "schema": "lux3d-asset-lock/v1",
  "assets": {
    "crate_lux3d": {
      "asset_id": "474c2fd22f0d3f9f",
      "source_sha256": "…", "stl_sha256": "…",
      "triangles": 21874,
      "bbox_m": {"min": […], "max": […], "size": [0.600, 0.511, 0.535]},
      "provenance": {"tool": "Aholo Lux3D", "task_id": "8389570", "source_url": "…"}
    }
  }
}
```

`asset_id` 只由「源字节哈希 + STL 哈希 + 三角数 + 包围盒 + 管线版本」决定，同一份输入永远
得到同一个 id——这是回执能对得上资产的前提。

## 8. Key 安全

- `.env` 已被 `.gitignore` 忽略；仓库里只有 `.env.example`（空值）。
- `aholo.py` 只读环境变量，不会写盘。
- key 一旦出现在公开仓库、截图或聊天里，就该去控制台轮换一次。
