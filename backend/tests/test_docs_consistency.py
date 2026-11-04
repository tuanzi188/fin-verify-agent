"""文档一致性测试：链接、工具清单、API 路由、archive frontmatter、CURRENT 数字。

目的不是替代评审，而是拦住最常见的文档漂移：
- 新增/删除工具或路由，README/AGENTS 没跟上；
- 相对链接指向已删除的文件；
- archive 新文档漏了 frontmatter；
- CURRENT.md 又写死了会过期的评测数字。

全部离线，秒级。
"""

import re
from pathlib import Path

from app.tools.finance import FINANCE_TOOL_SPECS

ROOT = Path(__file__).resolve().parents[2]
DOCS = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "docs" / "CURRENT.md",
    ROOT / "docs" / "INDEX.md",
    ROOT / "docs" / "evidence" / "INDEX.md",
]

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"\{[^}]+\}")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _without_code_blocks(text: str) -> str:
    return _FENCED_CODE_RE.sub("", text)


def test_relative_links_resolve():
    broken: list[str] = []
    for path in DOCS:
        text = _without_code_blocks(_read(path))
        for target in _LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            clean = target.split("#", 1)[0].strip()
            if not clean:
                continue
            resolved = (path.parent / clean).resolve()
            if not resolved.exists():
                broken.append(f"{path.relative_to(ROOT)} -> {target}")
    assert not broken, "文档相对链接失效：\n" + "\n".join(broken)


def test_readme_documents_all_finance_tools():
    readme = _read(ROOT / "README.md")
    missing = [
        spec["function"]["name"]
        for spec in FINANCE_TOOL_SPECS
        if f"`{spec['function']['name']}`" not in readme
    ]
    assert not missing, f"README 工具循环缺少：{missing}"


def _api_routes() -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for path in sorted((ROOT / "backend" / "app" / "api").glob("*.py")):
        text = _read(path)
        for method, route in re.findall(
            r"@router\.(get|post|put|delete|patch)\(\s*[\"']([^\"']+)", text
        ):
            normalized = _PLACEHOLDER_RE.sub("{id}", route)
            routes.add((method.upper(), "/api/v1" + normalized))
    return routes


def _readme_routes() -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for line in _read(ROOT / "README.md").splitlines():
        match = re.match(
            r"\|\s*`(?P<path>/api/v1/[^`]+)`\s*\|\s*(?P<method>[A-Z]+)\s*\|",
            line,
        )
        if not match:
            continue
        normalized = _PLACEHOLDER_RE.sub("{id}", match.group("path"))
        routes.add((match.group("method"), normalized))
    return routes


def test_readme_documents_all_api_routes():
    missing = sorted(_api_routes() - _readme_routes())
    assert not missing, f"README API 表缺少：{missing}"


def test_archive_docs_have_frontmatter():
    archive = ROOT / "docs" / "archive"
    missing = []
    for path in sorted(archive.glob("*.md")):
        text = _read(path)
        if not text.startswith("---"):
            missing.append(path.name)
            continue
        head = text.split("---", 2)[1]
        if "status:" not in head:
            missing.append(path.name)
    assert not missing, f"archive 文档缺少 status frontmatter：{missing}"


def test_current_does_not_hardcode_eval_numbers():
    current = _read(ROOT / "docs" / "CURRENT.md")
    patterns = {
        "pass@1 读数": r"pass@1[^\n]{0,20}\d+(?:\.\d+)?%",
        "pass^k 读数": r"pass\^k[^\n]{0,20}\d+(?:\.\d+)?%",
        "通过率": r"\b\d+/\d+\s*=\s*\d+(?:\.\d+)?%",
        "百分比读数": r"(?<![\d.])\d{2,3}\.\d%",
        "token/题": r"token/题\s*[\d,]+",
    }
    hits = {
        name: re.findall(pattern, current)
        for name, pattern in patterns.items()
        if re.search(pattern, current)
    }
    assert not hits, f"CURRENT.md 不应写死评测数字，请放 docs/evidence/INDEX.md：{hits}"


def test_top_level_docs_are_registered():
    index = _read(ROOT / "docs" / "INDEX.md")
    missing = [
        path.name
        for path in sorted((ROOT / "docs").glob("*.md"))
        if path.name != "INDEX.md" and path.name not in index
    ]
    assert not missing, f"docs/INDEX.md 未登记：{missing}"
