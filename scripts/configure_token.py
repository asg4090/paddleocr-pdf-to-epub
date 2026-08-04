#!/usr/bin/env python3
"""Store the PaddleOCR API token in a private per-user config file."""

from __future__ import annotations

import argparse
import getpass
import os
import stat
import sys
import tempfile
from pathlib import Path


TOKEN_ENV = "PADDLEOCR_API_TOKEN"


def token_file_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "paddleocr-pdf-to-epub" / "token"


def token_is_configured() -> bool:
    if os.environ.get(TOKEN_ENV, "").strip():
        return True
    path = token_file_path()
    return path.is_file() and path.stat().st_size > 0


def _write_private(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.parent.chmod(stat.S_IRWXU)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".token-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
        temporary.replace(path)
        if os.name != "nt":
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure the PaddleOCR API token securely.")
    parser.add_argument("--check", action="store_true", help="Check presence without reading the token.")
    parser.add_argument("--replace", action="store_true", help="Replace an existing saved token.")
    args = parser.parse_args()

    if args.check:
        print("configured" if token_is_configured() else "not configured")
        return 0 if token_is_configured() else 1

    token_path = token_file_path()
    if token_path.is_file() and not args.replace:
        print(f"Token is already configured at {token_path}. Use --replace to change it.")
        return 0

    token = os.environ.get(TOKEN_ENV, "").strip() or getpass.getpass("PaddleOCR API token: ").strip()
    if not token:
        print("No token supplied.", file=sys.stderr)
        return 2
    _write_private(token_path, token)
    print(f"Token saved to {token_path} with user-only permissions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
