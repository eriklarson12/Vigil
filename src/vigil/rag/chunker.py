"""Heading-aware markdown chunker (spec §7).

A chunk = one H2/H3 section; adjacent sibling sections merge up to
TARGET_TOKENS; oversized sections split at paragraph boundaries with
OVERLAP_TOKENS of overlap. Token estimate: len(text) // 4.
"""

import hashlib
import re
from typing import Any

TARGET_TOKENS = 400
MAX_TOKENS = 512
OVERLAP_TOKENS = 50

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")


def est_tokens(text: str) -> int:
    return len(text) // 4


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body). Minimal YAML: title + service_tags."""
    import yaml

    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return {}, raw
    meta = yaml.safe_load(m.group(1)) or {}
    return meta, raw[m.end() :]


def _sections(body: str, doc_title: str) -> list[dict[str, Any]]:
    """Split into H2/H3 sections; content before the first H2 forms a preamble section."""
    sections: list[dict[str, Any]] = []
    h1, h2, h3 = doc_title, None, None
    current_lines: list[str] = []

    def heading_path() -> str:
        return " > ".join(p for p in (h1, h2, h3) if p)

    def flush() -> None:
        text = "\n".join(current_lines).strip()
        if text:
            sections.append({"heading_path": heading_path(), "content": text})
        current_lines.clear()

    for line in body.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            if level == 1:
                flush()
                h1, h2, h3 = m.group(2).strip(), None, None
                continue
            if level == 2:
                flush()
                h2, h3 = m.group(2).strip(), None
                continue
            if level == 3:
                flush()
                h3 = m.group(2).strip()
                continue
        current_lines.append(line)
    flush()
    return sections


def _split_oversized(section: dict[str, Any]) -> list[dict[str, Any]]:
    paragraphs = re.split(r"\n\s*\n", section["content"])
    parts: list[dict[str, Any]] = []
    buf: list[str] = []

    def buf_tokens() -> int:
        return est_tokens("\n\n".join(buf))

    for para in paragraphs:
        if buf and buf_tokens() + est_tokens(para) > MAX_TOKENS:
            parts.append({"heading_path": section["heading_path"], "content": "\n\n".join(buf)})
            # keep tail paragraphs as overlap for continuity
            overlap: list[str] = []
            while buf and est_tokens("\n\n".join(overlap)) < OVERLAP_TOKENS:
                overlap.insert(0, buf.pop())
            buf = overlap
        buf.append(para)
    if buf:
        parts.append({"heading_path": section["heading_path"], "content": "\n\n".join(buf)})
    return parts


def chunk_markdown(raw: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (frontmatter, chunks). Each chunk: heading_path, content,
    token_count, content_hash, embed_text (breadcrumb-prefixed)."""
    meta, body = parse_frontmatter(raw)
    doc_title = meta.get("title", "Runbook")
    sections: list[dict[str, Any]] = []
    for s in _sections(body, doc_title):
        if est_tokens(s["content"]) > MAX_TOKENS:
            sections.extend(_split_oversized(s))
        else:
            sections.append(s)

    # merge adjacent sections that share a parent while under TARGET_TOKENS
    merged: list[dict[str, Any]] = []
    for s in sections:
        if merged:
            prev = merged[-1]
            same_parent = prev["heading_path"].split(" > ")[:2] == s["heading_path"].split(" > ")[:2]
            if same_parent and est_tokens(prev["content"]) + est_tokens(s["content"]) <= TARGET_TOKENS:
                prev["content"] += f"\n\n### {s['heading_path'].split(' > ')[-1]}\n{s['content']}"
                continue
        merged.append(dict(s))

    chunks = []
    for i, s in enumerate(merged):
        embed_text = f"# {s['heading_path']}\n\n{s['content']}"
        chunks.append(
            {
                "chunk_index": i,
                "heading_path": s["heading_path"],
                "content": s["content"],
                "token_count": est_tokens(s["content"]),
                "content_hash": hashlib.sha256(embed_text.encode()).hexdigest(),
                "embed_text": embed_text,
            }
        )
    return meta, chunks
