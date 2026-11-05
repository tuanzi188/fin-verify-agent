"""标题树：从 PDF 文本层离线抽章节层级，运行时只读。

设计取舍（对应方案 §6.4）：

1. **正则优先、字号为辅**。中文年报的章标题高度规整（`第X节`、`一、`、`（一）`、
   `1、`），用字号阈值反而会被致股东信、图表标注、脚注误伤——实测同一份报告
   里正文字号能有两三档（美的 2025 是 12.0 + 10.6，比亚迪是 9.0）。
   所以先按编号模式认，再用字号做否决：字号明显小于正文的候选一律不是标题。
2. **目录页整页跳过**。目录里同样有 `第一节 … 第五节`，且带点线。
   规则是"同一页出现 ≥3 个章标题即判为目录页"——正文不会在一页里起三章。
3. **`第X节` 与标题分行时合并**。茅台的报告把"第一节"和"释义"排成两行，
   不合并会得到一串只有编号的一级标题。
4. 不在这里调 LLM。标题补全（opaque heading）留到需要时另开一步，
   结果落盘并进 `tree_version`——否则两次运行可能是两棵树。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional

import fitz

# 一级：第X节 / 第X章；允许标题在同一行的后面
SECTION_RE = re.compile(r"^第\s*([一二三四五六七八九十百零]+)\s*(节|章|部分)\s*[、.．：:]?\s*(.*)$")
SECTION_BARE_RE = re.compile(r"^第\s*([一二三四五六七八九十百零]+)\s*(节|章|部分)\s*$")
# 表单位声明。它会在几十页上重复出现，但属于内容而不是页眉，不能当水印剔掉。
_UNIT_DECLARATION_RE = re.compile(r"^[（(]?\s*单位\s*[:：]")
# 每个模式都显式捕获两段：编号 + 标题。别用 `\S.*` 兜住编号，
# 否则 `(3).重大采购合同` 会把点号吃进标题里，标签变成 `(3) .重大采购合同`。
# 二级：一、
L2_RE = re.compile(r"^([一二三四五六七八九十]{1,3}\s*[、.．])\s*(\S.*)$")
# 三级：（一）
L3_RE = re.compile(r"^([（(]\s*[一二三四五六七八九十]{1,3}\s*[）)])\s*(\S.*)$")
# 四级：1、 / 1.
L4_RE = re.compile(r"^(\d{1,2}\s*[、.．])\s*([^\d.．].*)$")
# 四级（多级编号）：1.1 / 2.3
L4_MULTI_RE = re.compile(r"^(\d{1,2}(?:[.．]\d{1,2})+)\s*([^\d\s.．].*)$")
# 五级：（1）
L5_RE = re.compile(r"^([（(]\s*\d{1,2}\s*[）)])\s*(\S.*)$")

DOT_LEADER_RE = re.compile(r"(\.{3,}|…{2,}|·{4,})")
TOC_TITLE_RE = re.compile(r"^目\s*录$")
PAGE_NUMBER_RE = re.compile(r"^\d{1,4}\s*/\s*\d{1,4}$|^第\s*\d+\s*页(\s*共\s*\d+\s*页)?$|^-\s*\d+\s*-$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

HEADING_MAX_CHARS = 46
# 各级标题的长度上限：`一、本公司董事会及董事…不存在` 这种带编号的正文长句
# 在重要提示里成片出现，靠长度和字数能干净地挡掉。
HEADING_LIMITS = {1: 46, 2: 30, 3: 30, 4: 40, 5: 30}
SENTENCE_END = "。；;！？"


@dataclass
class Line:
    """一行文本及其几何信息。page 从 1 开始，与 PDF 阅读器一致。"""

    page: int
    y0: float
    y1: float
    x0: float
    x1: float
    text: str
    size: float
    bold: bool

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass
class Heading:
    page: int
    y0: float
    level: int
    text: str
    size: float
    title: str = ""

    @property
    def label(self) -> str:
        if not self.title:
            return self.text
        # `一、` `（1）` 这类编号后面直接接标题；`第一节` 后面补一个空格才好读
        joiner = "" if self.text[-1:] in "、.．:：）)" else " "
        return f"{self.text}{joiner}{self.title}"


@dataclass
class OutlineNode:
    label: str
    level: int
    page: int
    children: list["OutlineNode"] = field(default_factory=list)


def _is_bold(font: str) -> bool:
    font = (font or "").lower()
    return any(token in font for token in ("bold", "hei", "黑", "heavy", "medium"))


def extract_lines(doc: "fitz.Document") -> list[Line]:
    """按页、按行取文本。空行与纯空白丢弃。"""
    lines: list[Line] = []
    for page_index in range(doc.page_count):
        page = doc[page_index]
        payload = page.get_text("dict")
        for block in payload.get("blocks") or []:
            if block.get("type") != 0:
                continue
            for raw in block.get("lines") or []:
                spans = [span for span in raw.get("spans") or [] if span.get("text", "").strip()]
                if not spans:
                    continue
                text = "".join(span["text"] for span in spans).strip()
                if not text:
                    continue
                size = max(span.get("size", 0.0) for span in spans)
                bold = any(_is_bold(span.get("font", "")) for span in spans)
                x0, y0, x1, y1 = raw.get("bbox") or (0, 0, 0, 0)
                lines.append(
                    Line(
                        page=page_index + 1,
                        y0=float(y0),
                        y1=float(y1),
                        x0=float(x0),
                        x1=float(x1),
                        text=text,
                        size=float(size),
                        bold=bold,
                    )
                )
    return lines


def count_repeated_lines(lines: Iterable[Line], min_pages: int = 8) -> set[str]:
    """页眉页脚/水印：同一行文本出现在大量页面上。

    cninfo 下载的报告页脚常带 `www.cninfo.com.cn`，混进正文会污染章节，
    也会在检索时把每一页都算成命中。

    `单位：千元` 这类表单位声明也出现在几十页上，但它是内容不是页眉：
    剔掉它会让 `_unit_for` 拿不到单位，数字回退成文档默认的「元」（差 1000 倍）。
    """
    page_seen: dict[str, set[int]] = {}
    for line in lines:
        text = line.text.strip()
        if len(text) > 60:
            continue
        if _UNIT_DECLARATION_RE.match(text):
            continue
        page_seen.setdefault(text, set()).add(line.page)
    return {text for text, pages in page_seen.items() if len(pages) >= min_pages}


def body_size(lines: Iterable[Line]) -> float:
    """正文主字号：按字符数加权的众数，丢掉过小的脚注字号。"""
    counter: Counter[float] = Counter()
    for line in lines:
        if line.size < 6.0:
            continue
        counter[round(line.size, 1)] += len(line.text)
    if not counter:
        return 0.0
    return counter.most_common(1)[0][0]


def _level_of(text: str, size: float, body: float) -> Optional[tuple[int, str, str]]:
    """判标题级别。返回 (level, 编号部分, 标题部分) 或 None。

    实测（3 家 6 份年报 + 1 份募集书）：**中文年报的标题几乎不加粗、字号与正文一样**。
    茅台 2025 的 `一、公司信息` 是 10.4pt 非粗体，正文也是 10.4pt；美的 2025 的
    标题与正文同为 12.0pt。所以字号/加粗不能当判据，判据只能是——

    1. 有编号（`第X节` / `一、` / `（一）` / `1、` / `（1）`）；
    2. 短（各级有长度上限）且不以句末标点收尾；
    3. 编号之后必须是中文正文，不是数字。这一条挡掉了 `12.11%`、`4.95`
       ——它们在 `\\d+[.、]` 规则下会被认成"十二、11%"。
    """
    stripped = text.strip()
    if not stripped or len(stripped) > HEADING_MAX_CHARS:
        return None
    if DOT_LEADER_RE.search(stripped) or PAGE_NUMBER_RE.match(stripped):
        return None
    if stripped[-1] in SENTENCE_END:
        return None
    # 字号明显小于正文 ⇒ 脚注/图表标注，不是标题。
    # 用相对阈值而不是 `body - 1.0`：美的 2025 的正文是 12.0pt，二级标题却是 10.6pt，
    # 减 1 点会把整棵子树砍掉（实测只剩 84 个 section，同行 2024 是 208 个）。
    if body and size < max(body * 0.85, 6.0):
        return None

    match = SECTION_RE.match(stripped)
    if match and len(stripped) <= HEADING_LIMITS[1]:
        number, unit, rest = match.group(1), match.group(2), match.group(3).strip()
        return 1, f"第{number}{unit}", rest

    for pattern, level in (
        (L3_RE, 3),
        (L5_RE, 5),
        (L4_MULTI_RE, 4),
        (L2_RE, 2),
        (L4_RE, 4),
    ):
        match = pattern.match(stripped)
        if not match or len(stripped) > HEADING_LIMITS[level]:
            continue
        prefix, rest = match.group(1).strip(), match.group(2).strip()
        if len(_CJK_RE.findall(rest)) < 2:
            continue
        return level, prefix, rest
    return None


def extract_headings(
    lines: list[Line],
    body: float,
    *,
    table_boxes: Optional[dict[int, list[tuple]]] = None,
) -> list[Heading]:
    """抽标题。table_boxes 里的行不参与判定——表格单元格里的 `一、` 不是章节。"""
    repeated = count_repeated_lines(lines)
    boxes = table_boxes or {}
    usable = [
        line
        for line in lines
        if line.text.strip() not in repeated
        and not _inside_any(line, boxes.get(line.page, []))
    ]

    # 目录页：同一页出现 >=3 个章标题，或页面标题就是"目录"
    per_page_sections: Counter[int] = Counter()
    toc_pages: set[int] = set()
    for line in usable:
        text = line.text.strip()
        if SECTION_RE.match(text) or SECTION_BARE_RE.match(text):
            per_page_sections[line.page] += 1
        if TOC_TITLE_RE.match(text):
            toc_pages.add(line.page)
    for page, count in per_page_sections.items():
        if count >= 3:
            toc_pages.add(page)
    # 注意：不要顺手把 page+1 也排除。茅台的目录在 p4、正文"第一节"就在 p5，
    # 多排除一页会把第一、二节整段删掉，第二节正好是主要财务指标。

    headings: list[Heading] = []
    for index, line in enumerate(usable):
        if line.page in toc_pages:
            continue
        text = line.text.strip()
        if SECTION_BARE_RE.match(text):
            title = _next_title_line(usable, index, body)
            headings.append(
                Heading(page=line.page, y0=line.y0, level=1, text=text, size=line.size, title=title)
            )
            continue
        parsed = _level_of(text, line.size, body)
        if not parsed:
            continue
        level, label, title = parsed
        headings.append(
            Heading(page=line.page, y0=line.y0, level=level, text=label, size=line.size, title=title)
        )
    return _dedupe(headings)


def _next_title_line(lines: list[Line], index: int, body: float) -> str:
    """`第X节` 单独一行时，取同页下一行当标题。最多看 2 行。"""
    for offset in (1, 2):
        if index + offset >= len(lines):
            return ""
        candidate = lines[index + offset]
        text = candidate.text.strip()
        if not text or DOT_LEADER_RE.search(text) or len(text) > HEADING_MAX_CHARS:
            return ""
        if SECTION_RE.match(text) or SECTION_BARE_RE.match(text):
            return ""
        if body and candidate.size < body - 1.0:
            return ""
        return text
    return ""


def _dedupe(headings: list[Heading]) -> list[Heading]:
    seen: set[tuple[int, str]] = set()
    result = []
    for heading in headings:
        key = (heading.page, heading.label)
        if key in seen:
            continue
        seen.add(key)
        result.append(heading)
    return result


def build_sections(
    lines: list[Line],
    headings: list[Heading],
    *,
    table_boxes: Optional[dict[int, list[tuple]]] = None,
) -> list[dict]:
    """把行序列按标题切段，返回 store 需要的 section 列表。

    每个标题生成一个 section：正文 = 从该标题到下一个任意级别标题之间的行。
    父子关系通过 `path` 表达——检索看 path，读原文看 text。

    没有正文的标题**保留**（text 为空）。年报里 `七、近三年主要会计数据和财务指标`
    这类小节本身就是一句标题 + 一张表，删掉它会连带丢掉 `path` 的祖先链，
    后面的 `1、…` 全部变成孤儿。检索时按 text 长度加权，空节点自然沉底。
    """
    table_boxes = table_boxes or {}
    repeated = count_repeated_lines(lines)
    # 必须按 (page, y0) 排序：PyMuPDF 的 block 顺序不保证自上而下，
    # 不排序会让"从标题切到下一个标题"取到跨块的错误区间。
    ordered = sorted(
        (
            line
            for line in lines
            if line.text.strip() not in repeated and not PAGE_NUMBER_RE.match(line.text.strip())
        ),
        key=lambda line: (line.page, line.y0, line.x0),
    )

    starts: list[int] = []
    cursor = 0
    for heading in headings:
        position = (heading.page, heading.y0)
        while cursor < len(ordered) and (ordered[cursor].page, ordered[cursor].y0) < position:
            cursor += 1
        starts.append(cursor)

    paths = _heading_paths(headings)

    sections: list[dict] = []
    for index, heading in enumerate(headings):
        begin = starts[index] + 1
        end = starts[index + 1] if index + 1 < len(headings) else len(ordered)
        body_lines = [
            line
            for line in ordered[begin:end]
            if not _inside_any(line, table_boxes.get(line.page, []))
        ]
        text = "\n".join(line.text for line in body_lines).strip()
        sections.append(
            {
                "section_id": "",
                "level": heading.level,
                "path": paths[index],
                "page_start": min((line.page for line in body_lines), default=heading.page),
                "page_end": max((line.page for line in body_lines), default=heading.page),
                "text": text,
                "_start": (heading.page, heading.y0),
                "_title": heading.label,
            }
        )

    # 第一个标题之前的内容（封面、致股东信）单列一段
    if headings:
        first = starts[0]
        preamble = [
            line
            for line in ordered[:first]
            if not _inside_any(line, table_boxes.get(line.page, []))
        ]
        if preamble:
            sections.insert(
                0,
                {
                    "section_id": "",
                    "level": 0,
                    "path": ["（前置）"],
                    "page_start": preamble[0].page,
                    "page_end": preamble[-1].page,
                    "text": "\n".join(line.text for line in preamble).strip(),
                    "_start": (preamble[0].page, preamble[0].y0),
                    "_title": "（前置）",
                },
            )

    for number, section in enumerate(sections, 1):
        section["section_id"] = f"s{number:04d}"
    return sections


def _heading_paths(headings: list[Heading]) -> list[list[str]]:
    """每个标题的祖先链。用级别栈算，与 section 是否留下正文无关。"""
    paths: list[list[str]] = []
    stack: list[tuple[int, str]] = []
    for heading in headings:
        while stack and stack[-1][0] >= heading.level:
            stack.pop()
        stack.append((heading.level, heading.label))
        paths.append([label for _, label in stack])
    return paths


def _inside_any(line: Line, boxes: list[tuple]) -> bool:
    for x0, y0, x1, y1 in boxes:
        center_y = (line.y0 + line.y1) / 2
        center_x = (line.x0 + line.x1) / 2
        if x0 - 2 <= center_x <= x1 + 2 and y0 - 2 <= center_y <= y1 + 2:
            return True
    return False


def outline_tree(sections: list[dict]) -> list[dict]:
    """把平铺的 sections 还原成嵌套大纲，写进 document.json 便于人工核对。"""
    roots: list[dict] = []
    stack: list[dict] = []
    for section in sections:
        node = {
            "section_id": section["section_id"],
            "title": section.get("_title") or (section["path"][-1] if section["path"] else ""),
            "level": section["level"],
            "path": section["path"],
            "page_start": section["page_start"],
            "page_end": section["page_end"],
            "chars": len(section["text"]),
            "children": [],
        }
        while stack and stack[-1]["level"] >= node["level"]:
            stack.pop()
        if stack:
            stack[-1]["children"].append(node)
        else:
            roots.append(node)
        stack.append(node)
    return roots
