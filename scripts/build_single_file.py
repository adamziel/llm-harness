#!/usr/bin/env python3
"""Build the self-contained single-file harness release asset."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import shutil
import stat
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "single-file"
DIST = ROOT / "dist"
OUT = DIST / "harness"
DEPENDENCIES = (("turso", "pyturso>=0.6.1"),)
BOOTSTRAP_MARKER = b"\n# --- llm-harness payload zip ---\n"

BOOTSTRAP = r'''#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import base64
import io
import os
import shutil
import sys
import zipfile

MARKER = b"\n# --- llm-harness payload zip ---\n"


def _cache_root() -> str:
    root = os.environ.get("LLM_HARNESS_EXTRACT_DIR")
    if root:
        return root
    return os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "llm-harness", "single-file")


def _extract_payload() -> str:
    with open(sys.argv[0], "rb") as fh:
        executable = fh.read()
    marker_index = executable.rfind(MARKER)
    if marker_index < 0:
        raise RuntimeError("llm-harness payload marker is missing")
    payload_block = executable[marker_index + len(MARKER):]
    payload_start = payload_block.find(b'"""')
    payload_end = payload_block.rfind(b'"""')
    if payload_start < 0 or payload_end <= payload_start:
        raise RuntimeError("llm-harness payload block is malformed")
    payload = base64.b64decode(payload_block[payload_start + 3:payload_end])
    digest = hashlib.sha256(payload).hexdigest()
    target = os.path.join(_cache_root(), digest[:16])
    sentinel = os.path.join(target, ".complete")
    if os.path.exists(sentinel):
        return target

    tmp = f"{target}.tmp.{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        archive.extractall(tmp)
    with open(os.path.join(tmp, ".complete"), "w") as fh:
        fh.write(digest)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.exists(target):
        shutil.rmtree(target)
    os.rename(tmp, target)
    return target


def main() -> int:
    sys.path.insert(0, _extract_payload())
    from llm_harness.cli import main as harness_main

    return int(harness_main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
'''


def main() -> int:
    """Package the harness and native Turso dependency into one executable file."""

    if BUILD.exists():
        shutil.rmtree(BUILD)
    DIST.mkdir(exist_ok=True)
    BUILD.mkdir(parents=True)
    for module, requirement in DEPENDENCIES:
        if not copy_installed_package(module, BUILD):
            install_dependency(requirement, BUILD)
    shutil.copytree(
        ROOT / "llm_harness",
        BUILD / "llm_harness",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    if OUT.exists():
        OUT.unlink()

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(BUILD.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                archive.write(path, path.relative_to(BUILD))
    digest = hashlib.sha256(payload.getvalue()).hexdigest()
    OUT.write_bytes(
        textwrap.dedent(BOOTSTRAP).encode()
        + f"\n# payload-sha256: {digest}\n".encode()
        + BOOTSTRAP_MARKER
        + b'PAYLOAD = """\n'
        + base64.b64encode(payload.getvalue())
        + b'\n"""\n'
    )
    OUT.chmod(OUT.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(OUT)
    return 0


def copy_installed_package(module: str, target: Path) -> bool:
    """Copy an installed dependency when available so local builds stay offline."""

    spec = importlib.util.find_spec(module)
    if not spec or not spec.submodule_search_locations:
        return False
    source = Path(next(iter(spec.submodule_search_locations)))
    shutil.copytree(source, target / module, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return True


def install_dependency(requirement: str, target: Path) -> None:
    """Install a binary dependency into the payload build directory."""

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-compile",
            "--only-binary",
            ":all:",
            "--target",
            str(target),
            requirement,
        ],
        cwd=ROOT,
        check=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
