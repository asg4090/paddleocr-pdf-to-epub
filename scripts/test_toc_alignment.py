#!/usr/bin/env python3
"""Regression tests for PDF-outline / TOC alignment in epub_builder.

Covers choose_outline(): a PDF outline authored with printed page numbers
(systematic offset) is repaired, a garbage outline falls back to
OCR-inferred headings, and a correct outline is left untouched.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import fitz  # type: ignore

sys.path.insert(0, str(Path(__file__).resolve().parent))

from epub_builder import choose_outline  # noqa: E402


def _title(text: str) -> str:
    return text.lstrip("#").strip()


def make_book(
    work: Path, contents: list[str], toc: list[list] | None
) -> tuple[Path, list[dict]]:
    document = fitz.open()
    for text in contents:
        page = document.new_page(width=420, height=600)
        page.insert_text((72, 120), _title(text)[:24], fontsize=18)
    if toc:
        document.set_toc(toc)
    pdf_path = work / "book.pdf"
    document.save(pdf_path)
    document.close()
    pages = [
        {"prunedResult": {"parsing_res_list": []}, "markdown": {"text": text, "images": {}}}
        for text in contents
    ]
    return pdf_path, pages


CONTENTS = [
    "Cover",
    "Contents",
    "# Chapter One",
    "# Chapter Two",
    "## Section 2.1",
    "# Chapter Three",
]


def test_printed_numbering_offset() -> None:
    """Bookmarks using printed page numbers (off by 2) are repaired."""
    work = Path(tempfile.mkdtemp(prefix="toc-test-"))
    printed = {"Chapter One": 1, "Chapter Two": 2, "Section 2.1": 2, "Chapter Three": 3}
    toc = [
        [2 if text.startswith("##") else 1, _title(text), printed[_title(text)]]
        for text in CONTENTS[2:]
    ]
    pdf_path, pages = make_book(work, CONTENTS, toc)
    entries, source, detail = choose_outline(pdf_path, pages)
    assert source == "pdf-outline", source
    assert detail["page_offset"] == 2, detail
    by_title = {entry.title: entry.page for entry in entries}
    assert by_title["Chapter One"] == 3, by_title
    assert by_title["Chapter Two"] == 4, by_title
    assert by_title["Section 2.1"] == 5, by_title
    assert by_title["Chapter Three"] == 6, by_title


def test_garbage_outline_falls_back() -> None:
    """An outline that cannot be verified falls back to OCR headings."""
    work = Path(tempfile.mkdtemp(prefix="toc-test-"))
    toc = [
        [1, "Made up title one", 5],
        [2, "Invented subtitle", 2],
        [1, "Nonexistent chapter", 6],
    ]
    pdf_path, pages = make_book(work, CONTENTS, toc)
    entries, source, _ = choose_outline(pdf_path, pages)
    assert source == "ocr-headings", source
    assert [entry.title for entry in entries] == [
        "Chapter One",
        "Chapter Two",
        "Section 2.1",
        "Chapter Three",
    ]


def test_correct_outline_untouched() -> None:
    """A correct outline is used as-is with no page shift."""
    work = Path(tempfile.mkdtemp(prefix="toc-test-"))
    toc = [
        [2 if text.startswith("##") else 1, _title(text), index]
        for index, text in enumerate(CONTENTS[2:], 3)
    ]
    pdf_path, pages = make_book(work, CONTENTS, toc)
    entries, source, detail = choose_outline(pdf_path, pages)
    assert source == "pdf-outline", source
    assert detail["page_offset"] == 0, detail
    assert [entry.page for entry in entries] == [3, 4, 5, 6]


def main() -> int:
    tests = [
        test_printed_numbering_offset,
        test_garbage_outline_falls_back,
        test_correct_outline_untouched,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        else:
            print(f"PASS {test.__name__}")
    print(f"{len(tests) - failed}/{len(tests)} TOC alignment tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
