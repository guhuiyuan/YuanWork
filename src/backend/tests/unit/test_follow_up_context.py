"""追问的上下文 —— **只有位置与读者自己上一句问话**（ADR-057）。

这个文件盯住三件事：

* 带进提示词的是**位置**与**读者自己的问话**；
* **上一轮模型生成的回答进不来** —— 它没有出处，带进提示词等于开了一条
  「不用证据也能说」的通道；
* 上下文有**上界** —— 提示词不能被撑大（延迟预算 AC-P-002/003/004）。

还有一件同样要紧的事：**不是追问时，行为与没有上下文时完全一样**
（AC-F-038）——检索的查询不被扩，答案的提示词里也没有上一轮。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from yuanwork.orchestrator import follow_up


# --------------------------------------------------------------------------
# 这一段上下文里有什么
# --------------------------------------------------------------------------


def test_the_block_says_the_positions_and_the_question():
    context = {
        "question": "把这份合同的违约金讲清楚",
        "positions": [{"file_name": "采购合同.pdf", "page": 3}],
    }

    block = follow_up.block(context)

    assert "《采购合同.pdf》第 3 页" in block
    assert "把这份合同的违约金讲清楚" in block


def test_the_previous_answer_is_not_carried():
    """上一轮的回答不在上下文里 —— 即使有人把它塞了进来。"""
    context = {
        "question": "再详细点",
        "positions": [{"file_name": "采购合同.pdf", "page": 3}],
        # Nothing else may come along: the previous answer is
        # model-generated text with no source (ADR-057).
        "answer": "上一轮模型生成的回答原文",
        "content": "上一轮模型生成的回答原文",
    }

    block = follow_up.block(context)

    assert "上一轮模型生成的回答原文" not in block
    assert "上一轮模型生成的回答原文" not in follow_up.search_terms(context)


def test_no_context_means_no_block():
    assert follow_up.block(None) == ""
    assert follow_up.block({}) == ""
    assert follow_up.search_terms(None) == ""


# --------------------------------------------------------------------------
# 位置说人话
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "position,expected",
    [
        ({"file_name": "合同.pdf", "page": 12}, "《合同.pdf》第 12 页"),
        ({"file_name": "汇报.pptx", "slide_number": 4}, "《汇报.pptx》Slide 4"),
        ({"file_name": "方案.docx", "section": "三、风险"}, "《方案.docx》章节：三、风险"),
        ({"file_name": "报表.xlsx", "sheet": "明细", "row": 7}, "《报表.xlsx》工作表：明细"),
        ({"file_name": "报表.xlsx", "row": 7}, "《报表.xlsx》第 7 行"),
        ({"file_name": "合同.pdf"}, "《合同.pdf》"),
        (
            {"file_name": "合同.pdf", "page": 3, "location": "第 3 页（第三条）"},
            "《合同.pdf》第 3 页（第三条）",
        ),
    ],
)
def test_a_position_is_readable(position: dict, expected: str):
    assert follow_up._position_text(position) == expected


def test_a_position_without_a_file_name_is_dropped():
    assert follow_up._position_text({"page": 3}) == ""


# --------------------------------------------------------------------------
# 上界
# --------------------------------------------------------------------------


def test_at_most_ten_positions():
    context = {
        "question": "再详细点",
        "positions": [{"file_name": f"文件{i}.pdf", "page": i} for i in range(30)],
    }

    places = follow_up.positions(context)

    assert len(places) == 10
    assert places[0] == "《文件0.pdf》第 0 页"


def test_the_question_is_cut_at_500_characters():
    context = {"question": "问" * 900, "positions": []}

    assert len(follow_up.question(context)) == 500


# --------------------------------------------------------------------------
# 检索通道要并进去的词
# --------------------------------------------------------------------------


def test_the_search_terms_are_the_question_and_the_file_names():
    context = {
        "question": "把这份合同的违约金讲清楚",
        "positions": [
            {"file_name": "采购合同.pdf", "page": 3},
            {"file_name": "采购合同.pdf", "page": 4},
            {"file_name": "补充协议.pdf", "page": 1},
        ],
    }

    terms = follow_up.search_terms(context)

    assert "把这份合同的违约金讲清楚" in terms
    assert "采购合同.pdf" in terms
    assert "补充协议.pdf" in terms
    # The same file twice is one term.
    assert terms.count("采购合同.pdf") == 1


# --------------------------------------------------------------------------
# 不是追问时，行为与没有上下文时完全一样
# --------------------------------------------------------------------------


class FakeLLM:
    """A model that says what the test tells it to say."""

    def __init__(self, content: str):
        self.content = content
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        return type("R", (), {"content": self._reply(task)})()

    async def stream(self, task):
        self.tasks.append(task)
        for piece in self._reply(task):
            yield piece

    def _reply(self, task) -> str:
        task_type = getattr(task, "task_type", "")
        if task_type == "evidence_gate":
            return '{"sufficient": true, "reason": "证据覆盖了问题"}'
        if task_type == "answer":
            return "答案 [1]。"
        return self.content


class FakeSQLite:
    async def list_files(self, limit=100, offset=0, file_type=None, search=None):
        return []


class FakeSemantic:
    async def search(self, query, top_k=50, filter_expr=None):
        return []


class RecordingRetriever:
    """It remembers the query it was given."""

    def __init__(self):
        self.queries: list[str] = []

    async def search(self, query, top_k=10, doc_ids=None, file_types=None, min_score=0.0):
        self.queries.append(query)
        # Two pieces at least - the hard rule wants more than one.
        return [
            {"id": f"c{i}", "doc_id": "d1", "text": f"证据 {i}", "file_name": "a.pdf",
             "file_path": "/a.pdf", "page": 12, "_score": 0.9 - i * 0.05}
            for i in range(3)
        ]


class RecordingConsolidation:
    async def record_episode(self, session_id, role, content, topic_tags=None,
                             entities=None, importance_score=0.5):
        return 1


class RecordingWikiTrigger:
    async def maybe_compile(self, evidence, understanding):
        return {"triggered": False, "reason": "no_evidence"}


@pytest.fixture(autouse=True)
def _sqlite_store():
    from yuanwork.api import deps

    deps.set_sqlite_store(FakeSQLite())  # type: ignore[arg-type]
    yield
    deps.reset_dependencies()


def make_agent(llm: FakeLLM):
    from yuanwork.retrieval.confidence import ConfidenceScorer
    from yuanwork.orchestrator.agent import AgentLoop

    agent = AgentLoop(
        llm_gateway=llm,  # type: ignore[arg-type]
        semantic_memory=FakeSemantic(),  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
        consolidation_engine=RecordingConsolidation(),  # type: ignore[arg-type]
        wiki_compile_trigger=RecordingWikiTrigger(),  # type: ignore[arg-type]
    )
    agent.retriever = RecordingRetriever()  # type: ignore[assignment]
    agent.scorer = ConfidenceScorer()  # type: ignore[assignment]
    return agent


CONTEXT = {
    "question": "把这份合同的违约金讲清楚",
    "positions": [{"file_name": "采购合同.pdf", "page": 3}],
}


def test_a_follow_up_is_searched_for_with_the_previous_question():
    """只有「再详细点」三个字时检索不到东西 —— 查询要跟着扩。"""
    llm = FakeLLM(
        '{"intent": "explain", "topic": "违约金", "file_hint": "采购合同.pdf", '
        '"is_follow_up": true}'
    )
    agent = make_agent(llm)

    asyncio.run(agent.run(query="再详细点", session_id="s", follow_up=CONTEXT))

    assert agent.retriever.queries, "the retrieval channel was not asked"
    query = agent.retriever.queries[0]
    assert "再详细点" in query
    assert "把这份合同的违约金讲清楚" in query
    assert "采购合同.pdf" in query


def test_a_new_question_is_searched_for_as_it_is():
    """不是追问时，检索的查询一个字都不多（AC-F-038）。"""
    llm = FakeLLM(
        '{"intent": "explain", "topic": "违约金", "file_hint": null, '
        '"is_follow_up": false}'
    )
    agent = make_agent(llm)

    asyncio.run(agent.run(query="另起一个问题", session_id="s", follow_up=CONTEXT))

    assert agent.retriever.queries == ["另起一个问题"]


def test_the_answer_prompt_carries_the_context_only_when_it_is_a_follow_up():
    llm = FakeLLM(
        '{"intent": "explain", "topic": "违约金", "file_hint": "采购合同.pdf", '
        '"is_follow_up": true}'
    )
    agent = make_agent(llm)

    asyncio.run(agent.run(query="再详细点", session_id="s", follow_up=CONTEXT))

    answer = next(t for t in llm.tasks if t.task_type == "answer")
    assert "《采购合同.pdf》第 3 页" in answer.messages[0].content


def test_the_answer_prompt_has_no_context_when_it_is_not_a_follow_up():
    llm = FakeLLM(
        '{"intent": "explain", "topic": "违约金", "file_hint": null, '
        '"is_follow_up": false}'
    )
    agent = make_agent(llm)

    asyncio.run(agent.run(query="另起一个问题", session_id="s", follow_up=CONTEXT))

    answer = next(t for t in llm.tasks if t.task_type == "answer")
    assert "《采购合同.pdf》第 3 页" not in answer.messages[0].content


def test_the_gate_prompt_carries_the_context_only_when_it_is_a_follow_up():
    """裁判也要看到上下文 —— 否则「再详细点」没有主语，只能判不足。

    实测：裁判只看到「再详细点」三个字时，会说「未指明需要展开的主题」，
    于是这一轮被判成证据不足 —— 追问就断在那里（ADR-057）。
    """
    llm = FakeLLM(
        '{"intent": "explain", "topic": "违约金", "file_hint": "采购合同.pdf", '
        '"is_follow_up": true}'
    )
    agent = make_agent(llm)

    asyncio.run(agent.run(query="再详细点", session_id="s", follow_up=CONTEXT))

    gate = next(t for t in llm.tasks if t.task_type == "evidence_gate")
    assert "《采购合同.pdf》第 3 页" in gate.messages[0].content
    assert "把这份合同的违约金讲清楚" in gate.messages[0].content


def test_the_gate_prompt_has_no_context_when_it_is_not_a_follow_up():
    llm = FakeLLM(
        '{"intent": "explain", "topic": "违约金", "file_hint": null, '
        '"is_follow_up": false}'
    )
    agent = make_agent(llm)

    asyncio.run(agent.run(query="另起一个问题", session_id="s", follow_up=CONTEXT))

    gate = next(t for t in llm.tasks if t.task_type == "evidence_gate")
    assert "《采购合同.pdf》第 3 页" not in gate.messages[0].content


def test_without_a_context_the_run_is_the_same_run():
    """没有上一轮时，一切与从前一样。"""
    llm = FakeLLM('{"intent": "explain", "topic": "违约金"}')
    agent = make_agent(llm)

    asyncio.run(agent.run(query="再详细点", session_id="s"))

    assert agent.retriever.queries == ["再详细点"]
    answer = next(t for t in llm.tasks if t.task_type == "answer")
    assert "上一轮" not in answer.messages[0].content
