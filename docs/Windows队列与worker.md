# Windows 后台队列与 worker

0.4.0 借鉴上游 [PR #5](https://github.com/oil-oil/oil-codex-title/pull/5) 的同步入队方案。Stop Hook 校验本轮标识后入队并返回 `{}`，后台 worker 再读取宿主、判断是否命名并通过官方标题接口写入。原来的官方 / 中转站配置、时间后缀、暂停、锁定、手动改名和归档保护继续生效。

## 配置方式

从 0.4.4 起，安装脚本保留带 BOM 的 UTF-8 编码，以兼容入口默认调用的 Windows PowerShell 5.1；PowerShell 7 也可读取。仅在 7 中解析通过不足以验证 5.1。源码的编码检查和安全执行回归可运行 `py -3 -m unittest discover -s tests -p test_worker_installer.py -v`；它们会替代任务计划和 ACL 写入，不改动真实任务。0.4.3 用户更新完整插件后，可重新执行 `worker-setup`，无需改用 PowerShell 7 绕过编码问题。

先从 `mogoo-codex-title` 市场安装或更新本插件。以下命令在实际安装的插件目录执行；Windows 需要可用的 Python 3.10+：

```powershell
py -3 -X utf8 scripts/mogoo_codex_title.py worker-setup
py -3 -X utf8 scripts/mogoo_codex_title.py doctor
```

`worker-setup` 必须在当前登录用户身份下运行。它创建每分钟运行的计划任务，任务名称为 `MogooOilCodexTitleWorker-` 加数据目录的哈希；任务使用交互登录身份和普通权限，因此用户登出期间不消费队列。运行入口写在插件数据目录，优先从 `mogoo-codex-title` 缓存选择支持队列且入口完整的最新版本，兼容旧市场中的新名称和 `mogoo-oil-title/oil-codex-title` 历史缓存。

从 0.4.2 及更早版本改名迁移后，须在新安装目录重新执行一次 `worker-setup`，更新已经生成的 `worker_runner.py`；仅更新插件源码不会改变旧 runner。计划任务名称、ProgramData 队列目录和数据目录保留历史标识，以便覆盖更新同一个任务、继续处理原队列。先停用或卸载旧插件的 Hook，再启用并信任新定义，避免同时加载两套 Hook。

Windows 队列位于 `%PROGRAMDATA%\MogooOilCodexTitle\<数据目录哈希>\queue`。安装器只向本机已有的 `CodexSandboxUsers` 组授予这个队列的修改权限，不扩大 `.codex` 目录权限，也不自动信任 Hook。若组不存在，返回的 `sandbox_queue_authorized` 为 `false`；这不能证明沙箱 Hook 能入队。任务或授权配置报错时，保留原标题并检查当前用户权限。

同用户身份的 Hook 可以启动独立 worker；沙箱身份只投递到已配置的用户任务，不把启动子进程当作身份转换。主动触发任务可能受 Windows 权限限制，周期任务仍作为消费入口。Windows worker 会读取当前用户环境变量中的中转站密钥；密钥不写入队列或任务参数。

需要手动消费已入队的请求时，可在当前用户身份下运行：

```powershell
py -3 -X utf8 scripts/mogoo_codex_title.py worker
```

撤销周期任务及队列的沙箱组授权：

```powershell
py -3 -X utf8 scripts/mogoo_codex_title.py worker-setup --remove
```

撤销不删除队列或插件配置。同用户直接启动仍可工作；需要暂停所有自动命名时，使用 `pause` 或在官方界面禁用 Hook。

## 处理和数据边界

- 请求只保存 UUID 话题 / 轮次标识、版本、提交时间及重试字段，不携带对话、密钥、路径或待执行命令。
- 同一个请求在等待、处理中或已完成时不重复覆盖。worker 使用内核锁串行认领；进程退出后才允许恢复遗留请求，不按文件年龄回收活跃 worker。
- 每次最多处理 4 项，失败最多尝试 3 次，采用 30 / 60 秒退避。轮次未稳定或话题忙时重新排队；持续无法处理会结束该请求，保留原标题。
- 共享完成记录只有状态。详细本地诊断仅记录固定错误类别，不保存模型 stderr 原文、认证头或提示词。
- 用户周期任务只接受本插件定义的字段，并重新检查宿主和标题保护；不会执行请求提供的脚本或命令。

## 安装后验收

新版 Hook 的同步方式和超时定义已改变，更新插件后须在 Codex 官方 Hook 管理界面重新信任。确认只加载 `mogoo-codex-title` 来源，再在正常对话中完成一轮，分别核对以下证据：

1. `doctor` 的 Hook 状态与实际用户身份；`task_enabled` 只表示配置标记，不证明 Windows 任务当前存在或已运行。
2. 计划任务的实际最近运行时间 / 结果和队列状态；仅显示「就绪」不足以证明消费成功。
3. 本地日志出现本轮 `hook_queued` 与 `worker_result`，或明确的固定失败类别。
4. 官方接口读回标题，并检查桌面实际显示及时间后缀。

程序回归覆盖队列互斥、身份分支及恢复机制；真实 CLI 本地合成接口检查验证调用参数。两者均不代表用户账号模型、自然 Stop 触发或桌面显示已验收，也不应通过修改信任数据库或桌面私有状态补齐证据。
