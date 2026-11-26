# 量化平台（quant-platform）

A 股量化平台：盘后选股 + 自选股深度研报 + 公开跟踪验收，一个统一前端看和管。

**只出买卖建议，不自动交易。没有推送。** 不依赖任何外部服务器，全部跑在 GitHub Actions 上。

## 它由什么组成

| 部分 | 来源 | 作用 |
| --- | --- | --- |
| 选股 | ZhuLinsen/alphasift（盘后筛全市场 A 股） | 每天 18:30 跑，规则打分主、AI 复核副；T+5/T+10/T+20 公开跟踪 |
| 分析 | ZhuLinsen/daily_stock_analysis（DSA） | 每天 18:00 分析自选股，Gemini 出深度研报 + 大盘复盘 |
| 框架层 | 视频 12 节量化框架（方法论） | 回测、绩效归因、半凯利仓位、风控线、成本模型、因子 IC 验证 |
| 前端 | 本仓库 `docs/`（GitHub Pages） | 看今日选股 / 研报 / 跟踪 / 大盘复盘，管自选股 |

两个上游项目**不在这里 Fork、不复制代码**：每次运行都在 Actions 里按 pin 的 commit SHA 检出（见 `config/upstream.json`），产出的数据提交到本仓库。

## 每天的时间线（北京时间，工作日）

- **18:00** `daily-analysis.yml`：DSA 分析自选股 → `docs/data/reports/<日期>/`
- **18:30** `daily-picks.yml`：AlphaSift 选股 + T+N 验收 → `docs/data/picks/<日期>/`
- **19:15** `build-site-data.yml`：回测/归因/仓位/风控/因子 + 合并 → `docs/data/latest.json`
- 前端读 `docs/data/latest.json`，每天看一次就够了。

## 目录结构

```
.github/workflows/   3 个定时工作流（选股 / 分析 / 前端数据）
config/              upstream.json（上游 pin 的 SHA 与时间表）、strategy.yaml（成本/仓位/风控/因子参数）、watchlist.example.json
scripts/             纯标准库 Python：合并、解析、同步 run 历史、仓位、风控、因子、回测、跟踪验收
docs/                GitHub Pages 站点：index.html + app.js + style.css，数据在 docs/data/
vendor/              运行时目录，Actions 里检出上游代码用（不提交，见 .gitignore）
```

## 没有推送，怎么做到的

三重保证，缺一不可：

1. DSA 的 `main.py` 必须加 `--no-notify`；
2. AlphaSift 的 `daily-report` 不加 `--push`；
3. 仓库里不配任何通知类 Secret/变量（没有 NOTIFY_URLS、EMAIL_*、SERVERCHAN_* 等）。

就算以后有人误配了通知变量，第 1 条也能兜底。

## 部署

见 [BUILD-NOTE.md](BUILD-NOTE.md)。要点：建仓库 → 推送 → 贴两个 Secret（`GEMINI_API_KEY`、`STOCK_LIST`）→ 开 Pages（main 分支 `/docs`）→ 手动触发一次验证。

## 自选股怎么改

在前端页面点「设置」贴 GitHub 令牌（PAT，只存在你自己浏览器里），然后在「自选股管理」里增删，点「保存到仓库」——页面用仓库公钥把新值加密后写回 `STOCK_LIST` Secret，下次定时运行生效。令牌建法见 BUILD-NOTE.md。

## 已知边界

- **没有 09:26 竞价选股**：AlphaSift 是盘后选股，旧平台的开盘前选股能力没有替代。
- Gemini 免费额度紧张时（2026-11-26 曾出现 503），AI 复核会失败，但规则选股照常跑；DSA 没 Key 跑不起来。
- 上游改了输出格式或接口时，`scripts/extract_dsa.py` 和 `scripts/merge_data.py` 可能要跟着改（BUILD-NOTE 里有首次运行的检查点）。
