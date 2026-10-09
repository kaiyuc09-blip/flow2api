# 在 Codex / Claude 中使用 Flow2API

本项目的 MCP 入口是 `agent_mcp.py`，使用标准输入/输出（stdio）提供四个媒体工具。它调用已运行的 Flow2API 服务，不启动浏览器或主服务，不是聊天推理模型。主路径为 YesCaptcha 第三方打码和 Flow2API-Token-Updater 插件登录；服务按数据库配置运行，第三方模式不打开 Chrome。

## 当前验证范围

- 已做离线 HTTP 客户端、图片读写、模型/输入拒绝、错误脱敏、重定向与公网地址限制测试。
- 已通过官方 MCP SDK 的独立进程协议测试：`initialize`、`list_tools`、`call_tool`。该测试只连接本机临时模拟服务，不接触 Google 账号。
- Nano Banana 2.1 文生图与 Omni 1.1 Flash 文生视频已有独立的 `personal` 浏览器原生路径，提交前选择并读回模型、比例、数量、费用和视频清晰度/时长；不把旧 RPC 模型改名。**这些能力仍未完成真实生成验收。** 原生 UI 仅在 `personal` 模式的目录中出现，详见附录。
- 模型以 `list_models` 当时返回的目录为准。`available` 表示当前服务配置允许请求；`verification_state` 是验证状态，不能把“允许请求”理解为真实出图已通过。

截至 2026-10-08，Google 已公布 [Flow 的 Nano Banana 2.1](https://support.google.com/flow/answer/16352836?hl=en)，且已在登录后的实际 Flow 页面核对该模型选项。实现方 useapi.net 的 [10 月 5 日记录](https://useapi.net/docs/changelog#october-5-2026) 提到新输出的 `modelNameType=BELUGA`，但本项目尚未取得它作为 Flow 请求参数的直接证据。因此原生路径通过网页选项提交，不发送猜测的 `BELUGA` 或旧 `NARWHAL` 请求键。

## 环境准备：YesCaptcha + Token-Updater 插件

MCP 进程调用独立 HTTP 服务；它不负责登录，也不会因为工具已连接就证明生成可用。以下路径是占位示例。使用项目专用 Python 3.12 环境，按主 README 安装 `requirements-agent.txt`；不需要全局安装依赖。

```sh
cd '/absolute/path/to/flow2api/repo'
uv venv --python 3.12 ../venv
uv pip install --python ../venv/bin/python -r requirements-agent.txt
../venv/bin/python scripts/run_native.py \
  --private-dir '/absolute/path/to/flow2api/private' --port 8000 --background
```

保留 `run_native.py` 文件名以兼容已有启动入口，现在它支持数据库保存的打码方式。`--private-dir` 必须在仓库外，首次用新的空目录；`--prepare-only` 仅准备私有目录和随机管理凭证。服务只监听 `127.0.0.1`、只运行一个 worker，同一目录由文件锁保护。第三方模式不检查 Chrome 路径、不启动浏览器；只有 `personal` 才要求已安装的 Chrome，可用 `--browser` 指定。

`--background` 让服务独立于启动终端运行，并在带鉴权的模型目录检查成功后报告就绪。再次运行会复用已就绪服务。启动超时只报告未确认与进程号，不自动重复启动。此参数不注册开机自启；电脑重启后需再次启动，或经用户授权安装下文的 LaunchAgent。

按顺序完成以下准备，真实凭据只由账号持有人在本机管理页和插件中填写，不交给 Agent：

1. 在本机管理页登录私有目录中保存的管理账号；把验证码方式设为 `yescaptcha`，填写服务密钥，任务类型选 `RecaptchaV3TaskProxylessM1S9`。注册、充值及填写密钥由操作者本人完成。
2. 在日常 Chrome 安装上游配套 Flow2API-Token-Updater 插件。由操作者本人登录 Google Flow、打开 `flow.google.com/projects`，将管理页「插件配置」给出的连接地址和连接 Token 填入插件，由插件把登录态推送到本机 `POST /api/plugin/update-token`。不要把 Cookie、ST、插件 Token 或打码密钥粘贴给 Agent。
3. 核对插件显示同步成功，再用 `list_models` 核对 RPC 模型可用性。`gemini-3.1-flash-image-square` 应允许最多 3 张参考图；具体视频模型的参考图上下限按目录返回值。

插件不依赖 personal 浏览器。在第三方模式下，旧 `personal_browser` 账号可以保留，同时新增或更新其他普通账号；同一身份的原生账号仍禁止被插件覆盖，不能为解决冲突删除账号或数据库。`personal` 模式仍保留原有单账号保护。

重启始终使用同一私有目录。启动器不覆盖数据库保存的 `captcha_method` 或任何打码密钥。管理凭证不一致时明确拒绝启动，不用重新生成密钥或替换数据库绕过。**已有私有目录仍保存原模式时，需要操作者在管理页主动更改；更新代码不会自动切换它。**

首次初始化私有数据库时，`flow.max_retries` 默认 2；已有数据库的重试值不变，仍可在管理页设置。这个配置是最大尝试次数。Agent 任务为避免不确定提交产生重复扣费，继续关闭生成的自动重提；打码提供方内部的创建任务重试独立存在，并计入下文的实际调用次数。

私有目录权限仅当前用户可读，以下内容均不提交到 Git：

| 文件 / 目录 | 用途 |
|---|---|
| `service-credentials.json` | 随机本机管理凭证及服务 API Key，仅操作者本人本地查看。 |
| `api-key.txt` | 供 MCP 从文件读取服务 API Key；不是 Google 登录态。 |
| `flow.db` | 账号、设置、任务结果；Agent 不读取敏感字段。 |
| `browser-profile/` | 保留给可选 personal 模式；第三方模式不使用。 |
| `cache/`、`service.log`、`media/` | 缓存、私有日志和生成素材。 |
| `service.lock` | macOS/Linux 单实例锁；退出后自动释放，不需删除文件。 |

### 打码次数与费用边界

以下是没有重试、每张参考图上传一次且不放大的基础调用数，不是固定收费承诺：

| 操作 | 创建打码任务次数 |
|---|---:|
| 文生图 | 1 |
| 参考图生图，n 张参考图 | n 次上传 + 1 次生成 |
| 文生视频 | 1 |
| 图生视频，n 张参考图 | n 次上传 + 1 次视频提交 |
| 图片或视频放大 | 额外 1 |
| 查询任务或视频进度 | 0 |

`get_generation` 返回 `captcha_call_count`：记录本任务实际尝试发出的第三方创建打码任务请求数，包含上传、生成、放大及内部重试；获取已有打码任务结果的轮询不增加次数。历史记录、缺少完整观测或进程中断导致无法确认时返回 `null`，不能当成 0。原生 UI 没有可观测的第三方创建请求时也不据此推断浏览器内部行为。

调用次数不等于成功次数或供应商实际收费次数。若按 35 点/次、1000 点/元估算，一次约 0.035 元，15 次约 0.53 元；真实收费以供应商账单为准。Flow 自身点数另计。

**第三方 RPC 通道下，`max_credits` 不限制 YesCaptcha 费用，也不限制 Flow 的实际扣点。** 该参数只约束附录所述原生 UI 的页面点数；不能因 `max_credits=0` 宣称第三方生成免费。提交前须单独约定允许的生成次数、打码次数和 Flow 点数预算，出现不确定状态时保留原 `request_id`，不自动重提。

### macOS 登录后自启（需另行授权安装）

`scripts/launchagent.py` 有 `render`、`install`、`uninstall` 三个明确操作。阶段一仅生成和测试 plist，**不执行安装**。可先生成一个尚不存在的本地文件检查：

```sh
../venv/bin/python scripts/launchagent.py render \
  --private-dir '/absolute/path/to/flow2api/private' \
  --output '/absolute/path/to/review/flow2api.plist'
```

操作者另行同意安装后，才执行 `install --private-dir ...`；卸载用同一配置执行 `uninstall --private-dir ...`。默认对象是当前用户的 `~/Library/LaunchAgents/com.flow2api.local.plist`，不会改系统级目录；已有 plist 不覆盖。若已有手动启动的服务占着端口，安装拒绝，先核对并关闭该服务再操作。

LaunchAgent 在用户登录后托管一个前台服务进程，提供与独立后台运行相同的常驻效果；它不传 `--background`，以免子进程脱离 launchd 后被重复拉起。使用绝对路径、私有权限、失败后的重启间隔。卸载只停掉本脚本的精确匹配任务并删除其 plist，不删账号、密钥、浏览器资料或素材。

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
claude mcp add flow2api --scope local --transport stdio \
  --env 'FLOW2API_BASE_URL=http://127.0.0.1:8000' \
  --env 'FLOW2API_API_KEY_FILE=/absolute/path/to/flow2api/private/api-key.txt' \
  --env 'FLOW2API_OUTPUT_DIR=/absolute/path/to/flow2api/media' \
  -- '/absolute/path/to/flow2api/venv/bin/python' \
  '/absolute/path/to/flow2api/repo/agent_mcp.py'
```

服务名放在 `--env` 之前，避免被当前 Claude CLI 的多值环境变量参数误读为环境变量。注册后在同一项目运行 `claude mcp get flow2api`，应显示 `Local config` 和 `Connected`；这只验证 MCP 连接，不代表 Google 账号已连接或生成成功。

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

阶段一运行 `../venv/bin/python -m pytest -q`，含隔离临时数据库的插件测试、真实本机临时服务启动和模型目录读取；使用假密钥与空账号库，不访问 Google 或付费打码端点。LaunchAgent 只生成到临时目录，安装/卸载系统调用在测试中替换为模拟调用。

阶段二只有操作者明确授权后才做。先确认 RPC 图片模型 available、账号活跃和 Flow 点数余额，再依次验证文生图、参考图生图、文生视频、图生视频；每项一个稳定 request_id，不自动重提。分别记录 captcha_call_count、Flow 点数前后差、本地文件路径，并实际打开图片和播放视频。配置可用、MCP 连接成功或离线测试通过，都不能替代真实生成验收。

## 附录：可选 personal 原生 UI

原生 UI 代码保留，但只在数据库选择 `personal` 时出现在模型目录里。该模式需要专用 Chrome 和它自己的登录态；不作为参考图和完整视频工作流的主路径。异常退出后若专用窗口占用资料目录，只关闭已核实属于该服务的专用窗口，保留资料，不结束所有 Chrome。

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
