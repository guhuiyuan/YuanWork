"""图谱 —— 实体与页面之间的关系图。

读者问「这跟什么有关」时看到的就是这张图。所以图上每一条边都得是
库里真有的关系：一页由哪份文件编译出来的、两页出自同一批材料、
这一页提到了哪个实体、两个实体是不是在同一页出现过。**只是相似**
的边是猜的，而读者没有判断猜得对不对的能力（ADR-054）。

图是问的时候现算的，不落盘 —— 库是唯一知道真相的地方。
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yuanwork.db.sqlite import SQLiteStore


logger = logging.getLogger(__name__)

#: 图里最多放多少页。一个工作域能有几百页。
MAX_PAGES = 500
#: 图里最多放多少实体 —— **被最多页面提到的**那些排在前面。
#:
#: 排序用的是「有几页提到了它」，不是「它被写过多少次」：后者是
#: 整合层写下它的次数，跟图上的关系无关。一个被写过十次却没人
#: 提到的实体，在图上只是一个点。
MAX_ENTITIES = 100

#: 图上画的边有哪几种。
EDGE_DERIVED_FROM = "derived_from"        # 一页出自一份文件
EDGE_REFERENCES = "references"            # 一页指向另一页（显式的 [[链接]]）
EDGE_SHARES_EVIDENCE = "shares_evidence"  # 两页出自同一批材料
EDGE_MENTIONS = "mentions"                # 一页提到一个实体
EDGE_CO_OCCURS = "co_occurs"              # 两个实体出现在同一页

#: 一「窝」多少个还算是关系。**超过这个数就整窝不画** —— 一堆页面
#: 共用一个切片，说的是巧合，不是关系。
#:
#: 同一把尺子量两件事：共用一个切片的页面、一页里提到的实体。
#: 两边量的是同一种东西 —— 人一多，彼此之间就什么也不说了。
MAX_CROWD = 8

#: 正着读反着读都一样的关系。其余的边方向本身带着意思：一页是
#: **出自**一份文件的，一页是**提到**一个实体的。
UNDIRECTED_EDGES = frozenset(
    {EDGE_REFERENCES, EDGE_SHARES_EVIDENCE, EDGE_CO_OCCURS}
)

#: 节点的 id 是带命名空间的。叫「华润」的页面与叫「华润」的实体
#: slug 出来是同一个字符串，一个 id 挂两个节点就是一张坏图 ——
#: 画图库会挑一个画出来，边却指着另一个。页面保留自己的 id：
#: 读者点它要带的就是这个 id。
NODE_PREFIX_ENTITY = "entity:"
NODE_PREFIX_DOCUMENT = "doc:"


def _loads(value: Any) -> list[str]:
    """一列里存着一串 id 时，把它读出来。"""
    if isinstance(value, list):
        return [str(v) for v in value]
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(v) for v in parsed]


def _name_is_usable(name: str) -> bool:
    """这个名字值不值得到页面里去找。

    一两个字母的名字什么都匹配得上；看着像路径的根本不是名字。
    """
    cleaned = name.strip()
    if len(cleaned) < 2:
        return False
    if "/" in cleaned or "\\" in cleaned:
        return False
    # 汉字要两个字才有意义；拉丁字母要四个 —— 太短的名字在正文里
    # 到处都是，匹配上了也说明不了什么（ADR-054）。
    if any("一" <= ch <= "鿿" for ch in cleaned):
        return len(cleaned) >= 2
    return len(cleaned) >= 4


@lru_cache(maxsize=4096)
def _name_pattern(name: str) -> re.Pattern[str]:
    """这个名字在正文里怎么找。

    汉字名用子串：汉字之间没有词边界，「合同」出现在「本合同」里
    就是提到了。拉丁名要有词边界：四个字母也足以落在普通词中间
    （「Data」在「database」里面），那不是提到了 Data。
    """
    pattern = re.escape(name)
    if name[0].isascii() and name[0].isalnum():
        pattern = r"(?<![0-9A-Za-z])" + pattern
    if name[-1].isascii() and name[-1].isalnum():
        pattern = pattern + r"(?![0-9A-Za-z])"
    return re.compile(pattern)


def _mentions(text: str, name: str) -> bool:
    """正文里说的是不是这个名字。"""
    cleaned = name.strip().lower()
    if not cleaned:
        return False
    return _name_pattern(cleaned).search(text) is not None


#: 正文里的标记 —— ``[[来源:文件名 页码]]`` 是出处，``[[另一页]]``
#: 是链接。它们不是正文：文件名常常就是主题名（「采购合同.md」），
#: 拿它去匹配实体名，由这份文件编译出来的每一页都会「提到」那个
#: 实体 —— 正文里根本没提。
_MARKER = re.compile(r"\[\[[^\[\]]*\]\]")


def _page_text(page: dict[str, Any]) -> str:
    """页面上能找得到名字的所有地方 —— 标记不算正文。"""
    parts: list[str] = [str(page.get("title") or "")]
    parts.append(str(page.get("summary") or ""))
    parts.extend(_loads(page.get("key_points")))
    parts.append(str(page.get("markdown") or ""))
    return "\n".join(_MARKER.sub(" ", part) for part in parts).lower()


class WikiGraph:
    """建一张页面、文件与实体之间的关系图。"""

    def __init__(self, sqlite_store: "SQLiteStore"):
        self.sqlite = sqlite_store

    async def build_graph(self) -> dict[str, Any]:
        """建出整张图，给可视化用。

        返回的字典长这样（d3.js 或 vis-network 吃得下）::

            {
                "nodes": [{"id": ..., "label": ..., "type": ..., "weight": ...}],
                "edges": [{"source": ..., "target": ..., "type": ..., "weight": ...}]
            }
        """
        pages = await self.sqlite.list_wiki_pages(limit=MAX_PAGES)
        links = await self._all_links()
        entities = await self._entities()

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []

        # 页面本身。
        page_ids = {str(page["id"]) for page in pages}
        for page in pages:
            nodes.append(
                {
                    "id": page["id"],
                    "label": page["title"],
                    "type": "page",
                    "category": page.get("category"),
                    "weight": page.get("version", 1),
                    # 要点是这一页自己说的话 —— 不打开页面也能看见。
                    "key_points": _loads(page.get("key_points")),
                    "related_pages": [],
                }
            )

        # 页面是从哪些文件来的。一份文件撑起很多页时，那份文件自己
        # 就是一个节点：把页面彼此连起来是一团毛球，把它们连到文件
        # 上是一把伞。
        #
        # 有两处记着「哪一页出自哪份文件」：页面上记着它的来源文件，
        # 库里也有一条 ``derived_from`` 链接。两处都读 —— 核心编译器
        # 编译出来的页面只有前者。
        pages_per_doc: dict[str, list[str]] = {}
        for page in pages:
            for doc_id in _loads(page.get("source_doc_ids")):
                compiled = pages_per_doc.setdefault(doc_id, [])
                # 同一页可能把同一份文件列两遍（核心编译器写「对比页」
                # 时，旧版与新版出自同一份文件）—— 那一页还是一页。
                if str(page["id"]) not in compiled:
                    compiled.append(str(page["id"]))
        for link in links:
            if link.get("target_type") != "doc":
                continue
            if link.get("link_type") != EDGE_DERIVED_FROM:
                continue
            source = str(link.get("source_id"))
            target = str(link.get("target_id"))
            if source not in page_ids:
                continue
            compiled = pages_per_doc.setdefault(target, [])
            if source not in compiled:
                compiled.append(source)

        documents = await self._documents(set(pages_per_doc))
        for doc_id, doc in documents.items():
            compiled = pages_per_doc.get(doc_id, [])
            nodes.append(
                {
                    "id": NODE_PREFIX_DOCUMENT + doc_id,
                    "label": doc["file_name"],
                    "type": "document",
                    "category": doc.get("file_type"),
                    "weight": len(compiled),
                    "related_pages": compiled,
                }
            )
            for page_id in compiled:
                edges.append(
                    {
                        "source": page_id,
                        "target": NODE_PREFIX_DOCUMENT + doc_id,
                        "type": EDGE_DERIVED_FROM,
                        "weight": 1.0,
                    }
                )

        # 库里记着的显式 [[链接]]：一页指向另一页。
        #
        # 只画 ``link_type`` 是「references」的那种 —— 那才是 ADR-054
        # 说的「显式的 [[链接]]」。库里别的 ``link_type``（比如
        # ``related``）说的是另一件事，图谱不替它们改名字：一条边必须
        # 说得清是什么关系，说不清的就不画（ADR-054）。
        for link in links:
            if link.get("target_type") != "page":
                continue
            if link.get("link_type") != EDGE_REFERENCES:
                continue
            source = str(link.get("source_id"))
            target = str(link.get("target_id"))
            if source not in page_ids or target not in page_ids:
                continue
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "type": EDGE_REFERENCES,
                    # 权重是一个量纲，都在 1 以上（见 _shared_evidence）。
                    "weight": max(1.0, float(link.get("weight") or 1.0)),
                }
            )

        # 两页出自同一批材料。
        for source, target, weight in self._shared_evidence(pages):
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "type": EDGE_SHARES_EVIDENCE,
                    "weight": weight,
                }
            )

        # 实体，以及提到它们的页面。
        #
        # 先把「哪一页提到了哪个实体」算全，再挑画哪几个：挑谁进图
        # 得按图上的份量排 —— 被几页提到，而不是被写过几次。
        mentions = self._mentions_by_page(pages, entities)
        entities = self._pick_entities(entities, mentions)
        mentions = {
            str(entity["id"]): mentions.get(str(entity["id"]), set())
            for entity in entities
        }
        for entity in entities:
            entity_id = str(entity["id"])
            node_id = NODE_PREFIX_ENTITY + entity_id
            pages_mentioning = sorted(mentions.get(entity_id, set()))
            nodes.append(
                {
                    "id": node_id,
                    "label": entity["name"],
                    "type": f"entity_{entity['type']}",
                    "category": entity["type"],
                    # 实体在图上的份量 —— 有几页提到了它。
                    "weight": len(pages_mentioning),
                    "aliases": entity.get("aliases") or [],
                    "related_pages": pages_mentioning,
                }
            )
            for page_id in pages_mentioning:
                edges.append(
                    {
                        "source": page_id,
                        "target": node_id,
                        "type": EDGE_MENTIONS,
                        "weight": 1.0,
                    }
                )

        # 两个实体出现在同一页上。
        for source, target, weight in self._co_occurrences(mentions):
            edges.append(
                {
                    "source": NODE_PREFIX_ENTITY + source,
                    "target": NODE_PREFIX_ENTITY + target,
                    "type": EDGE_CO_OCCURS,
                    "weight": weight,
                }
            )

        # 每条边都得指着真在那儿的节点 —— 画图库遇到找不到的节点会抛。
        # 一条边从节点指向自己什么也没说；同一条边画两遍就是同一个
        # 关系画了两根线。
        known = {str(node["id"]) for node in nodes}
        seen_edges: set[tuple[str, str, str]] = set()
        clean_edges: list[dict[str, Any]] = []
        for edge in edges:
            source = str(edge["source"])
            target = str(edge["target"])
            if source not in known or target not in known:
                continue
            if source == target:
                continue
            key = (source, target, str(edge["type"]))
            if str(edge["type"]) in UNDIRECTED_EDGES:
                # 「这两个页面有关系」不在乎读者从哪一头走过去 ——
                # 一根线就够了。
                key = tuple(sorted((source, target))) + (str(edge["type"]),)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            clean_edges.append(edge)
        edges = clean_edges

        # 每个节点说自己连着谁。
        related: dict[str, set[str]] = {}
        for edge in edges:
            related.setdefault(str(edge["source"]), set()).add(str(edge["target"]))
            related.setdefault(str(edge["target"]), set()).add(str(edge["source"]))
        for node in nodes:
            node_id = str(node["id"])
            if node["type"] == "page":
                node["related_pages"] = sorted(
                    related.get(node_id, set()) & page_ids
                )

        return {"nodes": nodes, "edges": edges}

    async def _all_links(self) -> list[dict[str, Any]]:
        """库里所有的链接 —— 一次查完，不是一页一次。"""
        async with self.sqlite.conn.execute(
            "SELECT source_id, source_type, target_id, target_type, "
            "link_type, weight FROM wiki_links"
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "source_id": row[0],
                "source_type": row[1],
                "target_id": row[2],
                "target_type": row[3],
                "link_type": row[4],
                "weight": row[5],
            }
            for row in rows
        ]

    async def _entities(self) -> list[dict[str, Any]]:
        """库里在效的实体。

        只画 ``status='active'`` 的。``pending`` 的是等着用户确认的
        规律（ADR-039）：它是一句还没成立的结论，画成「实体 · 概念」
        会让读者以为它已经成立；拿它的名字去匹配正文，还会给一条
        未确认的结论连出「提到」的边。

        这里不排序也不限量 —— 谁进图要按「被几页提到」排，而那要等
        匹配完正文才知道（见 _pick_entities）。
        """
        async with self.sqlite.conn.execute(
            "SELECT id, name, type, aliases, mention_count FROM entities "
            "WHERE status = 'active'"
        ) as cursor:
            rows = await cursor.fetchall()
        entities: list[dict[str, Any]] = []
        for entity_id, name, entity_type, aliases, mention_count in rows:
            entities.append(
                {
                    "id": entity_id,
                    "name": name,
                    "type": entity_type,
                    "aliases": _loads(aliases),
                    "mention_count": mention_count or 0,
                }
            )
        return entities

    @staticmethod
    def _pick_entities(
        entities: list[dict[str, Any]], mentions: dict[str, set[str]]
    ) -> list[dict[str, Any]]:
        """图里放哪些实体。

        排在前面的，是**被最多页面提到的**那些。写得多不等于图上
        关系多 —— 「被写过多少次」是整合层写下它的次数。

        没人提到的实体不进图：它在图上只是一个点，读者读不出它跟
        什么有关（ADR-054：没有关系就不画）。
        """
        def rank(entity: dict[str, Any]) -> tuple[int, int, str]:
            entity_id = str(entity["id"])
            return (
                -len(mentions.get(entity_id, ())),
                -(entity.get("mention_count") or 0),
                str(entity["name"]),
            )

        mentioned = [
            entity for entity in entities if mentions.get(str(entity["id"]))
        ]
        mentioned.sort(key=rank)
        return mentioned[:MAX_ENTITIES]

    async def _documents(self, doc_ids: set[str]) -> dict[str, dict[str, Any]]:
        """这些页面是从哪些文件编译出来的。"""
        if not doc_ids:
            return {}
        documents: dict[str, dict[str, Any]] = {}
        wanted = list(doc_ids)
        # SQLite 对一条查询里能带的变量个数有上限，所以 id 分批查。
        for start in range(0, len(wanted), 300):
            batch = wanted[start : start + 300]
            placeholders = ",".join("?" for _ in batch)
            async with self.sqlite.conn.execute(
                f"SELECT doc_id, file_name, file_type FROM files "
                f"WHERE doc_id IN ({placeholders})",
                batch,
            ) as cursor:
                rows = await cursor.fetchall()
            for doc_id, file_name, file_type in rows:
                documents[str(doc_id)] = {
                    "file_name": file_name,
                    "file_type": file_type,
                }
        return documents

    @staticmethod
    def _shared_evidence(
        pages: list[dict[str, Any]],
    ) -> list[tuple[str, str, float]]:
        """出自同一批材料的两页。"""
        chunks_by_page: dict[str, set[str]] = {}
        by_chunk: dict[str, list[str]] = {}
        for page in pages:
            page_id = str(page["id"])
            chunks = set(_loads(page.get("source_chunk_ids")))
            chunks_by_page[page_id] = chunks
            for chunk_id in chunks:
                by_chunk.setdefault(chunk_id, []).append(page_id)

        groups = {
            chunk_id: sorted(set(page_ids))
            for chunk_id, page_ids in by_chunk.items()
        }
        # 一堆页面共用的切片 —— 这个切片对哪两页都不算证据。
        crowded = {
            chunk_id for chunk_id, page_ids in groups.items()
            if len(page_ids) > MAX_CROWD
        }

        pairs: dict[tuple[str, str], set[str]] = {}
        for chunk_id, unique in groups.items():
            if len(unique) < 2 or len(unique) > MAX_CROWD:
                # 一堆页面共用一个切片，说的是巧合，不是关系。
                continue
            for i, left in enumerate(unique):
                for right in unique[i + 1 :]:
                    pairs.setdefault((left, right), set()).add(chunk_id)

        shared: list[tuple[str, str, float]] = []
        for (left, right), chunks in pairs.items():
            # 分母用与分子同一批切片：拥挤的切片分子不算，它也不该
            # 出现在分母里 —— 否则「共享的比例」说的不是真的比例。
            union = (
                chunks_by_page.get(left, set()) | chunks_by_page.get(right, set())
            ) - crowded
            ratio = len(chunks) / len(union) if union else 0.0
            # 权重是一个量纲，都在 1 以上：前端拿它当线宽，比值一律
            # 被夹到 1，计数却能到 3 —— 「共享得越多」的那条反而画
            # 得最细。
            shared.append((left, right, 1.0 + ratio))
        return shared

    @staticmethod
    def _mentions_by_page(
        pages: list[dict[str, Any]], entities: list[dict[str, Any]]
    ) -> dict[str, set[str]]:
        """哪一页提到了哪个实体。

        来源是**文本匹配**：实体的名字或别名出现在页面的标题、摘要、
        要点或正文里（ADR-054 选项 B② 的第二条）。正文里的
        ``[[来源:…]]``、``[[…]]`` 标记不算正文 —— 那是出处与链接，
        不是页面说的话（见 _page_text）。

        另一个来源是 ``entity_mentions`` 表（哪一页提到了哪个实体）——
        那张表现在还是空的：写入它的 ``EntityExtractor`` 全项目只有
        测试在调。等它上线了，「这一页提到了这个实体」就有两个来源：
        表里记着的（更准）与文本匹配出来的（会有假阳性）。到那时要由
        ADR-054 决定以哪个为准；在那之前按 ADR-054 写的做 —— 只认
        文本匹配。
        """
        mentions: dict[str, set[str]] = {}
        for page in pages:
            text = _page_text(page)
            for entity in entities:
                names = [str(entity["name"])] + [
                    str(a) for a in (entity.get("aliases") or [])
                ]
                if not any(_name_is_usable(n) and _mentions(text, n) for n in names):
                    continue
                mentions.setdefault(str(entity["id"]), set()).add(str(page["id"]))
        return mentions

    @staticmethod
    def _co_occurrences(
        mentions: dict[str, set[str]],
    ) -> list[tuple[str, str, float]]:
        """出现在同一页上的实体。"""
        by_page: dict[str, list[str]] = {}
        for entity_id, page_ids in mentions.items():
            for page_id in page_ids:
                by_page.setdefault(page_id, []).append(entity_id)

        pairs: dict[tuple[str, str], int] = {}
        for entity_ids in by_page.values():
            unique = sorted(set(entity_ids))
            if len(unique) > MAX_CROWD:
                # 一页提到一窝实体时，它们彼此之间就什么也不说了 ——
                # 与共用一个切片的那堆页面是同一把尺子（MAX_CROWD）。
                continue
            for i, left in enumerate(unique):
                for right in unique[i + 1 :]:
                    key = (left, right)
                    pairs[key] = pairs.get(key, 0) + 1
        return [
            (left, right, float(count))
            for (left, right), count in pairs.items()
        ]

    async def get_related_pages(
        self, page_id: str, max_depth: int = 2
    ) -> list[dict[str, Any]]:
        """与某一页相关的页面，最多走 N 跳。"""
        visited = set()
        queue = [(page_id, 0)]
        related: list[dict[str, Any]] = []

        while queue:
            current_id, depth = queue.pop(0)
            if current_id in visited or depth > max_depth:
                continue
            visited.add(current_id)

            if depth > 0:
                page = await self.sqlite.get_wiki_page(current_id)
                if page:
                    related.append(
                        {
                            "id": page["id"],
                            "title": page["title"],
                            "depth": depth,
                        }
                    )

            # 出边
            outgoing = await self.sqlite.get_wiki_outgoing_links(current_id)
            for link in outgoing:
                if link["target_type"] == "page":
                    queue.append((link["target_id"], depth + 1))

        return related
