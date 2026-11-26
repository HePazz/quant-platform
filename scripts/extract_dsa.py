# -*- coding: utf-8 -*-
"""解析 DSA 的 reports/ 产物。

DSA 的 main.py 把报告写到 reports/（HTML 为主，文件名和内部结构随
上游版本变，不做强假设）。本脚本的策略：
  1. 把 reports/ 原样拷到 docs/data/reports/<日期>/，前端直接链接 HTML 看全文；
  2. 顺带找里面的 *.json（如果上游写了结构化报告），按“看起来像研报”
     的启发式规则抽出 code / name / score / advice / summary，写成
     docs/data/reports/<日期>/dsa.json 供 merge_data.py 用；
  3. 找不到 JSON 也不报错——前端照样能点开 HTML 看，只是列表里不显示
     评分和结论摘要。

用法：
  python scripts/extract_dsa.py --reports-dir vendor/dsa/reports \
      --out-dir docs/data/reports/2026-11-27 [--watchlist 002242,600519,000625]
"""
from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

from common import load_json, save_json, setup_logging

log = setup_logging("extract_dsa")

CODE_RE = re.compile(r"\b([036]\d{5})\b")


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
        for path in sorted(out_dir.glob("*.json")):
            data = load_json(path)
            if data is None:
                continue
            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                parsed = _looks_like_report(item)
                if parsed is None:
                    continue
                code = parsed["code"]
                parsed["html"] = None  # 下面按同名 HTML 补
                reports[code] = parsed
        # 给每只研报找同名 HTML（有就链过去）
        htmls = {p.stem: p.name for p in out_dir.glob("*.html")}
        for code, item in reports.items():
            for stem, name in htmls.items():
                if code in stem:
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
