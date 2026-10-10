#!/usr/bin/env python3
"""Build a generic, reflowable EPUB 3 from PaddleOCR page objects."""

from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import re
import subprocess
import tempfile
import unicodedata
import uuid
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

import fitz  # type: ignore
import markdown  # type: ignore
from bs4 import BeautifulSoup  # type: ignore
from latex2mathml.converter import convert as latex_to_mathml  # type: ignore
from lxml import etree  # type: ignore
from PIL import Image  # type: ignore

from doctor import epubcheck_base_command


@dataclass(frozen=True)
class Metadata:
    title: str
    author: str
    language: str
    publisher: str = ""
    identifier: str = ""
    source: str = ""


@dataclass
class TocEntry:
    level: int
    title: str
    page: int
    children: list["TocEntry"] = field(default_factory=list)


@dataclass
class Section:
    title: str
    start_page: int
    end_page: int
    filename: str
    properties: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class ImageRef:
    page: int
    source: str
    local_path: Path
    alt: str


@dataclass
class BuildResult:
    epub_path: Path
    report_path: Path
    report: dict[str, Any]


def write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)


def write_json(path: Path, payload: Any) -> None:
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def clean_text(value: str) -> str:
    value = value or ""
    if "<" in value and ">" in value:
        value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    value = unicodedata.normalize("NFKC", html.unescape(value))
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n-–—:：")
    value = re.sub(r"\b(Chapter\s+\d+)\.(?=\S)", r"\1. ", value, flags=re.I)
    return value


def slug(value: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    ascii_value = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    if ascii_value:
        return ascii_value[:64]
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return f"{fallback}-{digest}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _usable_metadata(value: str | None, *, filename: str) -> bool:
    if not value or not value.strip():
        return False
    candidate = value.strip().lower()
    generic = {"untitled", "unknown", "microsoft word", "pdf", filename.lower()}
    return candidate not in generic and not candidate.startswith("scan")


def _first_heading(pages: list[dict[str, Any]]) -> str:
    for page in pages[:8]:
        text = str((page.get("markdown") or {}).get("text") or "")
        for match in re.finditer(r"^#{1,3}\s+(.+?)\s*$", text, flags=re.M):
            candidate = clean_text(match.group(1))
            compact = re.sub(r"\s+", "", candidate)
            if 2 <= len(candidate) <= 180 and compact.casefold() not in {"contents", "目录"}:
                return candidate
        blocks = (page.get("prunedResult") or {}).get("parsing_res_list") or []
        for block in blocks:
            if block.get("block_label") in {"doc_title", "paragraph_title"}:
                candidate = clean_text(str(block.get("block_content") or ""))
                if 2 <= len(candidate) <= 180:
                    return candidate
    return "Untitled Book"


def detect_language(pages: list[dict[str, Any]]) -> str:
    sample = "".join(
        str((page.get("markdown") or {}).get("text") or "") for page in pages[:40]
    )[:100_000]
    han = len(re.findall(r"[\u3400-\u9fff]", sample))
    latin = len(re.findall(r"[A-Za-z]", sample))
    if han > max(100, latin * 0.25):
        return "zh-CN"
    return "en"


def infer_metadata(
    pdf_path: Path,
    pages: list[dict[str, Any]],
    *,
    title: str | None = None,
    author: str | None = None,
    language: str | None = None,
    publisher: str | None = None,
    identifier: str | None = None,
) -> Metadata:
    with fitz.open(pdf_path) as document:
        pdf_meta = document.metadata or {}
    inferred_title = title
    if not inferred_title and _usable_metadata(pdf_meta.get("title"), filename=pdf_path.name):
        inferred_title = str(pdf_meta["title"]).strip()
    inferred_title = inferred_title or _first_heading(pages)
    inferred_author = author
    if not inferred_author and _usable_metadata(pdf_meta.get("author"), filename=pdf_path.name):
        inferred_author = str(pdf_meta["author"]).strip()
    inferred_author = inferred_author or "Unknown"
    source_hash = sha256_file(pdf_path)
    inferred_identifier = identifier or f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, source_hash)}"
    return Metadata(
        title=clean_text(inferred_title),
        author=clean_text(inferred_author),
        language=language or detect_language(pages),
        publisher=clean_text(publisher or str(pdf_meta.get("producer") or "")),
        identifier=inferred_identifier,
        source=pdf_path.name,
    )


def pdf_outline(pdf_path: Path, page_count: int) -> list[TocEntry]:
    with fitz.open(pdf_path) as document:
        raw = document.get_toc(simple=True)
    entries: list[TocEntry] = []
    seen: set[tuple[int, str, int]] = set()
    for level, title, page in raw:
        candidate = clean_text(str(title))
        level = max(1, min(int(level), 4))
        page = int(page)
        key = (level, candidate.casefold(), page)
        if candidate and 1 <= page <= page_count and key not in seen:
            entries.append(TocEntry(level, candidate[:240], page))
            seen.add(key)
    return entries


def _heading_level(title: str, markdown_level: int) -> int:
    normalized = clean_text(title)
    if re.search(r"^(chapter|part|book|appendix)\s+[\divxlcdm]+\b", normalized, re.I):
        return 1
    if re.search(r"^第[0-9一二三四五六七八九十百零〇两]+[篇部卷章]", normalized):
        return 1
    if re.search(
        r"^(preface|foreword|introduction|contents|bibliography|references|glossary|index|"
        r"前言|序言|绪论|引言|目录|参考文献|附录|索引)\b",
        normalized,
        re.I,
    ):
        return 1
    numeric = re.match(r"^(\d+(?:\.\d+){0,3})\b", normalized)
    if numeric:
        return min(numeric.group(1).count(".") + 1, 4)
    return max(1, min(markdown_level, 4))


def ocr_outline(pages: list[dict[str, Any]]) -> list[TocEntry]:
    candidates: list[TocEntry] = []
    seen_nearby: dict[str, int] = {}
    for page_number, page in enumerate(pages, 1):
        markdown_text = str((page.get("markdown") or {}).get("text") or "")
        found_on_page = 0
        for match in re.finditer(r"^(#{1,6})\s+(.+?)\s*$", markdown_text, flags=re.M):
            title = clean_text(match.group(2))
            if not (2 <= len(title) <= 200):
                continue
            level = _heading_level(title, len(match.group(1)))
            key = title.casefold()
            if page_number - seen_nearby.get(key, -100) <= 2:
                continue
            candidates.append(TocEntry(level, title, page_number))
            seen_nearby[key] = page_number
            found_on_page += 1
            if found_on_page >= 8 or len(candidates) >= 500:
                break
        if len(candidates) >= 500:
            break
    if not candidates:
        for page_number, page in enumerate(pages, 1):
            blocks = (page.get("prunedResult") or {}).get("parsing_res_list") or []
            for block in blocks:
                if block.get("block_label") not in {"doc_title", "paragraph_title"}:
                    continue
                title = clean_text(str(block.get("block_content") or ""))
                if 2 <= len(title) <= 200:
                    level = 1 if block.get("block_label") == "doc_title" else 2
                    candidates.append(TocEntry(_heading_level(title, level), title, page_number))
                    if len(candidates) >= 500:
                        return candidates
    if candidates:
        minimum = min(entry.level for entry in candidates)
        if minimum > 1:
            for entry in candidates:
                entry.level = max(1, entry.level - minimum + 1)
    return candidates


def choose_outline(
    pdf_path: Path, pages: list[dict[str, Any]]
) -> tuple[list[TocEntry], str, dict[str, Any]]:
    """Pick the table of contents, repairing a misaligned PDF outline.

    A PDF outline is only trusted after cross-checking it against the headings
    actually found in the OCR text.  Bookmarks are frequently authored with
    printed page numbers (ignoring front matter) or simply point at the wrong
    pages; without validation the generated EPUB navigation links chapters to
    the wrong locations.  When the bookmark titles line up with OCR headings
    at a shifted page, the systematic offset is corrected.  When too few
    entries can be verified, the OCR-inferred headings are used instead.
    """

    page_count = len(pages)
    pdf_entries = pdf_outline(pdf_path, page_count)
    ocr_entries = ocr_outline(pages)
    detail: dict[str, Any] = {"page_offset": 0, "verified": 0, "total": 0}
    if len({entry.page for entry in pdf_entries}) >= 3:
        index = _heading_index(ocr_entries)
        offset = _best_page_offset(pdf_entries, index, page_count)
        shifted = [
            TocEntry(entry.level, entry.title, min(page_count, max(1, entry.page + offset)))
            for entry in pdf_entries
        ]
        presnap_verified = sum(1 for entry in shifted if _entry_matches(entry, index, entry.page))
        ratio = presnap_verified / max(1, len(shifted))
        if not ocr_entries or len(ocr_entries) < 3 or ratio >= 0.5:
            realigned = [_snap_to_heading(entry, index) for entry in shifted]
            realigned.sort(key=lambda entry: (entry.page, entry.level))
            verified = sum(1 for entry in realigned if _entry_matches(entry, index, entry.page))
            detail = {"page_offset": offset, "verified": verified, "total": len(realigned)}
            return realigned, "pdf-outline", detail
    if ocr_entries:
        return ocr_entries, "ocr-headings", detail
    if pdf_entries:
        fallback = sorted(pdf_entries, key=lambda entry: (entry.page, entry.level))
        return fallback, "pdf-outline", detail
    return [], "generated-sections", detail


def _normalize_title(value: str) -> str:
    return re.sub(r"\s+", "", clean_text(value)).casefold()


def _titles_match(wanted: str, candidate: str) -> bool:
    if not wanted or not candidate:
        return False
    if wanted == candidate:
        return True
    short, long = (wanted, candidate) if len(wanted) <= len(candidate) else (candidate, wanted)
    return len(short) >= 3 and short in long


def _heading_index(entries: list[TocEntry]) -> dict[int, set[str]]:
    index: dict[int, set[str]] = defaultdict(set)
    for entry in entries:
        index[entry.page].add(_normalize_title(entry.title))
    return index


def _entry_matches(
    entry: TocEntry, index: dict[int, set[str]], page: int, *, window: int = 1
) -> bool:
    if page < 1:
        return False
    wanted = _normalize_title(entry.title)
    for candidate_page in range(page - window, page + window + 1):
        for candidate in index.get(candidate_page, ()):
            if _titles_match(wanted, candidate):
                return True
    return False


def _snap_to_heading(entry: TocEntry, index: dict[int, set[str]]) -> TocEntry:
    """Move a bookmark to the nearest page whose OCR heading matches its title.

    Repairs individual entries that a systematic page offset cannot explain,
    such as subsections sharing their chapter's printed page number.  Exact
    title matches are trusted over a wider window than loose containment
    matches; the closest candidate always wins.
    """

    wanted = _normalize_title(entry.title)

    def nearest(exact_only: bool, window: int) -> int | None:
        best: int | None = None
        for page in range(entry.page - window, entry.page + window + 1):
            if page < 1:
                continue
            for candidate in index.get(page, ()):
                matched = (wanted == candidate) if exact_only else _titles_match(wanted, candidate)
                if matched:
                    if best is None or abs(page - entry.page) < abs(best - entry.page):
                        best = page
        return best

    target = nearest(True, 8) or nearest(False, 3)
    if target is not None and target != entry.page:
        return TocEntry(entry.level, entry.title, target)
    return entry


def _best_page_offset(
    entries: list[TocEntry],
    index: dict[int, set[str]],
    page_count: int,
    *,
    max_offset: int = 10,
) -> int:
    """Find the systematic page shift that best aligns bookmarks with headings."""

    def score(offset: int) -> int:
        total = 0
        for entry in entries:
            page = entry.page + offset
            if 1 <= page <= page_count and _entry_matches(entry, index, page, window=0):
                total += 1
        return total

    baseline = score(0)
    best, best_score = 0, baseline
    for offset in range(-max_offset, max_offset + 1):
        if offset == 0:
            continue
        candidate = score(offset)
        if candidate > best_score or (candidate == best_score and abs(offset) < abs(best)):
            best, best_score = offset, candidate
    if best != 0 and best_score > baseline and best_score >= 2:
        return best
    return 0


def build_toc_tree(entries: list[TocEntry]) -> list[TocEntry]:
    roots: list[TocEntry] = []
    stack: list[TocEntry] = []
    for source in entries:
        entry = TocEntry(source.level, source.title, source.page)
        while len(stack) >= entry.level:
            stack.pop()
        if entry.level == 1 or not stack:
            roots.append(entry)
        else:
            stack[-1].children.append(entry)
        stack.append(entry)
    return roots


def make_sections(
    entries: list[TocEntry], page_count: int, *, max_pages: int = 30
) -> list[Section]:
    if page_count <= 1:
        return []
    start_titles: dict[int, str] = {}
    for entry in entries:
        if entry.level == 1 and entry.page >= 2:
            start_titles.setdefault(entry.page, entry.title)
    starts = sorted({2, *start_titles})
    sections: list[Section] = []
    section_number = 1
    for index, start in enumerate(starts):
        logical_end = (starts[index + 1] - 1) if index + 1 < len(starts) else page_count
        cursor = start
        chunk_number = 1
        while cursor <= logical_end:
            end = min(logical_end, cursor + max_pages - 1)
            base_title = start_titles.get(start, "Front Matter" if start == 2 else f"Pages {start}–{logical_end}")
            title = base_title if chunk_number == 1 else f"{base_title} — continued"
            filename = f"section-{section_number:04d}-{slug(base_title, 'section')}.xhtml"
            sections.append(Section(title, cursor, end, filename))
            section_number += 1
            chunk_number += 1
            cursor = end + 1
    return sections


def section_for_page(sections: list[Section], page: int) -> Section | None:
    for section in sections:
        if section.start_page <= page <= section.end_page:
            return section
    return None


def _safe_local_asset(ocr_dir: Path, source: str) -> Path:
    normalized = PurePosixPath(source.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise RuntimeError(f"Unsafe OCR image source: {source!r}")
    candidate = ocr_dir.joinpath(*normalized.parts).resolve()
    if ocr_dir.resolve() not in candidate.parents:
        raise RuntimeError(f"OCR image source escapes its directory: {source!r}")
    return candidate


def _image_sources(text: str) -> list[str]:
    sources = re.findall(r'<img[^>]+src=["\']([^"\']+)', text, flags=re.I)
    sources += re.findall(r'!\[[^\]]*\]\(([^)\s]+)', text)
    return list(dict.fromkeys(sources))


def _image_alt(page: dict[str, Any], source: str, page_number: int) -> str:
    blocks = (page.get("prunedResult") or {}).get("parsing_res_list") or []
    source_index = None
    for index, block in enumerate(blocks):
        if source in str(block.get("block_content") or ""):
            source_index = index
            break
    if source_index is not None:
        for block in reversed(blocks[max(0, source_index - 5) : source_index]):
            if block.get("block_label") == "figure_title":
                candidate = clean_text(str(block.get("block_content") or ""))
                if candidate:
                    return candidate[:500]
    markdown_text = str((page.get("markdown") or {}).get("text") or "")
    for label, image_source in re.findall(r"!\[([^\]]*)\]\(([^)\s]+)\)", markdown_text):
        if image_source == source and clean_text(label):
            return clean_text(label)[:500]
    return f"Illustration from PDF page {page_number}"


def collect_images(pages: list[dict[str, Any]], ocr_dir: Path) -> list[ImageRef]:
    refs: list[ImageRef] = []
    for page_number, page in enumerate(pages, 1):
        if page_number == 1:
            continue
        text = str((page.get("markdown") or {}).get("text") or "")
        for source in _image_sources(text):
            local_path = _safe_local_asset(ocr_dir, source)
            if not local_path.exists():
                raise RuntimeError(f"Missing localized OCR image for page {page_number}: {source}")
            refs.append(ImageRef(page_number, source, local_path, _image_alt(page, source, page_number)))
    return refs


def image_type(path: Path) -> tuple[str, str]:
    with Image.open(path) as image:
        fmt = str(image.format or "PNG").upper()
        image.verify()
    if fmt in {"JPEG", "JPG"}:
        return ".jpg", "image/jpeg"
    if fmt == "PNG":
        return ".png", "image/png"
    if fmt == "GIF":
        return ".gif", "image/gif"
    if fmt == "WEBP":
        return ".webp", "image/webp"
    return path.suffix.lower() or ".png", mimetypes.guess_type(path.name)[0] or "image/png"


def copy_images(
    refs: list[ImageRef], image_dir: Path
) -> tuple[dict[tuple[int, str], str], dict[str, Any]]:
    mapping: dict[tuple[int, str], str] = {}
    digests: dict[str, str] = {}
    total_bytes = 0
    for ref in refs:
        content = ref.local_path.read_bytes()
        total_bytes += len(content)
        digest = hashlib.sha256(content).hexdigest()
        if digest not in digests:
            extension, _ = image_type(ref.local_path)
            filename = f"figure-{digest[:20]}{extension}"
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / filename).write_bytes(content)
            digests[digest] = filename
        mapping[(ref.page, ref.source)] = digests[digest]
    return mapping, {
        "references": len(refs),
        "unique": len(digests),
        "source_bytes": total_bytes,
    }


def render_cover(pdf_path: Path, destination: Path) -> str:
    with fitz.open(pdf_path) as document:
        page = document[0]
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
        png = pixmap.tobytes("png")
    with Image.open(BytesIO(png)) as image:
        rgb = image.convert("RGB")
        rgb.thumbnail((1800, 2600), Image.Resampling.LANCZOS)
        destination.parent.mkdir(parents=True, exist_ok=True)
        rgb.save(destination, "JPEG", quality=90, optimize=True)
    return destination.name


def _mathml(tex: str, *, display: bool) -> str:
    source = html.unescape(tex.strip())
    converted = latex_to_mathml(source, display="block" if display else "inline")
    converted = converted.replace(' columnspacing=""', "")
    label = html.escape(source, quote=True)
    return converted.replace("<math ", f'<math alttext="{label}" ', 1)


def _plausible_inline_math(body: str) -> bool:
    source = body.strip()
    if not source or "\n" in source or "$" in source or len(source) > 600:
        return False
    if re.fullmatch(r"[\d,.]+", source):
        return False
    if re.search(r"\\[A-Za-z]+|[_^{}]|[=+*/<>≤≥≈∂∑√]", source):
        return True
    if re.fullmatch(r"-?\d+(?:\.\d+)?", source):
        return source.startswith("-")
    if re.fullmatch(r"[A-Za-z](?:\s*[(),']\s*[A-Za-z0-9]+)*", source):
        return True
    return False


def protect_math(source: str) -> tuple[str, dict[str, str], Counter[str]]:
    replacements: dict[str, str] = {}
    stats: Counter[str] = Counter()

    def display_replacement(match: re.Match[str]) -> str:
        token = f"MATHDISPLAYTOKEN{len(replacements):08d}"
        try:
            replacements[token] = _mathml(match.group(1), display=True)
            stats["display_math"] += 1
        except Exception:
            replacements[token] = (
                '<pre class="math-fallback">' + html.escape(match.group(1).strip()) + "</pre>"
            )
            stats["math_fallbacks"] += 1
        return f"\n\n{token}\n\n"

    protected = re.sub(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", display_replacement, source, flags=re.S)
    output: list[str] = []
    cursor = 0
    while cursor < len(protected):
        if protected[cursor] != "$" or (cursor > 0 and protected[cursor - 1] == "\\"):
            output.append(protected[cursor])
            cursor += 1
            continue
        closing = protected.find("$", cursor + 1)
        while closing >= 0 and protected[closing - 1] == "\\":
            closing = protected.find("$", closing + 1)
        body = protected[cursor + 1 : closing] if closing >= 0 else ""
        if closing >= 0 and _plausible_inline_math(body):
            token = f"MATHINLINETOKEN{len(replacements):08d}"
            try:
                replacements[token] = _mathml(body, display=False)
                stats["inline_math"] += 1
            except Exception:
                replacements[token] = (
                    '<code class="math-fallback">' + html.escape(body.strip()) + "</code>"
                )
                stats["math_fallbacks"] += 1
            output.append(token)
            cursor = closing + 1
        else:
            output.append("$")
            cursor += 1
    return "".join(output), replacements, stats


def sanitize_fragment(soup: BeautifulSoup) -> None:
    dangerous_tags = {
        "applet",
        "audio",
        "base",
        "button",
        "embed",
        "form",
        "frame",
        "frameset",
        "iframe",
        "input",
        "link",
        "meta",
        "object",
        "script",
        "source",
        "style",
        "textarea",
        "video",
    }
    for tag in list(soup.find_all(dangerous_tags)):
        tag.decompose()
    for tag in soup.find_all(True):
        for attribute in list(tag.attrs):
            name = attribute.lower()
            if name.startswith("on") or name in {"style", "srcset", "formaction", "poster"}:
                del tag.attrs[attribute]
                continue
            if name not in {"href", "src", "xlink:href"}:
                continue
            value = str(tag.attrs.get(attribute) or "").strip()
            has_scheme = bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value))
            if name == "href":
                if has_scheme and not value.lower().startswith(("http://", "https://", "mailto:")):
                    del tag.attrs[attribute]
            elif has_scheme or value.startswith("//"):
                del tag.attrs[attribute]


def markdown_to_xhtml(source: str) -> tuple[str, Counter[str]]:
    source = unicodedata.normalize("NFKC", source).replace("___", "")
    protected, replacements, stats = protect_math(source)
    fragment = markdown.markdown(
        protected,
        extensions=["extra", "sane_lists"],
        output_format="xhtml",
    )
    for token, replacement in replacements.items():
        fragment = fragment.replace(token, replacement)
    soup = BeautifulSoup(fragment, "html.parser")
    for table in soup.find_all("table"):
        table.attrs.pop("border", None)
        table["class"] = sorted(set(table.get("class", [])) | {"data-table"})
        wrapper = soup.new_tag("div")
        wrapper["class"] = ["table-wrap"]
        table.wrap(wrapper)
        stats["tables"] += 1
    for image in soup.find_all("img"):
        width = image.attrs.pop("width", None)
        style = str(image.get("style", "")).strip(";")
        if width and str(width).endswith("%"):
            style = f"{style};width:{width};max-width:100%;height:auto".strip(";")
        if style:
            image["style"] = style
        image["loading"] = "lazy"
    for paragraph in soup.find_all("p"):
        if paragraph.find("math", attrs={"display": "block"}) and not paragraph.get_text(strip=True):
            paragraph.name = "div"
            paragraph["class"] = ["equation"]
    sanitize_fragment(soup)
    return str(soup), stats


def replace_image_sources(
    page_number: int,
    source: str,
    mapping: dict[tuple[int, str], str],
    alt_by_key: dict[tuple[int, str], str],
) -> str:
    def html_replacement(match: re.Match[str]) -> str:
        tag = match.group(0)
        original = match.group(1)
        filename = mapping.get((page_number, original))
        if not filename:
            raise RuntimeError(f"No packaged image for page {page_number}: {original}")
        tag = tag.replace(original, f"../images/{filename}")
        alt = html.escape(alt_by_key.get((page_number, original), "Illustration"), quote=True)
        if re.search(r"\balt=[\"\']", tag, flags=re.I):
            tag = re.sub(r"\balt=[\"\'][^\"\']*[\"\']", f'alt="{alt}"', tag, flags=re.I)
        else:
            tag = re.sub(r"\s*/?>$", f' alt="{alt}" />', tag)
        return tag

    source = re.sub(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>', html_replacement, source, flags=re.I)

    def markdown_replacement(match: re.Match[str]) -> str:
        original = match.group(2)
        filename = mapping.get((page_number, original))
        if not filename:
            raise RuntimeError(f"No packaged image for page {page_number}: {original}")
        label = alt_by_key.get((page_number, original), match.group(1) or "Illustration")
        label = label.replace("\\", "\\\\").replace("]", "\\]")
        return f"![{label}](../images/{filename})"

    return re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", markdown_replacement, source)


def footnote_markdown(page: dict[str, Any], page_source: str) -> str:
    blocks = (page.get("prunedResult") or {}).get("parsing_res_list") or []
    existing = clean_text(page_source).casefold()
    notes: list[str] = []
    for block in blocks:
        if block.get("block_label") not in {"footnote", "vision_footnote"}:
            continue
        note = str(block.get("block_content") or "").strip()
        normalized = clean_text(note).casefold()
        if normalized and normalized not in existing:
            notes.append(note)
    return "\n\n".join(notes)


def page_fragment(
    page_number: int,
    page: dict[str, Any],
    mapping: dict[tuple[int, str], str],
    alt_by_key: dict[tuple[int, str], str],
) -> tuple[str, Counter[str]]:
    source = str((page.get("markdown") or {}).get("text") or "")
    source = replace_image_sources(page_number, source, mapping, alt_by_key)
    body, stats = markdown_to_xhtml(source)
    notes = footnote_markdown(page, source)
    if notes:
        note_body, note_stats = markdown_to_xhtml(notes)
        body += f'<aside class="page-footnotes" epub:type="footnotes">{note_body}</aside>'
        stats.update(note_stats)
        stats["footnote_blocks"] += 1
    pagebreak = (
        f'<span id="page-{page_number}" class="pagebreak" epub:type="pagebreak" '
        f'title="PDF page {page_number}" aria-label="PDF page {page_number}"></span>'
    )
    return (
        f'<section class="pdf-page" data-pdf-page="{page_number}">{pagebreak}{body}</section>',
        stats,
    )


def document_template(metadata: Metadata, title: str, body: str, body_class: str) -> str:
    language = html.escape(metadata.language, quote=True)
    return f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{language}" xml:lang="{language}">
  <head>
    <meta charset="utf-8" />
    <title>{html.escape(title)}</title>
    <link rel="stylesheet" type="text/css" href="../styles/book.css" />
  </head>
  <body class="{html.escape(body_class, quote=True)}">
{body}
  </body>
</html>
'''


def cover_document(metadata: Metadata, cover_filename: str) -> str:
    body = f'''    <section class="cover" epub:type="cover">
      <span id="page-1" class="pagebreak" epub:type="pagebreak" title="PDF page 1" aria-label="PDF page 1"></span>
      <img src="../images/{html.escape(cover_filename)}" alt="Cover of {html.escape(metadata.title, quote=True)}" />
    </section>'''
    return document_template(metadata, "Cover", body, "cover-page")


def toc_href(entry: TocEntry, sections: list[Section]) -> str:
    if entry.page <= 1:
        return "cover.xhtml#page-1"
    section = section_for_page(sections, entry.page)
    if not section:
        return "cover.xhtml#page-1"
    return f"{section.filename}#page-{entry.page}"


def nav_list(entries: list[TocEntry], sections: list[Section]) -> str:
    lines = ["<ol>"]
    for entry in entries:
        lines.append(
            f'<li><a href="{html.escape(toc_href(entry, sections), quote=True)}">'
            f"{html.escape(entry.title)}</a>"
        )
        if entry.children:
            lines.append(nav_list(entry.children, sections))
        lines.append("</li>")
    lines.append("</ol>")
    return "\n".join(lines)


def nav_document(
    metadata: Metadata, entries: list[TocEntry], sections: list[Section]
) -> str:
    tree = build_toc_tree(entries)
    if not tree:
        tree = [TocEntry(1, section.title, section.start_page) for section in sections]
    beginning = f"{sections[0].filename}#page-{sections[0].start_page}" if sections else "cover.xhtml"
    body = f'''    <nav epub:type="toc" id="toc">
      <h1>Contents</h1>
      {nav_list(tree, sections)}
    </nav>
    <nav epub:type="landmarks" hidden="hidden">
      <h2>Landmarks</h2>
      <ol>
        <li><a epub:type="cover" href="cover.xhtml">Cover</a></li>
        <li><a epub:type="toc" href="nav.xhtml#toc">Table of Contents</a></li>
        <li><a epub:type="bodymatter" href="{html.escape(beginning, quote=True)}">Beginning of Content</a></li>
      </ol>
    </nav>'''
    return document_template(metadata, "Contents", body, "navigation")


def ncx_document(
    metadata: Metadata, entries: list[TocEntry], sections: list[Section]
) -> str:
    tree = build_toc_tree(entries)
    if not tree:
        tree = [TocEntry(1, section.title, section.start_page) for section in sections]
    counter = 0
    play_order_by_target: dict[str, int] = {}

    def render(nodes: list[TocEntry]) -> str:
        nonlocal counter
        lines: list[str] = []
        for node in nodes:
            counter += 1
            current = counter
            target = toc_href(node, sections)
            play_order = play_order_by_target.setdefault(target, len(play_order_by_target) + 1)
            lines.append(f'<navPoint id="nav-{current}" playOrder="{play_order}">')
            lines.append(f"<navLabel><text>{html.escape(node.title)}</text></navLabel>")
            lines.append(f'<content src="text/{html.escape(target, quote=True)}" />')
            if node.children:
                lines.append(render(node.children))
            lines.append("</navPoint>")
        return "\n".join(lines)

    return f'''<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="{html.escape(metadata.identifier, quote=True)}" /></head>
  <docTitle><text>{html.escape(metadata.title)}</text></docTitle>
  <docAuthor><text>{html.escape(metadata.author)}</text></docAuthor>
  <navMap>{render(tree)}</navMap>
</ncx>
'''


CSS = r'''
@namespace epub "http://www.idpf.org/2007/ops";

body {
  margin: 0 5%;
  font-family: serif;
  line-height: 1.5;
  text-align: left;
  orphans: 2;
  widows: 2;
}

p { margin: 0.55em 0; text-indent: 1.25em; }
h1, h2, h3, h4, h5, h6 {
  font-family: sans-serif;
  line-height: 1.25;
  break-after: avoid;
  page-break-after: avoid;
}
h1 { font-size: 1.75em; margin: 1.5em 0 0.8em; }
h2 { font-size: 1.45em; margin: 1.3em 0 0.65em; }
h3 { font-size: 1.2em; margin: 1.15em 0 0.55em; }
h4, h5, h6 { font-size: 1.05em; margin: 1em 0 0.5em; }

.pdf-page { position: relative; }
.pagebreak { display: block; height: 0; line-height: 0; }
img {
  display: block;
  max-width: 100%;
  height: auto;
  margin: 0.8em auto;
  break-inside: avoid;
  page-break-inside: avoid;
}
.cover-page, .cover { margin: 0; padding: 0; text-align: center; }
.cover img { max-height: 100vh; margin: 0 auto; }
.equation, math[display="block"] {
  display: block;
  margin: 0.8em auto;
  overflow-x: auto;
  text-align: center;
  break-inside: avoid;
}
math[display="inline"] { display: inline math; white-space: nowrap; }
.math-fallback { font-family: monospace; white-space: pre-wrap; }
.table-wrap { width: 100%; margin: 1em 0; overflow-x: auto; break-inside: avoid; }
.data-table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
.data-table th, .data-table td { padding: 0.32em 0.42em; border: 1px solid #888; vertical-align: top; }
.page-footnotes { margin: 1.2em 0 0.8em; padding-top: 0.5em; border-top: 1px solid #888; font-size: 0.82em; }
.page-footnotes p { text-indent: 0; margin: 0.35em 0; }
.navigation ol { padding-left: 1.35em; }
.navigation li { margin: 0.3em 0; }
a { color: inherit; text-decoration: underline; }
'''


def media_type(path: Path) -> str:
    return {
        ".xhtml": "application/xhtml+xml",
        ".css": "text/css",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".ncx": "application/x-dtbncx+xml",
    }.get(path.suffix.lower(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")


def opf_document(
    metadata: Metadata,
    sections: list[Section],
    image_dir: Path,
    cover_filename: str,
    modified: str,
) -> str:
    manifest = [
        '<item id="nav" href="text/nav.xhtml" media-type="application/xhtml+xml" properties="nav" />',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml" />',
        '<item id="css" href="styles/book.css" media-type="text/css" />',
        f'<item id="cover" href="text/cover.xhtml" media-type="application/xhtml+xml" />',
        f'<item id="cover-image" href="images/{html.escape(cover_filename, quote=True)}" media-type="image/jpeg" properties="cover-image" />',
    ]
    for index, section in enumerate(sections, 1):
        properties = f' properties="{" ".join(sorted(section.properties))}"' if section.properties else ""
        manifest.append(
            f'<item id="section-{index:04d}" href="text/{html.escape(section.filename, quote=True)}" '
            f'media-type="application/xhtml+xml"{properties} />'
        )
    for index, image_path in enumerate(sorted(image_dir.iterdir()), 1):
        if image_path.name == cover_filename:
            continue
        manifest.append(
            f'<item id="image-{index:04d}" href="images/{html.escape(image_path.name, quote=True)}" '
            f'media-type="{media_type(image_path)}" />'
        )
    spine = ['<itemref idref="cover" linear="yes" />']
    spine.extend(f'<itemref idref="section-{index:04d}" />' for index in range(1, len(sections) + 1))
    spine.append('<itemref idref="nav" linear="no" />')
    publisher_xml = f"<dc:publisher>{html.escape(metadata.publisher)}</dc:publisher>" if metadata.publisher else ""
    return f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id" xml:lang="{html.escape(metadata.language, quote=True)}" prefix="dcterms: http://purl.org/dc/terms/">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">{html.escape(metadata.identifier)}</dc:identifier>
    <dc:title>{html.escape(metadata.title)}</dc:title>
    <dc:creator>{html.escape(metadata.author)}</dc:creator>
    <dc:language>{html.escape(metadata.language)}</dc:language>
    {publisher_xml}
    <dc:source>{html.escape(metadata.source)}</dc:source>
    <meta property="dcterms:modified">{modified}</meta>
    <meta name="cover" content="cover-image" />
  </metadata>
  <manifest>{''.join(manifest)}</manifest>
  <spine toc="ncx">{''.join(spine)}</spine>
</package>
'''


def write_container(build_root: Path) -> None:
    write_text(build_root / "mimetype", "application/epub+zip", encoding="ascii")
    write_text(
        build_root / "META-INF" / "container.xml",
        '''<?xml version="1.0" encoding="utf-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml" />
  </rootfiles>
</container>
''',
    )


def validate_xml(build_root: Path) -> list[str]:
    errors: list[str] = []
    candidates = list(build_root.rglob("*.xhtml")) + list(build_root.rglob("*.xml"))
    candidates += [build_root / "OEBPS" / "content.opf", build_root / "OEBPS" / "toc.ncx"]
    for path in candidates:
        try:
            etree.parse(str(path))
        except Exception as exc:
            errors.append(f"{path.relative_to(build_root)}: {exc}")
    return errors


def validate_links(build_root: Path) -> list[str]:
    errors: list[str] = []
    parser = etree.XMLParser(recover=False)
    text_dir = build_root / "OEBPS" / "text"
    for path in text_dir.glob("*.xhtml"):
        tree = etree.parse(str(path), parser)
        local_ids = set(tree.xpath("//@id"))
        for href in tree.xpath("//@href"):
            if href.startswith(("http://", "https://", "mailto:")):
                continue
            target, _, fragment = href.partition("#")
            target_path = (path.parent / target).resolve() if target else path.resolve()
            if build_root.resolve() != target_path and build_root.resolve() not in target_path.parents:
                errors.append(f"{path.name}: link escapes EPUB root: {href}")
                continue
            if not target_path.exists():
                errors.append(f"{path.name}: missing link target {href}")
                continue
            if fragment:
                target_tree = tree if target_path == path.resolve() else etree.parse(str(target_path), parser)
                target_ids = local_ids if target_path == path.resolve() else set(target_tree.xpath("//@id"))
                if fragment not in target_ids:
                    errors.append(f"{path.name}: missing fragment {href}")
        for source in tree.xpath("//@src"):
            if source.startswith(("http://", "https://", "data:")):
                errors.append(f"{path.name}: external resource remains: {source[:160]}")
                continue
            source_path = (path.parent / source).resolve()
            if build_root.resolve() != source_path and build_root.resolve() not in source_path.parents:
                errors.append(f"{path.name}: resource escapes EPUB root: {source}")
                continue
            if not source_path.exists():
                errors.append(f"{path.name}: missing resource {source}")
    return errors


def package_epub(build_root: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w") as archive:
        archive.write(build_root / "mimetype", "mimetype", compress_type=zipfile.ZIP_STORED)
        for path in sorted(build_root.rglob("*")):
            if path.is_file() and path.name != "mimetype":
                archive.write(
                    path,
                    path.relative_to(build_root).as_posix(),
                    compress_type=zipfile.ZIP_DEFLATED,
                )


def run_epubcheck(epub_path: Path, log_path: Path, *, skip: bool = False) -> dict[str, Any]:
    if skip:
        return {"status": "skipped", "errors": 0, "warnings": 0}
    command = epubcheck_base_command()
    if not command:
        message = "EPUBCheck is unavailable. Install epubcheck or set EPUBCHECK_JAR and install Java.\n"
        write_text(log_path, message)
        return {"status": "unavailable", "errors": 0, "warnings": 0, "log": str(log_path)}
    result = subprocess.run(
        [*command, str(epub_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    write_text(log_path, output)
    errors = len(re.findall(r"\bERROR\b", output))
    warnings = len(re.findall(r"\bWARNING\b", output))
    status = "passed" if result.returncode == 0 else "failed"
    return {
        "status": status,
        "return_code": result.returncode,
        "errors": errors,
        "warnings": warnings,
        "log": str(log_path),
    }


def build_epub(
    pdf_path: Path,
    pages_path: Path,
    *,
    ocr_dir: Path,
    output_path: Path,
    work_dir: Path,
    title: str | None = None,
    author: str | None = None,
    language: str | None = None,
    publisher: str | None = None,
    identifier: str | None = None,
    max_pages_per_section: int = 30,
    skip_epubcheck: bool = False,
) -> BuildResult:
    pages = json.loads(pages_path.read_text(encoding="utf-8"))
    if not isinstance(pages, list) or not all(isinstance(page, dict) for page in pages):
        raise RuntimeError("Normalized OCR pages.json must contain a list of page objects")
    for page_number, page in enumerate(pages, 1):
        markdown_payload = page.get("markdown")
        if not isinstance(markdown_payload, dict) or not isinstance(markdown_payload.get("text"), str):
            raise RuntimeError(f"OCR page {page_number} has no string markdown.text field")
    with fitz.open(pdf_path) as document:
        pdf_page_count = document.page_count
    if pdf_page_count != len(pages):
        raise RuntimeError(
            f"Page-count mismatch: PDF has {pdf_page_count} pages; OCR has {len(pages)} pages"
        )
    if not pages:
        raise RuntimeError("Cannot build an EPUB from an empty PDF")

    metadata = infer_metadata(
        pdf_path,
        pages,
        title=title,
        author=author,
        language=language,
        publisher=publisher,
        identifier=identifier,
    )
    entries, toc_source, toc_detail = choose_outline(pdf_path, pages)
    sections = make_sections(entries, len(pages), max_pages=max(1, max_pages_per_section))
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.stem}-",
        suffix=".epub",
        dir=output_path.parent,
    )
    os.close(descriptor)
    temporary_epub = Path(temporary_name)
    report_path = work_dir / "build_report.json"
    modified = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    try:
        with tempfile.TemporaryDirectory(prefix="epub-build-", dir=work_dir) as build_name:
            build_root = Path(build_name)
            text_dir = build_root / "OEBPS" / "text"
            image_dir = build_root / "OEBPS" / "images"
            style_dir = build_root / "OEBPS" / "styles"
            for directory in (build_root / "META-INF", text_dir, image_dir, style_dir):
                directory.mkdir(parents=True, exist_ok=True)

            refs = collect_images(pages, ocr_dir)
            image_mapping, image_stats = copy_images(refs, image_dir)
            alt_by_key = {(ref.page, ref.source): ref.alt for ref in refs}
            cover_filename = render_cover(pdf_path, image_dir / "cover.jpg")
            write_text(text_dir / "cover.xhtml", cover_document(metadata, cover_filename))

            totals: Counter[str] = Counter()
            for section in sections:
                fragments: list[str] = []
                for page_number in range(section.start_page, section.end_page + 1):
                    fragment, page_stats = page_fragment(
                        page_number,
                        pages[page_number - 1],
                        image_mapping,
                        alt_by_key,
                    )
                    fragments.append(fragment)
                    totals.update(page_stats)
                content = document_template(
                    metadata,
                    section.title,
                    "\n".join(fragments),
                    "book-section",
                )
                if "<math " in content:
                    section.properties.add("mathml")
                write_text(text_dir / section.filename, content)

            write_text(text_dir / "nav.xhtml", nav_document(metadata, entries, sections))
            write_text(build_root / "OEBPS" / "toc.ncx", ncx_document(metadata, entries, sections))
            write_text(style_dir / "book.css", CSS.strip() + "\n")
            write_text(
                build_root / "OEBPS" / "content.opf",
                opf_document(metadata, sections, image_dir, cover_filename, modified),
            )
            write_container(build_root)

            xml_errors = validate_xml(build_root)
            link_errors = validate_links(build_root) if not xml_errors else []
            if xml_errors or link_errors:
                raise RuntimeError(
                    "EPUB internal validation failed:\n" + "\n".join(xml_errors + link_errors)
                )
            package_epub(build_root, temporary_epub)

        epubcheck = run_epubcheck(
            temporary_epub,
            work_dir / "epubcheck.log",
            skip=skip_epubcheck,
        )
        report = {
            "schema": 2,
            "output": str(output_path.resolve()),
            "output_replaced": False,
            "title": metadata.title,
            "author": metadata.author,
            "language": metadata.language,
            "identifier": metadata.identifier,
            "source_pdf": str(pdf_path.resolve()),
            "source_pages_json": str(pages_path.resolve()),
            "pdf_pages": pdf_page_count,
            "ocr_pages": len(pages),
            "sections": len(sections),
            "toc_entries": len(entries),
            "toc_source": toc_source,
            "toc_page_offset": toc_detail["page_offset"],
            "toc_entries_verified": f"{toc_detail['verified']}/{toc_detail['total']}",
            "images": image_stats,
            "math": {
                "display": totals["display_math"],
                "inline": totals["inline_math"],
                "fallbacks": totals["math_fallbacks"],
            },
            "tables": totals["tables"],
            "footnote_pages": totals["footnote_blocks"],
            "internal_xml_errors": xml_errors,
            "internal_link_errors": link_errors,
            "epubcheck": epubcheck,
            "epub_bytes": temporary_epub.stat().st_size,
            "generated_at": modified,
        }
        if epubcheck["status"] not in {"passed", "skipped"}:
            write_json(report_path, report)
            if epubcheck["status"] == "failed":
                raise RuntimeError(f"EPUBCheck failed; see {epubcheck['log']}")
            raise RuntimeError(
                "EPUBCheck is required. Install it, or use --skip-epubcheck only with user approval."
            )

        temporary_epub.replace(output_path)
        report["output_replaced"] = True
        report["epub_bytes"] = output_path.stat().st_size
        write_json(report_path, report)
        return BuildResult(output_path, report_path, report)
    finally:
        temporary_epub.unlink(missing_ok=True)


__all__ = ["BuildResult", "build_epub"]
