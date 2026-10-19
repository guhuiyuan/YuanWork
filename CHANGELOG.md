# Changelog

All notable changes to YuanWork will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Tauri 桌面 Debug 二进制** — Tauri 2.x 壳编译通过，产出 `D:\cargo-target\debug\yuanwork.exe` (23 MB)
  - `sidecar.rs` 重写：`BackendLaunch` 枚举支持 bundled 二进制 + Python venv 两种启动模式
  - `find_python_interpreter()` 向上爬 8 层找 `.venv/Scripts/python.exe`，fallback 到 `py` / `python` / `python3`
  - `find_backend_module_dir()` 向上爬 8 层找 `src/backend/`，让 `python -m yuanwork` 可解析模块
  - 新增 `get_wiki_backlinks` / `get_wiki_graph` Tauri commands（前端已调用，Python 后端 `/api/v1/wiki/graph` + `/api/v1/wiki/pages/{id}/backlinks` 早已存在）
  - 占位图标：32/128/256 PNG + multi-resolution ICO + ICNS（深蓝 + "Y"）
  - `run.bat` 项目根目录启动器：先 `cd` 再启动 .exe，确保 sidecar 找得到项目
  - 工具链：Rust 1.99.1 (stable-msvc) → `D:\cargo-target`，避免 C: 盘空间不足
  - `Cargo.lock` 提交（Rust 二进制项目约定）
- 文档 / 配置：完整的 README.md / CHANGELOG.md / CONTRIBUTING.md / docs/

## [0.3.0] - 2026-10-19 — Stage 3: 开源准备

### Added

- **Tantivy FTS** (commit `a9d18b2`) — 替代 bge-m3 sparse channel
  - `retrieval/fts.py` 封装 tantivy 0.26 + jieba 0.42，BM25 over CJK-segmented text
  - `SemanticMemory.search_hybrid()` 用 FTS 替换 in-Python sparse dot product
  - 懒加载 + 自动从 LanceDB 重建（v3 → v4 迁移无感）
  - 数字 / CJK 召回从 0.0 → 1.0
  - 13 个新测试
- **网络隔离** (commit `d194189`) — socket 层域名白名单
  - `NetworkGuard.install()` 拦截 `socket.create_connection` + `socket.socket.connect`
  - 三种模式：`ALLOW_ALL` / `LLM_ONLY`（默认） / `OFFLINE`
  - `NetworkIsolationError` 带 hostname / mode / allowed_domains + 可操作消息
  - `__main__.py` 启动时自动安装并打印状态
  - 17 个新测试
- **审计日志** (commit `5d19db0`) — 每次 LLM 调用记录
  - `LLMGateway` 注入 `AuditLogger`，try/finally 包裹
  - SHA-256 16 字符 prompt 哈希（不持久化原文）
  - JSONL 格式，路径 `<data_dir>/logs/audit.jsonl`
  - API：`GET /audit` / `GET /audit/stats` / `DELETE /audit`
  - 流式响应累加文本 + 从最终 chunk 读 usage
  - 17 个新测试
- **文档**：docs/architecture.md 完整化（10 个章节）；新增 docs/privacy.md；CHANGELOG.md

### Changed

- README.md 升级到 Stage 3 完成态；更新架构图 / 隐私承诺 / 路线图
- .env.example 改用 `YUANWORK_*` 前缀，添加全部新环境变量
- `LLMGateway` 构造函数新增 `audit_logger` 可选参数

## [0.2.0] - 2026-10-18 — Stage 2: 差异化

### Added

- 整合层：writer / conflict / reflection / engine
- 冲突检测 v1：LLM vs KB 一致性判别 + 显式标注
- Reranker：bge-reranker-v2-m3（懒加载）
- 情景记忆：episodes / summaries / profile
- Wiki 主动巡检：trigger + page generator
- Wiki 浏览 UI：4 个 React 组件（tree / page / graph / backlinks）
- d3 力导向图谱视图
- Tauri 2.x 桌面壳 + Vite dev server
- 11 种新 API 端点

## [0.1.0] - 2026-10-17 — Stage 1: 核心可用

### Added

- Tauri 桌面骨架（Rust sidecar + Webview）
- 文档解析：PDF / DOCX / PPTX (按 slide) / Markdown / TXT
- bge-m3 嵌入（本地）+ LanceDB 存储（嵌入式）
- 混合检索：BM25 + 向量（v3 阶段，仍是 bge-m3 sparse）
- 3 步推理：understand → localize → extract → answer
- 强制引用 + 拒绝 + 置信度
- FastAPI + SSE 流式聊天
- 22 个单元测试
- SQLite WAL 存储
- LiteLLM 多 provider 适配
- MiniMax / OpenAI / Claude / DeepSeek 6 个模型配置

---

## 版本约定

- **Major (x.0.0)**：破坏性 API 变更
- **Minor (0.x.0)**：新功能（向后兼容）
- **Patch (0.0.x)**：bug fix / 文档

## 提交规范

```
<scope>: <imperative summary>

<optional body>

Co-Authored-By: Claude Code <noreply@anthropic.com>
```

Scope: `backend` / `app` / `docs` / `infra` / `test`
