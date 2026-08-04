#!/usr/bin/env python3
"""Build an EPUB from a PDF and existing PaddleOCR JSON/JSONL."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from epub_builder import build_epub
from paddleocr_api import PaddleOCRError, import_ocr_result


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a validated EPUB 3 from a PDF and PaddleOCR JSON/JSONL."
    )
    parser.add_argument("pdf", type=Path, help="Original PDF, used for cover and metadata.")
    parser.add_argument("ocr_result", type=Path, help="PaddleOCR website JSON, API JSONL, or pages.json.")
    parser.add_argument("--ocr-assets-dir", type=Path, help="Local directory containing OCR image paths.")
    parser.add_argument("--output", type=Path, help="Destination EPUB path.")
    parser.add_argument("--work-dir", type=Path, help="Persistent build/report directory.")
    parser.add_argument("--use-env-proxy", action="store_true", help="Use proxy variables for trusted image downloads.")
    parser.add_argument("--title")
    parser.add_argument("--author")
    parser.add_argument("--language")
    parser.add_argument("--publisher")
    parser.add_argument("--identifier")
    parser.add_argument("--max-pages-per-section", type=_positive_int, default=30)
    parser.add_argument(
        "--skip-epubcheck",
        action="store_true",
        help="Allow an internally checked EPUB without external EPUBCheck validation.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    pdf_path = args.pdf.expanduser().resolve()
    result_path = args.ocr_result.expanduser().resolve()
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        print(f"Source must be an existing PDF: {pdf_path}", file=sys.stderr)
        return 2
    if not result_path.is_file():
        print(f"OCR result does not exist: {result_path}", file=sys.stderr)
        return 2

    output_path = (args.output or pdf_path.with_suffix(".epub")).expanduser().resolve()
    work_dir = (
        args.work_dir or (pdf_path.parent / f"{pdf_path.stem}_epub_work")
    ).expanduser().resolve()
    if output_path == pdf_path:
        print("Output EPUB path must differ from the source PDF.", file=sys.stderr)
        return 2
    assets_dir = args.ocr_assets_dir.expanduser().resolve() if args.ocr_assets_dir else None
    if assets_dir and not assets_dir.is_dir():
        print(f"OCR assets directory does not exist: {assets_dir}", file=sys.stderr)
        return 2

    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        ocr_run = import_ocr_result(
            result_path,
            output_dir=work_dir / "ocr",
            existing_assets_dir=assets_dir,
            use_env_proxy=args.use_env_proxy,
        )
        result = build_epub(
            pdf_path,
            ocr_run.pages_path,
            ocr_dir=work_dir / "ocr",
            output_path=output_path,
            work_dir=work_dir,
            title=args.title,
            author=args.author,
            language=args.language,
            publisher=args.publisher,
            identifier=args.identifier,
            max_pages_per_section=args.max_pages_per_section,
            skip_epubcheck=args.skip_epubcheck,
        )
    except (OSError, PaddleOCRError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"EPUB build failed: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "epub": str(result.epub_path),
                "report": str(result.report_path),
                "ocr_pages": ocr_run.page_count,
                "epubcheck": result.report["epubcheck"]["status"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
