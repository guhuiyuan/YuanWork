"""The synthetic corpus for the build benchmark.

A build of thousands of files has to be measured on something. The
real workspace is the truth, but it changes while the user works, and
its numbers cannot be compared between two runs. So there are two
layers:

* the **synthetic layer** (this module) - a corpus with a known
  structure, built from a fixed seed. The same seed gives the same
  bytes, so two runs measure the same work.
* the **real layer** (`profile.py`) - the structure of the workspace
  the user actually has, counted without reading a word of it.

The corpus follows a *structure profile*: how many pages, paragraphs,
tables and pictures a document of each kind has. The profile is what
makes the numbers mean something - a thousand one-line text files are
not a thousand contracts.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The kinds of file a workspace holds.
KINDS = ("pdf", "pptx", "docx", "xlsx", "md", "png")

#: The words the documents are made of. They are ordinary Chinese words
#: - the point is the length and the structure, not the meaning.
_WORDS = (
    "合同 甲方 乙方 条款 履行 违约 责任 赔偿 验收 交付 付款 结算 期限 附件 签署",
    "采购 供应商 报价 规格 数量 批次 物流 入库 检验 库存 退货 发票 结算 单据",
    "制度 流程 规范 审批 权限 记录 归档 培训 检查 整改 考核 汇报 会议 纪要",
    "项目 进度 里程碑 计划 风险 依赖 负责人 节点 排期 延期 复盘 结论 建议",
)


@dataclass
class StructureProfile:
    """What the documents of a corpus look like.

    The numbers are the *shape* of the corpus - pages per document,
    paragraphs per page, tables, pictures. They are not the content.
    """

    name: str = "mixed"
    #: How many documents of each kind.
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "pdf": 4,
            "pptx": 2,
            "docx": 3,
            "xlsx": 2,
            "md": 4,
            "png": 1,
        }
    )
    #: The size of a document, in pages / slides / sheets.
    pages: int = 12
    #: The paragraphs on a page.
    paragraphs: int = 6
    #: The tables per document.
    tables: int = 1
    #: The rows per table.
    rows: int = 12
    #: The pictures per document.
    images: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "counts": dict(self.counts),
            "pages": self.pages,
            "paragraphs": self.paragraphs,
            "tables": self.tables,
            "rows": self.rows,
            "images": self.images,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StructureProfile":
        return cls(
            name=data.get("name", "mixed"),
            counts=dict(data.get("counts") or {}),
            pages=int(data.get("pages", 12)),
            paragraphs=int(data.get("paragraphs", 6)),
            tables=int(data.get("tables", 1)),
            rows=int(data.get("rows", 12)),
            images=int(data.get("images", 0)),
        )


#: The profile the benchmark uses when none is given: a workspace with
#: contracts, decks, reports, sheets, notes and screenshots.
DEFAULT_PROFILE = StructureProfile()


def _sentence(rng: random.Random) -> str:
    """A line of ordinary Chinese - long enough to be a real paragraph."""
    words = _WORDS[rng.randrange(len(_WORDS))].split()
    picked = [words[rng.randrange(len(words))] for _ in range(rng.randint(8, 18))]
    return "、".join(picked) + "。"


def _paragraph(rng: random.Random, sentences: int = 3) -> str:
    return "".join(_sentence(rng) for _ in range(sentences))


def _write_pdf(path: Path, profile: StructureProfile, rng: random.Random) -> None:
    """A PDF with the profile's number of pages."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    page = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    width, height = A4
    for number in range(profile.pages):
        page.setFont("Helvetica", 11)
        page.drawString(60, height - 60, f"Contract page {number + 1}")
        top = height - 90
        for _ in range(profile.paragraphs):
            text = _paragraph(rng)
            page.drawString(60, top, text[:80])
            top -= 16
        if profile.tables:
            # A ruled table - the kind that has to be read as a table
            # and not as a column of text.
            top -= 14
            left = 60
            page.setLineWidth(0.5)
            for index in range(profile.rows + 1):
                y = top - index * 14
                page.line(left, y, left + 320, y)
                page.line(left, y, left, y + 14)
                page.line(left + 320, y, left + 320, y + 14)
            page.setFont("Helvetica", 9)
            for row in range(profile.rows):
                page.drawString(left + 4, top - row * 14 + 3, f"Item {row + 1}")
                page.drawString(left + 164, top - row * 14 + 3, f"{rng.randint(100, 9999)}")
            page.setFont("Helvetica", 11)
        page.showPage()
    page.save()


def _write_pptx(path: Path, profile: StructureProfile, rng: random.Random) -> None:
    """A deck with the profile's number of slides."""
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    for number in range(profile.pages):
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = f"Slide {number + 1}"
        box = slide.shapes.add_textbox(
            Inches(1), Inches(1.5), Inches(8), Inches(4.5)
        )
        box.text_frame.text = "\n".join(
            _sentence(rng) for _ in range(profile.paragraphs)
        )
    presentation.save(str(path))


def _write_docx(path: Path, profile: StructureProfile, rng: random.Random) -> None:
    """A report with paragraphs and tables."""
    from docx import Document

    document = Document()
    document.add_heading("Report", level=1)
    for _ in range(profile.pages):
        document.add_heading("Chapter", level=2)
        for _ in range(profile.paragraphs):
            document.add_paragraph(_paragraph(rng))
    for table_number in range(profile.tables):
        table = document.add_table(rows=profile.rows, cols=3)
        for row in range(profile.rows):
            for column in range(3):
                table.cell(row, column).text = _sentence(rng)[:30]
    document.save(str(path))


def _write_xlsx(path: Path, profile: StructureProfile, rng: random.Random) -> None:
    """A workbook with the profile's number of sheets."""
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)
    for sheet_number in range(max(1, profile.tables)):
        sheet = workbook.create_sheet(title=f"Sheet{sheet_number + 1}")
        for row in range(profile.rows):
            sheet.append([_sentence(rng)[:20] for _ in range(3)])
    workbook.save(str(path))


def _write_md(path: Path, profile: StructureProfile, rng: random.Random) -> None:
    """A note with headings and paragraphs."""
    lines = ["# Note", ""]
    for number in range(max(1, profile.pages)):
        lines.append(f"## Section {number + 1}")
        lines.append("")
        for _ in range(profile.paragraphs):
            lines.append(_paragraph(rng))
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_png(path: Path, profile: StructureProfile, rng: random.Random) -> None:
    """A picture with a few lines of text on it."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (800, 600), "white")
    draw = ImageDraw.Draw(image)
    top = 30
    for _ in range(max(1, profile.paragraphs)):
        draw.text((20, top), _sentence(rng)[:40], fill="black")
        top += 40
    image.save(str(path))


_WRITERS = {
    "pdf": _write_pdf,
    "pptx": _write_pptx,
    "docx": _write_docx,
    "xlsx": _write_xlsx,
    "md": _write_md,
    "png": _write_png,
}


def generate_corpus(
    target_dir: Path,
    profile: StructureProfile | None = None,
    seed: int = 20261115,
) -> dict[str, Any]:
    """Build the corpus and say what it holds.

    The same seed and the same profile give the same bytes - that is
    what makes two runs comparable.
    """
    profile = profile or DEFAULT_PROFILE
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    written: list[dict[str, Any]] = []
    for kind, count in profile.counts.items():
        writer = _WRITERS.get(kind)
        if writer is None:
            continue
        for index in range(count):
            # A seed per document: the corpus does not depend on the
            # order the documents are built in.
            rng = random.Random(f"{seed}:{kind}:{index}")
            suffix = "jpg" if kind == "png" else kind
            path = target_dir / f"{kind}-{index + 1:04d}.{suffix}"
            writer(path, profile, rng)
            written.append(
                {
                    "path": str(path),
                    "kind": kind,
                    "size_bytes": path.stat().st_size,
                }
            )

    return {
        "seed": seed,
        "profile": profile.to_dict(),
        "files": written,
        "total_bytes": sum(f["size_bytes"] for f in written),
    }
