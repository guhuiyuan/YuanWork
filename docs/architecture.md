# YuanWork 架构设计

> 待补充：详细的架构图、3 步推理时序、整合层数据流、Wiki ↔ RAG ↔ Memory 关系

## 已实现模块概览

| 模块 | 文件 | 状态 |
|---|---|---|
| 文档解析 | `src/backend/yuanwork/ingestion/parser/` | ✅ |
| 向量存储 | `src/backend/yuanwork/memory/semantic.py` | ✅ |
| 情景记忆 | `src/backend/yuanwork/memory/episodic.py` | ✅ |
| LLM 网关 | `src/backend/yuanwork/llm/gateway.py` | ✅ |
| 混合检索 | `src/backend/yuanwork/retrieval/hybrid.py` | ✅ |
| 置信度 | `src/backend/yuanwork/retrieval/confidence.py` | ✅ |
| 3 步推理 | `src/backend/yuanwork/orchestrator/` | ✅ |
| 整合层 | `src/backend/yuanwork/consolidation/` | ✅ |
| Wiki 层 | `src/backend/yuanwork/wiki/` | ✅ |
| 隐私 | `src/backend/yuanwork/privacy/` | ✅ |
| Tauri 壳 | `src/app/src-tauri/src/` | ✅ |
| 前端 UI | `src/app/src/components/` | 🟡 最小可用 |

## 待补充文档

- [ ] 3 步推理状态机时序图
- [ ] 整合层数据流图
- [ ] Wiki 页面生成算法详解
- [ ] 隐私边界（数据流图）
- [ ] LiteLLM 任务路由配置
- [ ] 索引增量更新流程
