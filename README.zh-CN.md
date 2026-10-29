<h1 align="center">VPS Route</h1>

<p align="center">
  <b>在隔离环境中运行 Claude Code，所有网络流量只经你自己的 VPS 出去。</b>
</p>

<p align="center">
  <img alt="版本 0.4.0" src="https://img.shields.io/badge/version-0.4.0-0b7a6a">
  <img alt="许可证 MIT" src="https://img.shields.io/badge/license-MIT-0b7a6a">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-0b7a6a">
  <img alt="平台 Linux 和 macOS" src="https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows%20WSL2-0b7a6a">
</p>

<p align="center">
  <a href="README.md">English</a> · 简体中文
</p>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/network-path-zh-dark.svg">
  <img alt="Claude Code 在你电脑上的隔离沙箱里运行，唯一的出口是本机代理，经 SSH 隧道到你的 VPS，再访问互联网。沙箱直连外网的路径被阻断。" src="docs/images/network-path-zh-light.svg" width="100%">
</picture>

## 为什么用 VPS Route

- **默认只有一个出口。** Claude Code 和它执行的 Linux 命令运行在一个没有外网路由的网络命名空间里，
  唯一的出口是本机代理，经 SSH 连到你的 VPS。
- **断线不回退直连。** SSH 断开时会话保持打开，VPS Route 在后台重连同一台 VPS。
  任何请求都不会从你的电脑直接发出。
- **独立的一套 Claude。** 安装、登录、历史和技能都放在独立的 profile 里。
  你原有的 `~/.claude` 目录和 `claude` 命令保持原样。
- **官方 Claude Code。** 配置时经你的 VPS 运行 Anthropic 官方安装程序。
  VPS Route 不打包、也不修改 Claude Code。
- **小巧、易读。** 本机和 VPS 两端都只用 Python 标准库，VPS 端只有一个 systemd 服务。

## 支持的平台

| 平台 | 状态 |
| --- | --- |
| Linux | 支持。已在 Ubuntu 22.04 上验证，其他发行版未经测试。 |
| macOS | 支持，位于单独维护的 [`macos-support`](https://github.com/sidiangongyuan/vps-route/tree/macos-support) 分支，见 [macOS](#macos)。 |
| Windows (WSL2) | 本分支支持，验证环境为 Ubuntu 22.04。见 [Windows](#windowswsl2)。 |

## Windows（WSL2）

在 WSL2 的 Ubuntu 中安装和运行本分支，参见 [Windows 中文指南](docs/WINDOWS.zh-CN.md)。
默认模式隔离 Windows 执行、运行时和项目之外的盘符文件。
显式使用 `vps-route run --windows` 可访问 Windows 程序与 WSLg；
这些宿主程序使用 Windows 网络和用户权限，Linux Claude 进程继续经 VPS 联网。

## 需要准备

- **你的电脑：** Linux，Python 3.10+（包括 `/usr/bin/python3`）、bubblewrap、OpenSSH、
  curl、Bash 和 git，并允许非特权用户命名空间。
- **你的 VPS：** 带 systemd 的 Linux，`/usr/bin/python3` 3.10+，支持 SSH 密钥登录，
  能访问外网 HTTPS 和 DNS。非 root 用户需要可用的 systemd 用户服务。
- **你自己：** 能登录 VPS 的 SSH 密钥、经服务商核对过的主机指纹，以及能使用 Claude Code 的
  Claude 账号。

## 安装

Ubuntu 或 Debian：

```bash
sudo apt-get install python3 python3-venv pipx git bubblewrap openssh-client \
  netcat-openbsd curl ca-certificates
pipx install "git+https://github.com/sidiangongyuan/vps-route.git@v0.4.0"
pipx ensurepath
```

重新打开终端，执行 `vps-route --version` 确认安装成功。本工具没有发布到 PyPI。

## 首次配置

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/first-run-zh-dark.svg">
  <img alt="六个步骤：init、setup-vps --apply、doctor --connect、install 和 auth login 只需执行一次；run 是日常命令。" src="docs/images/first-run-zh-light.svg" width="100%">
</picture>

```bash
vps-route init
vps-route setup-vps --apply
vps-route doctor --connect
vps-route install
vps-route auth login
vps-route auth status --text
```

- **`init`** 询问 VPS 的 IP 地址、SSH 用户、端口、密钥、`known_hosts` 文件，以及 VPS 上的
  服务 socket 路径。大多数项目用默认值即可。每一项的含义见
  [完整配置例子](docs/FIRST_RUN.zh-CN.md)。
- **`setup-vps --apply`** 在 VPS 上安装连接服务，不改动 SSH、VPN 和防火墙设置。
  VPS 上已有兼容的服务时可以跳过。
- **`doctor --connect`** 必须显示 `"connectivity_ok": true` 再登录。
  `observed_exit_ip` 是当前的出口 IP，仅供查看。
- **`auth login`** 会打印官方登录网址。VPS Route 不接管浏览器的网络，请在你打算让这个账号
  使用的网络环境中打开网址，再把一次性 code 粘贴回同一个终端。

重复执行 `init` 或 `install` 是安全的，已有的配置、登录和历史都会保留。

## 日常使用

```bash
cd ~/projects/my-app
vps-route run
```

VS Code Remote SSH 的终端里也是同样的用法。

| 要做的事 | 命令 |
| --- | --- |
| 检查线路和出口 IP | `vps-route doctor --connect` |
| 查看登录状态 | `vps-route auth status --text` |
| 更新 Claude Code | `vps-route update` |
| 更新 VPS Route | `vps-route self-update --apply` |
| 查看 Claude Code 版本 | `vps-route native-version` |
| 恢复之前的对话 | `vps-route run -- --resume 会话ID` |
| 关闭所有自定义配置启动 | `vps-route run --safe-mode` |
| 查看最近的连接日志 | `vps-route logs --tail 20` |

## Claude 能访问什么

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/isolation-zh-dark.svg">
  <img alt="可读写：项目目录、独立 profile 和私有的 /tmp。只读：系统程序、系统设置和你的技能。看不到：家目录里的其他内容、密钥和账号、你原有的 Claude。网络：只能经 VPS 访问 HTTPS。" src="docs/images/isolation-zh-light.svg" width="100%">
</picture>

项目里的 `CLAUDE.md`、规则、自动记忆和技能照常可用。`~/.claude/skills` 和
`~/.agents/skills` 中的技能以只读方式加载；profile 里有同名技能时优先使用 profile 里的。
Hooks、MCP 服务、账号 connectors、remote control 和云端技能同步都已关闭。

会话里只能经 VPS 访问 HTTPS，其他连接都会失败：SSH 和经 SSH 的 `git push`、
`http://` 网址、443 以外的端口、局域网地址，以及你电脑上 `localhost` 的服务。
这类操作请在普通终端里执行。

需要访问更多文件时，可以打开可选的 `research_filesystem`：它会挂载你的家目录以及
`/mnt`、`/media`、`/opt`、`/srv`，同时继续隐藏常见的凭据位置。
用 `private_home_entries` 添加你自己要隐藏的条目：

```bash
vps-route config set research_filesystem true --apply
vps-route config set private_home_entries '[".my-vpn", ".tool-*"]' --apply
```

只在可信的工作中打开它。哪些内容仍然可见，见 [安全说明](docs/SECURITY.md)。

## 连接如何运作

- 启动前，VPS Route 经你的 VPS 访问两个独立的检测网站，任一成功即可。
- 会话期间不做定期检测。请求错误和重试由 Claude 自己显示和处理，
  VPS Route 不会因为断网关闭 Claude。
- SSH 断开后，VPS Route 依次在 5、10、20、30 秒后重连同一台 VPS，之后每 30 秒重试一次。
  它不会换用其他 VPS，也不会直连。
- SSH 主机指纹或身份验证失败时停止重连并给出提示。排除原因后重新启动会话。
- `install`、`update` 和 `auth login` 在 SSH 断开或连续两次线路检查失败时停止。
- 出口 IP 变化不会让 Claude 退出。`doctor --connect` 会显示当前出口 IP，并在 `diagnosis`
  中指出出问题的环节：本机配置、SSH、VPS 服务，或 VPS 自己的上游。

## 可选功能

### 长期 token

可以不用浏览器登录，改为导入官方 `claude setup-token` 命令生成的 token，
例如用 `vps-route run -- setup-token` 生成：

```bash
vps-route auth import-token
vps-route auth status --text
```

在隐藏提示中粘贴 token。它以 `0600` 权限保存在 profile 里，新会话会优先使用它。
用 `auth import-token --replace` 替换，用 `auth remove-token --apply` 删除本机副本。
请像保管密码一样保管它，详见 [安全说明](docs/SECURITY.md#setup-token-credentials)。

### Tab 补全

```bash
source <(vps-route completion zsh)    # Bash：source <(vps-route completion bash)
```

把同一行加进 `~/.zshrc` 或 `~/.bashrc`，之后每个新终端都会自动加载。

### Orca 多智能体控制

在 Orca 管理的 Linux 终端里，`vps-route run --orca` 允许 Claude 控制本机的 Orca 运行时。
`vps-route config set orca_control true --apply` 会让 Orca 终端自动启用这一功能。
Orca 能在宿主机上启动终端和 agent，这些操作在沙箱之外，所以只在你确实想授予这种能力时启用。
详见 [安全说明](docs/SECURITY.md#optional-orca-control)。

## 更新与卸载

先关闭所有 VPS Route 会话，然后：

```bash
vps-route self-update                       # 查看当前版本和最新版本
vps-route self-update --apply               # 安装最新版本
vps-route self-update --rollback --apply    # 回到更新前的版本
```

更新只来自 GitHub 上正式发布的版本。`self-update` 失败时，可以直接重装某个版本，例如
`pipx install --force "git+https://github.com/sidiangongyuan/vps-route.git@v0.4.0"`。
之后如果 `vps-route server-version` 显示 `"compatible": false`，再执行一次
`vps-route setup-vps --apply`。

卸载前先预览要删除的内容：

```bash
vps-route uninstall --dry-run
vps-route uninstall
```

这会永久删除本机的 profile、登录、历史、备份、日志、配置和程序本身。
想保留 Claude 资料，加上 `--keep-profiles --keep-backups`。VPS 上的内容不会被改动；
要移除 VPS 上的服务，在 VPS 上执行 `systemctl --user disable --now vps-route-connect.service`
（root 用户去掉 `--user`）。

## macOS

macOS 版位于 [`macos-support`](https://github.com/sidiangongyuan/vps-route/tree/macos-support)
分支，由一位合作者维护，已在真实的 Mac 上测试。它用系统自带的 Seatbelt 沙箱隔离 Claude，
需要用 Homebrew 安装的 `pipx`。

```bash
pipx install "git+https://github.com/sidiangongyuan/vps-route.git@macos-support"
vps-route macos-check
```

其余步骤见 [macOS 使用说明](https://github.com/sidiangongyuan/vps-route/blob/macos-support/docs/MACOS.zh-CN.md)。
更新时执行同一条 `pipx install --force` 命令。Mac 上不要用 `self-update`：它安装的是 `main`
上的正式版，目前还不包含 macOS 支持。

## 常见问题

| 情况 | 处理 |
| --- | --- |
| 提示找不到 `vps-route` | 执行 `pipx ensurepath`，然后重新打开终端。 |
| `network_isolation_unavailable` | 安装 bubblewrap。Ubuntu 23.10 及以后的版本用 AppArmor 限制非特权用户命名空间；管理员可以为 `/usr/bin/bwrap` 添加允许 `userns` 的 AppArmor 配置，这会放宽系统策略。不要尝试跳过隔离检查。 |
| SSH 主机指纹校验失败 | 通过 VPS 服务商核对指纹，保持校验开启。 |
| SSH 登录失败或超时 | 检查用户名、密钥、端口，以及你配置的本机代理。 |
| Claude 里 `git push`、SSH 或 `http://` 失败 | 属于正常现象：会话里只能经 VPS 访问 HTTPS。请在普通终端里执行。 |
| Claude 提示连接错误、正在重试 | 等 Claude 自己重试，SSH 会在后台重连。重试用完后，用 `vps-route doctor --connect` 检查，恢复后再重试请求。 |
| 出口 IP 显示为空，但连通正常 | IP 查询服务暂时不可用，不影响使用。 |
| 退出后终端出现 `35;142;34M` 这类数字 | 执行系统命令 `reset`，或者新开一个终端标签页。 |
| 其他问题 | 运行 `vps-route doctor --connect --report ~/vps-route-report.json` 和 `vps-route logs --tail 50`，按需删去出口 IP 后提交 issue。 |

更多内容：[使用说明](docs/USAGE.zh-CN.md) · [完整配置例子](docs/FIRST_RUN.zh-CN.md) · [安全说明](docs/SECURITY.md)（英文）。

## 安全与隐私

VPS Route 管的是 Claude Code 及其启动的进程的网络路径。以下内容不在保护范围内：

- 你的浏览器、在终端之外打开的链接、编辑器扩展和其他应用；
- 恶意代码：Claude 能读取你启动它时所在项目目录里的全部内容；
- VPS 服务商能看到的信息，包括访问目标和连接时间；
- 远端服务如何看待你的流量：本工具不隐藏系统信息，不评估 IP 信誉，也不能防止账号受限。

日志只记录连接事件和耗时，不记录提示词、登录 code 或 token。
如需私下报告安全漏洞，见 [SECURITY.md](docs/SECURITY.md#reporting-a-vulnerability)。

## 许可证

[MIT](LICENSE)。Claude Code 单独安装，遵循 Anthropic 的条款。
VPS Route 是独立项目，与 Anthropic 没有关联，也未获其认可。
