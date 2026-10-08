# 在 Codex / Claude 中使用 Flow2API

本项目的 MCP 入口是 `agent_mcp.py`，使用标准输入/输出（stdio）提供四个媒体工具。它调用已运行的 Flow2API 服务，不启动浏览器或主服务，不是聊天推理模型。

## 当前验证范围

- 已做离线 HTTP 客户端、图片读写、模型/输入拒绝、错误脱敏、重定向与公网地址限制测试。
- 已通过官方 MCP SDK 的独立进程协议测试：`initialize`、`list_tools`、`call_tool`。该测试只连接本机临时模拟服务，不接触 Google 账号。
- **尚未完成真实 Google 账号生成、最新模型协议确认、Codex/Claude 全局注册和真实工具验收。** Nano Banana 2.1 的真实 Flow 请求参数确认前，不通过改名宣称支持。
- 模型以 `list_models` 当时返回的目录为准。`available` 表示当前服务配置允许请求；`verification_state` 是验证状态，不能把“允许请求”理解为真实出图已通过。

截至 2026-10-08，Google 已公布 [Flow 的 Nano Banana 2.1](https://support.google.com/flow/answer/16352836?hl=en)。实现方 useapi.net 的 [10 月 5 日记录](https://useapi.net/docs/changelog#october-5-2026) 提到新输出的 `modelNameType=BELUGA`，但本项目尚未取得它作为 Flow 请求参数的直接证据。因此 `BELUGA` 仅是待核实线索，不将旧 `NARWHAL` 改名冒充 2.1。

## 环境准备

先用项目专用虚拟环境安装 `requirements-agent.txt`，再按主 README 配好并启动 Flow2API HTTP 服务。MCP 进程和 HTTP 服务是两个进程；MCP 不负责账号登录。

以下绝对路径是占位示例，请替换成自己的实际路径。已有 uv 时，可创建专用 Python 3.12 环境：

```sh
cd '/absolute/path/to/flow2api/repo'
uv venv --python 3.12 ../venv
uv pip install --python ../venv/bin/python -r requirements-agent.txt
```

当前服务任务执行只支持**单 worker**，启动时不要增加 `--workers`，也不要让多个服务进程共用同一任务数据库。MCP 客户端可以分别连接这个服务。

精确传递模型、比例、参考图的 Agent 路径，当前要求已配置第三方验证码服务，并有可用的 Flow 账号。验证码服务及真实生成可能消耗费用或账号额度；本项目的离线测试不验证账户可用性或成本。旧浏览器入口的默认文生图仍可保留，但不将其视为已验证的指定模型生成。

需要的环境变量：

| 变量 | 含义 |
|---|---|
| `FLOW2API_BASE_URL` | HTTP 服务地址，默认 `http://127.0.0.1:8000`。远程服务要求 HTTPS；不在 URL 中放凭证。 |
| `FLOW2API_API_KEY_FILE` | **推荐**。已存在的专用密钥文件绝对路径；文件只包含服务 API Key，不是 `KEY=VALUE` 格式。不要使用 Google 登录态或管理员密码。 |
| `FLOW2API_API_KEY` | 可选，从进程环境传入服务 API Key；优先于密钥文件。不要把值写进共享配置或命令历史。 |
| `FLOW2API_OUTPUT_DIR` | 必填，生成结果保存目录的绝对路径，建议放在仓库外的专用素材目录。 |

密钥文件建议放在仓库外的私有目录，权限仅当前用户可读。下面只给文件路径示例，**不会创建密钥文件或修改任何现有配置**。准备后可在终端检查 MCP 能启动；启动后等待客户端输入、没有欢迎文字是正常的。

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
| `generate_image` | `model`、`prompt`、`request_id`，可选 `image_paths` | 快速提交文生图/参考图编辑，返回任务。 |
| `submit_video` | 同上 | 快速提交文生视频/参考图视频，返回任务。时长、横竖比例由选择的具体模型 ID 决定。 |
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
```

其中一项会绑定本机随机端口并启动 MCP 子进程；限制本机监听的沙盒需要允许该离线测试。所有 HTTP 响应来自模拟服务，不访问 Google。

真实验收需先确认账号、调用模型及额度范围，再从选定 Agent 执行：文生图、参考商品图编辑、Omni 文生视频、Omni 参考图视频；核对实际模型参数、参考图是否生效、文件尺寸/时长、可打开/播放及额度变化。尚未完成这一轮时，只能说离线接口和 MCP 协议通过。
