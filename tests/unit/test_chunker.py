from vigil.rag.chunker import MAX_TOKENS, chunk_markdown, parse_frontmatter

SAMPLE = """---
slug: sample
title: Sample Runbook
service_tags: [checkout]
---

# Sample Runbook

Intro paragraph before any H2.

## First procedure

Do the first thing. This section is short.

## Second procedure

Do the second thing, which is unrelated to the first.

### Substep detail

More detail on the second thing.
"""


def test_frontmatter_parsed():
    meta, body = parse_frontmatter(SAMPLE)
    assert meta["slug"] == "sample"
    assert meta["service_tags"] == ["checkout"]
    assert "---" not in body.split("\n")[0]


def test_heading_paths_and_breadcrumbs():
    meta, chunks = chunk_markdown(SAMPLE)
    paths = [c["heading_path"] for c in chunks]
    assert paths[0] == "Sample Runbook"  # preamble
    assert "Sample Runbook > First procedure" in paths
    assert any(p.startswith("Sample Runbook > Second procedure") for p in paths)
    for c in chunks:
        assert c["embed_text"].startswith(f"# {c['heading_path']}")
        assert c["content_hash"]


def test_h2_sections_do_not_merge_across_parents():
    _, chunks = chunk_markdown(SAMPLE)
    first = next(c for c in chunks if c["heading_path"].endswith("First procedure"))
    assert "Second procedure" not in first["content"]


def test_oversized_section_splits_with_all_chunks_under_max():
    big_paragraphs = "\n\n".join(f"Paragraph {i}. " + "word " * 120 for i in range(10))
    doc = f"# Big Doc\n\n## Huge section\n\n{big_paragraphs}\n"
    _, chunks = chunk_markdown(doc)
    assert len(chunks) > 1
    assert all(c["token_count"] <= MAX_TOKENS for c in chunks)
    assert all(c["heading_path"] == "Big Doc > Huge section" for c in chunks)


def test_chunker_is_deterministic():
    assert chunk_markdown(SAMPLE) == chunk_markdown(SAMPLE)
