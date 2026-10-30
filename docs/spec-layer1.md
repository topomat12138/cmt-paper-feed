# 第 1 层实现规格（给 Codex）

Oct 28, 2026 · @Zhuo

> 导出自 Claude Docs 原文档：https://claude.ai/artifact/7L9xkC1FZ8NNBfZVzq3xsn（2026-10-28，rev 7）。以原文档为准，这里是给 Codex 读的副本。

> 2026-10-29 更新：PR 6 的邮箱接入方案已由仓库所有者改为外部 Apps Script → repository_dispatch；以下 PR 6、对应决策表和 daily 接口补充以此更新为准。

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
      scholar_alert.py   # Scholar 公开标题/链接 dispatch，不读取邮件
    resolve.py           # 把不完整的记录补全为 arXiv / DOI 元数据
    dedup.py
    store.py             # 读写 data 分支的 jsonl
    embed.py
    build_index.py       # 从 jsonl 生成 SQLite FTS5 索引
    cli.py               # feed ingest / scholar / embed / index / search / backfill
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
- `daily/<date>.json` 格式：`{"date": "2026-10-28", "new": [paper_id...], "updated": [paper_id...]}`，第 2 层据此处理新增及更新论文。第 2 层应处理自上次运行以来新增或变化的所有 daily 文件，而不是只读一个日期。

## 分步实现

一共 6 个 PR，按顺序做。PR 6 的外部 dispatch 方案现已确定。每个 PR 都要带测试，测试不联网，网络请求用保存或人工构造的样例响应代替。

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

### PR 6：Google Scholar 提醒来源（外部 dispatch）

- 读邮件在仓库外完成：外部 Gmail 的 Google Apps Script 已另行实现，每天只提取论文标题和真实链接，调用 GitHub API 的 `repository_dispatch`。脚本、邮箱凭据、邮件原文、提醒名称和触发作者均不在本仓库中处理或保存。
- 事件类型为 `scholar_alert`，`client_payload` 格式为 `{"version": 1, "items": [{"title": "...", "url": "https://..."}]}`，URL 可为 null。脚本每批最多 50 条，一天可以多批，也可能重试重复发送；存储必须幂等。
- `scholar.yml` 名称为 `Scholar alert ingest`，支持 `repository_dispatch(types: [scholar_alert])` 和带 `items_json` 输入的 `workflow_dispatch`。只声明 `contents: write`，与其他数据工作流共用 `cmt-paper-feed-data` concurrency 组。该组的四个写入工作流统一使用 `queue: max`，避免多批 dispatch 在默认单个 pending 队列中互相替换。
- 工作流 checkout main 和隔离的 data，运行 `feed scholar --data-dir DIR --payload "$GITHUB_EVENT_PATH"`。手动输入也从该事件文件读取，避免把原始 items 放进步骤环境变量、shell 表达式或日志；本地测试可用 `--items-json`。
- JSON 报告只包含新增/更新、输入及去重数量、各解析路径数量和错误分类计数，写入 step summary；不打印整段 payload 或 URL 查询串。成功部分照常发布，再报告失败。`publish --kind scholar` 只提交 papers/、daily/，提交信息为 `data: scholar <新增>/<更新>`；无变化不提交。
- 在 embed.yml 的 `workflow_run` 列表加入 `Scholar alert ingest`，发布后的新记录自动补向量。

**输入校验（sources/scholar_alert.py）**

- version 严格为整数 1；items 必须是列表且至多 200 条。标题必须为 1–1000 字符的非空字符串；URL 仅允许 null 或至多 2000 字符、以 http/https 开头的字符串。非法条目跳过并记录 `invalid_item`，不能拖垮整批。
- scholar.google.* / google.* 主机的 URL 视为 null。同批归一化标题相同的条目合并；论文标题中的 Unicode 下标和 LaTeX 数学定界符应归一化为相同文本，如 MnBi₂Te₄ 与 MnBi$_2$Te$_4$。

**解析顺序（resolve.py，每步命中就停）**

1. 从 arxiv.org 的 /abs/ 或 /pdf/ URL 提取 arXiv 号，支持版本、.pdf 后缀和旧式 cond-mat/0601001。
2. 提取 doi.org 或出版商 URL 路径中的 `10.\d{4,9}/...`，去掉末尾 /abstract、/full、/pdf、.pdf 等；Nature /articles/<slug> 映射为 10.1038/<slug>。APS 新式短 DOI、Wiley /doi/abs|full|pdf/ 和 IOP 都要支持；ScienceDirect 的 pii 不当作 DOI。
3. 用 arXiv API 的 ti 短语查询回查标题，沿用 PR 2 的限速/重试，仅接受归一化标题完全相等的唯一候选。
4. 用 OpenAlex `/works?filter=title.search:<归一化标题>&per_page=50` 回查标题，User-Agent 只写仓库 URL、不带邮箱。检查前 50 条中的归一化标题完全相等候选，不因 meta.count 大于已返回条数而放弃。同标题的预印本/正式版 work 合并标识：locations 中的 arXiv 号须唯一，期刊 DOI 须唯一（排除 `10.48550/arXiv.*`）；仅保留唯一的标识，两者都不唯一时放弃。
5. 均未命中时保留 title: 记录，仅有公开标题和 sources=["scholar_alert"]。

标题以 … 或 ... 结尾时，第 3、4 步改为归一化前缀匹配，但截断前内容至少 60 字符，且须对应唯一完整归一化标题。arXiv 未完整返回的搜索结果不能用来猜测唯一候选；OpenAlex 按上述前 50 条规则处理。

拿到 arXiv 号后复用现有批量元数据接口（每批至多 100、请求至少间隔 3 秒、最多 3 次尝试），生成完整 Paper，listing_type 和 announced 留 null。只有 DOI 时用 OpenAlex 元数据的标题、作者、abstract_inverted_index 还原摘要和期刊名组装 Paper，links 只放 canonical DOI。已知 DOI 的元数据请求失败或返回 404 时保留 DOI、输入标题和 canonical DOI 链接，记录 resolution_failure，不退化为 title:。Crossref 后备可选，本 PR 不要求。

Scholar 的 DOI-only 记录写入前，若已有记录中恰好有一条 arXiv 记录同时满足归一化标题完全相等、至少一位作者姓氏相同、自身没有 DOI，则关联到该 arXiv 记录，计数为 doi_title_link；其他情况维持原合并规则。已有 DOI 强匹配优先，多个不同 DOI 不能同时认领同一条无 DOI 的 arXiv 记录。通用存储不对已识别记录仅按标题合并。

所有输入记录的 sources 仅为 ["scholar_alert"]，再复用既有合并逻辑；已有 arxiv_listing 记录增加来源，计作本次 updated。外部失败按条目隔离，退回 title: 或记录安全错误，不影响其他成功论文；不请求 payload 中任意 URL，不保留其查询串。

所有诊断仍写入 daily 和计数报告。后续步骤成功解析或保住 DOI 时不因 resolution_failure 单独标红；输入不合法、因失败退化为 title-only、存储失败或标识冲突仍返回非零。以上解析细化按 PR 6 的 Claude 审查意见更新。

**daily 与验收**

- daily 使用运行时 UTC 日期，与 arXiv 同日的 new/updated 清单累加，不覆盖既有记录；周末只含 Scholar 条目的 daily 文件也是正常结果。
- 离线测试用人工 payload 和 httpx.MockTransport，不使用真实邮件/提醒内容。覆盖 URL 变体、唯一/歧义/截断标题、非法项、Google 链接、Unicode/LaTeX、重复发送字节不变、来源并集、周末 daily、发布目录隔离和 actionlint。
- scholar.yml 不使用 secrets，仅 checkout/publish 使用 github.token；不打印 github.event 内容。PR 描述列出验收证据、偏离清单及合并后在 Actions 粘贴两条公开样例（arXiv + 期刊 DOI）进行手动验证的方法。

## 开工前要定的事

仓库创建已完成，Scholar 接入方案已确定；其余事项继续使用已确认的范围或默认值。

| 事项 | 默认 | 说明 |
| --- | --- | --- |
| 创建公开仓库 | 你在 GitHub 上建 `topomat12138/cmt-paper-feed`，空仓库 | 名字可以改；我这边不能建仓库 |
| Scholar 提醒怎么读 | 已定：仓库外的 Gmail Google Apps Script 只发送公开标题/真实链接，以 scholar_alert repository_dispatch 传入 version 1 的 items | 脚本每批最多 50 条，可重复发送；仓库校验至多 200 条并幂等合并。不读取邮箱，不接触邮箱凭据、邮件原文、提醒名称或触发作者；脚本不在本 PR 范围 |
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
