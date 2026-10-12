# Linux 使用说明

这是 Linux 试用版。Mac 原生支持尚未实现，也没有发布到 PyPI。
安装包只提供工具，不包含 Claude、账号、技能、VPN 或住宅代理。

首次使用请先看 [完整例子：从自己的 VPS 到首次登录](FIRST_RUN.zh-CN.md)。
里面逐项说明需要向 VPS 管理员拿什么、路径在哪里、初始化如何填写。

## 新机器开始使用

需要两台机器：运行 Claude 的 Linux，以及你能通过 SSH 管理的 Linux VPS。
客户端需要 Python 3.10+、系统 Python 3.10+、bubblewrap、OpenSSH、curl 和 Bash。
若 SSH 需要经过本地 HTTP 代理，还需要 OpenBSD netcat。
VPS 需要系统 Python 3.10+、systemd，并能访问外部 HTTPS。

先准备可用的 SSH 私钥和 VPS 已验证的主机指纹。私钥不需要发送到 VPS；
VPS 的 authorized_keys 需要有对应公钥。工具不会修改 SSH 登录配置。

安装：

~~~bash
gh repo clone sidiangongyuan/vps-route
cd vps-route
pipx install .
~~~

也可以用虚拟环境中的 pip install .，详见首页；不建议装进系统 Python。
当前不是直接 pip install vps-route，因为尚未发布到 PyPI。

依次执行：

~~~bash
vps-route init
vps-route doctor
vps-route setup-vps
vps-route setup-vps --apply
vps-route doctor --connect
vps-route network-test
vps-route install
vps-route auth login
vps-route auth status --text
~~~

init 填连接地址、端口、用户名、私钥路径、known_hosts 路径、远端私有 socket、
预期出口 IP/国家。时区可以留空。本机若需要代理才能连 VPS，明确填写本地 HTTP
代理，例如 http://127.0.0.1:7890；不要填住宅代理或把它当最终出口。
配置保存在 ~/.config/vps-route/config.json，不覆盖已有配置。

init 不再只填连接信息：默认先展示并封存旧的用户级 Claude 资料和所选独立 profile，
再建立空 profile。检测到相关 Claude/HICC 进程仍在运行就拒绝，不自动结束会话。
有旧资料时需要确认；-y 只跳过确认。init --dry-run 仅预览，
init --keep-existing 只写连接配置、保留已有状态。重复 init 不会覆盖配置或再次清理；
已经初始化后的重建使用 reset。
旧资料包括 ~/.claude（含技能、凭据、会话、配置）、~/.claude.json、
~/.cache/claude-cli-nodejs、~/.cache/claude 和 Claude URL handler。
不动项目内文件、共享 .agents/skills、SSH 密钥、HICC 或 VPS；自定义旧资料路径不自动扫描。
封存路径见完整例子。封存是移走并保留，不是令牌撤销或伪造新机器。

setup-vps 默认预览，--apply 才部署。它只增加本工具的私有 CONNECT 服务；
不动朋友的 SS/Hy2、VPN、SSH、端口、防火墙或 Claude 账号。
已有兼容 CONNECT 服务时，填原 socket，跳过部署。
普通 VPS 用户需要可用的 systemd 用户服务；开机持续运行可能需要管理员启用 linger。
若失败，只报告失败，不静默切到直连。错误后可能留下本工具的安装文件。

登录时，打开终端显示的官方网址。浏览器需要由你自己保证走预期出口。
授权后把一次性 code 粘贴到发起登录的同一终端，回车即可。
本工具不提供 VPS 浏览器或可视化登录界面。

## 日常使用

~~~bash
cd /你的项目路径
vps-route run
~~~

VS Code Remote SSH 的集成终端也是同样的用法。不要默认直接输入 claude：
新机器安装工具不会接管已有 claude，也不自动接管 VS Code 扩展。

登录、更新、版本诊断都走相同工具链：

~~~bash
vps-route auth status --text
vps-route update
vps-route native-version
vps-route doctor --connect
vps-route logs --tail 20
~~~

代理地址仍为 http://127.0.0.1:17878，但仅存在于每次启动的独立网络环境；
不是整个服务器的公共代理。VPN 是 SSH 的前置连接方式，不是 Claude 的备用出口。
启动前检查预期 IP/国家；运行中检测 SSH 进程存活，不持续查询 IP。
SSH 退出会结束本工具启动的 Claude。上游无响应但 SSH 还活着时可能只表现为请求失败。

## 资料、技能和项目目录

默认账号、会话和独立安装位于：

~~~text
~/.local/share/claude-launcher/profiles/default/
~~~

配置 profile 名称不同，目录最后一级也不同。不在 VPS 上保存 Claude 会话。
技能放到该目录的 .claude/skills/；本机 .agents/skills 和 .claude/skills 中符合要求的
技能可只读加载，同名时独立资料中的版本优先。不会带入旧凭据、会话或设置。

正常加载项目 CLAUDE.md/规则并允许自动记忆；禁用 hooks、MCP、账号 connectors、
remote control 和云端技能同步。排查时使用：

~~~bash
vps-route run --safe-mode
~~~

默认只开放当前项目。要访问跨项目数据，配置 research_filesystem 为 true；
它会扩大可访问范围，不能当成对恶意代码的强文件隔离。
额外 TeX Live 可填 texlive_root；不会自动开放所有主目录开发环境。
在本机 HOME 下启动时，会转到独立 default-workspace，避免自动扫描整个人的主目录。

## 重置

~~~bash
vps-route reset
~~~

先看预览。reset --apply 会封存独立资料和检测到的旧用户级 Claude/HICC 状态，
然后通过 VPS 重新安装最新版，但不登录。不要在其他旧会话运行时执行。
只重置独立资料用 auth reset，随后需要 install。
封存并非删除备份、撤销令牌或伪造全新机器。

自检或代理故障应修复连接，不要关闭网络隔离来绕过故障。

## 本机卸载：默认永久删除备份

~~~bash
vps-route uninstall --dry-run
vps-route uninstall
# 已确认预览内容后，非交互执行可用：
vps-route uninstall -y
~~~

默认先列出清单、要求确认，再永久删除本工具的本地 profile（含独立 Claude 和账号资料）、
全部 profile 封存、旧资料封存、日志、连接配置，以及本工具生成的普通 claude 包装器。
随后用 pipx 或当前虚拟环境的 pip 卸载 vps-route 包。不读取账号正文，不连接 VPS。
有关进程仍在运行、目录属于其他用户、危险链接/挂载或清理范围在确认期间改变时拒绝执行。
-y 不跳过这些检查。不恢复可能直连的旧 claude 包装器。

选择性保留可以组合使用：

~~~bash
vps-route uninstall --keep-backups --keep-logs
vps-route uninstall --keep-profiles --keep-config --keep-package
~~~

可用 --keep-profiles、--keep-backups、--keep-logs、--keep-config、--keep-package。
保留 profile 不代表保留备份，两类分别控制。通过 --config 指定的本机 HOME 内配置也会删除；
HOME 外的配置不自动删除。工具默认目录外的配置必须能验证为连接配置，
损坏或无法识别的自定义文件需手动处理，避免 --config 误删其他文件。
系统 Python 安装不自动卸载包，请用 --keep-package
后按原包管理方式卸载。新用户推荐 pipx 或独立虚拟环境安装。

项目源码、共享技能、SSH 私钥、其他 Claude 安装、仓库源码、Python 环境和 VPS 服务不删除。
已经搬入封存中的旧用户技能也属于备份，会随默认卸载删除。
这是永久文件删除，不是磁盘安全擦除，也不会撤销远端令牌或清理浏览器/编辑器/云备份。
