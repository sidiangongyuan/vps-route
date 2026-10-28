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

### 使用长期 token（可选）

完成 `init` 和 `install` 后，可以导入官方 `claude setup-token` 生成的长期 token：

生成时运行 `vps-route run -- setup-token`，在官方网页确认授权；命令最终输出 token。
请自行妥善保存生成结果，再在接收设备上导入。

```bash
vps-route auth import-token
vps-route auth status --text
vps-route run
```

按隐藏提示粘贴 token。工具将其保存在当前 profile 的
`.launcher-auth/setup-token` 中：目录权限 `0700`，文件权限 `0600`。
仅新启动的 Claude 会话使用它；已有会话、网页登录资料、历史和 VPS 设置保持原样。
导入与本地状态查询只检查本地格式、存储和认证来源，服务端有效性由 Claude 的实际请求确认。
格式错误、权限异常或符号链接会阻止启动，不自动换用其他凭据。

替换已有 token 用 `vps-route auth import-token --replace`。
切回网页登录时，先用 `vps-route auth remove-token` 查看，再执行
`vps-route auth remove-token --apply`，最后运行 `vps-route auth login`。
移除只处理本机副本；撤销远端 token 需要另外操作。
重置会把 token 随旧 profile 一并封存；卸载遵循原有 profile/备份保留选项。
导入不会清空旧历史，切换账号前请先明确使用哪个独立 profile。

token 按长期密码保管。它仅注入选定的 Claude 及其子进程环境；同一系统用户和管理员仍可读取。
工具继续过滤外部终端中的其他认证变量，VPS 路由和断线保护保持原样。
长期 token 的权限和功能范围见[官方说明](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)。

### Tab 命令补全

在当前终端启用：

```bash
# Zsh，Linux 和 macOS 均可：
source <(vps-route completion zsh)

# Bash：
source <(vps-route completion bash)
```

将对应的 `source` 一行加入 `~/.zshrc` 或 Bash 实际读取的启动文件，
之后新终端自动加载。macOS 的登录 Bash 可能读取 `~/.bash_profile`。
例如输入 `vps-route au` 后按 Tab，或输入 `vps-route auth imp` 后按 Tab。
支持命令、子命令、选项和路径补全；补全期间不读取账号/连接配置、不连接 VPS。
macOS 隔离运行的真机验证状态见该分支的 macOS 使用说明。

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

在 Orca 中需要多智能体编排时，从 Orca 管理的 Linux 终端启动：

```bash
vps-route run --orca
```

`--orca` 显式授权当前 Claude 调用本机 Orca 运行时。当前适配 Orca 的 Linux
远程 Node CLI 包装器；读取 `ORCA_CLI_COMMAND` 选择同一版本，不自动换到其他 Orca。
沙盒中可使用 `orca-ide` 或 `orca`；它们只连接指定的本地 Unix socket。
只挂载所需 CLI、临时连接信息和 socket，不暴露整个 `.orca` 账号/历史数据库，
也不打开宿主网络。普通 `vps-route run` 默认不开放此接口。

若要同一 profile 的 Orca 主/子 Claude 自动启用，执行一次：

```bash
vps-route config set orca_control true --apply
```

仅在启动终端提供 `ORCA_CLI_COMMAND` 时自动启用；普通 SSH 终端不受影响。
关闭自动启用用 `vps-route config set orca_control false --apply`，新会话生效。
这样，Orca 通过已有 Claude 包装入口启动子会话时，也能接入同一个本地运行时。

Orca 可以代你在宿主机启动终端和 agent，这些操作不受主 Claude 的网络隔离保护。
新建 Claude 必须仍通过 VPS Route 入口；不要使用会绕过该入口的独立二进制或 Chat UI。
本功能不自动更改 Orca 的 agent 配置、账号、hooks 或模型设置。
Orca 重启/升级后需要重新启动该 Claude 会话，以取得新的连接信息。

## 4. 连接保护

Claude 在本机运行，不是在 VPS 上运行。网络路径是：

```text
Claude → 独立网络中的代理 → 加密 SSH → VPS → 目标网站
```

`/status` 中代理应为 `http://127.0.0.1:17878`；它只存在于独立环境，
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
新增的 `diagnosis` 提供故障范围、中文说明 `summary_zh`、下一步建议和
`not_checked`（尚未检查的环节）。可区分本机依赖/前置代理、SSH 授权/指纹、
VPS 连接、VPS 代理服务及转发 HTTPS 的故障。SSH 超时仅能说明 VPS 或中间路径
待排查，无法单凭超时确定责任方；尚未到达的上游也无法据此判断。
仅 `doctor --connect` 增加本机 HTTPS 实测：并行访问 `connectivity_urls` 中的检测站点，
在配置的超时时间内记录各站点结果。配置了 `ssh_http_proxy` 就只通过该代理测试；
未配置则使用系统网络。检测请求不携带 Claude 凭据、不跟随重定向。
`stages` 中的 `local_network` 展示本机实测结果和所用路径；任一站点成功即可确认连通。
本机测试失败仍继续检查 VPS，避免测试网站故障阻断诊断。若本机测试成功而 SSH 失败，
提示排查 VPS/SSH 及到 VPS 的路径；两者均失败则建议优先检查本机网络/VPN，保留不确定性。
这些额外测试仅用于手动诊断，日常启动和会话重试行为保持原样。
诊断适用于任意上游供应商及 VPS 自身出口，不要求住宅或固定出口 IP。
旧配置中的 `expected_ip`、`expected_country` 自动忽略，无需重新初始化。
`connectivity_urls` 可配置 HTTPS 检测目标；`probe_url` 只用于 doctor 查询出口信息。
等待期间网络隔离不变，没有直连备用路线。Claude 自身崩溃或主动退出不属于链路恢复。
登录、安装、更新等一次性命令仍在 SSH 断开或连续两轮检查失败时结束并返回错误；
`runtime_probe_interval_seconds` 和 `runtime_probe_failures` 仅控制这类命令的周期检测。

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
