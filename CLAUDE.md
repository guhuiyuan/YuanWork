<!-- SDD:BEGIN -->
## SDD 规范（必须遵循）

本项目的开发工作必须遵循以下规范文档。规范是唯一事实源，代码是规范的衍生物；
若代码与规范冲突，以规范为准，或先走逆向修改流程更新规范。

| 规范 | 路径 | 当前状态 |
|------|------|----------|
| 需求定义 proposal | `specs/yuanwork/proposal.md` | v0.1 · AC 59 条 |
| 架构设计 design | `specs/yuanwork/design.md` | v0.1 · ADR 41 条 |
| 任务拆解 tasks | `specs/yuanwork/tasks.md` | v0.1 · 51/51 完成 |

**当前阶段**：Phase 5 — 验证与交付
**下一步**：整体验收 —— 按 59 条 AC 跑回放矩阵，产出 `specs/yuanwork/acceptance-report.md`

规则：
1. 任何代码改动必须对应 `tasks.md` 中的一个 Task，不得做范围外的事。
2. 每个 Task 完成前必须跑通其「验证命令」；未通过不得推进到下游 Task。
3. 不得擅自增加 `proposal.md` §2.1 之外的功能；需要新增时先改规范。
4. 发现规范与实现冲突时，先停下并提出规范修改方案，经确认后再改代码。
5. 提交前跑 `~/.claude/skills/sdd/scripts/validate_specs.py --feature <feature>`
   （规范类）或该 Task 的验证命令（代码类）。
<!-- SDD:END -->

**历史归档**：`archive/as-is-2026-11/` 是 2026-11 对旧实现做的现状快照，
**不是规范**，仅供做「现状与目标的差距」分析时参考。
它放在 `specs/` 之外是有意的——`specs/` 下的每个目录都是一份「规范」，
放进去会让 SDD 工具把它当成一个真的 feature（`update_claude_md.py`
会把 `specs/` 下的所有目录列进 CLAUDE.md 的规范清单）。
