# macOS（实验性）

macOS 为实验性支持。隔离回归测试已在 macOS 27.0.1 上执行；真实 Claude 登录、
API 请求和 VPS 端到端流程尚未验证。测试方法见文末。

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
- 登录须保存到独立 profile 的 `.claude/.credentials.json`；该文件不存在时登录检查失败。

## 与 Linux 的差异和边界

- 不创建独立网络命名空间，通过内核权限限制连接。不能访问 localhost 开发服务，
  也不能在沙箱内启动监听服务；Docker socket、ssh-agent、tmux 不可用。
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
