# macOS（实验性）

macOS 支持已实现，但**尚未在真实 Mac 上验证**。首批测试请按文末“测试清单”执行，
把输出发回作者。确认可用前，不要用于重要账号。

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
pipx install git+https://github.com/sidiangongyuan/vps-route.git
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

Claude 在 macOS 自带的 Seatbelt 沙箱（`sandbox-exec`）中运行：

- 禁止所有直接联网，只允许连接本机一个随机端口；工具在沙箱外把这个端口转发到
  SSH 隧道，经 VPS 出网。SSH 断线重连时端口不变，Claude 无需重启。
- 每次启动先在同一沙箱规则下自检：公网 IPv4/IPv6 TCP、UDP、本机其他端口、
  钥匙串工具 `security` 和 `open` 都必须被拒绝，转发端口必须可连。任一项不符就拒绝启动。
- 用户目录可读写，但屏蔽常见凭据位置：`.ssh`、`.config`、`.aws`、`.gnupg`、shell 配置、
  `~/.claude*`、钥匙串、浏览器和邮件数据、`~/Library/Containers`、`Group Containers`、
  代理客户端（Clash/mihomo/v2ray 等）数据、本工具的其他 profile 和日志，
  以及配置里的 SSH 私钥和 known_hosts。`private_home_entries` 同样生效。
- Claude 无法使用 macOS 钥匙串，登录保存在独立 profile 的 `.claude/.credentials.json`，
  与本机原有 Claude 登录完全分开。登录后若该文件不存在，`auth login` 报错。

## 与 Linux 的差异

- 会话里无法访问本机任何 `localhost` 服务，包括 Claude 自己启动的开发服务器
  （浏览器仍可从外部访问它）。Unix socket 也被禁止：Docker、ssh-agent、tmux 不可用。
- 转发端口在会话期间可被本机其他进程连接，连接会经你的 VPS 出网。
- 屏蔽按路径生效，被屏蔽项的文件名和大小仍可见；它防止误读，不防刻意绕过的恶意代码。
- 通过系统服务间接完成的操作（如其他应用、系统更新与证书检查）不在沙箱网络边界内。
  `open` 命令和 Apple Events 已禁止，Claude 无法自动替你打开浏览器。
- Claude 自带的 `/sandbox` 功能关闭（不能嵌套沙箱）。
- 项目 `.claude/` 里的 settings、commands、agents 会正常加载（Linux 上只加载
  `CLAUDE.md` 和 rules）；hooks 和 MCP 仍然关闭。本机 `~/.claude/skills` 不会合并进来，
  技能请放在独立 profile 的 `.claude/skills/`。
- `/status` 中的代理是 `http://127.0.0.1:随机端口`，不是 17878。

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
