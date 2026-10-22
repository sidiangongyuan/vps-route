# macOS（实验性）

macOS 为实验性支持。隔离回归测试已在 macOS 27.0.1 上执行；这些测试不代表
真实 Claude 交互登录和模型请求已完成端到端验证。测试方法见文末。

## 要求

- Homebrew，以及用 Homebrew 安装的 `pipx` 和 `gh`。不要依赖系统自带的
  `/usr/bin/python3`（通常是 3.9，可能触发 Xcode 命令行工具安装弹窗）。
- 系统自带的 `/usr/bin/sandbox-exec`、OpenSSH、curl 和 Bash。
- VPS 端要求与 Linux 相同，`setup-vps` 部署的服务也相同。

## 安装

```bash
brew install pipx gh
pipx ensurepath
gh auth login
gh auth setup-git
# 在包含 macOS 实现的本地仓库中安装当前代码。
pipx install .
```

重新打开终端，先运行自检：

```bash
vps-route macos-check
```

输出最后的 `"ready": true` 表示隔离自检通过。之后的配置和登录与 Linux 相同，
见 [完整配置例子](FIRST_RUN.zh-CN.md)：`init`、`setup-vps --apply`、
`doctor --connect`、`install`、`auth login`、`auth status --text`、`run`。

## 支持的命令

| 可用 | macOS 上暂不可用 |
| --- | --- |
| `init`、`setup-vps`、`doctor`、`install`、`auth login`、`auth status`、`run`、`update`、`native-version`、`logs`、`config`、`server-version`、`self-update`、`uninstall`、`macos-check` | `reset`、`auth reset`、`status`、`sessions`、`shell`、`network-test`，以及 `texlive_root` 配置 |

## 隔离方式

Claude 主进程及其子进程在默认拒绝的 Seatbelt 沙箱（`sandbox-exec`）中运行：

- 仅允许 TCP 连接本机一个随机代理端口。沙箱外转发器将流量送入 SSH 隧道，经 VPS
  出网；不允许 UDP、其他端口、Unix socket、监听或接收连接。SSH 重连时代理端口不变。
- 不允许查询宿主 Mach 服务，包括 Launch Services、securityd 和 trustd；不允许
  Apple Events，也不允许向沙箱外进程发信号或获取其任务端口。
- 只有选定项目、独立 profile 和本次会话的临时目录可读写。系统运行时及工具链只读，
  其他宿主文件内容不可读。文件元数据仍可见。项目中的指定凭据路径继续被屏蔽，
  受保护路径的父目录不能通过改名绕过限制。
- 每次启动先验证网络限制、宿主文件和信号限制，以及上述三类 Mach 服务的拒绝结果。
  任一检查失败就拒绝启动。
- TLS 使用证书文件；`SSL_CERT_FILE` 和 `CURL_CA_BUNDLE` 指向 `/etc/ssl/cert.pem`。
  依赖宿主钥匙串或 trustd 的客户端可能无法工作，不应通过放行服务来绕过限制。
- `auth login` 须保存到独立 profile 的 `.claude/.credentials.json`；
  该文件不存在时登录检查失败。已有长期 token 的使用方式见
  [token 导入说明](USAGE.zh-CN.md#使用已有的长期-oauth-token)。

## 额外的本地读写目录

默认只允许当前工作目录、独立 profile 和会话私有临时目录写入。
可通过 `macos_allowed_roots` 为 `run` 增加本地目录，例如：

```bash
vps-route config set macos_allowed_roots '["/absolute/path/work","/absolute/path/misc"]' --apply
```

下次 `run` 生效，子目录也可读写；现有会话不变。目录须存在，
符号链接会解析到真实路径；整个 HOME、HOME 的祖先和受保护凭据目录会被拒绝。
受保护路径的拒绝规则仍优先，链接到白名单外也不会获得额外权限。
`auth login/status`、安装和更新不使用该白名单，网络权限不变。
`doctor` 和 `macos-check` 会将配置的目录纳入隔离检查。

macOS 会话同时设置 `TMPDIR` 和 `CLAUDE_CODE_TMPDIR`，让 Claude 自身的
临时文件保留在会话私有目录中；不会放开整个系统 `/tmp`。

## 沙箱内使用 GitHub CLI

用 Go 1.26 构建的 `gh` 在 macOS 上通过系统服务校验证书，即使设置
`SSL_CERT_FILE`，也可能因沙箱禁止访问 trustd 而报 `x509: OSStatus -26276`。
不要为此开放 Mach 服务或禁用 TLS 校验。

可使用 [沙箱专用 gh 构建和安装说明](../tools/macos-gh/README.md)：
用 Go 1.27.3 构建官方 `gh` v2.93.0，在独立 profile 中安装专用入口，
明确启用基于 `/etc/ssl/cert.pem` 的 Go 证书校验。该入口仍受现有 Seatbelt
和 SSH 代理限制，不增加网络、文件或系统服务权限。宿主 Homebrew 的 `gh` 不变。

GitHub 登录与 Claude 登录独立。沙箱的 GitHub 凭据保存在 profile 的
`.config/gh/hosts.yml`，不能读取宿主钥匙串。复用宿主 token 时，应由宿主
一次性读取，在内存中通过标准输入交给沙箱内的
`gh auth login --hostname github.com --git-protocol https --with-token --insecure-storage`；
该命令的网络验证也必须经过 VPS 通道。不要把 token 放进命令参数、环境变量、
日志或聊天。

这里的 `--insecure-storage` 仅表示使用权限为 `0600` 的明文凭据文件，
不关闭 HTTPS 校验。Claude 及沙箱内子进程可以使用这个 token，其 GitHub
权限与原 token 相同。导入后，在新启动的 `vps-route run` 中检查：

```bash
! command -v gh
! gh auth status --hostname github.com
```

第一条应指向独立 profile 的 `.local/bin/gh`，第二条应显示已登录。

## 与 Linux 的差异和边界

Go、Python、Git 及原生编译器的入口配置和验证见
[沙箱开发工具说明](../tools/macos-dev/README.md)。这些入口不增加隔离权限。

- 不创建独立网络命名空间，通过内核权限限制连接。不能访问 localhost 开发服务，
  也不能在沙箱内启动监听服务；Docker socket、ssh-agent、tmux 不可用。
  唯一允许的连接是本会话 IPv4 TCP 代理；同端口的 IPv6、UDP 及公网地址仍被拒绝。
- `research_filesystem=true` 不受支持，不能开放整个宿主 HOME。Homebrew 工具链只读；
  安装在其他宿主用户目录中的工具不自动获得访问权。
- 转发端口可被本机其他进程使用。宿主用户、启动器、SSH、终端和内核属于信任边界。
- 项目和 profile 仍与宿主共享。宿主 IDE、文件同步器、自动执行器或之后执行的项目
  脚本可以在沙箱外产生动作。已有硬链接、继承资源及系统漏洞也不能靠这些测试排除。
  **若要求恶意代码不能通过任何间接方式借宿主联网，应使用控制出口的独立虚拟机，
  并关闭共享可写目录和宿主 IPC。** Linux 版同样未承诺抵抗任意恶意代码。
- Claude 自带的 `/sandbox` 关闭。项目 `.claude/` 的 settings、commands、agents
  正常加载；hooks 和 MCP 仍关闭。本机 `~/.claude/skills` 不会合并，技能放在独立 profile。
- Apple 已将 `sandbox-exec` 标为 deprecated，需要按 macOS 版本验证。

本地回归测试不需要账号或 VPS，只使用临时文件和本地测试服务：

```bash
python3 -m unittest discover -s tests -p 'test_macos*.py' -v
```

测试需在普通终端运行；已有外层沙箱可能阻止创建测试端口或嵌套 Seatbelt。

完整测试使用 `python3 -m unittest discover -s tests -v`。通用测试和 macOS
测试在 Mac 上运行；依赖 Linux 网络命名空间或 pidfd 的原生测试会明确跳过，
需在 Linux 上另行执行。模拟 Linux CLI 的单元测试显式选择 Linux 分支，不修改
启动器的平台判断。临时目录先解析系统别名，避免 `/var` 和 `/private/var` 的
字符串差异影响路径断言。

终端退出清理使用立即恢复和输入刷新，不等待终端输出排空。重置控制序列的
写入和最后退出提示都有时间上限；终端停止读取或缓冲区填满时，仍会完成清理。此时终端可能
收不到全部重置序列，但不会阻塞启动器退出，也不会增加沙箱权限。
输入和输出转发使用有限缓冲，阻塞时仍处理退出和连接状态。子进程退出后继续
发送剩余输出；连续一秒无法写出数据时，会丢弃未发送的尾部输出并完成退出。
读端持续接收数据时不受总耗时限制。

## 测试清单（首批测试者）

1. 安装后、登录前：`vps-route macos-check > ~/vps-route-check-1.json`
2. 按顺序执行 `init`、`setup-vps --apply`、`doctor --connect`、`install`、`auth login`、
   `auth status --text`，记录每步是否成功和报错原文。
3. 登录后：`vps-route macos-check > ~/vps-route-check-2.json`
4. 在一个测试项目里 `vps-route run`，让 Claude 依次执行下面的命令并记录结果：

   | 命令 | 预期 |
   | --- | --- |
   | `curl -s https://ipinfo.io/ip` | 显示 VPS 出口 IP |
   | `curl -s --noproxy '*' https://ipinfo.io/ip` | 失败 |
   | `ls /Users/你的用户名/.ssh` | `Operation not permitted` |
   | `open https://example.com` | 失败，不弹出浏览器 |
   | `security find-generic-password -s test` | 失败 |
   | `git ls-remote https://github.com/git/git HEAD` | 成功 |

5. 正常对话几轮，再退出 Claude，确认终端恢复正常。
6. 发回：两个 JSON 文件、第 2 步记录、第 4 步结果、`vps-route logs --tail 50`。
   JSON 已把用户目录替换为 `~`，只含钥匙串条目名称和修改时间，不含密钥或登录内容；
   如介意，可删去出口 IP 和条目名称后再发送。
