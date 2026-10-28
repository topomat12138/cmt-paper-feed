# 第 1 层实现规格（给 Codex）

Oct 28, 2026 · @Zhuo

> 导出自 Claude Docs 原文档：https://claude.ai/artifact/7L9xkC1FZ8NNBfZVzq3xsn（2026-10-28，rev 7）。以原文档为准，这里是给 Codex 读的副本。

## 目标与硬性边界

第 1 层是一个公开仓库，每天自动把多个来源的新论文收进来，去重、算向量、建全文索引，存成结构化数据供第 2 层读取。它不打分、不筛选、不写摘要。本规格是写给 Codex 执行的，每一步是一个独立的 PR。

背景材料（供你参考，Codex 不需要读）：[过往仓库梳理与新架构草案](https://claude.ai/artifact/SziVA8JhsN1SLDCPj4XBMY)。研究画像是私有内容，不对 Codex 公开，第 1 层也不需要它。

**硬性边界**（每个 PR 都要满足）

1. 仓库里不出现任何体现个人兴趣的信息：没有关键词表、权重、画像、反馈、打分。配置里只允许写来源和 arXiv 分类。
2. 来自 Google Scholar 提醒的论文只记 `sources: ["scholar_alert"]`，不记录是哪一条提醒、哪位作者触发的，也不提交邮件原文。
3. 密钥只放 GitHub Secrets，日志里不打印邮箱地址和密钥。
4. 第 1 层不调用任何生成式 LLM。向量模型默认在 Actions 的 CPU 上本地运行。
5. 所有步骤可重复执行：同一天重跑不会产生重复记录，也不会覆盖更完整的旧记录。
6. 代码在 `main`，数据在 `data` 分支，沿用 daily-arXiv-ai-enhanced 的做法。

## 仓库结构与数据契约

数据契约是第 1 层和第 2 层之间唯一的接口，字段名一旦上线就只增不改。仓库名暂定 `cmt-paper-feed`。

**`main` 分支**

```
cmt-paper-feed/
  feed/
    schema.py            # Paper 模型（pydantic v2）
    sources/
      base.py            # Source 接口 + 注册装饰器
      arxiv_listing.py   # arxiv.org/list/<cat>/new
      scholar_alert.py   # Google Scholar 提醒邮件
    resolve.py           # 把不完整的记录补全为 arXiv / DOI 元数据
    dedup.py
    store.py             # 读写 data 分支的 jsonl
    embed.py
    build_index.py       # 从 jsonl 生成 SQLite FTS5 索引
    cli.py               # feed ingest / embed / index / backfill
  config/sources.yml
  tests/
  .github/workflows/ingest.yml
  pyproject.toml         # uv 管理
```

**`data` 分支**

```
papers/2026/2026-10.jsonl     # 一行一篇，按首次收录的月份分片
daily/2026-10-28.json         # 当天新增和更新的 paper_id 清单
embeddings/2026-10.parquet    # paper_id, model, text_hash, vector
```

SQLite 索引不提交到 git（二进制文件会让仓库历史膨胀），由 `feed index` 从 jsonl 现场生成，几万篇的规模只需几秒。

**Paper 记录**

```json
{
  "paper_id": "arxiv:2610.33463",
  "arxiv_id": "2610.33463", "arxiv_version": 1,
  "doi": null, "journal_ref": null,
  "title": "...", "authors": ["..."], "abstract": "...",
  "primary_category": "cond-mat.supr-con",
  "categories": ["cond-mat.supr-con", "cond-mat.mtrl-sci"],
  "comment": "36 pages, 7 Figures",
  "submitted": "2026-10-26", "announced": "2026-10-27",
  "listing_type": "new",
  "sources": ["arxiv_listing"],
  "links": {"abs": "https://arxiv.org/abs/2610.33463", "pdf": "https://arxiv.org/pdf/2610.33463"},
  "first_seen": "2026-10-28T01:30:00Z", "last_updated": "2026-10-28T01:30:00Z"
}
```

- `paper_id` 有 arXiv 号时用 `arxiv:<不带版本号>`，否则用 `doi:<小写 DOI>`；两者都没有时用 `title:<归一化标题的 sha1 前 12 位>`，并在之后能解析出 arXiv/DOI 时合并。
- `listing_type` 取 `new` / `cross` / `replacement`；`sources` 是集合，同一篇从两个来源进来时合并。
- 合并规则：非空字段不被空值覆盖；`arxiv_version` 取最大；`first_seen` 不变。
- `daily/<date>.json` 格式：`{"date": "2026-10-28", "new": [paper_id...], "updated": [paper_id...]}`，第 2 层每天只读这个文件就知道要处理哪些论文。

## 分步实现

一共 6 个 PR，按顺序做。PR 1–5 可以马上开始；PR 6 要等下一节的邮箱方案定下来。每个 PR 都要带测试，测试不联网，网络请求用保存的样例文件代替。

### PR 1：骨架、数据模型与存储

- uv 项目、`feed` 包、pytest、在 PR 上跑测试的 `test.yml`。
- `schema.py`：按上一节实现 Paper 和 `paper_id` 规则（含 DOI 小写、标题归一化：NFKC、去 LaTeX 命令与标点、小写、合并空白）。
- `store.py`：按月读写 jsonl，按 `paper_id` 合并，写出时按 `paper_id` 排序以保证 diff 稳定；生成 `daily/<date>.json`。
- **验收**：同一批记录写两次，文件字节不变；合并规则每条都有测试。

### PR 2：arXiv 来源

- 从 daily-arXiv-ai-enhanced 的 `daily_arxiv/spiders/arxiv.py` 移植列表页解析，去掉 Scrapy，改用 httpx + selectolax（或 BeautifulSoup）。
- 保留三个分区并写入 `listing_type`：new、cross、replacement。旧代码丢弃了 replacement，这里要保留。
- 元数据用 arXiv API 的 `id_list` 批量取，每批至多 100 个，请求间隔至少 3 秒。旧代码是一篇一个请求，不要照搬。
- 分类从 `config/sources.yml` 读，默认 `cond-mat.supr-con`、`cond-mat.mes-hall`、`cond-mat.str-el`、`cond-mat.mtrl-sci`。同一篇出现在多个分类页时只留一条。
- 当天没有新列表（周末、假日）时正常退出，写一个空的 daily 文件。
- **验收**：用保存的列表页 HTML 和 API XML 做测试；对一个真实日期手动跑一次，记录数与网页上三个分区之和（去重后）一致。

### PR 3：导入历史数据

- `feed backfill`：读取 `topomat12138/daily-arXiv-ai-enhanced` 的 `data` 分支中 `data/YYYY-MM-DD.jsonl`（只要原始文件，忽略 `*_AI_enhanced_*` 和 `.md`）。
- 字段映射：`summary` → `abstract`，文件日期 → `announced`，`listing_type` 置为 null，`sources: ["arxiv_listing"]`。旧记录没有 `submitted`，留空，不要为此重新请求 API。
- 不导入 `config/` 和 `feedback/` 里的任何东西。
- **验收**：导入 2026-04-11 至 2026-10-27 共 193 天，记录数等于去重后的 arXiv 号数（约 4200）；重跑不增加记录。

### PR 4：每日工作流

- `ingest.yml`：每天 UTC 01:30 运行（arXiv 在美东 20:00 发布），另加 `workflow_dispatch`，可以输入日期重跑。
- 步骤：checkout main 和 data `→ feed ingest（PR 5 合并后再加 feed embed）→` 提交到 data 分支（沿用旧仓库的重试推送逻辑）。`concurrency` 防止两次运行重叠。
- 任一来源失败不影响其他来源；失败信息写进 `daily/<date>.json` 的 `errors` 字段，工作流标红。
- **验收**：连续跑 3 个工作日无人工干预；手动重跑同一天 data 分支没有新提交。

### PR 5：向量与全文索引

- `embed.py`：文本为标题 + 摘要，`text_hash` 未变就不重算；向量存 float16。模型可配置，默认 `jinaai/jina-embeddings-v5-text-nano`（zotero-arxiv-daily 的默认值，CPU 可跑）；模型文件用 Actions cache 缓存。
- `build_index.py`：SQLite FTS5，索引标题、摘要、作者；`feed search "Shiba chain"` 返回前 20 条。
- 把历史数据的向量一次性补算。
- **验收**：每日增量的向量计算在 Actions 上不超过 10 分钟；重跑不重算；索引能从零生成。

### PR 6：Google Scholar 提醒来源（等邮箱方案）

- 从邮箱读取发件人为 `scholaralerts-noreply@google.com` 的未处理邮件，解析每条的标题、作者行和链接（链接通常是 scholar.google.com 的跳转，取出其中的真实 URL）。
- `resolve.py`：真实 URL 是 arXiv 就取 arXiv 号；否则按标题查 arXiv API，再查 Crossref 或 OpenAlex 拿 DOI；都查不到就存为 `title:` 记录。
- 处理完的邮件打标签或标已读，保证不重复处理。遵守硬性边界第 2 条。
- **验收**：用人工构造的样例邮件测试（不提交真实邮件）；端到端跑一次后，绝大多数条目解析出 arXiv 号或 DOI。

## 开工前要定的事

只有第 1 条会挡住 PR 1，第 2 条挡住 PR 6，其余都有默认值，你不改就按默认做。

| 事项 | 默认 | 说明 |
| --- | --- | --- |
| 创建公开仓库 | 你在 GitHub 上建 `topomat12138/cmt-paper-feed`，空仓库 | 名字可以改；我这边不能建仓库 |
| Scholar 提醒怎么读 | 新建一个专用 Gmail，在主邮箱设过滤器把 Scholar 提醒自动转发过去，工作流用 IMAP + 应用专用密码读取 | 好处是工作流碰不到主邮箱。另一种是 Gmail API 授权主邮箱，配置更复杂、权限更大 |
| arXiv 分类 | supr-con、mes-hall、str-el、mtrl-sci | 候选变多只增加第 2 层的计算量，不增加你的阅读量 |
| 旧 tracker | 第 2 层上线前继续运行 | 新旧可以对照几天 |
| 旧仓库里公开的画像与反馈 | 从当前版本删掉 `config/profile.yml`、`keywords.manual.yml`、`topic_tags.yml`、`ranking.yml` 和 data 分支的 `feedback/`，历史记录保留 | 内容不算敏感；若要彻底清除，只能重写历史或把仓库设为私有 |
| 期刊 RSS（PRL、PRB、PRX 等） | 暂不做 | 大多数论文先上 arXiv，等第 2 层跑起来再看是否有漏 |

## 第 2、3 层的接口与后续

第 2 层的详细规格等第 1 层 PR 4 稳定运行后再写，那时可以用真实数据调阈值。这里只定下会影响第 1 层设计的部分。

- **私有仓库** `research-brain` 由你创建。它的工作流在第 1 层之后运行，只读公开仓库的 `data` 分支，不往公开仓库写任何东西。
- **第 2 层流程**：读 `daily/<date>.json` → 粗筛（与锚点论文、问题描述的向量相似度；订阅作者直接通过）留约 10 篇 → LLM 带着画像和问题清单精判 → 0–3 篇进 digest → 在私有仓库开当天的 Issue，并存 `digest/YYYY/MM-DD.md` → 解析你在 Issue 里的回复，写进 `feedback/events.jsonl`，"关注 X"一类指令更新 `profile/interests.yml`。
- **第 2 层还要定**：用哪个模型和 API（密钥放私有仓库的 Secrets），以及精判阈值。建议先把历史的约 4200 篇跑一遍，你看一周的结果再定阈值。
- **第 3 层**不写通用代码。每个问题先在这个项目里讨论方案和工具，定了再在 `projects/` 下开工，不预设任何计算库。

## 工作约定

Codex 每次只做一个 PR，你把 PR 链接发到这里，我来审。把这份文档导出为 Markdown，放进新仓库的 `docs/spec-layer1.md`，再让 Codex 按它做。

1. 分支名 `codex/prN-<简述>`，与你之前的习惯一致；PR 描述写明对应规格里的哪一步，以及哪些验收项已满足。
2. 偏离规格的地方（换库、改字段名、合并步骤）在 PR 描述里单独列出来。
3. 数据契约里的字段名不能改，只能加字段。
4. 不提交密钥、真实邮件、模型权重。
5. 我审的重点：是否守住硬性边界、合并和去重是否正确、重跑是否幂等、测试是否覆盖验收项。审完以评论的形式给出，由你决定是否合并。
