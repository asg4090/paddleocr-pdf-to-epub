# PaddleOCR PDF to EPUB

> **The best PDF-to-EPUB skill built for agents.**

Turn complete PDF books into polished, reflowable, validated EPUB 3 files through a workflow designed for agents from the ground up. The skill gives an agent a clear path to inspect the environment, obtain PaddleOCR JSON, build the book, verify the result, and report exactly what happened.

Users can hand over existing OCR results, choose a simple manual PaddleOCR workflow, or configure an API token once for repeat conversions. The agent handles environment setup, safe credential use, intermediate artifacts, recovery, and EPUBCheck validation.

## Why it is agent-native

- One explicit workflow: prepare, obtain OCR JSON, build, validate, deliver.
- Three clean starting states: existing JSON, manual web OCR, or reusable API access.
- Deterministic scripts with machine-readable reports and safe failure behavior.
- No bundled runtime, vendored Python package, or 35 MB EPUBCheck distribution.
- Strict validation by default, with an explicit opt-out only when the user approves it.

## Install as a Codex skill

```bash
git clone --depth 1 https://github.com/asg4090/paddleocr-pdf-to-epub.git \
  "${CODEX_HOME:-$HOME/.codex}/skills/paddleocr-pdf-to-epub"
```

Restart Codex or open a new task, then say:

```text
Use $paddleocr-pdf-to-epub to convert this PDF book into a validated EPUB.
```

The skill checks Python dependencies and EPUBCheck. When needed, the agent creates a dedicated `.venv` and installs `requirements.txt`.

## Choose an OCR path

1. **Manual OCR:** Upload the PDF at [PaddleOCR Online](https://aistudio.baidu.com/paddleocr), download the JSON result, and give it to the agent. This path does not require a saved API token.
2. **Reusable API:** Save the token through a hidden prompt once, then let the agent submit future PDFs directly. This path uploads the PDF to the PaddleOCR service.

When JSON or JSONL is already available, the agent skips OCR and starts building immediately.

## Command-line usage

Prepare the environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/doctor.py
```

On macOS, install EPUBCheck with `brew install epubcheck`. On other systems, install Java and set `EPUBCHECK_JAR` to a downloaded EPUBCheck JAR.

Build from existing OCR JSON:

```bash
.venv/bin/python scripts/build_epub.py BOOK.pdf RESULT.json \
  --output BOOK.epub \
  --work-dir BOOK_epub_work
```

Add `--ocr-assets-dir OCR_ASSETS` when the web workflow also provides a local image-assets directory.

Use the API workflow:

```bash
.venv/bin/python scripts/configure_token.py
.venv/bin/python scripts/paddleocr_api.py BOOK.pdf --output-dir BOOK_ocr
.venv/bin/python scripts/build_epub.py BOOK.pdf BOOK_ocr/pages.json \
  --ocr-assets-dir BOOK_ocr \
  --output BOOK.epub \
  --work-dir BOOK_epub_work
```

The builder accepts PaddleOCR website JSON, API JSONL, and normalized `pages.json`. It localizes figures, generates navigation and metadata, handles tables and common formulas, and runs EPUBCheck before delivery.

## Development checks

```bash
.venv/bin/python -m compileall -q scripts
.venv/bin/python scripts/smoke_test.py --skip-epubcheck
```

The project is released under the [MIT License](LICENSE). It is an independent community project and is not affiliated with or endorsed by Baidu or PaddleOCR.
