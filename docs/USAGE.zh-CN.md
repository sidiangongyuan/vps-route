# VPS Route 使用说明

在独立环境中使用官方 Claude Code，通过你自己的 VPS 联网；连接失败不回退直连。
支持 Linux，只在 Ubuntu 22.04 上验证过，其他发行版未经测试。
macOS 为实验性支持，原生隔离已做本地测试，真实账号和 VPS 端到端流程仍待验证，见 [macOS 说明](MACOS.zh-CN.md)。
不包含 Claude 账号、VPS 或浏览器，也没有发布到 PyPI。

首次使用请看 [完整配置例子](FIRST_RUN.zh-CN.md)。

## 1. 安装工具

客户端需要 Python 3.10+（包含 `/usr/bin/python3`）、bubblewrap、OpenSSH、curl、
Bash；VPS 需要系统 Python 3.10+、systemd、SSH 密钥登录和外网访问。
客户端必须支持非特权网络命名空间，否则工具会拒绝启动。

Ubuntu/Debian 安装依赖：

```bash
sudo apt-get install git gh python3 python3-venv pipx bubblewrap openssh-client \
  netcat-openbsd curl ca-certificates
```

仓库是私有的。用有访问权限的 GitHub 账号登录，并让 git 使用这个登录，
然后直接从 GitHub 安装，不需要 clone 仓库：

```bash
gh auth login
gh auth setup-git
pipx install git+https://github.com/sidiangongyuan/vps-route.git
pipx ensurepath
```

找不到 `vps-route` 时重新打开终端。虚拟环境安装方法见完整例子。

## 2. 首次配置和登录

先准备能登录 VPS 的 SSH 私钥，并向 VPS 管理员核对主机指纹。

```bash
vps-route init
vps-route setup-vps --apply
vps-route doctor --connect
vps-route install
vps-route auth login
vps-route auth status --text
```

- `init`：填写 VPS 信息，创建或复用独立环境。不清空账号、历史或本机原有资料。
- `setup-vps --apply`：在 VPS 安装本工具的连接服务；已有兼容服务可跳过。
- `doctor --connect`：检测连通性，并展示当前实际出口 IP；不要求固定 IP。
- `install`：通过 VPS 下载官方最新版 Claude。已安装则提示并跳过，不重复下载。
- `auth login`：显示官方登录网址；授权码粘贴回发起登录的同一终端。

登录网页需要由你保证走预期出口。本工具不会接管浏览器流量。
已有正常环境不需要重新 `init` 或 `install`，直接使用即可。
配置冲突、损坏或安装入口异常时会报错，不自动覆盖原件。

## 3. 日常使用

```bash
cd /你的项目路径
vps-route run
```

VS Code Remote SSH 集成终端也是同样的操作。新用户请使用 `vps-route run`；
安装不会接管已有 `claude` 命令，也不会配置 Claude 的 VS Code 扩展。

| 操作 | 命令 |
| --- | --- |
| 检查连接与出口 | `vps-route doctor --connect` |
| 查看登录状态 | `vps-route auth status --text` |
| 更新 Claude（不是本工具） | `vps-route update` |
| 更新本工具 | `vps-route self-update --apply` |
| 查看 Claude 版本 | `vps-route native-version` |
| 恢复会话 | `vps-route run -- --resume 会话ID` |
| 排查技能或项目配置问题 | `vps-route run --safe-mode` |
| 查看最近连接日志 | `vps-route logs --tail 20` |

独立资料位置为 `~/.local/share/claude-launcher/profiles/default/`，
其中 `.claude/` 保存登录、历史、技能和记忆。更改 profile 名称后，将 `default`
替换成实际名称。不要分享此目录。

支持项目 `CLAUDE.md`、规则、自动记忆和技能。独立目录的 `.claude/skills/`
优先加载，也会只读加载本机 `~/.agents/skills/` 和 `~/.claude/skills/` 中符合要求的技能。
Hooks、MCP、账号 connectors、remote control 和云端技能同步关闭。

本机原有的 `~/.claude`、`~/.claude.json` 和已有的 `claude` 命令不会被移动或删除，
`reset` 和 `uninstall` 也不会碰它们。

### Claude 能访问什么

在 `vps-route run` 会话里，只有经 VPS 访问公网 HTTPS（443 端口）可以连通，
包括 Claude API、`pip`/`npm` 下载和 HTTPS 网页。以下操作在会话里**不可用**：

- 通过 SSH 的 `git push`/`git clone`（`git@github.com:...`），以及任何 SSH 连接
- `http://` 网址和 443 以外的端口
- 内网地址，以及本机上运行的服务（宿主机的 `localhost`）

这类命令请在普通终端里执行。

## 4. 连接保护

Claude 在本机运行，不是在 VPS 上运行。网络路径是：

```text
Claude → 独立网络中的代理 → 加密 SSH → VPS → 目标网站
```

`/status` 中代理应为 `http://127.0.0.1:17878`（macOS 为随机端口）；它只供独立环境使用，
不是整个系统的代理。最终出口用 `doctor --connect` 核对，不能只看代理端口。

启动前检查通过 VPS 的 HTTPS 连通性：并行检测两个独立目标，任一连通即可。
日常 `run` 不再定期访问检测网站，也不插入额外的断开/恢复提示；
请求错误和重试由 Claude 自己显示与处理，工具不会因为断网关闭 Claude。
SSH 断开时保留会话，自动重连原配置的 VPS；失败后按 5、10、20、30 秒退避，
之后每次等待 30 秒再试。不会自动换 VPS、绕过已配置的前置代理或回退直连。
SSH 主机指纹或身份验证失败会停止自动重试，提示检查配置并手动重启；不会自动信任新指纹。
上游代理断开而 SSH 仍在时，工具不反复重建 SSH，由 Claude 的请求重试等待恢复。
工具不自动补发提示词；Claude 自带重试耗尽后，需在链路恢复后手动重试请求。
日志记录启动前检测的目标编号、耗时、失败类别，以及 SSH 断开/重连事件；
SSH 重连成功不代表上游/API 已恢复，实际请求结果由 Claude 展示。日志不记录对话内容。
出口 IP/国家变化不会导致退出。`doctor --connect` 的 `connectivity_ok: true`
表示连通，`observed_exit_ip` 展示当前出口；IP 查询失败仅提示，不影响连通性结果。
旧配置中的 `expected_ip`、`expected_country` 自动忽略，无需重新初始化。
`connectivity_urls` 可配置 HTTPS 检测目标；`probe_url` 只用于 doctor 查询出口信息。
等待期间网络隔离不变，没有直连备用路线。Claude 自身崩溃或主动退出不属于链路恢复。
登录、安装、更新等一次性命令仍在 SSH 断开或连续两轮检查失败时结束并返回错误；
`runtime_probe_interval_seconds` 和 `runtime_probe_failures` 仅控制这类命令的周期检测。

手动终止工具等异常退出时自动重置终端，保留正常运行时的鼠标功能。重置可能清空当前屏幕显示，
不会删除 Claude 会话记录。若终端仍在刷坐标数字，先执行系统命令 `reset`；无效则新开终端标签页。

浏览器、点击后在本机打开的链接、VS Code 扩展和其他应用不受该网络保护。
更多边界见 [安全说明](SECURITY.md)。

## 5. 更新工具

`vps-route update` 更新的是 Claude。更新 VPS Route 本身用 `self-update`，
需先关闭它启动的所有会话。默认只预览，加 `--apply` 才安装，只安装 GitHub 上正式发布的版本：

```bash
vps-route self-update                      # 查看当前版本和最新版本
vps-route self-update --apply              # 升级到最新版本
vps-route self-update --rollback --apply   # 回到升级前的版本
```

需要安装时用的同一个 `gh` 登录。如果 `self-update` 失败，直接重装最新代码：

```bash
pipx install --force git+https://github.com/sidiangongyuan/vps-route.git
```

虚拟环境安装则执行 `~/.venvs/vps-route/bin/pip install --upgrade --force-reinstall git+https://github.com/sidiangongyuan/vps-route.git`。

更新本机工具不会自动更新 VPS 服务。若 `vps-route server-version` 显示
`"compatible": false`，再执行一次 `vps-route setup-vps --apply`。

## 6. 卸载

```bash
vps-route uninstall --dry-run
vps-route uninstall
```

先预览再确认。默认永久删除本机工具资料、登录、历史、历史备份、日志和配置，
并移除支持自动卸载的安装包。不删除 VPS 服务、SSH 密钥或项目。
保留 Claude 资料可加 `--keep-profiles --keep-backups`；`-y` 只跳过确认。

## 使用已有的长期 OAuth token

如果已有 `claude setup-token` 生成的长期 token，在自己的终端执行：

```bash
vps-route auth import-token
vps-route auth status --text
vps-route run
```

在隐藏输入提示处粘贴 token 并回车。不要将 token 放进命令参数或聊天。
导入仅检查格式，不验证服务端有效性；`auth status` 也不能代替真实模型请求。
每次启动会从独立 profile 的 `.launcher-auth/oauth-token` 读取凭据，
仅向隔离的 Claude 进程注入 `CLAUDE_CODE_OAUTH_TOKEN`。宿主认证变量仍被过滤，
网络和文件隔离规则不变。导入和移除前须退出正在运行的 vps-route 会话。

该文件保存明文 token，目录权限 0700、文件权限 0600。Claude 及子进程能读取它；
profile 备份也包含凭据。不要分享 profile。导入 token 后如需改用原有浏览器登录：

```bash
vps-route auth remove-token
vps-route auth login
```

移除只删除本地 token，不在服务端撤销它，也不删除原有浏览器登录凭据。
