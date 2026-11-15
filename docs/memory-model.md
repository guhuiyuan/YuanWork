# YuanWork 记忆模型

> 情景记忆 + 语义记忆 + 整合层 + Wiki 之间的数据流
> 最近更新：2026-10-19

---

## 1. 概览

YuanWork 的记忆系统由三层组成：

| 层 | 存储 | 内容 | 生命周期 |
|---|---|---|---|
| **情景记忆** (Episodic) | SQLite | 对话原始记录 | 永久（用户可清空） |
| **语义记忆** (Semantic) | LanceDB + Tantivy | 文档 chunks + 向量 + FTS 索引 | 与 KB 同步 |
| **用户画像** (Profile) | SQLite | 主题偏好 / 重要实体 / 习惯 | 持续更新 |
| **Wiki** | SQLite + Markdown 文件 | 编译后的结构化页面 | 按需生成 / 手动更新 |
| **冲突日志** | SQLite | LLM vs KB 的不一致记录 | 永久 |
| **审计日志** | JSONL | 每次 LLM 调用 | 用户可清空 |

四层之间由 **整合层 (Consolidation Layer)** 编排：写入、合并、冲突解决、反思。

---

## 2. 数据流

```
对话轮次
    │
    ▼
Agent Orchestrator (3 步推理)
    │
    ├─► Hybrid Retrieval ──► Semantic Memory (LanceDB + Tantivy)
    │                            │
    │                            ▼ chunks
    │                            │
    ├─► Wiki Compile Trigger (if high-confidence cluster) ──► Wiki Page
    │                                                              │
    ▼                                                              │
LLM Call (Audited)                                                │
    │                                                              │
    ▼                                                              │
Answer + Citations                                                │
    │                                                              │
    ▼                                                              │
Writer (整合层) ──► Episodic Memory (SQLite) ──► Profile ──► Reflection
                          │                            │
                          ▼                            │
                    Embedding (bge-m3)                │
                          │                            │
                          ▼                            │
                    Semantic Memory ◄──────────────────┘
```

---

## 3. 各层详解

### 3.1 情景记忆 (Episodic)

**目的**：保留对话的原始序列，支持"上次我们聊过 X"这种查询。

**Schema**：

```sql
CREATE TABLE episodes (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    role TEXT,             -- 'user' / 'assistant'
    content TEXT,          -- 原始文本
    topic_tags TEXT,       -- JSON 数组
    entities TEXT,         -- JSON 数组
    embedding BLOB,        -- bge-m3 1024-dim
    created_at TIMESTAMP
);
```

**写入**：每轮对话（user + assistant）结束 → 摘要 → 主题/实体抽取 → 向量化 → 入库

**检索**：

- 同 session_id → 时序回放
- 向量相似度 → 跨 session 主题检索
- 时间范围 → "上周的对话"

### 3.2 语义记忆 (Semantic)

**目的**：让 YuanWork 能从文档中"找知识"。

**存储**：

- **LanceDB** 表 `chunks`，每行 = 一个 chunk
  - 字段：`id` / `doc_id` / `text` / `dense` (1024-dim vector) / `metadata` (json)
  - metadata 含：`file_name` / `page_number` / `slide_number` / `chunk_idx` / `content_type`
- **Tantivy FTS** 索引，BM25 over jieba-segmented text
  - 字段：`id` (raw) / `doc_id` (raw) / `text` (default tokenizer)

**写入**：

```
文档 → 解析 (Block) → 分块 (Chunker) → embed (bge-m3) + segment (jieba)
                                                  │
                                                  ├─► LanceDB.add(records)
                                                  └─► Tantivy.add(docs)
                                  [先 delete_by_ids 防重导入翻倍]
```

**检索**：

```
query ──┬─► bge-m3.embed ──► LanceDB.vector_search(top-50)
        │                            │
        └─► jieba.segment ──► Tantivy.search(top-50)
                                     │
                                     ▼
                          融合 (alpha*dense + beta*sparse) ──► top-20
                                                                    │
                                                                    ▼
                                                              Reranker ──► top-5
```

**删**：`delete_by_doc_id` 同时清 LanceDB + Tantivy。

**迁移**：v3 → v4（无 FTS）首次访问 FTS 时若空但 LanceDB 有行 → 自动从 LanceDB 重建。

### 3.3 用户画像 (Profile)

**目的**：让 YuanWork 知道用户是谁、关心什么、怎么用。

**Schema**：

```sql
CREATE TABLE user_profile (
    key TEXT PRIMARY KEY,
    value TEXT,                    -- JSON
    source_episode_id TEXT,        -- 哪条对话触发的
    confidence REAL,
    updated_at TIMESTAMP,
    expires_at TIMESTAMP
);
```

**示例 key**：`name` / `preferred_language` / `domain_focus` / `entity:<entity_name>`

**写入**：

- 用户显式说"我住在北京" → 立即更新
- 对话推断"用户关注房地产" → 异步更新，confidence 0.7
- 反思时重新打分（confidence > 0.92 合并，< 0.3 删除）

### 3.4 Wiki

**目的**：把碎片 chunks 编译成结构化、可浏览、可双向链接的页面。

**Schema**：

```sql
CREATE TABLE wiki_pages (
    id TEXT PRIMARY KEY,            -- slug, e.g. "购房合同/违约金条款"
    title TEXT,
    markdown TEXT,                  -- 编译后的内容
    summary TEXT,                   -- 200 字摘要
    source_chunk_ids TEXT,          -- JSON 数组
    source_doc_ids TEXT,            -- JSON 数组
    category TEXT,                  -- 项目/概念/人物/合同/法规
    tags TEXT,                      -- JSON
    version INTEGER,
    created_at, updated_at, last_compiled_at
);

CREATE TABLE wiki_links (
    source_id, source_type,         -- 'page' | 'chunk' | 'doc' | 'episode'
    target_id, target_type,
    link_type,                      -- 'reference' | 'derived_from' | 'cites' | 'related'
    weight REAL,
    created_at,
    PRIMARY KEY (...)
);

CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    name TEXT,
    type TEXT,                      -- 'person' | 'project' | 'concept' | 'contract' | 'law'
    aliases TEXT,                   -- JSON
    properties TEXT,                -- JSON
    mention_count INTEGER,
    first_seen, last_seen
);
```

**触发**：

- **被动**：3 步推理时如果召回 chunks 跨越 ≥4 个 chunk / ≥2 个 doc / top-1 ≥0.55 → 触发编译
- **主动**：反思时批量发现高频主题 → 主动编译 → 推送给用户

**编译流程**：

```
chunks ──► LLM (compile prompt) ──► Markdown
                                       │
                                       ├─► 保存到 wiki_pages
                                       └─► 抽取 [[wiki:link]] / [[entity]] ──► wiki_links + entities
```

**页面的四个部分**：标题、摘要、要点、出处。要点是页面自己的列表（`wiki_pages.key_points`），
不只是正文里的一段——页面列表与图谱靠它说清「这页讲什么」。

**用户改过的页面**：

页面正文按块（段落）存，每块记归属（模型 / 用户）：

```sql
CREATE TABLE wiki_page_blocks (
    id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL,
    order_index INTEGER,
    text TEXT NOT NULL,
    owner TEXT NOT NULL,             -- 'model' | 'user'
    conclusion_id TEXT
);
```

- 用户编辑页面（`PUT /api/v1/wiki/pages/{page_id}`）→ **只有改过的段落**归用户，其余保持原归属
- 之后自动重编译 → **只替换模型的块**，用户的块原样留在原位（AC-F-027）
- 用户写的那一段，模型新写的对应内容被丢掉——那一段是用户的

### 3.5 冲突日志

**目的**：让 YuanWork 知道 LLM 训练知识 vs KB 的不一致。

**Schema**：

```sql
CREATE TABLE conflicts (
    id TEXT PRIMARY KEY,
    episode_id TEXT,
    knowledge_chunk_id TEXT,        -- KB 中触发冲突的 chunk
    conflict_type TEXT,             -- 'factual' / 'procedural' / 'temporal'
    llm_answer TEXT,
    kb_evidence TEXT,
    resolution TEXT,                -- 'kb_wins' | 'llm_wins' | 'unresolved'
    user_acknowledged INTEGER,      -- 0 / 1
    created_at TIMESTAMP
);
```

**检测**：用同一小 LLM 做"事实一致性判别"。

**解决**：**以 KB 为准**（用户已确认），但**必须显式标注**：

```
[冲突提示] 你的知识库《xxx》第 N 页写的是「A」，而通用模型回答的是「B」。
已以知识库为准，请确认是否需要更新知识库。
```

**复用**：下次遇到同主题 + 同 chunk → 自动提示这是已知冲突。

### 3.6 审计日志

详见 [docs/privacy.md#审计日志](privacy.md#审计日志-stage-3)。

---

## 4. 整合层 (Consolidation Layer)

**目的**：把所有写入、合并、冲突解决、反思动作编排成一个统一状态机。

### 4.1 写入流程

每轮对话结束 → 异步触发：

```
对话轮次结束
    │
    ▼
Writer
    │
    ├─► 摘要 (LLM) ──► episode_summaries
    │
    ├─► 主题/实体抽取 (LLM) ──► episode.topic_tags + episode.entities
    │
    ├─► 重要性打分 (LLM) ──► episode.importance_score
    │
    ├─► 画像更新 ──► profile (去重 + 冲突解决)
    │
    ├─► 向量化 ──► episode.embedding
    │
    └─► 入库 ──► episodes
```

### 4.2 反思触发

任一条件满足：

- 对话轮数 ≥ `YUANWORK_REFLECTION_TRIGGER_TURNS`（默认 10）
- session 跨越 ≥ 2 小时
- 闲置 ≥ `YUANWORK_REFLECTION_TRIGGER_IDLE_MINUTES`（默认 30）
- 手动触发（API）

三种触发都真的能跑一轮整合（ADR-015）：

| 触发 | 谁来发现 | 怎么发现 |
|---|---|---|
| 轮数 | 每轮问答结束后 | 编排层记完这一轮就问一次引擎「该整合了吗」 |
| 闲置 | 后台看门狗 | 每分钟看一眼空闲时钟；没人问问题到阈值就跑一轮 |
| 手动 | `POST /api/v1/memory/consolidate` | 用户点一下，立刻跑一轮 |

整合是批量作业，**不在问答的关键路径上**：轮数触发在后台跑，问答不等它。

### 4.3 反思动作

```
Reflection
    │
    ├─► 去重 (相似度 > 0.92 合并)
    │
    ├─► 摘要提炼 (把多条 episode 合并成 1 条更精炼的 summary)
    │
    ├─► 画像更新 (重新计算 confidence)
    │
    └─► 过期归档 (importance < 0.3 + 90 天未访问 → 标记过期)
```

### 4.4 归纳：情景事件 → 实体、规律、页面要点

一轮整合读最近的**情景事件**（提问、引用、采纳、否定、增删文件），问模型「这些加起来说明什么」，
拿到三类候选，写回语义记忆：

| 候选 | 写到哪 | 生效条件 | 为什么 |
|---|---|---|---|
| 实体 | `entities`（`status=active`） | 自动生效 | 文档里客观存在的东西，不需要人确认 |
| 规律 | `entities`（`status=pending`，id 前缀 `rule-`） | **待审**：用户确认前不计入作答证据 | 跨事件推断出来的结论，可能错（ADR-039） |
| 页面要点 | `page_update_candidates` | 等页面编译时应用 | 页面由文档编译、由用户编辑 |

三条硬规矩：

1. **每条结果都带来源情景事件**（`source_event_ids`）—— 结论凭什么，看得见（ADR-030）
   模型指明了来自哪几条就记哪几条；没指明就记**整个窗口**——它确实是从整个窗口读出来的
2. **单条归纳失败只跳过那一条**，整轮继续（设计 §3.1）
3. **模型不可用时降级**：只归纳实体（模式匹配），规律宁缺毋滥——编出来的规律是错规律，用户还得去读它

已经确认过的规律不会被下一轮打回待审；同一实体再次归纳时来源事件是**合并**而不是覆盖。

---

## 5. Wiki ↔ RAG ↔ Memory 三角

```
                ┌────────────────────────────────────────┐
                │         Wiki Layer (结构化知识)         │
                │   Pages / Links / Entities / Graph     │
                └────┬─────────────────────┬─────────────┘
                     │ 编译来源              │ 反向链接
                     ▼                     ▲
   ┌─────────────────────────┐    ┌─────────────────────────┐
   │  Semantic Memory (LanceDB)│    │  Episodic Memory (SQLite)│
   │  文档 chunks（碎片化）    │◄───│  对话引用了哪些 Page      │
   └─────────────────────────┘    └─────────────────────────┘
```

**工作流**：

1. 用户提问 → RAG 召回 chunks
2. 如果 chunks 散乱 → 触发 Wiki 编译 → 缓存到 `wiki_pages`
3. 答案引用 Wiki Page（`[1] 详见 [[违约金条款]]`）
4. 用户点开 Wiki Page → 浏览 → 追问 → 回到 RAG
5. 整个过程记录到情景记忆，下次同主题可召回

**双向链接**：

- chunk → wiki_page：`derived_from` 边
- wiki_page → wiki_page：`related` 边
- episode → wiki_page：`cites` 边（用户对话中引用了哪个 page）

---

## 6. 数据保留策略

| 数据 | 默认保留 | 清空方式 |
|---|---|---|
| episodes | 永久 | `DELETE /api/v1/memory/episodic` |
| **情景事件（episodic_events）** | **5000 条 / 180 天，先到先淘汰** | **记录事件后自动清理；只记淘汰数量，不留原文副本（ADR-038）** |
| chunks | 与文档同步 | 文档删除时自动清 |
| wiki_pages | 永久 | 手动 `DELETE /api/v1/wiki/{id}` |
| conflicts | 永久 | 手动 |
| audit.jsonl | 永久 | `DELETE /api/v1/audit` |
| profile | 永久（带 expires_at） | 反思时自动过期 |

---

## 7. 三存储的已知边界

三处持久化（关系库 / 向量库 / 全文索引）之间**没有事务**，只有顺序。以下是当前的实际行为：

| 现状 | 后果 |
|---|---|
| 写路径：向量库 → 全文索引 → 关系库 | 向量写成功而关系库写失败时，向量库留下**孤儿切片段**，文件却不在知识库里 |
| 删路径：关系库 → 向量库 → 全文索引 | 关系库行已删而向量删除失败时，切片段成为**文件级孤儿** |
| 两库之间无事务 | 进程在两次写入之间崩溃，两个存储**永久分叉** |
| 全文索引只是向量库的镜像 | 全文写失败只记告警，稠密检索仍可用，但全文通道从此缺数据 |
| 全文索引只在**完全为空**时重建 | 部分缺失的分叉**永远不会被修复**；重建失败也只记告警 |
| 迁移只加列、不回填 | 老知识库的 `sparse_str` 等新列恒为空，溯源信息缺失 |
| 重摄取只删「本次要写的切片 id」 | 切片 id 含文本前 100 字符，改动正文即换 id，**旧切片永远残留** |
| 关系库三张表建了不用 | `episode_summaries`、`working_memory`、`ingest_jobs` 全无读写；摄取任务状态实际存在**进程内存**里，后端重启即丢 |
| 表格文件的溯源全丢 | `sheet_name` / `sheet_index` / `row_count` 既不在表结构里也不在正文里，Excel 引用只能定位到「哪个文件」 |
| `content_type` 恒为 `text` | 切块器写的是 `type` 键，入库读的是 `content_type`，表格与图片无法按类型区分 |
| `token_count` 是字符数除以 4 | 不是真正的令牌数 |
| 对话历史的向量列恒为空 | 情景记忆只有模糊匹配，没有向量检索 |
| 删除文件不清理实体提及 | 实体提及表里的相关行成为孤儿 |

---

## 8. 记忆与 Wiki 层的已知边界

| 现状 | 后果 |
|---|---|
| **五个模块从未被实例化** | 实体抽取器、链接器、导出器、写入控制器、反思调度器在 `src/` 下零调用点；15 个模块只有 6 个真正生效 |
| 实体抽取的模型路径必然抛错 | 抽取提示词里含 JSON 示例，`.format()` 把它的花括号当成占位符 → 配了模型就直接 `KeyError`，模型永远不会被调用 |
| 手动编译 Wiki 是桩 | `POST /api/v1/wiki/compile/{topic}` 恒返回「未实现」，真实编译只能由对话轮触发 |
| 取消对话是桩 | `POST /api/v1/chat/cancel/{session_id}` 恒返回「尚未实现」，不做任何取消 |
| 冲突的确认接口没有路由包装 | 冲突表里的「已确认」标志恒为 0，「包含已确认」查询参数无效 |
| 记忆之间没有矛盾检测 | 只检测「模型答案 vs 知识库证据」；情景记忆、画像、语义记忆之间的矛盾无人发现 |
| 画像是后写覆盖 | 无版本、无历史；整合层会二次更新同一画像，置信度可能从 0.75 被降成 0.6 |
| 中文主题标签存的是转义形式 | 写入时 `json.dumps` 默认转义非 ASCII，所以按中文标签做模糊匹配**搜不到**，只有正文搜索正常 |
| 整合的去重只计数 | 用 Python 内置 `hash()` 做哈希（每进程随机加盐，跨进程不稳定），且**只计数不删不合** |
| 整合的「归档旧冲突」是 TODO | `conflicts_archived` 恒为 0 |
| 整合的 `force` 参数被接收但从不使用 | 传与不传结果一样 |
| 反思调度器没有触发点 | 类从未被实例化；即使接上，「全库对话数 ≥ 10」几乎恒真，会变成每轮都反思 |
| Wiki 页面重编译残留孤儿链接 | 指向旧片段的来源链接不清理，链接表只增不减 |
| Wiki 编译的冷却状态只在进程内 | 重启即失效；同一主题可以连续编译两次 |
| 表格类文件的溯源全丢 | 工作表名与行号既不在表结构里也不在正文里，Excel 引用只能定位到「哪个文件」 |
| 情景记忆没有向量 | 向量列恒为空，情景记忆只有模糊匹配，没有语义检索 |
| 删除会话未实现 | `delete_session` 抛 `NotImplementedError`，也没有对应的接口 |

---

## 9. 未来工作

- [ ] 多用户隔离（个人版暂不需要）
- [ ] 跨设备同步（端到端加密）
- [ ] 联邦学习（不分享数据只分享模型）
- [ ] 自动 import / export 对话历史
- [ ] Obsidian 兼容（双向同步 .md 文件）
