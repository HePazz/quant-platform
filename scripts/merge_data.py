# -*- coding: utf-8 -*-
"""把各 job 的产物合并成前端要读的数据。

输入（由两个每日 job 写到 docs/data/ 下）：
  docs/data/picks/<YYYY-MM-DD>/report.json   AlphaSift daily-report 的 DailyReport JSON
  docs/data/picks/<YYYY-MM-DD>/runs.json     AlphaSift 各策略 ScreenResult 汇总（可选）
  docs/data/reports/<YYYY-MM-DD>/           DSA 的 reports/ 原样拷贝（HTML 为主）
  docs/data/reports/<YYYY-MM-DD>/dsa.json   extract_dsa.py 解析出的结构化研报（可选）

输出：
  docs/data/latest.json    前端首页唯一要读的文件
  docs/data/history.json   日期索引（前端做历史切换用）
  docs/data/days/<YYYY-MM-DD>.json  当日快照归档

合并是幂等的：哪个 job 先跑、哪个后跑，结果一样；没跑的 job 对应段落
就是 null，前端会显示"暂无数据"，不会报错。

latest.json 的 schema（schema_version=1）：
  {
    "schema_version": 1,
    "generated_at": "2026-11-27T19:15:00+08:00",
    "trade_date": "2026-11-27",
    "sample": false,               # true 表示这是仓库自带的示例数据，还没跑过
    "watchlist": ["002242", ...],  # 当日分析用的自选股（由分析 job 写入）
    "picks": {                     # AlphaSift DailyReport 的精简版
      "trade_date": ..., "stale": bool, "ai_brief": "...",
      "market_regime": {...}, "announcement_risks": [...],
      "strategies": [{"strategy": "dual_low", "picks": [
        {"rank":1,"code":"601997","name":"贵阳银行","final_score":78,
         "screen_score":80,"risk_level":"中","risk_flags":[],
         "industry":"银行","concepts":[],"ranking_reason":"..."}]}]
    },
    "reports": {                   # DSA 结构化研报（有才有）
      "002242": {"code":...,"name":...,"score":null,"advice":"...",
                 "summary":"...","html":"data/reports/2026-11-27/xxx.html"}
    },
    "market": {"review": "..."},   # 大盘复盘文字（有才有）
    "track_record": {...},         # evaluate_track.py 写的结果（T+5/T+10/T+20）
    "position": {...},             # position.py 写的结果
    "backtest": {...},             # backtest.py 写的结果
    "factors": {...},              # factors.py 写的结果
  }

用法：
  python scripts/merge_data.py [--date YYYY-MM-DD] [--data-dir docs/data]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import load_json, now_cst_iso, repo_root, save_json, setup_logging, today_cst

log = setup_logging("merge_data")

# 前端读 latest.json 时，先看这个字段就知道数据新不新鲜
STALE_AFTER_HOURS = 26


def _pick_fields(pick: dict) -> dict:
    """只留前端要显示的字段，多余的扔掉，文件小一点。"""
    return {
        "rank": pick.get("rank"),
        "code": str(pick.get("code", "")).strip(),
        "name": pick.get("name", ""),
        "final_score": pick.get("final_score"),
        "screen_score": pick.get("screen_score"),
        "risk_level": pick.get("risk_level", ""),
        "risk_flags": pick.get("risk_flags") or [],
        "industry": pick.get("industry", ""),
        "concepts": pick.get("concepts") or [],
        "ranking_reason": pick.get("ranking_reason", ""),
        "llm_ranked": bool(pick.get("llm_ranked")),
    }


def _summarise_picks(report: dict | None) -> dict | None:
    """把 AlphaSift DailyReport 精简成前端要的形状。"""
    if not isinstance(report, dict):
        return None
    strategies = []
    for strat in report.get("strategies") or []:
        if not isinstance(strat, dict):
            continue
        picks = [_pick_fields(p) for p in (strat.get("picks") or []) if isinstance(p, dict)]
        strategies.append(
            {
                "strategy": strat.get("strategy", ""),
                "market": strat.get("market", "cn"),
                "picks": picks,
            }
        )
    return {
        "trade_date": report.get("trade_date") or report.get("expected_trade_date"),
        "stale": bool(report.get("pack_is_stale")),
        "stale_reason": report.get("stale_reason", ""),
        "ai_brief": report.get("ai_brief", ""),
        "market_regime": report.get("market_regime") or {},
        "announcement_risks": report.get("announcement_risks") or [],
        "strategies": strategies,
    }


def merge(data_dir: Path, trade_date: str) -> dict:
    """合并当日产物，返回 latest.json 的内容（不含 track/position 等段落，
    那些段落由各自的脚本原地更新——merge 只负责 picks/reports/market/watchlist，
    绝不覆盖别的脚本已经写好的段落）。"""
    picks_dir = data_dir / "picks" / trade_date
    reports_dir = data_dir / "reports" / trade_date

    latest_path = data_dir / "latest.json"
    latest = load_json(latest_path, {}) or {}
    if not isinstance(latest, dict):
        latest = {}

    # --- AlphaSift 选股 ---
    report = load_json(picks_dir / "report.json")
    picks = _summarise_picks(report)
    if picks is not None:
        latest["picks"] = picks
        log.info("已并入选股：%s，%d 个策略", trade_date, len(picks["strategies"]))
    else:
        log.info("当日无选股产物（%s），保留旧值", picks_dir)

    # --- DSA 研报 ---
    dsa = load_json(reports_dir / "dsa.json")
    if isinstance(dsa, dict) and dsa.get("reports"):
        latest["reports"] = dsa["reports"]
        if dsa.get("watchlist"):
            latest["watchlist"] = dsa["watchlist"]
        if dsa.get("market_review"):
            latest["market"] = {"review": dsa["market_review"]}
        log.info("已并入研报：%d 只", len(dsa["reports"]))
    else:
        # 没解析出结构化数据，但 HTML 报告可能在：给前端留一个目录提示
        htmls = sorted(p.name for p in reports_dir.glob("*.html")) if reports_dir.is_dir() else []
        if htmls and not latest.get("reports"):
            latest["reports"] = {}
            log.info("研报只有 HTML（%d 个），未解析结构化数据", len(htmls))
        else:
            log.info("当日无研报产物（%s），保留旧值", reports_dir)

    latest["schema_version"] = 1
    latest["trade_date"] = trade_date
    latest["generated_at"] = now_cst_iso()
    latest["sample"] = False
    return latest


def update_history(data_dir: Path, latest: dict) -> None:
    """把当日快照归档，并更新日期索引。"""
    trade_date = latest["trade_date"]
    days_dir = data_dir / "days"
    save_json(days_dir / f"{trade_date}.json", latest)
    history = load_json(data_dir / "history.json", {}) or {}
    entries = history.get("dates") or []
    if trade_date not in entries:
        entries.append(trade_date)
    history["dates"] = sorted(entries)
    history["latest"] = trade_date
    history["updated_at"] = now_cst_iso()
    save_json(data_dir / "history.json", history)


def main() -> None:
    parser = argparse.ArgumentParser(description="合并当日产物为前端数据")
    parser.add_argument("--date", default="", help="交易日 YYYY-MM-DD，默认今天（北京时间）")
    parser.add_argument("--data-dir", default="", help="数据目录，默认 docs/data")
    args = parser.parse_args()

    root = repo_root()
    data_dir = Path(args.data_dir) if args.data_dir else root / "docs" / "data"
    trade_date = args.date or today_cst()

    latest = merge(data_dir, trade_date)
    save_json(data_dir / "latest.json", latest)
    update_history(data_dir, latest)
    log.info("已写 %s（trade_date=%s）", data_dir / "latest.json", trade_date)


if __name__ == "__main__":
    main()
