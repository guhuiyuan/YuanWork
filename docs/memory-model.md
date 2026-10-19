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
| chunks | 与文档同步 | 文档删除时自动清 |
| wiki_pages | 永久 | 手动 `DELETE /api/v1/wiki/{id}` |
| conflicts | 永久 | 手动 |
| audit.jsonl | 永久 | `DELETE /api/v1/audit` |
| profile | 永久（带 expires_at） | 反思时自动过期 |

---

## 7. 未来工作

- [ ] 多用户隔离（个人版暂不需要）
- [ ] 跨设备同步（端到端加密）
- [ ] 联邦学习（不分享数据只分享模型）
- [ ] 自动 import / export 对话历史
- [ ] Obsidian 兼容（双向同步 .md 文件）
