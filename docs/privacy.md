# YuanWork 隐私设计

> 数据流图、网络隔离、审计日志、敏感字段处理
> 最近更新：2026-10-19

---

## 1. 隐私原则

YuanWork 的三条核心隐私原则：

1. **本地优先**：文档原文不持久化；向量、对话历史、配置全部本地。
2. **最小外发**：只把"当前查询 + 召回 chunks"发往云端 LLM。
3. **可审计**：所有 LLM 调用都有记录（无原文）；用户可随时查看、导出、清空。

---

## 2. 数据流图

```
┌──────────────────────────────────────────────────────────────────┐
│                       本地 (YuanWork 用户机)                       │
│                                                                  │
│  文档文件 (./kb/)         不持久化         临时解析 → Block 对象   │
│       │                                               │          │
│       │  parse (in-memory only)                       │          │
│       │                                               ▼          │
│       │                                         chunker         │
│       │                                               │          │
│       │                                               ▼          │
│       │                          ┌─► LanceDB (vector + metadata) │
│       │                          │                              │
│       │                          └─► Tantivy FTS (BM25 index)   │
│       │                                                          │
│       │                                                          │
│  对话 (./data/store.db) ── SQLite WAL                            │
│       │   ├─ episodes                                            │
│       │   ├─ profile                                             │
│       │   ├─ wiki_pages                                          │
│       │   └─ conflicts                                           │
│       │                                                          │
│       │                                                          │
│  审计日志 (./data/logs/audit.jsonl) ── JSONL, 无原文           │
│                                                                  │
└────────────────────────────┬─────────────────────────────────────┘
                             │   ← 受 socket 层域名白名单约束
                             │
                             ▼   仅以下数据外发：
                  ┌──────────────────────────┐
                  │ 当前用户查询              │
                  │ 召回的 chunks (top-K)     │
                  │ 必要的 system prompt     │
                  └──────────┬───────────────┘
                             │
                             ▼
                       Cloud LLM
                (api.openai.com / api.anthropic.com / etc.)
```

**关键点**：

- 文档原文在解析后**立即丢弃**（Block 对象是 in-memory）
- LanceDB / Tantivy 只存 chunk 文本（**已经过 chunker 切分**）+ 向量 + 元数据
- SQLite 存对话历史、用户画像、wiki metadata
- 审计日志只记 `prompt_hash` + `prompt_summary`（前 200 字符），不存全文

---

## 3. 网络隔离（Stage 3）

### 模式

通过环境变量 `YUANWORK_NETWORK_MODE` 控制（默认 `1` = LLM_ONLY）：

| 模式 | 值 | 行为 | 适用 |
|---|---|---|---|
| `allow_all` | 0 | 不拦截 socket | 开发调试；**不建议日常使用** |
| `llm_only` | 1 | 仅放行 `allowed_domains` + localhost | **默认**；个人 KB 日常 |
| `offline` | 2 | 全部拦截，含 localhost | 离线/敏感场景；仅本地模型可用 |

### 白名单配置

```bash
# .env
YUANWORK_NETWORK_MODE=1
YUANWORK_ALLOWED_DOMAINS=["api.openai.com","api.anthropic.com","api.deepseek.com","api.minimaxi.com"]
```

### 实现位置

- 模块：`src/backend/yuanwork/privacy/network_guard.py`
- 触发点：`__main__.py` 启动时调用 `guard.install()`
- 拦截层：`socket.create_connection` + `socket.socket.connect`

### 域匹配规则

`exact match` OR `endswith('.' + allowed)`：

- `api.openai.com` ✓ 匹配 `api.openai.com`
- `v1.api.openai.com` ✓ 匹配 `api.openai.com`（子域）
- `api.openai.com.evil.com` ✗ **不匹配**（右锚定 endswith）
- `fakeapiopenai.com` ✗ **不匹配**

IPv6 字面量单独处理（多个冒号不剥离端口）。

### 错误 UX

被拒时抛 `NetworkIsolationError(PermissionError)`，错误信息完整：

```
Network isolation: outbound to 'evil.com' is blocked.
  Network mode: LLM_ONLY
  Allowed domains: ['api.openai.com', 'api.anthropic.com', ...]
  To allow this domain, add it to settings.allowed_domains
  (or configs/privacy.yaml), or set
  YUANWORK_NETWORK_MODE=allow_all to disable isolation.
```

### 单元测试覆盖

`tests/unit/test_network_guard.py`（17 测试）：

- 纯策略：exact / subdomain / lookalike / localhost / offline / allow_all / port strip
- Socket hook：blocks via create_connection / blocks via socket.connect / idempotent / context manager
- 集成：真实 httpx 调用被拦截

### 已知边界

| 场景 | 行为 | 缓解 |
|---|---|---|
| 系统级 HTTP 代理（如 `HTTP_PROXY=127.0.0.1:7897`） | 流量走代理，guard 只看到代理 IP | 用户显式把代理加入白名单；测试用 `trust_env=False` 验证 |
| DNS 解析后 httpx 直接传 IP 给 socket | guard 看到 IP 形式 | guard 用 IP 后缀匹配（`v1.api.openai.com` 仍能匹配） |
| Tauri 桌面端内置浏览器走 proxy | 同上 | 同上 |
| WebView 渲染的任意前端 | 浏览器自己的网络栈绕过 Python socket guard | UI 只能调本地后端 API（`http://localhost:8765`），由后端的 guard 兜底 |

---

## 4. 审计日志（Stage 3）

### 目的

- **透明**：用户随时知道 YuanWork 跟云端 LLM 交互过几次、花了多少 token
- **可审计**：合规要求 / 个人审查 / 调试
- **去重**：同 prompt 多次发送可以识别（通过 SHA-256 哈希）

### 数据格式

每条记录一行 JSON（JSONL），路径：`<data_dir>/logs/audit.jsonl`

```json
{
  "timestamp": "2026-10-19T12:34:56.789",
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

### Prompt 哈希

`prompt_summary` 前缀 `#<16hexhash>` 是 prompt 消息列表的 SHA-256 前 16 字符。计算方式：

```python
canon = json.dumps(
    [{"role": m.role, "content": m.content} for m in messages],
    sort_keys=True,
    ensure_ascii=False,
)
h = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
```

**不持久化 prompt 原文**——这是隐私承诺的关键。

### API 访问

```bash
# 最近 100 条
GET /api/v1/audit?limit=100

# 统计
GET /api/v1/audit/stats
# →
# {
#   "llm_calls": 42,
#   "failures": 1,
#   "total_prompt_tokens": 12345,
#   "total_completion_tokens": 6789,
#   "by_model": {"MiniMax-chat": 30, "gpt-4o-mini": 12},
#   "by_task_type": {"answer": 20, "understand": 15, "conflict_check": 7}
# }

# 清空
DELETE /api/v1/audit
# → {"cleared": 42}
```

### 写入保证

- `try/finally` 包裹 litellm 调用，**成功和失败都写**
- 写日志本身再 `try/except` 兜底，**日志失败永不让聊天挂**
- 写日志是同步 I/O（`jsonl.open('a')`），单次 < 5ms，不成为瓶颈

### 流式响应

流式 API（`/chat/stream`）也审计，累加响应文本，从最后一个 chunk 读 usage。

### 用户控制

| 操作 | 方式 |
|---|---|
| 查看 | `GET /api/v1/audit` 或直接读 `audit.jsonl` |
| 统计 | `GET /api/v1/audit/stats` |
| 清空 | `DELETE /api/v1/audit` 或删除文件 |
| 导出 | `cp <data_dir>/logs/audit.jsonl ./backup.jsonl` |
| 禁用 | 暂未提供（默认开启；TODO Stage 4） |

---

## 5. API Key 管理

### 不入代码

`.env` 永远 gitignore（见根目录 `.gitignore`）。LiteLLM 配置（`configs/models.yaml`）只写 **env var 名称**，不写实际 key：

```yaml
models:
  MiniMax-chat:
    provider: anthropic
    api_key_env: MINIMAX_API_KEY    # ← 只引用，不存值
    api_base: https://api.minimaxi.com/anthropic
    model_name: MiniMax-M3
```

`LLMGateway._load_config()` 在启动时把 env var 解析为实际值，存内存。

### 启动检查

如果 `api_key_env` 引用的 env var 没设置，启动时会 warn：

```
Model 'gpt-4o' references api_key_env=OPENAI_API_KEY but that env var is not set
```

但**不阻断启动**——用户可以先用本地模型跑，再加 API key。

### 操作系统级（未来）

TODO Stage 4：把 `api_key` 改为 OS keyring 存储（Windows Credential Manager / macOS Keychain / Linux Secret Service）。LiteLLM 已经支持。

---

## 6. 敏感字段处理（未来）

`src/backend/yuanwork/privacy/redactor.py` 已存在但未启用。计划：

- 身份证 / 手机号 / 银行卡正则替换为 `<ID>` / `<PHONE>` / `<BANK>`
- 配置开关 `YUANWORK_ENABLE_REDACTION=true`
- 在 chunk 文本发往 LLM 之前 redact，不修改本地存储

---

## 7. 已知隐私边界

诚实声明：YuanWork 不是匿名化工具。它保护的是「你的 KB 不被未经授权的代码外发」和「你跟 LLM 的每次交互都有记录」。它**不**保护：

- 你自己 LLM 调用的 prompt 内容——那是 LLM 自己的隐私政策
- 截图 / 录屏——那是操作系统的能力
- 物理访问——拥有你机器物理访问的人有完全控制权

---

## 8. 审计与合规

个人场景不需要 SOC2 / GDPR 合规，但代码风格上：

- API key 不入 git（`.gitignore` 覆盖 `.env`）
- 文档原文不入库（设计上）
- 用户可一键清空对话历史（`DELETE /api/v1/memory/episodic`）
- 用户可一键清空审计日志（`DELETE /api/v1/audit`）
- 用户可一键切换离线模式（`YUANWORK_NETWORK_MODE=2`）

---

## 9. 未来工作

- [ ] OS keyring 存储 API key
- [ ] 自动 redact 敏感字段（身份证/手机/银行卡）
- [ ] 端到端加密本地数据库（SQLite 加密扩展）
- [ ] 多用户隔离
- [ ] 完整的 audit 导出格式（CSV / Parquet）
- [ ] 与 Obsidian 双向同步（保留本地为主原则）
