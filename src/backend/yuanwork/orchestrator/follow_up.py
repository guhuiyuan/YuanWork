"""追问的上下文 —— **只有位置与读者自己上一句问话**（ADR-057）。

上一轮模型生成的回答**不进这里**：它没有出处，把它带进提示词等于开了一条
「不用证据也能说」的通道——而这个项目的全部价值建立在「答案能在原文里定位」
上（ADR-041 已经因为同样的理由拒绝过「模型生成术语解释」）。

带进来的两样东西都是可核验的：
- **位置**（文件名 + 页码/章节）：指向原文，读者能自己去看；
- **读者上一句问话**：那是读者的**意图**（「它」指什么），不是结论。

这一层是**会话内**的：随请求带过来，不落盘、不进长期记忆。
"""

from __future__ import annotations

from typing import Any

#: 位置最多带几条 —— 提示词不能被撑大（延迟预算 AC-P-002/003/004）
MAX_POSITIONS = 10
#: 读者上一句问话最多带多少字
MAX_QUESTION_CHARS = 500


def _position_text(position: dict[str, Any]) -> str:
    """一个位置说成人话：《采购合同.pdf》第 12 页。"""
    name = str(position.get("file_name") or "").strip()
    if not name:
        return ""
    where = str(position.get("location") or "").strip()
    if not where:
        page = position.get("page")
        slide = position.get("slide_number")
        section = position.get("section")
        sheet = position.get("sheet")
        row = position.get("row")
        if page is not None:
            where = f"第 {page} 页"
        elif slide is not None:
            where = f"Slide {slide}"
        elif section:
            where = f"章节：{section}"
        elif sheet:
            where = f"工作表：{sheet}"
        elif row is not None:
            where = f"第 {row} 行"
    return f"《{name}》{where}" if where else f"《{name}》"


def positions(follow_up: dict[str, Any] | None) -> list[str]:
    """上一轮答到的位置（人话，最多 10 条）。"""
    if not follow_up:
        return []
    out: list[str] = []
    for position in (follow_up.get("positions") or [])[:MAX_POSITIONS]:
        text = _position_text(position)
        if text:
            out.append(text)
    return out


def question(follow_up: dict[str, Any] | None) -> str:
    """读者上一句问话（最多 500 字）。"""
    if not follow_up:
        return ""
    return str(follow_up.get("question") or "").strip()[:MAX_QUESTION_CHARS]


def file_names(follow_up: dict[str, Any] | None) -> list[str]:
    """上一轮答到的文件名（去重，顺序不变）。"""
    if not follow_up:
        return []
    names: list[str] = []
    for position in (follow_up.get("positions") or [])[:MAX_POSITIONS]:
        name = str(position.get("file_name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def block(follow_up: dict[str, Any] | None) -> str:
    """提示词里的一段上下文。

    没有上下文就是空字符串 —— 调用方直接把它渲染进提示词即可。
    """
    lines: list[str] = []
    asked = question(follow_up)
    if asked:
        lines.append(f"读者上一句问：{asked}")
    places = positions(follow_up)
    if places:
        lines.append("上一轮答到的位置：" + "；".join(places))
    return "\n".join(lines)


def search_terms(follow_up: dict[str, Any] | None) -> str:
    """检索通道要并进去的词。

    只有「再详细点」三个字时，检索通道拿不到任何东西 —— 上一轮的问句
    与**文件名**并进去，全文通道才召得回那一页。
    """
    pieces: list[str] = []
    asked = question(follow_up)
    if asked:
        pieces.append(asked)
    pieces.extend(file_names(follow_up))
    return " ".join(pieces)
