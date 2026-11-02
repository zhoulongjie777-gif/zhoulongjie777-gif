#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线上渲染验证 —— 主页上的图真的能看见吗？

audit_profile.py 看的是**源码**，这个脚本看的是**渲染结果**。
两者会不一致：图挂了页面不报错、暗色版画错但亮色正常、SVG 里写的外部资源被 CSP 掐掉。

用法:
    python verify_render.py <username>
    python verify_render.py <username> --direct
    python verify_render.py <username> --save       # 把抓到的主页 HTML 存到本地排查
    python verify_render.py --local ./profile      # 发布前离线检查

退出码: 0 = 全部通过; 1 = 有问题。

只依赖标准库。
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ElementTree
from html.parser import HTMLParser
from pathlib import Path

UA = "profile-polish-verify/1.0"


def harden_stdout() -> None:
    """Windows 中文环境下输出重定向到管道时编码是 cp936，
    打印 ✅/❌ 会抛 UnicodeEncodeError 中断验证。降级为替换字符。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

# SVG 里会被 GitHub 掐掉 / 剥掉的东西
SVG_FORBIDDEN = [
    (r"<script[\s>]", "含 `<script>` → GitHub 会剥离"),
    (r"<foreignObject[\s>]", "含 `<foreignObject>` → 高风险，不保证渲染"),
    (r"@import\s", "含 `@import` → 外部样式被 CSP 阻止"),
    (r"<image[^>]+href=\"https?://", "含远程 `<image>` → 外部图片不加载"),
    (r"@font-face", "含 `@font-face` → 远程字体不加载，会掉回系统字体"),
    (r"\son\w+=\"", "含内联事件处理器 → GitHub 会剥离"),
]


def svg_findings(text: str, *, depth: int = 0) -> list[str]:
    if depth > 3 or len(text) > 2_000_000:
        return ["SVG 或内嵌图像过大、嵌套过深"]
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
        return ["SVG 不允许 DTD 或实体声明"]
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return ["SVG XML 无法解析"]
    issues = []
    if root.tag.rsplit("}", 1)[-1] != "svg":
        return ["根元素不是 svg"]
    if depth == 0:
        if not any(node.tag.rsplit("}", 1)[-1] == "title" and (node.text or "").strip() for node in root.iter()):
            issues.append("SVG 缺少 title 无障碍描述")
        try:
            viewbox = [float(value) for value in root.attrib.get("viewBox", "").replace(",", " ").split()]
            if len(viewbox) != 4 or not all(math.isfinite(value) for value in viewbox) or viewbox[2] <= 0 or viewbox[3] <= 0:
                raise ValueError
        except ValueError:
            issues.append("SVG 缺少有效的 viewBox")
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag in {"script", "foreignobject", "iframe"}:
            issues.append(f"SVG 含不支持的元素 {tag}")
        for attribute, value in node.attrib.items():
            name = attribute.rsplit("}", 1)[-1].lower()
            if name.startswith("on"):
                issues.append("SVG 含内联事件处理器")
            if name == "href" and not value.startswith(("#", "data:image/")):
                issues.append("SVG 引用了外部资源")
            if re.search(r"url\(\s*['\"]?(?:https?:|//)", value, re.I):
                issues.append("SVG 样式引用了外部资源")
            if tag == "image" and name == "href" and value.startswith("data:image/"):
                try:
                    header, encoded = value.split(",", 1)
                    if not header.endswith(";base64"):
                        raise ValueError
                    payload = base64.b64decode(encoded, validate=True)
                    if not payload or len(payload) > 2_000_000:
                        raise ValueError
                    if header.startswith("data:image/svg+xml"):
                        issues.extend(svg_findings(payload.decode("utf-8"), depth=depth + 1))
                except (ValueError, UnicodeError, binascii.Error):
                    issues.append("SVG 内嵌图片不是有效的 base64 图像")
        if tag == "style" and re.search(r"@import|@font-face|url\(\s*['\"]?(?:https?:|//)", node.text or "", re.I):
            issues.append("SVG 使用了被 CSP 阻止的样式或字体")
    return list(dict.fromkeys(issues))


class ProfileMarkup(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images = []
        self.sources = []
        self.pictures = []
        self.current_picture = None
        self.inline_svg = False

    def handle_starttag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        values = dict(attributes)
        if tag == "svg":
            self.inline_svg = True
        elif tag == "picture":
            self.current_picture = {"images": [], "sources": []}
            self.pictures.append(self.current_picture)
        elif tag in {"img", "source"}:
            collection = "images" if tag == "img" else "sources"
            getattr(self, collection).append(values)
            if self.current_picture is not None:
                self.current_picture[collection].append(values)

    def handle_endtag(self, tag: str) -> None:
        if tag == "picture":
            self.current_picture = None


def local_findings(root: Path) -> list[str]:
    root = root.expanduser().resolve()
    readme = root / "README.md"
    if not readme.is_file():
        return ["没有 README.md"]
    try:
        text = readme.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ["README.md 不是可读取的 UTF-8 文件"]
    issues = []
    if not text.strip():
        issues.append("README.md 为空")
    for marker in ("CONTRIB", "STATS"):
        beginning, ending = f"<!-- {marker}:BEGIN -->", f"<!-- {marker}:END -->"
        if beginning in text or ending in text:
            if text.count(beginning) != 1 or text.count(ending) != 1 or text.index(beginning) > text.index(ending):
                issues.append(f"{marker} 标记缺失、重复或顺序错误")
    markup = ProfileMarkup()
    markup.feed(text)
    if markup.inline_svg:
        issues.append("README 内联 SVG 会被 GitHub 剥离，必须引用文件")
    for picture in markup.pictures:
        if len(picture["images"]) != 1:
            issues.append("picture 必须有一个 img 亮色兜底")
        if not any(re.search(r"prefers-color-scheme\s*:\s*dark\b", source.get("media") or "", re.I) and source.get("srcset") for source in picture["sources"]):
            issues.append("picture 缺少暗色 source")
    references = []
    for image in markup.images:
        if not image.get("alt"):
            issues.append("图片缺少非空 alt 描述")
        references.append((image.get("src", ""), True))
    for source in markup.sources:
        candidates = (source.get("srcset") or "").strip().split(",")[0].split()
        references.append((candidates[0] if candidates else "", True))
    for match in re.finditer(r"(!?)\[[^\]\n]*\]\((?:<([^>]+)>|([^\s)]+))\)", text):
        references.append((match.group(2) or match.group(3), bool(match.group(1))))
    for reference, image in references:
        if not reference:
            issues.append("图片或链接路径为空")
            continue
        parsed = urllib.parse.urlsplit(reference)
        if parsed.scheme in {"http", "https", "mailto"}:
            continue
        if parsed.scheme or parsed.netloc:
            issues.append("链接使用不支持的协议：" + reference)
            continue
        if not parsed.path:
            continue
        target = (root / urllib.parse.unquote(parsed.path)).resolve()
        if not target.is_relative_to(root):
            issues.append("链接越过输出目录边界：" + reference)
        elif not target.is_file():
            issues.append("本地链接或图片不存在：" + reference)
        elif image and target.suffix.lower() == ".svg":
            try:
                issues.extend(f"{reference}: {finding}" for finding in svg_findings(target.read_text(encoding="utf-8-sig")))
            except (OSError, UnicodeError):
                issues.append("SVG 文件无法读取：" + reference)
    config = root / "profile.json"
    if config.is_file():
        try:
            profile = json.loads(config.read_text(encoding="utf-8-sig"))
            if profile.get("theme") == "t4" and (markup.images or any(image for _, image in references)):
                issues.append("t4 必须是纯 Markdown，不能引用贡献图或身份图")
        except (OSError, ValueError, AttributeError):
            issues.append("profile.json 无法解析")
    return list(dict.fromkeys(issues))


class Fetch:
    def __init__(self, direct: bool = False):
        handlers = [urllib.request.ProxyHandler({})] if direct else []
        self.opener = urllib.request.build_opener(*handlers)

    def get(self, url: str, retries: int = 3, timeout: int = 20) -> tuple[int, bytes, dict]:
        last = None
        for i in range(retries):
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            try:
                with self.opener.open(req, timeout=timeout) as r:
                    return r.status, r.read(), dict(r.headers)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 404):
                    return e.code, b"", {}
                last = e
            except Exception as e:  # noqa: BLE001
                last = e
            time.sleep(1.2 * (i + 1))
        return 0, str(last).encode(), {}


def parse_article(html: str) -> str:
    m = re.search(r'<article[^>]*itemprop="text"[^>]*>', html)
    if not m:
        m = re.search(r"<article[^>]*>", html)
    if not m:
        return ""
    start = m.end()
    end = html.find("</article>", start)
    return html[start:end if end > 0 else len(html)]


def main() -> int:
    harden_stdout()
    ap = argparse.ArgumentParser(description="发布前本地检查，或发布后线上资源检查")
    ap.add_argument("username", nargs="?")
    ap.add_argument("--local", type=Path, help="离线检查生成目录，不访问 GitHub")
    ap.add_argument("--direct", action="store_true")
    ap.add_argument("--save", action="store_true", help="保存主页 HTML")
    args = ap.parse_args()

    if bool(args.username) == bool(args.local):
        ap.error("请选择 GitHub 用户名或 --local 目录，不能同时使用")
    if args.local:
        issues = local_findings(args.local)
        for issue in issues:
            print("待修：" + issue)
        print(f"本地检查：{'未通过' if issues else '通过'}")
        print("本次仅检查结构、本地链接和 SVG 合规；外部链接连通性与实际视觉效果需另行验证。")
        return 1 if issues else 0

    f = Fetch(args.direct)
    url = f"https://github.com/{args.username}"
    status, body, _ = f.get(url)
    if status != 200:
        print(f"❌ 主页 {url} 返回 {status}", file=sys.stderr)
        return 2
    html = body.decode("utf-8", errors="replace")
    if args.save:
        p = Path(f"{args.username}-profile.html")
        p.write_text(html, encoding="utf-8")
        print(f"主页 HTML 已存到 {p.resolve()}\n")

    art = parse_article(html)
    if not art:
        print("❌ 页面里找不到 README 容器（没有 Profile README？）", file=sys.stderr)
        return 1

    print(f"# 线上渲染验证 · `{args.username}`\n")

    # ---- 结构
    pics = len(re.findall(r"<themed-picture", art))
    sources = re.findall(r'<source[^>]*srcset="([^"]+)"', art)
    imgs = re.findall(r'<img[^>]*\ssrc="([^"]+)"', art)
    print("## 结构\n")
    print(f"- `<themed-picture>` {pics} 个 · `<source>` {len(sources)} 个 · `<img>` {len(imgs)} 个")
    fails: list[str] = []
    if pics and len(sources) < pics:
        print(f"  ❌ 有 {pics} 个 picture 但只有 {len(sources)} 个 dark source —— "
              "暗色主题下会掉回亮色版")
        fails.append("picture 缺少 dark source")
    if pics:
        print("  ✅ picture 已被 GitHub 包成 themed-picture，跟随站点主题")
    print()

    # ---- 图片存活（亮色 + 暗色都要验，只验一个会漏掉一半）
    print("## 图片存活\n")

    def check(src: str, label: str) -> tuple[bool, bytes | None]:
        full = src if src.startswith("http") else "https://github.com" + src
        st, data, hdrs = f.get(full, retries=2, timeout=25)
        ctype = next((value for header, value in hdrs.items() if header.lower() == "content-type"), "")
        ok = st == 200 and ctype.startswith("image/")
        short = src if len(src) <= 70 else src[:67] + "…"
        print(f"- {'✅' if ok else '❌'} [{label}] {st} · "
              f"{ctype[:24] or '?':24} · `{short}`")
        if not ok:
            fails.append(src)
            return False, None
        return True, data

    svg_issues: dict[str, list[str]] = {}
    checked: set[str] = set()

    def audit_svg(src: str, data: bytes | None) -> None:
        if not data or len(data) > 2_000_000:
            return
        text = data.decode("utf-8", errors="replace")
        if not re.search(r"<svg[\s>]", text[:1000], re.I):
            return
        bad = svg_findings(text)
        # 自己生成的东西应该有 title / viewBox；第三方工具的输出只提示不阻断
        third_party = bool(re.search(r"githubusercontent\.com|shields\.io|ghpvc", src))
        if third_party:
            bad = [("⚠️ " + issue) if "title" in issue or "viewBox" in issue else issue for issue in bad]
        if bad:
            svg_issues[src] = bad

    for src in sources:
        if src in checked:
            continue
        checked.add(src)
        ok, data = check(src, "暗")
        if ok:
            audit_svg(src, data)
    for src in imgs:
        if src in checked:
            continue
        checked.add(src)
        ok, data = check(src, "亮")
        if ok:
            audit_svg(src, data)
    print()

    # ---- SVG 内容合规
    hard_svg = {k: v for k, v in svg_issues.items()
                if any(not x.startswith("⚠️") for x in v)}
    if svg_issues:
        print("## SVG 内容检查\n")
        for src, issues in svg_issues.items():
            print(f"`{src}`")
            for i in issues:
                print(f"  - {i}")
        print()
        fails.extend(hard_svg.keys())

    # ---- 结论
    print("## 结论\n")
    if fails:
        print(f"**{len(fails)} 项待修**：")
        for i, x in enumerate(dict.fromkeys(fails), 1):
            print(f"{i}. `{x}`")
        print()
        print("> 图片 404 通常是路径写错或还没 push；content-type 不是 image/* 说明"
              "GitHub 没把它当图片给，多半是文件太大或后缀不对。")
        return 1
    print("全部通过 ✅")
    print()
    print("> 这个脚本查的是「能不能看见」。**看不见之外的问题它查不到**——"
          "字号太小、对比度不够、暗色版配色错了，都得 headless chrome 截图看：")
    print("> `chrome.exe --headless --disable-gpu --screenshot=out.png "
          "--window-size=962,400 file:///<svg路径>`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
