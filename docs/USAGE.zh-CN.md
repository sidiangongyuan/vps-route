# VPS Route 使用说明

在独立环境中使用官方 Claude Code，通过你自己的 VPS 联网；连接失败不回退直连。
目前仅支持 Linux，不包含 Claude 账号、VPS 或浏览器，也没有发布到 PyPI。

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

有仓库访问权限的 GitHub 账号执行；尚未登录时先运行 `gh auth login`：

```bash
gh repo clone sidiangongyuan/vps-route
cd vps-route
pipx install .
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
| 更新 Claude | `vps-route update` |
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

## 4. 连接保护

Claude 在本机运行，不是在 VPS 上运行。网络路径是：

```text
Claude → 独立网络中的代理 → 加密 SSH → VPS → 目标网站
```

`/status` 中代理应为 `http://127.0.0.1:17878`；它只存在于独立环境，
不是整个系统的代理。最终出口用 `doctor --connect` 核对，不能只看代理端口。

启动前检查通过 VPS 的 HTTPS 连通性；运行中监测 SSH，默认每 30 秒检查一次连通性。
每次并行检测两个独立目标，每个目标都有完整超时；任一目标连通即可。
日常 `run` 检测失败只提示，不关闭 Claude；恢复时再提示一次，不持续刷屏。
SSH 断开时保留会话，自动重连原配置的 VPS；失败后按 5、10、20、30 秒退避，
之后每次等待 30 秒再试。不会自动换 VPS、绕过已配置的前置代理或回退直连。
SSH 主机指纹或身份验证失败会停止自动重试，提示检查配置并手动重启；不会自动信任新指纹。
上游代理断开而 SSH 仍在时，不反复重建 SSH，等待原链路恢复。
恢复不会自动补发已失败的提示词；Claude 可能自行重试，也可能需要你手动重试。
日志记录各目标编号、耗时、失败类别以及断开/恢复事件，不记录对话内容。
出口 IP/国家变化不会导致退出。`doctor --connect` 的 `connectivity_ok: true`
表示连通，`observed_exit_ip` 展示当前出口；IP 查询失败仅提示，不影响连通性结果。
旧配置中的 `expected_ip`、`expected_country` 自动忽略，无需重新初始化。
`connectivity_urls` 可配置 HTTPS 检测目标；`probe_url` 只用于 doctor 查询出口信息。
检查存在延迟，检测网站均不可用可能产生警告，并不证明 Claude API 一定不可用。
等待期间网络隔离不变，没有直连备用路线。Claude 自身崩溃或主动退出不属于链路恢复。
登录、安装、更新等一次性命令仍在 SSH 断开或连续两轮检查失败时结束并返回错误；
`runtime_probe_failures` 仅控制这类命令，日常 `run` 在首轮失败时提示但不强退。

手动终止工具等异常退出时自动重置终端，保留正常运行时的鼠标功能。重置可能清空当前屏幕显示，
不会删除 Claude 会话记录。若旧 Orca 标签页仍刷坐标，先执行 `reset`；无效则新建终端标签页。

浏览器、点击后在本机打开的链接、VS Code 扩展和其他应用不受该网络保护。
更多边界见 [安全说明](SECURITY.md)。

## 5. 更新工具

`vps-route update` 更新的是 Claude。更新 VPS Route 本身，请先关闭它启动的会话，
然后在仓库目录执行：

```bash
git pull --ff-only
pipx reinstall vps-route
```

虚拟环境安装则执行 `.venv/bin/python -m pip install --upgrade .`。
更新本机工具不会自动部署 VPS 服务。

## 6. 卸载

```bash
vps-route uninstall --dry-run
vps-route uninstall
```

先预览再确认。默认永久删除本机工具资料、登录、历史、历史备份、日志和配置，
并移除支持自动卸载的安装包。不删除 VPS 服务、SSH 密钥或项目。
保留 Claude 资料可加 `--keep-profiles --keep-backups`；`-y` 只跳过确认。
