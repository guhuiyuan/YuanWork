# YuanWork · 本地个人工作助手

> **Wiki + 记忆工程 + RAG** 的桌面级本地工作助手
> 为严格场景（合同条款、规章法条）而生的本地知识库问答系统

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Status: Stage 3 done](https://img.shields.io/badge/Status-Stage%203%20done-brightgreen)]()
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue)]()
[![Tests: 137 passed](https://img.shields.io/badge/Tests-137%20passed-brightgreen)]()
[![Tauri 2.x](https://img.shields.io/badge/Tauri-2.x-orange)]()

[English](#english-summary) · [中文](#中文)

---

## 它是什么

YuanWork 是一款**完全本地**的个人工作助手，专为以下场景设计：

- 你有一堆合同 / 法规 / 项目文档放在本地文件夹
- 你问「**这份合同里违约金怎么约定？**」
- YuanWork 会**精确定位**到那份合同，**引用第 7 页原文**回答你
- 如果知识库的内容与 LLM 训练知识有冲突，**以你的知识库为准**并显式提醒
- 如果置信度低，**直接拒绝回答**并提示最可能位置

它不是又一个通用 RAG：它把 **wiki 形态（结构化页面 + 双向链接 + 实体图谱）** 和 **记忆工程（情景 + 语义 + 整合层）** 缝在一起。

---

## 核心特性

| 模块 | 能力 |
|---|---|
| **多格式解析** | Word / PDF / **PPT (按 slide)** / Excel / Markdown / TXT / 图片 OCR (rapidocr-onnxruntime) |
| **本地向量库** | bge-m3 (本地嵌入) + LanceDB (嵌入式存储) |
| **混合检索** | Tantivy BM25 (CJK + 数字) + bge-m3 dense + 元数据 + Reranker |
| **3 步推理** | 理解 → 定位 → 抽取 → 回答（带完整 trace） |
| **流式输出** | SSE 逐 token 推送回答，事件循环全程不阻塞（健康检查 / 其他对话不受影响） |
| **强制引用** | 高置信答案必带原文 [n] 引用 + 页码/slide 号 |
| **拒绝策略** | 低置信时拒绝直接回答 + 列出「最可能位置」 |
| **冲突解决** | LLM 训练知识 vs 知识库 → KB 优先 + 显式提醒 |
| **整合层** | 写入控制 / 去重 / 冲突日志 / 自动反思 |
| **Wiki 形态** | LLM 把多 chunk 编译成结构化页面 + 双向链接 + 实体图谱 + d3 图谱视图 |
| **网络隔离** | Socket 层拦截 / 域白名单 / LLM_ONLY / OFFLINE / ALLOW_ALL 三种模式 |
| **审计日志** | 每条 LLM 调用记 model / task / 16字符 prompt 哈希 / tokens / 时长 / 成功/失败 |
| **多 LLM 适配** | LiteLLM + YAML 配置（OpenAI / Claude / DeepSeek / Ollama / MiniMax...） |

---

## 架构

```
Tauri 桌面端 (Rust + Webview, React + TS)
    │ Tauri IPC + HTTP localhost:8765 + SSE
    ▼
Python 后端 (FastAPI)
    │
    ├─ Agent Orchestrator  ──►  3 步推理 (理解→定位→抽取→回答)
    ├─ Consolidation Layer  ──►  整合层 (写入/冲突/反思)   【差异化】
    ├─ Retrieval Engine     ──►  Tantivy FTS + bge-m3 dense + Reranker + 置信度
    ├─ Wiki Layer           ──►  页面生成/链接/图谱         【差异化】
    ├─ LLM Gateway (LiteLLM)──► 路由 + 降级 + 预算 + 审计日志
    ├─ Privacy Guard        ──►  Socket 层域名白名单
    ▼
Memory Substrate: Episodic (SQLite) + Semantic (LanceDB + Tantivy FTS) + Profile
                  + Wiki (SQLite + Markdown)
Document Pipeline: 监听 → 解析 → 分块 → 嵌入 → 入库
Cloud LLM (受白名单约束)
```

完整架构与设计决策见 [docs/architecture.md](docs/architecture.md)。

---

## 快速开始

### 前置条件

- **Python 3.10+**
- **Node.js 18+**（仅当启动 Tauri 桌面端时需要）
- **Rust 1.70+**（仅当编译 Tauri 时需要）
- **Windows / macOS / Linux**

### 1. 启动 Python 后端

```bash
cd src/backend
pip install -e .                    # 或 uv sync
cp .env.example .env                 # 配置 LLM API key
python -m yuanwork --port 8765
```

> **Windows 注意**：stdout 默认 GBK，启动时需设置 `PYTHONIOENCODING=utf-8`。
> 完整启动命令见 [docs/architecture.md#启动](docs/architecture.md#启动)。

首次启动会下载 bge-m3 嵌入模型（约 2.3GB），请耐心等待。如果只跑单元测试，可以跳过这一步。

### 2. 启动 Tauri 桌面端（可选）

#### 2.1 直接跑 debug 二进制

```bash
# 项目根目录
./run.bat                        # Windows
# 或
cd "$(dirname "$0")" && ./D:/cargo-target/debug/yuanwork.exe   # bash
```

**前提**：

- 工具链：Rust 1.75+ (`stable-msvc`) + MSVC BuildTools 2022 + WebView2
- `CARGO_TARGET_DIR` 推荐设到非 C: 盘（首次编译约 5-10 GB）
- 项目 venv 在 `.venv/`（Sidecar 启动 Python 走 venv）

Sidecar 启动链路：

1. Tauri shell 在项目根目录启动（`run.bat` 自动 `cd`）
2. `find_python_interpreter()` 向上爬树找 `.venv/Scripts/python.exe`
3. `find_backend_module_dir()` 向上爬树找 `src/backend/`
4. spawn `python -m yuanwork --port 8765 --host 127.0.0.1`
5. 等待 `/api/v1/health` 返回 200（最长 30 秒）
6. 打开 WebView 显示前端

#### 2.2 开发模式（Vite HMR + 浏览器）

```bash
cd src/app
npm install
npm run tauri dev          # Tauri 壳（需要 tauri-cli + Rust）
# 或
npm run dev                # 仅 Vite，浏览器开 http://localhost:5173
```

### 3. 在 UI 中

1. 点击左侧「+ 添加」选择本地文件夹作为知识库
2. 等待索引完成
3. 在对话窗口提问，如：
   - 「这份合同里违约金怎么约定？」
   - 「上次我们讨论过的方案三是什么？」
   - 「总结这个产品 PPT 的核心功能」

### 4. 验证流式输出

```bash
# 端到端验证：启动真实后端，只 mock LLM provider，测量每个 SSE 帧到达客户端的时刻
./.venv/Scripts/python.exe scripts/e2e_stream_check.py
# → 8 passed, 0 failed
```

脚本会断言「第一个 token 至少比最后一个早 300ms 到达」，因此把整段回答攒到最后一次性吐出的实现无法通过。

也可以直接看后端的 SSE 流：

```bash
curl -N -X POST http://127.0.0.1:8765/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"query": "这份合同里违约金怎么约定？"}'
```

事件序列：

| 事件 | 含义 |
|---|---|
| `trace` | 推理步骤状态（理解 / 定位 / 抽取 / 回答 / 冲突检测） |
| `token` | **增量文本**，逐 token 推送 |
| `answer` | 完整回答全文（兜底用，正常情况下 `token` 已拼出全文） |
| `citations` | 引用列表 |
| `refusal` | 置信度不足，拒绝回答 |
| `conflict` | LLM 与知识库冲突 |
| `done` | 结束事件，携带完整 `ChatResponse` |

`done` 一定作为终止事件发出（即使中途出错），客户端据此区分「正常结束」与「被切断」。

---

## 配置

所有配置都通过环境变量（`.env`）或 `src/backend/configs/*.yaml` 加载。前缀统一为 `YUANWORK_`，详见 [.env.example](.env.example)。

### LLM (LiteLLM + YAML)

**当前默认只用 MiniMax 一个模型**（`MINIMAX_API_KEY` 是目前唯一配置了的 key），所有任务都路由到它。

编辑 `src/backend/configs/models.yaml`：

```yaml
models:
  minimax-chat:
    provider: anthropic
    api_base: https://api.minimaxi.com/anthropic
    api_key_env: MINIMAX_API_KEY
    model_name: MiniMax-M3

routing:
  understand: minimax-chat       # 理解
  extract: minimax-chat          # 抽取
  answer: minimax-chat           # 出答案
  conflict_check: minimax-chat   # 冲突检测
  reflection: minimax-chat       # 记忆反思
  wiki_compile: minimax-chat     # Wiki 页面编译
  ner: minimax-chat              # 实体识别
  general: minimax-chat          # 兜底
```

要换模型：取消对应 provider 的注释（`models.yaml` 里已把 OpenAI / Claude / DeepSeek / Ollama 都留成注释示例），在 `.env` 里设好对应的 `*_API_KEY`，再把 `routing` 指过去即可。**不需要改代码** —— 换 provider 只是换个 YAML。

LiteLLM 支持 OpenAI / Claude / DeepSeek / 智谱 / Ollama / MiniMax 等 100+ provider，详见 [LiteLLM 文档](https://docs.litellm.ai/)。

> 如果 `routing` 指向的模型没有配 key，gateway 会自动回退到第一个有 key 的模型（并在日志里提示），所以配错不会直接崩掉。

### 网络隔离模式

```bash
# .env
YUANWORK_NETWORK_MODE=llm_only    # 默认：仅允许 LLM provider 域名
# YUANWORK_NETWORK_MODE=offline   # 完全离线（仅本地模型）
# YUANWORK_NETWORK_MODE=allow_all # 不限制
```

`llm_only` 模式下，socket 层会拦截所有出站连接，仅放行 `allowed_domains` 列表中的域名 + localhost。详见 [docs/privacy.md](docs/privacy.md)。

### 审计日志

每次 LLM 调用都会记录到 `<data_dir>/logs/audit.jsonl`，包含：

- `timestamp` · `model` · `task_type`
- `prompt_summary`（前缀 `#<16hexhash>` 是 prompt 的 SHA-256 前 16 字符；用于去重，不含原始文本）
- `response_summary` · `prompt_tokens` · `completion_tokens` · `duration_ms`
- `success` · `error`（如失败）

通过 API 查看：

```bash
# 最近 100 条
curl http://localhost:8765/api/v1/audit

# 统计聚合
curl http://localhost:8765/api/v1/audit/stats

# 清空
curl -X DELETE http://localhost:8765/api/v1/audit
```

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 系统架构、模块划分、关键决策、启动细节 |
| [docs/privacy.md](docs/privacy.md) | 隐私设计：数据流图、网络隔离、审计日志、敏感字段处理 |
| [docs/memory-model.md](docs/memory-model.md) | 情景记忆 + 语义记忆 + 整合层 + Wiki 之间的数据流 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南、代码风格、PR 流程 |

---

## 项目结构

```
YuanWork/
├── LICENSE (Apache 2.0)
├── README.md
├── CHANGELOG.md
├── CONTRIBUTING.md
├── .env.example
├── .gitignore
├── docs/
│   ├── architecture.md
│   ├── privacy.md
│   └── memory-model.md
├── src/
│   ├── app/                  # Tauri 桌面端
│   │   ├── package.json, vite.config.ts
│   │   ├── src/              # React + TypeScript
│   │   │   ├── components/   # Sidebar, ChatWindow, WikiTree, WikiGraph, ...
│   │   │   └── api/backend.ts
│   │   └── src-tauri/        # Rust 壳
│   │       └── src/{main,sidecar,commands,fs_scope,secrets,sse}.rs
│   └── backend/              # Python 后端
│       ├── pyproject.toml
│       ├── configs/          # models.yaml
│       ├── yuanwork/
│       │   ├── api/          # FastAPI 路由 (chat/health/ingest/kb/memory/wiki/audit)
│       │   ├── db/           # SQLite + LanceDB
│       │   ├── llm/          # LiteLLM gateway
│       │   ├── memory/       # episodic / semantic / profile
│       │   ├── ingestion/    # 解析 / 分块 / 监听
│       │   ├── retrieval/    # hybrid (Tantivy FTS) / reranker / confidence
│       │   ├── orchestrator/ # 3 步推理 (agent + steps)
│       │   ├── consolidation/# 整合层 (writer / conflict / reflection)
│       │   ├── wiki/         # Wiki (page_generator / linker / entity / trigger)
│       │   └── privacy/      # network_guard / audit / redactor
│       └── tests/            # 单元 + 集成测试
└── scripts/                  # 端到端验证脚本
    └── e2e_stream_check.py   # 流式输出真流式验证
```

---

## 测试

```bash
cd src/backend
PYTHONPATH=src/backend python -m pytest tests/ -q
# → 137 passed
```

不依赖 LLM API key 也不下载 bge-m3 模型。覆盖：

- 文档解析（PDF / DOCX / PPTX / XLSX / Markdown / 图片 OCR）
- 混合检索（Tantivy FTS + dense 召回）
- 3 步推理（mock LLM）
- 整合层（冲突检测 / 反思）
- Wiki 页面编译
- 网络隔离（socket 层拦截）
- 审计日志（logger + API）
- 流式输出（gateway 不阻塞事件循环 / agent 逐 token 发事件 / SSE 帧格式 / 错误时补发 `done`）

Rust 侧还有 SSE 解析器的单元测试：

```bash
cd src/app/src-tauri && cargo test
# → 11 passed
```

---

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| **阶段 1：核心可用** | Tauri 骨架 / 文档解析 / 嵌入检索 / 3 步推理 / 强制引用 / 拒绝 / 对话 UI | ✅ 完成 |
| **阶段 2：差异化** | 整合层 / 冲突检测 / Reranker / 情景记忆 / Wiki 主动巡检 / 图谱视图 | ✅ 完成 |
| **阶段 3：开源准备** | Tantivy FTS / 网络隔离 / 审计日志 / 文档 | ✅ 完成 |
| **阶段 4：流式输出** | SSE 端到端真流式 / gateway 不阻塞事件循环 / 桌面端增量渲染 | ✅ 完成 |

---

## 关键设计决策

- **为什么 Wiki + RAG 而不是纯 RAG？** —— 纯 RAG 只能"问"，Wiki 让用户能"问" + "浏览"，跨文档形成结构化知识，添加双向链接和实体图谱。
- **为什么自研状态机而不是 LangGraph？** —— 3 步推理本质是线性 DAG + 1 个条件分支，自研 ~200 行足够清晰，避免 langchain 生态绑架。
- **为什么 bge-m3 + LanceDB？** —— bge-m3 同时输出 dense + sparse（虽然 sparse 对纯数字/CJK 弱，详见 [Stage 3 FTS 设计](docs/architecture.md#stage-3-tantivy-fts)），LanceDB 嵌入式单文件备份简单，两者 Apache 2.0 开源。
- **为什么 Tantivy FTS 而不是只用 bge-m3 sparse？** —— 实测 bge-m3 的 `lexical_weights` 在「1280」「2026」这类纯数字查询上返回 0.0。Tantivy + jieba 给真实 BM25 分数，召回率从 0.0 → 1.0。详见 [docs/architecture.md](docs/architecture.md)。
- **为什么 LiteLLM？** —— 100+ provider 统一接口，YAML 配置即切换，不锁死单一厂商。
- **为什么 Socket 层网络隔离而不是 LiteLLM 回调？** —— Socket 层覆盖所有出站（含 embedding 下载、未来扩展的 HTTP 客户端），LiteLLM 回调只看得到 LLM 自身。
- **为什么 prompt 用 SHA-256 哈希而不是原文？** —— 审计日志可以"同 prompt 查重"但**不持久化敏感文本**，符合「不持久化原文」的隐私承诺。

---

## 隐私承诺

- ✅ 文档原文**不持久化**，只存 chunks + 向量
- ✅ 向量 + 对话历史**全部本地**
- ✅ 仅当前查询 + 召回 chunks 发往云端 LLM
- ✅ Socket 层域名白名单，恶意库无法外泄
- ✅ 三种网络模式：默认 llm_only / 严格 offline / 宽松 allow_all
- ✅ API key 走环境变量 / LiteLLM YAML 解析 `api_key_env` 间接引用
- ✅ 审计日志只记 prompt 哈希和摘要，**不记原文**

详见 [docs/privacy.md](docs/privacy.md)。

---

## License

Apache 2.0 — 详见 [LICENSE](LICENSE)。

---

## 致谢

灵感来源：

- [海派程序猿的 CSDN 博客](https://blog.csdn.net/weixin_41455464/article/details/156490304) — 情景记忆 + 语义记忆 + 整合层
- **SiYuan 思源笔记** — 块级引用 + 双向链接
- **Obsidian** — 图谱视图
- **Karpathy wiki-rag 范式** — LLM 编译 chunks 成 Wiki
- **RAGFlow / Dify / Mem0** — 工程实践参考

---

## English Summary

YuanWork is a **local-first personal work assistant** that combines wiki technology (structured pages, bidirectional links, entity graph) with memory engineering (episodic + semantic + consolidation) on top of a 3-step RAG (understand → localize → extract → answer).

**Highlights**:

- 6+ document formats (Word / PDF / PPT slide-level / Excel / Markdown / image OCR)
- Local embeddings via bge-m3, vector storage via LanceDB, full-text search via Tantivy + jieba
- Mandatory source citations on high-confidence answers, refuse + suggest location on low confidence
- KB-primary conflict resolution: if the LLM's training knowledge disagrees with your KB, KB wins with an explicit warning
- **Network isolation** at the socket layer (whitelist domains + localhost; LLM_ONLY / OFFLINE / ALLOW_ALL modes)
- **Audit log** records every LLM call: model, task, token counts, duration, and a SHA-256 prompt hash (no plaintext)
- LiteLLM-based, provider-agnostic (OpenAI / Anthropic / DeepSeek / Ollama / MiniMax / 100+)

Apache 2.0 licensed. See [docs/architecture.md](docs/architecture.md) for design details.
