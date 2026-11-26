# -*- coding: utf-8 -*-
"""风控层（视频 1.7 节）：把硬性风控线算成每只股的具体价位。

视频里的线：
  止损 8% / 账户回撤 15% / 日内亏损 2% / 单只最大亏损 5% / 跟随止盈 5%

本脚本只做“算价位 + 出检查清单”，不做任何自动操作：
  - 每只入选股：按入场参考价（次日开盘参考价——取自选股当日的收盘价，
    没有就取最新价）算出止损价（-8%）和跟随止盈触发价（+5%）；
  - 账户级三条线（回撤 15%、日内亏损 2%、单只亏损 5%）没法在 Actions 里
    算——因为不知道用户真实持仓和账户——所以输出成检查清单，由用户在
    券商 App 里自己对照，脚本不编数字。

入场参考价来源：latest.json 里 picks 的 pick 自带 daily 数据时用收盘价，
否则只出百分比、不出绝对价（不编价格）。

用法：
  python scripts/risk.py --data-dir docs/data [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from common import load_json, repo_root, save_json, setup_logging, today_cst

log = setup_logging("risk")

ACCOUNT_CHECKS = [
    {"line": "账户回撤", "limit_pct": 15.0, "action": "账户从高点回撤 15% 就停手，全部检查一遍再决定"},
    {"line": "日内亏损", "limit_pct": 2.0, "action": "当天亏到账户 2% 就收手，不加仓、不报复交易"},
    {"line": "单只最大亏损", "limit_pct": 5.0, "action": "单只股浮亏到成本 5% 就走，不补仓摊平"},
]


def compute(data_dir: Path, trade_date: str) -> dict:
    root = repo_root()
    cfg = {}
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    risk_cfg = {"stop_loss_pct": 8.0, "trailing_take_profit_pct": 5.0}
    if yaml is not None:
        path = root / "config" / "strategy.yaml"
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                if isinstance(loaded.get("risk"), dict):
                    risk_cfg.update(loaded["risk"])
            except Exception as exc:
                log.warning("读 strategy.yaml 失败，用默认值：%s", exc)

    latest = load_json(data_dir / "latest.json", {}) or {}
    picks = (latest.get("picks") or {}).get("strategies") or []

    per_stock: dict[str, dict] = {}
    for strat in picks:
        for pick in strat.get("picks") or []:
            code = str(pick.get("code", "")).strip()
            if not code or code in per_stock:
                continue
            # 入场参考价：优先用 pick 自带的收盘价字段（不同版本字段名不一样）
            ref_price = None
            for key in ("close", "price", "last_price", "entry_price"):
                value = pick.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    ref_price = float(value)
                    break
            stop_pct = float(risk_cfg["stop_loss_pct"])
            tp_pct = float(risk_cfg["trailing_take_profit_pct"])
            item = {
                "stop_loss_pct": stop_pct,
                "trailing_take_profit_pct": tp_pct,
                "reference_price": ref_price,
                "stop_loss_price": round(ref_price * (1 - stop_pct / 100), 2) if ref_price else None,
                "take_profit_trigger_price": round(ref_price * (1 + tp_pct / 100), 2) if ref_price else None,
            }
            per_stock[code] = item

    result = {
        "trade_date": trade_date,
        "per_stock": per_stock,
        "account_checks": ACCOUNT_CHECKS,
        "note": "百分比线是硬性的；绝对价位只有拿到入场参考价时才算，没有就不编。",
    }
    latest["risk"] = result
    save_json(data_dir / "latest.json", latest)
    log.info("风控已写：%d 只（trade_date=%s）", len(per_stock), trade_date)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="风控线计算")
    parser.add_argument("--data-dir", default="", help="数据目录，默认 docs/data")
    parser.add_argument("--date", default="", help="交易日，默认今天")
    args = parser.parse_args()

    root = repo_root()
    data_dir = Path(args.data_dir) if args.data_dir else root / "docs" / "data"
    compute(data_dir, args.date or today_cst())


if __name__ == "__main__":
    main()
