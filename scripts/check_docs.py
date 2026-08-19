"""Check that local links in repository Markdown files resolve.

External URLs are intentionally left to the reader's network environment. This
check covers the failures the repository can decide hermetically: renamed
documents, moved runbooks, and missing images.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\((?P<target>[^)\n]+)\)")
EXTERNAL_SCHEMES = ("http://", "https://", "mailto:")


def repository_markdown() -> tuple[Path, ...]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to enumerate tracked documentation")
    result = subprocess.run(  # noqa: S603 - executable resolved locally; arguments are fixed
        [
            git,
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "*.md",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths = (ROOT / name.decode() for name in result.stdout.split(b"\0") if name)
    return tuple(path for path in paths if path.is_file())


def local_target(raw: str) -> str | None:
    value = raw.strip()
    if value.startswith("<"):
        closing = value.find(">")
        if closing == -1:
            return value[1:]
        value = value[1:closing]
    else:
        value = value.split(maxsplit=1)[0]
    if not value or value.startswith(("#", *EXTERNAL_SCHEMES)):
        return None
    return unquote(value.split("#", 1)[0].split("?", 1)[0])


def broken_links(files: tuple[Path, ...]) -> list[str]:
    failures: list[str] = []
    for document in files:
        text = document.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK.finditer(text):
            target = local_target(match.group("target"))
            if target is None:
                continue
            resolved = (document.parent / target).resolve()
            if not resolved.exists():
                location = text.count("\n", 0, match.start()) + 1
                failures.append(
                    f"{document.relative_to(ROOT)}:{location}: "
                    f"local link does not exist: {target}"
                )
    return failures


def main() -> int:
    failures = broken_links(repository_markdown())
    if failures:
        sys.stderr.write("\n".join(failures) + "\n")
        return 1
    sys.stdout.write("repository Markdown links resolve\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
