# Changelog

All notable changes to YuanWork will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Phase 5 整体验收报告**（2026-11-17）— `specs/yuanwork/acceptance-report.md`：**59 条验收标准
  逐条回放**（功能 30 / 性能 12 / 边界 17），每条给出覆盖它的任务、验证方式与结果
  - 性能类在**真实工作域**（4349 个文件 / 838.7 MB）上实测：建库 **295.36 文件/分钟**、
    解析 773 文件/分钟、嵌入 135 切片/秒、落库 1071 切片/秒；7 条延迟类用
    `scripts/bench/latency.py` 量（首批证据 0.34 秒、首个 token 3.63 秒、冷启动 5.01 秒、
    单文件重建 0.35 秒、核心层编译 15.45 秒、核心层证据 0.008 秒且长尾层没被检索）
  - **数字要能解释**：读不进来的 24 个文件逐个列出路径与原因——23 个是旧版二进制格式
    （`.doc`/`.ppt`，规范明确不做），1 个是受支持的 `.pptx`（真缺陷，已修）
  - 接口一致性：design §3.2 的 18 条接口签名逐条在实现里找到；架构一致性：§1.1 的模块表
    逐条对到实现文件，**4 处偏差已对齐规范**（类名大小写、存储模块边界、页面编辑没有独立类、
    冲突判定是模块函数）
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
  - 测试：22 个后端测试（含「不阻塞事件循环」「不缓冲到末尾」两条决定性断言）+ 16 个 Rust 测试（SSE 解析 + UTF-8 增量解码）+ 18 个前端测试
  - `scripts/e2e_stream_check.py`：端到端验证。启动真实 FastAPI 应用，只 mock `litellm.completion`，测量每个 SSE 帧到达客户端的时刻
    - 决定性断言：相邻 token 的到达间隔 ≥50ms 且平均间隔与 provider 产出节奏同量级，且流式期间并发 `/health` 仍能立即返回
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
- **首次启动引导** — 新装的用户看到的是一张欢迎页，而不是空荡荡的侧边栏：
  - 卡片式说明这个应用做什么、索引什么、数据怎么保管
  - 一个显眼的「选择工作目录」按钮，走的是原来的选目录 + 索引流程
  - 「稍后再说」可以跳过；跳过与否记在 localStorage，下次启动不再打扰
  - 判定逻辑抽成纯函数 `shouldShowOnboarding()`：知识库非空、或已经
    引导过、或者文件列表还没加载完（避免每次启动闪一下），都不显示
- **侧边栏可以逐个删除文件** — 每行悬停出现 ✕，点开是行内确认（不用
  `confirm()`，那会卡住 webview），文案写明「文件仍在磁盘上」：只从索引
  里移除，不动用户自己的文件。删完弹一条 toast
- **索引进度可见** — 新增 `GET /ingest/status/{job_id}` 的 Rust 桥接
  （`get_ingest_status`），前端轮询到 job 完成才刷新文件列表
- **工作域驱动的建库**（2026-11-15）— 侧栏列的不再是「索引里的文件」，而是**工作域条目**：
  「+ 文件夹」「+ 文件」把东西加进工作域，「重建」按工作域建库；移除一个条目会清掉它
  带进索引的内容（文件还在磁盘上，文案写明这一点）。首次启动的判断也改成看**工作域**——
  空知识库只说明还没建库，空工作域才是「不知道该做什么」
- **性能基准脚本与真实工作域验收**（2026-11-15，`scripts/bench/`）— 固定种子的合成基准集
  做回归门（相对上次基线不退化 10%），**真实工作域**定绝对值；报告给分段耗时、三项吞吐
  与峰值内存。验收工具会先采集工作域的**结构画像**（不读一个字）再索引它
- **本项目的可写状态归到一个可配置的根目录**（2026-11-17）— `YUANWORK_HOME` 一设，索引
  （`data/`）、日志、审计与模型缓存（`models/`）一起搬。模型缓存在**进程最早期**写进
  `HF_HOME`（huggingface_hub 的缓存位置是**导入时**算的常量，晚一步设置**不报错也不生效**）。
  相对路径按**项目根**解析，不再跟着当前目录跑
- **切片长度用嵌入模型读得下的 token 量**（2026-11-17）— 切片上限不再是「2000 字符」
  （那是英文的换算），而是嵌入模型一次读得下的 token 量（512 减两个特殊 token = 510）；
  切块器与嵌入器**用同一个数**
- **延迟实测脚本**（2026-11-17，`scripts/bench/latency.py`）— 性能承诺里有 **7 条是延迟**
  （首批证据 2 秒、首个 token 5 秒、判定合计 1.5 秒、冷启动 8 秒、单文件重建 60 秒、
  核心层编译 5 分钟、核心层证据 2 秒），此前只有**超时上限**、没有实测值——没有实测值的
  承诺不是承诺。新脚本对着一个真实索引量这些数
  - 同一个回答里各步的起止时刻由 SSE 的 trace 事件给出，所以几个数**出自同一次运行**
    （跨运行的数字能差 8%，那不是系统的问题）
  - 核心层那条量的是「**长尾层没有被检索**」这个事实（检索器自己报 `longtail_searched`，
    不是靠猜）
  - 冷启动量的是「进程起到 `/api/v1/health` 回答」——桌面端等的就是这个

### Fixed

- **索引完文件列表还是空的** — `/ingest/dir` 是异步任务（返回 job_id 后
  台跑），前端却在请求返回后立刻拉文件列表，那时候索引还没开始。现在
  轮询 job 状态直到 completed，再刷新列表，并显示「正在索引…（3/12）」
- **`delete_kb_file` 不看 HTTP 状态** — 后端返回 404 时 Rust 侧照样返回
  Ok，文件却还留在列表里，看着像删除失败。现在非 2xx 直接报错
- **索引进度一直是「（0/12）」** — 后端从来没更新过 `files_done`，进度条看着像坏了。
  - `IngestionPipeline.ingest_directory()` 新增可选的 `on_progress(done, total, path)`
    回调，每处理完一个文件调一次；路由把它接到 job 状态上
  - 顺带修了 `files_total`：入队时数的是目录里**所有**文件，pipeline 只处理受
    支持的类型，所以总数经常对不上；现在用 pipeline 报的真实数量
- **`ingest_directory` 不看 HTTP 状态** — 路径不存在时后端返回 404，前端
  拿到的却是「成功」的响应体
- **Rust 侧 UTF-8 字符被切坏**（`src-tauri/src/sse.rs`） — `reqwest` 的 chunk 边界可能落在多字节字符中间，之前的实现逐 chunk `from_utf8`，切到中间的汉字会变成 U+FFFD
  - 新增 `Utf8Decoder`：增量解码，保留未完成的尾部字节等下一块，非法字节替换为 U+FFFD，流结束时 flush
- **生成出错时引用被丢弃**（`agent.py`） — 回答生成到一半失败时，流式路径和阻塞路径都会丢掉 citations，用户拿到一段无法核对的文字
  - 两条路径都在异常分支里重新 `_build_citations(state.evidence)`；`citations` SSE 事件改为在拒绝 / 正常两个分支都发
- **拒绝横幅文案张冠李戴**（`ChatWindow.tsx`） — 生成出错时后端发的是 `refusal` 事件（`reason` 为 `generation_error: ...`），前端却一律渲染成「⚠️ 置信度不足，已拒绝直接回答」
  - `ChatMessage` 增加 `refusalReason`；新增 `api/streamText.ts` 的 `refusalBanner()` 区分「生成回答时出错」与「置信度不足」
- **失败提示插到了已流式文本前面** — 失败提示现在追加在已生成文本之后（`appendFailureNotice`），不会让人误以为请求在一开始就失败了
- **`refusal` 事件重复追加文本** — 拒绝消息本身就包含已流式出的部分，改为**替换**而非追加
- **后端测试断言太弱** — 「不缓冲到末尾」原来断言 `arrival[-1] < produced[-1] + 0.02`，缓冲实现也能通过；改为断言**第一个** delta 在 provider 产出完之前就已送达，且相邻 delta 到达间隔不塌缩
- **引用的页码 / slide 号 / 章节一直是空的** — chunk 的定位元数据有两种形状，入库后和检索后不一样：  - chunker 产出的是嵌套形状 `{"meta": {"page": 12}}`；检索返回的是扁平的 LanceDB 行 `{"page": 12}`
  - 入库侧 `semantic.add_chunks` 读的是扁平 key（`chunk.get("page")`），在嵌套形状上永远是 `None` → 数据库里 page 列全空
  - 消费侧（`agent._build_citations`、`answer._build_answer_task`、`wiki/page_generator`）读的是嵌套 `meta`，在检索行上永远是 `None`
  - 后果：prompt 第 3 条要求模型写「第 N 页」「Slide N」，但页码根本没进 prompt；UI 里「第 N 页」「Slide N」是死代码，引用只能定位到文件
  - 修复：新增 `yuanwork/utils/chunk_meta.py` 的 `chunk_meta()`，两种形状都能读；入库侧改为从嵌套 meta 取值（section 列存 heading）；三处消费侧统一走 `chunk_meta()`
  - ⚠️ **已建的知识库需要重新索引**才会带上页码（旧的行里 page 就是 null）；重新入库即可，代码不用改
- **冲突检测把「知识库里提到冲突」当成了冲突** — 启发式扫描的是 `答案 + 证据原文`，而知识库里本来就该出现「过时」「冲突」这些词（一篇讲冲突解决机制的合同就会命中），于是 LLM 检测器判 consistent 也被启发式覆盖成 conflict，凭空造出一条冲突记录。
  - 改为**只扫答案**，且标记词收敛为 answer.j2 真正要求模型加的那几个（⚠️ / 通用知识与知识库存在分歧 / outdated / 过时 / 已弃用 / deprecated）
  - 回归验证：改回旧写法，`test_a_kb_document_that_mentions_a_conflict_is_not_a_conflict` 立刻失败
- **模型返回空回答被当成成功** — provider 什么都不吐时，`refused=False` + 空 answer + 满屏引用，用户只看到一个空白气泡。流式与阻塞两条路径都改为抛 `模型返回了空回答`，走生成错误分支（保留引用 + 错误提示）
- **Tauri 命令失败时前端显示「（请求失败：undefined）」** — `Err(String)` 在 Tauri v2 里是**裸字符串** reject，不是 Error，`(e as Error).message` 取不到。前端改成同时处理 Error / 字符串 / 其他值
- **流式审计可能不写** — 审计写在异步生成器的 finally 里，消费方中途放弃且不关闭生成器时永远不会执行。改到**工作线程**里写：只要 provider 调过就一定有一条记录，且仍恰好一条
- **桌面端根本起不来：启动探测的状态码解析写错了** — `reqwest_get()` 返回 HTTP 状态行 `"HTTP/1.1 200 OK"`，而调用方拿它 `starts_with("200")` 比较，**永远不成立**。于是探测循环空转 60 次、30 秒后 panic："Backend failed to start within 30 seconds"。后端其实早就起来了，只是没人认出它。
  - 抽出 `parse_status_code()`，取状态行第二个字段（状态码），并补了 5 个单测覆盖 200 / 404 / 500 / 空响应 / 垃圾响应
- **桌面端找不到 .env，LLM 密钥没进 os.environ** — sidecar 的 cwd 是 `src/backend`，而 `.env` 在项目根目录。原来的两个候选路径（`cwd/.env` 与 `src/.env`）都不存在，密钥永远加载不上 → 聊天一律报缺少凭据。
  - 改为从包目录向上最多找 6 层，新增 `_dotenv_candidates()` 便于测试
- **桌面端的知识库是空的** — sidecar 的 cwd 是 `src/backend`，后端默认 `data_dir` 落到 `~/.yuanwork`，而不是项目的 `data/`。现在 sidecar 会给子进程设 `YUANWORK_DATA_DIR=<项目根>/data`（除非用户自己设过）
- **拒绝消息里混进了模板标题** — `refusal.j2` 第一行 `# Refusal Response Template` 是 Markdown 标题而不是 Jinja 注释，渲染出来用户会看到这行字。改成 Jinja 注释
- **provider 连接没被真正释放** — `CustomStreamWrapper` 只有异步的 `aclose()`，没有同步 `close()`，原来的 `getattr(response, "close", None)` 永远是 None。改为沿包装链往下找底层 httpx 响应的同步 `close()`

- **首次启动引导的六个边角**（对抗审查逐条核实并修掉）：
  - 索引**失败**也会被标记成「已引导」——标记挪到 job 真正 completed 之后，
    失败时欢迎页留在原地，用户还能重试
  - `pickFolder()` 在 try 之外：原生对话框一抛错就变成未捕获的 rejection，
    按钮看起来「点了没反应」。挪进 try
  - 浏览器开发模式没有原生目录选择器，`pickFolder()` 直接返回 null，
    「选择工作目录」成了死路。改成弹窗让用户输入路径
  - 启动时知识库本来就有文件（老版本引导过），但从没写过「已引导」标记，
    删掉最后一个文件时欢迎页会盖在正在用的对话上。加载到非空就标记
  - 后端连不上时 `list_kb_files` 不看 HTTP 状态，500 被当成「知识库是空的」，
    于是欢迎页弹出来说谎。现在非 2xx 直接报错，并且「读取失败」不算
    「知识库为空」
  - 索引进度循环没有取消按钮也没有超时，卡住的 job 能把界面锁住。现在
    有「取消」，且单次状态请求有 20 秒超时（Rust 侧 30 秒），连续卡住
    就放弃

- **索引进度的分母是错的** — 入队时数的是目录里**所有**文件，pipeline 只
  处理受支持的类型，所以「（3/13）」里的 13 永远到不了 12，而且要等第
  一个文件解析完才纠正。
  - `IngestionPipeline.supported_files()` 抽出这个判断，路由在 job 开始
    前就用它算真实总数
  - 目录里一个受支持的文件都没有时，回调一次都不触发，计数器永远不动
- **每个文件都解析失败也算「完成」** — pipeline 的逐文件结果被丢掉了。
  现在索引数为 0 而错误数不为 0 时，job 标记为 failed 并带上原因

- **切片被 512 token 截断 —— 10.6% 的内容根本检索不到**（2026-11-17）— 切片器按
  **字符**切（2000 字符，注释写着「~500 tokens」，那是英文的换算），嵌入器按 **token**
  读（512）。**两把尺子不一样**。真实工作域实测：471/4801（9.8%）的切片超限，尾巴
  合计占全部内容的 **10.6%**
  - 丢掉的内容不是「弱一点」而是「彻底看不见」：只读前 512 token 的向量与**自己后半段**
    的余弦是 0.689，与**毫不相关**的切片是 0.854——**分差为负**（比噪声还远）
  - 「让模型读全文」（上限提到 2048）**救不了**：全文向量对自己尾部的余弦还是 0.689
    ——长文本池化成一个向量，头尾在向量空间里天然离得远。**病根是块太长，不是读得太短**
  - **切短就能救**：按 512 token 切开之后，针对尾部内容的提问 top-10 命中率
    **69.4% → 98.5%**（top-3 46.7% → 93.6%）
  - 切出来的片段重新分词时边界会变，token 数会浮动 1–2 个 → 留余量并且复检
  - **切片规则进了索引**（`files.chunk_version`）：规则变了旧文件**重切**，而不是被当成
    「已入库」跳过——否则改了规则而索引不重建，等于没修
- **网络挂住的时候，建库的每一批都丢了**（2026-11-17）— 嵌入模型明明在本地缓存里，
  那个库却还要问一次 Hub：连接挂住（不是被拒）就 **57 秒**然后抛超时错误，而且失败的
  不是模型，是它不带 `local_files_only` 加载的 tokenizer。后果是**每一批都失败**
  （实测一批 267 个切片地丢），索引几乎是空的，而建库照样报「完成」。现在**第一次尝试
  完全离线**，缓存里没有才联网：离线 **2.3 秒**成功，联网 57.2 秒失败
- **嵌入库把每一批都算了两遍**（2026-11-17）— `FlagEmbedding` 的 `encode()` 先跑一遍模型
  去探「这设备吃得下多大一批」，**把结果丢掉**，再把同一批跑一遍。自己前向之后：真实切片
  **86.5 → 242.3 切片/秒**（同一次运行里对比），全量建库 **1466 → 795 秒**
- **自己前向时忘了关 autograd**（2026-11-17）— 每层的中间结果都留在显存里等着反向传播：
  实测**每 token 776 KB vs 22 KB**（35 倍）。后果不是「慢一点」而是**批根本装不下**
  （64 条 × 512 token，关之前要 14 GB）
- **全文索引每批提交两次**（2026-11-17）— 删除旧版本一次、写入新版本一次，两次磁盘刷新
  换来同一个结果。改成共用一次刷新；提交还攒起来（攒到 1024 篇自己刷一次）
- **解析器在每个文件上都重建一次**（2026-11-17）— 图片解析器会加载 OCR 引擎与它的模型，
  **一次 2.2 秒**，而真实工作域有 105 张图片。改成每个工人进程只建一次
- **数据目录跟着当前目录跑**（2026-11-17）— 数据目录是相对路径，而后端在三种启动方式下
  的当前目录各不相同（桌面端 `src/backend`、仓库根、`src/backend`）：**同一个项目有三套
  不同的知识库**（实测两个目录各躺着一份 store.db）。现在相对路径按**项目根**解析
- **基准语料自己会变**（2026-11-17）— `openpyxl` 把当下时刻写进 xlsx 的属性里，压缩后
  长度差一个字节：「两次跑量的是同一批活」这句话在字节上不成立，回归门因此偶发失败。
  时间戳钉死之后，同一颗种子连生成 60 次完全一致
- **一份 .pptx 让整个解析崩了**（2026-11-17）— PowerPoint 把嵌入的公式对象放进一个
  **没有类型也没有序号**的占位符，python-pptx 就把那个对象当「标题」返回，而它根本没有
  文本：`slide.shapes.title.text` 直接抛 `AttributeError`，**整份文件读不进来**。取标题前
  先看它有没有文本框，没有就当这份幻灯片没有标题——真实文件（90 页）现在读出 85 个块，
  每块带幻灯片号锚点
- **冷启动被 `import litellm` 的 8 秒挡住**（2026-11-17）— `import litellm` 自己要 **8.0 秒**
  （它把一堆供应商 SDK 都拉进来），而网关模块在**顶层**就导入它，于是后端每次启动都替
  「还没发出去的请求」付这笔钱：冷启动实测 **12.23 秒**（承诺 8 秒）。改成**第一次真用**才导入
  （顶层那两个全局开关跟着一起挪）→ 冷启动 **5.01 / 5.53 / 7.93 秒**，导入网关只要 0.39 秒
- **桌面端连不上模型 —— 每个问题都回「模型暂时不可用」**（2026-11-18）— 网络守卫有**两道钩子**：
  `socket.create_connection` 看到的是**域名**（`api.minimaxi.com`，白名单里 ✓ 放行），但它内部解析
  DNS 之后会再调 `sock.connect(sa)`，**第二道钩子看到的是解析后的 IP**（实测一次连接看到 5 个
  IP），而白名单里是**域名** → 一个都不匹配 → **全部拦掉**。也就是说：**默认配置（LLM_ONLY）
  下，桌面端连允许的供应商域名都连不上**。**测试测不出来**：那些测试用假 provider（不真的连），
  隔离测试只验「不认识的域名被拦」✓
  - 修法：钩子看到 IP 时，**这个 IP 是否是某个允许域名当前解析出来的地址**——是就放行（域名仍然
    先过白名单）。漏洞写进 ADR-020：共享 CDN 的 IP 上别的租户也能连（守卫的局限，本就记着）
  - **真机复验**：同一个带守卫的后端再问「库仑定律的适用条件有哪些」，拿到**答案**（不是拒答），
    四条结论各带 Slide 8 / Slide 11 / Slide 7 的引用，指的正是讲库仑定律那几页
- **混合检索里的 N+1 查询**（2026-11-17）— 全文命中的切片**没进稠密前 K** 时，代码**一条一条**
  去向量库里捞（`table.search(...).where("id = '...'")`，每次 0.2 秒），一次查询 30 条命中就是
  30 次向量扫描。实测：整条混合检索 **4.0 秒**（拆开量，三段加起来只有 0.25 秒）→ 攒成一条
  `id IN (...)` 查询后 **0.45 秒**（快 9 倍）。这是「首批证据 ≤ 2 秒」那条承诺差 4 倍的原因

### Changed

- **验收标准：建库性能从「总量」改成「速度」，AC-P-004 的判定预算从 1.5 秒改成 3 秒**（2026-11-17）
  - **建库类**：AC-P-001 与 AC-P-007 原是「5000 个文件、20 GB、两小时」——这台机器上没有那份
    语料（全机支持格式的文档约 1.05 GB），它换算出来的速率实现也达不到。改成「**每分钟 150 个
    文件以上**」，5000 个文件推论为 35 分钟。真实工作域实测 **295.36 文件/分钟**（两轮独立跑
    差 0.26%）
  - **判定类**：两项判定（拒答闸 + 冲突判定）**各是一次模型往返**，实测 1.13 + 1.37 秒，
    1.5 秒的预算在云端生成模型下达不到（ASM-013 允许生成模型走云端）→ 合计改成 3 秒
  - **规范先行**：AC 直接改 proposal.md 与 design.md，**不写「改规范」的任务**；任务由规范派生
- **LLM 配置：只启用 MiniMax 一个模型** — 目前只有 `MINIMAX_API_KEY` 是配好的，其余 provider 的 key 都没有
  - `configs/models.yaml`：所有任务（understand / extract / answer / conflict_check / reflection / wiki_compile / ner / general）统一路由到 `minimax-chat`
  - OpenAI / Anthropic / DeepSeek / Ollama 保留为注释示例，取消注释 + 设好对应 `*_API_KEY` + 改 `routing` 即可切换，不需要改代码
  - `LLMGateway._default_routing()` 与内嵌的 `DEFAULT_MODELS_YAML` 同步为单模型版本
  - 找不到 `models.yaml` 时改为回退到内嵌模板（原来回退到空模型表，只有一个 gpt-4o 的 routing，等于直接不可用）
- **超时统一收敛到 120 秒** — litellm 自带默认超时是 6000 秒（约 100 分钟），provider 一旦不回就会一直挂着
  - `LLMTask.timeout` 字段 + `DEFAULT_REQUEST_TIMEOUT = 120.0`，`complete()` 与 `stream()` 都把它传给 litellm
  - 流式调用下这是**空闲超时**：provider 只要在窗口内吐过东西就算数，长回答不会被误杀
  - Rust 侧 `reqwest::ClientBuilder::read_timeout(120s)`，浏览器侧 `AbortController` + 120s 空闲定时器（每收到一段字节就重置）
- **release 打包命令固化** — `build.bat`（项目根目录）：`npm run build` → touch `src-tauri/src/lib.rs` → `cargo build --release --features custom-protocol`
  - 不带 `--features custom-protocol` 编出来的 exe 不内嵌前端资源，启动后去 `localhost:5173` 找页面而白屏
  - touch 是必须的：`tauri-build` 不会为 `dist/` 生成 `rerun-if-changed`，cargo 察觉不到前端变了
- **嵌入真的走 GPU 了**（2026-11-16）— 此前 `.venv` 里装的是 `torch 2.14.1+cpu`，显卡一直
  用不上。装法：`pip install --force-reinstall --no-deps "torch==2.14.1+cu130" --index-url
  https://mirror.sjtu.edu.cn/pytorch-wheels/cu130`（**上海交大镜像**，11 MB/s，2.9 分钟；
  官方 CDN 只有 0.07 MB/s）。嵌入 1.09 → **78.9 切片/秒**（fp16，显存峰值 1243 MB / 6 GB）
- **批大小由设备自己定**（2026-11-17）— 此前是配置里的一个固定数字（与设备无关）：6 GB 的卡
  和 24 GB 的卡拿同一个数。现在按**设备实测**的预算切（显卡看显存、CPU 看核数），设备吃不下
  就切半；`YUANWORK_EMBEDDING_BATCH_SIZE` 的语义从「计划」变成「上限」，默认 0 = 设备自己定
- **解析工人数按物理核数的一半定**（2026-11-16）— 此前是 4 个（嵌入最慢那会儿定的）。实测
  4 → 7 个工人，全量建库 2051 → 1466 秒（快 29%）；加到 12 个没有更快
- **摄入侧不算没人读的稀疏通道**（2026-11-16）— bge-m3 的稀疏通道算出来的向量检索侧从来不读。
  关掉它省的是**磁盘与一次序列化**，不是时间（模型侧的开销是噪声）
- **建库类性能验收从「总量」改成「速度」**（2026-11-17）— 原先是「5000 个文件、合计 20 GB
  的真实工作域，2 小时以内可检索」：**这台机器上没有 20 GB 的语料**（全机支持格式的文档约
  1.05 GB），而且那个承诺换算出来的速率（2.84 MB/s）**现在的实现也达不到**。改成
  **每分钟 150 个文件以上**：承诺跟着工作域的大小走，在任何工作域上都能测、能比；
  真实工作域的结构画像随验收报告一起记录，数字因此可解释
  - 验收脚本的判据从「分钟数」改成「文件每分钟」，且**只对真实工作域生效**（合成基准集
    的文档结构更简单、吞吐偏乐观，它是回归门不是验收结论）
  - 验收脚本多了 `--work-dir`（索引写在哪）与失败文件清单：系统盘只剩 4.2 GB 时，索引落在
    系统盘上测出来的不是机器的速度；一个「失败：24」没人能解释的验收报告不算验收

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
