#!/usr/bin/env python3
"""Run a small, offline end-to-end PDF/JSON-to-EPUB smoke test."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import fitz  # type: ignore
from PIL import Image, ImageDraw  # type: ignore


ROOT = Path(__file__).resolve().parent.parent


def make_fixture(root: Path) -> tuple[Path, Path, Path]:
    pdf_path = root / "fixture.pdf"
    document = fitz.open()
    cover = document.new_page(width=420, height=600)
    cover.insert_text((72, 180), "PDF to EPUB Smoke Test", fontsize=22)
    page = document.new_page(width=420, height=600)
    page.insert_text((72, 100), "Chapter 1", fontsize=20)
    document.set_metadata({"title": "Smoke Test Book", "author": "Test Runner"})
    document.set_toc([[1, "Cover", 1], [1, "Chapter 1", 2]])
    document.save(pdf_path)
    document.close()

    assets_dir = root / "assets"
    figure_path = assets_dir / "imgs" / "figure.png"
    figure_path.parent.mkdir(parents=True)
    image = Image.new("RGB", (320, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 25, 300, 135), outline="navy", width=4)
    draw.line((40, 115, 110, 70, 185, 95, 280, 45), fill="darkred", width=5)
    image.save(figure_path)

    pages = [
        {
            "prunedResult": {"parsing_res_list": []},
            "markdown": {"text": "# Smoke Test Book", "images": {}},
        },
        {
            "prunedResult": {
                "parsing_res_list": [
                    {"block_label": "paragraph_title", "block_content": "Chapter 1"},
                    {"block_label": "figure_title", "block_content": "Figure 1. Test chart"},
                    {"block_label": "image", "block_content": "![chart](imgs/figure.png)"},
                    {"block_label": "footnote", "block_content": "1. Offline test footnote."},
                ]
            },
            "markdown": {
                "text": (
                    "# Chapter 1\n\n"
                    "Inline math $E=mc^2$.\n\n"
                    "$$\\int_0^1 x^2 \\, dx = \\frac{1}{3}$$\n\n"
                    "| Item | Value |\n|---|---:|\n| Alpha | 1 |\n\n"
                    "![chart](imgs/figure.png)\n\n"
                    "1. Offline test footnote.\n\n"
                    "<script>alert('unsafe')</script>"
                ),
                "images": {"imgs/figure.png": ""},
            },
        },
    ]
    json_path = root / "fixture.jsonl"
    json_path.write_text(
        "\n".join(
            json.dumps({"result": {"layoutParsingResults": [page]}}, ensure_ascii=False)
            for page in pages
        )
        + "\n",
        encoding="utf-8",
    )
    return pdf_path, json_path, assets_dir


def validate_epub(epub_path: Path) -> None:
    with zipfile.ZipFile(epub_path) as archive:
        names = archive.namelist()
        if names[0] != "mimetype":
            raise AssertionError("mimetype must be the first ZIP entry")
        if archive.getinfo("mimetype").compress_type != zipfile.ZIP_STORED:
            raise AssertionError("mimetype must be stored without compression")
        required = {"META-INF/container.xml", "OEBPS/content.opf", "OEBPS/text/nav.xhtml"}
        missing = required.difference(names)
        if missing:
            raise AssertionError(f"missing EPUB files: {sorted(missing)}")
        if not any(name.startswith("OEBPS/images/figure-") for name in names):
            raise AssertionError("localized figure is missing")
        content = "\n".join(
            archive.read(name).decode("utf-8")
            for name in names
            if name.endswith(".xhtml")
        )
        if "<script" in content or "alert('unsafe')" in content:
            raise AssertionError("unsafe OCR HTML was packaged")
        if "Figure 1. Test chart" not in content:
            raise AssertionError("figure caption was not used as alt text")
        if content.count("Offline test footnote") != 1:
            raise AssertionError("OCR footnote was duplicated")


def run_test(*, skip_epubcheck: bool, keep_temp: bool) -> dict[str, object]:
    temporary = Path(tempfile.mkdtemp(prefix="paddleocr-epub-smoke-"))
    try:
        pdf_path, json_path, assets_dir = make_fixture(temporary)
        output_path = temporary / "fixture.epub"
        work_dir = temporary / "work"
        command = [
            sys.executable,
            str(ROOT / "scripts" / "build_epub.py"),
            str(pdf_path),
            str(json_path),
            "--ocr-assets-dir",
            str(assets_dir),
            "--output",
            str(output_path),
            "--work-dir",
            str(work_dir),
            "--title",
            "Smoke Test Book",
            "--author",
            "Test Runner",
            "--language",
            "en",
        ]
        if skip_epubcheck:
            command.append("--skip-epubcheck")
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                "Smoke pipeline failed.\nSTDOUT:\n"
                + result.stdout[-4000:]
                + "\nSTDERR:\n"
                + result.stderr[-4000:]
            )
        validate_epub(output_path)
        report = json.loads((work_dir / "build_report.json").read_text(encoding="utf-8"))
        assertions = {
            "pdf_pages": report.get("pdf_pages") == 2,
            "image": (report.get("images") or {}).get("unique") == 1,
            "display_math": (report.get("math") or {}).get("display", 0) >= 1,
            "inline_math": (report.get("math") or {}).get("inline", 0) >= 1,
            "table": report.get("tables", 0) >= 1,
            "footnote_not_duplicated": report.get("footnote_pages", 0) == 0,
            "epubcheck": (report.get("epubcheck") or {}).get("status")
            in {"passed", "skipped"},
        }
        failed = [name for name, passed in assertions.items() if not passed]
        if failed:
            raise AssertionError(f"Smoke assertions failed: {failed}")
        summary: dict[str, object] = {
            "status": "passed",
            "epubcheck": report["epubcheck"]["status"],
            "epub_bytes": output_path.stat().st_size,
        }
        if keep_temp:
            summary["temporary_directory"] = str(temporary)
        return summary
    finally:
        if not keep_temp:
            import shutil

            shutil.rmtree(temporary, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the offline PDF-to-EPUB smoke test.")
    parser.add_argument("--skip-epubcheck", action="store_true")
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()
    try:
        summary = run_test(skip_epubcheck=args.skip_epubcheck, keep_temp=args.keep_temp)
    except Exception as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
