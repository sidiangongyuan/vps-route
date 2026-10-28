"""Generate the README diagrams in docs/images (English/Chinese, light/dark).

Run from the repository root:  python3 tools/diagrams.py
Edit the TEXT table below to change wording; never hand-edit the generated SVGs.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent.parent / "docs/images"

THEMES = {
    "light": {
        "ink": "#1f2328", "muted": "#59636e", "line": "#d1d9e0", "panel": "#f6f8fa",
        "card": "#ffffff", "code": "#eff2f5",
        "accent": "#0b7a6a", "accent_soft": "#e2f3ef", "on_accent": "#ffffff",
        "read": "#9a6700", "read_soft": "#fff6dc",
        "block": "#c62a2f", "block_soft": "#fdeceb",
    },
    "dark": {
        "ink": "#e6edf3", "muted": "#9198a1", "line": "#3d444d", "panel": "#151b23",
        "card": "#0d1117", "code": "#1f2630",
        "accent": "#3fc1a5", "accent_soft": "#0f2b27", "on_accent": "#06231e",
        "read": "#d8a53a", "read_soft": "#2b2312",
        "block": "#ff7b72", "block_soft": "#3a1a1a",
    },
}

SANS = ("-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', 'PingFang SC', "
        "'Hiragino Sans GB', 'Microsoft YaHei', 'Noto Sans CJK SC', Helvetica, Arial, sans-serif")
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"

TEXT = {
    "en": {
        "route_title": "VPS Route network path",
        "computer": "YOUR COMPUTER",
        "sandbox": "Isolated sandbox · no network route out",
        "claude": "Claude Code", "claude_sub": "official CLI",
        "proxy": "Local proxy", "proxy_sub": "127.0.0.1:17878",
        "profile_note": "Separate login, history and skills",
        "tunnel": "SSH tunnel", "tunnel_sub": "key + verified host",
        "vps": "Your VPS", "vps_sub": "connection service", "vps_sub2": "DNS and HTTPS exit",
        "internet": "Internet",
        "sites": ["Anthropic API", "GitHub", "npm · PyPI", "other HTTPS sites"],
        "blocked": "Direct connection: blocked. No fallback, even while SSH is down.",
        "flow_title": "First-time setup in six commands",
        "once": "one-time", "daily": "every day",
        "steps": [
            ("Configure", "vps-route init",
             ["Enter your VPS address, SSH user", "and key. Creates a separate profile."]),
            ("Deploy", "vps-route setup-vps --apply",
             ["Installs a small connection", "service on your VPS over SSH."]),
            ("Check", "vps-route doctor --connect",
             ["Tests SSH and HTTPS through", "the VPS and shows the exit IP."]),
            ("Install", "vps-route install",
             ["Downloads official Claude Code", "through the VPS into the profile."]),
            ("Log in", "vps-route auth login",
             ["Open the printed URL in a browser,", "then paste the code back."]),
            ("Run", "vps-route run",
             ["Starts Claude in your project.", "This is the only daily command."]),
        ],
        "iso_title": "What Claude can access",
        "columns": [
            ("Read and write", "accent", [
                ("Your project folder", "the directory you start run in", False),
                ("The separate profile", "login · history · skills · memory", False),
                ("Temporary files", "/tmp, private to each session", True),
            ]),
            ("Read only", "read", [
                ("System programs", "/usr  /bin  /lib", True),
                ("System settings", "/etc", True),
                ("Your skills", "~/.claude/skills  ~/.agents/skills", True),
            ]),
            ("Not visible", "block", [
                ("The rest of your home", "other projects and documents", False),
                ("Keys and accounts", "~/.ssh  ~/.config  ~/.aws  ~/.gnupg", True),
                ("Your existing Claude", "~/.claude  ~/.claude.json", True),
            ]),
        ],
        "network": "Network",
        "network_text": "Only HTTPS (port 443) through your VPS. No direct internet,",
        "network_text2": "no localhost services on your computer, no SSH and no plain http://.",
    },
    "zh": {
        "route_title": "VPS Route 网络路径",
        "computer": "你的电脑",
        "sandbox": "隔离沙箱 · 没有通往外网的路由",
        "claude": "Claude Code", "claude_sub": "官方命令行",
        "proxy": "本机代理", "proxy_sub": "127.0.0.1:17878",
        "profile_note": "独立的登录、历史和技能",
        "tunnel": "SSH 隧道", "tunnel_sub": "密钥 + 已核对指纹",
        "vps": "你的 VPS", "vps_sub": "连接服务", "vps_sub2": "负责 DNS 与 HTTPS 出口",
        "internet": "互联网",
        "sites": ["Anthropic API", "GitHub", "npm · PyPI", "其他 HTTPS 网站"],
        "blocked": "直连：已阻断。SSH 断开时也不会回退直连。",
        "flow_title": "六条命令完成首次配置",
        "once": "一次性", "daily": "日常",
        "steps": [
            ("填写配置", "vps-route init",
             ["填写 VPS 地址、SSH 用户和密钥，", "创建独立的 profile。"]),
            ("部署服务", "vps-route setup-vps --apply",
             ["通过 SSH 在 VPS 上部署", "一个小型连接服务。"]),
            ("检查连接", "vps-route doctor --connect",
             ["检查 SSH 和经 VPS 的 HTTPS，", "并显示出口 IP。"]),
            ("安装 Claude", "vps-route install",
             ["经 VPS 下载官方 Claude Code，", "装进独立 profile。"]),
            ("登录", "vps-route auth login",
             ["在浏览器打开终端显示的网址，", "再把 code 粘贴回终端。"]),
            ("开始使用", "vps-route run",
             ["在项目目录启动 Claude，", "日常只需要这一条命令。"]),
        ],
        "iso_title": "Claude 能访问什么",
        "columns": [
            ("可读写", "accent", [
                ("你的项目目录", "执行 run 时所在的目录", False),
                ("独立 profile", "登录 · 历史 · 技能 · 记忆", False),
                ("临时文件", "/tmp，每次会话单独一份", False),
            ]),
            ("只读", "read", [
                ("系统程序", "/usr  /bin  /lib", True),
                ("系统设置", "/etc", True),
                ("你的技能", "~/.claude/skills  ~/.agents/skills", True),
            ]),
            ("看不到", "block", [
                ("家目录里的其他内容", "其他项目和文档", False),
                ("密钥和账号", "~/.ssh  ~/.config  ~/.aws  ~/.gnupg", True),
                ("你原有的 Claude", "~/.claude  ~/.claude.json", True),
            ]),
        ],
        "network": "网络",
        "network_text": "只能经 VPS 访问 HTTPS（443 端口）。没有直连外网，",
        "network_text2": "连不到本机的 localhost 服务，也不能使用 SSH 和 http://。",
    },
}


def text(x, y, value, *, size=13, fill="ink", weight=400, mono=False, anchor="start",
         spacing=0.0):
    family = "mono" if mono else "sans"
    extra = f' letter-spacing="{spacing}"' if spacing else ""
    return (f'<text x="{x}" y="{y}" font-family="{{{family}}}" fill="var(--{fill})" '
            f'font-size="{size}" font-weight="{weight}" text-anchor="{anchor}"'
            f' xml:space="preserve"{extra}>'
            f'{escape(value)}</text>')


def rect(x, y, w, h, *, fill, stroke=None, radius=10, dash=None, width=1.0):
    stroke_part = f' stroke="var(--{stroke})" stroke-width="{width}"' if stroke else ""
    dash_part = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" '
            f'fill="var(--{fill})"{stroke_part}{dash_part}/>')


def document(width, height, title, theme, body):
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">\n'
        f"<title>{escape(title)}</title>\n"
        f'<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="10" '
        f'markerHeight="10" markerUnits="userSpaceOnUse" orient="auto-start-reverse">'
        f'<path d="M0 1L10 5L0 9z" fill="var(--accent)"/></marker></defs>\n'
        + "\n".join(body) + "\n</svg>\n"
    )
    # Literal colors and fonts render the same in browsers and simple SVG renderers.
    for name, value in THEMES[theme].items():
        svg = svg.replace(f"var(--{name})", value)
    return svg.replace("{sans}", escape(SANS, quote=False)).replace("{mono}", escape(MONO, quote=False))


def lock(x, y, color):
    return (f'<rect x="{x}" y="{y + 6}" width="12" height="9" rx="2" fill="var(--{color})"/>'
            f'<path d="M{x + 3} {y + 6}v-2a3 3 0 0 1 6 0v2" fill="none" '
            f'stroke="var(--{color})" stroke-width="1.8"/>')


def route(language, theme):
    t = TEXT[language]
    body = [
        rect(16, 16, 404, 244, fill="panel", stroke="line", radius=14),
        text(36, 44, t["computer"], size=12, fill="muted", weight=600, spacing=1.2),
        rect(36, 60, 364, 180, fill="accent_soft", stroke="accent", radius=12, dash="6 5",
             width=1.5),
        text(54, 86, t["sandbox"], size=12, fill="accent", weight=600),
        rect(54, 104, 150, 64, fill="card", stroke="line"),
        text(129, 132, t["claude"], size=15, weight=600, anchor="middle"),
        text(129, 152, t["claude_sub"], size=12, fill="muted", anchor="middle"),
        rect(232, 104, 150, 64, fill="card", stroke="line"),
        text(307, 132, t["proxy"], size=15, weight=600, anchor="middle"),
        text(307, 152, t["proxy_sub"], size=11.5, fill="muted", mono=True, anchor="middle"),
        '<path d="M204 136H226" stroke="var(--accent)" stroke-width="2" '
        'marker-end="url(#arrow)"/>',
        text(150, 216, t["profile_note"], size=12, fill="muted"),
        # SSH tunnel from the proxy through the computer boundary to the VPS.
        '<path d="M382 136H548" stroke="var(--accent)" stroke-width="3" '
        'marker-end="url(#arrow)"/>',
        rect(436, 123, 60, 26, fill="accent", radius=13),
        lock(446, 127, "on_accent"),
        text(479, 141, "SSH", size=11, fill="on_accent", weight=700, mono=True,
             anchor="middle"),
        text(466, 113, t["tunnel"], size=12, fill="ink", weight=600, anchor="middle"),
        text(466, 168, t["tunnel_sub"], size=11.5, fill="muted", anchor="middle"),
        rect(552, 78, 180, 116, fill="panel", stroke="line", radius=14),
        text(642, 112, t["vps"], size=16, weight=700, anchor="middle"),
        text(642, 136, t["vps_sub"], size=12.5, fill="muted", anchor="middle"),
        text(642, 156, t["vps_sub2"], size=12, fill="muted", anchor="middle"),
        '<path d="M732 136H778" stroke="var(--accent)" stroke-width="3" '
        'marker-end="url(#arrow)"/>',
        rect(782, 40, 162, 192, fill="panel", stroke="line", radius=14),
        text(863, 72, t["internet"], size=16, weight=700, anchor="middle"),
    ]
    for index, site in enumerate(t["sites"]):
        body.append(text(863, 104 + index * 26, site, size=12.5, fill="muted",
                         anchor="middle"))
    # The direct route: stopped at the sandbox wall.
    body += [
        '<path d="M129 168V240" stroke="var(--block)" stroke-width="2" '
        'stroke-dasharray="5 4"/>',
        '<path d="M129 240V292H863V232" fill="none" stroke="var(--block)" stroke-width="1.5" '
        'stroke-dasharray="5 4" opacity="0.35"/>',
        '<circle cx="129" cy="240" r="11" fill="var(--block)"/>',
        '<path d="M124 235L134 245M134 235L124 245" stroke="var(--block_soft)" '
        'stroke-width="2.2" stroke-linecap="round"/>',
        text(496, 316, t["blocked"], size=13, fill="block", weight=600, anchor="middle"),
    ]
    return document(960, 336, t["route_title"], theme, body)


def flow(language, theme):
    t = TEXT[language]
    width, height = 284, 132
    positions = [(16, 16), (338, 16), (660, 16), (16, 196), (338, 196), (660, 196)]
    body = []
    # Connectors: across each row, then from step 3 down to step 4.
    for (x, y), (nx, _) in zip(positions[:2], positions[1:3]):
        body.append(f'<path d="M{x + width + 6} {y + 66}H{nx - 8}" stroke="var(--accent)" '
                    f'stroke-width="2" marker-end="url(#arrow)"/>')
    for (x, y), (nx, _) in zip(positions[3:5], positions[4:6]):
        body.append(f'<path d="M{x + width + 6} {y + 66}H{nx - 8}" stroke="var(--accent)" '
                    f'stroke-width="2" marker-end="url(#arrow)"/>')
    body.append('<path d="M802 148V168H158V188" fill="none" stroke="var(--accent)" '
                'stroke-width="2" stroke-linejoin="round" marker-end="url(#arrow)"/>')
    for index, ((x, y), (title, command, lines)) in enumerate(zip(positions, t["steps"])):
        daily = index == len(positions) - 1
        body += [
            rect(x, y, width, height, fill="accent_soft" if daily else "panel",
                 stroke="accent" if daily else "line", radius=12, width=1.5 if daily else 1),
            f'<circle cx="{x + 28}" cy="{y + 28}" r="13" fill="var(--accent)"/>',
            text(x + 28, y + 33, str(index + 1), size=13, fill="on_accent", weight=700,
                 anchor="middle"),
            text(x + 50, y + 33, title, size=15, weight=700),
        ]
        chip = t["daily"] if daily else t["once"]
        chip_width = 14 + len(chip) * (12 if language == "zh" else 6.6)
        chip_x = x + width - 16 - chip_width
        body += [
            rect(chip_x, y + 16, chip_width, 22, fill="card" if daily else "code",
                 stroke="accent" if daily else None, radius=11),
            text(chip_x + chip_width / 2, y + 31, chip, size=11.5,
                 fill="accent" if daily else "muted", weight=600, anchor="middle"),
            rect(x + 14, y + 50, width - 28, 28, fill="code" if not daily else "card",
                 radius=6),
            text(x + 26, y + 69, command, size=13, mono=True, weight=500),
        ]
        for line_index, line in enumerate(lines):
            body.append(text(x + 16, y + 100 + line_index * 19, line, size=12.5, fill="muted"))
    return document(960, 344, t["flow_title"], theme, body)


def icon(kind, x, y, color):
    stroke = f'stroke="var(--{color})" stroke-width="2" fill="none" stroke-linecap="round"'
    if kind == "accent":  # pencil: read and write
        return (f'<path d="M{x + 3} {y + 17}l2-6 9-9 4 4-9 9z" {stroke} '
                f'stroke-linejoin="round"/><path d="M{x + 12} {y + 4}l4 4" {stroke}/>')
    if kind == "read":  # eye
        return (f'<path d="M{x} {y + 10}q10-10 20 0q-10 10-20 0z" {stroke}/>'
                f'<circle cx="{x + 10}" cy="{y + 10}" r="3" fill="var(--{color})"/>')
    if kind == "block":  # crossed-out eye
        return (f'<path d="M{x} {y + 10}q10-10 20 0q-10 10-20 0z" {stroke}/>'
                f'<path d="M{x + 2} {y + 18}L{x + 18} {y + 2}" {stroke}/>')
    return (f'<circle cx="{x + 10}" cy="{y + 10}" r="9" {stroke}/>'
            f'<ellipse cx="{x + 10}" cy="{y + 10}" rx="4" ry="9" {stroke}/>'
            f'<path d="M{x + 1} {y + 10}H{x + 19}" {stroke}/>')


def isolation(language, theme):
    t = TEXT[language]
    width, height, top = 298, 262, 16
    body = []
    for index, (title, color, items) in enumerate(t["columns"]):
        x = 16 + index * 316
        body += [
            rect(x, top, width, height, fill="panel", stroke="line", radius=14),
            f'<path d="M{x} {top + 14}a14 14 0 0 1 14-14h{width - 28}a14 14 0 0 1 14 14'
            f'v40h-{width}z" fill="var(--{color}_soft)"/>',
            f'<path d="M{x} {top + 54}h{width}" stroke="var(--line)"/>',
            icon(color, x + 20, top + 17, color),
            text(x + 52, top + 33, title, size=15, fill=color, weight=700),
        ]
        for row, (label, detail, mono) in enumerate(items):
            y = top + 88 + row * 62
            body += [
                text(x + 20, y, label, size=14, weight=600),
                text(x + 20, y + 22, detail, size=11.5 if mono else 12.5, fill="muted",
                     mono=mono),
            ]
    strip = top + height + 16
    body += [
        rect(16, strip, 928, 62, fill="accent_soft", stroke="accent", radius=12),
        icon("network", 36, strip + 21, "accent"),
        text(68, strip + 36, t["network"], size=15, fill="accent", weight=700),
        text(140 if language == "en" else 120, strip + 27, t["network_text"], size=13),
        text(140 if language == "en" else 120, strip + 47, t["network_text2"], size=13),
    ]
    return document(960, strip + 78, t["iso_title"], theme, body)


def social(language, theme):
    """1280x640 card for the repository's social preview (Settings > Social preview)."""
    del language
    body = [
        rect(0, 0, 1280, 640, fill="card", radius=0),
        text(96, 132, "github.com/sidiangongyuan/vps-route", size=22, fill="muted", mono=True),
        text(92, 236, "VPS Route", size=96, weight=700),
        text(96, 300, "Claude Code that reaches the internet", size=34, fill="muted"),
        text(96, 346, "only through your own VPS.", size=34, fill="muted"),
    ]
    boxes = [(96, "Claude Code", "isolated sandbox"), (475, "Your VPS", "SSH tunnel"),
             (854, "Internet", "HTTPS")]
    for x, title, detail in boxes:
        body += [
            rect(x, 410, 330, 110, fill="panel", stroke="line", radius=18, width=1.5),
            text(x + 165, 458, title, size=30, weight=700, anchor="middle"),
            text(x + 165, 494, detail, size=20, fill="muted", anchor="middle"),
        ]
    for start in (426, 805):
        body.append(f'<path d="M{start + 7} 465H{start + 42}" stroke="var(--accent)" '
                    f'stroke-width="5" marker-end="url(#arrow)"/>')
    body.append(text(96, 580, "No direct connection. No fallback.", size=24, fill="block",
                     weight=600))
    return document(1280, 640, "VPS Route", theme, body).replace(
        'markerWidth="10" markerHeight="10"', 'markerWidth="18" markerHeight="18"')


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, build in (("network-path", route), ("first-run", flow),
                        ("isolation", isolation)):
        for language in TEXT:
            for theme in THEMES:
                path = OUTPUT / f"{name}-{language}-{theme}.svg"
                path.write_text(build(language, theme), encoding="utf-8")
                print(path.relative_to(OUTPUT.parent.parent))
    path = OUTPUT / "social-preview.svg"
    path.write_text(social("en", "dark"), encoding="utf-8")
    print(path.relative_to(OUTPUT.parent.parent), "(export to PNG for GitHub settings)")


if __name__ == "__main__":
    main()
