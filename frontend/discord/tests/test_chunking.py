from discord_frontend.chunking import chunk_message


def test_short_text_single_chunk():
    assert chunk_message("hello") == ["hello"]


def test_paragraph_splitting():
    text = "\n\n".join(["para one", "para two", "para three"])
    parts = chunk_message(text, limit=20)
    assert all(len(p) <= 20 for p in parts)
    assert "".join("".join(p.split("```\n")) for p in parts).count("para") == 3


def test_fence_reopened_across_chunks():
    text = "intro\n\n```python\n" + "x = 1\n" * 100 + "```\n"
    parts = chunk_message(text, limit=200)
    assert len(parts) > 1
    for p in parts:
        assert p.count("```") % 2 == 0  # fences never left dangling


def test_pathological_single_line():
    parts = chunk_message("a" * 5000, limit=2000)
    assert all(len(p) <= 2000 for p in parts)
    assert "".join(parts) == "a" * 5000


def test_close_suffix_reserved_from_budget():
    text = "intro\n\n```python\n" + "x" * 2500 + "\n```"
    parts = chunk_message(text, limit=2000)
    assert all(len(p) <= 2000 for p in parts)
    assert "".join(parts).count("x") == 2500  # payload preserved
    assert all(p.count("```") % 2 == 0 for p in parts)


def test_adversarial_inputs_respect_limit():
    cases = [
        "",
        "   \n\n\t",
        "```",
        "```\ncode\n```",
        "a" * 1999,
        "a" * 2000,
        "a" * 2001,
        "a" * 2000 + "\n\nb",
        "```python\n" + "line\n" * 700,
        "```\n" + "z" * 2000 + "\n```",
        "intro\n\n```py\n" + "y" * 1999 + "\n```\n\noutro",
        ("word\n\n" * 300).strip(),
        ("inline ``` fence\n\n" * 300).strip(),
        "x" * 1997 + "\n```",
        "```" + "x" * 1996,
    ]
    for text in cases:
        for limit in (2000, 200, 50):
            parts = chunk_message(text, limit)
            assert all(len(p) <= limit for p in parts), (limit, len(text))
    assert chunk_message("") == []
    assert chunk_message("  \n\n ") == []
