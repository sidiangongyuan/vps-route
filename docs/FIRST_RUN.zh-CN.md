# 首次配置：完整例子

以下 `xxx` 都是占位符，需换成自己的信息。除标注“在 VPS 上”的操作外，
命令均在运行 Claude 的 Linux 机器执行。macOS 的安装和工具更新需选择
`macos-support` 分支，见 [macOS 说明](MACOS.zh-CN.md)，其余步骤相同。

## 1. 准备什么

| 信息 | 示例格式 | 从哪里获取 |
| --- | --- | --- |
| VPS 地址 | `xxx.xxx.xxx.xxx` | VPS 控制台的连接 IP，不能填域名 |
| SSH 端口 | `22` | VPS 管理员提供，未改端口通常为 22 |
| SSH 用户 | `xxx` 或 `root` | 能通过密钥登录的 VPS 用户 |
| 本机私钥路径 | `~/.ssh/id_ed25519_vps_route` | 本机密钥文件，不是 VPS 的路径 |
| 主机记录路径 | `~/.ssh/known_hosts` | 核对主机指纹后保存的本机 SSH 记录 |
| VPS 服务路径 | `/home/xxx/.local/state/claude-launcher/connect.sock` | 下方说明；不是端口或网址 |

私钥只保存在本机，VPS 的 `authorized_keys` 安装对应公钥。
尚未配置密钥登录时，可先在本机生成专用密钥，再将公钥交给 VPS 管理员安装：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_vps_route
```

不要覆盖已有同名密钥。通过 VPS 控制台或管理员核对主机指纹，再测试连接：

```bash
ssh -i ~/.ssh/id_ed25519_vps_route -p 22 xxx@xxx.xxx.xxx.xxx
```

无需事先填写出口 IP 或国家。配置完成后，`doctor --connect` 会显示实际出口；
若 VPS 使用上游代理，它可能不同于 VPS 的连接 IP。退出 VPS 后继续操作。

普通用户的服务路径通常是：

```text
/home/xxx/.local/state/claude-launcher/connect.sock
```

`root` 用户通常是 `/root/.local/state/claude-launcher/connect.sock`。
若 VPS 用户 HOME 不在默认位置，使用该用户实际 HOME。
该文件由 `setup-vps` 创建，不需要提前手动建立。

## 2. 安装工具

客户端和 VPS 的 `/usr/bin/python3` 都需为 3.10+。目前只在 Ubuntu 22.04 客户端上验证过。
客户端 Ubuntu/Debian 可安装以下依赖：

```bash
sudo apt-get install git gh python3 python3-venv pipx bubblewrap openssh-client \
  netcat-openbsd curl ca-certificates
```

仓库是私有的，需要有访问权限的 GitHub 账号。登录并让 git 使用这个登录，
然后直接从 GitHub 安装：

```bash
gh auth login
gh auth setup-git
pipx install git+https://github.com/sidiangongyuan/vps-route.git
pipx ensurepath
```

安装后重新打开终端。没有 pipx 也可使用虚拟环境：

```bash
python3 -m venv ~/.venvs/vps-route
~/.venvs/vps-route/bin/pip install git+https://github.com/sidiangongyuan/vps-route.git
```

虚拟环境用户将下面的 `vps-route` 替换为 `~/.venvs/vps-route/bin/vps-route`。

## 3. 填写连接信息

执行 `vps-route init`。以下是填写例子，不是可整段执行的命令：

```text
VPS SSH user [vpsuser]: xxx
VPS numeric IP (not a hostname): xxx.xxx.xxx.xxx
VPS SSH port [22]: 22
Existing SSH private key path [~/.ssh/id_ed25519]: ~/.ssh/id_ed25519_vps_route
Verified known_hosts file [~/.ssh/known_hosts]:
Private socket on VPS [/home/xxx/.local/state/claude-launcher/connect.sock]:
Independent profile name [default]:
Local HTTP proxy for SSH; blank means direct SSH:
Process timezone; blank means host default:
```

空白表示回车使用默认值。时区留空即可，不会修改系统时区。
若本机需要代理才能连接 VPS，填写本机 HTTP 代理，例如
`http://127.0.0.1:7897`，不要填住宅代理地址。全局代理变量不会自动作为该配置使用；
TUN VPN 仍可能承载 SSH 连接。

也可复制 [config.example.json](../config.example.json)，替换占位值后导入：

```bash
cp config.example.json my-route.json
chmod 600 my-route.json
# 编辑 my-route.json，填写自己的信息，再执行：
vps-route init --from-file ./my-route.json
```

配置保存在 `~/.config/vps-route/config.json`。不要提交填写后的配置到 GitHub。
重复 `init` 复用已有配置和资料，缺失目录会补建；配置冲突则报错，不覆盖。
它不清空旧账号，不自动安装 Claude、登录或修改 VPS。

## 4. 连接并登录

```bash
vps-route setup-vps --apply
vps-route doctor --connect
vps-route install
vps-route auth login
```

已有兼容服务时可跳过部署。部署只增加本工具的服务，不修改 SSH、VPN 或防火墙。
非 root 用户需要可用的 systemd 用户服务；若部署提示用户服务不可用，
请 VPS 管理员检查该用户的 user manager 和 linger 设置。

确认 `doctor --connect` 显示 `connectivity_ok: true` 且正常退出，再登录。
`observed_exit_ip` 是当前实际出口，仅供查看；查询失败或出口变化不会阻止使用。
`install` 下载官方最新版 Claude；已安装则跳过，更新使用 `vps-route update`。

1. 保持登录终端等待。
2. 在走预期出口的浏览器打开终端显示的完整官方网址，不修改参数。
3. 完成授权，将返回的完整一次性 code 粘贴到同一终端并回车。
4. 执行 `vps-route auth status --text` 确认登录。

本工具不提供浏览器或远程桌面，也不会替浏览器选择出口。
失败后重新发起登录必须使用新网址及对应的新 code，不能混用。

## 5. 开始使用

```bash
cd /你的项目路径
vps-route run
```

以后无需重复初始化或安装。VS Code Remote SSH 的集成终端也是同样的命令。
运行时 `/status` 中代理为 `http://127.0.0.1:17878`，最终出口以连接检查为准。
不要把这个地址填进浏览器：它不是本机全局代理。

| 本机路径 | 内容 |
| --- | --- |
| `~/.config/vps-route/config.json` | VPS 连接配置 |
| `~/.local/share/claude-launcher/profiles/default/.local/bin/claude` | Claude 安装入口 |
| `~/.local/share/claude-launcher/profiles/default/.claude/` | 登录、会话、技能和记忆，勿分享 |
| `~/.local/state/claude-launcher/logs/usage.jsonl` | 简要连接日志，不记录提示词或令牌 |

`~` 是运行工具的 Linux 用户 HOME。更改 profile 名称后，将 `default` 换成实际名称。

## 常见问题

| 情况 | 处理 |
| --- | --- |
| 找不到 `vps-route` | 执行 `pipx ensurepath` 后重新打开终端，或使用虚拟环境中的完整入口路径 |
| `network_isolation_unavailable` | 先确认已安装 bubblewrap。Ubuntu 23.10/24.04 默认用 AppArmor 限制非特权用户命名空间，可能导致这个错误；本工具未在这些版本上验证。可请管理员为 `/usr/bin/bwrap` 添加允许 `userns` 的 AppArmor 配置后重试（这会放宽系统策略）。不要尝试绕过隔离检查 |
| 在 Claude 里 `git push`、SSH 或 `http://` 失败 | 会话里只能访问公网 HTTPS（443）。这类命令请在普通终端执行 |
| `self-update` 失败 | macOS 按 [macOS 更新步骤](MACOS.zh-CN.md#更新本工具) 使用 `macos-support`；Linux 先确认 `gh auth status` 正常、会话都已关闭，仍失败就执行 `pipx install --force git+https://github.com/sidiangongyuan/vps-route.git` 重装 |
| SSH 主机身份校验失败 | 向管理员核对主机指纹，不关闭身份校验 |
| SSH 登录失败或超时 | 检查用户名、密钥、SSH 端口，以及配置的本机代理是否运行 |
| 出口 IP 显示为空，但连通性正常 | IP 查询服务暂不可用，不影响使用；稍后重试 doctor |
| 已安装提示 | 正常，直接登录或运行；升级用 `vps-route update` |
| 配置冲突 | 原配置已保留；核对导入文件，不用 init 强行覆盖 |
| 登录 code 失败 | 使用最新登录终端对应的网址和完整 code，勿重复使用旧 code |
| Claude 提示连接错误、正在重试 | 等待 Claude 自带重试；SSH 在后台自动重连。若重试耗尽，可用 `vps-route doctor --connect` 检查，恢复后手动重试请求 |
| 提示 SSH 验证失败、停止重连 | 核对密钥和 VPS 指纹，不要关闭指纹校验；修复后手动退出并重新启动 |
| 使用中 Claude 退出 | 用 `vps-route logs --tail 50` 区分是手动停止、SSH 无法重连还是 Claude 自身退出 |
| 退出后终端出现 `35;142;34M` 一类数字 | 执行系统命令 `reset`（不是 `vps-route reset`），无效则新开终端标签页 |
| 反馈问题 | 附上 `vps-route doctor --connect --report ~/vps-route-report.json` 生成的文件和 `vps-route logs --tail 50`，分享前去掉出口 IP 等敏感信息 |

其他操作见 [使用说明](USAGE.zh-CN.md)。
