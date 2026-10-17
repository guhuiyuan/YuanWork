# YuanWork · 本地个人工作助手

> **Wiki + 记忆工程 + RAG** 的桌面级本地工作助手
> 为严格场景（合同条款、规章法条）而生的本地知识库问答系统

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Status: Stage 1](https://img.shields.io/badge/Status-Stage%201-orange)]()
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue)]()
[![Tauri 2.x](https://img.shields.io/badge/Tauri-2.x-orange)]()

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
| **多格式解析** | Word / PDF / **PPT (按 slide)** / Excel / Markdown / TXT / 图片 OCR |
| **本地向量库** | bge-m3 (本地嵌入) + LanceDB (嵌入式存储) |
| **混合检索** | BM25 + 向量 + 元数据 + Reranker |
| **3 步推理** | 理解 → 定位 → 抽取 → 回答（带完整 trace） |
| **强制引用** | 高置信答案必带原文 [n] 引用 + 页码/slide 号 |
| **拒绝策略** | 低置信时拒绝直接回答 + 列出「最可能位置」 |
| **冲突解决** | LLM 训练知识 vs 知识库 → KB 优先 + 显式提醒 |
| **整合层** | 写入控制 / 去重 / 冲突日志 / 自动反思 |
| **Wiki 形态** | LLM 把多 chunk 编译成结构化页面 + 双向链接 + 实体图谱 |
| **隐私优先** | 网络隔离模式 / 域白名单 / API key 走 OS keyring / 审计日志 |
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
    ├─ Retrieval Engine     ──►  BM25 + 向量 + Reranker + 置信度
    ├─ Wiki Layer           ──►  页面生成/链接/图谱         【差异化】
    ├─ LLM Gateway (LiteLLM)──► 路由 + 降级 + 预算
    ▼
Memory Substrate: Episodic (SQLite) + Semantic (LanceDB) + Profile
                  + Wiki (SQLite + Markdown)
Document Pipeline: 监听 → 解析 → 分块 → 嵌入 → 入库
Cloud LLM (按需)
```

完整架构与设计决策见 [docs/architecture.md](docs/architecture.md)（待补）。

---

## 快速开始

### 前置条件

- **Python 3.10+**
- **Node.js 18+**
- **Rust 1.70+**（编译 Tauri）
- **uv**（推荐的 Python 包管理工具）或 pip

### 1. 启动 Python 后端

```bash
cd src/backend
uv sync                       # 安装依赖
cp .env.example .env           # 配置 LLM API key
uv run python -m yuanwork --port 8765
```

首次启动会下载 bge-m3 嵌入模型（约 2.3GB），请耐心等待。

### 2. 启动 Tauri 桌面端

```bash
cd src/app
npm install
npm run tauri dev
```

### 3. 在 UI 中

1. 点击左侧「+ 添加」选择本地文件夹作为知识库
2. 等待索引完成
3. 在对话窗口提问，如：
   - 「这份合同里违约金怎么约定？」
   - 「上次我们讨论过的方案三是什么？」
   - 「总结这个产品 PPT 的核心功能」

---

## 配置

### LLM (LiteLLM + YAML)

复制并编辑 `src/backend/configs/models.yaml`：

```yaml
default:
  model: minimax/MiniMax-M3
  api_base: https://api.minimaxi.com/anthropic
  api_key_env: MINIMAX_API_KEY

tasks:
  understand:
    model: minimax/MiniMax-M3
    temperature: 0.0
  answer:
    model: minimax/MiniMax-M3
    temperature: 0.3
  conflict_check:
    model: minimax/MiniMax-M3
    temperature: 0.0
```

支持 OpenAI / Claude / DeepSeek / 智谱 / Ollama / MiniMax 等 100+ provider。详见 [LiteLLM 文档](https://docs.litellm.ai/)。

### 网络隔离模式

`.env` 中设置：

```bash
NETWORK_MODE=llm_only        # 仅允许 LLM API 域名
# NETWORK_MODE=offline       # 完全离线（仅本地模型）
# NETWORK_MODE=allow_all     # 不限制
```

---

## 项目结构

```
YuanWork/
├── LICENSE (Apache 2.0)
├── README.md
├── .gitignore
├── .env.example
├── src/
│   ├── app/                  # Tauri 桌面端
│   │   ├── package.json, vite.config.ts, tsconfig.json
│   │   ├── index.html
│   │   ├── src/              # React + TypeScript 前端
│   │   │   ├── main.tsx, App.tsx, store.ts
│   │   │   ├── api/backend.ts       # Tauri command bridge
│   │   │   ├── components/          # Sidebar, ChatWindow, RightPanel
│   │   │   └── styles/global.css
│   │   └── src-tauri/        # Rust 壳
│   │       ├── Cargo.toml, tauri.conf.json
│   │       ├── capabilities/default.json
│   │       └── src/          # main.rs, lib.rs, sidecar.rs, commands.rs, fs_scope.rs, secrets.rs
│   └── backend/              # Python 后端
│       ├── pyproject.toml
│       ├── configs/          # models.yaml
│       ├── yuanwork/         # 业务代码
│       │   ├── api/          # FastAPI 路由
│       │   ├── db/           # SQLite + LanceDB
│       │   ├── llm/          # LiteLLM gateway
│       │   ├── memory/       # episodic / semantic / profile
│       │   ├── ingestion/    # 解析 / 分块 / 监听
│       │   ├── retrieval/    # 混合 / 置信度
│       │   ├── orchestrator/ # 3 步推理
│       │   ├── consolidation/# 整合层
│       │   ├── wiki/         # Wiki 层
│       │   └── privacy/      # 脱敏 / 网络守卫 / 审计
│       └── tests/            # 单元 + 集成测试
└── docs/                     # 设计文档
```

---

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| **阶段 1：核心可用** | Tauri 骨架 / 文档解析 / 嵌入检索 / 3 步推理 / 强制引用 / 拒绝 / 对话 UI | 🟡 骨架完成 |
| **阶段 2：差异化** | 整合层 / 冲突检测 / Reranker / 情景记忆 / Wiki 主动巡检 / 图谱视图 | ⏳ 待开始 |
| **阶段 3：开源准备** | 网络隔离 / 审计日志 / 文档 / 性能调优 | ⏳ 待开始 |

---

## 关键设计决策

- **为什么 Wiki + RAG 而不是纯 RAG？** —— 纯 RAG 只能"问"，Wiki 让用户能"问" + "浏览"，跨文档形成结构化知识，添加双向链接和实体图谱。
- **为什么自研状态机而不是 LangGraph？** —— 3 步推理本质是线性 DAG + 1 个条件分支，自研 200 行足够清晰，避免 langchain 生态绑架。详见 [docs/architecture.md](docs/architecture.md)。
- **为什么 bge-m3 + LanceDB？** —— bge-m3 同时输出 dense + sparse，LanceDB 嵌入式单文件备份简单，两者 Apache 2.0 开源。
- **为什么 LiteLLM？** —— 100+ provider 统一接口，YAML 配置即切换，不锁死单一厂商。

---

## 隐私承诺

- ✅ 文档原文**不持久化**，只存 chunks + 向量
- ✅ 向量 + 对话历史**全部本地**
- ✅ 仅当前查询 + 召回 chunks 发往云端 LLM
- ✅ API key 走 OS keyring（Windows Credential Manager / macOS Keychain）
- ✅ 可启用「完全离线」模式（仅本地模型）
- ✅ 审计日志记录每次 LLM 调用，可查看/导出/清空

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
