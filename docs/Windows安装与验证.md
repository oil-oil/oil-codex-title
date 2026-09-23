# Windows 安装与验证

Windows 版本沿用 Python 实现，没有额外的文件锁依赖。自动化测试、Windows CLI 真实 Stop 触发、Luna 调用和 App Server 标题读回均已验证；另有一条 Windows 桌面话题完成真实 Stop 改名和显示验收。置顶列表等其他显示状态尚未实测。

## 首次预检

在完整 Windows PowerShell 中执行：

```powershell
py -3 --version
codex --version
```

[Windows 实测 Issue #1](https://github.com/oil-oil/oil-codex-title/issues/1) 使用 CLI `0.155.0` 完成真实 Stop 命名；这是已验证版本，不代表低于它的版本一定不兼容。`doctor` 的 `cli_preflight` 会报告当前版本与该版本的关系，但 `ready` 仍只表示 App Server 和 Hook 定义可用。若官方更新器在 Codex 内置 PowerShell 中提示缺少 `Get-FileHash`，请改用完整 Windows PowerShell 运行官方更新命令。

## 安装条件

1. 安装 Python 3.10 或以上版本，确保 `py -3 --version` 可用。
2. 安装并登录兼容的 Codex CLI。可使用 PATH 中的原生 `codex.exe`，或标准 npm 安装提供的 `codex.cmd`。
3. 将完整插件安装到本机 Codex，通过官方 Hook 管理入口检查并信任定义。安装插件本身不代表 Hook 已获信任。

插件会将标准 npm 入口解析到原生 `codex.exe`，支持 x64/ARM64 对应包及新旧 vendor 布局；不会通过 `cmd.exe` 转义命名参数。自动发现会先运行候选入口的 `--version`，跳过存在但拒绝执行的 WindowsApps 入口。显式配置的不可执行路径会报错，不会静默改用另一份 CLI。无法识别的自定义启动脚本需要显式指定原生可执行文件。

在插件目录的 PowerShell 中运行：

```powershell
py -3 scripts/oil_codex_title.py doctor
py -3 scripts/oil_codex_title.py configure --codex-bin 'C:\Codex\codex.exe'
```

第二条仅在默认检测找不到正确 CLI 时使用，替换为实际存在的路径。

## 已适配的行为

- Hook 使用 Windows 专用命令 `py -3 -X utf8`，通过插件路径变量定位脚本。
- Windows 使用标准库 `msvcrt` 的内核字节锁；macOS/Linux 保留 `fcntl`。进程退出后自动释放锁。
- 子进程使用参数数组与 UTF-8 管道，支持路径中的中文、空格和标题中的 emoji。
- 后台 Codex 子进程不创建新的控制台窗口。
- 校验器拒绝将 Windows 盘符路径或 UNC 路径写入标题。

## 验收方式

自动化矩阵覆盖 Windows 的 Python 3.10/3.13，以及 macOS/Linux 的 Python 3.13。程序测试不调用付费模型；Windows 另安装官方 Codex CLI，检查原生入口解析与 App Server 连接。

2026-09-14 的四组环境均通过全部 54 项测试，结果见 [跨平台验收记录](https://github.com/oil-oil/oil-codex-title/actions/runs/34799381646)。Windows CLI 检查使用 codex-cli 0.154.0，App Server 连接成功；CI 没有登录账号，也没有加载桌面 Hook。

2026-09-23 的本地 Windows CLI `0.156.1` 验收：新建测试对话后，首轮 Stop 确实触发并完成独立 Luna 调用，但宿主首次标题同时变化，插件记录 `stale_result` 且未写入；下一轮正常 Stop 记录 `renamed`，App Server 读回标题与日志一致，原对话没有额外命名消息。该证据只覆盖 CLI，不覆盖桌面侧边栏刷新。

同日用户完成一条 Windows 桌面话题的实测：插件日志记录 `renamed`，后续轮次记录 `kept`；App Server 读回的标题与用户提供的桌面截图一致。该证据确认这一话题的自动触发、持久标题和可见显示，不证明置顶列表、其他任务或所有桌面刷新场景均一致。文档不保存真实话题 ID、对话内容或截图路径。

其他桌面状态仍需在 Windows Codex 中逐项检查：新建正常话题、结束一轮有具体目标的对话、核对后台日志、App Server 标题与实际显示。不要把单条话题的成功扩展为所有列表状态已经验收。

首次验收按以下边界排查：`doctor` 失败先检查 CLI 路径、版本与 App Server；Hook 未 `ready` 时在 Codex CLI 输入 `/hooks`，选中本插件的 Stop Hook 后按 `t` 信任；`ready` 后仍要完成真实对话，等待日志出现 `renamed`，再用 `doctor --thread <话题 ID>` 读回相同标题。若独立模型超时，先检查 CLI 的网络或传输错误，最多恢复性重试一次，不要重复安装插件。`codex exec` 成功、手动运行脚本或仅有锁文件，都不能代替真实 Stop 验收。

参考：[官方 Hook 的 Windows 命令与异步配置](https://learn.chatgpt.com/docs/hooks)、[Python Windows 文件锁](https://docs.python.org/3/library/msvcrt.html)。
