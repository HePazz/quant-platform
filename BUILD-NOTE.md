# 量化平台 · 部署说明（BUILD-NOTE）

目标仓库：**HePazz/quant-platform**。本文档写给部署的人（W-Tao / 宝贝）。

## 一、架构

```
GitHub Actions（定时，无外部服务器）
  ├─ 18:00 daily-analysis.yml   DSA：自选股深度研报 + 大盘复盘
  ├─ 18:30 daily-picks.yml      AlphaSift：盘后选股 + T+5/T+10/T+20 验收
  └─ 19:15 build-site-data.yml  离线层：回测/归因/半凯利/风控/因子IC + 合并
        │ 产物全部提交到 main 分支 docs/data/
        ▼
GitHub Pages（main 分支 /docs）
  └─ docs/index.html：看（选股/研报/跟踪/复盘）+ 管（自选股）+ 设置（PAT）
        │ 改自选股时调 GitHub REST API
        ▼
  仓库 Secret STOCK_LIST（页面用仓库公钥 sealed-box 加密后写回）
```

**数据流（关键）：**

- 选股的原始 run 文件存在 `docs/data/picks/<日期>/runs/`；`scripts/sync_runs.py` 在每次选股前把历史拷进上游的 `track/data/runs/`（seed），跑完再按日期分回（sync），这样 evaluate 才能给旧入选股算 T+N 收益。
- 前端唯一入口是 `docs/data/latest.json`（`scripts/merge_data.py` 合并生成，幂等）；历史快照在 `docs/data/days/<日期>.json`，索引在 `docs/data/history.json`。
- 两个每日 job 都往 main 分支提交数据，用同一个并发组 `quant-platform-data` 串行跑，避免 push 冲突；提交时 `git pull --rebase` + 一次 push 重试。

**上游怎么用（不 Fork、不复制代码）：**

- 每次运行在 Actions 里用 `actions/checkout` 按 pin 的 commit SHA 检出到 `vendor/`（运行时目录，不提交）：
  - DSA `a780a5b15d8d67639c9db61a4d7914166c34e9ab`（MIT）
  - AlphaSift `6b779f23797773d28b6a960d853f81bf8451eecd`（Apache-2.0）
- 升级上游版本 = 改 `config/upstream.json` 里的 SHA + 对应 workflow 里的 SHA（两个地方要一起改），然后手动触发一次验证。

## 二、部署步骤

### 1. 建仓库

- GitHub 上新建 **HePazz/quant-platform**（Public，README 不用勾，空仓开始）。
- 把本目录内容推上去（`main` 分支）：
  ```bash
  cd ~/workspace/new-quant-platform
  git init -b main
  git add .
  git commit -m "init: quant-platform scaffold"
  git remote add origin git@github.com:HePazz/quant-platform.git
  git push -u origin main
  ```
  注意：`.gitignore` 已排除 `vendor/`、`track/`、`pack/`，这些是运行时目录，不会进仓库。

### 2. 贴 Secrets（Settings → Secrets and variables → Actions → New repository secret）

| Secret 名 | 值 | 备注 |
| --- | --- | --- |
| `GEMINI_API_KEY` | W-Tao 已有的 Gemini Key（AI Studio 的那个） | DSA 必填；AlphaSift 可选（没有就纯规则模式） |
| `STOCK_LIST` | `002242,600519,000625` | 自选股，逗号分隔，6 位代码 |

- **不要贴**任何推送类 Secret（没有 NOTIFY_URLS、EMAIL_*、SERVERCHAN_* 等）。本平台没有推送。
- 之前两个已删的 Fork 里贴过的 `GEMINI_API_KEY` 和 `STOCK_LIST` 已随删库一起删了，**要在新仓库重新贴**。

### 3. 开 Pages

Settings → Pages → Build and deployment → Source 选 **Deploy from a branch**，Branch 选 **main**，文件夹选 **/docs**，保存。地址约是 `https://hepazz.github.io/quant-platform/`。

### 4. 手动触发一次，验证

Actions 页 → 依次点 **每日分析（DSA）** 和 **每日选股（AlphaSift）** → Run workflow。两个都跑通后，等 19:15 的重建任务（或手动触发「重建前端数据」），再打开 Pages 看数据。

### 5. 前端设置里贴 PAT

- PAT **已经建好了**（2026-11-27 01:48，classic，Note `quant-platform`，No expiration，勾选 `repo`，W-Tao 自己存着）。**不要问他要值、不要让他发聊天里**——他自己在前端页面「设置」里粘贴就行。
- 贴完点「测试令牌」，显示账号名即有效。令牌只存在他自己浏览器的 localStorage 里，页面不会把它发到 api.github.com 以外的任何地方。
- 没有 PAT 也能看数据，只是不能改自选股、不能手动触发运行。

## 三、首次运行检查点（跑通一次后逐项确认）

1. **选股 job 耗时**：AlphaSift 全量数据包构建（250 日）第一次可能较久，看 Actions 日志确认在 120 分钟超时内完成；后续走缓存。
2. **run 文件名**：`scripts/sync_runs.py` 按文件名里的 `YYYY-MM-DD` 或 JSON 里的 `trade_date`/`created_at` 分组；如果上游改了 run 文件命名，看 `docs/data/picks/<日期>/runs/` 里文件是否按日期分对了。
3. **DSA 产物格式**：`scripts/extract_dsa.py` 解析 `reports/` 里的 HTML/JSON；如果上游改了报告文件名或结构，看 `docs/data/reports/<日期>/dsa.json` 有没有正常生成（没有结构化数据时前端只显示「暂无研报」，HTML 原文件仍在目录里）。
4. **stale 提示**：如果选股产物里 `pack_is_stale` 为 true，前端会提示数据包是旧的——一般是数据包缓存没建起来，看 daily-picks 日志里的 pack 步骤。
5. **Gemini 503**：2026-11-26 生成时 gemini-3.8-flash / 4-hydrogen / 3.5-flash / 3.6-flash 都返回过 503（需求过高，暂时的）。DSA 模型默认 `gemini-3.8-flash`（可改 Repository variables `GEMINI_MODEL`），AlphaSift 同理（`LITELLM_MODEL`）。AI 复核失败不影响规则选股。
6. **STOCK_LIST 写回**：在前端改一次自选股、保存成功后，去仓库 Secrets 页确认 `STOCK_LIST` 的 Updated 时间变了（值本身看不到，这是正常的）。

## 四、日常使用

- 每天看一次前端页面即可（18:00 分析、18:30 选股、19:15 数据重建）。
- 改自选股：页面「自选股管理」增删 → 「保存到仓库」→ 下次定时运行生效；也可以在页面点「手动跑一次分析」立即跑。
- 报告 HTML 原文件在 `docs/data/reports/<日期>/` 里，研报卡片上有「看完整报告」链接。

## 五、硬边界（不要碰）

- 不碰 **216.167.114.218**（VPN 专用服务器）。
- 不碰 **38.34.1.108** 旧量化平台（它继续跑它的，两边并行）。
- 不自动交易；A 股 only；没有任何推送。

## 六、还缺用户的什么

- [x] GitHub PAT：已建好（classic，Note `quant-platform`，No expiration，`repo`），W-Tao 自己存着，部署第 5 步他自己在前端设置里粘贴。
- [ ] `GEMINI_API_KEY` Secret：他有 Key 的值，在新仓库重新贴一次（第 2 步）。
- [ ] `STOCK_LIST` Secret：`002242,600519,000625`（第 2 步）。
- [ ] 建仓库 + 推送（第 1 步，由宝贝做）。
- [ ] 首次手动触发验证（第 4 步）。

## 七、已知边界

- **没有 09:26 竞价选股**：AlphaSift 是盘后选股，旧平台的开盘前选股能力没有替代。
- 因子 IC 检验要 ≥20 条带未来收益的样本才下结论，起步几周内都会显示「样本不足」，这是正常的。
- 回测是等权复盘、扣 0.96% 往返成本的历史统计，不是未来收益保证。
