# macOS 沙箱专用 gh

此工具构建官方 GitHub CLI，不修改 `gh` 源码。专用入口让 Go 使用公开 CA
证书文件校验 TLS，避免访问 Seatbelt 禁止的 macOS trustd 服务。
它不修改 `vps-route` 的网络、Mach 服务、文件或环境变量白名单。

## 构建与验证

准备 Go 1.27.3 和 Python 3.12 或更新版本，然后在仓库根目录执行：

```bash
python3 tools/macos-gh/build.py \
  --go /absolute/path/to/go1.27.3/bin/go \
  --output /absolute/path/to/new-build-directory

VPS_ROUTE_TEST_GH_BINARY=/absolute/path/to/new-build-directory/gh \
  python3 -m unittest discover -s tests -p 'test_macos*.py' -v
```

构建器校验固定版本源码归档的 SHA-256，使用 Go 模块校验和，关闭 CGO，
固定为当前 Mac 的原生架构，清除宿主的 Go 构建配置，验证产物目标并记录
二进制摘要。它需要联网下载公开源码和依赖，但不访问登录凭据。
安装前须在普通终端运行原生测试；外层沙箱可能阻止测试端口或嵌套 Seatbelt。
未设置 `VPS_ROUTE_TEST_GH_BINARY` 时，专用 `gh` 测试会跳过。

测试覆盖可信证书成功，以及不可信、过期、域名不符和 CA 文件缺失时失败；
同时验证代理断开、删除代理变量后无法直连，及原有网络和宿主服务隔离。
测试只使用临时证书和无效测试 token。真实 VPS 验证需另行执行。

## 安装布局

在所选 profile 中放置以下文件；不要替换宿主的 Homebrew `gh`：

| profile 内的路径 | 内容 | 权限 |
| --- | --- | --- |
| `.local/bin/gh` | 本目录的 `gh` 包装脚本 | `0700` |
| `.local/libexec/vps-route-gh/gh` | 通过测试的二进制 | `0700` |
| `.local/libexec/vps-route-gh/build.json` | 构建来源与摘要 | `0600` |

安装前检查目录归属和符号链接，不覆盖已有未知文件。profile 根目录及新建的
凭据目录使用 `0700`。启动器已经把 profile 的 `.local/bin` 放在 PATH 首位，
无需修改启动器或隔离规则。完成后重新运行 `vps-route run`。

包装脚本固定 `SSL_CERT_FILE=/etc/ssl/cert.pem`，清除 `SSL_CERT_DIR`，并设置
`GODEBUG=x509sslcertoverrideplatform=1`。最后一项必须显式设置：上游 `gh`
的 `go.mod` 指定 Go 1.26，新编译器会保留旧版默认行为。
此方式使用 PEM CA 集，不继承宿主钥匙串中的自定义证书或系统信任策略。
不要在此入口中设置跳过证书验证的选项，也不要改成允许访问 trustd。

GitHub token 的导入和验证见 [macOS 使用说明](../../docs/MACOS.zh-CN.md#沙箱内使用-github-cli)。
只有证书及隔离测试、真实 VPS 通道验证都通过后，才导入实际凭据。

## 回退与更新

移除 profile 的 `.local/bin/gh` 和 `.local/libexec/vps-route-gh`，重新启动会话，
即可恢复使用宿主 `gh` 的原有行为，包括可能再次出现的证书错误。凭据文件不会
自动移除；如需撤销沙箱内登录，单独移除 profile 的 `.config/gh`。
移除本地副本不会撤销 GitHub 上的 token，也不会改变宿主登录。

专用二进制不由 Homebrew 或 pipx 更新。后续升级应更新固定源码版本、摘要和
编译器，重新完成证书、隔离及真实 VPS 测试，再替换二进制。
