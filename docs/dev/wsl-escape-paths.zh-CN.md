# WSL2 逃逸路径清单：VPS 路由与 Windows 凭据边界

审查日期：2026-10-29。只读代码审查及官方资料核对；未运行逃逸程序、未读取真实凭据、未连接 Windows 服务或修改网络。

基线：`origin/main` 提交 `d40a9c26d85732e0bff3000b72bc9968374816d2`（v0.4.0）。
本文“当前 main”均指该提交。审查期间另一会话新增的 WSL 实现文件不属于本次基线结论。
审查宿主为 Linux `6.8.0-79-generic`，没有真实 WSL2 验证环境；以下 doctor 探测均为待实现、待执行的建议。

## 结论与优先级

当前 main 已有 Linux 网络命名空间、独立 HOME、环境变量清理，以及 `/run`、`/tmp` 隔离。
这些措施有实际保护作用。当前 main 尚不足以宣布“支持 WSL2，且能够防止所有 Windows 侧绕路和凭据访问”。

最重要的四点：

1. AF_VSOCK 必须由有效的 seccomp 策略限制，同时覆盖 io_uring、兼容 ABI 和参数截断；只隐藏 `/init` 不足以证明 WSLInterop 已禁用。
2. 研究模式直接挂载 `/mnt`，会暴露 Windows 文件系统及 WSL 共享通道；普通模式选择 Windows 用户目录作为项目，也存在凭据暴露。
3. `--windows` 允许 Windows 原生程序执行后，Linux 网络命名空间无法约束这些 Windows 进程。该选项应明确标记为降低隔离的模式，不能继续承诺其所有子任务只走 VPS。
4. doctor 应在与真实启动相同的沙箱中验证。现有迷你 namespace 检查和 IPv4 连通性检查不能证明上述边界成立。

优先级：P0 表示严格默认模式发布前必须处理的边界；P1 表示有条件风险或应明确限制的能力。
“未挡住”表示缺少相应控制或存在明确可见面；条件性路径仍需真实 WSL2 验证，本文没有宣称完成利用。

## 基线代码索引

以下行号属于上述提交，可用 `git show d40a9c2:<路径>` 复核。

| 位置 | 当前行为 |
| --- | --- |
| `claude_launcher/runner.py:127–149` | 环境变量白名单；WSL、显示和宿主代理类变量不会自然继承 |
| `runner.py:310–324` | 检查 Linux HOME 及部分敏感目录；没有 Windows 用户目录识别 |
| `runner.py:325–340` | unshare user/net/pid/ipc；独立 proc/dev，tmpfs `/tmp /run /sys`；无 seccomp 参数 |
| `runner.py:341–393` | 研究根目录、项目、profile、隐藏覆盖，以及后续 Orca/skills/TeX 挂载 |
| `runner.py:398–420` | 重设 HOME、CLAUDE_CONFIG_DIR、XDG 和 PATH |
| `runner.py:447–450` | PTY 子进程使用 `close_fds=True` 和新 session |
| `filesystem.py:12–59` | 研究模式包含 `/mnt /media /opt /srv` 和 Linux HOME；敏感名单主要针对 Linux HOME |
| `filesystem.py:72–94` | 启动时扫描宿主 `/proc/net/unix`，隐藏当时可见的部分路径型 socket |
| `cli.py:34–123` | doctor 使用另一个简化 bwrap 探测及宿主侧连接诊断 |
| `cli.py:315–345`、`network_test.py:12–50` | network-test 复用 sandbox_command，传入研究模式；临时项目、IPv4 直连检查及代理请求 |
| `entries/`、`inside.py:35–54` | 沙箱入口、安装/测试入口和回环代理；无 WSL 专用策略 |
| `orca_access.py:32–45` | 显式挂载 Orca 运行时 socket 和 CLI，授予宿主控制能力 |

## 01 · P0 · binfmt_misc 的 F 标志与 `/init`

- **机制：** binfmt_misc 的 `F` 标志会在注册时保留解释器，解释器可以跨挂载视图继续被调用。若 WSLInterop 的实际注册包含该标志，沙箱里看不到 `/init`，仍可能在执行 PE 文件时进入预打开的解释器。Windows 进程能否最终创建还取决于 IPC、vsock 和具体 WSL 实现。WSL 使用 `/init` 处理互操作入口。[内核 F 标志说明](https://docs.kernel.org/admin-guide/binfmt-misc.html)、[WSL 互操作实现说明](https://wsl.dev/technical-documentation/interop/)
- **当前 main：部分阻挡，未证明封闭。** 没有挂载 `/init`，清理环境并隐藏 `/run`；没有 WSL 专用 seccomp。隐藏 binfmt_misc 的控制文件也不能撤销已经注册的解释器。
- **建议修复：** 默认限制实际互操作通信能力，叠加第 02–04 项；不要依赖 PE 扩展名、PATH 或 `/init` 路径检查。不要全局注销宿主 WSLInterop。若考虑 namespace 局部 binfmt 配置，需要先确认目标 WSL 内核支持及真实隔离效果。
- **doctor 探测：** 宿主只读获取注册是否存在、flags 是否含 F，输出布尔值；沙箱中用明确受控、只打印固定标记的 PE fixture 测试执行。必须包含把 fixture 放进允许的项目目录再执行的情况。不能把“系统 cmd.exe 路径不存在”当作执行拦截成功。单独的 `/init` 不可见结果只作为辅助证据。

## 02 · P0 · `WSL_INTEROP`、`/run/WSL` 与 Unix socket 别名

- **机制：** WSL 的互操作客户端可按环境变量或进程祖先信息寻找 interop server。可达的路径型 Unix socket 是能力入口；只读挂载 socket 仍允许通信。环境变量可以由沙箱内程序重新设置。[WSL 互操作说明](https://wsl.dev/technical-documentation/interop/)
- **当前 main：标准路径已有保护，替代路径不完整。** 环境白名单和 tmpfs `/run` 挡住通常入口；研究根目录、显式 workspace 或其他挂载可能带入 socket 别名。现有 snapshot 隐藏也没有覆盖所有后建 socket。
- **建议修复：** 默认整目录隔离 interop 的运行时位置及已识别的共享挂载别名；保留明确授权的 VPS socket。不要通过挂载整个宿主 `/run` 实现 Windows 模式。环境清理继续保留，作为辅助措施。
- **doctor 探测：** 检查变量、标准路径和识别到的别名；用测试专属 Unix responder 验证连接是否被隔离，覆盖启动前、启动后创建两种情况。不要向真实 interop socket 发送执行请求或读取协议秘密。

## 03 · P0 · AF_VSOCK / Hyper-V 通道

- **机制：** vsock 提供虚拟机与宿主通信通道，普通 IPv4/IPv6 路由隔离不足以涵盖它。WSL 的互操作实现也使用 vsock。创建该 socket 不以 `/dev/vsock` 文件可见为前提。[vsock 接口说明](https://man7.org/linux/man-pages/man7/vsock.7.html)、[WSL binfmt 实现](https://raw.githubusercontent.com/microsoft/WSL/master/src/linux/init/binfmt.cpp)
- **当前 main：没有专门阻挡。** 已有 `--unshare-net`，没有 AF_VSOCK 过滤。具体 WSL 内核可能额外限制创建或连接，需要区分平台限制与工具保证。
- **建议修复：** 严格默认模式阻止 AF_VSOCK 的 `socket`，并覆盖 `socketpair` 等相关入口和全部允许的 ABI。过滤器必须在任何不可信子程序执行之前生效，安装失败就拒绝启动；不要静默降级为无过滤。
- **doctor 探测：** 在真正沙箱中只尝试创建不同类型的 AF_VSOCK socket，随即关闭，不连接任何宿主端口。检查预期策略拒绝和过滤器状态；`EAFNOSUPPORT` 仅说明平台暂不支持，不能单独证明策略存在。已有 socket FD 的传入另见第 12 项。

## 04 · P0 · io_uring 创建 socket 绕过 syscall 黑名单

- **机制：** 支持 `IORING_OP_SOCKET` 的内核可在处理 ring 请求时创建 socket。例如 Linux 6.6 的 `io_socket()` 调用 `__sys_socket_file()`；进程无需再执行一次 `socket(2)`。仅过滤该 syscall 会遗漏这条入口。io_uring 仍受相应内核命名空间/权限约束，本项涉及 syscall 过滤的覆盖范围。[Linux 6.6 实现](https://raw.githubusercontent.com/torvalds/linux/v6.6/io_uring/net.c)
- **当前 main：没有限制；拟议的单一 AF_VSOCK socket 过滤也不足。**
- **建议修复：** 对此工具默认拒绝 `io_uring_setup`、`io_uring_enter`、`io_uring_register`，并确保没有继承 ring FD；若将来要开放，另行设计和验证 ring 操作约束。不要假设所有用户的 WSL 已具有更新的内核限制机制。
- **doctor 探测：** 真实沙箱中验证 ring 创建被明确拒绝。专项 WSL 测试用最小 fixture 尝试 `IORING_OP_SOCKET(AF_VSOCK)`，不连接宿主；若内核不支持该 opcode，结果记为 unsupported，并结合策略检查，不能仅凭 opcode 不支持认定过滤充分。

## 05 · P0 · seccomp 的 ABI、参数截断与入口继承

- **机制：** x86-64、x32、i386 的 syscall 入口不同；i386 还有 `socketcall`。seccomp 看到完整寄存器值，内核可能只取 socket domain 的低 32 位。只比较某个原始 64 位值存在漏检风险。过滤器还必须覆盖 fork/exec 后代。[seccomp ABI 与参数说明](https://man7.org/linux/man-pages/man2/seccomp.2.html)
- **当前 main：尚无过滤器。** 现有 PTY wrapper 使用 close_fds 是有利条件，但新 filter FD 如何传到 bwrap 尚需实现审查。
- **建议修复：** 明确支持 x86_64/aarch64；拒绝未支持 ABI。x86_64 显式处理 x32 位和 i386；若无兼容需求，直接拒绝这些 ABI 比解析 socketcall 指针简单。按内核实际参数宽度判断 domain。对 bwrap/entry/安装/维护/测试入口统一施策，保留 no_new_privs，精确传递并关闭 filter FD。
- **doctor 探测：** 检查 Seccomp/NoNewPrivs，并测试 native syscall、带高位的 domain、fork/exec 后的同一操作；兼容 ABI fixture 在支持的平台执行。只查看 `/proc/self/status` 中 Seccomp=2 不能证明装载了正确规则，因为宿主可能已有其他过滤器。

## 06 · P0 · `/mnt/wsl` 的共享 socket 与其他发行版通道

- **机制：** `/mnt/wsl` 及其他共享挂载可能承载跨发行版、容器引擎或互操作服务。若服务能执行宿主任务或转发连接，访问该 socket 即可能绕过本进程 netns。是否存在取决于机器安装的服务，不能把任意目录内文件都描述成可利用入口。
- **当前 main：研究模式暴露。** `RESEARCH_ROOTS` 包含整个 `/mnt`。当前 socket 扫描仅覆盖启动时枚举到的部分路径；普通模式显式选择共享目录作为项目也需要限制。
- **建议修复：** 严格模式屏蔽共享运行时目录及别名；从 mountinfo 识别真实挂载布局，覆盖自定义位置。需要的文件通过明确子目录授权，不直接开放共享运行时根。
- **doctor 探测：** 检查有效挂载树中这些目录的覆盖关系；通过受控 socket fixture 验证替代路径、新建 socket、另一个挂载入口不可通信。只统计类别和结果，不输出实际 runtime token、发行版私有路径内容。

## 07 · P1 · `/mnt/wslg`、X11/Wayland/Pulse 与剪贴板

- **机制：** WSLg 将显示/音频服务器 socket 投射给用户发行版，并集成 Windows/Linux 剪贴板。允许连接这些服务会增加跨边界数据访问；X11、Wayland、音频协议的能力不同。WSLg 可达本身不能直接推出任意宿主命令执行。[WSLg 官方架构](https://raw.githubusercontent.com/microsoft/wslg/main/README.md)
- **当前 main：部分阻挡。** 显示环境变量被清理，`/tmp` 和 `/run` 被隔离；研究模式仍挂载 `/mnt/wslg` 所在的父目录。已存在 socket 的 snapshot 隐藏不能替代目录隔离。
- **建议修复：** 默认隐藏 WSLg 运行时及别名；启用时只挂载实际需要的端点，不开放整个 system distro 或 `/mnt`。建议将显示接入与任意 Windows 程序执行分别表达，避免一个显示需求默认授予宿主执行权限。任何剪贴板/音频授权均应向用户说明。
- **doctor 探测：** 默认验证显示端点不可连接；显式开启模式只用受控显示/剪贴板标记验证，禁止读取用户当前剪贴板或录音。`--dev /dev` 当前不会自然暴露所有宿主 GPU 设备；若新增 `/dev/dxg`，另列设备授权及测试。

## 08 · P0 · 现成的 DrvFS/9p 挂载与再次挂载

- **机制：** 已挂载的 Windows 文件系统通过内核文件系统通道访问宿主文件，读取普通文件不需要进程新建 AF_VSOCK socket。因此封住 socket 不会撤销已有 Windows 文件挂载。WSL 的挂载实现会随版本变化；常见机制包括 drvfs/plan9，不能只匹配一个 fstype。[WSL DrvFS 说明](https://wsl.dev/technical-documentation/drvfs/)
- **当前 main：研究模式的现有挂载可达；重新挂载能力未专门验证。** `/init` 不可见会影响常见 mount.drvfs helper，但不改变已有挂载。是否能创建新 Windows 挂载取决于内核、权限及可达服务，本文未验证可利用。
- **建议修复：** 首先控制暴露的挂载源。显式去掉不需要的 capabilities，评估拒绝 root 启动及 `--disable-userns`；需要限制重新挂载时覆盖传统和新 mount API，且在 bwrap 完成必要挂载之后生效。不要依赖 root/nonroot 默认行为推断权限。[bubblewrap 参数](https://manpages.debian.org/bookworm/bubblewrap/bwrap.1.en.html)
- **doctor 探测：** 在沙箱里读取 mountinfo、有效能力和 userns 策略；检查未授权 Windows 挂载不存在。挂载能力测试仅在测试专属 namespace/空目录使用无敏感 fixture，不能默认重新挂载真实 C 盘。输出明确区分“现有挂载被隐藏”和“新建挂载受限”。

## 09 · P0 · Windows 凭据目录与不同用户、盘符、别名

- **机制：** Windows 用户可读的文件可经暴露的 Windows 文件系统被 Linux 读取。Windows ACL 仍有效；Linux root 不应被描述成自动取得所有 Windows 用户权限。浏览器部分数据可能受 DPAPI 保护，文件可读与成功解密需要分别判断。[WSL 文件权限](https://learn.microsoft.com/en-us/windows/wsl/file-permissions)
- **当前 main：没有 Windows 用户目录防护。** `private_paths()` 针对 Linux HOME，不枚举 Windows 用户或 Windows 用户目录内的 AppData。
- **建议修复：** 至少覆盖所有可见 Windows 用户的 `AppData`、`NTUSER.DAT*`、`.ssh`、`.aws`、`.azure`、`.kube`、`.docker`、`.gnupg`、`.git-credentials`、`.netrc`、`.npmrc`、`.pypirc`、`.claude*`、`.codex*` 及相关工具配置；AppData 内的 UsrClass/浏览器数据包含在目录屏蔽内。清单应可扩展。识别自定义 automount 根、其他盘符和用户资料迁移；考虑大小写别名、启用时的短文件名、NTFS junction/reparse point、其他 bind 路径。仅硬编码 `/mnt/c/Users/*` 无法充分覆盖。
- **doctor 探测：** 用专属 Windows 测试用户目录/fixture 构造上述名称和别名，在沙箱内检查 canary 不可读；实际目录只检查路径元数据/可达性，不打开真实 token、密钥或浏览器库。对未覆盖的重定向目录输出限制说明。路径黑名单无法防止用户主动将秘密复制进允许项目。

## 10 · P0 · 普通/研究模式中的 `/mnt/c` 项目与挂载顺序

- **机制：** workspace 直接 bind，选择 `/mnt/c`、Windows 用户根或其 `.ssh` 等目录，可把凭据目录作为项目开放。后续重新挂载项目、skills、TeX、profile 也可能覆盖早先的隐藏层。
- **当前 main：未覆盖 Windows 情况。** 目录拒绝规则基于 Linux HOME；研究模式的 hidden 同样来源于 Linux HOME。不能只为 research 分支新增 Windows 遮罩。
- **建议修复：** 两种模式都拒绝磁盘根、Windows 用户根、敏感目录及会包含这些目录的过宽 workspace；允许具体、安全的项目子目录。Windows 源检查应用于所有额外挂载。明确最终 mount 顺序，避免后续父目录 rebind 重新暴露敏感项。profile 位于 Windows 文件系统时，也须检查权限语义，不能仅依靠 POSIX mode 判断私密性。
- **doctor 探测：** 覆盖普通/研究模式的合法项目、用户根、`.ssh`、大小写别名、junction、额外挂载源。合法项目里的源码 canary 可读，邻居凭据 canary 不可读；禁止位置应在启动前报清楚原因。用实际待启动 workspace 测，不能仅使用 `/tmp` 下的干净项目。

## 11 · P1 · 写 Windows 文件后由宿主执行

- **机制：** 可写 Windows 挂载可以修改宿主稍后执行的脚本、启动文件或已配置 watcher 的输入。即使即时 PE 执行被封，宿主进程稍后消费这些文件仍在 Linux 沙箱控制之外。
- **当前 main：研究根目录和 workspace 是可写 bind。** Linux HOME 中部分 shell 配置被隐藏，但没有完整 Windows 自启动/宿主配置保护。
- **建议修复：** 严格模式以明确项目子目录为可写范围；额外资料优先只读。研究模式说明共享可写文件与宿主自动化的边界，屏蔽 Windows 自启动/宿主配置位置。Windows 宿主正在自动执行的项目构建脚本，本身就是用户主动开放的宿主能力，不能仅靠路径名单消除风险。
- **doctor 探测：** 只在测试专属目录验证允许/拒绝写入，不写真实启动目录、任务计划、PowerShell profile 或 shell 配置。记录 shared_workspace_writable 的事实，不宣称检查完目录名就阻止了所有延迟执行。

## 12 · P0 · 宿主 socket、继承 FD、SCM_RIGHTS 与 Orca

- **机制：** 路径型 Unix socket 可跨 netns 通信；服务可代为联网/执行，或通过 SCM_RIGHTS 传入已经打开的 socket/目录 FD。只限制新建 socket 无法撤销传入的能力。抽象 Unix socket 的命名空间边界与路径型不同。[network namespace 范围](https://man7.org/linux/man-pages/man7/network_namespaces.7.html)
- **当前 main：部分保护且有显式例外。** close_fds、独立 PID/proc、私有 reconnect socket 目录有效；host_socket_mounts 是启动快照，profile 内 socket 有例外，普通 workspace 没有同样扫描。Orca 接入明确授予宿主运行时控制能力，属于独立的降隔离选项。
- **建议修复：** 默认不给通用宿主服务/运行时目录。socket 目录白名单要按能力定义，readonly 无法约束服务执行。过滤器 FD 只在需要的启动阶段传递。研究模式如继续广泛共享目录，应明确不能提供对恶意代码的完整宿主 IPC 隔离；如承诺更强，需要单独的持续访问控制设计，启动快照无法承担该保证。
- **doctor 探测：** 枚举子进程继承 FD 类型，测试启动后新建的专属 Unix socket 与受控 SCM_RIGHTS fixture；不要接触真实 ssh-agent/docker/Orca token。Orca 开启时输出 host_control_enabled，不能把默认沙箱的保证照搬到该模式。

## 13 · P1 · mirrored 网络、IPv6 与宿主回环服务

- **机制：** WSL mirrored 模式使发行版与 Windows 的网络互通方式不同。但新建 Linux netns 仍隔离其网络设备、IPv4/IPv6 栈及路由。不能仅因 mirrored 支持 localhost 就认定沙箱可通过 localhost 越界；需要检查实际 namespace 和可达端点。[微软 WSL 网络说明](https://learn.microsoft.com/en-us/windows/wsl/networking)、[Linux netns 定义](https://man7.org/linux/man-pages/man7/network_namespaces.7.html)
- **当前 main：普通 Linux IP 隔离已有设计。** `--unshare-net` 是核心保护；已有检查偏向 IPv4、回环接口和单个测试地址，没有 mirrored/IPv6/宿主服务覆盖。
- **建议修复：** 保留独立 netns，不为兼容 WSL 改成 share-net。IPv4、IPv6 和所有出站协议都按 namespace 边界限制；只放行指定 VPS relay。Windows 原生进程的联网另见第 14 项。
- **doctor 探测：** 在 NAT、mirrored 分别验证真实沙箱接口及 IPv4/IPv6 路由；使用父 namespace 中的受控 TCP/UDP canary 验证回环、宿主地址均不可达，同时验证批准的 relay 可用。直接请求超时只能记 inconclusive，明确无路由/策略拒绝或受控端点实验才可支持阻断结论。断开测试专属 relay 后不应出现另一条可用路径；不要中断用户的活动隧道。

## 14 · P0（严格保证边界）· `--windows` 原生宿主执行

- **机制：** Windows 原生程序由宿主启动后使用 Windows 网络栈与用户权限；可以运行浏览器、HTTP 客户端、凭据工具，或通过 wsl.exe 启动沙箱外的 Linux 命令。Linux 内的 HTTP_PROXY、netns 和文件遮罩无法全面约束它们。此结论由 WSL 宿主执行架构和上述命名空间边界推导。
- **当前 main：没有该开关。** 这是拟议设计必须面对的权限变化。开启 interop 后，不能继续承诺“所有被 Claude 启动的任务都走 VPS”或“Windows 凭据不可访问”。
- **建议修复：** 默认关闭；显式开启时记录并展示 `windows_interop_enabled=true`、`host_execution_allowed=true`、`vps_only_guarantee=false`。若产品坚持开启后仍严格限制宿主出网，则需要 Windows 侧独立执行身份及网络/权限强制机制，单纯设置代理变量不充分。普通 WSLg 显示需求与任意 Windows 执行分开表达更清晰。
- **doctor 探测：** opt-in 模式用只输出固定标记的 Windows fixture 确认能力已授权；默认模式同 fixture 必须无法完成宿主执行。无需连接外部站点、读取真实 Windows 凭据来证明风险。若另做 Windows 网络隔离测试，应独立授权并使用受控目标。

## 15 · P1 · 浏览器 opener、凭据 helper 与所有入口一致性

- **机制：** 浏览器/编辑器 opener、Windows Git credential helper 等若通过互操作启动，会把用户操作交给宿主，其网络和认证走宿主环境。终端里由用户 Ctrl+点击 URL 也属于终端宿主行为，Linux 沙箱无法强制其路由。
- **当前 main：浏览器 hook 有收敛。** `entries/browser_hook.py` 校验官方授权链接并返回，不自动调用宿主浏览器；其他 entries 转入各自 Python 入口，当前没有统一 WSL 策略。PTY wrapper 已使用独立 session，不能简单套用“没有 setsid 就能注入外层终端”的结论。
- **建议修复：** 在共同沙箱构造边界接入策略，覆盖 run/auth/setup-token/install/update/network-test 及未来入口。严格模式不因登录 convenience 自动开 wslview/cmd/start；人工打开链接的网络边界明确说明。可选 interop 保持显式授权。
- **doctor 探测：** 用不联网的 entry fixture 检查不同入口策略一致、没有继承宿主 Windows PATH/WSLENV/WSL_INTEROP；验证 browser hook 只处理 URL、不启动宿主程序。不要用真实授权码或账号执行测试。

## 16 · P0 · doctor 的“真沙箱”与结果解释

- **机制：** 检测进程与业务进程使用不同 mount/seccomp/namespace 参数时，检测通过不能证明业务受保护。环境缺失导致的失败也容易被误记为策略拦截。
- **当前 main：不满足完整 WSL 验证。** `namespace_available()` 使用简化 bwrap；doctor 还有宿主侧诊断。network-test 已复用生产 sandbox_command 并传入研究配置，是可复用基础，但采用临时 workspace，缺少 WSL 探测以及实际项目/额外挂载的完整等价性。
- **建议修复：** 将宿主到 VPS 的诊断与沙箱隔离诊断分开输出；后者通过生产 builder、同样配置和工作目录运行无账号 probe command。不得为了 doctor 放松规则。结果区分 `blocked_by_policy`、`reachable`、`unsupported`、`inconclusive`，附有限错误码；超时、缺少 fixture、未支持架构都不能自动视为通过。
- **doctor 探测：** 组合上述项目，检查实际有效配置。无需启动 Claude、读取 token、发送模型请求或固定出口 IP。输出是否可连配置 VPS/relay、沙箱代理请求是否成功、直连通道是否阻断及当前降隔离选项。诊断日志默认隐去真实 IP、SSH 用户、主机指纹和凭据目录明细。

## 建议实施与真实环境验证顺序

1. **严格默认路径：** seccomp fail-closed、AF_VSOCK/io_uring/ABI、清理 host IPC、全部模式的 Windows workspace 和挂载检查。
2. **挂载策略：** 识别实际 Windows/WSL 挂载，落实目录覆盖与别名限制；保证后续 skills/TeX/profile 挂载不会重新打开禁区。
3. **显式降低隔离：** 对 windows_interop、WSLg、Orca 分别报告能力；保持默认模式保证范围清晰。
4. **探测复用：** doctor 复用真实 builder；先合成 fixture，再在真实 WSL2 测。

最小有信息量的真实 WSL2 覆盖矩阵：

| 条件 | 关注点 |
| --- | --- |
| x86_64；目标 ARM64 若宣称支持 | native/compat syscall、filter 加载与继承 |
| NAT / mirrored | IPv4/IPv6、父 namespace 服务、relay 单一路径 |
| 普通 / research | Windows 文件、运行时目录、后建 socket |
| Linux 项目 / Windows 项目 | workspace 拒绝规则、合法源码可用、敏感邻居隐藏 |
| 默认 / windows_interop 显式开启 | 严格模式拒绝宿主执行；开放模式清楚报告权限放宽 |
| WSLg 开 / 关，常见共享服务存在 / 不存在 | socket 别名及 unsupported 状态解释 |

探测仅使用可丢弃 fixture 和测试专属网络端点，不枚举或读取实际秘密。
WSL 版本、内核、架构和有效配置需随诊断保留；单台机器通过只支持该已测组合。

## 审查边界

本次已完成基线静态审查及机制资料核对，没有执行 WSL2 运行时验证，也没有审阅另一会话尚在编写的实现。
当前项目自身的 `docs/SECURITY.md` 已说明其非恶意代码沙箱定位；研究模式、共享可写项目与宿主控制功能使这一限制尤其重要。
如果目标升级为抵抗主动恶意代码，广泛共享文件系统、后建 Unix socket、宿主任务代理都需要纳入额外设计，不能只增加 AF_VSOCK 过滤后宣布完整隔离。
本清单不分析内核未知漏洞，不承诺阻止有权限的宿主用户主动把凭据送进项目，也不将账号服务方的风控结果归因于上述任何路径。
