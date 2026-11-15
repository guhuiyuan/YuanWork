"""The full-text channel: segmentation and query semantics.

The BM25 channel replaced the embedding model's own sparse weights
because those scored 0.0 on plain numbers. Its behaviour is therefore
part of the contract:

* the segmentation heuristic (ASCII vs CJK)
* that the index side and the query side use the same function
* the OR semantics of a multi-word query
* **token-level matching**: a query matches a *jieba token*, not a
  substring of the indexed text
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yuanwork.retrieval.fts import FTSIndex, _segment


@pytest.fixture
def index(tmp_path: Path):
    idx = FTSIndex(index_dir=tmp_path / "fts")
    yield idx
    try:
        idx.close()
    except Exception:
        pass


# --------------------------------------------------------------------------
# the segmentation
# --------------------------------------------------------------------------


def test_an_ascii_text_is_lowercased_and_split_on_whitespace():
    assert _segment("Hello World  Foo") == "hello world foo"


def test_a_mostly_ascii_text_uses_the_ascii_branch():
    # 9 ascii characters out of 10 -> 90% > 80%
    assert _segment("Invoice 1280") == "invoice 1280"


def test_a_cjk_text_is_segmented_with_jieba():
    out = _segment("发票 金额 1280")
    assert "发票" in out
    assert "金额" in out


def test_the_ascii_branch_is_used_above_the_threshold():
    # exactly 80% ascii -> not *more* than 80%, so the CJK branch runs
    text = "a" * 8 + "发票"
    assert _segment(text) != text.lower()


def test_an_empty_text_segments_to_nothing():
    assert _segment("") == ""


def test_the_ascii_branch_keeps_punctuation():
    """Punctuation survives segmentation; tantivy's tokenizer drops it
    later, which is why a punctuation-only query finds nothing."""
    assert _segment("??? !!!") == "??? !!!"


# --------------------------------------------------------------------------
# the index
# --------------------------------------------------------------------------


def test_an_empty_index_has_no_hits(index):
    assert index.count() == 0
    assert index.search("发票") == []


def test_a_document_can_be_added_and_found(index):
    index.add_documents([{"id": "c1", "doc_id": "d1", "text": "发票 金额 1280"}])
    assert index.count() == 1

    hits = index.search("1280")
    assert hits
    assert hits[0][0] == "c1"
    assert hits[0][1] > 0


def test_the_hit_score_is_a_bm25_score(index):
    index.add_documents([{"id": "c1", "doc_id": "d1", "text": "发票 金额"}])
    _cid, score = index.search("发票")[0]
    assert score > 0


def test_a_plain_number_is_found(index):
    """The case the BM25 channel was introduced for."""
    index.add_documents([
        {"id": "c1", "doc_id": "d1", "text": "金额 1280 元"},
        {"id": "c2", "doc_id": "d1", "text": "其他 内容"},
    ])
    hits = index.search("1280")
    assert hits[0][0] == "c1"


def test_an_empty_query_returns_nothing(index):
    index.add_documents([{"id": "c1", "doc_id": "d1", "text": "发票 金额"}])
    assert index.search("") == []
    assert index.search("   ") == []


def test_a_punctuation_only_query_returns_nothing(index):
    index.add_documents([{"id": "c1", "doc_id": "d1", "text": "发票 金额"}])
    assert index.search("???") == []


def test_a_query_with_no_matching_term_returns_nothing(index):
    index.add_documents([{"id": "c1", "doc_id": "d1", "text": "发票 金额"}])
    assert index.search("完全 无关") == []


def test_a_multi_word_query_matches_any_of_the_words(index):
    """tantivy's default is OR, not AND."""
    index.add_documents([
        {"id": "c1", "doc_id": "d1", "text": "发票 金额"},
        {"id": "c2", "doc_id": "d1", "text": "合同 条款"},
    ])
    hits = index.search("发票 合同")
    found = {cid for cid, _score in hits}
    assert found == {"c1", "c2"}


def test_the_top_k_caps_the_number_of_hits(index):
    index.add_documents([
        {"id": f"c{i}", "doc_id": "d1", "text": f"发票 {i}"} for i in range(5)
    ])
    assert len(index.search("发票", top_k=2)) == 2


def test_a_document_can_be_deleted_by_id(index):
    index.add_documents([{"id": "c1", "doc_id": "d1", "text": "发票 金额"}])
    assert index.count() == 1
    index.delete_by_ids(["c1"])
    assert index.count() == 0
    assert index.search("发票") == []


def test_a_document_can_be_deleted_by_doc_id(index):
    index.add_documents([
        {"id": "c1", "doc_id": "d1", "text": "发票 金额"},
        {"id": "c2", "doc_id": "d2", "text": "合同 条款"},
    ])
    index.delete_by_doc_id("d1")
    assert index.count() == 1
    assert index.search("发票") == []
    assert index.search("合同")


def test_adding_nothing_is_a_no_op(index):
    assert index.add_documents([]) == 0
    assert index.count() == 0


def test_adding_the_same_id_twice_creates_two_documents(index):
    """tantivy does not de-duplicate by id — the de-duplication happens
    one layer up, where the ids are deleted before they are added."""
    index.add_documents([{"id": "c1", "doc_id": "d1", "text": "发票 金额"}])
    index.add_documents([{"id": "c1", "doc_id": "d1", "text": "合同 条款"}])
    assert index.count() == 2


# --------------------------------------------------------------------------
# token-level matching
# --------------------------------------------------------------------------


def test_a_query_matches_a_token_not_a_substring(index):
    """jieba keeps "合同条款" as one token, so the query "合同" does not
    find it — the channel matches tokens, not substrings."""
    index.add_documents([{"id": "c1", "doc_id": "d1", "text": "合同条款"}])
    assert index.search("合同条款")
    assert index.search("合同") == []


def test_a_spaced_query_does_match(index):
    index.add_documents([{"id": "c1", "doc_id": "d1", "text": "合同 条款"}])
    assert index.search("合同")


# --------------------------------------------------------------------------
# the rebuild
# --------------------------------------------------------------------------


def test_the_index_can_be_rebuilt_from_chunks(index):
    index.add_documents([{"id": "c1", "doc_id": "d1", "text": "发票 金额"}])
    count = index.reindex_from_chunks([
        {"id": "c1", "doc_id": "d1", "text": "发票 金额"},
        {"id": "c2", "doc_id": "d1", "text": "合同 条款"},
    ])
    assert count == 2
    assert index.count() == 2
    assert index.search("合同")
