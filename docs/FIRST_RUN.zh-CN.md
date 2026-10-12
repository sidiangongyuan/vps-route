# 完整例子：用自己的 VPS 首次使用

本文面向第一次拿到工具的用户。所有个人地址、用户名、公钥内容、指纹和授权码
都用 xxx 表示；IP 用 xxx.xxx.xxx.xxx 保留 IPv4 的写法。
这些不是可用地址或凭据，不能原样运行。仓库地址、协议名、常用文件名、默认端口和
工具目录结构保留真实固定格式。

**先分清两台机器：**

- 本机：安装工具、运行 Claude 的 Linux，可能是你的电脑，也可能是另一台无头服务器。
- VPS：负责转发 HTTPS 的远程 Linux。不要把本机用户名或路径误填成 VPS 的。

本文例子使用普通 VPS 用户 xxx、默认 SSH 端口 22、预期出口国家 US。
端口不是 22、国家不是 US 时，必须换成实际值；root 的远端路径另见下文。
本机和 VPS 的用户名可以不同，即使文中都写作 xxx。

## 1. 开始前需要哪些信息

| 要准备的东西 | 脱敏示例 / 格式 | 从哪里得到 |
| --- | --- | --- |
| VPS 连接 IP | xxx.xxx.xxx.xxx | VPS 控制台或管理员；当前填写数字 IP，不填写域名 |
| SSH 端口 | 22 | VPS 控制台或管理员；自定义端口填实际数字 |
| VPS SSH 用户名 | xxx；管理员也可能提供 root | VPS 管理员；不是 Claude 邮箱，也不一定是本机用户名 |
| SSH 私钥文件 | ~/.ssh/id_ed25519_vps_route | 本机已有密钥，或按第 3 节生成；填文件路径，不填文件内容 |
| VPS 主机指纹 | SHA256:xxx | 通过 VPS 控制台或管理员独立核对 |
| known_hosts 文件 | ~/.ssh/known_hosts | 本机 SSH 核对 VPS 身份后保存的主机公钥记录 |
| 预期最终出口 IP | xxx.xxx.xxx.xxx | 在 VPS 上查询出口，或由管理员提供；不一定等于连接 IP |
| 出口国家 | US | 与出口检测返回值一致的两位大写国家代码，如 US、JP、SG |
| VPS 用户的主目录 | 普通用户通常 /home/xxx，root 通常 /root | 登录 VPS 后执行 pwd 或打印 HOME；自定义目录以实际值为准 |
| 本地代理（可选） | http://127.0.0.1:7890 | 本机 VPN/Clash 软件的 HTTP 或 mixed 监听端口 |
| Claude 账号 | xxx | 安装完成后才需要，在官方页面登录；不写进连接配置 |

只有 VPS 密码还不够：当前工具需要可用的 SSH 密钥登录。
密码只在最初手动配置 SSH 时可能用到；工具不保存 VPS 密码，不自动改 SSH 认证设置。
必须能通过 SSH 执行命令，并允许转发 Unix socket。

VPS 需要 Linux、systemd、系统 /usr/bin/python3 3.10+ 和正常的对外 HTTPS/DNS。
本机需要 Linux、Python 3.10+、系统 /usr/bin/python3 3.10+、bubblewrap、OpenSSH、
curl、Bash 和 CA 证书。需要本地 HTTP 代理连接 SSH 时，再准备 OpenBSD netcat。
Linux 禁用了非特权网络命名空间时，工具会拒绝启动，不能关闭隔离后继续使用。

Ubuntu/Debian 本机可安装这些系统依赖；其他发行版使用对应包管理器：

~~~bash
sudo apt-get update
sudo apt-get install python3 python3-venv pipx bubblewrap openssh-client \
  curl bash ca-certificates netcat-openbsd git gh
/usr/bin/python3 --version
~~~

确认版本至少为 3.10。上述命令只用于本机，不是让 VPS 安装 bubblewrap 或 Claude。

## 2. 安装工具：在本机执行

仓库目前是私有的。先由维护者授予读取权限，再使用已登录的 GitHub 账号下载：

~~~bash
gh repo clone sidiangongyuan/vps-route
cd vps-route
pipx install .
vps-route --help
~~~

pipx 必须已安装且其命令目录在 PATH 中。Ubuntu/Debian 可通过系统包安装 pipx，
再执行 pipx ensurepath，并重新打开终端。

若不使用 pipx，可以在仓库目录使用独立 Python 虚拟环境：

~~~bash
python3 -m venv .venv
.venv/bin/python -m pip install .
source .venv/bin/activate
vps-route --help
~~~

安装的是路由工具，尚未安装 Claude，也没有登录账号。
工具依赖的初次下载使用本机网络；后面的 vps-route install 才通过配置好的 VPS 下载 Claude。
未发布 PyPI，所以不要直接执行 pip install vps-route。

## 3. 准备 SSH 密钥与主机记录

已有可用私钥和已核对的 known_hosts 时，直接进入第 4 节。

### 3.1 没有密钥：在本机生成

~~~bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_vps_route -C vps-route
~~~

若这个文件已存在，不要同意覆盖，使用原密钥或另取新文件名。
可以设置私钥口令；它不是 VPS 登录密码。
生成后有两个文件：

| 文件 | 用途 | 保存在哪里 |
| --- | --- | --- |
| id_ed25519_vps_route | 私钥；工具 identity_file 填这个文件的路径 | 留在本机，不发到 VPS，不粘贴到配置或聊天 |
| id_ed25519_vps_route.pub | 公钥；通常是一行 ssh-ed25519 xxx vps-route | 交给 VPS 管理员安装到目标 SSH 用户的 authorized_keys |

VPS 上公钥记录的常用位置是该用户的 ~/.ssh/authorized_keys。
这里的 ~ 指 VPS 用户的 HOME，不是本机的 HOME。
管理员需要确认公钥认证已开启，且没有禁止本工具所需的命令执行和 Unix-socket 转发。

若最初可以通过密码登录，也可以在本机使用以下命令安装公钥。
首次出现主机指纹提示时，必须按下一小节核对后再确认：

~~~bash
ssh-copy-id -i ~/.ssh/id_ed25519_vps_route.pub -p 22 xxx@xxx.xxx.xxx.xxx
~~~

输入密码时在终端交互输入，不把密码拼在命令里。
SSH 必须能连通；如果需要代理，参考第 3.3 节，不能假定系统 HTTP 代理会自动作用于 SSH。

### 3.2 主机指纹和 known_hosts 不是私钥

VPS 管理员可在 VPS 控制台执行：

~~~bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
~~~

通过可信渠道核对返回的 ED25519 指纹，格式为 SHA256:xxx。
你首次 SSH 连接看到的对应指纹必须与之匹配，才能输入 yes。
不匹配就停止，联系管理员；不要关闭主机验证来绕过。

本机确认身份后，SSH 通常把 VPS 的主机公钥写入：

~~~text
~/.ssh/known_hosts
~~~

工具需要的是这个文件的路径，不是 SHA256:xxx 字符串。
不要手动把指纹字符串当成文件内容，也不要用自己的 .pub 文件替代它。

### 3.3 验证公钥登录

在本机执行，替换用户名、地址、端口和密钥文件：

~~~bash
ssh -i ~/.ssh/id_ed25519_vps_route -p 22 xxx@xxx.xxx.xxx.xxx
~~~

成功进入的应当是 VPS。它可能要求私钥口令，但配置正确后不应再要求 VPS 账号密码。
如果私钥有口令，需要让本机 ssh-agent 先解锁它，本工具的连接不会交互询问口令：

~~~bash
ssh-add ~/.ssh/id_ed25519_vps_route
~~~

若提示没有运行 agent，需要先启动或接入本机的 ssh-agent，再执行 ssh-add；
例如当前终端可以使用 eval "$(ssh-agent -s)"。不要把 agent 转发到 VPS。

如果只有开 VPN 才能访问 VPS：

- TUN/虚拟网卡模式可能直接承载 SSH，可先尝试上面的命令。
- 只有本地 HTTP 代理时，SSH 不会自动读取 HTTP_PROXY，可以明确指定连接方式：

~~~bash
ssh -i ~/.ssh/id_ed25519_vps_route -p 22 \
  -o 'ProxyCommand=nc -n -X connect -x 127.0.0.1:7890 %h %p' \
  xxx@xxx.xxx.xxx.xxx
~~~

7890 只是常见监听端口，必须换成本机软件的实际 HTTP/mixed 端口。
这里的 %h 和 %p 是 OpenSSH 固定占位符，不用手动替换。
要让 ssh-copy-id 经过同一代理，可为它添加相同的 -o ProxyCommand 选项。

## 4. 在 VPS 确认 HOME 和最终出口

进入 VPS 后执行：

~~~bash
printf '%s\n' "$HOME"
/usr/bin/python3 --version
curl --noproxy '*' -4 -fsS https://ipinfo.io/json
~~~

最后一个命令适用于 VPS 已直接或透明地配置最终出口的情况：
--noproxy 不使用 shell 的 HTTP 代理变量，但不能绕过透明路由。
如果管理员通过其他服务实现出口，请让管理员确认本工具 CONNECT 服务实际使用的出口，
而不是只看某个交互终端单独设置的代理。

查询结果格式示例，其他字段省略：

~~~json
{
  "ip": "xxx.xxx.xxx.xxx",
  "country": "US"
}
~~~

将 ip 填为 expected_ip，country 填为 expected_country。
普通 VPS 直接出网时，出口 IP 常常与连接 IP 相同；VPS 再转住宅代理等上游时，两者可能不同。
不要填本机 IP、VPN 前置节点 IP，或仅根据购买地区猜测国家。
退出 VPS，后面的工具命令在本机执行。

## 5. 完整初始化填写示例

在本机执行：

~~~bash
vps-route init --dry-run
vps-route init
~~~

init 默认会封存旧的用户级 Claude 资料，不能在旧会话仍运行时执行。
--dry-run 会询问连接信息并展示路径，但不写配置、不移动文件；正式 init 需重新填写。
有旧资料时会显示清单并要求确认；不希望移动旧资料时用 init --keep-existing。
init -y 只跳过封存确认，连接信息仍需填写；全自动可结合 --from-file 使用。
不自动登录、下载 Claude、部署 VPS 或接管普通 claude 命令。

默认移走 ~/.claude、~/.claude.json、~/.cache/claude-cli-nodejs、~/.cache/claude、
~/.local/share/applications/claude-code-url-handler.desktop 中存在的项目，
并封存所选 profile 中已有的独立安装和账号资料。目录内符号链接随目录移动，不跟随。
项目源码/项目配置、.agents/skills、SSH 密钥和 HICC 不移动；自定义旧资料位置不自动扫描。

下面是终端交互的填写示例，不能当成 shell 命令整段执行。
方括号是工具默认值，冒号后是你输入的内容；空白表示直接回车：

~~~text
VPS SSH user [vpsuser]: xxx
VPS numeric IP (not a hostname): xxx.xxx.xxx.xxx
VPS SSH port [22]: 22
Existing SSH private key path [~/.ssh/id_ed25519]: ~/.ssh/id_ed25519_vps_route
Verified known_hosts file [~/.ssh/known_hosts]: ~/.ssh/known_hosts
Private socket on VPS [/home/xxx/.local/state/claude-launcher/connect.sock]:
Expected final public exit IP: xxx.xxx.xxx.xxx
Expected two-letter country [US]: US
Independent profile name [default]:
Local HTTP proxy for SSH; blank means direct SSH: http://127.0.0.1:7890
Process timezone; blank means host default:
~~~

这份例子选择通过本机 HTTP 代理连接 VPS；能直接 SSH 或由 TUN 承载时，代理项留空。
时区留空就使用本机系统默认时区，不修改本机或 VPS 系统时区。

remote_socket 是工具将在 VPS 上创建的私有通信文件，不需要你事先创建，也不是网址，
更不是 17878 端口。普通用户常用：

~~~text
/home/xxx/.local/state/claude-launcher/connect.sock
~~~

root 用户常用：

~~~text
/root/.local/state/claude-launcher/connect.sock
~~~

HOME 不是常用位置时，用第 4 节确认的真实绝对目录。
remote_socket 必须是 VPS 路径，不能写 ~；本机私钥和 known_hosts 路径可以使用 ~。
已有兼容 CONNECT 服务时，填写它实际使用的 socket，后面跳过服务部署。

## 6. 或者通过配置文件提交信息

若更喜欢一次性填写文件，复制仓库的 [config.example.json](../config.example.json)，
把 xxx 和端口、国家等示例值换成真实值，再导入：

~~~bash
cp config.example.json my-route.json
chmod 600 my-route.json
# 在编辑器打开 my-route.json，填写真实值并保存后，再执行下一行。
vps-route init --from-file ./my-route.json
~~~

不要把填写后的文件提交到 GitHub。确认初始化成功后，可删除这份重复的临时输入文件；
正式配置已经保存到 ~/.config/vps-route/config.json。
保留 xxx 的模板只是语法示例，不通过真实地址验证，不能直接导入成功。

| 配置字段 | 应填写什么 | 路径或格式说明 |
| --- | --- | --- |
| ssh_host | VPS 连接地址 | 数字 IPv4/IPv6；示例 xxx.xxx.xxx.xxx 需要替换 |
| ssh_user | VPS 登录用户名 xxx | 可改成 root；不是本机用户或 Claude 邮箱 |
| ssh_port | 实际 SSH 端口 | JSON 数字；22 是默认示例，不是必须固定 |
| identity_file | 本机私钥文件 | 如 ~/.ssh/id_ed25519_vps_route；不能填 .pub 或私钥文本 |
| known_hosts | 本机主机公钥记录文件 | 通常 ~/.ssh/known_hosts；不是 VPS 上的 authorized_keys |
| remote_socket | VPS 上服务的私有 socket 绝对路径 | 普通用户通常位于其 HOME/.local/state/claude-launcher/ 下 |
| expected_ip | 最终出口公网 IP | 可以与 ssh_host 不同，必须是真实公网地址 |
| expected_country | 与出口检测一致的国家代码 | 两位大写字母，例如 US |
| profile | 本机独立资料名称 | default 即可；不是账号用户名，只允许字母、数字、下划线和连字符 |
| timezone | 工具进程时区 | null 使用系统默认；指定时填有效 IANA 名称，如 Asia/Shanghai |
| ssh_http_proxy | 本机供 SSH 使用的代理 | null 或 http://127.0.0.1:实际端口；不支持在此填写远端代理或密码 |
| texlive_root | 本机 TeX Live 安装根目录 | 不使用时 null；填写包含 bin 和 texmf-dist 的根目录，不是 bin 子目录 |
| research_filesystem | 是否扩大可访问的本机文件范围 | 默认 false；仅可信项目考虑 true，见安全说明 |
| probe_url | 出口检测接口 | 默认 https://ipinfo.io/json；自定义接口需返回 ip/country，使用 HTTPS 443 |
| interval_seconds | 可选诊断探测间隔参数 | 默认 5；日常启动不持续查询出口 IP |
| timeout_seconds | 连接和出口检测等待时间 | 默认 8 秒 |

## 7. 部署 VPS 端服务并检查

在本机执行：

~~~bash
vps-route doctor
vps-route setup-vps
vps-route setup-vps --apply
vps-route doctor --connect
vps-route network-test
~~~

setup-vps 不带 --apply 只预览；带 --apply 才通过已验证的 SSH 上传少量服务代码。
它增加自己的 vps-route-connect.service、服务目录和私有 socket，
不改 SSH 认证、朋友的 SS/Hy2、VPN、防火墙或其他服务。
若已有兼容 CONNECT 服务，跳过两条 setup-vps 命令，直接检查链路。

普通 VPS 用户需要可用的 systemd 用户服务；要在退出登录、重启后持续运行，
管理员可能需要执行 loginctl enable-linger xxx，其中 xxx 是 VPS 用户名。
root 使用系统服务。工具不会自动创建用户或修改这些管理员设置。

doctor --connect 应返回成功，并显示 configured_egress_matches 为 true、
route_result 为 configured_ip_and_country_match。
同时确认 private_network_namespace 等本机依赖检查正常，不仅仅看一个 IP 字段。
失败就停在这里，先修复；不要在错误出口下继续登录。
network-test 不登录账号、不发送模型请求，它会访问出口检测和无凭据 HTTPS 测试接口。

## 8. 下载最新 Claude 并完成官方登录

仍在本机执行：

~~~bash
vps-route install
vps-route native-version
vps-route auth login
~~~

工具不预装 Claude。install 通过 VPS 路由下载官方 latest 版本，
保存在本机独立 profile 中，不覆盖你原来的 Claude。
登录时，终端会显示官方的手动授权网址和输入提示，网址里的临时参数是 xxx，
必须使用本次终端显示的完整网址，不能使用本文占位内容。

1. 保持发起 auth login 的终端等待，不退出也不重新启动另一次登录。
2. 在走预期出口的浏览器打开这次官方网址，登录自己的 Claude 账号并授权。
3. 复制官方返回的一次性授权码 xxx，粘贴到同一个终端提示处，回车。
4. 执行以下命令确认登录状态：

~~~bash
vps-route auth status --text
~~~

浏览器的代理由你自行配置，工具不会替你改变普通浏览器、VS Code 扩展或其他软件的流量。
不修改授权网址参数，不把过期的 code 或另一轮登录的 code 混用。
登录操作不需要把 Claude 密码、邮箱验证链接或授权码写进配置文件，也不需要发给维护者。

## 9. 日常启动、更新和资料位置

在本机进入项目目录：

~~~bash
cd ~/projects/xxx
vps-route run
~~~

~/projects/xxx 是你的项目目录示例，可换成任意实际项目路径。
在 VS Code Remote SSH 中，就在远端 Linux 的集成终端执行相同命令。
新安装不会默认替换普通 claude，也不接管 VS Code 扩展。

~~~bash
vps-route run --safe-mode
vps-route update
vps-route native-version
vps-route logs --tail 20
~~~

默认位置如下，profile 改名后把 default 换成你填写的名称：

| 路径 | 在哪台机器 | 放什么 |
| --- | --- | --- |
| ~/.config/vps-route/config.json | 本机 | 私有连接配置；不会保存 VPS 密码或私钥正文 |
| ~/.local/share/claude-launcher/profiles/default/.local/bin/claude | 本机 | 官方 Claude 独立安装入口 |
| ~/.local/share/claude-launcher/profiles/default/.claude/ | 本机 | 登录凭据、会话、技能、设置、记忆；不要分享 |
| ~/.local/state/claude-launcher/logs/usage.jsonl | 本机 | 精简启动/退出日志，不记录提示词、code 或终端正文 |
| ~/.local/share/claude-launcher/legacy-archives/cutover-xxx/ | 本机 | init 封存的旧用户级资料，权限 0700；含旧凭据，不分享 |
| ~/.local/share/claude-launcher/profiles/.archives/default-xxx/ | 本机 | 所选独立 profile 的封存；含旧安装与账号资料 |
| ~/.local/share/claude-launcher/server/ | VPS | 工具部署的 CONNECT 服务代码，不是 Claude 会话 |
| ~/.local/state/claude-launcher/connect.sock | VPS | 配置默认路径时的私有通信 socket |
| ~/.ssh/authorized_keys | VPS | 接受哪些本机公钥登录 |

本机 ~ 总是执行命令的 Linux 用户 HOME；VS Code 远程终端中的 ~ 是远端 Linux 用户的，
不是 Windows 电脑的目录。VPS 一列的 ~ 则是你填写的 SSH 用户 HOME。
查看当前机器自己的主目录可执行 printf '%s\n' "$HOME"。

Claude 看到的本地代理为 http://127.0.0.1:17878，只存在于该次独立网络环境。
日常启动前检查出口，运行中检测 SSH 进程；并不持续查询公网 IP 是否变化。
VPS/代理故障时没有直连备用路径；上游无响应但 SSH 仍存活时，可能只表现为请求失败。

## 10. 常见问题

| 现象 | 先检查什么 |
| --- | --- |
| config_already_exists | 已经初始化，不需要重复执行；更换配置用 --config 指定另一个私有文件 |
| invalid_or_unreadable_config | 是否还留有 xxx、端口是否是数字、国家是否两位大写、远端路径是否绝对 |
| ssh_key_or_verified_known_hosts_missing | 两个本机文件是否存在，私钥是不是误填了 VPS 路径 |
| ssh_host_key_verification_failed | IP/端口对应的主机记录和管理员确认的身份；不要关闭验证 |
| ssh_authentication_failed | 目标用户名、公钥安装、私钥是否已解锁、SSH 密钥认证是否允许 |
| ssh_tunnel_failed | 本机代理端口、VPN 到 VPS 的连通性、SSH 端口、socket 转发权限 |
| server_setup_failed | VPS 系统 Python 版本、systemd 用户服务/权限、目录是否被其他服务占用 |
| configured_egress_matches 为 false | 预期出口是不是误写成连接 IP，以及检测国家是否一致 |
| network_isolation_unavailable | 本机 bubblewrap 与非特权用户/网络命名空间支持；不要关闭隔离绕过 |
| install_official_claude_in_separate_profile_first | 执行 install 后再登录或启动 |

## 11. 卸载与选择性保留

~~~bash
vps-route uninstall --dry-run
vps-route uninstall
~~~

默认卸载永久删除本工具的本地账号资料、独立 Claude、全部封存备份、日志、配置，
并通过包管理器移除 vps-route。默认包含 init 移走的旧资料，先确认没有重要内容只剩这份备份。
不连接或清理 VPS，不删除 SSH 密钥、项目源码、共享技能、其他 Claude 安装或仓库源码。
仅删除工具生成的普通 claude 包装器，不恢复可能直连的旧入口。

需要无人值守时使用 uninstall -y；需要保留某类资料时，可组合
--keep-profiles、--keep-backups、--keep-logs、--keep-config、--keep-package。
例如 uninstall --keep-backups --keep-logs 保留备份和日志，其他类别仍会删除。
卸载范围与各种系统安装方式的边界详见 [使用说明](USAGE.zh-CN.md)。
永久删除不是磁盘安全擦除、远端令牌撤销或“全新机器”的保证。

当前是 Linux alpha：新用户仍需检查自己的 SSH/systemd 条件，不承诺所有 VPS 开箱即用。
重置和扩大文件访问范围等操作请另看 [使用说明](USAGE.zh-CN.md)、
[安全边界](SECURITY.md)。首次使用不需要接管旧 claude 命令。

SSH 命令的格式依据：
[OpenSSH ssh-keygen](https://man.openbsd.org/ssh-keygen)、
[OpenSSH ssh](https://man.openbsd.org/ssh)、
[OpenSSH ssh-add](https://man.openbsd.org/ssh-add)。
