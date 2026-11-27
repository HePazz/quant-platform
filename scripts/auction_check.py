# -*- coding: utf-8 -*-
"""开盘竞价复核：09:21（北京时间）用集合竞价状态复核昨晚的选股。

背景：AlphaSift 每天收盘后筛出次日选股，写在 docs/data/picks/<日期>/summary.json；
次日 09:15–09:25 是集合竞价，价格会动——09:20 之后不能撤单，但仍可下买单，
09:25 定开盘价，09:30 开始连续竞价。本脚本在 09:21 触发（workflow 定时），
用当时的竞价状态给每只昨晚的选股一个结论，提交后约 09:23 能在页面上看到，
还剩约 2 分钟竞价时间可以下单。

只做规则判断，不跑 LLM。输入：
  - 昨晚选股：docs/data/picks/ 下最近一个日期的 summary.json
    （latest.cn[]，每只 pick 有 rank/code/name/industry/price/final_score）
  - 竞价行情：腾讯免费行情接口 qt.gtimg.cn（不用 Key），
    09:21 时返回的是指示性竞价（撮合价、竞价量、昨收）
  - 5 日均量：新浪免费日 K（CN_MarketDataService.getKLineData），
    只用来算量比的分母
输出：
  docs/data/auction/<YYYY-MM-DD>.json（前端接线后由 merge_data.py 并进 latest.json）

结论规则（与用户约定的阈值，一个字不改）：
  - 涨幅 > +5%            → 高开勿追（开盘涨幅已吃掉预期）
  - +3% < 涨幅 ≤ +5%      → 谨慎，减半
  - -2% ≤ 涨幅 ≤ +3%      → 值得入
  - 涨幅 < -2%            → 跳空走弱，谨慎（低开，先查有无利空再决定）
  量比叠加（只追加到理由里，不单独改结论；只看上涨的）：
  - 量比 > 2 且上涨 → 资金强；量比 < 1 且上涨 → 量能不足。
  量比的定义：竞价量（手）/ 5 日均量（手）再除以 24——
  竞价只有 10 分钟，一天交易 240 分钟，所以分母是“10 分钟窗口的平均量”
  （5 日均量 / 24）；再按取数时已过的竞价分钟数折算
  （09:21 时只过了 6 分钟，分母再 ×6/10），量比 1 就是到目前为止的
  平均速度，>2 是两倍速度，直接拿竞价量比全日均量会永远 <1，
  那个比法没有意义。
  理由里一定写清数字（竞价价、涨幅、量比），不编基本面。

其它约定：
  - 同一只股被多个策略选中时只留一条，取最高分，记下所有选中它的策略；
  - 行情取不到 → 该只记一条 error，不影响其他只；
  - 休市日（行情拿到了，但时间戳不是今天）→ 写空结果，退出 0；
  - 整批行情拉不到（网络问题）→ 写带 error 的结果，market_open 为 null，
    不报「休市」，退出 0；
  - 选股的交易日一定写进输出（picks_trade_date），不拿过期数据冒充当天的；
    如果选股比今天早 3 天以上，小结里会提醒一句。

用法：
  python3 scripts/auction_check.py [--date YYYY-MM-DD] [--data-dir docs/data]
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date as date_cls
from pathlib import Path

from common import load_json, now_cst_iso, repo_root, save_json, setup_logging, today_cst

log = setup_logging("auction_check")

TENCENT_URL = "https://qt.gtimg.cn/q={codes}"
SINA_KLINE_URL = (
    "https://quotes.sina.cn/cn/api/json_v2.php/"
    "CN_MarketDataService.getKLineData?symbol={symbol}&scale=240&ma=no&datalen=6"
)
QUOTE_TIMEOUT = 30
QUOTE_RETRIES = 1
KLINE_TIMEOUT = 15
KLINE_WORKERS = 8

# 结论阈值（与用户约定的，不改）
CHASE_LIMIT = 5.0    # 高于此 → 高开勿追
HALF_LIMIT = 3.0     # 高于此 → 谨慎，减半
WEAK_LIMIT = -2.0    # 低于此 → 跳空走弱，谨慎
STRONG_RATIO = 2.0   # 量比高于此且上涨 → 资金强
WEAK_RATIO = 1.0     # 量比低于此且上涨 → 量能不足

NOTE = (
    "结论按约 09:21 的集合竞价状态算出，不是 09:25 的最终开盘价；"
    "最后几分钟价格仍可能变动，下单前以实际开盘价为准，"
    "开盘价冲出阈值时仍按规则不追。"
)


def _tencent_symbol(code: str) -> str:
    """A 股代码转腾讯行情前缀：6 开头上交所，其余深交所。"""
    return ("sh" if code.startswith("6") else "sz") + code


def _sina_symbol(code: str) -> str:
    """A 股代码转新浪日 K 前缀（小写）。"""
    return ("sh" if code.startswith("6") else "sz") + code


def _parse_quote_line(line: str) -> dict | None:
    """解析一行 v_sz002242="51~九阳股份~002242~45.27~41.90~42.82~981855~...~20261127135606~3.37~8.04~..."。

    字段（按 ~ 切）：0 市场，1 名称，2 代码，3 现价（竞价时为指示性撮合价），
    4 昨收，5 今开，6 成交量（手；竞价时为已撮合量），
    ……，时间戳（14 位数字 YYYYMMDDHHMMSS），随后是涨跌额、涨跌幅。
    涨跌幅优先读时间戳后第 2 个字段，读不到就用（现价-昨收）/昨收算。
    """
    m = re.match(r'v_\w+="([^"]*)";?\s*$', line.strip())
    if not m:
        return None
    fields = m.group(1).split("~")
    if len(fields) < 7:
        return None
    name = fields[1]
    code = fields[2]
    try:
        price = float(fields[3])
        prev_close = float(fields[4])
        volume = int(float(fields[6]))
    except (TypeError, ValueError):
        return None
    change_pct = None
    quote_time = None
    quote_date = None
    for i, f in enumerate(fields):
        if re.fullmatch(r"\d{14}", f or ""):
            quote_date = f[:8]
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
        "volume": volume,
        "change_pct": round(change_pct, 2),
        "quote_time": quote_time,
        "quote_date": quote_date,
    }


def fetch_quotes(codes: list[str]) -> dict[str, dict]:
    """一次请求拉全部代码的行情（含上证指数 sh000001 也放进来一起拉）。
    返回 code -> 行情 dict；整次请求失败或某只解析失败时，该只记 error，不抛异常。"""
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


def fetch_avg5_volume_hands(code: str) -> float | None:
    """新浪日 K 取最近 5 个完整交易日的均量（手）。失败返回 None。"""
    try:
        req = urllib.request.Request(
            SINA_KLINE_URL.format(symbol=_sina_symbol(code)),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=KLINE_TIMEOUT) as resp:
            bars = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if not isinstance(bars, list) or not bars:
            return None
        vols = []
        for bar in bars[-5:]:
            try:
                vols.append(float(bar["volume"]))
            except (KeyError, TypeError, ValueError):
                continue
        if not vols:
            return None
        return sum(vols) / len(vols) / 100.0  # 股 → 手
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        log.warning("拉取 %s 日 K 失败：%s", code, exc)
        return None


def fetch_avg5_volumes(codes: list[str]) -> dict[str, float | None]:
    """并行拉多只的 5 日均量。"""
    with ThreadPoolExecutor(max_workers=KLINE_WORKERS) as pool:
        results = list(pool.map(fetch_avg5_volume_hands, codes))
    return dict(zip(codes, results))


def latest_picks_summary(data_dir: Path) -> tuple[str | None, dict | None]:
    """读 docs/data/picks/ 下最近一个日期目录的 summary.json。
    返回 (选股交易日, summary 原文)，没有则 (None, None)。
    选股交易日取各 run 的 date（都一样，如 2026-11-26）。"""
    picks_dir = data_dir / "picks"
    if not picks_dir.is_dir():
        return None, None
    for day_dir in sorted(picks_dir.iterdir(), reverse=True):
        if not day_dir.is_dir():
            continue
        data = load_json(day_dir / "summary.json")
        if isinstance(data, dict) and isinstance((data.get("latest") or {}).get("cn"), list):
            runs = data["latest"]["cn"]
            trade_date = None
            for run in runs:
                if isinstance(run, dict) and run.get("date"):
                    trade_date = run["date"]
                    break
            return trade_date or day_dir.name, data
    return None, None


def dedup_picks(summary: dict) -> list[dict]:
    """把 summary.json 里各策略的选股按代码去重：同一只取最高分，
    记下选中它的全部策略。返回 [{code, name, industry, final_score, strategies}]。"""
    best: dict[str, dict] = {}
    for run in (summary.get("latest") or {}).get("cn") or []:
        if not isinstance(run, dict):
            continue
        strategy = run.get("strategy", "")
        for p in run.get("picks") or []:
            if not isinstance(p, dict):
                continue
            code = str(p.get("code", "")).strip()
            if not re.fullmatch(r"\d{6}", code):
                continue
            score = p.get("final_score")
            score = score if isinstance(score, (int, float)) else 0.0
            cur = best.get(code)
            if cur is None:
                best[code] = {
                    "code": code,
                    "name": p.get("name", ""),
                    "industry": p.get("industry", ""),
                    "final_score": score,
                    "strategies": [strategy] if strategy else [],
                }
            else:
                if strategy and strategy not in cur["strategies"]:
                    cur["strategies"].append(strategy)
                if score > cur["final_score"]:
                    cur["final_score"] = score
                    cur["name"] = p.get("name", "") or cur["name"]
                    cur["industry"] = p.get("industry", "") or cur["industry"]
    return sorted(best.values(), key=lambda p: p["final_score"], reverse=True)


def verdict_for(change_pct: float) -> str:
    """按约定阈值给结论（边界：+5% 算谨慎减半，+3% 和 -2% 算值得入）。"""
    if change_pct > CHASE_LIMIT:
        return "高开勿追"
    if change_pct > HALF_LIMIT:
        return "谨慎，减半"
    if change_pct >= WEAK_LIMIT:
        return "值得入"
    return "跳空走弱，谨慎"


def _pct_text(pct: float) -> str:
    return f"{'+' if pct >= 0 else ''}{pct:.2f}%"


def build_reason(
    quote: dict, volume_ratio: float | None
) -> tuple[str, str]:
    """按规则拼一只股的复核理由。返回 (结论, 理由)。
    理由一定写清数字（竞价价、涨幅、量比），不编基本面。"""
    if "error" in quote:
        return "", f"竞价行情未取到（{quote['error']}），无法复核。"
    price = quote["price"]
    prev_close = quote["prev_close"]
    pct = quote["change_pct"]
    volume = quote["volume"]
    verdict = verdict_for(pct)
    reason = (
        f"竞价 {price} 元（昨收 {prev_close} 元），{_pct_text(pct)}；"
        f"竞价量 {volume} 手"
    )
    if volume_ratio is None:
        reason += "，量比未取到。"
    else:
        reason += f"，量比 {volume_ratio:.2f}。"
    if verdict == "高开勿追":
        reason += "开盘涨幅已吃掉预期，不追。"
    elif verdict == "谨慎，减半":
        reason += "高开较多，减半参与。"
    elif verdict == "值得入":
        reason += "在买入区间内。"
    else:
        reason += "低开，先查有无利空再决定。"
    if pct > 0 and volume_ratio is not None:
        if volume_ratio > STRONG_RATIO:
            reason += "资金强。"
        elif volume_ratio < WEAK_RATIO:
            reason += "量能不足。"
    return verdict, reason


def check(data_dir: Path, date: str) -> dict:
    """跑一次竞价复核，返回要写进 auction/<date>.json 的内容。"""
    trade_date, summary = latest_picks_summary(data_dir)
    if summary is None:
        log.warning("没有找到选股 summary.json，无法复核")
        return {
            "date": date,
            "generated_at": now_cst_iso(),
            "note": NOTE,
            "picks_trade_date": None,
            "market_open": None,
            "market_regime": {"index_change_pct": None, "note": "无选股数据，未复核。"},
            "picks": [],
            "summary": "没有找到昨晚的选股数据，无法复核。",
        }
    picks = dedup_picks(summary)
    log.info("昨晚选股：交易日 %s，去重后 %d 只", trade_date, len(picks))
    codes = [p["code"] for p in picks] + ["000001"]
    quotes = fetch_quotes(codes)
    fetched = [q for c, q in quotes.items() if c != "000001" and "error" not in q]
    if not fetched:
        # 整批行情都没拿到：网络问题，不是休市——不能报「休市」，退出 0。
        log.warning("行情整批拉取失败，无法判断是否开市")
        index_quote = quotes.get("000001", {})
        return {
            "date": date,
            "generated_at": now_cst_iso(),
            "note": NOTE,
            "picks_trade_date": trade_date,
            "market_open": None,
            "market_regime": {
                "index_change_pct": index_quote.get("change_pct"),
                "note": "行情拉取失败，无法判断今日盘面。",
            },
            "picks": [
                {
                    "code": p["code"],
                    "name": p["name"],
                    "industry": p["industry"],
                    "prev_close": None,
                    "auction_price": None,
                    "change_pct": None,
                    "auction_volume": None,
                    "volume_ratio": None,
                    "final_score": p["final_score"],
                    "strategies": p["strategies"],
                    "verdict": "",
                    "reason": f"竞价行情未取到（{quotes[p['code']].get('error', '')}），无法复核。",
                    "error": quotes[p["code"]].get("error", ""),
                }
                for p in picks
            ],
            "summary": "行情拉取失败，无法判断今日盘面；按昨晚选股自行看盘。",
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
            "note": NOTE,
            "picks_trade_date": trade_date,
            "market_open": False,
            "market_regime": {
                "index_change_pct": None,
                "note": "今日休市（无交易），无竞价复核。",
            },
            "picks": [],
            "summary": "今日休市（无交易），无竞价复核。",
        }
    index_quote = quotes.get("000001", {})
    index_pct = index_quote.get("change_pct")
    if isinstance(index_pct, (int, float)):
        regime_note = f"上证指数竞价{_pct_text(index_pct)}"
        if index_pct < -1:
            regime_note += "，低开超 1%，整体宜降仓或不做"
        regime_note += "。"
    else:
        regime_note = "指数行情未取到。"
    # 取数时已过的竞价分钟数（09:15 起算，夹在 1–10 分钟之间）：
    # 量比分母要按已过的时间折算，否则 09:21 只过了 6 分钟，
    # 用满 10 分钟的分母会把平均速度的股也算成“量能不足”。
    elapsed_min = 6  # 默认按 09:21 算
    for q in fetched:
        qt = q.get("quote_time") or ""
        m = re.fullmatch(r"(\d{2}):(\d{2})", qt)
        if m:
            elapsed_min = max(
                1, min(10, (int(m.group(1)) * 60 + int(m.group(2))) - (9 * 60 + 15))
            )
            break
    avg5 = fetch_avg5_volumes([p["code"] for p in picks])
    rows = []
    for p in picks:
        quote = quotes.get(p["code"], {"error": "行情无返回"})
        if "error" in quote:
            verdict, reason = build_reason(quote, None)
            rows.append(
                {
                    "code": p["code"],
                    "name": p["name"],
                    "industry": p["industry"],
                    "prev_close": None,
                    "auction_price": None,
                    "change_pct": None,
                    "auction_volume": None,
                    "volume_ratio": None,
                    "final_score": p["final_score"],
                    "strategies": p["strategies"],
                    "verdict": verdict,
                    "reason": reason,
                    "error": quote.get("error", ""),
                }
            )
            continue
        avg5_hands = avg5.get(p["code"])
        volume_ratio = None
        if isinstance(avg5_hands, (int, float)) and avg5_hands > 0:
            # 竞价 10 分钟 vs 全天 240 分钟：分母取 5 日均量 / 24，
            # 再按已过的竞价分钟数折算（09:21 时 ×6/10）
            volume_ratio = round(
                quote["volume"] / (avg5_hands / 24.0 * (elapsed_min / 10.0)), 2
            )
        verdict, reason = build_reason(quote, volume_ratio)
        rows.append(
            {
                "code": p["code"],
                "name": p["name"] or quote.get("name") or "",
                "industry": p["industry"],
                "prev_close": quote["prev_close"],
                "auction_price": quote["price"],
                "change_pct": quote["change_pct"],
                "auction_volume": quote["volume"],
                "volume_ratio": volume_ratio,
                "final_score": p["final_score"],
                "strategies": p["strategies"],
                "verdict": verdict,
                "reason": reason,
                "error": "",
            }
        )
    n_ok = sum(1 for r in rows if r["verdict"] == "值得入")
    n_chase = sum(1 for r in rows if r["verdict"] == "高开勿追")
    n_half = sum(1 for r in rows if r["verdict"] == "谨慎，减半")
    n_weak = sum(1 for r in rows if r["verdict"] == "跳空走弱，谨慎")
    n_err = sum(1 for r in rows if r["error"])
    parts = [f"昨日 {len(rows)} 只选股（交易日 {trade_date}）"]
    if n_ok:
        parts.append(f"{n_ok} 只值得入")
    if n_half:
        parts.append(f"{n_half} 只谨慎减半")
    if n_chase:
        parts.append(f"{n_chase} 只高开勿追")
    if n_weak:
        parts.append(f"{n_weak} 只跳空走弱")
    if n_err:
        parts.append(f"{n_err} 只行情未取到")
    # 选股过期提醒：交易日比今天早 3 天以上（长假后），明说一句
    try:
        gap = (date_cls.fromisoformat(date) - date_cls.fromisoformat(trade_date)).days
    except (TypeError, ValueError):
        gap = 0
    if gap > 3:
        parts.append(f"注意：选股是 {gap} 天前（{trade_date}）的，已过期")
    summary_text = "；".join(parts) + "。"
    return {
        "date": date,
        "generated_at": now_cst_iso(),
        "note": NOTE,
        "picks_trade_date": trade_date,
        "market_open": True,
        "market_regime": {"index_change_pct": index_pct, "note": regime_note},
        "picks": rows,
        "summary": summary_text,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="开盘竞价复核：09:21 用集合竞价状态复核昨晚的选股，输出结论。"
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

    result = check(data_dir, date)
    out_path = data_dir / "auction" / f"{date}.json"
    save_json(out_path, result)
    log.info("已写 %s（market_open=%s）", out_path, result["market_open"])
    log.info("小结：%s", result["summary"])


if __name__ == "__main__":
    main()
