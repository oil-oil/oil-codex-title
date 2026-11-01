# Windows 安装与验证

Windows 版本沿用 Python 实现，没有额外的文件锁依赖。当前已完成适配并通过自动化测试，官方 Codex CLI 的原生入口与 App Server 连接也已验证；Windows 桌面端登录后的真实 Stop 触发、Luna 调用与列表刷新尚未实测。

## 安装条件

1. 安装 Python 3.10 或以上版本，确保 `py -3 --version` 可用。
2. 安装并登录兼容的 Codex CLI。可使用 PATH 中的原生 `codex.exe`，或标准 npm 安装提供的 `codex.cmd`。
3. 将完整插件安装到本机 Codex，通过官方 Hook 管理入口检查并信任定义。安装插件本身不代表 Hook 已获信任。

插件会将标准 npm 入口解析到原生 `codex.exe`，支持 x64/ARM64 对应包及新旧 vendor 布局；不会通过 `cmd.exe` 转义命名参数。无法识别的自定义启动脚本需要显式指定原生可执行文件。

在插件目录的 PowerShell 中运行：

```powershell
py -3 scripts/mogoo_codex_title.py doctor
py -3 scripts/mogoo_codex_title.py configure --codex-bin 'C:\Codex\codex.exe'
```

默认每次运行重新查找，不固定桌面版的版本目录。顺序为有效的手动入口、PATH、当前用户的 npm 安装和 `%LOCALAPPDATA%\OpenAI\Codex\bin` 下的桌面程序；桌面版本按程序修改时间优先尝试较新的版本。每个候选通过无模型的 `--version` 检查，无法启动、超时或输出不符合 Codex CLI 的入口会被跳过。

第二条仅在需要优先使用指定 CLI 时执行，替换为实际存在的路径。旧配置中的程序被更新清理后，后台会自动继续查找；不自动改写原配置。手动保存 npm 的 `codex.cmd` 时保留该入口，每次再解析原生程序。设置时的错误路径仍会报错，不能以自动回退掩盖输入错误。

已有固定路径可恢复自动查找：

```powershell
py -3 scripts/mogoo_codex_title.py configure --codex-bin auto
py -3 scripts/mogoo_codex_title.py doctor
```

版本检查只证明程序可以启动；App Server 兼容性、账号登录和模型调用仍分别验证。自动查找不会修改模型服务、密钥或 Hook 信任。

## 已适配的行为

- Hook 使用 Windows 专用命令 `py -3 -X utf8`，通过插件路径变量定位脚本。
- Windows 使用标准库 `msvcrt` 的内核字节锁；macOS/Linux 保留 `fcntl`。进程退出后自动释放锁。
- 子进程使用参数数组与 UTF-8 管道，支持路径中的中文、空格和标题中的 emoji。
- 后台 Codex 子进程不创建新的控制台窗口。
- 校验器拒绝将 Windows 盘符路径或 UNC 路径写入标题。

## 验收方式

自动化矩阵覆盖 Windows 的 Python 3.10/3.13，以及 macOS/Linux 的 Python 3.13。程序测试不调用付费模型；Windows 另安装官方 Codex CLI，检查原生入口解析与 App Server 连接。

2026-09-14 的四组环境均通过全部 54 项测试，结果见 [跨平台验收记录](https://github.com/oil-oil/oil-codex-title/actions/runs/34799381646)。Windows CLI 检查使用 codex-cli 0.154.0，App Server 连接成功；CI 没有登录账号，也没有加载桌面 Hook。

账号环境中的最终检查仍需在 Windows Codex 中完成：新建正常话题、结束一轮有具体目标的对话、检查后台日志与实际显示标题，确认没有额外命名消息。没有这一步证据时，不宣称 Windows 桌面体验已经完整验收。

参考：[官方 Hook 的 Windows 命令与异步配置](https://learn.chatgpt.com/docs/hooks)、[Python Windows 文件锁](https://docs.python.org/3/library/msvcrt.html)。
