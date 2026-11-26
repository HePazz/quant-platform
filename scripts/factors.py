# -*- coding: utf-8 -*-
"""因子检验层（视频 1.10 节）：IC / ICIR。

IC = 因子值（选股打分）与未来收益的秩相关系数（Spearman）。
标准：|IC| > 0.03 可用；ICIR = mean(IC) / std(IC)，ICIR > 0.5 可靠。

数据来源：docs/data/picks/<日期>/runs/ 里的 ScreenResult。
每条 run 里的 pick 如果带有已实现的未来收益字段（AlphaSift 的
evaluate 步骤写回的 t5_return / t10_return / t20_return，或
realized_return），就拿来算；没有就不算。
样本少于 config/strategy.yaml 里的 min_observations（默认 20）时，
结论写“样本不足”，不下“可用/不可靠”的判断——刚搭起来头几周必然如此。

用法：
  python scripts/factors.py --data-dir docs/data [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from common import load_json, repo_root, save_json, setup_logging, today_cst

log = setup_logging("factors")

RETURN_FIELDS = ("t5_return", "t10_return", "t20_return", "realized_return", "forward_return")


def _rank(values: list[float]) -> list[float]:
    """带并列平均的秩次（Spearman 用）。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    if den == 0:
        return None
    return num / den


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    return _pearson(_rank(xs), _rank(ys))


def _collect_observations(picks_root: Path) -> list[dict]:
    """从所有历史 run 文件里收集（打分，未来收益）对。"""
    obs = []
    if not picks_root.is_dir():
        return obs
    for run_file in sorted(picks_root.glob("*/runs/*.json")):
        data = load_json(run_file)
        if not isinstance(data, dict):
            continue
        picks = data.get("picks") or []
        for pick in picks:
            if not isinstance(pick, dict):
                continue
            score = pick.get("final_score", pick.get("screen_score"))
            if not isinstance(score, (int, float)):
                continue
            ret = None
            for key in RETURN_FIELDS:
                value = pick.get(key)
                if isinstance(value, (int, float)):
                    ret = float(value)
                    break
            if ret is None:
                continue
            obs.append(
                {
                    "date": run_file.parent.parent.name,
                    "strategy": data.get("strategy", ""),
                    "code": str(pick.get("code", "")),
                    "score": float(score),
                    "return": ret,
                }
            )
    return obs


def compute(data_dir: Path, trade_date: str) -> dict:
    cfg = {"ic_usable": 0.03, "icir_reliable": 0.5, "min_observations": 20}
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    if yaml is not None:
        path = repo_root() / "config" / "strategy.yaml"
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                if isinstance(loaded.get("factors"), dict):
                    cfg.update(loaded["factors"])
            except Exception as exc:
                log.warning("读 strategy.yaml 失败，用默认值：%s", exc)

    obs = _collect_observations(data_dir / "picks")
    min_obs = int(cfg["min_observations"])
    result: dict = {
        "trade_date": trade_date,
        "observations": len(obs),
        "ic": None,
        "icir": None,
        "verdict": "样本不足",
        "note": "",
    }
    if len(obs) < min_obs:
        result["note"] = f"只有 {len(obs)} 条带未来收益的样本，少于 {min_obs} 条，不下结论。等 track-record 积累几周后再看。"
        log.info("因子检验：样本不足（%d 条）", len(obs))
    else:
        # 按日期分组算每天的 IC，再对每天的 IC 求均值/标准差得 ICIR
        by_date: dict[str, list[dict]] = {}
        for item in obs:
            by_date.setdefault(item["date"], []).append(item)
        daily_ics = []
        for date, items in sorted(by_date.items()):
            if len(items) < 3:
                continue
            ic = _spearman([i["score"] for i in items], [i["return"] for i in items])
            if ic is not None:
                daily_ics.append(ic)
        if len(daily_ics) >= 3:
            mean_ic = sum(daily_ics) / len(daily_ics)
            var = sum((v - mean_ic) ** 2 for v in daily_ics) / len(daily_ics)
            std_ic = math.sqrt(var)
            icir = mean_ic / std_ic if std_ic > 0 else None
            result["ic"] = round(mean_ic, 4)
            result["icir"] = round(icir, 4) if icir is not None else None
            usable = abs(mean_ic) > float(cfg["ic_usable"])
            reliable = icir is not None and abs(icir) > float(cfg["icir_reliable"])
            if usable and reliable:
                result["verdict"] = "可用且可靠"
            elif usable:
                result["verdict"] = "可用，但 ICIR 未达可靠线"
            else:
                result["verdict"] = "暂不可用（|IC| 未过 0.03）"
            result["note"] = f"按 {len(daily_ics)} 个交易日的 IC 汇总。"
            log.info("因子检验：IC=%s ICIR=%s（%d 天）", result["ic"], result["icir"], len(daily_ics))
        else:
            result["note"] = "能算 IC 的交易日不足 3 天，暂不下结论。"

    latest = load_json(data_dir / "latest.json", {}) or {}
    latest["factors"] = result
    save_json(data_dir / "latest.json", latest)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="因子 IC/ICIR 检验")
    parser.add_argument("--data-dir", default="", help="数据目录，默认 docs/data")
    parser.add_argument("--date", default="", help="交易日，默认今天")
    args = parser.parse_args()

    root = repo_root()
    data_dir = Path(args.data_dir) if args.data_dir else root / "docs" / "data"
    compute(data_dir, args.date or today_cst())


if __name__ == "__main__":
    main()
