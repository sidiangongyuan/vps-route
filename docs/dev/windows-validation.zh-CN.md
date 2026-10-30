# Windows / WSL2 验证记录

更新日期：2026-10-30。分支：`windows-wsl`。运行时版本仍为 0.4.0，v0.5.0 尚待合并和发布流程统一处理。

## 已有证据

| 范围 | 结果与来源 |
| --- | --- |
| R1–R8 审查修复 | `e1106b0` 修复嵌套 bubblewrap、文件/socket 别名遮罩、doctor 模式选择与 workspace 计划、IPv6/架构降级、cmd AutoRun 和 seccomp 测试失败处理，并更新 WSLg 文档 |
| 首次安装目录权限 | `acdae42` 显式创建私有 profile，避免递归创建子目录时产生过宽的父目录权限 |
| WSL2 Ubuntu 22.04 x86_64 | 242 项测试，239 通过、3 跳过、无失败；跳过项为未安装 zsh、macOS 专用后端、未配置真实 TeX 编译环境 |
| 原生 Ubuntu 22.04 CI | 242 项测试，236 通过、6 跳过、无失败；额外跳过三项仅适用于真实 WSL/Windows fixture 的检查 |
| 嵌套 bubblewrap | 生产 builder 内运行嵌套 bwrap 成功；WSL 及原生 Linux CI 均通过 |
| doctor | 默认、research_filesystem、windows_interop 三种配置的连接和隔离检查通过；Windows 项目的 workspace 计划通过 |
| Windows 原生程序 | 专属测试项目内用 csc.exe 编译；Windows 模式运行成功，默认模式拦截同一个 exe。此证据来自生产 builder 的无账号 fixture |
| Claude 账号请求 | 用户自行导入 setup token 后，2026-10-30 的最小非交互请求返回指定回复，退出码 0；未显示或复制 token |
| 用户使用反馈 | 2026-10-30，在项目及 Orca WSL 接入说明之后，用户报告测试正常。反馈未逐项列出测试命令，不扩大为完整功能验收 |

原生 Linux 运行记录：[242 项回归测试](https://github.com/sidiangongyuan/vps-route/actions/runs/40899806728)。

## 仍需验收

- 通过 Claude 非交互任务生成 PPT、运行 Python、编译和执行 Windows 程序、验证默认模式拦截，以及启用 Claude 自带 Bash 沙箱。基础 fixture 通过和最小模型回复未替代这些任务。
- Orca `--orca` 控制接口的真实运行验证；WSL 主机和终端可用不代表控制接口兼容性已经验证。
- ARM64、NAT、其他 Windows 用户目录迁移布局，以及 WSLg 应用、音频和剪贴板的专项验证。

## 官方安装器结果

在新的临时 HOME/profile 中执行正常的 `vps-route install`：第一阶段下载完成，第二阶段返回 `Download timed out: exceeded the total deadline`，退出码 1。因此，全新官方安装验收尚未通过。

本次没有用手工分段下载替代成功结果。临时 profile 和复制的连接配置已清理；已有主 profile 保留，Claude 2.1.312 的版本检查通过。后续需在下载路径改善后重新执行全新安装验证。

本记录只包含公开版本、合成测试结果及必要的验收状态。连接参数、主机指纹、出口地址、账号凭据和个人项目路径均不纳入仓库。
