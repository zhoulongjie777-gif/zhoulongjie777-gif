#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「开源贡献」卡片：官方 logo + ★star + 已合并 PR 数，亮暗双版。

数据全部来自 GitHub 公开 API，任何数字都能被第三方复算：
    /search/issues?q=author:<user>+type:pr+is:merged   → 已合并 PR 总数 + 逐仓库分布
    /repos/<owner>/<repo>                              → star 数、仓库描述、owner 官方头像

logo 用 base64 内嵌进 SVG，不写外链——GitHub 给 raw SVG 的响应头是
`Content-Security-Policy: default-src 'none'`，外链图片一律被拦，
只有 data: URI 能显示（2026-11 按线上 CSP 本地 1:1 复现验证过）。

logo 来源：**默认就是仓库自己的 owner 官方头像**，不内置任何品牌图标库
（内置一份 25 个图标 ≈98KB，还会随品牌改版过期，且涉及商标归属）。
只有想覆盖时才用 `--logo owner=path` 或把文件放进 `logos/`。

用法:
    python generate_contrib.py shi-felix --out ./profile
    python generate_contrib.py shi-felix --out ./profile --top 5 --accent blue
    python generate_contrib.py shi-felix --out ./profile --offline   # 用缓存重渲染
    python generate_contrib.py shi-felix --json contrib.json          # 同时导出数据

只依赖标准库。token 优先取 --token，其次 GITHUB_TOKEN / GH_TOKEN，再次 `gh auth token`。
没有 token 时 search API 限流 10 次/分钟，仓库接口 60 次/小时——够跑一次，但别狂刷。
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scaffold as S  # noqa: E402  复用色值、字体栈、字宽与转义，保证设计口径唯一

API = "https://api.github.com"
UA = "profile-polish-contrib/1.0"
BEGIN, END = "<!-- CONTRIB:BEGIN -->", "<!-- CONTRIB:END -->"

# 版式常量（962 宽画布，GitHub 内容列 806 → 缩放 0.84）
W = 962
MARGIN = 40
LIST_TOP = 206
ROW_H = 62
LOGO = 30

LOGO_DIR = Path(__file__).resolve().parent.parent / "logos"


# ---------------------------------------------------------------- logo 解析
# 设计决策：**不内置任何品牌图标**。
# 权威来源就是仓库自己的 owner 头像（实时、换了 logo 自动跟上），
# 内置一份图标库既增加体积（实测 98KB/25 个），又会随品牌改版过期，
# 还有商标归属问题。只在「自己想覆盖」时留一个本地目录。
def _read_logo_file(p: Path) -> dict | None:
    try:
        if p.suffix.lower() == ".svg":
            return {"kind": "svg", "svg": p.read_text(encoding="utf-8-sig")}
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "webp": "image/webp", "gif": "image/gif"}.get(p.suffix.lower().lstrip("."))
        if not mime:
            return None
        return {"kind": "raster", "b64": base64.b64encode(p.read_bytes()).decode(),
                "mime": mime}
    except OSError:
        return None


def resolve_logo(owner: str, overrides: dict, repo: dict | None = None,
                 allow_local: bool = True, allow_avatar: bool = True,
                 f: "Fetcher | None" = None) -> tuple[dict | None, str]:
    """取 logo：`--logo` 指定 > `logos/<owner>.*` 自备 > GitHub 官方头像 > 无。

    返回 (logo, 来源标记)。`allow_local=False`（--avatar-only）只用官方头像。
    """
    key = owner.lower()
    if allow_local:
        if key in overrides:
            got = _read_logo_file(overrides[key])
            if got:
                return got, "override"
        for suffix in (".svg", ".png", ".jpg", ".jpeg", ".webp"):
            p = LOGO_DIR / f"{key}{suffix}"
            if p.exists():
                got = _read_logo_file(p)
                if got:
                    return got, "local"
    if allow_avatar and f and repo:
        avatar = ((repo.get("owner") or {}).get("avatar_url") or "")
        if avatar:
            sep = "&" if "?" in avatar else "?"
            st, body, hdrs = f.raw(f"{avatar}{sep}s=64", retries=2, timeout=15)
            ctype = next((value for header, value in hdrs.items() if header.lower() == "content-type"), "")
            if st == 200 and ctype.startswith("image/") and len(body) < 200_000:
                return ({"kind": "raster", "b64": base64.b64encode(body).decode(),
                         "mime": ctype.split(";")[0]}, "avatar")
    return None, "none"


def logo_href(logo: dict | None, C: dict) -> str:
    """把 logo 变成 data: URI。

    自备的 SVG 若自身没写 fill，就注入主题文字色 —— 这样同一个文件在亮暗两版都看得见；
    已经写了 fill 的彩色 logo 不受影响（根元素 fill 只对没写 fill 的图元生效）。
    """
    if not logo:
        return ""
    if logo.get("kind") == "svg":
        from verify_render import svg_findings

        findings = svg_findings(logo["svg"], depth=1)
        if findings:
            raise ValueError("自定义 logo 不是安全的独立 SVG：" + "; ".join(findings))
        root = ElementTree.fromstring(logo["svg"])
        if "fill" not in root.attrib:
            root.set("fill", C["text"])
        svg = ElementTree.tostring(root, encoding="unicode")
        return "data:image/svg+xml;base64," + base64.b64encode(
            svg.encode("utf-8")).decode()
    return f'data:{logo["mime"]};base64,{logo["b64"]}'


class Fetcher:
    def __init__(self, token: str | None):
        self.token = token
        self.opener = urllib.request.build_opener()

    def raw(self, url: str, *, accept: str | None = None,
            retries: int = 3, timeout: int = 20) -> tuple[int, bytes, dict]:
        last: Exception | str = "unknown"
        for i in range(retries):
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            if accept:
                req.add_header("Accept", accept)
            destination = urllib.parse.urlsplit(url)
            if self.token and destination.scheme == "https" and destination.hostname == "api.github.com":
                req.add_header("Authorization", f"Bearer {self.token}")
            try:
                with self.opener.open(req, timeout=timeout) as r:
                    return r.status, r.read(), dict(r.headers)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 404, 422):
                    return e.code, b"", dict(e.headers or {})
                last = e
            except Exception as e:  # noqa: BLE001
                last = e
            time.sleep(1.2 * (i + 1))
        return 0, str(last).encode(), {}

    def json(self, path: str) -> tuple[dict | list | None, int]:
        url = path if path.startswith("http") else API + path
        st, body, _ = self.raw(url, accept="application/vnd.github+json")
        if st != 200:
            return None, st
        try:
            return json.loads(body.decode("utf-8")), st
        except json.JSONDecodeError:
            return None, st


def get_token(cli: str | None) -> str | None:
    if cli:
        return cli
    for env in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(env):
            return os.environ[env]
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------- 取数
def collect_merged(f: Fetcher, user: str, max_pages: int = 3,
                   include_self: bool = False) -> tuple[dict, int, str, int]:
    """返回 ({repo: 样本内 PR 数}, 总数, 最近合并年月, 实际抓到的条数)。

    ⚠️ 样本内计数**不等于**该仓库的真实 PR 数：search API 最多翻 1000 条，
    对贡献多的人（如 1910 个 PR）样本只覆盖最近一小段。
    逐仓库的真实数字必须再用 `repo:` 维度精确查（见 exact_pr_count）。

    默认排除 `user:<自己>`：自己给自己合并的 PR 不是「上游贡献」，
    混进来会让卡片的口径站不住（见 references/content-model.md）。
    """
    q = f"author:{user} type:pr is:merged is:public"
    if not include_self:
        q += f" -user:{user}"
    counts: dict[str, int] = {}
    total = 0
    latest = ""
    fetched = 0
    page = 0
    url = (f"{API}/search/issues?q={urllib.parse.quote(q)}"
           f"&per_page=100&sort=updated&order=desc")
    while url and page < max_pages:
        data, st = f.json(url)
        if not isinstance(data, dict):
            if st == 403:
                raise RuntimeError("search API 被限流（403）。等一分钟，或配 GITHUB_TOKEN，"
                                   "或直接用 --offline 读缓存")
            raise RuntimeError(f"search API 返回 {st}")
        if data.get("incomplete_results"):
            raise RuntimeError("GitHub 返回了不完整的搜索结果，请稍后重试；不把部分结果当成真实总数")
        total = data.get("total_count", total)
        for it in data.get("items", []):
            repo_url = (it.get("repository_url") or "")
            m = re.search(r"/repos/([^/]+/[^/]+)$", repo_url)
            if not m:
                continue
            name = m.group(1)
            counts[name] = counts.get(name, 0) + 1
            fetched += 1
            closed = (it.get("closed_at") or "")[:7]
            if closed > latest:
                latest = closed
        page += 1
        url = ""
        if page < max_pages and len(data.get("items", [])) == 100:
            url = (f"{API}/search/issues?q={urllib.parse.quote(q)}"
                   f"&per_page=100&sort=updated&order=desc&page={page + 1}")
    return counts, total, latest, fetched


def search_total(f: Fetcher, q: str) -> int:
    """跑一条 search 查询，只取 total_count；失败返回 -1。"""
    data, st = f.json(f"{API}/search/issues?q={urllib.parse.quote(q)}&per_page=1")
    if isinstance(data, dict) and "total_count" in data and not data.get("incomplete_results"):
        return int(data["total_count"])
    return -1


def exact_pr_count(f: Fetcher, user: str, repo: str) -> int:
    """该用户在本仓库的**真实**已合并 PR 数（search 的 total_count）。

    卡片上的每个数字都必须走这里：样本计数在大账号上会差一到两个数量级
    （实测 antfu 的 vitejs/vite：样本数 1，真实 130）。
    """
    return search_total(f, f"author:{user} type:pr is:merged is:public repo:{repo}")


def collect_repos(f: Fetcher, names: list[str], overrides: dict,
                  allow_local: bool = True,
                  allow_avatar: bool = True) -> list[dict]:
    out = []
    for name in names:
        data, st = f.json(f"/repos/{name}")
        if not isinstance(data, dict):
            out.append({"repo": name, "stars": -1, "desc": "", "owner": name.split("/")[0],
                        "logo": None, "logo_src": "none"})
            continue
        if data.get("private"):
            raise RuntimeError("GitHub 返回了非公开仓库，已停止生成，避免泄露私有贡献")
        owner_login = ((data.get("owner") or {}).get("login")
                       or name.split("/")[0])
        logo, src = resolve_logo(owner_login, overrides, data,
                                 allow_local, allow_avatar, f)
        out.append({
            "repo": data.get("full_name") or name,
            "stars": data.get("stargazers_count", -1),
            "desc": (data.get("description") or "").strip(),
            "owner": owner_login,
            "logo": logo,
            "logo_src": src,
        })
    return out


def collect_data(fetcher: Fetcher, user: str, *, max_pages: int = 3,
                 probe: int = 8, include_self: bool = False,
                 overrides: dict | None = None, avatar_only: bool = False,
                 sort: str = "prs") -> dict:
    account, status = fetcher.json(f"/users/{user}")
    if not isinstance(account, dict):
        raise RuntimeError(f"无法读取 GitHub 用户 {user}（状态 {status}），请检查用户名、网络或认证")
    if account.get("type") != "User":
        raise ValueError("此工具面向 GitHub 个人主页，不支持组织账号")
    counts, total, last, fetched = collect_merged(fetcher, user, max_pages, include_self)
    if total and not counts:
        raise RuntimeError("搜索返回了非零总数但没有可核实的仓库，未生成贡献统计")
    candidates = sorted(counts, key=lambda name: counts[name], reverse=True)[:probe]
    repos = collect_repos(fetcher, candidates, overrides or {}, allow_local=not avatar_only)
    for repo, name in zip(repos, candidates):
        exact = exact_pr_count(fetcher, user, name)
        if exact < 0:
            raise RuntimeError(f"无法核实 {name} 的 PR 数；未使用抽样数量替代，原缓存保持不变")
        repo["prs"] = exact
        repo["prs_exact"] = True
    sort_key = (lambda repo: (repo["prs"], repo["stars"])) if sort == "prs" else (
        lambda repo: (repo["stars"], repo["prs"]))
    repos.sort(key=sort_key, reverse=True)
    query = f"author:{user} type:pr is:merged is:public" + ("" if include_self else f" -user:{user}")
    return {
        "user": user, "scope": "all" if include_self else "upstream", "visibility": "public", "query": query,
        "total_merged": total, "last_merged": last, "complete": fetched >= total,
        "repo_count": len(counts), "repos": repos,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def validate_data(data: dict, user: str, include_self: bool = False) -> dict:
    if not isinstance(data, dict) or str(data.get("user", "")).lower() != user.lower():
        raise ValueError("缓存用户名与当前用户不一致，不能复用他人的贡献数据")
    scope = "all" if include_self else "upstream"
    if data.get("scope") != scope:
        raise ValueError("缓存缺少统计口径或与当前口径不一致，请重新联网生成缓存")
    if data.get("visibility") != "public" or "is:public" not in str(data.get("query", "")):
        raise ValueError("缓存未限定公开贡献，请重新联网生成，不能将私有统计发布到主页")
    if type(data.get("total_merged")) is not int or data["total_merged"] < 0:
        raise ValueError("缓存中的 PR 总数无效")
    if not isinstance(data.get("repos"), list):
        raise ValueError("缓存中的 repos 必须是列表")
    if bool(data["total_merged"]) != bool(data["repos"]):
        raise ValueError("缓存总数与仓库列表不一致")
    seen = set()
    for repo in data["repos"]:
        if not isinstance(repo, dict) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(repo.get("repo", ""))):
            raise ValueError("缓存中的仓库名无效")
        if repo["repo"].lower() in seen:
            raise ValueError("缓存中出现重复仓库")
        seen.add(repo["repo"].lower())
        if type(repo.get("prs")) is not int or repo["prs"] <= 0 or repo.get("prs_exact") is not True:
            raise ValueError("缓存含未核实的 PR 数，请重新联网生成")
        if type(repo.get("stars")) is not int or repo["stars"] < -1:
            raise ValueError("缓存中的 star 数无效")
        if not isinstance(repo.get("desc", ""), str):
            raise ValueError("缓存中的仓库描述无效")
    if sum(repo["prs"] for repo in data["repos"]) > data["total_merged"]:
        raise ValueError("各仓库 PR 数超过总数，请重新获取一致的数据快照")
    return data


# ---------------------------------------------------------------- 版式
def fmt_stars(n: int) -> str:
    if n < 0:
        return "?"
    if n < 1000:
        return str(n)
    if n < 100_000:
        v = f"{n / 1000:.1f}"
        return (v[:-2] if v.endswith(".0") else v) + "k"
    return f"{n / 1000:.0f}k"


def render(rows: list[dict], total: int, last: str, C: dict, accent: str,
           complete: bool = True, repo_count: int = 0, *, theme: str = "t1",
           include_self: bool = False, language: str = "zh") -> str:
    n = len(rows)
    last_bottom = LIST_TOP + (n - 1) * ROW_H + 34
    line_y = last_bottom + 24
    note_y = line_y + 26
    H = note_y + 16

    offset = 34 if theme == "t2" else 0
    p = [S.svg_open(W, H + offset, f"{total} merged PRs")]
    if theme in {"t2", "t3"}:
        p.append(f'<rect x="16" y="1" width="{W - 32}" height="{H + offset - 2}" rx="8" '
                 f'fill="{C["canvas"]}" stroke="{C["border"]}"/>')
    if theme == "t2":
        p.append(S.tx(MARGIN, 25, "$ github contributions", 13, accent, family=S.MONO))
        p.append(f'<line x1="16" y1="34" x2="{W - 16}" y2="34" stroke="{C["border"]}"/>')
        p.append('<g transform="translate(0,34)">')
    # 眉行
    p.append(f'<rect x="{MARGIN}" y="47" width="7" height="7" fill="{accent}"/>')
    heading = "MERGED PULL REQUESTS" if include_self else "OPEN SOURCE / MERGED PULL REQUESTS"
    p.append(S.tx(56, 54, heading, 13, accent, "600", family=S.MONO))
    # 焦点数字：唯一的重心
    num = str(total)
    number_size = min(96, W * 0.45 / max(S.text_w(num, 1), 1))
    number_width = S.text_w(num, number_size)
    p.append(S.tx(MARGIN - 4, 152, num, number_size, C["text"], "700", max_width=number_width / 0.95))
    nx = MARGIN - 4 + number_width + 18
    p.append(S.tx(nx, 130, "merged PRs", 18.5, C["muted"], max_width=W - MARGIN - nx))
    # 副标题只说能被 API 证实的：抓全了才敢报仓库总数，
    # 抓不全（大账号翻不完）就老实说是「展示贡献最深的 N 个」
    if not complete:
        sub = f"展示 {n} 个已核实的贡献仓库"
    elif repo_count > n:
        sub = f"覆盖 {repo_count} 个仓库 · 展示前 {n} 个"
    else:
        sub = f"覆盖 {repo_count} 个仓库"
    if language == "en":
        sub = f"Showing {n} verified {'repository' if n == 1 else 'repositories'}" if not complete else (
            f"Across {repo_count} {'repository' if repo_count == 1 else 'repositories'}")
    if include_self:
        sub += " / includes own repositories" if language == "en" else " · 包含自有仓库"
    p.append(S.tx(nx, 152, S.clip(sub, 13, W - MARGIN - nx), 13, C["muted"], max_width=W - MARGIN - nx))
    p.append(f'<line x1="{MARGIN}" y1="182" x2="{W - MARGIN}" y2="182" '
             f'stroke="{C["border"]}" stroke-width="1"/>')

    right = W - MARGIN
    name_x = MARGIN + LOGO + 12
    reserve = 150                      # 右侧 star / PR 列
    avail = right - reserve - name_x
    for i, r in enumerate(rows):
        y = LIST_TOP + i * ROW_H
        href = logo_href(r.get("logo"), C)
        if href:
            p.append(f'<defs><clipPath id="lg{i}"><rect x="{MARGIN}" y="{y + 3}" '
                     f'width="{LOGO}" height="{LOGO}" rx="7"/></clipPath></defs>')
            p.append(f'<image x="{MARGIN}" y="{y + 3}" width="{LOGO}" height="{LOGO}" '
                     f'clip-path="url(#lg{i})" href="{href}"/>')
        else:
            # 没有 logo 就退成一个字母方块，不要让整行塌掉
            p.append(f'<rect x="{MARGIN}" y="{y + 3}" width="{LOGO}" height="{LOGO}" '
                     f'rx="7" fill="{C["inset"]}"/>')
            p.append(S.tx(MARGIN + LOGO / 2, y + 24, r["repo"][:1].upper(), 14,
                          C["muted"], "700", anchor="middle"))
        p.append(S.tx(name_x, y + 14, S.clip(r["repo"], 19, avail, word=False), 19,
                      C["text"], "700", max_width=avail))
        desc = r.get("desc") or ("No repository description" if language == "en" else "（仓库无描述）")
        p.append(S.tx(name_x, y + 34, S.clip(desc, 13, avail, word=True), 13, C["muted"], max_width=avail))
        p.append(S.tx(right, y + 14, f'★ {fmt_stars(r["stars"])}', 17, C["muted"],
                      "600", anchor="end"))
        n_pr = r["prs"]
        p.append(S.tx(right, y + 34, f'{n_pr} PR' if n_pr == 1 else f'{n_pr} PRs', 13,
                      accent, "600", family=S.MONO, anchor="end"))
    p.append(f'<line x1="{MARGIN}" y1="{line_y}" x2="{W - MARGIN}" y2="{line_y}" '
             f'stroke="{C["border"]}" stroke-width="1"/>')
    note = "GitHub Search API / is:pr is:merged / Each PR is publicly verifiable" if language == "en" else (
        "数据源 GitHub Search API（is:pr is:merged）· 每个 PR 均可在 GitHub 复核")
    p.append(S.tx(MARGIN, note_y, note, 13, C["muted"]))
    if theme == "t2":
        p.append("</g>")
    p.append("</svg>")
    return "\n".join(p)


def snippet(data: dict | None = None, *, theme: str = "t1", language: str = "zh", top: int = 4) -> str:
    if data is not None and data["total_merged"] == 0:
        return f"{BEGIN}\n{END}"
    if theme == "t4" and data is not None:
        label = "已合并 PR" if language == "zh" else "Merged PRs"
        if data["scope"] == "all":
            label += "（包含自有仓库）" if language == "zh" else " (including own repositories)"
        entries = [f"{BEGIN}", f"## {label}", "", f"**{data['total_merged']}**", ""]
        for repo in data["repos"][:top]:
            entries.append(f"- [{repo['repo']}](https://github.com/{repo['repo']}) · {repo['prs']} {'PR' if repo['prs'] == 1 else 'PRs'} · ★ {fmt_stars(repo['stars'])}")
        query = urllib.parse.quote(data["query"])
        entries.extend(("", f"[GitHub Search](https://github.com/search?q={query}&type=pullrequests)", END))
        return "\n".join(entries)
    return f"""{BEGIN}
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="./assets/contrib-dark.svg">
  <img src="./assets/contrib-light.svg" width="100%" alt="Merged pull requests and repositories">
</picture>
{END}"""


def write_contribution(out: Path, data: dict, *, theme: str = "t1", accent: str = "blue",
                       top: int = 4, language: str = "zh", update_readme: bool = True) -> None:
    from verify_render import svg_findings

    include_self = data.get("scope") == "all"
    validate_data(data, data["user"], include_self)
    profile_path = out / "profile.json"
    if profile_path.is_file() and S.read_profile(profile_path)["username"].lower() != data["user"].lower():
        raise ValueError("贡献账号与该目录的个人配置不一致，请使用另一个输出目录")
    cache = out / "assets" / "contrib.json"
    if cache.is_file():
        try:
            previous = json.loads(cache.read_text(encoding="utf-8-sig"))
            meaningful = lambda snapshot: {key: value for key, value in snapshot.items() if key != "generated_at"}
            if isinstance(previous, dict) and meaningful(previous) == meaningful(data):
                data["generated_at"] = previous.get("generated_at", data.get("generated_at"))
        except (OSError, ValueError):
            pass
    outputs = {"assets/contrib.json": json.dumps(data, ensure_ascii=False, indent=2) + "\n"}
    rows = data["repos"][:top]
    if theme != "t4" and rows:
        for index, (variant, colors) in enumerate((("light", S.LIGHT), ("dark", S.DARK))):
            svg = render(rows, data["total_merged"], data.get("last_merged", ""), colors,
                         S.ACCENTS[accent][index], bool(data.get("complete")), int(data.get("repo_count", len(rows))),
                         theme=theme, include_self=include_self, language=language)
            findings = svg_findings(svg)
            if findings:
                raise ValueError("贡献图不符合渲染要求：" + "; ".join(findings))
            outputs[f"assets/contrib-{variant}.svg"] = svg
    readme = out / "README.md"
    if update_readme and readme.exists():
        text = readme.read_text(encoding="utf-8")
        if text.count(BEGIN) != 1 or text.count(END) != 1 or text.index(BEGIN) > text.index(END):
            raise ValueError("README 必须有且只有一组顺序正确的 CONTRIB:BEGIN / END 标记")
        beginning, tail = text.split(BEGIN, 1)
        _, ending = tail.split(END, 1)
        outputs["README.md"] = beginning + snippet(data, theme=theme, language=language, top=top) + ending
    S.ensure_targets(out, list(outputs), overwrite=True)
    for relative, content in outputs.items():
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------- main
def main() -> int:
    S.harden_stdout()
    ap = argparse.ArgumentParser(description="生成开源贡献卡（logo + star + PR 数）")
    ap.add_argument("user")
    ap.add_argument("--out", default=".")
    ap.add_argument("--top", type=int, default=4, help="展示几个仓库，默认 4")
    ap.add_argument("--accent", default="blue", choices=list(S.ACCENTS))
    ap.add_argument("--theme", default="t1", choices=list(S.THEMES))
    ap.add_argument("--language", default="zh", choices=list(S.LABELS))
    ap.add_argument("--offline", action="store_true", help="只读 assets/contrib.json 缓存")
    ap.add_argument("--token", default=None)
    ap.add_argument("--json", default=None, help="把结构化数据写到指定文件")
    ap.add_argument("--max-pages", type=int, default=3, help="search 最多翻几页（100/页）")
    ap.add_argument("--probe", type=int,
                    help="精确查询候选仓库数，默认 max(8, top)，每个仓库一次 search 调用")
    ap.add_argument("--sort", default="prs", choices=["prs", "stars"],
                    help="列表排序：prs=贡献深度（默认），stars=仓库含金量")
    ap.add_argument("--include-self", action="store_true",
                    help="把自己的仓库也算进来（默认只统计提给他人的 PR）")
    ap.add_argument("--logo", action="append", default=[], metavar="OWNER=PATH",
                    help="用本地文件覆盖某个组织的 logo，可重复：--logo vitejs=./my.svg")
    ap.add_argument("--avatar-only", action="store_true",
                    help="只用 GitHub 官方头像，忽略 logos/ 里的内置图标")
    ap.add_argument("--no-readme", action="store_true", help="不回写 README 标记区间")
    args = ap.parse_args()
    if args.probe is None:
        args.probe = max(8, args.top)

    out = Path(args.out).expanduser()
    cache = out / "assets" / "contrib.json"
    try:
        S.normalize_profile({"username": args.user})
        if not 1 <= args.top <= 10 or not 1 <= args.max_pages <= 10 or not args.top <= args.probe <= 30:
            raise ValueError("top 和 max-pages 必须在 1-10 之间；probe 必须不小于 top 且不超过 30")
        overrides = {}
        for item in args.logo:
            if "=" not in item:
                raise ValueError("--logo 必须写成 OWNER=PATH")
            owner, filename = item.split("=", 1)
            logo_path = Path(filename.strip()).expanduser()
            if not owner.strip() or not logo_path.is_file():
                raise ValueError("--logo 指定的组织名为空或文件不存在")
            overrides[owner.strip().lower()] = logo_path
        if args.offline:
            data = json.loads(cache.read_text(encoding="utf-8-sig"))
        else:
            data = collect_data(Fetcher(get_token(args.token)), args.user, max_pages=args.max_pages,
                                probe=args.probe, include_self=args.include_self, overrides=overrides,
                                avatar_only=args.avatar_only, sort=args.sort)
        validate_data(data, args.user, args.include_self)
        export = Path(args.json).expanduser() if args.json else None
        if export:
            S.ensure_targets(export.parent, [export.name])
        write_contribution(out, data, theme=args.theme, accent=args.accent, top=args.top,
                           language=args.language, update_readme=not args.no_readme)
        if export:
            export.parent.mkdir(parents=True, exist_ok=True)
            export.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        print("网络或认证失败时不会伪造数据；已有匹配缓存可使用 --offline。", file=sys.stderr)
        return 2
    rows = data["repos"][:args.top]
    if not rows:
        print("当前统计范围没有已合并 PR，已清空贡献展示区；可以继续展示自己的作品和介绍。")
    else:
        print(f"{data['total_merged']} 个已合并 PR · 展示 {len(rows)} 个已核实仓库")
        for repo in rows:
            print(f"  {repo['repo']} · {repo['prs']} PRs · ★ {fmt_stars(repo['stars'])}")
    if args.no_readme or not (out / "README.md").exists():
        print(snippet(data, theme=args.theme, language=args.language, top=args.top))
    print(f"产物目录：{out.resolve()}；发布前请完成本地和线上检查。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
