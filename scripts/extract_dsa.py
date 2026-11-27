# -*- coding: utf-8 -*-
"""解析 DSA 的 reports/ 产物。

DSA 的 main.py 把报告写到 reports/（HTML 为主，文件名和内部结构随
上游版本变，不做强假设）。本脚本的策略：
  1. 把 reports/ 原样拷到 docs/data/reports/<日期>/，前端直接链接 HTML 看全文；
  2. 顺带找里面的 *.json（如果上游写了结构化报告），按“看起来像研报”
     的启发式规则抽出 code / name / score / advice / summary，写成
     docs/data/reports/<日期>/dsa.json 供 merge_data.py 用；
  3. JSON 没有时再解析 report_*.md（DSA 实际写出的决策仪表盘）和
     market_review_*.md（大盘复盘）——2026-11-27 实测 DSA 只写 md，
     不写 JSON；找不到 JSON 也不报错——前端照样能点开 HTML 看，
     只是列表里不显示评分和结论摘要。

用法：
  python scripts/extract_dsa.py --reports-dir vendor/dsa/reports \
      --out-dir docs/data/reports/2026-11-27 [--watchlist 002242,600519,000625]
"""
from __future__ import annotations

import argparse
import html as html_lib
import re
import shutil
from pathlib import Path

from common import load_json, save_json, setup_logging

log = setup_logging("extract_dsa")

CODE_RE = re.compile(r"\b([036]\d{5})\b")

# 摘要行，如：🟠 **长安汽车(000625)**: 减仓 | 评分 32 | 看空
SUMMARY_LINE_RE = re.compile(
    r"\*\*([^*()]+?)\((\d{6})\)\*\*\s*:\s*([^|\n]+?)\s*\|\s*评分\s*(\d+)(?:\s*\|\s*([^\n]*))?"
)
# 个股小节里的一句话决策，如：> **一句话决策**: 空头排列且套牢盘极重……
DECISION_RE = re.compile(r"一句话决策\*\*:\s*(.+)")
# 核心结论里的操作/方向，如：**🟠 减仓** | 看空
ACTION_RE = re.compile(
    r"\*\*[^*]*?(买入|增持|持有|观望|减仓|卖出)[^*]*?\*\*\s*\|\s*(看多|看空|中性|震荡)"
)


def _looks_like_report(data) -> dict | None:
    """启发式判断一个 JSON 对象是不是单只股的研报，返回抽出的字段。"""
    if not isinstance(data, dict):
        return None
    text = str(data)
    code = None
    for key in ("code", "stock_code", "symbol", "ts_code"):
        value = str(data.get(key, "")).strip()
        if value:
            match = CODE_RE.search(value)
            if match:
                code = match.group(1)
                break
    if code is None:
        match = CODE_RE.search(text[:2000])
        if not match:
            return None
        code = match.group(1)
    name = ""
    for key in ("name", "stock_name", "company_name"):
        if data.get(key):
            name = str(data[key]).strip()
            break
    score = None
    for key in ("score", "total_score", "rating_score", "final_score"):
        value = data.get(key)
        if isinstance(value, (int, float)):
            score = value
            break
    advice = ""
    for key in ("advice", "operation_advice", "recommendation", "action", "conclusion"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            advice = value.strip()
            break
    summary = ""
    for key in ("summary", "core_conclusion", "brief", "analysis_summary"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            summary = value.strip()
            break
    return {"code": code, "name": name, "score": score, "advice": advice, "summary": summary}


def _parse_report_md(text: str) -> dict[str, dict]:
    """解析 DSA 的 report_*.md（决策仪表盘），返回 {code: 研报字段}。

    主路径：“分析结果摘要”里的行，如
        🟠 **长安汽车(000625)**: 减仓 | 评分 32 | 看空
    摘要取自各只股小节里的“一句话决策”行。
    摘要行缺失时退到小节解析：标题里的 code/name + 核心结论里的
    操作/方向 + 小节内出现的评分。
    """
    entries: dict[str, dict] = {}
    for match in SUMMARY_LINE_RE.finditer(text):
        name, code, action, score, direction = match.groups()
        action = action.strip()
        direction = (direction or "").strip()
        advice = f"{action}/{direction}" if direction else action
        entries[code] = {
            "code": code,
            "name": name.strip(),
            "score": int(score),
            "advice": advice,
            "summary": "",
        }
    # 按 ## 小节切分，找每只股的“一句话决策”
    for section in re.split(r"(?m)^##\s+", text):
        head = section.split("\n", 1)[0].strip()
        m = re.match(r"(?:\S+\s+)?(.+?)\s*\((\d{6})\)\s*$", head)
        if not m:
            continue
        name, code = m.group(1).strip(), m.group(2)
        decision = DECISION_RE.search(section)
        summary = decision.group(1).strip() if decision else ""
        if code in entries:
            if summary and not entries[code].get("summary"):
                entries[code]["summary"] = summary
            continue
        # 摘要行里没有这只：从小节里兜底
        action_m = ACTION_RE.search(section)
        score_m = re.search(r"评分\s*(\d+)", section)
        advice = ""
        if action_m:
            advice = action_m.group(1)
            if action_m.group(2):
                advice = f"{advice}/{action_m.group(2)}"
        entries[code] = {
            "code": code,
            "name": name,
            "score": int(score_m.group(1)) if score_m else None,
            "advice": advice,
            "summary": summary,
        }
    return entries


def _md_to_text(text: str) -> str:
    """把复盘 Markdown 粗转成纯文本（前端用 textContent 显示）：
    去标题/加粗/引用标记，表格行转成空格分隔的文本行（保留指数和
    板块数字），丢分隔行和文末脚注。"""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("---"):
            continue
        if line.startswith("*复盘时间") or line.startswith("*报告生成时间"):
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue  # 表格分隔行
            line = " ".join(c for c in cells if c)
            if line:
                lines.append(line)
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = line.replace("**", "").replace("__", "")
        line = re.sub(r"^>\s*", "", line)
        lines.append(line)
    return "\n".join(lines).strip()


def _split_stock_sections(text: str) -> dict[str, tuple[str, str]]:
    """按 ## 小节切分 report_*.md，返回 {code: (name, 正文)}。

    正文不含标题行本身（`⚪ 九阳股份 (002242)` 这类标题由页面
    头部单独渲染），含小节内全部内容，直到下一个 ## 小节或文件尾。
    只认标题末尾带 (6 位代码) 的小节；“分析结果摘要”小节没有
    代码，不会进来。
    """
    sections: dict[str, tuple[str, str]] = {}
    for chunk in re.split(r"(?m)^##\s+", text):
        head, sep, body = chunk.partition("\n")
        m = re.match(r"(?:\S+\s+)?(.+?)\s*\((\d{6})\)\s*$", head.strip())
        if not m:
            continue
        name, code = m.group(1).strip(), m.group(2)
        sections[code] = (name, body.strip("\n"))
    return sections


# 行内 Markdown：先转义 HTML，再套格式（顺序：代码 → 链接 → 加粗 → 斜体；
# 斜体正则排除 <>，不会横跨前面生成的标签）。
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_INLINE_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_INLINE_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_INLINE_ITALIC_RE = re.compile(r"(?<![*\w])\*([^*\n<>]+)\*(?![*\w])")


def _inline_md(text: str) -> str:
    """一行内的 Markdown → HTML（**加粗** / *斜体* / `代码` / [链接](url)）。"""
    out = html_lib.escape(text, quote=False)
    out = _INLINE_CODE_RE.sub(lambda m: "<code>" + m.group(1) + "</code>", out)
    out = _INLINE_LINK_RE.sub(
        lambda m: '<a href="' + html_lib.escape(m.group(2), quote=True) + '">'
        + m.group(1) + "</a>",
        out,
    )
    out = _INLINE_BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _INLINE_ITALIC_RE.sub(r"<em>\1</em>", out)
    return out


def _table_to_html(rows: list[str]) -> str:
    """GFM 表格 → <table>。第二行是分隔行时第一行当表头。"""
    def cells(row: str) -> list[str]:
        return [c.strip() for c in row.strip().strip("|").split("|")]

    def is_sep(row: list[str]) -> bool:
        return bool(row) and all(re.fullmatch(r":?-{2,}:?", c) for c in row)

    def align(cell: str) -> str:
        if cell.startswith(":") and cell.endswith(":"):
            return ' style="text-align:center"'
        if cell.endswith(":"):
            return ' style="text-align:right"'
        return ""

    header = None
    aligns: list[str] = []
    body_rows = rows
    if len(rows) >= 2 and is_sep(cells(rows[1])):
        header = cells(rows[0])
        aligns = [align(c) for c in cells(rows[1])]
        body_rows = rows[2:]
    parts = ["<table>"]
    if header is not None:
        parts.append("<thead><tr>" + "".join(
            f"<th{aligns[k] if k < len(aligns) else ''}>" + _inline_md(c) + "</th>"
            for k, c in enumerate(header)) + "</tr></thead>")
    parts.append("<tbody>")
    for row in body_rows:
        cs = cells(row)
        parts.append("<tr>" + "".join(
            f"<td>{_inline_md(c)}</td>" for c in cs) + "</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _md_section_to_html(body: str) -> str:
    """把一只股的小节 Markdown 转成 HTML 片段（不含 <html> 外壳）。

    支持 DSA 实际用到的结构：### 标题、> 引用、- 无序列表、
    1. 有序列表、GFM 表格、--- 分隔线、段落；行内支持
    **加粗** / *斜体* / `代码` / [链接](url)。
    """
    lines = body.splitlines()
    out: list[str] = []
    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + " ".join(_inline_md(l) for l in para) + "</p>")
            para.clear()

    i, n = 0, len(lines)
    while i < n:
        line = lines[i].strip()
        if not line:
            flush_para()
            i += 1
            continue
        if re.fullmatch(r"---+|___+|\*\*\*+", line):  # 分隔线
            flush_para()
            out.append("<hr>")
            i += 1
            continue
        m = re.match(r"(#{1,6})\s+(.*)$", line)  # 标题
        if m:
            flush_para()
            level = len(m.group(1))
            out.append(f"<h{level}>" + _inline_md(m.group(2).strip()) + f"</h{level}>")
            i += 1
            continue
        if line.startswith(">"):  # 引用：连续的 > 行合成一个 blockquote
            flush_para()
            quotes = []
            while i < n and lines[i].strip().startswith(">"):
                quotes.append(lines[i].strip()[1:].strip())
                i += 1
            out.append("<blockquote>" + "<br>".join(_inline_md(q) for q in quotes) + "</blockquote>")
            continue
        if line.startswith("|"):  # 表格：连续的 | 行
            flush_para()
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            out.append(_table_to_html(rows))
            continue
        if line.startswith("- ") or line.startswith("* "):  # 无序列表
            flush_para()
            items = []
            while i < n:
                l = lines[i].strip()
                if l.startswith("- ") or l.startswith("* "):
                    items.append(l[2:].strip())
                else:
                    break
                i += 1
            out.append("<ul>" + "".join("<li>" + _inline_md(t) + "</li>" for t in items) + "</ul>")
            continue
        m = re.match(r"\d+[.)]\s+(.*)$", line)  # 有序列表
        if m:
            flush_para()
            items = []
            while i < n:
                m2 = re.match(r"\d+[.)]\s+(.*)$", lines[i].strip())
                if not m2:
                    break
                items.append(m2.group(1).strip())
                i += 1
            out.append("<ol>" + "".join("<li>" + _inline_md(t) + "</li>" for t in items) + "</ol>")
            continue
        para.append(line)
        i += 1
    flush_para()
    return "\n".join(out)


_HTML_CSS = """
body{margin:0;background:#f4f4f2;color:#222;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.7}
main{max-width:860px;margin:24px auto;background:#fff;padding:28px 32px;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
h1{font-size:24px;margin:0 0 4px}
h2{font-size:20px;margin:28px 0 10px;border-bottom:1px solid #eee;padding-bottom:6px}
h3{font-size:17px;margin:22px 0 8px}
p{margin:8px 0}
.meta{color:#666;font-size:14px;margin:0 0 8px}
blockquote{margin:12px 0;padding:10px 14px;border-left:4px solid #2f7de1;background:#f2f7fe;color:#333}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:14px}
th,td{border:1px solid #ddd;padding:7px 10px;text-align:left;vertical-align:top}
th{background:#f7f7f7}
ul,ol{margin:8px 0;padding-left:22px}
li{margin:4px 0}
hr{border:none;border-top:1px solid #e5e5e5;margin:24px 0}
code{background:#f1f1f1;padding:1px 5px;border-radius:3px;font-size:.9em}
footer{margin-top:28px;padding-top:12px;border-top:1px solid #eee;color:#999;font-size:13px}
"""


def _stock_html_page(code: str, name: str, date: str,
                     entry: dict | None, body_html: str) -> str:
    """单只股研报的完整独立 HTML 页（内联样式，无外部资源）。"""
    meta = []
    if entry is not None:
        if entry.get("score") is not None:
            meta.append(f"评分 {entry['score']}")
        if entry.get("advice"):
            meta.append(f"建议：{entry['advice']}")
    meta.append(date)
    title = f"{name} ({code})"
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{html_lib.escape(title)} 研报 · {html_lib.escape(date)}</title>\n"
        f"<style>{_HTML_CSS}</style>\n</head>\n<body>\n<main>\n"
        f"<h1>{html_lib.escape(title)}</h1>\n"
        f"<p class=\"meta\">{html_lib.escape(' · '.join(meta))}</p>\n"
        f"{body_html}\n"
        f"<footer>由 quant-platform 从 DSA 研报生成 · {html_lib.escape(date)}</footer>\n"
        "</main>\n</body>\n</html>\n"
    )


def extract(reports_dir: Path, out_dir: Path, watchlist: list[str]) -> dict:
    """拷贝 + 解析，返回要写进 dsa.json 的内容。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    if reports_dir.is_dir():
        for item in reports_dir.iterdir():
            if item.is_file():
                shutil.copy2(item, out_dir / item.name)
                copied += 1
    log.info("已拷贝 %d 个报告文件到 %s", copied, out_dir)

    reports: dict[str, dict] = {}
    market_review = ""
    if out_dir.is_dir():
        # 注意：out_dir 里可能已有上一次运行写出的 dsa.json（本脚本自己
        # 的输出），它不是上游报告——必须跳过，否则 _looks_like_report
        # 会从它的 watchlist 里抓到第一只代码，造出一只有名无实的空
        # 研报（2026-11-27 实测：002242 空占位就是这么来的）。
        for path in sorted(out_dir.glob("*.json")):
            if path.name == "dsa.json":
                continue
            data = load_json(path)
            if data is None:
                continue
            if isinstance(data, dict) and "reports" in data and "market_review" in data:
                continue  # 同上：本脚本自己的输出格式，不是上游报告
            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                parsed = _looks_like_report(item)
                if parsed is None:
                    continue
                code = parsed["code"]
                parsed["html"] = None  # 下面按同名 HTML 补
                reports[code] = parsed
        # Markdown 兜底：DSA 实际写的是 report_*.md / market_review_*.md，
        # 不是 JSON。JSON 已解析出的只补空字段，md 新增的直接收进来。
        md_source: dict[str, str] = {}  # code -> md 文件名（找同名 HTML 用）
        for path in sorted(out_dir.glob("report_*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                log.warning("读 Markdown 失败 %s：%s", path, exc)
                continue
            for code, parsed in _parse_report_md(text).items():
                parsed["html"] = None
                if code in reports:
                    existing = reports[code]
                    for key in ("score", "advice", "summary", "name"):
                        if not existing.get(key) and parsed.get(key):
                            existing[key] = parsed[key]
                else:
                    reports[code] = parsed
                    md_source[code] = path.stem
            # 每只股的小节单独写成 HTML 页，前端“看完整报告”链过去；
            # html 字段直接写 data/reports/<日期>/<代码>.html。
            sections = _split_stock_sections(text)
            for code, (name, body) in sections.items():
                entry = reports.get(code)
                page = _stock_html_page(code, name, out_dir.name, entry,
                                        _md_section_to_html(body))
                (out_dir / f"{code}.html").write_text(page, encoding="utf-8")
                if entry is not None:
                    entry["html"] = f"data/reports/{out_dir.name}/{code}.html"
            if sections:
                log.info("已生成 %d 个单股 HTML 研报（%s）", len(sections), path.name)
        # 给每只研报找同名 HTML（有就链过去）
        htmls = {p.stem: p.name for p in out_dir.glob("*.html")}
        for code, item in reports.items():
            if item.get("html"):
                continue
            for stem, name in htmls.items():
                if code in stem or stem == md_source.get(code):
                    item["html"] = f"data/reports/{out_dir.name}/{name}"
                    break
        # 大盘复盘：找名字带 market/review 的 json 或 html
        for path in sorted(out_dir.glob("*market*.json")) + sorted(out_dir.glob("*review*.json")):
            data = load_json(path)
            if isinstance(data, dict):
                for key in ("review", "summary", "content", "text"):
                    value = data.get(key)
                    if isinstance(value, str) and len(value.strip()) > 20:
                        market_review = value.strip()
                        break
            if market_review:
                break
        # JSON 里没有复盘时，再读 market_review_*.md
        if not market_review:
            for path in sorted(out_dir.glob("market_review_*.md")):
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError as exc:
                    log.warning("读 Markdown 失败 %s：%s", path, exc)
                    continue
                cleaned = _md_to_text(text)
                if len(cleaned) > 20:
                    market_review = cleaned
                    break

    result = {
        "reports": reports,
        "watchlist": watchlist,
        "market_review": market_review,
        "files_copied": copied,
    }
    save_json(out_dir / "dsa.json", result)
    log.info("解析出 %d 只结构化研报", len(reports))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="解析 DSA 报告产物")
    parser.add_argument("--reports-dir", required=True, help="DSA 的 reports/ 目录")
    parser.add_argument("--out-dir", required=True, help="输出目录 docs/data/reports/<日期>")
    parser.add_argument("--watchlist", default="", help="当日自选股，逗号分隔")
    args = parser.parse_args()

    watchlist = [c.strip() for c in args.watchlist.split(",") if c.strip()]
    extract(Path(args.reports_dir), Path(args.out_dir), watchlist)


if __name__ == "__main__":
    main()
