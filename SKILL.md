---
name: paddleocr-pdf-to-epub
description: Convert PDF books or existing PaddleOCR JSON/JSONL results into polished, reflowable, validated EPUB 3 files. Use when an agent needs to convert a PDF to EPUB, turn a scanned book into a searchable ebook, build from PaddleOCR output, install or configure the conversion environment, or diagnose PaddleOCR input and EPUBCheck errors. Support both manual OCR handoff and a reusable API token.
---

# PaddleOCR PDF to EPUB

Follow four stages: prepare the environment, obtain PaddleOCR JSON, build the EPUB, and verify it. Keep the user in control of any PDF upload.

## 1. Prepare the environment

Resolve this skill directory as `SKILL_DIR`. Run the environment check before processing a book:

```bash
python3 "$SKILL_DIR/scripts/doctor.py"
```

If Python packages are missing, create a dedicated virtual environment inside the skill, install `requirements.txt`, and use that environment's absolute Python path for every later command. Adapt the activation path on Windows.

```bash
python3 -m venv "$SKILL_DIR/.venv"
"$SKILL_DIR/.venv/bin/python" -m pip install -r "$SKILL_DIR/requirements.txt"
"$SKILL_DIR/.venv/bin/python" "$SKILL_DIR/scripts/doctor.py"
```

Set `PYTHON` to the absolute path of the interpreter that passed `doctor.py` (for example, `PYTHON="$SKILL_DIR/.venv/bin/python"`). Do not rely on shell activation carrying into a later agent command.

Install `epubcheck` with an available package manager, or set `EPUBCHECK_JAR` to a local EPUBCheck JAR and ensure Java is available. Rerun `doctor.py` until all required checks pass.

## 2. Obtain PaddleOCR JSON

If the user already supplied PaddleOCR JSON, JSONL, or normalized `pages.json`, keep it local and continue to stage 3.

If no OCR result exists, explain that the API path uploads the PDF to PaddleOCR, then ask the user to choose one path:

- **Manual:** Direct the user to <https://aistudio.baidu.com/paddleocr>. Ask them to process the PDF, download the JSON, and return it to the task. Also accept a downloaded image-assets directory when the site provides one. Wait for the result before building.
- **Reusable API:** Configure the token through the hidden prompt once, then run OCR. Never request or echo the token in chat.

```bash
"$PYTHON" "$SKILL_DIR/scripts/configure_token.py"
"$PYTHON" "$SKILL_DIR/scripts/paddleocr_api.py" BOOK.pdf --output-dir BOOK_ocr
```

The API command writes `BOOK_ocr/pages.json` plus localized image assets. Reuse that directory when rebuilding; rerun with `--force` only when the PDF or OCR settings changed.

## 3. Build from JSON

For a manual JSON result:

```bash
"$PYTHON" "$SKILL_DIR/scripts/build_epub.py" BOOK.pdf RESULT.json \
  --output BOOK.epub --work-dir BOOK_epub_work
```

Add `--ocr-assets-dir ASSETS_DIR` when local OCR images were downloaded separately. For API output, use `BOOK_ocr/pages.json` and `--ocr-assets-dir BOOK_ocr`.

Pass `--title`, `--author`, or `--language` only when metadata needs correction. Accept website JSON arrays, API JSONL, and normalized `pages.json`; do not assume one JSONL line equals one PDF page.

## 4. Verify and deliver

Read `BOOK_epub_work/build_report.json`. Require `epubcheck.status` to be `passed`; fix the environment or EPUB and rebuild on any other default result. Use `--skip-epubcheck` only after the user explicitly accepts an internally checked but externally unvalidated EPUB.

Deliver the EPUB and build-report paths. Report the PDF/OCR page counts, table-of-contents source, image count, formula fallbacks, and EPUBCheck result.

## Safety and recovery

- Keep credentials outside the repository. Use the interactive configurator or `PADDLEOCR_API_TOKEN`; never place a token in a command argument, source file, log, or response.
- Treat OCR JSON as untrusted input. Download only PaddleOCR-hosted HTTPS assets; require a local assets directory for other hosts.
- Keep each book in its own OCR and build directories. The scripts refuse unsafe cache reuse and avoid deleting caller-owned directories.
- Apply the API service's size and page limits only to the API path. Existing JSON can still be built locally.
