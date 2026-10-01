# 本机本地适配说明（非上游内容）

本文件记录本机与上游的差异。**源码目录与安装缓存目录现已保持一致**，重新安装时不会
再退回旧行为；若两者再次出现差异，请先运行 `C:\ProgramData\oil-codex-title\ensure-config.py`
的漂移检测（会写进 `config-guard.log`）再重装。

## 1. Hook 改为同步 + 秒回投递（关键修复）

- 文件：`hooks/hooks.json`
- 上游：`"async": true`、`"timeout": 500`
- 本机：`"async": false`、`"timeout": 20`

原因（实测）：宿主**不会执行 `async` 的 Stop Hook**（抓 app-server 的 `hook/started`
事件可证：同步 Hook 执行、异步 Hook 连事件都没有），所以 Hook 必须同步且秒回；
真正的命名交给脱离 Hook 生命周期的后台 worker。

## 2. `commandWindows` 不能以引号开头

- 文件：`hooks/hooks.json` → `commandWindows`
- 本机值：`<uv python 绝对路径> -X utf8 ${PLUGIN_ROOT}/scripts/oil_codex_title.py hook`
  （**不带引号**）

原因（实测）：宿主以 `cmd /c "<命令>"` 包裹执行，命令若以带引号的路径开头会被双重引号
破坏，表现为 `hook exited with code 1`、脚本一行未执行、报"文件名、目录名或卷标语法不正确"。
本机没有 Python Launcher（`py` 不存在），Python 由 uv 管理，因此使用绝对路径且不加引号
（路径中不含空格）。

## 3. 命名子进程跟随用户 provider 配置

- 文件：`scripts/codex_adapter.py` → `generate_json()`
- 改动：删除 `--ignore-user-config`，并新增 `-c notify=[]`

原因：本机无法直连 `chatgpt.com` / `api.openai.com`，模型流量必须经过本地代理
（`http://127.0.0.1:15721/v1`）。同时修正子进程 `CODEX_HOME` 落到沙箱用户目录的问题。

## 4. 命名模型

- 上游默认：`gpt-5.6-luna` + `service_tier=priority`
- 本机现值：`gpt-6-luna`（实测本机代理可用，HTTP 200）

配置在 `%USERPROFILE%\.codex\oil-codex-title\config.json`，用
`python scripts/oil_codex_title.py configure --model <模型名>` 修改。

## 5. 配套的本机基础设施（不属于插件源码）

- `C:\ProgramData\oil-codex-title\queue`：Hook 与 worker 的共享队列（仅该目录对沙箱组授权）。
- 计划任务 `OilCodexTitleHandoff`：每分钟兜底消费队列（`pythonw` 静默）。
- 计划任务 `OilCodexTitleConfigGuard` + `ensure-config.py`：Codex 应用在启动/写入配置时会
  丢掉插件注册与 Hook 信任段，守卫负责补回（只补缺失段），并检测源码/缓存漂移。

## 如何恢复上游原状

1. 从 `https://github.com/oil-oil/oil-codex-title` 重新复制 `hooks/` 与 `scripts/`。
2. 删除上面第 5 节的两个计划任务与 `C:\ProgramData\oil-codex-title`。
3. 若本机将来能直连 OpenAI，可把 `--ignore-user-config` 加回并恢复 Luna Fast。
4. 若安装了 Python Launcher 且 `py -3 --version` 可用，可把 `commandWindows` 改回上游原值
   （注意仍需保持"不以引号开头"）。

