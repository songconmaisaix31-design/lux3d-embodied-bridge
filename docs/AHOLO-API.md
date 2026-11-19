# Aholo Lux3D · API 接入文档

> 全部字段来自 2026-11-19 的实跑（`taskId 8389570` / `8389873` / `8390507`），并与官方 Python SDK
> `manycore-aholo-sdk-lux3d` 的源码逐条对齐。凡是文档没写、我们也没实测到的，本文明确写「未知」，
> 不猜。实现代码在 `src/lux3d_bridge/aholo.py`，可执行入口是 `python bridge.py generate`。

---

## 1. 五分钟接入

```bash
# 1) 把 key 放进 .env（不要进代码、不要进 git）
echo "AHOLO_API_KEY=<你的 key>" > .env

# 2) 只校验请求体，不花钱、不排队
python bridge.py generate --prompt "a small wooden crate" --dry-run

# 3) 真跑：创建 → 轮询 → 下载 → 入账
python bridge.py generate \
  --prompt "a small wooden shipping crate with metal corner brackets, single object, centered" \
  --version G1-Turbo --face-count 20000 --format glb --name crate --out out/live/assets
```

跑完会得到三样东西：`out/live/assets/<name>.glb`、`assets.lock.json` 里的一条
`asset_id`（源文件 sha256 的前 16 位），以及一份可提交给仿真台的 STL。

---

## 2. 网关与鉴权

| 项 | 值 |
|---|---|
| 国际网关 | `https://api.aholo.ai`，路径带 `/global` 前缀 |
| 国内网关 | `https://api.aholo.cn`，路径**不带** `/global` |
| 鉴权头 | `Authorization: <key>` |
| 禁止 | `Authorization: Bearer <key>` —— 带 Bearer 会被 401，这是实测踩过的坑 |
| 请求体 | `Content-Type: application/json` |

```http
POST /global/lux3d/v1/generate/text-to-3d/task/create HTTP/1.1
Host: api.aholo.ai
Authorization: <key>
Content-Type: application/json
```

切换网关是换 base URL，不是换一套协议；本仓库用 `--profile global|cn` 暴露这个开关。

---

## 3. 统一信封

所有业务接口都返回同一个信封，**先看 `c`，再读 `d`**：

```json
{ "c": "0", "m": "", "d": { } }
```

- `c` 是字符串，`"0"` 才代表业务成功。非 `"0"` 时 `m` 是人话错误信息，**不要重试**，它不会自己好。
- `d` 是真正的载荷。少数端点直接返回裸值（见 §4.1），不要假设它一定是对象。
- 容错写法：先判断 `"c" in body`，没有就当裸响应处理；`c == "0"` 但 `d` 为空同样按失败处理，
  不要用空载荷继续往下走。

---

## 4. 端点

### 4.1 创建任务

```
POST {base}/global/lux3d/v1/generate/text-to-3d/task/create
POST {base}/global/lux3d/v1/generate/img-to-3d/task/create
```

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `version` | string | 是 | `G1` 或 `G1-Turbo`。G1-Turbo 出图更快，质量略低 |
| `prompt` | string | 文生必填 | 一句英文描述最稳：主体 + 结构 + 「single object, centered」 |
| `img` | string | 图生必填 | 图片地址，或 `data:image/png;base64,...` 内联（免上传，适合本地图） |
| `faceCount` | int | 是 | 面数预算，实测可用区间 `10000`–`300000`；`20000` 是桌面物件的甜点 |
| `outputFormat` | string[] | 是 | `["glb"]` \| `["zip"]` \| `["ply"]`；`glb` 带 PBR 材质，是最通用的选择 |
| `enablePbr` | bool | 否 | 默认 `true`，关掉能减小体积 |
| `style` | string | 否 | `photorealistic` / `cartoon` / `anime` / `hand_painted` / `cyberpunk` / `fantasy` / `glass` |

响应：

```json
{ "c": "0", "m": "", "d": 8390507 }
```

**`d` 是一个裸整数 taskId**，不是 `{"taskId": ...}`。这是接入时最容易写错的一处：
按对象去解析会直接 `KeyError`。稳妥的写法是把 `d` 当 `int | str | object` 三种都可能，
对象形态再去深搜 `taskId` / `task_id` / `id`。

### 4.2 查询任务

```
GET {base}/global/lux3d/v1/generate/task/get?taskid=<id>
```

响应（节选，字段按实测）：

```json
{
  "c": "0",
  "d": {
    "taskId": 8390507,
    "status": 3,
    "outputs": [
      { "content": "https://qhsmodel.kujiale.com/.../tex_mesh.glb" }
    ]
  }
}
```

### 4.3 任务列表

```
GET {base}/global/lux3d/v1/generate/task/list?page=1&pagesize=10&status=3
```

用于核对 key 的可用性、查历史任务的 id——比「再创建一次」便宜得多。`status` 可省略。

---

## 5. 状态机

| `status` | 含义 | 该做什么 |
|---|---|---|
| `0` | 已创建 / 排队 | 等，继续轮询 |
| `1` | 生成中 | 等，继续轮询 |
| `3` | **成功** | 取 `outputs[n].content` 立刻下载 |
| `4` | 失败 | 读 `message`，不要重试同一个请求 |
| `6` | 已取消 | 停止轮询 |

两个必须注意的点：

1. **它是整数，不是字符串**。拿 `"success"` 去比会永远判不中；反过来 SDK 某些版本会给字符串，
   所以实现里两套都认，但以整数为主。
2. **成功 ≠ 一定能拿到产物**。`status=3` 但 `outputs` 为空是可能的，要显式报错，
   不要静默返回 `None` 让下游以为「下载成功但文件是空的」。

---

## 6. 下载与文件生命周期

- 地址在 `d.outputs[n].content`，`outputs` 是数组，按扩展名挑（`.glb` / `.ply` / `.obj`）。
- **有效期约 2 小时**（实测）。所以正确顺序是：拿到 URL → 立刻落盘 → 再入账，
  不要「先记下 URL 稍后再说」。本仓库下载后立刻算 sha256 并写进 `assets.lock.json`。
- `outputFormat` 为 `zip` 时会得到压缩包，包里通常有一个 `tex_mesh.glb`；实现里会自动解出
  第一个 GLB/OBJ/PLY，原始 zip 保留。
- 下载超时建议 180s（10 MB 级文件在普通网络下 5–20s）。

---

## 7. 轮询策略（实测数字）

| 参数 | 建议值 | 依据 |
|---|---|---|
| 首次轮询延迟 | 10 s | 太快只是打空气 |
| 轮询间隔 | 10 s | 状态变化粒度在分钟级 |
| 总等待上限 | 1800 s | `faceCount 20000` 实测排队+生成约 10–15 分钟 |
| 单次 HTTP 超时 | 60 s | 网关偶发慢响应 |

失败要区分「暂时」和「永久」：`HTTP 5xx` / 网络错误可以退避重试；`401`（key 错）、
`c != "0"`（业务拒绝）、`status = 4`（生成失败）都不该重试。

---

## 8. 一份可直接跑的 Python（不依赖 SDK）

```python
import json, os, time, urllib.request

BASE = "https://api.aholo.ai"
KEY = os.environ["AHOLO_API_KEY"]          # 只用裸 key

def call(method, path, payload=None):
    body = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(BASE + path, data=body, method=method)
    req.add_header("Authorization", KEY)   # 不加 Bearer
    if payload:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        env = json.loads(r.read())
    if "c" in env:
        if str(env["c"]) != "0":
            raise RuntimeError(f"c={env['c']} {env.get('m')}")
        return env["d"]
    return env

task_id = call("POST", "/global/lux3d/v1/generate/text-to-3d/task/create", {
    "version": "G1-Turbo", "prompt": "a small wooden crate",
    "faceCount": 20000, "outputFormat": ["glb"], "enablePbr": True,
})

deadline = time.time() + 1800
while time.time() < deadline:
    task = call("GET", f"/global/lux3d/v1/generate/task/get?taskid={task_id}")
    status = task["status"]
    if status == 3:
        url = task["outputs"][0]["content"]
        urllib.request.urlretrieve(url, "crate.glb")   # 2 小时有效，立刻下
        break
    if status in (4, 6):
        raise RuntimeError(f"task {task_id} ended status={status}")
    time.sleep(10)
```

同样的逻辑在本仓库里是 `aholo.create_task / fetch_task / wait_for_artifact / download` 四个函数，
每个都能单独替换，`--base-url` / `--create-path` / `--poll-path` 三个参数让网关变更只是配置项。

---

## 9. 已知边界（写出来，不藏）

- **配额与并发上限未公开**。实测同一 key 连续创建三个任务（`8389570` → `8389873` → `8390507`）
  均正常；更高并发没有验证，不要按「无限」设计。
- **不同入口（SDK / 裸 HTTP / 控制台）字段名不完全一致**，所以本仓库允许 `--payload-json` 覆盖
  整个请求体，不假设自己的字段名永远对。
- **模型不会返回碰撞信息**。GLB 只有视觉网格，物理属性（质量、摩擦、凸包）必须由下游决定，
  这是本仓库转 STL 并交给引擎做 `ConvexHull` 的原因。
- **没有「查询生成进度百分比」**，只有状态机。
- 生成的 URL 域名是 `qhsmodel.kujiale.com`，说明产物落在群核的对象存储上；这个域名若变更，
  只影响下载，不影响任务协议。

---

## 10. 排错速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `HTTP 401` | 带了 `Bearer` | 改成裸 `Authorization: <key>` |
| `KeyError: 'taskId'` | 把 `d` 当对象 | `d` 可能是裸整数 |
| 永远等不到成功 | 用字符串比状态 | 状态是整数 `3` |
| 下载 403/404 | 超过 ~2h | 重新生成，URL 不可续期 |
| `c != "0"` 但无 `m` | 服务端业务异常 | 记录 `c`，换参数再试一次即可，不要循环重试 |
| 请求体被拒（faceCount 越界） | 超出 `10000`–`300000` | 回到 20000 再调 |
