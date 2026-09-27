# codebuddy2api 部署与使用文档（Windows，含 WorkBuddy 5.6.x 加密登录态修复）

本仓库在上游 `ShouZhuo0413/codebuddy2api` 基础上合入了 **WorkBuddy 5.6.0+ `$wbEncrypted` 登录态解密修复**（上游 issue #23 / PR #26），已在 Windows x64 完整验证：`/health`、`/v1/models`、真实 `chat/completions`、OpenAI SDK 全部通过。

把 WorkBuddy / CodeBuddy 桌面端已登录的账号，转成本机 OpenAI / Anthropic 兼容 API，转发到腾讯后端 `copilot.tencent.com`。**不需要任何官方 API Key**，凭据来自本机桌面端登录态。

---

## 1. 前置条件

| 条件 | 说明 |
|------|------|
| WorkBuddy / CodeBuddy 桌面端 | 必须已安装 **并且已登录**（登录态文件存在且未过期） |
| Python | 3.8+（验证环境为 3.10.10） |
| Git | 任意近期版本 |
| 网络 | 能访问 `copilot.tencent.com` |

登录态文件位置（桌面端自动生成）：

```text
Windows: %LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info
macOS:   ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/*.info
Linux:   ~/.local/share/CodeBuddyExtension/Data/Public/auth/*.info
```

> 注意：WorkBuddy 5.6.0+ 会把 accessToken/refreshToken 加密为 `$wbEncrypted` 信封。本仓库已内置解密支持，**上游原版代码会遇到 401**，请务必使用本仓库。

---

## 2. 方式一（推荐）：GUI 桌面工具 `CodeBuddy2API-UI.exe`

单文件图形界面，集成全部功能，无需命令行。两种获取方式：

**A. 直接使用已构建的 exe**：拷贝 `dist\CodeBuddy2API-UI.exe` 到目标电脑，双击运行。

**B. 自行构建**（在装好 Python 的电脑上）：

```bat
git clone https://github.com/lindexyz/codebuddy2api.git
cd codebuddy2api
python -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt pyinstaller
build_exe.bat
:: 产物: dist\CodeBuddy2API-UI.exe
```

### GUI 功能一览

| 页签 | 功能 |
|------|------|
| 服务控制 | WorkBuddy 路径自动检测/浏览、监听地址/端口、API Key、脱敏开关、启动/停止服务、健康状态轮询（含登录态 token 过期提示） |
| 模型列表 | 一键刷新 `/v1/models`，显示全部可用模型 |
| 对话测试 | 选择模型 + 输入消息 + temperature，直接发送真实聊天请求并显示回复 |
| 额度查询 | 查询当前账号当前周期剩余积分（国内账号，数据来自 codebuddy.cn 积分服务） |
| 日志 | 实时查看 `converter.log`（自动跟随） |

配置保存在 exe 同目录的 `gui_config.json`，下次启动自动加载。

### GUI 使用步骤

1. 确保 WorkBuddy / CodeBuddy 桌面端已在本机**登录**
2. 双击 `CodeBuddy2API-UI.exe`
3. 在“服务控制”页点 **自动检测** 填入 WorkBuddy 路径 → 点 **启动服务**
4. 状态变绿后，在“对话测试”页发一条消息验证，或用外部客户端接入
   （Base URL `http://127.0.0.1:8787/v1`，API Key 留空，除非启动时设置了鉴权）

### 自检模式（无需打开界面即可验证全链路）

```powershell
.\dist\CodeBuddy2API-UI.exe --selftest
```

依次验证：启动服务 → /health → /v1/models → 真实对话（期望回复 `GUI_SELFTEST_OK`）→ 停止，全部输出 PASS 即部署成功。

---

## 3. 方式二：命令行部署

```powershell
# 1. 克隆本仓库
git clone https://github.com/lindexyz/codebuddy2api.git codebuddy2api
cd codebuddy2api

# 2. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\pip.exe install -r requirements.txt
# requirements.txt 已包含 fastapi / uvicorn / httpx / cryptography

# 3. （可选）运行离线自检测试
.venv\Scripts\python.exe -m pytest tests/test_workbuddy_atrest.py -q
```

---

## 4. 指定 WorkBuddy 可执行文件（关键步骤）

解密登录态需要通过本机 WorkBuddy 自带的 Electron 二进制读取密钥。程序按以下顺序查找：

1. 环境变量 `WORKBUDDY_ELECTRON_PATH`（推荐，最可靠）
2. Windows 默认路径 `%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe`

如果你的 WorkBuddy 装在非默认位置（例如 `G:\AI\WorkBuddy\WorkBuddy.exe`），先找到它：

```powershell
# 方法一：如果 WorkBuddy 正在运行，直接从进程取路径
Get-Process | Where-Object { $_.Name -match 'buddy' } | ForEach-Object { $_.Path }
```

然后设置环境变量（当前会话）：

```powershell
$env:WORKBUDDY_ELECTRON_PATH = "G:\AI\WorkBuddy\WorkBuddy.exe"   # 换成你的实际路径
```

想永久生效可写入用户环境变量：

```powershell
[Environment]::SetEnvironmentVariable("WORKBUDDY_ELECTRON_PATH", "G:\AI\WorkBuddy\WorkBuddy.exe", "User")
```

---

## 5. 启动服务

```powershell
cd codebuddy2api
.venv\Scripts\python.exe -m core.converter --desensitize --log converter.log
```

看到以下输出即启动成功：

```text
✅ 监听 http://127.0.0.1:8787（直连后端，原生 function calling）
   GET  /v1/models
   POST /v1/chat/completions
   POST /v1/responses          (Codex CLI 兼容)
   POST /v1/messages           (Claude Code / CC Switch 兼容)
   GET  /health
```

后台运行（PowerShell）：

```powershell
Start-Process -FilePath ".venv\Scripts\python.exe" `
  -ArgumentList "-m","core.converter","--desensitize","--log","converter.log" `
  -WorkingDirectory (Get-Location) -WindowStyle Hidden `
  -RedirectStandardOutput "server_out.log" -RedirectStandardError "server_err.log"
```

常用参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8787` | 监听端口 |
| `--api-key xxx` | 无 | 给本地 API 加一层鉴权（对外部署建议开启） |
| `--desensitize` | 关 | 压缩提示词、脱敏高风险关键词，建议开启 |
| `--log converter.log` | 无 | 记录请求/响应日志 |

---

## 6. 验证部署（按顺序执行）

```powershell
# 1. 健康检查（应返回 200 与 credential.token_expired=false）
curl http://127.0.0.1:8787/health

# 2. 模型列表（应返回 18 个模型，含 deepseek-v4-pro / deepseek-v4.1-flash / deepseek-v4-flash）
curl http://127.0.0.1:8787/v1/models

# 3. 真实聊天请求
curl http://127.0.0.1:8787/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"请只回复：OK"}],"temperature":0}'
```

第 3 步返回 `choices[0].message.content` 有内容即代表整条链路（本地 API → 转换器 → 本机登录态 → copilot.tencent.com → 模型）全部打通。

### OpenAI SDK 验证（可选）

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="test")
resp = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

---

## 7. 客户端接入

### 通用 OpenAI 兼容客户端（Cherry Studio / LobeChat / NextChat / Open WebUI 等）

- Base URL：`http://127.0.0.1:8787/v1`
- API Key：留空（或填启动时 `--api-key` 设置的值）
- 模型名：`deepseek-v4-flash` / `deepseek-v4-pro` / `glm-5.2` / `kimi-k2.7` / `auto` 等（以 `/v1/models` 实际返回为准）

### Codex CLI（走 `/v1/responses`）

`~/.codex/config.toml`：

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "deepseek-v4-pro"
model_provider = "workbuddy"
```

```powershell
$env:CODEBUDDY2OPENAI_KEY = "any-value"
codex --profile workbuddy "你的任务"
```

### Claude Code / CC Switch（走 `/v1/messages`）

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

---

## 8. 故障排查

| 现象 | 原因与处理 |
|------|-----------|
| 启动时报"无法获取 WorkBuddy at-rest 密钥（loggerGet）" | 未找到 WorkBuddy 可执行文件。设置 `WORKBUDDY_ELECTRON_PATH` 指向 `WorkBuddy.exe` |
| 聊天请求返回 `401 Authorization Required`（openresty/APISIX） | ① 用了未修复的上游原版代码 → 确认使用本仓库；② 登录态过期 → 打开 WorkBuddy 桌面端重新登录后重启服务；③ 多账号时选错 auth 文件 |
| `envelope keyId(...) 与本机派生 keyId(...) 不一致` | WorkBuddy 更换了密钥，重启服务重新读取（必要时重开桌面端） |
| `刷新 token 失败` | 登录态已整体失效，打开 WorkBuddy 桌面端重新登录 |
| 本地 401 invalid api key | 启动时设置了 `--api-key`，客户端未带同一 key |
| 响应慢 | 换快模型，如 `deepseek-v4-flash` |
| 被"敏感内容"拦截 | 开 `--desensitize`；仍不稳定用 `--desensitize --no-compact` |

日志排查：`converter.log` 中每个请求有唯一 ID，可查 `REQUEST BODY` / `ERROR BODY`。

---

## 9. 安全注意事项

- 本服务仅绑定 `127.0.0.1`，不要直接暴露公网；对外部署请放 HTTPS 反向代理后并启用 `--api-key`
- 不要把 `.info` 登录态文件、`converter.log`、`.env` 提交到 Git 或上传到任何第三方
- 本项目仅用于个人学习与研究，请仅在合法拥有订阅的前提下使用

---

## 10. 新机器快速 checklist

```text
[ ] WorkBuddy 桌面端已安装并登录（auth 目录存在 .info 文件）
[ ] 方式一：拷贝 dist\CodeBuddy2API-UI.exe 双击运行（或方式二：命令行部署）
[ ] GUI 中点“自动检测”填入 WorkBuddy 路径 → 启动服务
[ ] 状态变绿 / curl /health → 200
[ ] 对话测试或 curl /v1/models → 有 deepseek-v4-*
[ ] 发送一次 chat/completions → 有真实回复
```
