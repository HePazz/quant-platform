# -*- coding: utf-8 -*-
"""仓位层（视频 1.6 节）：半凯利 + 上限。

公式：f* = 0.5 * (b*p - q) / b，其中 p = 胜率，q = 1-p，
b = 平均盈利 / 平均亏损（盈亏比）。
上限：单只 <5%，单笔 ≤2%（占账户比例）。

胜率和盈亏比优先用 track-record 里该策略的真实统计；没有历史时用
config/strategy.yaml 里的先验值，并在结果里写明数据来源。
Kelly 算出来是负数或零 = 数学上不该下注，返回 0 并写清原因。

这只是建议值，最终下单由人在券商 App 里决定。

用法：
  python scripts/position.py --data-dir docs/data [--date YYYY-MM-DD]
  读取 docs/data/latest.json 里的 picks + track_record，写回 position 段落。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import load_json, repo_root, save_json, setup_logging, today_cst

log = setup_logging("position")

# 读不到 yaml 库也不依赖：strategy.yaml 是手写的简单键值，用 tiny 解析
try:
    import yaml  # type: ignore
except ImportError:  # Actions 镜像里不一定有，缺了就用内置默认值
    yaml = None


def _load_strategy_config(root: Path) -> dict:
    defaults = {
        "kelly": {
            "default_win_rate": 0.45,
            "default_payoff_ratio": 1.0,
            "half_kelly": 0.5,
            "max_single_stock_pct": 5.0,
            "max_single_trade_pct": 2.0,
        },
        "risk": {
            "stop_loss_pct": 8.0,
            "trailing_take_profit_pct": 5.0,
        },
    }
    path = root / "config" / "strategy.yaml"
    if yaml is not None and path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            for section, values in defaults.items():
                if isinstance(loaded.get(section), dict):
                    values.update(loaded[section])
        except Exception as exc:  # 配置坏了也别让整个 job 炸掉
            log.warning("读 strategy.yaml 失败，用默认值：%s", exc)
    return defaults


def half_kelly(win_rate: float, payoff: float, half: float = 0.5) -> float:
    """返回建议仓位比例（0~1 的小数）。负数归零。"""
    if payoff <= 0 or not (0 < win_rate < 1):
        return 0.0
    edge = payoff * win_rate - (1 - win_rate)
    return max(0.0, half * edge / payoff)


def compute(data_dir: Path, trade_date: str) -> dict:
    """给 latest.json 里当天的每个选股算建议仓位。"""
    root = repo_root()
    cfg = _load_strategy_config(root)
    kelly = cfg["kelly"]
    risk = cfg["risk"]

    latest = load_json(data_dir / "latest.json", {}) or {}
    picks = (latest.get("picks") or {}).get("strategies") or []
    track = latest.get("track_record") or {}
    stats = track.get("strategy_stats") or {}

    out: dict[str, dict] = {}
    for strat in picks:
        name = strat.get("strategy", "")
        # 优先用该策略在 track-record 里的真实胜率/盈亏比
        st = stats.get(name) or {}
        win_rate = st.get("win_rate") if isinstance(st.get("win_rate"), (int, float)) else kelly["default_win_rate"]
        payoff = st.get("payoff_ratio") if isinstance(st.get("payoff_ratio"), (int, float)) else kelly["default_payoff_ratio"]
        source = "track-record 实测" if st else "先验默认值（无历史）"
        frac = half_kelly(float(win_rate), float(payoff), float(kelly["half_kelly"]))
        pct = min(frac * 100, float(kelly["max_single_stock_pct"]), float(kelly["max_single_trade_pct"]))
        for pick in strat.get("picks") or []:
            code = str(pick.get("code", "")).strip()
            if not code:
                continue
            out[code] = {
                "strategy": name,
                "suggested_pct": round(pct, 2),
                "win_rate": round(float(win_rate), 3),
                "payoff_ratio": round(float(payoff), 2),
                "source": source,
                "stop_loss_pct": risk["stop_loss_pct"],
                "trailing_take_profit_pct": risk["trailing_take_profit_pct"],
                "note": (
                    "半凯利为 0：期望为负，建议不下注"
                    if pct <= 0
                    else f"半凯利建议仓位 {pct:.2f}%（已按单只<{kelly['max_single_stock_pct']}%、单笔≤{kelly['max_single_trade_pct']}%封顶）"
                ),
            }
    return {"trade_date": trade_date, "positions": out}


def main() -> None:
    parser = argparse.ArgumentParser(description="半凯利仓位计算")
    parser.add_argument("--data-dir", default="", help="数据目录，默认 docs/data")
    parser.add_argument("--date", default="", help="交易日，默认今天")
    args = parser.parse_args()

    root = repo_root()
    data_dir = Path(args.data_dir) if args.data_dir else root / "docs" / "data"
    trade_date = args.date or today_cst()

    result = compute(data_dir, trade_date)
    latest_path = data_dir / "latest.json"
    latest = load_json(latest_path, {}) or {}
    latest["position"] = result
    save_json(latest_path, latest)
    log.info("仓位已写：%d 只（trade_date=%s）", len(result["positions"]), trade_date)


if __name__ == "__main__":
    main()
