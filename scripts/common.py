# -*- coding: utf-8 -*-
"""公共小工具：日期、JSON 读写、日志。

本平台所有脚本只用 Python 标准库，不额外装包。
所有时间都用北京时间（Asia/Shanghai）。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")


def setup_logging(name: str = "quant-platform") -> logging.Logger:
    """统一日志格式，输出到 stdout（Actions 日志里能看到）。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger(name)


def today_cst() -> str:
    """今天（北京时间），YYYY-MM-DD。"""
    return datetime.now(BEIJING).date().isoformat()


def now_cst_iso() -> str:
    """现在（北京时间），ISO 8601 带时区。"""
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def repo_root() -> Path:
    """仓库根目录（脚本在 scripts/ 下，根是上一级）。"""
    return Path(__file__).resolve().parent.parent


def load_json(path: Path, default=None):
    """读 JSON；文件不存在或解析失败返回 default（并记一条 warning）。"""
    log = logging.getLogger("quant-platform")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("读 JSON 失败 %s：%s", path, exc)
        return default


def save_json(path: Path, data, indent: int = 2) -> None:
    """写 JSON（先写临时文件再改名，避免写一半被读到）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


def fail(message: str, code: int = 1):
    """打印错误并退出（Actions 里会把这一步标红）。"""
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)
