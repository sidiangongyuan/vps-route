# Windows（WSL2）

Windows 支持位于 `windows-wsl` 分支，使用 WSL2 内的 Linux 沙箱。已验证的发行版为 Ubuntu 22.04；代码接受其他具备相同内核能力的发行版。WSL1 会返回 `wsl1_unsupported`。

## 安装

在 Windows 终端安装并完成 Ubuntu 的用户设置：

```powershell
wsl --install -d Ubuntu-22.04
```

在 Ubuntu 终端安装依赖和正式版本：

```bash
sudo apt-get update
sudo apt-get install python3 python3-venv pipx git bubblewrap socat openssh-client netcat-openbsd curl ca-certificates
pipx install "git+https://github.com/sidiangongyuan/vps-route.git@v0.5.0"
pipx ensurepath
```

v0.5.0 发布之前，把安装地址中的 `@v0.5.0` 换成 `@windows-wsl`。可选安装 `zstd`，让官方 Claude 安装器使用其支持的压缩包。

重新打开 Ubuntu 终端。首次配置使用 `vps-route init`，填入自己的连接信息、专用密钥和已经独立核对的 known_hosts。程序和 profile 应放在 WSL 的 Linux 文件系统中。

已有 VPS 服务可以直接运行 `vps-route doctor --connect`、`vps-route install` 和 `vps-route auth login`。登录时在 Windows 浏览器打开终端显示的完整官方授权网址，再把授权 code 粘贴回同一终端。浏览器通过 Windows 当前的网络连接访问授权页面。已有 setup token 时，可在 Ubuntu 终端运行 `vps-route auth import-token` 并粘贴到隐藏提示中。

## SSH 与 Clash Verge

Clash Verge 规则模式可承载 SSH 的 HTTP CONNECT 连接。检查混合代理端口，并让自己的 VPS 地址匹配代理节点规则。该地址匹配 DIRECT 时，SSH 的传输路径仍是直连。程序保持严格 SSH 主机校验。

| WSL 网络模式 | ssh_http_proxy 示例 | 前置条件 |
| --- | --- | --- |
| mirrored | `http://127.0.0.1:7897` | Windows 代理监听该端口 |
| NAT | `http://windows-host:7897` | Windows 代理允许来自 WSL 的访问 |

`windows-host` 在连接时从 `/proc/net/route` 取默认网关，仅用于已确认的 NAT 模式。mirrored 模式的默认网关可能属于局域网路由器，因此使用回环地址。doctor 显示检测到的网络模式和建议写法。未知模式会要求先检查网络设置。

NAT 下可能需要你自行调整 Clash 的局域网访问和 Windows 防火墙。VPS Route 不修改 Clash、WSL 配置、防火墙或 binfmt 注册。GitHub 下载需要代理时，可在单次克隆命令里加入 `git -c http.proxy=http://127.0.0.1:7897 clone ...`。

## 项目放在哪里

- `~/projects/demo`：Linux 工具和大量小文件访问更适合该位置，profile 与安装代码也采用 Linux 存储。
- `/mnt/c/Users/<用户>/projects/demo`：Windows 编辑器可以直接使用项目，默认沙箱只开放选定项目。自定义盘符挂载位置也可识别。

两种模式均拒绝盘根目录、Users 目录、Windows 用户根和 AppData、.ssh、.aws、.azure、.kube、.docker、.gnupg、.config、.claude*、.codex*、NTUSER.DAT* 等敏感路径及子目录，大小写变体同样检查。Linux symlink 和挂载别名按解析后的来源检查；短文件名形式采用保守拒绝策略。AppData/NTUSER.DAT 元数据用于识别迁移的用户根目录。额外的 Windows 工具/技能挂载应位于选定项目内；其他来源使用 Linux 存储。

共享项目是可写目录。只把工作所需文件放进去，避免放入凭据、通用宿主代理 socket 或由宿主自动执行的任务入口。用户主动复制进项目的秘密、预先放置的硬链接，以及外部文件监控器消费项目内容，都属于共享文件授权的范围。本机已用专用 canary 验证 NTFS 目录联接：指向敏感目录的项目入口被拒绝，项目内指向邻居的联接也无法读取目标。已测组合之外的迁移用户资料和重定向布局仍需专项测试。

## Windows 程序与 WSLg

默认模式禁止 Windows 程序执行，过滤 AF_VSOCK、io_uring 和兼容 ABI；保持私有网络、运行时目录和环境变量。受控 cmd.exe 副本测试验证实际执行边界。

单次开启：

```bash
vps-route run --windows
```

日常 run 的持久设置：

```bash
vps-route config set windows_interop true --apply
vps-route config set windows_interop false --apply
```

开启后提供 Windows 盘读写、Windows PATH、互操作运行时，以及整个 `/mnt/wslg` 共享树的只读挂载；同时接入 X11 和用户目录下的 Wayland socket。Windows 程序使用 Windows 用户权限和网络，也能访问该用户可访问的 Windows 文件。显示端点还具有剪贴板、音频等宿主能力。此模式的 `vps_only_guarantee` 为 false，Linux Claude 进程本身仍在原有网络命名空间里。WSLg 的 X11 测试窗口已在本机显示并关闭，Wayland 用户端点也有单独映射。Wayland 应用、音频和剪贴板操作需要相应应用与单独验证；探测不会读取剪贴板或录音。

开关用于日常 run；auth、install、update、network-test 和 setup-token 保持严格策略。在 WSL2 之外开启会报错。

## 研究模式

`research_filesystem: true` 保留 Linux 资料目录的原有共享策略。在 WSL2 严格模式中，每个 Windows 盘及 WSL 共享运行时挂载都被空的只读层遮住，随后只重新开放选定项目。这可以覆盖 DrvFS 的大小写别名。文件与 socket 形式的别名用 `/dev/null` 覆盖；宿主 socket 扫描只跳过已经实际安装的遮罩。

研究模式仍允许较广的 Linux 文件共享。启动后创建的 Unix socket、主动传入的文件描述符和宿主自动化需要额外信任；该工具的用途是本机工作流隔离。Orca 和 Windows 集成的宿主能力会分别显示在 doctor 中。

## doctor 与常见问题

`vps-route doctor --connect` 分别检查真实沙箱和已配置的 VPS 连接。隔离探测使用生产 `sandbox_command()`、临时 profile、临时项目及假的上游服务，不需要 Claude 账号。探测器还从宿主先运行同一个 cmd.exe 副本作为对照。cmd.exe 使用 `/d /c echo vps-route-probe`，避免触发 Windows AutoRun。默认只检查当前配置；需要单独检查 Windows 能力时，运行 `vps-route doctor --windows`。

结果分为 `blocked_by_policy`、`reachable`、`unsupported`、`inconclusive`。每个必需项目都有明确的 expected 值。必需检查超时或结果不符会阻止就绪。严格模式下，如果宿主关闭了 Windows 互操作，Windows 执行对照会记为 `unsupported`；其余严格策略仍可正常验收。IPv6 不可用时对应项目也会单独标记。隔离检查失败时，`--connect` 仍会执行连接检查，`ready` 综合两者结果。

`doctor --workspace /mnt/c/Users/<用户>/projects/demo` 另行检查该项目的日常启动挂载计划，包括项目说明、技能和可重连 relay。它不启动 Claude，也不在真实项目中写入测试文件。输出将临时 fixture、实际项目计划及 Windows 挂载来源分开列出。启用了需要读取控制凭据的 Orca 运行时时，项目计划会明确标为未确认，避免把不完整计划判为通过。

doctor 检查嵌套 bubblewrap 是否正常运行，并通过 `unshare -rm` 验证继承的遮罩无法卸载。WSLg 的路径存在、socket 连接和应用运行结果分别报告；连上 socket 只能证明连接能力，应用层操作仍需单独验证。

- **wsl1_unsupported**：先将发行版转换为 WSL2。
- **windows_broad_directory_is_not_a_workspace**：进入具体项目子目录。
- **windows_private_directory_is_not_a_workspace**：选择账户和工具配置目录之外的项目。
- **windows_host_proxy_requires_nat**：mirrored 使用 127.0.0.1；先检查未知网络模式。
- **Windows 副本测试 inconclusive**：检查宿主互操作注册和 cmd.exe fixture 的宿主执行；保持默认限制。
- **SSH 指纹不匹配**：停止连接，通过可信渠道重新核实服务器身份。
- **安装下载中断**：确认 VPS 转发和代理路径恢复后重试 `vps-route install`，已安装成功的版本会保留。

安全机制参考：[WSL 互操作](https://wsl.dev/technical-documentation/interop/)、[微软 WSL 网络说明](https://learn.microsoft.com/en-us/windows/wsl/networking)、[16 项审查清单](dev/wsl-escape-paths.zh-CN.md)。
