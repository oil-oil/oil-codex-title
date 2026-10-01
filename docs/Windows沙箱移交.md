# Windows 沙箱下的命名移交

Windows 上 Codex 会把 Stop Hook 放进沙箱账户执行。该身份写不了插件状态目录，
因此 Hook 里既起不了 app-server，也连不上真实用户 daemon 的控制套接字。
本文说明插件的应对方式、安装步骤与验收要求。

## 现象与实测证据

本机实测（Codex 桌面版 + `windows.sandbox` 开启）：

| 观测 | 结果 |
| --- | --- |
| Hook 执行身份 | `<机器>\CodexSandboxOffline` |
| `~/.codex` 对该身份的权限 | 仅 `ReadAndExecute, Synchronize`（无写入） |
| Hook 内启动独立 app-server | `failed to initialize sqlite state runtime under ~/.codex` → `Access denied`，进程立即退出 |
| 复用真实用户 daemon | `failed to connect to ~/.codex/app-server-control/app-server-control.sock` → `WSAEACCES`（访问套接字权限不允许） |
| 宿主侧表现 | 设置页 Hook 开关为开启、`doctor` 只报笼统的 `App Server 连接已关闭` |

也就是说：**Hook 进程既不能写 Codex 状态，也不能连用户身份的 IPC**。
沙箱账户自己的 `~/.codex/app-server-control/` 是另一个身份的控制面，不是可用通道。

## 为什么不在 Hook 里绕

- Hook 处理器配置只有 `command` / `commandWindows` / `timeout` / `async` /
  `statusMessage` / `additionalContextLimit` / `mcp_tool` 等字段，
  **没有"脱离沙箱 / 指定运行身份"的开关**，配置层面无法换身份。
- 放宽 `windows.sandbox` 或给沙箱组开放 `.codex` 写权限，等于让每轮后台任务
  都能改写 Codex 状态，代价过大。
- 插件纪律要求改名只能走官方 App Server 接口，不能直接改写对话数据库。

## 设计：Hook 只投递，用户身份 worker 执行

```
Stop Hook（沙箱身份）
  └─ 能力探测：状态目录可写？
       ├─ 可写 → 与以往完全一致，直接命名
       └─ 不可写 → 原子投递请求到共享队列，输出 {}，退出 0
                        ↓
用户身份 worker（计划任务 / Codex automation 周期触发）
  └─ 取请求 → 复用同一命名流程（app-server + 独立命名模型）→ 写回标题
```

- 队列默认位置：`%PROGRAMDATA%\oil-codex-title\queue`（两侧身份看到的是同一路径），
  可用配置项 `handoff_dir` 或环境变量 `OIL_CODEX_TITLE_QUEUE` 覆盖。
- 目录结构：`requests/`（待处理）、`done/`（已完成）、`failed/`（超限失败）、
  `handoff.log`（移交日志，尽力写入）。
- 请求只包含话题与轮次标识、投递身份和时间，**不含**对话内容与转写文件路径；
  命名所需的上下文仍由 worker 通过 app-server 读取。
- 投递使用 `mkstemp` + `os.replace`，不会留下半截文件；同一话题同一轮次重复触发
  覆盖同一请求，不会重复命名。

## 安装

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-windows-handoff.ps1
```

脚本会：

1. 建立队列目录，并**仅对该目录**给沙箱组授予修改权限（不触碰 `.codex`）；
2. 注册以当前用户身份运行的计划任务 `OilCodexTitleHandoff`（每 5 分钟 + 登录时），
   执行 `oil_codex_title.py worker`；
3. 立刻跑一次 `worker` 自检并打印统计。

参数：`-QueueDir`、`-SandboxGroup`、`-TaskName`、`-IntervalMinutes`、`-PythonPath`。
受限令牌下（实测：管理员组为 deny-only 时）计划任务 Cmdlet 会被拒，脚本会自动改用
`schtasks.exe` 注册；两条路径都失败时会打印可手工执行的命令。

**静默执行**：计划任务以交互身份运行时，控制台解释器（`python.exe`）会每分钟闪出一个
窗口。脚本因此默认优先选用 `pythonw.exe`（同目录的无窗口解释器），worker 全程静默；
Hook 自身的窗口取决于宿主如何派生该命令，不由插件控制。
卸载：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-windows-handoff.ps1 -Uninstall
```

若本机已有 Codex automation 承担周期任务，也可以不用计划任务，让 automation 调用
`oil_codex_title.py worker` 即可 —— 关键是**必须由用户身份运行**。

## 验收（四类证据分开看）

1. **程序测试**：`python -m unittest discover -s tests -v`
   （覆盖不可写时投递、可写时不投递、非 Stop 事件不投递、重复投递只命名一次、
   失败重试与落 `failed/`、单条失败不影响后续、空队列统计）。
2. **真实 Hook 触发**：在 Windows 桌面端正常结束一轮对话，确认
   `queue/handoff.log` 出现移交记录、`requests/` 被 worker 清空。
3. **账号模型调用**：`usage/YYYY-MM-DD/` 出现当次命名尝试的账目（不额外计费重复次数）。
4. **桌面显示**：话题标题在侧边栏刷新为新标题。

四类不能互相替代：程序测试通过不等于 Hook 真的被宿主触发，用量记录也不能证明
桌面已经刷新。

## 回滚

- 删除计划任务并撤销队列权限：`install-windows-handoff.ps1 -Uninstall`；
- 停用移交：`configure` 里把 `handoff_dir` 留空并删除计划任务即可，
  Hook 会回到"不可写则静默退出"的旧行为（不再命名，但也不会报假成功）；
- 插件升级会覆盖 `scripts/`，本机适配需按下文重新应用。

## 被否方案

| 方案 | 否决原因 |
| --- | --- |
| 给沙箱组开放 `.codex` 写权限 | 每轮后台任务都能改写 Codex 状态，破坏沙箱边界 |
| 放宽 / 关闭 `windows.sandbox` | 全局降低隔离等级，代价远超本问题 |
| Hook 内继续尝试 proxy / 控制套接字 | 已实测 `WSAEACCES`，身份不通就永远不通 |
| Hook 内起临时 `CODEX_HOME` 的 app-server | 能启动但读不到真实话题状态，改名落到临时库上，等于没改 |
| 直接改写 `session_index.jsonl` / 桌面私有状态 | 违反插件纪律，且与宿主缓存不一致 |

## 与本地适配的关系

本机另有两处本地适配（见安装目录下的 `LOCAL-WINDOWS-ADAPTATION.md`）：Hook 的
Windows 命令改用 uv 管理的 Python；命名子进程不再 `--ignore-user-config` 以便走本地
代理。这两处与本方案互不影响，但**从上游覆盖安装会丢失**，需要重新应用。
