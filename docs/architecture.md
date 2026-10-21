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
State { query, query_understanding?, candidates?, evidence?, draft_answer?, confidence?, conflicts?, final_answer?, trace }

Node 1  understand   LLM(temp=0)   → {intent, topic, file_hint, time_hint}
Node 2  localize     文件名匹配 + 向量召回聚合  → candidates
Node 3  extract      候选文件内混合检索 + Reranker → evidence + draft_answer
Node 4  answer       置信度自评 + 冲突检测 + 强制引用模板 → final_answer
Router  证据 < 2 或 top_score < 0.4  → refusal_node
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
```

写入 `conflicts` 表，下次遇到同主题自动提示。

### 决策 3：检索层

- **混合检索**：`score = α*vector_sim + β*bm25 + γ*meta_boost`（α=0.65, β=0.35）
- **bge-m3 输出 dense 向量** → LanceDB 向量召回
- **Tantivy FTS + jieba** → BM25 召回（数字、CJK 准确）
- **Reranker**：bge-reranker-v2-m3 重排 top-10
- **置信度公式**：

```
confidence = 0.4*(top1/max) + 0.3*(top1-top2) + 0.2*LLM自评 + 0.1*(evidence_count≥3)
```

- **拒绝阈值**：`confidence < 0.4` 或 evidence 数 < 2 → 拒绝 + 列出"最可能位置"

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
  "prompt_summary": "#a1b2c3d4e5f6g7h8 [user] 发票上的金额是多少?",
  "response_summary": "根据您提供的发票,金额为 1280 元。",
  "prompt_tokens": 256,
  "completion_tokens": 128,
  "duration_ms": 1240.5,
  "success": true,
  "error": null
}
```

**关键设计**：
- `prompt_summary` 前缀 `#<16hexhash>` 是 prompt 的 SHA-256 前 16 字符，用于同 prompt 查重；**不持久化原始文本**
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

## 9. 测试覆盖

```
$ PYTHONPATH=src/backend pytest tests/ -q
122 passed in 30s
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
