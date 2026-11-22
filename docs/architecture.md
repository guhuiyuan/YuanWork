# YuanWork 架构设计

> 详细架构图、3 步推理时序、整合层数据流、Wiki ↔ RAG ↔ Memory 关系
> 最近更新：2026-10-19（Stage 3 完成）

---

## 1. 系统总览

```
┌────────────────────────────────────────────────────────────────────┐
│                   Tauri 桌面端 (Rust + Webview)                     │
│   ChatWindow · KB Panel · WikiTree · WikiGraph · Settings · Audit  │
└────────────────────┬───────────────────────────────────────────────┘
                     │ Tauri IPC + HTTP localhost:8765 + SSE
                     ▼
┌────────────────────────────────────────────────────────────────────┐
│                       Python 后端 (FastAPI)                         │
│                                                                    │
│   API routes ─► Agent Orchestrator ─► 3 步推理状态机                │
│      │                │                                             │
│      │                ├─► Retrieval Engine                          │
│      │                │     ├─ Tantivy FTS (BM25 + jieba)           │
│      │                │     ├─ LanceDB (bge-m3 dense 1024-dim)      │
│      │                │     └─ bge-reranker-v2-m3 (top-10)          │
│      │                │                                             │
│      │                ├─► Consolidation Layer                       │
│      │                │     ├─ Writer (episodic + profile)          │
│      │                │     ├─ Conflict detector (LLM vs KB)        │
│      │                │     └─ Reflection (idle / turn threshold)  │
│      │                │                                             │
│      │                └─► Wiki Compile Trigger                      │
│      │                      └─► PageGenerator (LLM → Markdown)      │
│      │                                                                │
│      ├─► LLM Gateway (LiteLLM)                                       │
│      │     ├─ Routing (task → model alias)                            │
│      │     ├─ Streaming + complete                                    │
│      │     └─ AuditLogger (every call)                                │
│      │                                                                │
│      └─► Privacy Guard                                                │
│            └─ socket-layer domain whitelist (LLM_ONLY / OFFLINE)      │
│                                                                    │
├────────────────────────────────────────────────────────────────────┤
│                    Memory Substrate (本地)                          │
│  SQLite (WAL) ── episodic / profile / wiki_meta / conflicts         │
│  LanceDB ────── semantic chunks (dense vectors + metadata)         │
│  Tantivy ────── full-text inverted index (BM25)                    │
│  JSONL ──────── audit log (<data_dir>/logs/audit.jsonl)            │
└────────────────────────────────────────────────────────────────────┘
                     │
                     ▼ (受白名单约束)
            Cloud LLM (OpenAI / Anthropic / DeepSeek / MiniMax / Ollama)
```

---

## 2. 关键设计决策

### 决策 1：3 步推理采用自研状态机（不是 LangGraph）

**理由**：单次 prompt 把"理解+定位+回答"压在一起，模型会偷懒（直接答不引用）。分步可控、可观测、可用不同模型/温度。

```
State { query, query_understanding?, candidates?, evidence?, confidence?, gate?, conflicts?, final_answer?, trace }

Node 1  understand   LLM(temp=0)   → {intent, topic, file_hint, time_hint}
Node 2  localize     文件名匹配 + 向量召回聚合  → candidates
Node 3  extract      候选文件内混合检索 + Reranker → evidence
Node 4  gate         证据闸：硬规则（条数 + 每条可定位）+ 模型自评 → 通过 / 拒答
Node 5  answer       生成 + 冲突检测 + 强制引用模板 → final_answer
Router  证据闸任一侧不过  → refusal_node
```

| 维度 | 自研状态机 | LangGraph |
|---|---|---|
| 依赖体积 | 0 | +~80MB |
| 学习曲线 | 会 Python 就行 | StateGraph/Node/Edge/Checkpoint |
| 调试 | pdb / print | LangSmith / 本地 trace |
| 检查点/时间旅行 | 自己实现 | 内置 |
| 维护 | 自己维护 | 社区维护 |

**触发升级**：反思循环需回退 / 多 Agent 投票 / 真正需要检查点。

### 决策 2：整合层（最关键差异化）

数据流：

```
对话轮次结束
    │
    ▼
Writer → 摘要 (LLM) + 主题/实体抽取 + 重要性打分
    │
    ▼
ProfileStore → 去重 + 冲突解决 (KB 优先)
    │
    ▼
EpisodicMemory → 向量化 + 入库
    │
    ▼
触发巩固检查 (turns ≥ 10 OR idle ≥ 30min)
    │
    ▼
Reflection → 去重 (相似度 > 0.92) + 摘要 + 过期归档
```

**冲突解决**：

```
[冲突提示] 你的知识库《xxx》第 N 页写的是「A」，而通用模型回答的是「B」。
已以知识库为准，请确认是否需要更新知识库。

## 以知识库为准
[1] 《xxx》（第 N 页）
<知识库原文>

## 模型的原答（与知识库不一致，仅作对照）
<模型原答>
```

判定失败（模型联系不上）时同样以知识库原文作答，但**不附模型原答**——
没被核对过的说法没有资格当结论。

写入 `conflicts` 表（分歧点、双方说法、出处、判定说明），下次遇到同主题自动提示。

### 决策 3：检索层

- **混合检索**：`score = α*稠密 + β*全文`（α/β 是**配置项**：`YUANWORK_FUSION_ALPHA` /
  `YUANWORK_FUSION_BETA`，两者相加为 1.0；默认 0.65 / 0.35）
- **bge-m3 输出 dense 向量** → LanceDB 向量召回
- **Tantivy FTS + jieba** → BM25 召回（数字、CJK 准确）
- **Reranker**：bge-reranker-v2-m3 重排 top-10
- **置信度公式**（只是给用户看的信号，不参与是否作答的判断）：

```
confidence = 0.4*norm_top + 0.3*norm_gap + 0.2*evidence_bonus + 0.1*min(1, n/5)
```

  归一化区间按**融合分的实际区间**标定：两路各自按自身最大值归一后相加，最好的一条命中
  落在 `[max(α,β), α+β]` 之间（只有一路找到 → 较大权重；两路都找到 → 权重之和）。

- **证据闸（是否作答由它决定）**：
  - 硬规则闸：证据条数 ≥ 2 **且** 每条证据都能定位到原文位置
  - 模型自评闸：模型判断这些证据能否回答该问题（`evidence_gate` 提示词，
    JSON 回答；调用失败或读不出结论一律按不通过处理）
  - 任一不过 → 拒绝 + 列出"最可能位置"
  - 硬规则不过时不再调用模型（规则已经定了，省一次调用）
  - 模型自评的等待有上限（`DEFAULT_GATE_TIMEOUT`），闸门不会无限期挂住

  原来的单一数值阈值（`confidence < 0.4`）已废止：它几乎从不触发，
  系统对再薄的证据也会照答——这正是双闸要补的洞。

  「模型不可用」与「证据不足」是两回事，用户看到的文案也不同：
  前者说模型暂时不可用、证据位置仍然给出；后者说没找到足够明确的证据。

### 决策 4：Tauri ↔ Python 通信

- **进程模型**：Tauri 启动 Python 子进程（sidecar）
- **协议**：HTTP localhost + SSE 流式（不用 stdio，不用 Tauri IPC 跨语言）
- **关键 API**：
  ```
  POST /chat/stream           (SSE: trace, token, answer, citations, refusal, conflict, done)
  POST /ingest/dir, /ingest/file
  GET  /kb/files, DELETE /kb/files/{doc_id}
  GET  /memory/{episodic,profile,conflicts}
  POST /memory/consolidate
  GET  /wiki, /wiki/{page_id}, /wiki/graph
  GET  /audit, /audit/stats
  ```

### 决策 5：文档解析

| 格式 | 工具 | 分块 | 关键元数据 |
|---|---|---|---|
| PDF | pdfplumber + PyMuPDF（扫描） | 按页 → 段落 → 表格 | `page`, `is_scanned` |
| Word | python-docx | 按 Heading | `heading_level` |
| PPT | python-pptx | 按 slide 分块 | `slide_number`, `slide_title`, `slide_total` |
| Excel | openpyxl | 每 sheet 每表 | `sheet_name` |
| 图片 | rapidocr-onnxruntime | **per-line + 全文** | `image_path`, `confidence`, `box` |

**图片 OCR 关键点**（Stage 3）：单张图片按行切分 + 全文合并块，每行一个 chunk（`[OCR] <text>`）+ 一个全文块（`[OCR全文] <text>`），避免 1-chunk image 被 chat 拒绝（`evidence_count < 2`）。

### 决策 6：复用（不重复造轮子）

| 需求 | 选型 |
|---|---|
| 文档解析 | pdfplumber, python-docx, python-pptx, openpyxl, rapidocr-onnxruntime |
| 向量库 | LanceDB（嵌入式，bge-m3 搭档） |
| Embedding | bge-m3（FlagEmbedding，本地） |
| 全文检索 | Tantivy（Rust BM25）+ jieba（CJK 分词） |
| Reranker | bge-reranker-v2-m3 |
| LLM 网关 | LiteLLM（YAML 配置即切换） |
| 文件监听 | watchdog |
| 桌面 | Tauri 2.x + React + TypeScript |
| API | FastAPI + 原生 sqlite3 |

**不引入**：LangChain/LlamaIndex（绑架代码）、LangGraph（自研）、SQLAlchemy（过重）。

### 决策 7：隐私设计

详见 [docs/privacy.md](privacy.md)。要点：

| 数据 | 存储 | 发云 |
|---|---|---|
| 文档原文 | 不入库（解析不持久化） | ❌ |
| 向量 + 元数据 | 本地 LanceDB | ❌ |
| 对话历史 | 本地 SQLite | ❌ |
| 当前查询文本 | 临时 | ✅ |
| 召回 chunks | 临时 | ✅ |
| Prompt 哈希 | 审计日志 | ❌ |

---

## 3. Stage 3 Tantivy FTS

**为什么不用 bge-m3 sparse？**

实测 bge-m3 的 `lexical_weights` 在「1280」「2026」这类纯数字查询上返回 sparse=0.0，因为 bge-m3 把 `1280` 和 `1280元` tokenize 成不同单元，literal-token 匹配走 bge-m3 自己的小头而不是 inverted index。

| 查询 | v2 dense | v3 bge-sparse | **v4 Tantivy FTS** |
|------|----------|---------------|--------------------|
| 发票上的金额是多少 | 0.591 | 1.000 | 0.988 |
| 金额 | 0.538 | 1.000 | **1.000** |
| **1280** | 0.439 | **0.000** | **1.000** |
| **2026** | - | **0.000** | **1.000** |
| **上海元工作** | - | ~0.3 | **1.000** |

**实现**：`src/backend/yuanwork/retrieval/fts.py` 封装 tantivy 0.26 + jieba 0.42：

- Schema：`id` (raw) / `doc_id` (raw) / `text` (default tokenizer on jieba-segmented input)
- `add_documents` / `delete_by_ids` / `delete_by_doc_id` / `search` / `reindex_from_chunks` / `count`
- 线程安全（threading.Lock）
- 懒加载：首次访问时若 FTS 为空但 LanceDB 有行 → 自动从 LanceDB 重建
- 写入时先 `delete_by_ids` 再 add，避免重导入翻倍

**集成点**：`SemanticMemory.search_hybrid()` 用 FTS 替换原来的 in-Python sparse dot product；`add_chunks` 双写 LanceDB + FTS；`delete_by_doc_id` 双删。

---

## 4. Stage 3 网络隔离

**为什么是 socket 层而不是 LiteLLM 回调？**

Socket 层覆盖所有出站（含 embedding 下载、未来扩展的 HTTP 客户端），LiteLLM 回调只看得到 LLM 自身。

**模式**（`YUANWORK_NETWORK_MODE`）：

| 模式 | 行为 | 适用 |
|---|---|---|
| `0` allow_all | 不 patch socket，escape hatch | 开发调试 |
| `1` llm_only | 白名单 + localhost（**默认**） | 日常个人 KB |
| `2` offline | 全部拦截（含 localhost） | 离线/敏感场景 |

**域匹配**：`exact` OR `endswith('.' + d)`，右锚定。

**UX**：拦截时抛 `NetworkIsolationError`（`PermissionError` 子类），消息含 hostname、当前 mode、allowed domains、加白名单指引。

**E2E 验证**：
```
socket.create_connection(('evil.com', 443), timeout=0.1)
→ NetworkIsolationError: Network isolation: outbound to 'evil.com' is blocked.
  Network mode: LLM_ONLY
  Allowed domains: ['api.openai.com', 'api.anthropic.com', ...]
  To allow this domain, add it to settings.allowed_domains ...
```

---

## 5. Stage 3 审计日志

每次 LLM 调用记一行到 `<data_dir>/logs/audit.jsonl`：

```json
{
  "timestamp": "2026-10-19T12:34:56",
  "type": "llm_call",
  "model": "MiniMax-chat",
  "task_type": "answer",
  "prompt_hash": "a1b2c3d4e5f6g7h8",
  "prompt_length": 14,
  "prompt_tokens": 256,
  "completion_tokens": 128,
  "duration_ms": 1240.5,
  "success": true,
  "error": null
}
```

**关键设计**：
- `prompt_hash` 是 prompt 的 SHA-256 前 16 字符，用于同 prompt 查重；**不持久化原始文本**，条目里只有哈希、长度、令牌数与耗时
- `try/finally` 保证成功和失败都写日志
- 日志写本身 `try/except` 包住，永不让日志失败拖垮聊天
- 流式响应累加文本，从最后一个 chunk 读 usage
- API：`GET /api/v1/audit?limit=100` / `GET /audit/stats` / `DELETE /audit`

**E2E 验证**：
```
POST /chat → 触发 LLM 调用 → 审计 +1 条
GET /api/v1/audit/stats → {llm_calls: 1, by_model: {MiniMax-chat: 1}, total_tokens: 384}
DELETE /api/v1/audit → {cleared: 1}
```

---

## 6. 启动细节

### Windows

```bash
PYTHONIOENCODING=utf-8 \
PYTHONPATH=src/backend \
YUANWORK_DATA_DIR=./data \
.venv/Scripts/python.exe -m yuanwork --port 8765
```

> Windows 默认 stdout 是 GBK，emoji 启动日志会崩溃。设置 `PYTHONIOENCODING=utf-8` 解决。

### macOS / Linux

```bash
PYTHONPATH=src/backend \
YUANWORK_DATA_DIR=./data \
python -m yuanwork --port 8765
```

### 预热 embedding 模型（可选）

默认 embedding 模型懒加载（首次使用 bge-m3 时下载 + 加载约 1-3 分钟）。启动时预热：

```bash
YUANWORK_PREWARM_EMBEDDING=1 python -m yuanwork
```

---

## 7. 数据生命周期

```
[文档文件] ── parse ──► [Block 对象] ── chunk ──► [Chunk]
                                                       │
                                                       ├─ embed (bge-m3)
                                                       │     │
                                                       │     ▼
                                                       │   [LanceDB row]
                                                       │
                                                       └─ segment (jieba) + index
                                                                 │
                                                                 ▼
                                                            [Tantivy doc]

[聊天] ── hybrid search ──► [chunks] ── rerank ──► [top-K] ── LLM ──► [answer + citations]
```

所有路径上**原始文档文本只存在于内存中**，不写盘。

### 7.1 摄取层的已知边界

以下几条是**当前代码的实际行为**，不是设计意图。改动摄取链路前请先读这一节
（现状基线规范见 `specs/ingestion/`）：

| 现状 | 后果 |
|---|---|
| 源文件内容被修改后重新摄取 | 旧 `doc_id` 的文件元数据行、向量库行与全文索引条目**全部保留**——摄取层没有任何清理路径，旧版本成为孤儿数据 |
| 重摄取只按"本次要写的切片 id"删除 | 切片 id 含文本前 100 字符，改动正文即换 id，旧切片永远不会被删 |
| `doc_id = sha256(绝对路径 + 内容哈希)` | 文件改名或移动即换 `doc_id`；而去重只看内容哈希，改名后同一份内容会同时存在两个条目 |
| 同一内容出现在两个路径 | 只保留**首次摄取**的那条记录，第二个路径不建文件元数据行 |
| 文件监听（watcher） | **未接入**：debounce 与重新摄取逻辑是死代码，`FileWatcher` 从未被实例化，也不在启动流程里。上文"监听 → 解析 → 分块"的数据流目前只由手动摄取触发 |
| `.doc` / `.ppt` / `.xls` | **不再尝试**（TASK-091）：已从解析器的扩展名清单里摘出去，按 proposal.md §2.2 归入「格式不支持」那一档，理由说清该另存为什么 |
| 扫描版 PDF | 只产出 `[Scanned page - no text extracted]` 占位块，**不做 OCR** —— OCR 只接在图片上（独立图片文件与文档里嵌的图片：`parser/image_parser.py` + `parser/ocr.py`），**PDF 里的扫描页还没接** |
| 文本文件编码 | UTF-8 解码失败即按 GBK 解码并忽略非法字节，二进制文件会被当作文本入库 |
| 切块尾部 | 过短的切片**并入相邻切片**（`structural_chunker._merge_short_chunks`），放不下的才单独留着 —— 文档末尾的短句不再被丢弃（ADR-005） |
| 全文索引目录被锁 | tantivy 写者在进程存活期间持有索引目录句柄，Windows 上索引目录删不掉、索引重建会失败（仅告警）。用临时目录跑索引测试时目录清理可能失败——`test_fts.py::test_semantic_memory_migrates_existing_fts` 在 Windows 上是 flaky 的 |

---

## 8. 模块清单

| 模块 | 文件 | 状态 |
|---|---|---|
| 文档解析 | `src/backend/yuanwork/ingestion/parser/` | ✅ PDF/DOCX/PPTX/XLSX/MD/PNG |
| OCR | `ingestion/parser/image_parser.py` | ✅ rapidocr-onnxruntime, per-line + 全文 |
| 向量存储 | `memory/semantic.py` + `retrieval/fts.py` | ✅ LanceDB + Tantivy |
| 情景记忆 | `memory/episodic.py` | ✅ |
| 用户画像 | `memory/profile.py` | ✅ |
| LLM 网关 | `llm/gateway.py` | ✅ + 审计 |
| 混合检索 | `retrieval/hybrid.py` | ✅ |
| Reranker | `retrieval/reranker.py` | ✅ bge-reranker-v2-m3 |
| 3 步推理 | `orchestrator/agent.py` + `steps/` | ✅ |
| 整合层 | `consolidation/{writer,conflict,reflection,engine}.py` | ✅ |
| Wiki 层 | `wiki/{page_generator,linker,entity_extractor,trigger}.py` | ✅ |
| 网络隔离 | `privacy/network_guard.py` | ✅ socket 层 |
| 审计日志 | `privacy/audit.py` | ✅ + API 路由 |
| Tauri 壳 | `src/app/src-tauri/src/` | ✅ |
| 前端 UI | `src/app/src/components/` | ✅ 11+ 组件 |

---

## 8.5 对外接口清单

后端共 **38 个端点**，全部挂在 `/api/v1` 前缀下，按 9 个标签分组。完整契约见
`specs/yuanwork/proposal.md` §3 与 `specs/yuanwork/design.md` §3。

| 标签 | 方法 | 路径 | 说明 |
|------|------|------|------|
| health | GET | `/api/v1/health` | 4 个组件的状态与总体状态 |
| health | GET | `/api/v1/version` | 版本、Python 版本、平台 |
| ingest | POST | `/api/v1/ingest/dir` | 提交目录摄取任务（后台执行），返回任务标识 |
| ingest | POST | `/api/v1/ingest/file` | 提交单文件摄取任务 |
| ingest | POST | `/api/v1/ingest/rebuild` | **按工作域条目**建库（桌面端建库的入口） |
| ingest | GET | `/api/v1/ingest/status/{job_id}` | 查询任务进度 |
| kb | GET | `/api/v1/kb/files` | 列出知识库文件 |
| kb | DELETE | `/api/v1/kb/files/{doc_id}` | 删除一个文件（**只删索引，不动磁盘文件**） |
| kb | POST | `/api/v1/kb/files/{doc_id}/tier` | 指定文档属于核心层还是长尾层 |
| kb | GET | `/api/v1/kb/tiers` | 文档的分层结果 |
| kb | GET | `/api/v1/kb/stats` | 知识库统计 |
| kb | GET | `/api/v1/kb/terms/{term}/context` | **术语的原文上下文**与位置；查不到就标记未找到，不生成解释 |
| chat | POST | `/api/v1/chat/stream` | **流式问答**，SSE 逐事件推送 |
| chat | POST | `/api/v1/chat` | 阻塞问答，返回完整结果 |
| chat | POST | `/api/v1/chat/cancel/{session_id}` | 取消正在生成的答案 |
| memory | GET | `/api/v1/memory/episodic` | 情景记忆列表（条数 1..100） |
| memory | GET | `/api/v1/memory/profile` | 用户画像列表 |
| memory | DELETE | `/api/v1/memory/profile/{key}` | 删除一个画像键 |
| memory | GET | `/api/v1/memory/conflicts` | 冲突日志列表（条数 1..500） |
| memory | GET | `/api/v1/memory/rules/pending` | **待审的规律**（用户确认前不计入作答证据） |
| memory | POST | `/api/v1/memory/rules/{entity_id}/approve` | 用户确认一条规律，使其生效 |
| memory | POST | `/api/v1/memory/consolidate` | 触发一次整合 |
| wiki | GET | `/api/v1/wiki/pages` | 页面列表（条数 1..500） |
| wiki | GET | `/api/v1/wiki/pages/{page_id}` | 页面详情（路径参数可含斜杠） |
| wiki | PUT | `/api/v1/wiki/pages/{page_id}` | **编辑页面正文**：改过的段落归用户，之后重编译不覆盖 |
| wiki | GET | `/api/v1/wiki/pages/{page_id}/blocks` | 页面正文按段落的**归属**（模型写的 / 用户写的） |
| wiki | GET | `/api/v1/wiki/pages/{page_id}/backlinks` | 反向链接 |
| wiki | GET | `/api/v1/wiki/pages/{page_id}/conclusions` | 页面的结论与各自的出处锚点 |
| wiki | GET | `/api/v1/wiki/graph` | 页面与实体图谱 |
| wiki | POST | `/api/v1/wiki/compile/{topic}` | 手动编译一个页面 |
| audit | GET | `/api/v1/audit` | 审计条目（条数 1..1000） |
| audit | GET | `/api/v1/audit/stats` | 审计统计 |
| audit | DELETE | `/api/v1/audit` | 清空审计 |
| workspace | POST | `/api/v1/workspace/folders` | 把一个文件夹加入工作域 |
| workspace | POST | `/api/v1/workspace/files` | 把单个文件加入工作域 |
| workspace | GET | `/api/v1/workspace/entries` | 工作域条目清单 |
| workspace | DELETE | `/api/v1/workspace/entries/{entry_id}` | 移除一个工作域条目，并清掉它带进来的索引 |
| interactions | POST | `/api/v1/interactions/{kind}` | 记录一次交互（提问 / 引用 / 采纳 / 否定 / 增删文件） |

### 已知边界

- **无鉴权、无传输加密**：全部端点不校验身份，服务只监听本机回环地址。
- **两个桩端点**：`/chat/cancel/{session_id}` 与 `/wiki/compile/{topic}` 曾长期返回 200
  却不做事；现在取消与手动编译都已真正生效（取消走 `cancel_chat`，手动编译真的编译）。
- **摄取任务状态存关系库**：后端重启不丢进度（ADR-022），桌面端轮询照常拿得到。
- **异常文本会回显**：全局异常处理器与阻塞问答端点都把异常原文放进响应体，
  可能带出本地路径与上游错误细节。
- **`detail` 键有两种形状**：未捕获异常时是字符串，参数校验失败时是对象列表。

---

### 8.6 检索层的已知边界

| 现状 | 后果 |
|---|---|
| 融合用「通道内最大值归一化 + 加权和」，不是 RRF | 每通道第一名恒为 1.0，所以融合分的实际区间是 `[max(α,β), α+β]` 而不是稠密相似度的区间；**数值置信度已不再参与是否作答的判断**，作答由证据闸决定 |
| 融合权重曾是硬编码常量 | 已改为配置项（`YUANWORK_FUSION_ALPHA` / `YUANWORK_FUSION_BETA`），可以随语料重新标定 |
| 重排模型在聊天链路从未启用 | `use_reranker` / `reranker_model` / `top_k_rerank` 三个配置项全是死配置 |
| 置信度阈值配置未接线 | `confidence_threshold_high/low` 改了不生效，打分器用内置的 0.7 / 0.4 |
| 嵌入批大小已接线，但不再是「计划」而是「上限」 | `YUANWORK_EMBEDDING_BATCH_SIZE` 默认 **0 = 设备自己定**：批按设备实测的预算切（显卡看显存、CPU 看核数），填一个数就是不超过这么多（ADR-044） |
| 无相关性下限 | 与问题完全无关的查询照样返回满额证据 |
| 全文通道按**词元**匹配，不是子串 | jieba 把「合同条款」切成一个词，查询「合同」就找不到它 |
| 稠密通道对退化查询不短路 | 空串、纯空白、纯标点的查询仍会返回最近邻（只有全文通道会短路） |
| 稀疏命中逐条回表 | 每条全文命中各做 1 次全表扫描，单次查询最多 50 次 |
| 向量库无近似最近邻索引 | 稠密检索是暴力全表扫描，知识库变大后线性变慢 |
| 检索链路无任何超时 | 向量库或嵌入模型卡住不会被主动中断 |

---

### 8.7 模型网关与编排层的已知边界

| 现状 | 后果 |
|---|---|
| 「强制引用 / 页码」只是提示词里的软要求 | 已收紧：引用编号会校验（越界或没有引用即转拒答），引用列表只保留答案真正用到的证据；但**代码不校验页码本身** |
| 模型自述的「证据不足」不被识别 | `answer.j2` 要求模型在证据不足时明说，但那段文字会作为普通答案返回，客户端无法区分"模型自己拒答"与"正常回答" |
| 冲突对象的字段名与响应模型不一致 | 已对齐：agent 直接产出响应模型要的 `llm_answer` / `kb_evidence`，HTTP 响应里冲突的两边说法不再是空字符串 |
| 冲突检测在答案发完之后才跑 | 流式路径下用户先收到完整答案，之后才收到 conflict 事件 |
| 拒答时引用被清空 | 证据确实存在，但引用列表为空 |
| 没有重试、限流、熔断 | provider 抖动直接变成用户可见的 `generation_error` |
| 令牌预算从不强制 | `per_session_max_tokens` / `per_query_max_tokens` / `monthly_cap_usd` 三个配置项无任何检查代码 |
| JSON 模式在 Anthropic 协议下不强制 | 只带了 `response_format`，没有 schema，模型返回非 JSON 时靠"去围栏 + 解析失败兜底"处理 |
| 抽取任务的路由键是死的 | 路由表里有 `extract`，但没有任何调用使用它——抽取步骤是纯检索 |
| 历史与选项参数被接收但不使用 | `history` / `opts` 一路传进编排层，从不被读取；**多轮上下文目前不生效** |
| 追踪模块是死代码 | `orchestrator/trace.py` 从未被调用；真正起作用的追踪直接随 API 响应返回客户端（含完整理解结果） |
| 生成失败时异常原文进入拒答原因 | 客户端会收到 provider 端点、请求片段等内部细节 |
| 离线模式自相矛盾 | 网关把模型改写为 `ollama/llama3.1:8b`，而网络守卫在离线模式下**连 localhost 也拦** |

---

### 8.8 桌面端的已知边界

| 现状 | 后果 |
|---|---|
| **Wiki 页面的字段在桥接层被裁剪** | Rust 侧的 `WikiPage` 只保留 4 个字段，前端类型却声明了 `tags`；在 Wiki 树筛选框输入任意字符即抛未捕获异常，而前端**没有错误边界** → 整棵树卸载 → **白屏** |
| 前端没有错误边界 | 任何渲染期异常都会卸载整棵组件树，用户只看到白屏，无法从 webview 恢复 |
| 子进程的 stdout / stderr 被管道接管却无人读取 | 写满操作系统管道缓冲区（Windows 上约 64 KB）后，**后端进程会永久阻塞** |
| 后端进程没有被监控 | 没有 `try_wait`、没有退出通知、没有自动重启；后端崩了桌面端毫无察觉 |
| release 构建是 `panic = "abort"` | 任一 Rust panic 直接终止进程，`Drop` 不执行 → **Python 后端可能变成孤儿进程** |
| 文件访问作用域模块从未接线 | `fs_scope.rs` 的路径白名单零调用；目录路径从原生对话框直达后端，后端只校验「存在且是目录」 |
| 打包未声明 `bundle.resources` | `sidecar.rs` 的「随包发布」分支不可达；**当前唯一可用的形态是源码树 + 项目 `.venv`** |
| 16 个命令中有 6 个不检查 HTTP 状态码 | 后端 500 会被当成「空结果」返回给前端（知识库文件列表与删除命令是检查了的，写法不一致） |
| 两个 Wiki 命令的 `page_id` 未做 URL 编码 | 含 `#` / `?` / `%` 的页面标识会拼出错误 URL |
| WebView 没有内容安全策略（CSP 为 null），同时授予了 shell 权限 | 注入面比必要的大 |
| Tauri 路径无法中止流式回答 | Rust 侧没有取消通道，后端虽然有 `/chat/cancel/{session_id}` 但桌面端没接；浏览器路径有 `AbortController`，两条路径能力不一致 |
| 两条路径的 SSE 非 JSON 载荷形状不同 | Rust 侧退化成裸字符串，浏览器侧退化成 `{raw}` 对象 |
| 取消索引只停前端轮询 | 后台的摄取任务**不会被终止**，仍在写库 |
| 16 个命令中 4 个前端从不调用 | `kill_backend` / `get_api_key` / `set_api_key` / `app_info` 已注册但无人使用；`secrets::delete_secret` 同样零调用 |
| 对话历史既不保留也不发送 | 前端每轮只发当前问题，`session_id` 也每次新建；**多轮上下文在桌面上不生效** |
| 引用不可点击 | 只能看到文件名与页码，无法跳回原文位置 |
| 没有鉴权、没有 TLS | 桌面端与后端之间是本机回环上的明文 HTTP |

---

## 9. 测试覆盖

```
$ .venv/Scripts/python.exe -m pytest src/backend/tests -q
1661 passed in 114s

$ npm --prefix src/app test
127 passed          # Node 内置 test runner，零新依赖

$ cd src/app/src-tauri && cargo test
68 passed
```

- 解析层：6 类格式各 3-6 个测试
- 检索：FTS / hybrid / reranker / 置信度
- Orchestrator：3 步推理各步骤 + refusal
- Consolidation：writer / conflict / reflection
- Wiki：page generation / linking / graph
- Privacy：network guard (17 tests) / audit (17 tests)
- 集成：完整 chat 流（含 mock LLM）

---

## 10. 未来工作

详见 [CHANGELOG.md](../CHANGELOG.md) 与 GitHub Issues。

- Wiki 全文本搜索
- Wiki 静态站点导出
- 多模态 Embedding（图、文统一）
- 服务端 GPU 加速
- Obsidian 插件
- 移动端
