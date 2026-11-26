# -*- coding: utf-8 -*-
"""在“提交进仓库的历史”和“上游 track 工作目录”之间同步 run 文件。

背景：AlphaSift 的 screen/evaluate 只认 track/data/runs/ 这一个目录，
而本平台把历史存在 docs/data/picks/<日期>/runs/ 里（main 分支）。
每次选股 job 要做两步：
  seed  -- 把已提交的历史 run 文件拷进 track/data/runs/（扁平化），
          这样 evaluate 才能给旧的入选股算 T+5/T+10/T+20；
  sync  -- evaluate 跑完后，把 track/data/runs/ 里的文件按日期分回
          docs/data/picks/<日期>/runs/（evaluate 会更新旧 run 的收益字段）。

日期判定（按顺序试）：文件名里的 YYYY-MM-DD → JSON 里的
created_at / trade_date / date 字段 → 当日（--date 传入）。
文件名本身原样保留，不改名。

用法：
  python scripts/sync_runs.py seed --data-dir docs/data --track-dir track
  python scripts/sync_runs.py sync --data-dir docs/data --track-dir track [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from common import load_json, setup_logging, today_cst

log = setup_logging("sync_runs")

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def run_date(path: Path, data: dict | None, fallback: str) -> str:
    """从文件名或内容里找出这条 run 的交易日。"""
    match = DATE_RE.search(path.name)
    if match:
        return match.group(1)
    if isinstance(data, dict):
        for key in ("trade_date", "date"):
            value = str(data.get(key, "")).strip()
            if DATE_RE.fullmatch(value[:10]):
                return value[:10]
        created = str(data.get("created_at", "")).strip()
        match = DATE_RE.search(created)
        if match:
            return match.group(1)
    return fallback


def seed(data_dir: Path, track_dir: Path) -> int:
    """历史 → track 工作目录。返回拷入的文件数。"""
    runs_dir = track_dir / "data" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    picks_root = data_dir / "picks"
    count = 0
    if picks_root.is_dir():
        for src in sorted(picks_root.glob("*/runs/*.json")):
            dest = runs_dir / src.name
            if dest.exists():
                continue
            shutil.copy2(src, dest)
            count += 1
    log.info("seed：已拷入 %d 个历史 run 文件", count)
    return count


def sync(data_dir: Path, track_dir: Path, fallback_date: str) -> int:
    """track 工作目录 → 按日期分回 docs/data/picks/。返回写回的文件数。"""
    runs_dir = track_dir / "data" / "runs"
    count = 0
    if not runs_dir.is_dir():
        log.info("sync：track 里没有 runs 目录，跳过")
        return 0
    for src in sorted(runs_dir.glob("*.json")):
        data = load_json(src)
        date = run_date(src, data, fallback_date)
        dest_dir = data_dir / "picks" / date / "runs"
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest_dir / src.name)
        count += 1
    log.info("sync：已写回 %d 个 run 文件", count)
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="同步 AlphaSift run 历史")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("seed", "sync"):
        sp = sub.add_parser(name)
        sp.add_argument("--data-dir", default="", help="数据目录，默认 docs/data")
        sp.add_argument("--track-dir", default="track", help="上游 track 工作目录")
        sp.add_argument("--date", default="", help="交易日（sync 的 fallback），默认今天")
    args = parser.parse_args()

    from common import repo_root

    root = repo_root()
    data_dir = Path(args.data_dir) if args.data_dir else root / "docs" / "data"
    track_dir = Path(args.track_dir)
    if args.command == "seed":
        seed(data_dir, track_dir)
    else:
        sync(data_dir, track_dir, args.date or today_cst())


if __name__ == "__main__":
    main()
