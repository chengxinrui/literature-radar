# 文献雷达（Literature Radar）

一个可配置的 Python 脚本：定期搜索 **arXiv、Crossref、OpenAlex** 的论文元数据，去重后整理成 JSON / CSV / Markdown；可选地推送到飞书。GitHub Actions 工作流可以每天自动运行并把新数据提交回仓库。

> 当前版本自动收集的是**题录、摘要、机构线索和论文/开放获取链接**，默认不下载 PDF。对有版权的版本不做绕过登录或批量抓取。

## 1. 先改检索方向

当前 `config.yaml` 已按你的论文示例预填为四组：World Models 与连续时间/潜变量动力学、Neural Combinatorial Optimization、PDE/Neural Operators/谱方法与最优控制，以及学到的动力学与规划/控制/优化的交叉探索。你可以在 `terms` 中添加、删除或改写检索词：

- `core`：两条核心主线；
- `adjacent`：邻近的方法群；
- `exploratory`：用于发现跨领域联系的探索方向。

建议关键词用英文（可同时加中文词），尽量用**问题/方法/对象**的具体短语，而不只是一个大领域名。arXiv 分类号可放入 `arxiv_categories`，比如 `cs.LG`、`cs.CL`、`stat.ML`。

`weight` 目前主要作为主题元数据保留，方便后续排序/扩展；脚本不会把低权重方向直接过滤掉。

## 2. 本地运行

需要 Python 3.10+：

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell 可用：.venv\Scripts\Activate.ps1

pip install -r requirements.txt
python literature_radar.py --config config.yaml
```

即使没有 API key，arXiv 和 Crossref 也能运行。OpenAlex 需要 `OPENALEX_API_KEY`；把它放在环境变量中，不要写进 `config.yaml` 或提交到 GitHub：

```bash
# macOS / Linux
export OPENALEX_API_KEY="你的 key"
export CROSSREF_MAILTO="你的联系邮箱"
python literature_radar.py
```

OpenAlex 的 API key 可在 [OpenAlex API 设置页](https://openalex.org/settings/api) 获取；从 2026-02-13 起，OpenAlex API 请求需要 key（免费 key 可用）。

## 3. GitHub 自动更新

1. 把本目录内容放进一个 GitHub 仓库并推送。
2. 在仓库 **Settings → Secrets and variables → Actions → New repository secret** 添加：
   - `OPENALEX_API_KEY`：推荐，机构/国家和文献元数据更完整；
   - `CROSSREF_MAILTO`：联系邮箱，建议设置；
   - `FEISHU_WEBHOOK_URL`：只有需要飞书通知时才设置。
3. 在仓库 Actions 页面手动运行一次 `Update literature radar`；之后会按工作流中的 UTC 时间每天自动运行。

工作流会提交以下文件的变化：

- `data/papers.json`：完整文献库，适合程序处理；
- `data/papers.md`：完整、可读的 Markdown 文献目录，按年份分组；
- `data/daily/YYYY-MM-DD.md`：按首次收录日期保存的每日新增摘要；不同日期的文件会保留。当天重复运行时，会重建当天摘要并保留当天已收录的全部新论文；
- `data/new_this_run.md`：仅显示本轮新增，每次运行都会覆盖；
- `data/papers.csv`：完整文献库的表格导出，每次运行重建，不是只追加当天论文。

如果 GitHub 仓库禁止 Actions 写入，请在仓库的 Actions/Workflow 权限中允许工作流写入内容。

## 4. 飞书通知（可选）

在飞书群里添加一个**自定义机器人**，复制它的 webhook URL，然后：

1. 在 `config.yaml` 将 `feishu.enabled` 设为 `true`；
2. 将机器人 URL 保存为 GitHub Actions Secret：`FEISHU_WEBHOOK_URL`。

脚本只在本次有新论文时推送，并且只展示前 `max_papers_in_feishu` 篇。机器人密钥不要写进仓库文件。

## 5. 字段和判断边界

每条记录尽量保存：标题、作者、摘要、机构及国家/地区、国内/国外/合作范围、期刊/会议、DOI、arXiv ID、首次提交/更新/出版日期、元数据来源、开放获取链接、主题标签、引用数（OpenAlex）和手动阅读状态/笔记。对于尚未发现出版记录的 arXiv 论文，脚本默认每 30 天重查一次 arXiv 元数据，以发现后来新增的期刊引用/DOI（每次最多 100 条，可在配置中调整）。

- `domestic_country_codes: [CN]` 默认按中国大陆机构分类；需要时可把 `HK`、`MO`、`TW` 加进来。
- 机构与国家来自数据库的作者单位元数据。缺失或不明确时会显示“未知”，不会根据作者姓名猜国家。
- `已接收/待刊` 只有在元数据明确写出 accepted / in press / to appear 等表述时才显示。
- `已发表/有期刊或会议记录` 表示数据库中找到了出版条目或 arXiv 提供了期刊引用，不代表脚本独立验证了同行评审。
- `arXiv/预印本（暂未检出正式出版记录）` 不是“确定没有发表”：只是这次检索到的来源里没有找到正式出版记录。
- `reading_status` 和 `my_notes` 是手动维护字段；合并新来源时会尽量保留。请在 `data/papers.json` 中改这两个字段，`papers.csv` 是导出文件，改它不会回写到 JSON。

来源说明：

- [arXiv API](https://info.arxiv.org/help/api/index.html)：公开 API，脚本对 API 请求作了间隔控制；
- [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/)：公开题录 API，建议提供 `mailto`；
- [OpenAlex API changelog](https://help.openalex.org/hc/en-us/articles/38868153578263-Changelog)：API key 相关变更说明。

## 6. 当前限制

- 文献数据库的收录范围、机构字段和状态字段不一致；特别是**“已接收但尚未正式出版”**通常没有统一、可靠的机器字段，所以脚本采用保守判定，并保留状态依据。
- 这不是系统综述检索器，也不能保证不漏文献；建议把它当作“雷达/候选清单”，再核验出版社页面、DOI 和论文原文。
- 不包含 Google Scholar、CNKI、万方等需要登录/许可或没有稳定公开接口的站点。需要中文数据库时，建议走学校图书馆授权或官方可用的导出/接口。
- 本版本只保存开放获取链接，不批量下载全文。需要的话可以再加“仅下载明确开放获取 PDF”的可选模块。
