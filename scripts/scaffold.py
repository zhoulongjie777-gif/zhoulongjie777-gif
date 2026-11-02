#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按主题生成 Profile README + hero SVG 骨架（亮暗双版）。

生成亮暗双版身份图和不含示例占位内容的 README；默认拒绝覆盖已有文件。
完整主页、配置文件和风格预览使用 build_profile.py。

用法:
    python scaffold.py --theme t1 --name "石敬荣" --latin "Felix Shi" \
        --eyebrow "AGENT DEVELOPER" --tagline "构建 Coding Agent，也持续向开源仓库贡献代码。" \
        --sub "2028 届本科在读 · Agent Harness / MCP" --out ./profile

    python scaffold.py --theme t2 --name "张三" --tagline "..." --out ./profile
    python scaffold.py --theme t3 ... 
    python scaffold.py --theme t4 --name "张三" --tagline "..." --out ./profile   # 不出 SVG

主题:
    t1 Editorial（默认） 透明背景 + 发丝线 + mono 眉行
    t2 Terminal          终端窗口 + 提示符 + 光标
    t3 Card              圆角卡片 + 边框
    t4 Minimal           纯 Markdown，不出 SVG

只依赖标准库。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path

SANS = ("-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',"
        "'Hiragino Sans GB','Microsoft YaHei',sans-serif")
MONO = "ui-monospace,'SF Mono',Menlo,Consolas,monospace"

# Primer 色值，见 references/design-system.md
LIGHT = dict(text="#1f2328", muted="#59636e", disabled="#818b98",
             border="#d1d9e0", canvas="#ffffff", inset="#f6f8fa", accent="#8250df")
DARK = dict(text="#f0f6fc", muted="#9198a1", disabled="#656c76",
            border="#3d444d", canvas="#0d1117", inset="#151b23", accent="#ab7df8")

ACCENTS = {  # (light, dark)，四色与 references/design-system.md §6 一致
    "purple": ("#8250df", "#ab7df8"),
    "blue": ("#0969da", "#4493f8"),
    "green": ("#1a7f37", "#3fb950"),
    "orange": ("#9a6700", "#d29922"),
}

THEMES = {"t1": "Editorial", "t2": "Terminal", "t3": "Card", "t4": "Minimal"}
PROFILE_DEFAULTS = {
    "username": "", "name": "", "latin": "", "eyebrow": "PROFILE",
    "tagline": "", "sub": "", "theme": "t1", "accent": "blue",
    "mode": "auto", "language": "zh", "projects": [], "links": [],
}
LABELS = {
    "zh": {"projects": "代表作品", "links": "更多信息"},
    "en": {"projects": "Selected work", "links": "Find me"},
}


def markdown_text(value: str) -> str:
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*{}\[\]()#+.!>|_])", r"\\\1", value)


def public_url(value: str, *, allow_email: bool = False) -> str:
    parsed = urllib.parse.urlsplit(value)
    allowed = {"https", "http", "mailto"} if allow_email else {"https", "http"}
    if parsed.scheme not in allowed or any(character.isspace() for character in value):
        raise ValueError("链接必须是完整的 http(s) 地址；联系链接也可使用 mailto")
    if parsed.scheme in {"http", "https"} and (not parsed.hostname or parsed.username or parsed.password):
        raise ValueError("链接必须有主机名，不能包含账号或密码")
    if parsed.scheme == "mailto" and ("@" not in parsed.path or parsed.query):
        raise ValueError("mailto 链接必须是邮箱地址，不能附带查询参数")
    return urllib.parse.quote(value, safe="/:?=#%&@+;,!$'()*-._~")


def normalize_profile(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("配置文件的顶层必须是 JSON 对象")
    unknown = set(config) - set(PROFILE_DEFAULTS)
    if unknown:
        raise ValueError("不支持的配置字段：" + ", ".join(sorted(unknown)))
    profile = {**PROFILE_DEFAULTS, **config}
    for field in ("username", "name", "latin", "eyebrow", "tagline", "sub", "theme", "accent", "mode", "language"):
        if not isinstance(profile[field], str):
            raise ValueError(f"{field} 必须是字符串")
        profile[field] = " ".join(profile[field].split())
    username = profile["username"]
    if username and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", username):
        raise ValueError("username 必须是 GitHub 用户名，不是网址或邮箱")
    profile["name"] = profile["name"] or username
    if not profile["name"]:
        raise ValueError("请提供 name 或 username")
    for field, choices in (("theme", THEMES), ("accent", ACCENTS), ("mode", ("auto", "projects", "contributions")), ("language", LABELS)):
        if profile[field] not in choices:
            raise ValueError(f"不支持的 {field}：{profile[field]}")
    for field, limit in (("projects", 6), ("links", 4)):
        items = profile[field]
        if not isinstance(items, list) or len(items) > limit:
            raise ValueError(f"{field} 必须是列表，最多 {limit} 项")
        normalized = []
        for item in items:
            allowed = {"name", "url", "description"} if field == "projects" else {"name", "url"}
            if not isinstance(item, dict) or set(item) - allowed:
                raise ValueError(f"{field} 的每一项只接受 {', '.join(sorted(allowed))}")
            if not all(isinstance(item.get(key), str) and item[key].strip() for key in ("name", "url")):
                raise ValueError(f"{field} 的每一项必须提供 name 和 url")
            description = item.get("description", "")
            if not isinstance(description, str):
                raise ValueError("description 必须是字符串")
            cleaned = {"name": " ".join(item["name"].split()), "url": public_url(item["url"], allow_email=field == "links")}
            if field == "projects":
                cleaned["description"] = " ".join(description.split())
            normalized.append(cleaned)
        profile[field] = normalized
    return profile


def read_profile(path: Path) -> dict:
    return normalize_profile(json.loads(path.read_text(encoding="utf-8-sig")))


def ensure_targets(root: Path, relative_paths: list[str], overwrite: bool = False) -> None:
    if root.is_symlink():
        raise ValueError("输出目录不能是符号链接")
    resolved_root = root.resolve()
    conflicts = []
    for relative in relative_paths:
        target = root / relative
        if target.is_symlink() or not target.resolve().is_relative_to(resolved_root):
            raise ValueError(f"输出路径不能越过目录边界或使用符号链接：{relative}")
        if target.exists() and (not overwrite or not target.is_file()):
            conflicts.append(relative)
    if conflicts:
        raise ValueError("拒绝覆盖已有文件：" + ", ".join(conflicts) + "；请换输出目录，或确认后使用 --overwrite")


def harden_stdout() -> None:
    """把 stdout/stderr 的编码错误降级为替换字符。

    Windows 中文环境把输出重定向到管道或文件时，stdout 编码是 cp936，
    `print("✅ ...")` 会抛 UnicodeEncodeError —— 文件其实已经写好了，
    但进程带着 traceback 退出 1，上层流程会判定失败。
    这里丢几个图标可以，丢退出码正确性不行。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or 0xFF00 <= o <= 0xFF60 or 0x3000 <= o <= 0x303F)


# 字符宽度分档（单位 em），2026-11 用 headless chrome 的
# getComputedTextLength() 逐字符实测校准（Segoe UI 回退栈）：
#   i/l/j/./, 0.24 · f/t/r/括号/斜杠 0.34 · 小写+数字 0.53
#   大写 0.59 · M/W/m/w/@/%/& 0.86 · 空格 0.26 · CJK 1.0 · emoji 1.15
# 早期版本拉丁一律 0.55em，实测误差 -38%~+92%，T2 的方块光标会飘出半个字身，
# 分档后同一批样本误差收敛到 ±4%。
_T_SPACE = 0.26
_T_ULTRA = set("ilj.,:;'|·!")            # ≈0.24
_T_NARROW = set("ftr()[]{}\"*-/\\?")     # ≈0.35
_T_WIDE = set("MWmw@%&")                 # ≈0.86


def _is_wide(o: int) -> bool:
    """东亚全角区里 is_cjk() 没覆盖的部分：假名、谚文、CJK 扩展、全角符号。"""
    return (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF
            or 0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF
            or 0xFE30 <= o <= 0xFE6F or 0xFFE0 <= o <= 0xFFE6
            or 0x20000 <= o <= 0x3FFFD)


def _is_emoji(o: int) -> bool:
    return 0x1F300 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF


def char_w(ch: str) -> float:
    if ch == " ":
        return _T_SPACE
    o = ord(ch)
    if is_cjk(ch) or _is_wide(o):     # 汉字 / 假名 / 谚文 / 全角
        return 1.0
    if _is_emoji(o):
        return 1.15
    if ch in _T_ULTRA:
        return 0.24
    if ch in _T_NARROW:
        return 0.35
    if ch in _T_WIDE:
        return 0.86
    if ch.isalpha() and o > 0x036F:   # 西里尔/希腊/阿拉伯/泰文… 比拉丁宽
        return 0.60
    if ch.isupper():
        return 0.59
    return 0.53


def text_w(s: str, size: float, mono: bool = False) -> float:
    """粗估文本宽度：逐字符分档宽度 × 字号。

    SVG 是静态文件，没法 getComputedTextLength()，只能估。
    CJK 按 1em 是精确的（实测误差 0%）；拉丁分五档，2026-11 用浏览器
    逐字符实测校准，中英混排误差 ±3% 以内。

    `mono=True` 用于等宽文本（0.6em/字符）——比例字体的分档表套到
    等宽字上会低估约 30%，眉行、终端标题这类 mono 文本必须传 True。

    调用方注意：**文本带 letter-spacing 时要把字距自己扣掉**，
    这个函数只算字宽。

    另外字体由系统决定（macOS SF Pro / Windows Segoe UI），
    同一串字的实际宽度并不相同，所以光标这类贴边元素算完还要按画布钳一次。
    """
    if mono:
        # 等宽字体的「中文」不在同一套字宽里，会回退到中文字体按 1em 排。
        # 拉丁按 0.56em 取中：Consolas 实测 0.53、Menlo / SF Mono 是 0.6，
        # 字体栈跨平台时不可能全对，取中间值把误差压到 ±6%。
        return sum(1.0 if is_cjk(character) or _is_wide(ord(character)) else
                   1.15 if _is_emoji(ord(character)) else 0.56 for character in s) * size
    return sum(char_w(c) for c in s) * size


def clip(s: str, size: float, max_w: float, word: bool = False, mono: bool = False) -> str:
    """超宽就截断加省略号。副标题不缩字号——缩了会比正文还小。

    `word=True` 时优先退到最近的空格，避免英文描述被切成半个单词。
    """
    if text_w(s, size, mono) <= max_w:
        return s
    while s and text_w(s, size, mono) > max_w - size:   # 预留省略号宽度
        s = s[:-1]
    if word and " " in s.strip():
        cut = s.rfind(" ")
        if cut > len(s) * 0.55:                  # 退让不多才按词切
            s = s[:cut]
    s = s.rstrip().rstrip(",;:、，；：·-—|")      # 别留个逗号再挂钩省略号
    return s + "…"


def fit_headline(name: str, latin: str, size: float, max_w: float,
                 tracking: float = 0.0, floor: float = 26.0,
                 margin: float = 0.04) -> tuple:
    """把「名字 · 拉丁名」压进 max_w，返回 (name, latin, font_size)。

    顺序：先缩字号（下限 floor），缩到下限还超宽就依次截拉丁名、截本名。
    viewBox 外的字在 GitHub 上是**直接消失**，不是出滚动条，
    所以宁可小一号也不能让它溢出。

    `margin` 是安全余量（默认 4%）：字宽只能估算，而 GitHub 上的字体由系统
    决定，非拉丁语种（西里尔 / 阿拉伯 / 泰文）误差更大。留 4% 是为了让
    「估算说刚好放下」的情况在真机上也不会被裁掉。
    """
    budget = max_w * (1 - margin)

    def measure(display_name: str, latin_name: str, font_size: float) -> float:
        line = display_name + (" · " + latin_name if latin_name else "")
        return text_w(line, font_size) + tracking * max(len(line) - 1, 0)

    sz = size
    while sz > floor and measure(name, latin, sz) > budget:
        sz = max(floor, sz - 0.5)
    if measure(name, latin, sz) <= budget:
        return name, latin, sz

    while latin and measure(name, latin, sz) > budget:
        latin = latin[:-1]
    latin = latin.rstrip()
    if measure(name, latin, sz) <= budget:
        return name, latin, sz

    while name and measure(name + "…", latin, sz) > budget:
        name = name[:-1]
    return name.rstrip() + "…", latin, sz


def width_attributes(content: str, size: float, max_width: float, mono: bool = False) -> str:
    """长文本使用 SVG 原生宽度约束，避免系统字体的估算差异导致越界。"""
    estimate = text_w(content, size, mono)
    if estimate < max_width * 0.7:
        return ""
    width = min(estimate, max_width * 0.95)
    return f' textLength="{width:.2f}" lengthAdjust="spacingAndGlyphs"'


def tx(x, y, s, size, fill, weight="normal", anchor="start",
       family=None, ls=None, opacity=None, max_width=None) -> str:
    a = f'<text x="{x}" y="{y}" font-family="{family or SANS}" font-size="{size}" ' \
        f'fill="{fill}" font-weight="{weight}"'
    if anchor != "start":
        a += f' text-anchor="{anchor}"'
    if ls is not None:
        a += f' letter-spacing="{ls}"'
    if opacity is not None:
        a += f' fill-opacity="{opacity}"'
    if max_width is not None:
        a += width_attributes(s, size, max_width, family == MONO)
    return f'{a}>{esc(s)}</text>'


def svg_open(w, h, title) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" role="img" aria-label="{esc(title)}">\n'
            f'<title>{esc(title)}</title>\n')


# ---------------------------------------------------------------- T1 Editorial
def hero_t1(name, latin, eyebrow, tagline, sub, C, accent) -> str:
    W, H = 962, 226
    name, latin, ns = fit_headline(name, latin, 44, W - 38 - 40)
    tagline = clip(tagline, 15.5, W - 40 - 40)
    sub = clip(sub, 13, W - 40 - 40)
    name_bounds = width_attributes(name + (" · " + latin if latin else ""), ns, W - 80)
    p = [svg_open(W, H, f"{name} {latin}".strip())]
    p.append(f'<rect x="40" y="47" width="7" height="7" fill="{accent}"/>')
    p.append(tx(56, 54, clip(eyebrow.upper(), 13, W - 96, mono=True), 13, accent, "600",
                family=MONO, max_width=W - 96))
    name_svg = (f'<text x="38" y="126" font-family="{SANS}" font-size="{ns:g}" '
                f'font-weight="700" fill="{C["text"]}"{name_bounds}>'
                f'{esc(name)}'
                + (f'<tspan fill="{C["muted"]}" font-weight="400"> · {esc(latin)}</tspan>'
                   if latin else "") + "</text>")
    p.append(name_svg)
    p.append(tx(40, 168, tagline, 15.5, C["muted"], max_width=W - 80))
    p.append(tx(40, 198, sub, 13, C["muted"], max_width=W - 80))
    p.append(f'<line x1="40" y1="{H-9}" x2="{W-40}" y2="{H-9}" '
             f'stroke="{C["border"]}" stroke-width="1"/>')
    p.append("</svg>")
    return "\n".join(p)


# ---------------------------------------------------------------- T2 Terminal
def hero_t2(name, latin, eyebrow, tagline, sub, C, accent) -> str:
    W, H = 962, 246
    name, latin, ns = fit_headline(name, latin, 42, W - 40 - 40)
    tagline = clip(tagline, 15.5, W - 40 - 40)
    sub = clip(sub, 13, W - 40 - 40)
    name_bounds = width_attributes(name + (" · " + latin if latin else ""), ns, W - 80)
    p = [svg_open(W, H, f"{name} terminal")]
    p.append(f'<rect x="0" y="0" width="{W}" height="{H}" rx="8" '
             f'fill="{C["canvas"]}" stroke="{C["border"]}" stroke-width="1"/>')
    # 标题栏
    p.append(f'<path d="M0 8a8 8 0 0 1 8-8h946a8 8 0 0 1 8 8v26H0z" '
             f'fill="{C["inset"]}"/>')
    for i, col in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        p.append(f'<circle cx="{26+i*20}" cy="17" r="5.5" fill="{col}"/>')
    p.append(tx(W // 2, 22, clip(f"{name} / profile", 13, W - 240, mono=True), 13, C["muted"],
                family=MONO, anchor="middle", max_width=W - 240))
    p.append(f'<line x1="0" y1="34" x2="{W}" y2="34" stroke="{C["border"]}"/>')
    # 内容（等宽只用于英数符号，中文走无衬线）
    y = 84
    p.append(tx(40, y, "$", 17, accent, "700", family=MONO))
    p.append(tx(62, y, clip(eyebrow, 17, W - 102, mono=True), 17, C["muted"], family=MONO, max_width=W - 102))
    y += 50
    name_svg = (f'<text x="40" y="{y}" font-family="{SANS}" font-size="{ns:g}" '
                f'font-weight="700" fill="{C["text"]}"{name_bounds}>'
                f'{esc(name)}'
                + (f'<tspan fill="{C["muted"]}" font-weight="400"> · {esc(latin)}</tspan>'
                   if latin else "") + "</text>")
    p.append(name_svg)
    # 光标位置只能靠估宽（静态 SVG 拿不到排版结果）。实测（Chrome +
    # Segoe UI）间距落在 -3~+15px 区间，读起来是「紧跟正文的终端光标」；
    # 早期按 0.55em 估算时偏差可达 46px，看起来像飘在空中的独立方块。
    # 间距随字号缩放，再按画布右边界钳一次。
    line = name + (" · " + latin if latin else "")
    tracked = text_w(line, ns)
    cx = min(40 + tracked + ns * 0.2, W - 40 - 9)
    p.append(f'<rect x="{cx:.1f}" y="{y - ns * 0.74:.1f}" width="9" '
             f'height="{ns * 0.76:.1f}" '
             f'rx="2" fill="{accent}"/>')
    y += 40
    p.append(tx(40, y, tagline, 15.5, C["muted"], max_width=W - 80))
    y += 30
    p.append(tx(40, y, sub, 13, C["muted"], max_width=W - 80))
    p.append("</svg>")
    return "\n".join(p)


# ---------------------------------------------------------------- T3 Card
def hero_t3(name, latin, eyebrow, tagline, sub, C, accent) -> str:
    W, H = 962, 218
    # 卡片四周留白 16/10：贴边（x=1）在 962→806 缩放后边框只有 0.84px，
    # 圆角读不出来，看上去只是一圈框而不是一张卡。
    CX, CY, CW, CH = 16, 10, W - 32, 198
    PAD = 28
    IX = CX + PAD                       # 内容左边界 44
    IW = CW - PAD * 2                   # 内容可用宽 874
    name, latin, ns = fit_headline(name, latin, 40, IW)
    tagline = clip(tagline, 15.5, IW)
    sub = clip(sub, 13, IW)
    name_bounds = width_attributes(name + (" · " + latin if latin else ""), ns, IW)
    p = [svg_open(W, H, f"{name} card")]
    p.append(f'<rect x="{CX}" y="{CY}" width="{CW}" height="{CH}" rx="8" '
             f'fill="{C["canvas"]}" stroke="{C["border"]}" stroke-width="1"/>')
    p.append(f'<rect x="{IX}" y="36" width="3" height="15" rx="1.5" fill="{accent}"/>')
    p.append(tx(IX + 12, 48, clip(eyebrow.upper(), 13, IW - 12, mono=True), 13, accent, "600",
                family=MONO, max_width=IW - 12))
    p.append(f'<text x="{IX}" y="112" font-family="{SANS}" font-size="{ns:g}" '
             f'font-weight="700" fill="{C["text"]}"{name_bounds}>'
             f'{esc(name)}'
             + (f'<tspan fill="{C["muted"]}" font-weight="400"> · {esc(latin)}</tspan>'
                if latin else "") + "</text>")
    p.append(tx(IX, 152, tagline, 15.5, C["muted"], max_width=IW))
    p.append(tx(IX, 180, sub, 13, C["muted"], max_width=IW))
    p.append("</svg>")
    return "\n".join(p)


BUILDERS = {"t1": hero_t1, "t2": hero_t2, "t3": hero_t3}


# ---------------------------------------------------------------- README
def readme_for(theme: str, name: str, tagline: str, sub: str, has_svg: bool,
               projects: list | None = None, links: list | None = None,
               language: str = "zh") -> str:
    hero = """<picture>
  <source media="(prefers-color-scheme: dark)" srcset="./assets/hero-dark.svg">
  <img src="./assets/hero-light.svg" width="100%" alt="{alt}">
</picture>""".format(alt=esc(" · ".join(part for part in (name, tagline, sub) if part))) if has_svg else "\n\n".join(
        markdown_text(part) if index else "# " + markdown_text(part)
        for index, part in enumerate((name, tagline, sub)) if part)
    sections = [hero, "<!-- CONTRIB:BEGIN -->\n<!-- CONTRIB:END -->"]
    labels = LABELS[language]
    if projects:
        entries = []
        for project in projects:
            entry = f"- **[{markdown_text(project['name'])}](<{project['url']}>)**"
            if project.get("description"):
                entry += ("：" if language == "zh" else ": ") + markdown_text(project["description"])
            entries.append(entry)
        sections.append("## " + labels["projects"] + "\n\n" + "\n".join(entries))
    if links:
        entries = [f"[{markdown_text(link['name'])}](<{link['url']}>)" for link in links]
        sections.append("## " + labels["links"] + "\n\n" + " · ".join(entries))
    return "\n\n".join(sections) + "\n"


def write_scaffold(out: Path, profile: dict, overwrite: bool = False) -> None:
    has_svg = profile["theme"] in BUILDERS
    targets = ["README.md"]
    if has_svg:
        targets.extend(("assets/hero-light.svg", "assets/hero-dark.svg"))
    ensure_targets(out, targets, overwrite)
    out.mkdir(parents=True, exist_ok=True)
    if has_svg:
        (out / "assets").mkdir(exist_ok=True)
        builder = BUILDERS[profile["theme"]]
        parameters = tuple(profile[field] for field in ("name", "latin", "eyebrow", "tagline", "sub"))
        for index, (variant, colors) in enumerate((("light", LIGHT), ("dark", DARK))):
            (out / "assets" / f"hero-{variant}.svg").write_text(
                builder(*parameters, colors, ACCENTS[profile["accent"]][index]), encoding="utf-8")
    (out / "README.md").write_text(readme_for(
        profile["theme"], profile["name"], profile["tagline"], profile["sub"], has_svg,
        profile["projects"], profile["links"], profile["language"]), encoding="utf-8")


def main() -> int:
    harden_stdout()
    ap = argparse.ArgumentParser(description="生成 Profile README + hero SVG 骨架")
    ap.add_argument("--theme", default="t1", choices=["t1", "t2", "t3", "t4"])
    ap.add_argument("--name", required=True)
    ap.add_argument("--latin", default="")
    ap.add_argument("--eyebrow", default="PROFILE")
    ap.add_argument("--tagline", default="")
    ap.add_argument("--sub", default="")
    ap.add_argument("--accent", default="blue", choices=list(ACCENTS))
    ap.add_argument("--language", default="zh", choices=list(LABELS))
    ap.add_argument("--out", default=".")
    ap.add_argument("--overwrite", action="store_true", help="确认后覆盖本工具生成的同名文件")
    args = ap.parse_args()

    out = Path(args.out)
    try:
        profile = normalize_profile({field: getattr(args, field) for field in
                                     ("name", "latin", "eyebrow", "tagline", "sub", "theme", "accent", "language")})
        write_scaffold(out, profile, args.overwrite)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2

    has_svg = args.theme in BUILDERS
    print(f"已生成到 {out.resolve()}")
    if has_svg:
        print(f"   assets/hero-light.svg / hero-dark.svg  （{args.theme}）")
    print("   README.md")
    print("\n下一步：")
    print(f"  1) 本地检查：python verify_render.py --local \"{out.resolve()}\"")
    print("  2) 完整内容配置和预览：python build_profile.py --help")
    print("  3) 确认文案和视觉后再发布；不需要自己补写生成脚本")
    return 0


if __name__ == "__main__":
    sys.exit(main())
