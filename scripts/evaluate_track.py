# -*- coding: utf-8 -*-
"""T+5/T+10/T+20 公开跟踪验收（AlphaSift track-record 的口径）。

AlphaSift 自己的 evaluate 步骤（scripts/track_record.py evaluate）
已经按 next_open、扣除一字涨停算好了 T+N 超额收益，写进
track/site/summary.json 的 track_comparison / integrity 里。
本脚本不重新发明口径，只做两件事：
  1. 把各日期的 summary.json 读出来，汇总成前端要的 track_record 段落；
  2. 顺带从 runs/*.json 里统计各策略的胜率和盈亏比，供 position.py 用。

如果某天还没有验收数据（刚跑、还没到 T+5），那天就记“待验收”，
不把没到期的写成 0。

用法：
  python scripts/evaluate_track.py --data-dir docs/data [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from common import load_json, repo_root, save_json, setup_logging, today_cst

log = setup_logging("evaluate_track")

RETURN_FIELDS = ("t5_return", "t10_return", "t20_return")


def _strategy_stats(picks_root: Path) -> dict:
    """从 runs 里统计各策略的胜率/盈亏比（有已实现收益的才算）。"""
    by_strategy: dict[str, dict[str, list[float]]] = {}
    if picks_root.is_dir():
        for run_file in sorted(picks_root.glob("*/runs/*.json")):
            data = load_json(run_file)
            if not isinstance(data, dict):
                continue
            name = str(data.get("strategy", ""))
            bucket = by_strategy.setdefault(name, {"wins": [], "losses": []})
            for pick in data.get("picks") or []:
                if not isinstance(pick, dict):
                    continue
                ret = None
                for key in ("t10_return", "t5_return", "t20_return"):
                    value = pick.get(key)
                    if isinstance(value, (int, float)):
                        ret = float(value)
                        break
                if ret is None:
                    continue
                (bucket["wins"] if ret > 0 else bucket["losses"]).append(ret)
    stats: dict[str, dict] = {}
    for name, bucket in by_strategy.items():
        wins, losses = bucket["wins"], bucket["losses"]
        total = len(wins) + len(losses)
        if total == 0:
            continue
        win_rate = len(wins) / total
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
        payoff = (avg_win / avg_loss) if avg_loss > 0 else None
        stats[name] = {
            "observations": total,
            "win_rate": round(win_rate, 3),
            "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "payoff_ratio": round(payoff, 2) if payoff is not None else None,
        }
    return stats


def compute(data_dir: Path, trade_date: str) -> dict:
    picks_root = data_dir / "picks"
    history: list[dict] = []
    if picks_root.is_dir():
        for day_dir in sorted(picks_root.iterdir()):
            if not day_dir.is_dir():
                continue
            summary = load_json(day_dir / "summary.json")
            entry: dict = {"date": day_dir.name}
            if isinstance(summary, dict):
                entry["track_comparison"] = summary.get("track_comparison") or []
                entry["integrity"] = summary.get("integrity") or {}
                entry["artifacts"] = summary.get("artifacts") or {}
            else:
                entry["track_comparison"] = []
                entry["integrity"] = {}
                entry["artifacts"] = {}
            history.append(entry)

    result = {
        "trade_date": trade_date,
        "days": len(history),
        "history": history,
        "strategy_stats": _strategy_stats(picks_root),
        "note": (
            "T+5/T+10/T+20 按 AlphaSift 口径（next_open 入场，扣除一字涨停）验收；"
            "没到期的记待验收。刚搭起来时历史为空，属正常。"
            if history
            else "还没有任何一天的验收数据：等选股 job 跑过、且到 T+5 后才有数字。"
        ),
    }
    latest = load_json(data_dir / "latest.json", {}) or {}
    latest["track_record"] = result
    save_json(data_dir / "latest.json", latest)
    log.info("跟踪验收已写：%d 天，%d 个策略有统计", len(history), len(result["strategy_stats"]))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="T+5/T+10/T+20 跟踪验收汇总")
    parser.add_argument("--data-dir", default="", help="数据目录，默认 docs/data")
    parser.add_argument("--date", default="", help="交易日，默认今天")
    args = parser.parse_args()

    root = repo_root()
    data_dir = Path(args.data_dir) if args.data_dir else root / "docs" / "data"
    compute(data_dir, args.date or today_cst())


if __name__ == "__main__":
    main()
