#!/usr/bin/env python3
"""PaddleOCR cloud job client and result normalizer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from configure_token import TOKEN_ENV, token_file_path


JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
DEFAULT_MODEL = "PaddleOCR-VL-1.6"
FALLBACK_MODEL = "PaddleOCR-VL-1.5"
TRUSTED_DOWNLOAD_HOSTS = {
    "paddleocr.aistudio-app.com",
}
MAX_RESULT_BYTES = 512 * 1024 * 1024
MAX_IMAGE_BYTES = 50 * 1024 * 1024


class PaddleOCRError(RuntimeError):
    """Base error for the cloud OCR pipeline."""


class PaddleOCRJobFailed(PaddleOCRError):
    def __init__(self, message: str, *, model: str):
        super().__init__(message)
        self.model = model


@dataclass
class OCRRun:
    pages_path: Path
    source_result_path: Path
    manifest_path: Path
    model: str
    page_count: int


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding=encoding, dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    temporary.replace(path)


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)


def write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_api_token() -> str:
    """Load the token without logging it.

    Resolution order: environment, then the private per-user config file.
    """

    candidates = [os.environ.get(TOKEN_ENV)]
    token_file = token_file_path()
    if token_file.exists():
        candidates.append(token_file.read_text(encoding="utf-8").strip())
    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate.strip()
    raise PaddleOCRError(
        f"PaddleOCR API token is not configured. Run configure_token.py or set {TOKEN_ENV}."
    )


def make_session(use_env_proxy: bool) -> requests.Session:
    session = requests.Session()
    session.trust_env = use_env_proxy
    if not use_env_proxy:
        session.proxies = {"http": None, "https": None}
    session.headers.update({"User-Agent": "Codex-PaddleOCR-PDF-to-EPUB/1.0"})
    return session


def _request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    *,
    attempts: int = 4,
    **kwargs: Any,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = session.request(method, url, **kwargs)
            if response.status_code not in {408, 425, 429, 500, 502, 503, 504}:
                return response
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else min(2**attempt, 12)
            response.close()
            time.sleep(delay)
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(2**attempt, 12))
    if last_error:
        raise PaddleOCRError(f"Network request failed after {attempts} attempts: {last_error}")
    return response


def _authorization(token: str) -> dict[str, str]:
    return {"Authorization": f"bearer {token}"}


def submit_job(
    session: requests.Session,
    source_pdf: Path,
    *,
    token: str,
    model: str,
    optional_payload: dict[str, bool],
) -> dict[str, Any]:
    data = {"model": model, "optionalPayload": json.dumps(optional_payload)}
    with source_pdf.open("rb") as file_handle:
        response = _request_with_retries(
            session,
            "POST",
            JOB_URL,
            headers=_authorization(token),
            data=data,
            files={"file": (source_pdf.name, file_handle, "application/pdf")},
            timeout=(20, 180),
        )
    if response.status_code != 200:
        raise PaddleOCRJobFailed(
            f"Job submission failed for {model}: HTTP {response.status_code}: {response.text[:1200]}",
            model=model,
        )
    payload = response.json()
    try:
        job_id = payload["data"]["jobId"]
    except (KeyError, TypeError) as exc:
        raise PaddleOCRError(f"Submission response has no jobId: {payload}") from exc
    if not job_id:
        raise PaddleOCRError("Submission response returned an empty jobId")
    return payload


def poll_job(
    session: requests.Session,
    job_id: str,
    *,
    token: str,
    model: str,
    status_path: Path,
    timeout: int,
    poll_interval: int,
) -> str:
    deadline = time.monotonic() + timeout
    last_progress: tuple[Any, ...] | None = None
    while time.monotonic() < deadline:
        response = _request_with_retries(
            session,
            "GET",
            f"{JOB_URL}/{job_id}",
            headers=_authorization(token),
            timeout=(15, 120),
        )
        if response.status_code != 200:
            raise PaddleOCRError(
                f"Polling job {job_id} failed: HTTP {response.status_code}: {response.text[:1200]}"
            )
        payload = response.json()
        data = payload.get("data") or {}
        state = str(data.get("state") or "unknown").lower()
        progress = data.get("extractProgress") or {}
        write_json(
            status_path,
            {
                "state": state,
                "extractProgress": progress,
                "errorMsg": data.get("errorMsg") if state == "failed" else None,
                "checked_at": utc_now(),
            },
        )
        marker = (state, progress.get("extractedPages"), progress.get("totalPages"))
        if marker != last_progress:
            print(
                f"OCR state={state}, pages={progress.get('extractedPages', '?')}/"
                f"{progress.get('totalPages', '?')}"
            )
            last_progress = marker
        if state == "done":
            json_url = ((data.get("resultUrl") or {}).get("jsonUrl") or "").strip()
            if not json_url:
                raise PaddleOCRError("Completed job did not provide resultUrl.jsonUrl")
            return json_url
        if state == "failed":
            raise PaddleOCRJobFailed(
                str(data.get("errorMsg") or "PaddleOCR job failed without an error message"),
                model=model,
            )
        time.sleep(max(1, poll_interval))
    raise TimeoutError(f"Timed out after {timeout} seconds waiting for PaddleOCR job {job_id}")


def download_file(
    session: requests.Session,
    url: str,
    destination: Path,
    *,
    timeout: tuple[int, int] = (15, 180),
    max_bytes: int = MAX_IMAGE_BYTES,
) -> None:
    current = url
    response: requests.Response | None = None
    for _ in range(6):
        parsed = urlparse(current)
        host = (parsed.hostname or "").lower().rstrip(".")
        trusted = host in TRUSTED_DOWNLOAD_HOSTS or host.endswith(".bcebos.com")
        if parsed.scheme != "https" or not trusted:
            raise PaddleOCRError(
                "Refusing an untrusted OCR asset URL. Provide the image through --ocr-assets-dir: "
                f"{host or '<missing host>'}"
            )
        response = _request_with_retries(
            session,
            "GET",
            current,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location", "")
            response.close()
            if not location:
                raise PaddleOCRError("OCR asset redirect has no Location header")
            current = urljoin(current, location)
            continue
        break
    else:
        raise PaddleOCRError("Too many redirects while downloading an OCR asset")

    assert response is not None
    if response.status_code != 200:
        response.close()
        raise PaddleOCRError(
            f"Download failed: HTTP {response.status_code} for {urlparse(current).path[-160:]}"
        )
    content_length = response.headers.get("Content-Length", "")
    if content_length.isdigit() and int(content_length) > max_bytes:
        response.close()
        raise PaddleOCRError(f"OCR asset exceeds the {max_bytes}-byte safety limit")

    ensure_private_dir(destination.parent)
    temporary: Path | None = None
    try:
        total = 0
        with tempfile.NamedTemporaryFile("wb", dir=destination.parent, delete=False) as handle:
            temporary = Path(handle.name)
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise PaddleOCRError(f"OCR asset exceeds the {max_bytes}-byte safety limit")
                handle.write(chunk)
        temporary.replace(destination)
    finally:
        response.close()
        if temporary and temporary.exists():
            temporary.unlink()


def _extract_pages(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        if all(isinstance(item, dict) and isinstance(item.get("markdown"), dict) for item in payload):
            return list(payload)
        pages: list[dict[str, Any]] = []
        for item in payload:
            pages.extend(_extract_pages(item))
        return pages
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("markdown"), dict):
        return [payload]
    if isinstance(payload.get("layoutParsingResults"), list):
        return [item for item in payload["layoutParsingResults"] if isinstance(item, dict)]
    if isinstance(payload.get("result"), (dict, list)):
        return _extract_pages(payload["result"])
    if isinstance(payload.get("data"), (dict, list)):
        return _extract_pages(payload["data"])
    return []


def load_pages_from_result(path: Path) -> list[dict[str, Any]]:
    """Load either website JSON, normalized pages.json, or API JSONL."""

    text = path.read_text(encoding="utf-8-sig")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        pages: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PaddleOCRError(f"Invalid JSONL at line {line_number}: {exc}") from exc
            pages.extend(_extract_pages(payload))
    else:
        pages = _extract_pages(payload)
    if not pages:
        raise PaddleOCRError(f"No PaddleOCR page objects found in {path}")
    for page_number, page in enumerate(pages, 1):
        markdown_payload = page.get("markdown")
        if not isinstance(markdown_payload, dict) or not isinstance(markdown_payload.get("text"), str):
            raise PaddleOCRError(
                f"PaddleOCR page {page_number} has no string markdown.text field"
            )
    return pages


def _safe_asset_path(output_dir: Path, relative_name: str) -> Path:
    normalized = PurePosixPath(relative_name.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise PaddleOCRError(f"Unsafe image path in OCR result: {relative_name!r}")
    destination = output_dir.joinpath(*normalized.parts).resolve()
    if output_dir.resolve() not in destination.parents:
        raise PaddleOCRError(f"Image path escapes OCR output directory: {relative_name!r}")
    return destination


def _referenced_image_sources(markdown_text: str) -> list[str]:
    html_sources = re.findall(r'<img[^>]+src=["\']([^"\']+)', markdown_text, flags=re.I)
    markdown_sources = re.findall(r'!\[[^\]]*\]\(([^)\s]+)', markdown_text)
    return list(dict.fromkeys(html_sources + markdown_sources))


def _copy_or_download_asset(
    session: requests.Session,
    source_name: str,
    remote_url: str,
    *,
    output_dir: Path,
    existing_assets_dir: Path | None,
    existing_source_name: str | None = None,
) -> Path:
    destination = _safe_asset_path(output_dir, source_name)
    if destination.exists() and destination.stat().st_size > 0:
        return destination
    if existing_assets_dir:
        candidate = _safe_asset_path(existing_assets_dir, existing_source_name or source_name)
        if candidate.exists() and candidate.stat().st_size > 0:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, destination)
            return destination
    if not remote_url.startswith("https://"):
        raise PaddleOCRError(f"No usable source for OCR image {source_name!r}")
    download_file(session, remote_url, destination, max_bytes=MAX_IMAGE_BYTES)
    return destination


def materialize_pages(
    pages: list[dict[str, Any]],
    *,
    output_dir: Path,
    session: requests.Session,
    source_result_path: Path,
    model: str,
    existing_assets_dir: Path | None = None,
    save_debug_images: bool = False,
    source_sha256: str | None = None,
    ocr_options: dict[str, bool] | None = None,
) -> OCRRun:
    ensure_private_dir(output_dir)
    downloaded_assets: set[str] = set()
    debug_assets = 0
    for page_index, page in enumerate(pages, 1):
        markdown = page.get("markdown") or {}
        markdown_text = str(markdown.get("text") or "")
        image_map = markdown.get("images") or {}
        localized_image_map: dict[str, str] = {}
        for source_name in _referenced_image_sources(markdown_text):
            remote_url = str(image_map.get(source_name) or "")
            source_parts = PurePosixPath(source_name.replace("\\", "/")).parts
            already_localized = (
                len(source_parts) >= 3
                and source_parts[0] == "page_assets"
                and source_parts[1] == f"{page_index:04d}"
            )
            localized_name = source_name if already_localized else f"page_assets/{page_index:04d}/{source_name}"
            _copy_or_download_asset(
                session,
                localized_name,
                remote_url,
                output_dir=output_dir,
                existing_assets_dir=existing_assets_dir,
                existing_source_name=source_name,
            )
            markdown_text = markdown_text.replace(source_name, localized_name)
            for block in (page.get("prunedResult") or {}).get("parsing_res_list") or []:
                block_content = block.get("block_content")
                if isinstance(block_content, str):
                    block["block_content"] = block_content.replace(source_name, localized_name)
            localized_image_map[localized_name] = ""
            downloaded_assets.add(localized_name)
        markdown["text"] = markdown_text
        markdown["images"] = localized_image_map
        page["markdown"] = markdown

        if save_debug_images:
            debug_dir = output_dir / "debug"
            for name, url in (page.get("outputImages") or {}).items():
                suffix = Path(urlparse(str(url)).path).suffix or ".jpg"
                destination = debug_dir / f"{name}_{page_index:04d}{suffix}"
                download_file(session, str(url), destination, max_bytes=MAX_IMAGE_BYTES)
                debug_assets += 1
            input_url = str(page.get("inputImage") or "")
            if input_url:
                suffix = Path(urlparse(input_url).path).suffix or ".jpg"
                download_file(
                    session,
                    input_url,
                    debug_dir / f"input_{page_index:04d}{suffix}",
                    max_bytes=MAX_IMAGE_BYTES,
                )
                debug_assets += 1

    pages_path = output_dir / "pages.json"
    write_json(pages_path, pages)
    manifest = {
        "schema": 2,
        "model": model,
        "pages": len(pages),
        "localized_markdown_assets": len(downloaded_assets),
        "debug_assets": debug_assets,
        "source_result": str(source_result_path),
        "source_sha256": source_sha256,
        "ocr_options": ocr_options or {},
        "created_at": utc_now(),
    }
    manifest_path = output_dir / "ocr_manifest.json"
    write_json(manifest_path, manifest)
    return OCRRun(
        pages_path=pages_path,
        source_result_path=source_result_path,
        manifest_path=manifest_path,
        model=model,
        page_count=len(pages),
    )


def _model_error(error: Exception) -> bool:
    message = str(error).lower()
    return any(
        token in message
        for token in (
            "unsupported model",
            "model not found",
            "invalid model",
            "model is not",
            "not support",
            "不支持的模型",
            "模型不存在",
            "模型不可用",
        )
    )


def run_cloud_ocr(
    source_pdf: Path,
    *,
    output_dir: Path,
    model: str = DEFAULT_MODEL,
    allow_model_fallback: bool = True,
    timeout: int = 7200,
    poll_interval: int = 5,
    use_env_proxy: bool = False,
    use_doc_orientation_classify: bool = False,
    use_doc_unwarping: bool = False,
    use_chart_recognition: bool = False,
    save_debug_images: bool = False,
    force: bool = False,
) -> OCRRun:
    source_pdf = source_pdf.expanduser().resolve()
    if not source_pdf.is_file() or source_pdf.suffix.lower() != ".pdf":
        raise PaddleOCRError(f"Source must be an existing PDF: {source_pdf}")
    if source_pdf.stat().st_size > 200 * 1024 * 1024:
        raise PaddleOCRError("Source exceeds the current 200 MB PaddleOCR API limit")
    try:
        import fitz  # type: ignore

        with fitz.open(source_pdf) as document:
            if document.page_count > 1000:
                raise PaddleOCRError("Source exceeds the current 1000-page PaddleOCR API limit")
    except PaddleOCRError:
        raise
    except Exception as exc:
        raise PaddleOCRError(f"Cannot inspect source PDF: {exc}") from exc

    ensure_private_dir(output_dir)
    pages_path = output_dir / "pages.json"
    manifest_path = output_dir / "ocr_manifest.json"
    source_result = output_dir / "result.jsonl"
    source_hash = sha256_file(source_pdf)
    optional_payload = {
        "useDocOrientationClassify": use_doc_orientation_classify,
        "useDocUnwarping": use_doc_unwarping,
        "useChartRecognition": use_chart_recognition,
    }
    models = [model]
    if allow_model_fallback and model == DEFAULT_MODEL:
        models.append(FALLBACK_MODEL)

    if not force and pages_path.exists() and manifest_path.exists():
        manifest = read_json(manifest_path)
        cache_matches = (
            manifest.get("schema") == 2
            and manifest.get("source_sha256") == source_hash
            and manifest.get("ocr_options") == optional_payload
            and manifest.get("model") in models
        )
        if cache_matches:
            pages = load_pages_from_result(pages_path)
            print(f"Reusing {len(pages)} cached PaddleOCR pages from {output_dir}")
            return OCRRun(
                pages_path=pages_path,
                source_result_path=source_result if source_result.exists() else pages_path,
                manifest_path=manifest_path,
                model=str(manifest["model"]),
                page_count=len(pages),
            )
        print("Ignoring OCR cache because its PDF hash, model, or options do not match")

    token = load_api_token()
    session = make_session(use_env_proxy)
    state_path = output_dir / "job_state.json"
    status_path = output_dir / "job_status_latest.json"
    state = read_json(state_path) if state_path.exists() and not force else {}
    state_matches = (
        state.get("schema") == 2
        and state.get("source_sha256") == source_hash
        and state.get("optional_payload") == optional_payload
        and state.get("model") in models
    )
    if not state_matches:
        state = {}
    if (
        state
        and state.get("job_id")
        and state.get("source_sha256") == source_hash
        and state.get("model") in models
    ):
        resumed_model = str(state["model"])
        models = [resumed_model, *[candidate for candidate in models if candidate != resumed_model]]
    last_error: Exception | None = None
    for selected_model in models:
        try:
            reusable = (
                state
                and state.get("job_id")
                and state.get("model") == selected_model
                and state.get("source_sha256") == source_hash
            )
            if reusable:
                job_id = str(state["job_id"])
                print(f"Resuming PaddleOCR job {job_id} ({selected_model})")
            else:
                print(f"Submitting {source_pdf.name} to PaddleOCR model {selected_model}")
                submit_payload = submit_job(
                    session,
                    source_pdf,
                    token=token,
                    model=selected_model,
                    optional_payload=optional_payload,
                )
                job_id = str(submit_payload["data"]["jobId"])
                state = {
                    "schema": 2,
                    "job_id": job_id,
                    "model": selected_model,
                    "source_name": source_pdf.name,
                    "source_sha256": source_hash,
                    "optional_payload": optional_payload,
                    "submitted_at": utc_now(),
                }
                write_json(state_path, state)

            result_url = poll_job(
                session,
                job_id,
                token=token,
                model=selected_model,
                status_path=status_path,
                timeout=timeout,
                poll_interval=poll_interval,
            )
            download_file(
                session,
                result_url,
                source_result,
                timeout=(20, 300),
                max_bytes=MAX_RESULT_BYTES,
            )
            pages = load_pages_from_result(source_result)
            return materialize_pages(
                pages,
                output_dir=output_dir,
                session=session,
                source_result_path=source_result,
                model=selected_model,
                save_debug_images=save_debug_images,
                source_sha256=source_hash,
                ocr_options=optional_payload,
            )
        except PaddleOCRJobFailed as exc:
            last_error = exc
            if selected_model != models[-1] and _model_error(exc):
                print(f"Model {selected_model} is unavailable; retrying with {models[-1]}")
                state = {}
                continue
            raise
    raise PaddleOCRError(f"PaddleOCR failed: {last_error}")


def import_ocr_result(
    result_path: Path,
    *,
    output_dir: Path,
    existing_assets_dir: Path | None = None,
    use_env_proxy: bool = False,
    model: str = "imported",
    save_debug_images: bool = False,
) -> OCRRun:
    result_path = result_path.expanduser().resolve()
    pages = load_pages_from_result(result_path)
    ensure_private_dir(output_dir)
    suffix = result_path.suffix.lower() if result_path.suffix.lower() in {".json", ".jsonl"} else ".json"
    archived_result = output_dir / f"source{suffix}"
    if result_path != archived_result.resolve():
        shutil.copy2(result_path, archived_result)
    else:
        archived_result = result_path
    session = make_session(use_env_proxy)
    return materialize_pages(
        pages,
        output_dir=output_dir,
        session=session,
        source_result_path=archived_result,
        model=model,
        existing_assets_dir=existing_assets_dir,
        save_debug_images=save_debug_images,
    )


__all__ = [
    "DEFAULT_MODEL",
    "FALLBACK_MODEL",
    "OCRRun",
    "PaddleOCRError",
    "import_ocr_result",
    "load_api_token",
    "load_pages_from_result",
    "run_cloud_ocr",
]


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Submit a PDF to PaddleOCR and save normalized JSON.")
    parser.add_argument("pdf", type=Path, help="PDF to upload to PaddleOCR.")
    parser.add_argument("--output-dir", type=Path, help="Persistent OCR directory.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--no-model-fallback", action="store_true")
    parser.add_argument("--force", action="store_true", help="Submit a fresh OCR job without deleting files.")
    parser.add_argument("--poll-interval", type=_positive_int, default=5)
    parser.add_argument("--timeout", type=_positive_int, default=7200)
    parser.add_argument("--use-env-proxy", action="store_true")
    parser.add_argument("--orientation-classify", action="store_true")
    parser.add_argument("--unwarp", action="store_true")
    parser.add_argument("--chart-recognition", action="store_true")
    parser.add_argument("--save-debug-images", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    pdf = args.pdf.expanduser().resolve()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else pdf.parent / f"{pdf.stem}_ocr"
    )
    try:
        result = run_cloud_ocr(
            pdf,
            output_dir=output_dir,
            model=args.model,
            allow_model_fallback=not args.no_model_fallback,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            use_env_proxy=args.use_env_proxy,
            use_doc_orientation_classify=args.orientation_classify,
            use_doc_unwarping=args.unwarp,
            use_chart_recognition=args.chart_recognition,
            save_debug_images=args.save_debug_images,
            force=args.force,
        )
    except (OSError, PaddleOCRError, TimeoutError, ValueError) as exc:
        print(f"PaddleOCR failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "pages": str(result.pages_path),
                "page_count": result.page_count,
                "model": result.model,
                "manifest": str(result.manifest_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
