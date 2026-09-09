"""Offer deployed service source without including runtime data or secrets."""

import tarfile
from functools import lru_cache
from io import BytesIO
from pathlib import Path


@lru_cache(maxsize=1)
def source_archive() -> bytes:
    root = Path(__file__).resolve().parent.parent
    files = [
        root / name
        for name in (
            "LICENSE",
            "NOTICE",
            "README.md",
            "pyproject.toml",
            "uv.lock",
            "Dockerfile",
            "compose.yaml",
            "template.yaml",
            ".env.example",
            ".gitignore",
            ".dockerignore",
        )
    ]
    for directory in ("app", "tests", "examples", "deploy"):
        files.extend(
            path
            for path in (root / directory).rglob("*")
            if path.is_file()
            and path.suffix in {".py", ".html", ".yaml", ".md", ".lambda"}
            and "__pycache__" not in path.parts
        )
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(files):
            if path.is_file() and not path.is_symlink():
                archive.add(
                    path, arcname=f"nude-detection-api/{path.relative_to(root)}", recursive=False
                )
    return buffer.getvalue()
