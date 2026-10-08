# 在 Codex / Claude 中使用 Flow2API

本项目的 MCP 入口是 `agent_mcp.py`，使用标准输入/输出（stdio）提供四个媒体工具。它调用已运行的 Flow2API 服务，不启动浏览器或主服务，不是聊天推理模型。

## 当前验证范围

- 已做离线 HTTP 客户端、图片读写、模型/输入拒绝、错误脱敏、重定向与公网地址限制测试。
- 已通过官方 MCP SDK 的独立进程协议测试：`initialize`、`list_tools`、`call_tool`。该测试只连接本机临时模拟服务，不接触 Google 账号。
- Nano Banana 2.1 文生图与 Omni 1.1 Flash 文生视频已有独立的 `personal` 浏览器原生路径，提交前选择并读回模型、比例、数量、费用和视频清晰度/时长；不把旧 RPC 模型改名。**尚未完成真实生成、Codex/Claude 全局注册和真实工具验收。**
- 模型以 `list_models` 当时返回的目录为准。`available` 表示当前服务配置允许请求；`verification_state` 是验证状态，不能把“允许请求”理解为真实出图已通过。

截至 2026-10-08，Google 已公布 [Flow 的 Nano Banana 2.1](https://support.google.com/flow/answer/16352836?hl=en)，且已在登录后的实际 Flow 页面核对该模型选项。实现方 useapi.net 的 [10 月 5 日记录](https://useapi.net/docs/changelog#october-5-2026) 提到新输出的 `modelNameType=BELUGA`，但本项目尚未取得它作为 Flow 请求参数的直接证据。因此原生路径通过网页选项提交，不发送猜测的 `BELUGA` 或旧 `NARWHAL` 请求键。

## 环境准备

先用项目专用虚拟环境安装 `requirements-agent.txt`，再按主 README 配好并启动 Flow2API HTTP 服务。MCP 进程和 HTTP 服务是两个进程；MCP 不负责账号登录。

以下绝对路径是占位示例，请替换成自己的实际路径。已有 uv 时，可创建专用 Python 3.12 环境：

```sh
cd '/absolute/path/to/flow2api/repo'
uv venv --python 3.12 ../venv
uv pip install --python ../venv/bin/python -r requirements-agent.txt
```

当前服务任务执行只支持**单 worker**，启动时不要增加 `--workers`，也不要让多个服务进程共用同一任务数据库。MCP 客户端可以分别连接这个服务。

### 启动专用原生浏览器服务

先完成代码和网页设置核对；实际连接账号与有费用的生成分别取得用户对本次操作的授权。下面的启动步骤会创建本机私有运行目录并启动专用窗口，不安装软件，不使用日常 Chrome 资料目录。

在仓库中运行：

```sh
../venv/bin/python scripts/run_native.py \
  --private-dir '/absolute/path/to/flow2api/private' \
  --port 8000
```

`--private-dir` 必须在仓库外，首次使用新的空目录。只准备本地目录和随机凭证、不启动浏览器和 HTTP 服务时，在同一命令后增加 `--prepare-only`。需要已安装的 Chrome；macOS 默认可执行文件为 `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`，其他位置或系统通过 `--browser '/absolute/path/to/chrome'` 指定。启动器不会自动安装 Chrome 或依赖。

私有目录内的文件用途如下，不能提交到 Git 或粘贴到聊天：

| 文件 / 目录 | 用途 |
|---|---|
| `service-credentials.json` | 本机管理页凭证：用户名 `local`，首次创建随机密码和服务 API Key；只在本机读取使用。 |
| `api-key.txt` | 供 MCP 的 `FLOW2API_API_KEY_FILE` 读取，不是 Google 登录态。 |
| `browser-profile/`、`flow.db` | 专用浏览器资料及本机账号、设置、任务数据库。 |
| `cache/`、`service.log` | 服务缓存与本机运行日志。 |
| `media/` | 建议作为 MCP 的 `FLOW2API_OUTPUT_DIR`，保存交付文件。 |

服务只监听本机。启动后，在它打开的**专用浏览器窗口**登录 Google Flow 并打开一个已有项目；再访问 [本机管理页](http://127.0.0.1:8000)，使用私有凭证文件中的管理账号登录，点击“连接原生浏览器账号”。如果更改了端口，管理页地址同步更改。只支持一个账号；连接按钮将该窗口的登录态保存到本机，用于之后的请求，不生成素材，不显示或导出凭证。

重启时继续使用同一个私有目录。若数据库中的设置或凭证与专用启动配置不一致，启动明确拒绝；不会为了启动而覆盖已有配置或重新生成密钥。先核对原目录和配置，勿删除数据库或替换私有凭证来绕过错误。

只有可用 Flow 账号、没有第三方验证码服务时，可将服务的验证码模式设为 `personal`，使用下面的原生模型目录。原生模式仍需要本地受控浏览器中的有效 Flow 登录状态；MCP 不自动获取或导出登录凭证。

| 路径 | 当前范围 | 边界 |
|---|---|---|
| `personal` + Nano Banana 2.1 原生 UI | 纯文生图，5 种比例，每次 1 张 | 提交前核对实际选项与费用；尚未做真实生成验收。 |
| `personal` + Omni 1.1 Flash 原生 UI | 纯文生视频，横竖屏，360p/720p，4/6/8/10 秒，每次 1 个 | 提交前读回设置与费用；提交后只读查询进度。各组合和真实生成尚未验收。 |
| 原生参考图编辑 / 参考图视频 | 当前不可用 | 可靠上传尚未实现；请求在账号查询、上传和提交前拒绝。 |
| 已配置第三方验证码服务的现有图片 / 视频 RPC | 保持原接口与参数映射 | 第三方服务及真实生成可能消耗费用或额度；不意味着账号/模型已实测。 |

原生图片和视频默认 `max_credits=0`：只有页面明确显示该次生成需要 0 点数才允许提交。无法读取费用或费用超过请求预算时直接拒绝；不会因为无法确认而默认免费。有费用的测试须先取得用户对该次费用范围的明确授权，再设置预算。预算只能是 0–1000 的整数，并与本次 `request_id` 一起固定；同 ID 改预算会冲突。此字段只约束原生 UI 显示的点数，不是现有第三方验证码或 RPC 通道的费用上限。

费用超限时任务失败，错误码为 `native_credit_limit`，并返回 `credits_shown`（页面点数）与 `max_credits`（本次预算）。此时尚未提交生成。Agent 应把这两个数交给用户决定，不自动增加预算；若用户明确授权新请求，应使用新的 `request_id`，保留原失败任务记录。

### Nano Banana 2.1 原生模型目录

| 模型 ID | 固定比例 |
|---|---|
| `gemini-nano-banana-2.1` / `gemini-nano-banana-2.1-landscape` | 16:9 |
| `gemini-nano-banana-2.1-portrait` | 9:16 |
| `gemini-nano-banana-2.1-square` | 1:1 |
| `gemini-nano-banana-2.1-four-three` | 4:3 |
| `gemini-nano-banana-2.1-three-four` | 3:4 |

这些 ID 使用 `generation_transport=native_ui`，不是 Google 官方 RPC ID。目录中的 `ui_option_observed_generation_not_live_verified` 表示模型选项已在真实页面见到，生成结果仍未实测。每次请求还要在受控浏览器中重新选择并核对；不能仅凭旧观察跳过此次读回。成功响应保留 `native_settings`（模型标签、比例、数量、费用和提交前核对标记），`actual_upstream_model` 仍为 `unknown`，不会将 UI 读回当成底层模型身份验证。

### Omni 1.1 Flash 原生模型目录

ID 格式为 `native-omni-1.1-flash-{方向}-{清晰度}-{时长}s`：方向为 `landscape`（16:9）或 `portrait`（9:16），清晰度为 `360p` / `720p`，时长为 `4` / `6` / `8` / `10`。例如 `native-omni-1.1-flash-landscape-360p-4s` 是横屏、360p、4 秒、单个视频。使用 `list_models` 返回的完整 ID，不自行拼不存在的参数。

这些独立选项已在真实页面核对；所有组合的选择和生成尚未逐个实测，能力字段仍明确 `live_generation_verified=false`、`upstream_model_verified=false`。成功响应保留选定清晰度、时长和费用读回。视频使用网页自己的提交请求，随后沿现有只读状态查询取得媒体；不直接发送猜测的模型键。旧 `omni-1.1-flash-*` ID 继续走已有第三方验证码 RPC 路径。

需要的环境变量：

| 变量 | 含义 |
|---|---|
| `FLOW2API_BASE_URL` | HTTP 服务地址，默认 `http://127.0.0.1:8000`。远程服务要求 HTTPS；不在 URL 中放凭证。 |
| `FLOW2API_API_KEY_FILE` | **推荐**。已存在的专用密钥文件绝对路径；文件只包含服务 API Key，不是 `KEY=VALUE` 格式。不要使用 Google 登录态或管理员密码。 |
| `FLOW2API_API_KEY` | 可选，从进程环境传入服务 API Key；优先于密钥文件。不要把值写进共享配置或命令历史。 |
| `FLOW2API_OUTPUT_DIR` | 必填，生成结果保存目录的绝对路径，建议放在仓库外的专用素材目录。 |

密钥文件建议放在仓库外的私有目录，权限仅当前用户可读。使用上面的专用启动器时，指向它创建的 `api-key.txt` 和 `media/` 即可。下面的 MCP 命令只读取已有密钥文件，**不会创建密钥文件或修改任何现有配置**。准备后可在终端检查 MCP 能启动；启动后等待客户端输入、没有欢迎文字是正常的。

```sh
FLOW2API_BASE_URL='http://127.0.0.1:8000' \
FLOW2API_API_KEY_FILE='/absolute/path/to/flow2api/private/api-key.txt' \
FLOW2API_OUTPUT_DIR='/absolute/path/to/flow2api/media' \
'/absolute/path/to/flow2api/venv/bin/python' \
'/absolute/path/to/flow2api/repo/agent_mcp.py'
```

## Codex 配置示例

以下注册命令根据本机 `codex mcp add --help` 核对。**执行会修改 Codex MCP 配置，确认配置路径后再执行。** 命令只传密钥文件路径，不传密钥值。

```sh
codex mcp add flow2api \
  --env 'FLOW2API_BASE_URL=http://127.0.0.1:8000' \
  --env 'FLOW2API_API_KEY_FILE=/absolute/path/to/flow2api/private/api-key.txt' \
  --env 'FLOW2API_OUTPUT_DIR=/absolute/path/to/flow2api/media' \
  -- '/absolute/path/to/flow2api/venv/bin/python' \
  '/absolute/path/to/flow2api/repo/agent_mcp.py'
```

## Claude 配置示例

Claude Code 可在目标项目中使用下面的 local 配置命令。参数根据 `claude mcp add --help` 核对。

```sh
claude mcp add --scope local --transport stdio \
  --env 'FLOW2API_BASE_URL=http://127.0.0.1:8000' \
  --env 'FLOW2API_API_KEY_FILE=/absolute/path/to/flow2api/private/api-key.txt' \
  --env 'FLOW2API_OUTPUT_DIR=/absolute/path/to/flow2api/media' \
  flow2api -- '/absolute/path/to/flow2api/venv/bin/python' \
  '/absolute/path/to/flow2api/repo/agent_mcp.py'
```

使用接受标准 `mcpServers` JSON 配置的 Claude 客户端时，新增如下条目，保留原有服务器条目：

```json
{
  "mcpServers": {
    "flow2api": {
      "command": "/absolute/path/to/flow2api/venv/bin/python",
      "args": ["/absolute/path/to/flow2api/repo/agent_mcp.py"],
      "env": {
        "FLOW2API_BASE_URL": "http://127.0.0.1:8000",
        "FLOW2API_API_KEY_FILE": "/absolute/path/to/flow2api/private/api-key.txt",
        "FLOW2API_OUTPUT_DIR": "/absolute/path/to/flow2api/media"
      }
    }
  }
}
```

## 工具调用顺序

| 工具 | 输入 | 输出与行为 |
|---|---|---|
| `list_models` | 无 | 获取准确的模型 ID、能力、可用性与验证状态，不生成。 |
| `generate_image` | `model`、`prompt`、`request_id`，可选 `image_paths`、`max_credits`（默认 0） | 快速提交文生图/参考图编辑，返回任务。 |
| `submit_video` | 同上 | 快速提交文生视频/参考图视频，返回任务。时长、横竖比例由选择的具体模型 ID 决定。原生视频当前不接受参考图。 |
| `get_generation` | `generation_id` 或原始 `request_id`，严格二选一 | 查一次任务；提交超时可凭 `request_id` 只读找回任务。完成后下载、验证媒体，返回 `local_path`、文件大小和 SHA-256。不会重新生成。 |

`image_paths` 只接受绝对本地路径。支持 PNG、JPEG、WebP，读取后实际解码验证；每张最多 20 MiB、40MP，编码后的参考图合计最多 28 MiB，数量受模型目录上限与工具总上限 8 张共同限制。提示词最多 16000 字符。不能把 Cookie、密钥或其他文本文档当作参考图。

一次真实生成意图对应一个稳定 `request_id`，长度 8–128，允许字母、数字、点、下划线、连字符，首位为字母或数字。服务端用它去重。**提交超时或服务 5xx 返回 `unknown` 时保留该 ID，不要自动换 ID 再生成。** 已拿到任务 ID 时按 `generation_id` 查询；没拿到时用 `get_generation(request_id=原始ID)` 只读找回任务。查不到直接返回 `not_found`，不会 POST 补提；后续是否产生新生成意图由操作者决定。

提交状态包括 `queued`、`running`、`completed`、`failed`、`unknown`。`get_generation` 不在一次调用中长时间轮询；等待任务处理后再查询。查询完成任务会再次下载到新文件名，不覆盖已有文件。下载/验证失败返回明确工具错误，不宣称已得到本地素材，也不重提生成。

服务重启后，未记录完成结果的任务变为 `unknown`，不会自动续跑或重新提交。Agent 请求同时关闭了上游生成的自动重试；这是为了避免网络断开后重复消耗额度，不代表能保证 Google 侧的执行结果。此时需要在 Flow 核对原任务。

图片输出会解码检查；支持服务放大/缓存回退路径返回的图片 data URL（严格 base64、实际 MIME 校验，解码后最多 20 MiB），保存后不向 Agent 回传整段 base64。不接受视频 data URL。视频输出校验完整 MP4 容器结构，返回 `validation=mp4_container_only`，**这不证明视频能完整播放或画面合格**。只有 `local_path` 返回后才完成文件交付，实际生成验收仍需打开图片/播放视频。

外部媒体仅允许公网 HTTPS，DNS 检查后固定目标 IP 并维持正确 TLS 主机名；每次重定向重新验证。服务 API Key 只发往配置的 Flow2API 服务及其 `/tmp/` 缓存，不发往外部媒体地址。MCP 不继承全局 HTTP 代理；如本机无法直连外部媒体，建议启用 Flow2API 服务端缓存，避免直接下载源链接。单文件下载上限 200 MiB。

## 离线验证与真实验收

```sh
cd '/absolute/path/to/flow2api/repo'
../venv/bin/python -m unittest discover -s tests -p 'test_agent_mcp.py' -v
../venv/bin/python -m unittest discover -s tests -p 'test_native_agent_routing.py' -v
```

其中一项会绑定本机随机端口并启动 MCP 子进程；限制本机监听的沙盒需要允许该离线测试。所有 HTTP 响应来自模拟服务，不访问 Google。

只有 Flow 账号的首轮真实验收应先做一张原生 Nano Banana 2.1 文生图：确认选定模型、比例、数量 1 和页面明确 0 点数，再检查文件能打开、实际尺寸/内容和额度变化。此轮通过后才能宣称该账号上的该路径真实生成通过。

Omni 文生视频应先读回一个短视频配置的实际点数，取得该次额度授权后再提交，核对选定模型、比例、清晰度、时长、数量 1、实际扣点和播放效果。参考商品图编辑和参考图视频需要各自实现或已配置的通道及独立费用授权。不能把纯文生图通过扩大解释为这些能力也通过。
