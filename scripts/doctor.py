#!/usr/bin/env python3
"""Check the runtime needed by the PDF-to-EPUB skill using only stdlib."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional


REQUIRED_MODULES = {
    "bs4": "beautifulsoup4",
    "fitz": "PyMuPDF",
    "latex2mathml": "latex2mathml",
    "lxml": "lxml",
    "markdown": "Markdown",
    "PIL": "Pillow",
    "requests": "requests",
}


def epubcheck_base_command() -> Optional[list[str]]:
    executable = shutil.which("epubcheck")
    if executable:
        return [executable]

    jar_value = os.environ.get("EPUBCHECK_JAR", "").strip()
    java = shutil.which("java")
    if jar_value and java:
        jar = Path(jar_value).expanduser()
        if jar.is_file():
            return [java, "-jar", str(jar.resolve())]
    return None


def main() -> int:
    missing = [
        package
        for module, package in REQUIRED_MODULES.items()
        if importlib.util.find_spec(module) is None
    ]
    python_ok = sys.version_info >= (3, 10)
    epubcheck = epubcheck_base_command()
    problems = []
    if not python_ok:
        problems.append("Python 3.10 or newer is required")
    if missing:
        problems.append("Install requirements.txt with this Python interpreter")
    if not epubcheck:
        problems.append("Install epubcheck or set EPUBCHECK_JAR and install Java")

    payload = {
        "ok": not problems,
        "python": {
            "executable": sys.executable,
            "version": ".".join(str(part) for part in sys.version_info[:3]),
            "supported": python_ok,
        },
        "missing_packages": missing,
        "epubcheck": {
            "available": bool(epubcheck),
            "command": epubcheck,
        },
        "problems": problems,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
