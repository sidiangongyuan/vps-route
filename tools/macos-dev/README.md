# macOS 沙箱开发工具入口

这些入口位于独立 profile 的 `.local/bin`，利用启动器已有的 PATH 顺序选择
实际工具。它们不加载宿主 shell 配置，也不修改 Seatbelt、环境变量继承白名单、
文件白名单或代理设置。执行的工具及其子进程继续受原沙箱限制。

## 配置

| 入口或配置 | 作用 |
| --- | --- |
| `.local/bin/go` | 使用 profile 内的 Go 1.27.3，启用文件证书校验，关闭自动切换工具链 |
| `.local/bin/python3` | 调用 Homebrew Python 3，不经过系统 Xcode 入口 |
| `.local/bin/git` | 调用 Command Line Tools 的实际 Git |
| `.local/bin/clang`、`clang++` | 调用实际 C/C++ 编译器，并明确指定原本可读的 macOS SDK |
| `.config/pip/pip.conf` | 使用证书文件校验 HTTPS，避免依赖宿主 trustd |
| `.gitconfig` | GitHub HTTPS 凭据助手使用 profile 中已有的 `gh` 登录 |

Go 1.26 在此沙箱内下载模块会报 `x509: OSStatus -26276`。
[Go 1.27](https://go.dev/doc/go1.27) 支持使用 `SSL_CERT_FILE`
取代系统信任服务。入口仅给自身及子进程设置公开的 CA 路径、证书校验开关和
`GOTOOLCHAIN=local`，不导入宿主环境。工具链固定后，要求更新 Go 的项目会明确
失败，需另行验证并升级；不会自动回到宿主 asdf 或旧版 Go。

上述证书设置只随 `go` 命令及当次子进程传递。旧模块（例如 `go.mod` 声明
`go 1.26`）生成的二进制，之后单独执行时仍可能使用 macOS 系统证书服务而失败。
需要联网的这类程序可在沙箱内按命令显式启动：

```bash
env -u SSL_CERT_DIR SSL_CERT_FILE=/etc/ssl/cert.pem \
  GODEBUG=x509sslcertoverrideplatform=1 ./your-program
```

该设置仅适用于包含此 Go 1.27 开关的二进制，不增加网络或系统服务权限。
其他语言或预编译客户端是否支持文件证书，需要分别验证。

pip 使用官方的 [`legacy-certs` 配置](https://pip.pypa.io/en/stable/topics/https-certificates/)
关闭系统信任服务接入，同时保留证书链、主机名和有效期校验。
没有设置 `trusted-host` 或关闭 TLS 验证。该配置也适用于 profile 内创建的虚拟环境。

GitHub 凭据助手只配置在 profile 的 `.gitconfig` 中；宿主配置不变。
远程操作使用 HTTPS。SSH URL、SSH 密钥和 ssh-agent 不因这些入口而获得访问权。

## 安装与维护

先准备官方 `go1.27.3.darwin-arm64.tar.gz`，对照官方发布元数据校验 SHA-256，
将归档中的 `go` 目录放到 profile 的 `.local/libexec/vps-route-go/go1.27.3`。
本机安装记录在该目录旁的 `install.json`，包含来源和归档摘要。
Go 本身不安装到宿主 `/usr/local/go` 或 asdf 目录。

随后在普通终端运行：

```bash
python3 tools/macos-dev/install.py --profile /absolute/path/to/profile
```

安装器拒绝符号链接目录和已有未知入口。只有确认现有文件属于此工具后，才使用
`--replace-existing` 更新。入口权限为 `0700`，pip 配置权限为 `0600`。
写入前会检查全部目标；已知冲突不会造成部分入口更新。磁盘故障等运行时错误
仍可能中断安装，因此安装后需完成下面的验证。
GitHub 凭据助手需单独配置，安装器不会读取、复制或修改 token。

安装器仅安装入口和 pip 配置。它要求机器上已存在对应的 Homebrew Python、
Command Line Tools 编译器、SDK 和 profile 内的 Go。系统工具或 SDK 升级后需复测。

## 验证范围

本机验证使用现有 VPS 隧道和当前配置的 Seatbelt，并在临时项目中执行：

- Go 从空模块缓存下载 `github.com/google/uuid@v1.6.0`，编译并运行引用它的程序。
- Python 创建虚拟环境，从 PyPI 安装 `idna==3.10` wheel 并成功导入。
- Git HTTPS clone、fetch、读取检出的内容，以及使用沙箱已有凭据读取私有仓库。
- CGO、Python 原生扩展和 C++ 的编译运行。
- 关闭本次测试拥有的 SSH 隧道后，三个下载工具均失败。
- 删除代理变量后，对固定公网 IP 的 Go、pip、Git 连接均被内核以 `EPERM` 拒绝。
- 三个工具都拒绝测试服务的自签名证书，测试服务未收到 HTTP 请求。

另行运行 `tests/test_macos*.py` 的原生回归，覆盖 TCP/UDP、宿主 IPC、信号、
文件边界和证书验证。以上不是用户所有项目、全部依赖或所有网络客户端的兼容性保证。
测试结果摘要保存在 profile 的 `.local/share/vps-route-dev-tools/validation.json`。

在仓库根目录使用下面的命令复测当前安装；报告路径必须尚不存在：

```bash
PYTHONPATH=. python3 tools/macos-dev/validate.py --report /tmp/vps-route-dev-check.json

VPS_ROUTE_TEST_DEV_PROFILE="$HOME/.local/share/claude-launcher/profiles/default" \
VPS_ROUTE_TEST_GH_BINARY="$HOME/.local/share/claude-launcher/profiles/default/.local/libexec/vps-route-gh/gh" \
python3 -m unittest discover -s tests -p 'test_macos*.py' -v
```

第一条读取当前 VPS 配置，创建独立 SSH 连接和临时项目，运行编译、下载、Git
及断路测试。它不启动 Claude、不导出 token、不关闭已有会话；结束时清理自己
的隧道和临时目录。可选 `--private-remote https://github.com/OWNER/REPO.git`
用于验证现有沙箱凭据的只读远程访问。报告记录命令、退出状态和断言，不记录
程序输出或凭据。第二条包含 Go、pip、Git 的本地自签名证书拒绝测试。
未提供对应环境变量时，真实已安装工具测试会跳过。

## 回退

移除本工具创建的五个入口和 pip 配置，即恢复原有命令选择方式。
不再需要时可移除独立 Go 目录。只删除 `.gitconfig` 中本工具添加的 GitHub
凭据助手条目，保留之后添加的其他设置；不需要删除或重新导入 `gh` token。
