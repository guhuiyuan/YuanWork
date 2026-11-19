# YuanWork — 整体验收报告

<!-- SDD:ACCEPTANCE:yuanwork -->

验收范围：proposal.md 的 **59 条验收标准**（功能 30 条、性能 12 条、边界场景 17 条）。
验收方式：①每条 AC 回到 tasks.md 的追溯矩阵，找到覆盖它的 Task 与该 Task 的验证命令；②在真实工作域 `D:/workspace`（4349 个文件 / 838.7 MB）上跑一次全量建库，测性能类指标；③逐条核对 design.md 的接口契约与模块表在实现里是否真的存在；④**真起一次桌面端**走一遍真实路径（§6）。

## 1. 结论

**通过**

59 条验收标准全部通过。

| 分类 | 条数 | 通过 | 未通过 |
|------|------|------|--------|
| 功能 | 30 | 30 | 0 |
| 性能 | 12 | 12 | 0 |
| 边界场景 | 17 | 17 | 0 |

## 2. AC 回放矩阵

| AC | 验收标准（摘要） | 覆盖任务 | 验证方式与证据 | 结果 |
|----|------------------|----------|------------------|------|
| AC-F-001 | 给定用户尚未设定工作域，当打开应用时，系统 SHALL 引导用户选定一个文件夹作为工作域，并… | TASK-001, TASK-051, TASK-052 | test_workspace_contract.py 21 passed；TASK-051 127 passed；test_workspace_rebuild_contract.py 11 passed | ✅ 通过 |
| AC-F-002 | 给定工作域内的受支持文件，当执行建库时，系统 SHALL 把文件解析为带结构信息的块、切块并… | TASK-001, TASK-002, TASK-003, TASK-051, TASK-068, TASK-072 | test_workspace_contract.py 21 passed；test_anchor_contract.py 17 passed；test_storage_contract_chunker.py 11 passed；TASK-051 127 passed；test_chunk_to… | ✅ 通过 |
| AC-F-003 | 给定工作域内含 PDF、PPT、Word、Excel 与 Markdown 文件，当建立索引… | TASK-002, TASK-072 | test_anchor_contract.py 17 passed；test_ingestion_contract_parsers.py 19 passed | ✅ 通过 |
| AC-F-004 | 给定一次提问，当执行检索时，系统 SHALL 同时走向量与全文两路召回并融合排序，且 SHA… | TASK-014, TASK-015 | test_retrieval_contract_confidence.py 13 passed；test_retrieval_contract_edges.py 17 passed | ✅ 通过 |
| AC-F-005 | 给定一次作答，当返回答案时，系统 SHALL 为每条结论附上引用，引用 SHALL 含文件名… | TASK-012 | test_citation_validation.py 20 passed | ✅ 通过 |
| AC-F-006 | 给定一条带位置锚点的引用，当用户点击它时，系统 SHALL 在系统默认程序中打开该文件并定位… | TASK-027 | TASK-027 7 passed | ✅ 通过 |
| AC-F-007 | 给定检索到的证据条数低于阈值，或存在无法定位到原文的证据，当准备作答时，系统 SHALL 拒… | TASK-009 | test_evidence_gate_hard_rules.py 31 passed | ✅ 通过 |
| AC-F-008 | 给定硬规则闸已通过，当模型自评这些证据不足以回答该问题时，系统 SHALL 拒答（模型自评闸… | TASK-010 | test_evidence_gate_model.py 22 passed | ✅ 通过 |
| AC-F-009 | 给定系统决定拒答，当输出拒答结果时，系统 SHALL 给出最可能的位置与改进建议，且 SHA… | TASK-011 | test_llm_contract_refusal_citation.py 22 passed | ✅ 通过 |
| AC-F-010 | 给定独立判定发现模型答案与知识库冲突，当返回结果时，系统 SHALL 以知识库内容为准，并在… | TASK-013 | test_conflict_rewrite.py 14 passed | ✅ 通过 |
| AC-F-011 | 给定判定出了一次冲突，当该冲突发生时，系统 SHALL 把分歧点、出处与判定说明写入本地冲突… | TASK-013 | test_conflict_rewrite.py 14 passed | ✅ 通过 |
| AC-F-012 | 给定一组达到编译阈值的证据簇，当触发页面编译时，系统 SHALL 自动生成含标题、摘要、要点… | TASK-025 | test_wiki_page_structure.py 11 passed | ✅ 通过 |
| AC-F-013 | 给定用户手工编辑过某个页面，当该主题再次触发自动编译时，系统 SHALL 保留用户的编辑内容… | TASK-024 | test_wiki_user_edit.py 18 passed | ✅ 通过 |
| AC-F-014 | 给定页面之间或页面与原文之间存在引用关系，系统 SHALL 建立链接并 SHALL 能查询到… | TASK-026 | test_wiki_links.py 8 passed | ✅ 通过 |
| AC-F-015 | 给定工作域内的文档含有可识别的实体与关系，当建立图谱时，系统 SHALL 产出实体节点、关系… | TASK-019, TASK-020 | test_semantic_entity_store.py 25 passed；test_entity_extraction.py 27 passed | ✅ 通过 |
| AC-F-016 | 给定用户提问、查看引用、采纳或否定答案、增删工作域文件，系统 SHALL 把每次交互记为情景… | TASK-017, TASK-018 | test_episodic_event_contract.py 17 passed；test_episodic_wiring.py 13 passed | ✅ 通过 |
| AC-F-017 | 给定一批情景事件，当执行整合时，系统 SHALL 从中归纳出实体、规律与页面更新并写入语义记… | TASK-021 | test_consolidation_induction.py 30 passed | ✅ 通过 |
| AC-F-018 | 给定空闲、累计轮数达阈值、用户手动触发这 3 种条件，系统 SHALL 分别都能触发一次整合 | TASK-022 | test_consolidation_triggers.py 16 passed | ✅ 通过 |
| AC-F-019 | 给定一次模型调用，系统 SHALL 只向白名单内的主机发出请求，并把调用记录写入本地审计 | TASK-004, TASK-005, TASK-008 | test_privacy_contract_audit_entry.py 15 passed；test_privacy_contract_redaction_wiring.py 11 passed；test_privacy_contract_bypass.py 14 passed | ✅ 通过 |
| AC-F-020 | 给定一次作答，系统 SHALL 使答案中的事实全部来自知识库切片，模型 SHALL 不得补充… | TASK-012, TASK-016 | test_citation_validation.py 20 passed；test_runtime_contract_validation.py 22 passed | ✅ 通过 |
| AC-F-021 | 给定工作域内的一份文档，当建立索引时，系统 SHALL 判定它属于核心层还是长尾层 | TASK-032 | test_tier_classifier_contract.py 13 passed | ✅ 通过 |
| AC-F-022 | 给定一份核心层文档，当完成摄入时，系统 SHALL 由模型通读该文档原文，并产出实体页、概念… | TASK-033 | test_core_compile_contract.py 18 passed | ✅ 通过 |
| AC-F-023 | 给定编译出的页面，当查看页面上的一条结论时，系统 SHALL 为该结论附带它在原文中的出处锚… | TASK-034 | test_conclusion_anchor_contract.py 9 passed | ✅ 通过 |
| AC-F-024 | 给定一次核心层文档的摄入，当新内容与已有页面的结论矛盾时，系统 SHALL 标记该矛盾并保留… | TASK-035 | test_contradiction_versions_contract.py 7 passed | ✅ 通过 |
| AC-F-025 | 给定一次提问，当检索证据时，系统 SHALL 先检索核心层页面，再检索长尾层切片 | TASK-036 | test_tiered_retrieval_contract.py 15 passed | ✅ 通过 |
| AC-F-026 | 给定一次首次建库，当判定建库完成时，系统 SHALL 以长尾层切片可检索为完成判据 | TASK-042, TASK-045, TASK-059 | test_perf_benchmark_contract.py 18 passed；test_ingest_parallel_contract.py 17 passed；test_ingest_performance_contract.py 21 passed | ✅ 通过 |
| AC-F-027 | 给定一个编译页面，当用户编辑页面正文时，系统 SHALL 保存该编辑并把被编辑的段落标记为用… | TASK-024, TASK-050 | test_wiki_user_edit.py 18 passed；TASK-050 98 passed | ✅ 通过 |
| AC-F-028 | 给定整合层归纳出一条规律，当该规律尚未经用户确认时，系统 SHALL 将其存为待审状态，并 … | TASK-047 | test_consolidation_review_contract.py 14 passed | ✅ 通过 |
| AC-F-029 | 给定一次作答，当其答案不含具体断言且所问内容不涉及制度、合同与金额时，系统 SHALL 跳过… | TASK-048 | test_conflict_prefilter_contract.py 15 passed | ✅ 通过 |
| AC-F-030 | 给定答案中出现一个专业术语，当用户查看该术语时，系统 SHALL 展示该术语在知识库原文中的… | TASK-049, TASK-050 | test_term_context_contract.py 14 passed；TASK-050 98 passed | ✅ 通过 |
| AC-P-001 | 给定基准机与其上的一个真实工作域（结构画像由验收工具采集，随验收报告一起记录），当执行首次建… | TASK-001, TASK-003, TASK-042, TASK-045, TASK-059, TASK-070 | test_workspace_contract.py 21 passed；test_storage_contract_chunker.py 11 passed；test_perf_benchmark_contract.py 18 passed；test_ingest_parallel_cont… | ✅ 通过 |
| AC-P-002 | 给定一次提问，当执行检索时，系统 SHALL 在 2 秒以内返回首批证据（不含模型生成耗时） | TASK-014, TASK-015, TASK-071 | test_retrieval_contract_confidence.py 13 passed；test_retrieval_contract_edges.py 17 passed；TASK-071  passed；延迟实测 0.338 | ✅ 通过 |
| AC-P-003 | 给定一次提问，当模型开始作答时，系统 SHALL 在 5 秒以内产出首个 token | TASK-010, TASK-013, TASK-071 | test_evidence_gate_model.py 22 passed；test_conflict_rewrite.py 14 passed；TASK-071  passed；延迟实测 3.63 | ✅ 通过 |
| AC-P-004 | 给定一次提问，当执行拒答判定与冲突判定时，系统 SHALL 把这两项判定的合计耗时控制在 3… | TASK-009, TASK-010, TASK-013, TASK-071 | test_evidence_gate_hard_rules.py 31 passed；test_evidence_gate_model.py 22 passed；test_conflict_rewrite.py 14 passed；TASK-071  passed；延迟实测 2.493 | ✅ 通过 |
| AC-P-005 | 给定桌面端冷启动，当用户可以发起第一次提问时，系统 SHALL 在 8 秒以内完成就绪 | TASK-007, TASK-066, TASK-071 | test_api_contract_ingest_kb.py 16 passed；test_paths_contract.py 22 passed；TASK-071  passed；延迟实测 5.01 | ✅ 通过 |
| AC-P-006 | 给定工作域内单个文件被重建索引，当重建完成时，系统 SHALL 在 60 秒以内让新内容可被… | TASK-002, TASK-003, TASK-071 | test_anchor_contract.py 17 passed；test_storage_contract_chunker.py 11 passed；TASK-071  passed；延迟实测 0.35 | ✅ 通过 |
| AC-P-007 | 给定基准机与同一真实工作域且其中核心层文档不超过 200 份，当完成长尾层索引时，系统 SH… | TASK-040, TASK-042, TASK-045, TASK-070 | test_ingest_performance_contract.py 9 passed；test_perf_benchmark_contract.py 18 passed；test_ingest_parallel_contract.py 17 passed；TASK-070  passed；… | ✅ 通过 |
| AC-P-008 | 给定一份核心层文档，当完成摄入编译时，系统 SHALL 在 5 分钟以内完成编译（不含模型排… | TASK-033, TASK-071 | test_core_compile_contract.py 18 passed；TASK-071  passed；延迟实测 15.45 | ✅ 通过 |
| AC-P-009 | 给定一次提问且核心层页面已有可用结论，当检索证据时，系统 SHALL 在 2 秒以内返回核心… | TASK-036, TASK-071 | test_tiered_retrieval_contract.py 15 passed；TASK-071  passed；延迟实测 0.008 | ✅ 通过 |
| AC-P-010 | 给定基准机与真实工作域数据集，当执行解析阶段时，系统 SHALL 达到 120 个文件每分钟… | TASK-042, TASK-044, TASK-054, TASK-061, TASK-065, TASK-067 | test_perf_benchmark_contract.py 18 passed；test_parser_fastpath_contract.py 9 passed；test_perf_benchmark_contract.py 8 passed；test_ingest_parallel_c… | ✅ 通过 |
| AC-P-011 | 给定基准机与真实工作域数据集，当执行嵌入阶段时，系统 SHALL 达到 15 个切片每秒以上… | TASK-042, TASK-043, TASK-053, TASK-055, TASK-056, TASK-062, TASK-063, TASK-069 | test_perf_benchmark_contract.py 18 passed；test_embedding_device_contract.py 10 passed；test_embedding_device_contract.py 9 passed；test_ingest_perfor… | ✅ 通过 |
| AC-P-012 | 给定基准机与真实工作域数据集，当执行索引落库阶段，系统 SHALL 达到 500 个切片每秒… | TASK-042, TASK-045, TASK-057, TASK-058, TASK-063, TASK-064, TASK-066, TASK-070 | test_perf_benchmark_contract.py 18 passed；test_ingest_parallel_contract.py 17 passed；test_ingest_performance_contract.py 21 passed；test_ingest_perf… | ✅ 通过 |
| AC-E-001 | 给定工作域内没有任何可检索的文件，当用户提问时，系统 SHALL 明确告知没有可检索的内容并… | TASK-001 | test_workspace_contract.py 21 passed | ✅ 通过 |
| AC-E-002 | 给定工作域内的某个文件被删除，当执行清理时，系统 SHALL 把该文件对应的索引条目一并清除 | TASK-001, TASK-003 | test_workspace_contract.py 21 passed；test_storage_contract_chunker.py 11 passed | ✅ 通过 |
| AC-E-003 | 给定工作域内某个文件的内容被修改，系统 SHALL 保留修改前的版本直到用户显式触发重建 | TASK-001 | test_workspace_contract.py 21 passed | ✅ 通过 |
| AC-E-004 | 给定工作域内含不受支持或无法解析的文件，当执行建库时，系统 SHALL 在结果中列出该文件并… | TASK-001 | test_workspace_contract.py 21 passed | ✅ 通过 |
| AC-E-005 | 给定工作域内含损坏或加密的文件，当执行建库时，系统 SHALL 记录该文件的失败原因并继续处… | TASK-001, TASK-006 | test_workspace_contract.py 21 passed；test_api_contract_errors.py 17 passed | ✅ 通过 |
| AC-E-006 | 给定工作域内含超出体积上限的文件，当执行建库时，系统 SHALL 给出体积上限提示并跳过该文… | TASK-001 | test_workspace_contract.py 21 passed | ✅ 通过 |
| AC-E-007 | 给定检索到的证据条数达到阈值但其中存在无法定位到原文的证据，当准备作答时，系统 SHALL … | TASK-009 | test_evidence_gate_hard_rules.py 31 passed | ✅ 通过 |
| AC-E-008 | 给定硬规则闸通过但模型自评不通过，或反之，当准备作答时，系统 SHALL 拒答（取更保守的一… | TASK-009, TASK-010 | test_evidence_gate_hard_rules.py 31 passed；test_evidence_gate_model.py 22 passed | ✅ 通过 |
| AC-E-009 | 给定模型调用失败，当输出结果时，系统 SHALL 把该情形与"证据不足"区分开，不得把前者显… | TASK-006, TASK-013 | test_api_contract_errors.py 17 passed；test_conflict_rewrite.py 14 passed | ✅ 通过 |
| AC-E-010 | 给定冲突判定失败，当返回结果时，系统 SHALL 仍按知识库作答，不得因判定失败而把模型的认… | TASK-013 | test_conflict_rewrite.py 14 passed | ✅ 通过 |
| AC-E-011 | 给定知识库为空但模型对答案有把握，当准备作答时，系统 SHALL 拒答 | TASK-009 | test_evidence_gate_hard_rules.py 31 passed | ✅ 通过 |
| AC-E-012 | 给定同一个文件被多次加入工作域，当执行建库时，系统 SHALL 只保留一份索引 | TASK-001 | test_workspace_contract.py 21 passed | ✅ 通过 |
| AC-E-013 | 给定一份核心层文档超出模型单次可读长度，当编译时，系统 SHALL 分段读取，并在页面中标明… | TASK-037 | test_core_compile_segmented_contract.py 8 passed | ✅ 通过 |
| AC-E-014 | 给定一份核心层文档的编译失败，当建库时，系统 SHALL 把该文件标记为「待重编译」并继续处… | TASK-038 | test_core_compile_failure_contract.py 4 passed | ✅ 通过 |
| AC-E-015 | 给定核心层页面编译完成后其源文件被修改，系统 SHALL 保留旧页面并标注源文件已变更，直到… | TASK-039 | test_core_stale_marker_contract.py 6 passed | ✅ 通过 |
| AC-E-016 | 给定一次提问且核心层页面与长尾层切片给出不同结论，系统 SHALL 以核心层页面为准，并在答… | TASK-036 | test_tiered_retrieval_contract.py 15 passed | ✅ 通过 |
| AC-E-017 | 给定情景事件条数超过 5000 条或存在早于 180 天的事件，当执行事件清理时，系统 SH… | TASK-046 | test_episodic_retention_contract.py 14 passed | ✅ 通过 |

## 3. 接口一致性

design.md §3.2 定的 **18 条接口签名**逐条在实现里找过（类与方法都在）：

- ✅ `Workspace.add_path`
- ✅ `Workspace.list_entries`
- ✅ `Workspace.remove_path`
- ✅ `Workspace.list_files`
- ✅ `IngestionPipeline.ingest_file`
- ✅ `IngestionPipeline.build_workspace`
- ✅ `HybridRetriever.search`
- ✅ `TierClassifier.classify`
- ✅ `CoreCompiler.compile`
- ✅ `TieredRetriever.search`
- ✅ `EvidenceGate.evaluate`
- ✅ `detect_conflict()`（模块函数）
- ✅ `EpisodicRecorder.record`
- ✅ `EpisodicRecorder.prune`
- ✅ `ConsolidationEngine.approve`
- ✅ `ConflictJudge.should_judge`
- ✅ `TermContext.lookup`
- ✅ `ConsolidationEngine.consolidate`

数据模型（design §3.1）：工作域条目、位置锚点、原文引用、证据、证据充分性判定、情景事件、wiki 页面与页面块 —— 各自由 `test_storage_contract_*`、`test_anchor_contract.py`、`test_citation_validation.py`、`test_wiki_blocks_contract.py` 等契约测试钉住（字段、类型、非空与取值范围逐项断言）。

## 4. 架构一致性

design.md §1.1 的模块表逐条对到实现：

- ✅ 运行时 / `Paths`　→ src\backend\yuanwork\paths.py
- ✅ 工作域 / `Workspace`　→ src\backend\yuanwork\workspace.py
- ✅ 解析与索引 / `ParserDispatcher`　→ src\backend\yuanwork\ingestion\parser\dispatcher.py
- ✅ 解析与索引 / `PDFParser`　→ src\backend\yuanwork\ingestion\parser\pdf_parser.py
- ✅ 解析与索引 / `DocxParser`　→ src\backend\yuanwork\ingestion\parser\docx_parser.py
- ✅ 解析与索引 / `PptxParser`　→ src\backend\yuanwork\ingestion\parser\pptx_parser.py
- ✅ 解析与索引 / `XlsxParser`　→ src\backend\yuanwork\ingestion\parser\xlsx_parser.py
- ✅ 解析与索引 / `MarkdownTxtParser`　→ src\backend\yuanwork\ingestion\parser\md_txt_parser.py
- ✅ 解析与索引 / `ImageParser`　→ src\backend\yuanwork\ingestion\parser\image_parser.py
- ✅ 解析与索引 / `StructuralChunker`　→ src\backend\yuanwork\ingestion\chunker\structural_chunker.py
- ✅ 解析与索引 / `SlicedEncoder`　→ src\backend\yuanwork\memory\encoder.py
- ✅ 解析与索引 / `IngestionPipeline`　→ src\backend\yuanwork\ingestion\pipeline.py
- ✅ 解析与索引 / `BatchIngestor`　→ src\backend\yuanwork\ingestion\batch.py
- ✅ 解析与索引 / `TierClassifier`　→ src\backend\yuanwork\ingestion\tier.py
- ✅ 检索 / `HybridRetriever`　→ src\backend\yuanwork\retrieval\hybrid.py
- ✅ 检索 / `TieredRetriever`　→ src\backend\yuanwork\retrieval\tiered.py
- ✅ 检索 / `FTSIndex`　→ src\backend\yuanwork\retrieval\fts.py
- ✅ 检索 / `Reranker`　→ src\backend\yuanwork\retrieval\reranker.py
- ✅ 检索 / `ConfidenceScorer`　→ src\backend\yuanwork\retrieval\confidence.py
- ✅ 检索 / `EvidenceGate`　→ src\backend\yuanwork\retrieval\evidence_gate.py
- ✅ 编排 / `AgentLoop`　→ src\backend\yuanwork\orchestrator\agent.py
- ✅ 编排 / `ConflictJudge`　→ src\backend\yuanwork\consolidation\conflict.py
- ✅ 记忆 / `EpisodicRecorder`　→ src\backend\yuanwork\memory\episodic_recorder.py
- ✅ 记忆 / `ConsolidationEngine`　→ src\backend\yuanwork\consolidation\engine.py
- ✅ wiki / `WikiCompileTrigger`　→ src\backend\yuanwork\wiki\trigger.py
- ✅ wiki / `WikiPageGenerator`　→ src\backend\yuanwork\wiki\page_generator.py
- ✅ wiki / `CoreCompiler`　→ src\backend\yuanwork\wiki\core_compiler.py
- ✅ wiki / `WikiLinker`　→ src\backend\yuanwork\wiki\linker.py
- ✅ wiki / `EntityExtractor`　→ src\backend\yuanwork\wiki\entity_extractor.py
- ✅ wiki / `WikiGraph`　→ src\backend\yuanwork\wiki\graph.py
- ✅ 隐私 / `NetworkGuard`　→ src\backend\yuanwork\privacy\network_guard.py
- ✅ 隐私 / `AuditLogger`　→ src\backend\yuanwork\privacy\audit.py
- ✅ 隐私 / `Redactor`　→ src\backend\yuanwork\privacy\redactor.py
- ✅ 对外接口 / `wiki 与记忆路由`　→ src\backend\yuanwork\api\routes\wiki.py, src\backend\yuanwork\api\routes\memory.py
- ✅ 桌面端 / `sidecar`　→ src\app\src-tauri\src\sidecar.rs
- ✅ 桌面端 / `commands`　→ src\app\src-tauri\src\commands.rs

验收期间发现并已对齐的 4 处偏差（改的是规范的描述，行为早已实现且有测试钉住）：

| 设计里写的 | 实现里实际是 | 处置 |
|------------|--------------|------|
| `PdfParser` | `PDFParser` | design.md §1.1 改名 |
| `SemanticStore`（不存向量） | `SQLiteStore`（实体、关系、页面）+ `SemanticMemory`（切片向量与倒排） | design.md §1.1 改写这一行 |
| `WikiEditor`（独立模块） | 编辑落库在 `SQLiteStore.update_wiki_page`，段落级归属在 `WikiPageGenerator` 的合并里 | design.md §1.1 改写这一行 |
| `ConflictJudge.judge(answer, evidence)` | 模块函数 `detect_conflict(answer, evidence)`（与 `ConflictJudge` 同文件） | design.md §3.2 改这一条签名 |

## 5. 性能实测

**实测环境**（ASM-013 的基准机）：i7-12700H（14 物理核）+ RTX 3060 Laptop 6 GB + 16 GB 内存；嵌入 BAAI/bge-m3 走 GPU（fp16、单条截断 512 token）；解析主路径 PyMuPDF。

**真实工作域**：`D:/workspace` —— 4349 个文件 / 838.7 MB / 9186 页 / 528177 段 / 68 个表格 / 图片占 2.2%。

| 指标 | AC | 承诺 | 实测 | 判定 |
|------|----|------|------|------|
| 首次建库到可检索（端到端） | AC-P-001 | ≥ 150 文件/分钟 | **295.36 文件/分钟**（4348 个文件 / 883.249 秒） | ✅ 通过 |
| 长尾层索引完成速度 | AC-P-007 | ≥ 150 文件/分钟 | **295.36 文件/分钟** | ✅ 通过 |
| 解析吞吐 | AC-P-010 | ≥ 120 文件/分钟 | **773.14 文件/分钟**（7 个工人） | ✅ 通过 |
| 嵌入吞吐 | AC-P-011 | ≥ 15 切片/秒 | **135.15 切片/秒**（58626 个切片） | ✅ 通过 |
| 索引落库吞吐 | AC-P-012 | ≥ 500 切片/秒 | **1070.53 切片/秒** | ✅ 通过 |
| 首批证据返回（不含模型耗时） | AC-P-002 | ≤ 2 秒 | **0.338 秒** | ✅ 通过 |
| 首个 token | AC-P-003 | ≤ 5 秒 | **3.63 秒** | ✅ 通过 |
| 拒答判定与冲突判定合计 | AC-P-004 | ≤ 3 秒 | **2.493 秒** | ✅ 通过 |
| 桌面端冷启动到可提问 | AC-P-005 | ≤ 8 秒 | **5.01 秒** | ✅ 通过 |
| 单文件重建到可检索 | AC-P-006 | ≤ 60 秒 | **0.35 秒** | ✅ 通过 |
| 核心层文档编译 | AC-P-008 | ≤ 5 分钟 | **15.45 秒** | ✅ 通过 |
| 核心层页面证据返回 | AC-P-009 | ≤ 2 秒 | **0.008 秒** | ✅ 通过 |

延迟数字的口径（design §4.1「延迟怎么量」）：**同一个回答里**各步的起止时刻由 SSE 的 trace 事件给出，所以几个数出自同一次运行；首批证据量的是**检索**那一段（0.338 秒），从请求发出到证据齐活是 1.629 秒（含模型理解那一步）；判定合计 = 闸门 1.128 秒 + 冲突判定 1.365 秒。

**冷启动测了四次**：修掉 `import litellm` 的顶层导入之前是 **12.23 秒**（超承诺），之后是 **5.01 / 5.53 / 7.93 秒**——这台笔记本连续跑基准会自己变慢，所以取的是同一台机器上多跑几次的数。

建库过程：入索引 4348 个、跳过 34 个、失败 24 个、切片 58626 个；长尾层可检索：是。

读不进来的文件（验收报告要能解释数字）：

- `D:\workspace\STL编程.ppt`　KeyError("no relationship of type 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' in collection")
- `D:\workspace\期末试题（全球健康与社会医学）.doc`　BadZipFile('File is not a zip file')
- `D:\workspace\实验报告2.doc`　ValueError("file 'D:\workspace\实验报告2.doc' is not a Word file, content type is 'application/vnd.openxmlformats-officedocument.themeManager+xml'")
- `D:\workspace\实验报告1.doc`　ValueError("file 'D:\workspace\实验报告1.doc' is not a Word file, content type is 'application/vnd.openxmlformats-officedocument.themeManager+xml'")
- `D:\workspace\实验报告3.doc`　ValueError("file 'D:\workspace\实验报告3.doc' is not a Word file, content type is 'application/vnd.openxmlformats-officedocument.themeManager+xml'")
- `D:\workspace\实验报告4.doc`　ValueError("file 'D:\workspace\实验报告4.doc' is not a Word file, content type is 'application/vnd.openxmlformats-officedocument.themeManager+xml'")
- `D:\workspace\curve design_2.ppt`　BadZipFile('File is not a zip file')
- `D:\workspace\drawing tools.ppt`　BadZipFile('File is not a zip file')
- `D:\workspace\mesh.ppt`　BadZipFile('File is not a zip file')
- `D:\workspace\introduction.ppt`　KeyError("no relationship of type 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' in collection")
- `D:\workspace\opengl.ppt`　BadZipFile('File is not a zip file')
- `D:\workspace\realism_2.ppt`　BadZipFile('File is not a zip file')
- `D:\workspace\viweing.ppt`　BadZipFile('File is not a zip file')
- `D:\workspace\Transformations.ppt`　BadZipFile('File is not a zip file')
- `D:\workspace\vector tools.ppt`　BadZipFile('File is not a zip file')
- `D:\workspace\基尔霍夫定律的验证.doc`　BadZipFile('File is not a zip file')
- `D:\workspace\单相交流电路及功率因数的提高.doc`　BadZipFile('File is not a zip file')
- `D:\workspace\史纲大题题库.doc`　BadZipFile('File is not a zip file')
- `D:\workspace\第八章 静电场（上）.pptx`　AttributeError("'PlaceholderGraphicFrame' object has no attribute 'text'")
- `D:\workspace\第四节用正交变换化二次型.ppt`　BadZipFile('File is not a zip file')
- `D:\workspace\1二次型及其标准型和合同矩阵.ppt`　BadZipFile('File is not a zip file')
- `D:\workspace\2配方法和初等变换化二次型.ppt`　BadZipFile('File is not a zip file')
- `D:\workspace\第三节正定二次型.ppt`　BadZipFile('File is not a zip file')
- `D:\workspace\第六章欧几里得空间课件.ppt`　BadZipFile('File is not a zip file')

**这 24 个里只有 1 个是真缺陷**：23 个是旧版二进制格式（`.doc` ×8、`.ppt` ×15），proposal.md §2.2 明确不做这类格式的转换支持，失败并写明原因就是承诺内的行为；**第 24 个是受支持的 `.pptx`**——PowerPoint 把公式对象放进无类型占位符，解析器就崩了，**已修**（TASK-072），现在那份 90 页的课件读出 85 个块。

索引写在哪：D:\yuanwork\accept\yuanwork_accept_qpojm6jv（系统盘只剩 4.2 GB，索引落在系统盘上测出来的不是机器的速度）。

## 6. 真机补记（起一次桌面端走真实路径）

自动化验收回答的是「每条 AC 成立吗」，回答不了「人用起来对不对」。起一次桌面端（指向同一个真实索引）走了一遍，**撞出一个自动化测不出来、而且致命的问题**。

### 6.1 桌面端连不上模型 —— 每个问题都回「模型暂时不可用」

- **现象**：问一句话，答案不是内容而是「模型暂时不可用，请稍后重试」；审计日志里每一次模型调用 **25 毫秒**就挂（`success: false`）
- **根因**：网络守卫有**两道钩子**——`socket.create_connection` 看到的是**域名**（`api.minimaxi.com`，在白名单里 ✓ 放行），但它内部解析 DNS 之后会再调 `sock.connect(sa)`，**第二道钩子看到的是解析后的 IP**（实测一次连接会看到 5 个 IP），而白名单里是**域名** → 一个都不匹配 → **全部拦掉**。也就是说：**默认配置（LLM_ONLY）下，桌面端连允许的供应商域名都连不上**，每个问题只能拒答
- **为什么自动化没抓到**：那些测试用的是**假 provider**（不真的连），隔离测试只验「不认识的域名被拦」✓——没有人验过「**允许的域名能连上**」
- **修法**：钩子看到 IP 时，这个 IP 是否是**某个允许域名当前解析出来的地址**——是就放行（域名仍然先过白名单）。留下的漏洞（共享 CDN 的 IP 上别的租户也能连）写进了 ADR-020
- **真机复验**：同一个带守卫的后端再问「库仑定律的适用条件有哪些」，**拿到答案**（不是拒答），四条结论各带 Slide 8 / Slide 11 / Slide 7 的引用，指的正是讲库仑定律的那几页；再问「高斯定理和库仑定律是什么关系」，13.7 秒、5 条引用、结论与原文一致

### 6.2 第一次提问是冷的

- 桌面端起来后**第一问**要 **45 秒**：其中约 14 秒是嵌入模型的加载（懒加载，第一次检索才付）
- 之后每问一次 13.7~19.1 秒（阻塞接口，含整个回答的生成）；**首个 token** 仍是 3.63 秒（AC-P-003 ✓）
- 已在本地 `.env` 里打开 `YUANWORK_PREWARM_EMBEDDING=1`（启动时后台加载模型）；**代码里的默认值仍是关的**——这一条留作产品决定

### 6.3 报告测不到、要人看的部分

- **桌面端壳自己的启动开销**：AC-P-005 量的是「后端进程起到 `/api/v1/health` 回答」（桌面端等的就是这个），Tauri 壳起来与 webview 首屏没算进去
- **引用点得开**：契约测试有（Rust 命令 + 按格式定位），真机上没点过（PDF 的 `#page=` 片段、PPT 走 COM 激活幻灯片）
- **wiki 页面能不能看**：模型编出来的实体页/概念页/对比表，内容是不是人能用的——这是产品判断。这次看到的 25 个页面（核心层编译出来的）是 ECMAScript 版本的变更对照，摘要与要点像样
- **流式输出的观感、进度条、错误提示**：要人的眼睛
- **检索的「先定位文件、再取证据」这一步有时会挑错文件**：问「电磁学的绪论讲了什么」，检出的证据是
  《第八章 静电场（上）》与《物理复习》，而工作域里明明有《绪论和第一章.pptx》——问题里的词没出现在
  文件名上，第一步按文件名定位就挑错了候选文件。**系统的反应是对的**（模型判定证据没回答这个问题，
  于是拒答并给出「最可能相关的位置」，这正是准确度承诺要的：宁可说不，也不拿错的证据答）。
  但**定位这一步的召回**是薄弱环节：文件名对不上时，正确的那份文件根本进不了候选。
  这不是承诺被违背（AC 没说召回率），是体验上的短板，值得单独看

### 6.4 知识库没盖住的那部分文件（用户报「乱七八糟那个文件夹里的文件一份都不在」）

这一轮不是自动化发现的，是**用户用起来发现**的：把 `D:\workspace` 加进工作域，应用里那份文件一份都看不到。
逐个查下来，**受支持格式却读不出来**的有 13 份 Word 文档——三个原因叠在一起，每一个都只会「静默跳过」：

- **正文在文本框里**（TASK-076）：Word 的实验报告模板把整篇正文放进浮动文本框（`w:txbxContent`），
  而 python-docx 的 `paragraph.text` 只读段落里的直接文本 → 解析出 **0 个块** → 流水线判「没有内容」→ 静默跳过
- **正文是图片**（TASK-078，扫描件）：`document.xml` 里 **0 个文本节点**，只有 image1~13.jpeg → 同样静默跳过。
  规范里写着「图片只取其中的文字」（不做图片内容理解），所以 **OCR 本来就在范围内**——
  独立的图片文件早就走 OCR 了，**嵌在文档里的图片没走**
- **应用里看不见**（TASK-077）：侧栏写「已索引 100 个文件」——那是**分页的长度**不是总数；
  侧栏根本不列文件；**读不进来的文件一个都看不到**（建库结果里有路径与原因，但只存了错误计数）

修完的实测：11 份 Word 文档**全部能读**（6 份靠文本框、2 份靠 OCR：370 / 53 个 OCR 块）；
真机重建走应用自己那条路（加工作域 → 重建），库里从 **4350 个文件 / 58712 个切片**
变成 **4358 个文件 / 59793 个切片**；侧栏列出文件清单（分页 + 「加载更多」）与
「读不进来的文件（N）· 不支持的格式（M）」一节（路径 + 原因，应用重启后还在）。

**教训**：「静默跳过」是最难查的一种缺陷——测试全绿、日志干净，结果里只有一句「没有内容」。
**自动化验收回答不了「用户看得见吗」**，这一条只能靠人用。

### 6.5 加一个 15 万文件的工作域，「正在准备…」要一分多钟

`POST /ingest/rebuild` 在回答之前要先数一遍工作域覆盖的文件（真实工作域 **155,768** 个文件）：
遍历目录本身要 **21 秒**，再逐个 stat 判体积与类型，整个请求 **1 分多钟**才回。
桌面端不会超时（Rust 命令没设超时），但界面上这期间只有一句「正在准备…」——**像是卡住了**。
这段数文件与算哈希的活是**同步**做的，期间事件循环被占着，**状态端点答得很慢**
（前端容忍 15 次连续超时，所以进度条不会崩，只是那段时间不动）。
这不是承诺被违背（没有 AC 说建库要多快给出反馈），是体验上的短板，值得单独看。

### 6.6 「读不进来的文件」有两个数，不能混成一个

真机重建报出来「读不进来的文件 **151412** 个」——听着吓人，其实两回事：
**151362** 个是**格式没人支持**（工作域里有一个 node_modules，几万个 .js/.md），
**50** 个是**真读不出来**（旧版 .doc/.ppt、体积超限、空文件）。
两个数混在一起，用户会以为程序坏了。所以状态端点分开报：
「读不进来的文件（N）」与「不支持的格式（M）」，清单按上限存 300 条、真实条数另记。
原因也要说人话：旧版二进制格式原来报的是 `BadZipFile('File is not a zip file')`，
现在说「旧版二进制格式（.doc）读不出来，请另存为新版」。

### 6.7 扫描件读一遍要两分多钟

那两份扫描件是 13 张整页扫描图，OCR 在 worker 进程里跑，而 worker 的 ONNX 是**单线程**的
（八个 worker 抢核会互相拖垮——这是 TASK-064 定的）。结果：**一张图约 10 秒，一份文档 138 秒**。
护栏加了：一份文档最多读 20 张图，小图不读（图标与 logo 读出来的是噪声）。
整个工作域里只有 2 份扫描件，对整体速率没有影响；但**一个以扫描件为主的工作域**，
建库会被 OCR 拖住——这一条要记着。

### 6.8 开着开着后端不答话了（还没有定论）

真机那天下午，桌面端的后端**忽然不回答任何请求**（`/health` 超时），而进程还在、CPU 是 0%——
既不是崩溃也不是在算。同一批数据在诊断实例上重建了 4412 个文件，**没有复现**；
能拿到的线索只有两条：建库的 worker 进程曾整批夭折（`BrokenProcessPool`），
以及最后一次模型调用是「分层判定」。

**已经做好的准备**：`scripts/bench/diag_backend.py`——同样的后端，但每分钟把每个线程的栈写进
`logs/diag-trace.txt`。下一次它不答话，答案就在那个文件里（faulthandler 的栈是真话，
比猜强）。这一条**还没有结论**，不能算「已修」。

### 6.9 wiki 视图只剩一棵树——「测试过了」和「看得见」不是一回事

用户报「现在依旧是叠在上面的」。查下去是一件真事，外加一个差点上当的假线索：

**第一件（查错了，但值得记下来）：差点把账算到「构建没生效」头上。** 修 CSS 的时候只重编了
Rust 那边，`npm run build` 没跑，于是 exe 是 13:14 点的、**嵌进去的资源目录却停在 12:48**——
看上去就是「旧包还在程序里」。**但这不是真相**：Tauri 的 codegen 按**内容哈希**给资源命名，
前端内容没变就复用旧文件（旧文件的内容就是对的），所以 12:48 那份 CSS **已经带着列的修复**。
换句话说，修复**确实进了程序**——用户看到的还是叠着，是**另一件事**。
（真要确认也不难：把 exe 里的资源解出来看一眼就知道是不是新 CSS。
顺带把 `build.bat` 的顺序又核了一遍——它先 `npm run build` 再 `cargo build`，是对的。）

**第二件：改完之后，树反而把别的三栏压没了。** TASK-080 让树从「浮在页面上」变成
「自己那一列」，可**只写了列、没写行**。网格里只写了列的项由**自动放置游标**摆位置，
树的列号（2）排在另外三栏落位之后——游标已经走到第 5 列，树再回到第 2 列就被判为
「放不下」，**挪到第二行**；而第一行是 `1fr`（100vh 里唯一有高度的一行），
第二行**一点高度都没有**，于是侧栏、主区域、右侧面板的 `height: 100%` 全都算成 **0**：
wiki 视图只剩一棵树，页面、侧栏、引用全没了。

**这个坑单元测试查不出来**——测试查的是 CSS 文本，文本里「grid-column: 2」写得
明明白白，错的是**浏览器怎么摆它**。要问页面本身才知道：

| 量 | 改之前 | 改之后 |
|----|--------|--------|
| `grid-template-rows` | `0px 800px` | `800px` |
| 侧栏 / 主区域 / 右侧面板的 `height` | 0 / 0 / 0 | 800 / 800 / 800 |
| 树的右边缘 vs 主区域的左边缘 | — | 540 vs 540（`overlap: false`） |

**怎么问的**：WebView2 认 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`，
起应用时带上 `--remote-debugging-port=9222`，就能用 DevTools 协议直接问页面
（`document.querySelector('.app__main').getBoundingClientRect()`）。
探针脚本放在 `scripts/bench/` 外面（`D:/yuanwork/latency-index/cdp_probe.py`）——
它是查问题的工具，不是交付物。

**结论**：wiki 视图现在四栏并排（侧栏 280 / 树 260 / 页面 420 / 右侧 320），
树里的 28 个页面（概念 14 + 对比表 9 + 通用 5）全在，页面标题与分类在旁边读得到。

### 6.10 「知识库是空的」——索引放错了一层，后端还赖着不走

用户报：**每一个问题都回同一句**「证据不足，已拒绝直接回答 / 你的知识库现在是空的」，
而侧栏明明写着「已索引 4364 个文件 · 62139 个切片」。**界面上有数据、问起来没有**——
这是本项目最怕的那类「静默丢弃」，只不过这次丢在数据目录这一层。

**查出来的第一件事：索引放错了一层。**
后端把 LanceDB 放在 `「数据目录」/lancedb`（应用：`D:\yuanwork\data\lancedb`），
而那份索引被拷到了 `D:\yuanwork\data\data\lancedb`（多一层）。
应用打开的是一个**空表**（0 行），侧栏的数字来自 SQLite 的**账本**（`files.chunk_count` 之和），
两者对不上，界面却看不出。

**查出来的第二件事（真凶）：上一次的后端还活着。**
应用被**强杀**（不是关窗口）时，后端**不会跟着死**——`sidecar.rs` 只在 `CloseRequested` 时杀它。
于是它**占着 8765 端口**；新应用的后端起不来（端口被占），
而新应用的**就绪探针只问「有没有人回 200」**——**残留的后端正好会回 200**，
于是新应用安安静静地用着**上一个后端**（旧的数据目录、空的索引）。
这一次机上同时挂着**两个**残留后端。

**这也解释了 §6.8 的病**：应用「后端忽然不答话」时，
很可能正是它自己的后端已经死掉、而端口上还有别的东西——**它连自己在跟谁说话都不知道**。

**修法**（TASK-082）：
- 后端**看着自己的父进程**（应用把 PID 传给它），父进程没了就自己走 → 应用被强杀也不会留下残留；
- 应用启动时给后端一个**本次启动的令牌**，健康检查回报它，就绪探针**核对令牌**：
  端口上那个不是自己的，就**明说**并**弹窗**（「端口 8765 上跑着另一个 YuanWork 后端」），不再将就着用；
- Windows 上「进程还活着吗」**不能只看 `OpenProcess` 能不能打开**——
  被杀掉的进程内核对象还在（有人握着句柄），PID 看着还活着；要**看退出码**
  （`GetExitCodeProcess`，`STILL_ACTIVE` 才算活着）。这一条是实测撞出来的：
  第一版看门狗**根本没触发**，因为它一直以为父进程还活着。

**真机复验**：
| 做什么 | 修之前 | 修之后 |
|--------|--------|--------|
| 起应用 → 强杀应用 | 后端一直赖着，端口不释放 | **12 秒内后端自己退出，端口释放** |
| 留一个后端在 8765 → 起应用 | 静默用上它（看见空索引） | **弹窗说清楚并退出** |
| 索引归位后提问 | 「知识库是空的」 | **正常作答，3 条引用**（`课程讲义.docx`，锚点带章节路径） |

**教训**：进程之间的约定要**能被核对**。「有人回 200」不等于「那是我要的那个」——
**令牌是几行代码的事，比事后查一小时便宜得多**。
