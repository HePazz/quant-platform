# -*- coding: utf-8 -*-
"""回测层（视频 1.4/1.5 节）：用已提交的选股历史做简单复盘。

做法（等权、按策略汇总）：
  - 每只入选股按次日开盘价附近入场（AlphaSift 的口径是 next_open，
    并扣除一字涨停不可成交的比例——直接用它 evaluate 后的收益字段）；
  - 持有到 T+5 / T+10 / T+20，收益扣掉往返成本 0.96%；
  - 输出：累计超额收益、最大回撤（MaxDD）、夏普比率、日收益分布
    （只报均值/偏度方向，不做正态假设）。

样本不足（没有任何已实现收益）时，所有指标写 null 并注明原因，
不编造曲线。这是脚手架阶段的诚实做法：回测要等 track-record
积累几周才有意义。

用法：
  python scripts/backtest.py --data-dir docs/data [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

from common import load_json, repo_root, save_json, setup_logging, today_cst

log = setup_logging("backtest")

RETURN_FIELDS = ("t5_return", "t10_return", "t20_return", "realized_return")


def _load_strategy_config(root: Path) -> float:
    cost = 0.96
    try:
        import yaml  # type: ignore
    except ImportError:
        return cost
    path = root / "config" / "strategy.yaml"
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            value = loaded.get("cost_round_trip_pct")
            if isinstance(value, (int, float)):
                cost = float(value)
        except Exception as exc:
            log.warning("读 strategy.yaml 失败，用默认成本 0.96%%：%s", exc)
    return cost


def _collect_returns(picks_root: Path) -> list[dict]:
    """收集每只入选股的已实现收益（优先 T+10，没有就 T+5）。"""
    rows = []
    if not picks_root.is_dir():
        return rows
    for run_file in sorted(picks_root.glob("*/runs/*.json")):
        data = load_json(run_file)
        if not isinstance(data, dict):
            continue
        for pick in data.get("picks") or []:
            if not isinstance(pick, dict):
                continue
            ret = None
            for key in ("t10_return", "t5_return", "t20_return", "realized_return"):
                value = pick.get(key)
                if isinstance(value, (int, float)):
                    ret = float(value)
                    break
            if ret is None:
                continue
            rows.append(
                {
                    "date": run_file.parent.parent.name,
                    "strategy": data.get("strategy", ""),
                    "code": str(pick.get("code", "")),
                    "return_pct": ret,
                }
            )
    return rows


def compute(data_dir: Path, trade_date: str) -> dict:
    cost = _load_strategy_config(repo_root())
    rows = _collect_returns(data_dir / "picks")

    result: dict = {
        "trade_date": trade_date,
        "observations": len(rows),
        "cost_round_trip_pct": cost,
        "cumulative_net_pct": None,
        "max_drawdown_pct": None,
        "sharpe": None,
        "mean_return_pct": None,
        "note": "",
    }
    if not rows:
        result["note"] = "没有任何已实现收益数据，回测暂无结果。等 track-record 积累后再看。"
        log.info("回测：暂无数据")
    else:
        # 等权组合：按日期汇总当日平均净收益（扣成本），再连乘
        by_date: dict[str, list[float]] = {}
        for row in rows:
            by_date.setdefault(row["date"], []).append(row["return_pct"] - cost)
        dates = sorted(by_date)
        daily = [sum(by_date[d]) / len(by_date[d]) for d in dates]
        equity = 100.0
        peak = 100.0
        max_dd = 0.0
        for r in daily:
            equity *= 1 + r / 100
            peak = max(peak, equity)
            dd = (peak - equity) / peak * 100
            max_dd = max(max_dd, dd)
        mean = sum(daily) / len(daily)
        var = sum((v - mean) ** 2 for v in daily) / len(daily) if len(daily) > 1 else 0.0
        std = math.sqrt(var)
        sharpe = (mean / std * math.sqrt(250)) if std > 0 else None
        result["cumulative_net_pct"] = round(equity - 100, 2)
        result["max_drawdown_pct"] = round(max_dd, 2)
        result["sharpe"] = round(sharpe, 2) if sharpe is not None else None
        result["mean_return_pct"] = round(mean, 3)
        result["note"] = (
            f"等权复盘 {len(dates)} 个交易日、{len(rows)} 只入选股，"
            f"已扣往返成本 {cost}%；AlphaSift 口径为 next_open 并扣除一字涨停。"
        )
        log.info(
            "回测：累计净收益 %s%%，MaxDD %s%%，夏普 %s（%d 天）",
            result["cumulative_net_pct"],
            result["max_drawdown_pct"],
            result["sharpe"],
            len(dates),
        )

    latest = load_json(data_dir / "latest.json", {}) or {}
    latest["backtest"] = result
    save_json(data_dir / "latest.json", latest)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="选股历史简单回测")
    parser.add_argument("--data-dir", default="", help="数据目录，默认 docs/data")
    parser.add_argument("--date", default="", help="交易日，默认今天")
    args = parser.parse_args()

    root = repo_root()
    data_dir = Path(args.data_dir) if args.data_dir else root / "docs" / "data"
    compute(data_dir, args.date or today_cst())


if __name__ == "__main__":
    main()
