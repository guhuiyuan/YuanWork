# Changelog

All notable changes to YuanWork will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **真流式输出** — `chat_stream` 之前是「一次性拿到完整回答再返回」，名字具有误导性。现在回答逐 token 推送到客户端
  - `LLMGateway.stream()`：litellm 的流式响应是**阻塞**生成器，直接迭代会占住事件循环整个生成过程
    - 改为在 daemon 线程上驱动该生成器，delta 通过 `asyncio.Queue` + `loop.call_soon_threadsafe` 交回事件循环
    - 事件循环全程可服务其他请求；客户端断开时 `stop` 事件让工作线程停止拉取
    - 审计日志在成功 / 失败 / 提前退出三种情况下都恰好写一次
  - `AgentLoop.stream()`：回答步骤改为 `generate_answer_stream()`，逐个 delta 发 `token` 事件
    - `done` 事件现在携带完整 `ChatResponse`（answer / citations / confidence / conflicts / trace）
    - 生成中途失败时保留已生成的部分文本，追加错误提示，不丢弃用户已读到的内容
    - `AgentLoop.run()` 保留阻塞路径，行为不变
  - `api/routes/chat.py`：出错时也发一个终止 `done` 事件，客户端据此区分「正常结束」与「被切断」；加 `Cache-Control: no-cache` + `X-Accel-Buffering: no` 防代理缓冲
  - Rust 侧新增 `src-tauri/src/sse.rs`：零依赖增量 SSE 解析器（CRLF / LF 均可，跨 chunk 断帧，注释帧，多行 data）
    - `chat_stream` command 改读 `/api/v1/chat/stream`，边读边用 `app.emit("chat:event", ...)` 转发给 webview
    - 用 `reqwest::Response::chunk()` 而非 `bytes_stream()`，因此**不需要新增任何 Rust 依赖**
  - 前端 `api/backend.ts`：`chatStream()` 统一 Tauri / 浏览器两种模式
    - 浏览器模式用 `fetch` + `getReader()` + `TextDecoder(stream: true)` 自己解析 SSE
    - Tauri 模式用 `listen("chat:event")` 订阅 Rust 转发的事件
  - 前端 `store.ts`：`appendToLast` 改为 `appendToMessage(id, chunk)` —— 旧实现会往「最后一条消息」追加，流式期间若插入新消息就会写错地方；顺带删掉了这个死代码
  - 前端 `ChatWindow.tsx`：`requestAnimationFrame` 批量 flush token 缓冲，避免每个 token 触发一次 re-render
    - `token` 是增量文本的唯一来源，`answer` 事件只作兜底，避免重复渲染
    - 失败时保留已流式出的文本，只在其后追加错误提示
  - 测试：15 个后端测试（含「不阻塞事件循环」「不缓冲到末尾」两条决定性断言）+ 9 个 Rust SSE 解析器测试
  - `scripts/e2e_stream_check.py`：端到端验证。启动真实 FastAPI 应用，只 mock `litellm.completion`，测量每个 SSE 帧到达客户端的时刻
    - 决定性断言：第一个 token 至少比最后一个早 300ms 到达，且流式期间并发 `/health` 仍能立即返回
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

### Changed

- **LLM 配置：只启用 MiniMax 一个模型** — 目前只有 `MINIMAX_API_KEY` 是配好的，其余 provider 的 key 都没有
  - `configs/models.yaml`：所有任务（understand / extract / answer / conflict_check / reflection / wiki_compile / ner / general）统一路由到 `minimax-chat`
  - OpenAI / Anthropic / DeepSeek / Ollama 保留为注释示例，取消注释 + 设好对应 `*_API_KEY` + 改 `routing` 即可切换，不需要改代码
  - `LLMGateway._default_routing()` 与内嵌的 `DEFAULT_MODELS_YAML` 同步为单模型版本
  - 找不到 `models.yaml` 时改为回退到内嵌模板（原来回退到空模型表，只有一个 gpt-4o 的 routing，等于直接不可用）

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
