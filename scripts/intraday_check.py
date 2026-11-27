# -*- coding: utf-8 -*-
"""收盘前尾盘检查：14:30（北京时间）用盘中行情重算一次自选股。

只做规则提醒，不跑 LLM（DSA 的 AI 分析只在晚上 18:00 跑）。
输入：
  - 自选股：--watchlist 或环境变量 STOCK_LIST（逗号分隔，如 002242,600519,000625）
  - 盘中行情：腾讯免费行情接口 qt.gtimg.cn（不用 Key）
  - 上次研报：docs/data/reports/ 下最近一个日期的 dsa.json
输出：
  docs/data/intraday/<YYYY-MM-DD>.json（前端接线后由 merge_data.py 并进 latest.json）

提醒规则（只用现价 + 今日涨跌 + 上次研报的建议/评分，不编基本面）：
  - 每只一句话：昨晚研报建议（评分）＋ 今日涨跌 ＋ 现价；
  - |涨跌| >= 3% 加一句「今日大涨/大跌超 3%，注意波动」；
  - 建议是减仓且今日上涨 → 「上涨是减仓窗口」；
    建议是减仓且今日下跌 → 「下跌中，减仓等反抽」；
  - 没有上次研报 → 只写盘面，并注明「暂无研报建议」；
  - 行情取不到 → 该只记一条 error，不影响其他只；
  - 休市日（行情拿到了，但时间戳不是今天）→ 写空结果，退出 0，不让 workflow 变红；
  - 整批行情拉不到（网络问题）→ 写带 error 的结果，market_open 为 null，
    不报「休市」，退出 0。

用法：
  python scripts/intraday_check.py [--watchlist 002242,600519,000625] [--date YYYY-MM-DD] [--data-dir docs/data]
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from common import load_json, now_cst_iso, repo_root, save_json, setup_logging, today_cst

log = setup_logging("intraday_check")

TENCENT_URL = "https://qt.gtimg.cn/q={codes}"
QUOTE_TIMEOUT = 30
QUOTE_RETRIES = 1
BIG_MOVE_PCT = 3.0


def _tencent_symbol(code: str) -> str:
    """A 股代码转腾讯行情前缀：6 开头上交所，其余深交所。"""
    return ("sh" if code.startswith("6") else "sz") + code


def _parse_quote_line(line: str) -> dict | None:
    """解析一行 v_sz002242="51~九阳股份~002242~44.29~41.90~42.82~...~20261127134315~2.39~5.70~44.47~40.60~..."。

    字段（按 ~ 切）：0 市场，1 名称，2 代码，3 现价，4 昨收，5 今开，
    ……，时间戳（14 位数字 YYYYMMDDHHMMSS），随后是涨跌额、涨跌幅、最高、最低。
    涨跌幅优先读时间戳后第 2 个字段，读不到就用（现价-昨收）/昨收算。
    """
    m = re.match(r'v_\w+="([^"]*)";?\s*$', line.strip())
    if not m:
        return None
    fields = m.group(1).split("~")
    if len(fields) < 6:
        return None
    name = fields[1]
    code = fields[2]
    try:
        price = float(fields[3])
        prev_close = float(fields[4])
    except (TypeError, ValueError):
        return None
    change_pct = None
    quote_time = None
    for i, f in enumerate(fields):
        if re.fullmatch(r"\d{14}", f or ""):
            quote_time = f"{f[8:10]}:{f[10:12]}"
            try:
                change_pct = float(fields[i + 2])
            except (IndexError, TypeError, ValueError):
                change_pct = None
            break
    if change_pct is None and prev_close:
        change_pct = round((price - prev_close) / prev_close * 100, 2)
    if change_pct is None:
        return None
    return {
        "code": code,
        "name": name,
        "price": price,
        "prev_close": prev_close,
        "change_pct": round(change_pct, 2),
        "quote_time": quote_time,
        "quote_date": fields[i][:8] if quote_time else None,
    }


def fetch_quotes(codes: list[str]) -> dict[str, dict]:
    """一次请求拉全部自选股的盘中行情。返回 code -> 行情 dict；
    整次请求失败或某只解析失败时，该只记 error，不抛异常。
    网络超时重试一次（腾讯接口偶尔慢，15 分钟的 step 够等）。"""
    symbols = ",".join(_tencent_symbol(c) for c in codes)
    text = None
    last_exc: Exception | None = None
    for attempt in range(QUOTE_RETRIES + 1):
        try:
            req = urllib.request.Request(
                TENCENT_URL.format(codes=symbols),
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=QUOTE_TIMEOUT) as resp:
                text = resp.read().decode("gbk", errors="ignore")
            break
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_exc = exc
            log.warning("拉取腾讯行情失败（第 %d 次）：%s", attempt + 1, exc)
            if attempt < QUOTE_RETRIES:
                import time

                time.sleep(5)
    if text is None:
        return {c: {"error": f"行情拉取失败：{last_exc}"} for c in codes}
    quotes: dict[str, dict] = {}
    for line in text.splitlines():
        parsed = _parse_quote_line(line)
        if parsed is not None:
            quotes[parsed["code"]] = parsed
    for code in codes:
        if code not in quotes:
            quotes[code] = {"error": "行情无返回（可能停牌或代码有误）"}
    return quotes


def latest_dsa_reports(data_dir: Path) -> tuple[str | None, dict]:
    """读 docs/data/reports/ 下最近一个日期目录的 dsa.json。
    返回 (报告日期, {code: {name, score, advice, summary}})，没有则 (None, {})。"""
    reports_dir = data_dir / "reports"
    if not reports_dir.is_dir():
        return None, {}
    for day_dir in sorted(reports_dir.iterdir(), reverse=True):
        if not day_dir.is_dir():
            continue
        data = load_json(day_dir / "dsa.json")
        if isinstance(data, dict) and isinstance(data.get("reports"), dict):
            return day_dir.name, data["reports"]
    return None, {}


def parse_watchlist(arg: str | None) -> list[str]:
    """--watchlist 优先，其次环境变量 STOCK_LIST；只要 6 位数字代码。"""
    raw = arg or ""
    if not raw:
        import os

        raw = os.environ.get("STOCK_LIST", "")
    codes = []
    for part in raw.replace("，", ",").split(","):
        code = part.strip()
        if re.fullmatch(r"\d{6}", code) and code not in codes:
            codes.append(code)
    return codes


def _pct_text(pct: float) -> str:
    return f"{'+' if pct >= 0 else ''}{pct:.2f}%"


def build_reminder(
    code: str, quote: dict, report: dict | None
) -> tuple[str, list[str]]:
    """按规则拼一只股的提醒句。返回 (提醒文本, flags)。只用现价、涨跌、
    上次研报的建议/评分/一句话结论，不编任何基本面或新判断。"""
    flags: list[str] = []
    if "error" in quote:
        return f"今日行情未取到（{quote['error']}），按昨晚研报执行。", ["行情缺失"]
    price = quote["price"]
    pct = quote["change_pct"]
    direction = "上涨" if pct > 0 else ("下跌" if pct < 0 else "平盘")
    if abs(pct) >= BIG_MOVE_PCT:
        flags.append(f"今日{direction}超 3%")
    if report is None:
        text = f"暂无研报建议；今日{direction}{_pct_text(pct)}，现价 {price} 元。"
    else:
        advice = report.get("advice") or ""
        score = report.get("score")
        score_text = f"（{score} 分）" if isinstance(score, (int, float)) else ""
        text = f"昨晚研报：{advice}{score_text}；今日{direction}{_pct_text(pct)}，现价 {price} 元。"
        if "减仓" in advice and pct > 0:
            text += "上涨是减仓窗口。"
        elif "减仓" in advice and pct < 0:
            text += "下跌中，减仓等反抽。"
    if flags:
        text += f"今日{direction}超 3%，注意波动。"
    return text, flags


def check(
    data_dir: Path, date: str, watchlist: list[str]
) -> dict:
    """跑一次尾盘检查，返回要写进 intraday/<date>.json 的内容。"""
    report_date, reports = latest_dsa_reports(data_dir)
    if report_date:
        log.info("上次研报：%s（%d 只）", report_date, len(reports))
    else:
        log.info("没有找到研报 dsa.json，本次只按盘面提醒")
    quotes = fetch_quotes(watchlist)
    fetched = [q for q in quotes.values() if "error" not in q]
    if not fetched:
        # 整批行情都没拿到：网络问题，不是休市——不能报「休市」，
        # 写带 error 的结果，提醒按昨晚研报执行，退出 0。
        log.warning("行情整批拉取失败，无法判断是否开市")
        stocks = []
        for code in watchlist:
            report = reports.get(code)
            reminder, flags = build_reminder(code, quotes[code], report)
            stocks.append(
                {
                    "code": code,
                    "name": (report or {}).get("name") or "",
                    "price": None,
                    "change_pct": None,
                    "quote_time": None,
                    "last_advice": (report or {}).get("advice") or "",
                    "last_score": (report or {}).get("score"),
                    "last_summary": (report or {}).get("summary") or "",
                    "reminder": reminder,
                    "flags": flags,
                    "error": quotes[code].get("error", ""),
                }
            )
        return {
            "date": date,
            "generated_at": now_cst_iso(),
            "market_open": None,
            "report_date": report_date,
            "stocks": stocks,
            "summary": "行情拉取失败，无法判断今日盘面；按昨晚研报执行。",
        }
    # 休市判断：行情拿到了，但时间戳都不是今天 → 今天没交易
    stamped_today = any(
        q.get("quote_date") == date.replace("-", "") for q in fetched
    )
    if not stamped_today:
        log.info("今日无行情时间戳（休市日），写空结果")
        return {
            "date": date,
            "generated_at": now_cst_iso(),
            "market_open": False,
            "report_date": report_date,
            "stocks": [],
            "summary": "今日休市（无交易），无尾盘检查。",
        }
    stocks = []
    for code in watchlist:
        quote = quotes.get(code, {"error": "行情无返回"})
        report = reports.get(code)
        reminder, flags = build_reminder(code, quote, report)
        stocks.append(
            {
                "code": code,
                "name": quote.get("name") or (report or {}).get("name") or "",
                "price": quote.get("price"),
                "change_pct": quote.get("change_pct"),
                "quote_time": quote.get("quote_time"),
                "last_advice": (report or {}).get("advice") or "",
                "last_score": (report or {}).get("score"),
                "last_summary": (report or {}).get("summary") or "",
                "reminder": reminder,
                "flags": flags,
                "error": quote.get("error", ""),
            }
        )
    n_reduce_up = sum(
        1
        for s in stocks
        if "减仓" in (s["last_advice"] or "")
        and isinstance(s["change_pct"], (int, float))
        and s["change_pct"] > 0
    )
    n_big = sum(1 for s in stocks if s["flags"])
    n_no_report = sum(1 for s in stocks if not s["last_advice"])
    n_no_quote = sum(1 for s in stocks if s["error"])
    parts = [f"今日 {len(stocks)} 只自选股"]
    if n_reduce_up:
        parts.append(f"{n_reduce_up} 只昨晚建议减仓且今日上涨，收盘前可按研报执行减仓")
    if n_big:
        parts.append(f"{n_big} 只波动超 3%")
    if n_no_report:
        parts.append(f"{n_no_report} 只暂无研报")
    if n_no_quote:
        parts.append(f"{n_no_quote} 只行情未取到")
    summary = "；".join(parts) + "。"
    return {
        "date": date,
        "generated_at": now_cst_iso(),
        "market_open": True,
        "report_date": report_date,
        "stocks": stocks,
        "summary": summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="收盘前尾盘检查：14:30 用盘中行情重算一次自选股，输出提醒。"
    )
    parser.add_argument(
        "--watchlist",
        default="",
        help="自选股代码，逗号分隔（默认读环境变量 STOCK_LIST）",
    )
    parser.add_argument(
        "--date", default="", help="交易日 YYYY-MM-DD，默认今天（北京时间）"
    )
    parser.add_argument(
        "--data-dir", default="", help="数据目录，默认 docs/data"
    )
    args = parser.parse_args()

    date = args.date or today_cst()
    data_dir = Path(args.data_dir) if args.data_dir else repo_root() / "docs" / "data"
    # 自选股来源优先级：--watchlist > 环境变量 STOCK_LIST > latest.json 的 watchlist
    watchlist = parse_watchlist(args.watchlist)
    if not watchlist:
        latest = load_json(data_dir / "latest.json", {}) or {}
        watchlist = [
            c
            for c in (latest.get("watchlist") or [])
            if isinstance(c, str) and re.fullmatch(r"\d{6}", c)
        ]
        if watchlist:
            log.info("未传自选股，改用 latest.json 的 watchlist：%s", watchlist)
    if not watchlist:
        # 自选股为空是配置问题（STOCK_LIST 没传、latest.json 也没有），不是休市，报错退出
        print(
            "ERROR: 自选股为空，请传 --watchlist 或设置 STOCK_LIST 环境变量",
            flush=True,
        )
        raise SystemExit(1)

    result = check(data_dir, date, watchlist)
    out_path = data_dir / "intraday" / f"{date}.json"
    save_json(out_path, result)
    log.info("已写 %s（market_open=%s）", out_path, result["market_open"])
    log.info("小结：%s", result["summary"])


if __name__ == "__main__":
    main()
