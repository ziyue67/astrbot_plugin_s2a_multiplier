#!/usr/bin/env python3
"""Build the installable AstrBot plugin archive for a release.

Why the archive needs a single top-level directory: AstrBot's CLI installer
takes ``Path(namelist[0]).parts[0]`` as the plugin directory and moves it into
``data/plugins/``. With a flat archive that resolves to a single file, and the
install breaks. The WebUI installer tolerates a flat archive, so a flat
archive means "works for some users and not others". Always nest.

Usage:
    python .github/scripts/build_plugin_zip.py --tag v1.2.0 --output-dir dist
"""

from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Anything here is a development artefact and has no business in an install
# package. Matched against the first path segment and against any directory
# segment further down.
EXCLUDED_DIRS = {
    ".github",
    ".git",
    ".workbuddy",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "tests",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
}

EXCLUDED_NAMES = {".DS_Store", ".gitignore"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo")


def read_metadata_field(text: str, key: str) -> str:
    """Read a scalar field from metadata.yaml without a YAML dependency."""

    match = re.search(rf"^{key}:\s*(.+?)\s*$", text, re.MULTILINE)
    if match is None:
        raise SystemExit(f"metadata.yaml 缺少 {key} 字段")
    return match.group(1).strip().strip("\"'")


def collect_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in relative.parts[:-1]):
            continue
        if relative.parts[0] in EXCLUDED_DIRS:
            continue
        if path.name in EXCLUDED_NAMES or path.name.endswith(EXCLUDED_SUFFIXES):
            continue
        files.append(relative)
    return files


def build(repo_root: Path, tag: str, output_dir: Path) -> tuple[str, str, Path]:
    metadata_path = repo_root / "metadata.yaml"
    if not metadata_path.is_file():
        raise SystemExit(f"找不到 metadata.yaml: {metadata_path}")

    metadata_text = metadata_path.read_text(encoding="utf-8")
    plugin_name = read_metadata_field(metadata_text, "name")
    version = read_metadata_field(metadata_text, "version")

    files = collect_files(repo_root)
    if Path("metadata.yaml") not in files or Path("main.py") not in files:
        raise SystemExit("打包内容里缺少 metadata.yaml 或 main.py")

    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"{plugin_name}-{tag}.zip"

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        # The explicit directory entry guarantees namelist[0] is the plugin
        # directory, which is what AstrBot's CLI installer keys off.
        archive.writestr(f"{plugin_name}/", b"")
        for relative in files:
            archive.write(repo_root / relative, f"{plugin_name}/{relative.as_posix()}")

    print(f"plugin_name={plugin_name}")
    print(f"version={version}")
    print(f"files={len(files)}")
    print(f"archive={archive_path}")
    return plugin_name, version, archive_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="发布用的 tag，例如 v1.0.0")
    parser.add_argument("--output-dir", default="dist", help="输出目录，默认 dist")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument(
        "--github-output",
        default="",
        help="写到这里（GitHub Actions 的 $GITHUB_OUTPUT），供后续步骤读取",
    )
    args = parser.parse_args()

    plugin_name, version, archive_path = build(
        Path(args.repo_root).resolve(),
        args.tag,
        Path(args.output_dir).resolve(),
    )

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"plugin_name={plugin_name}\n")
            handle.write(f"version={version}\n")
            handle.write(f"archive={archive_path}\n")
            handle.write(f"archive_name={archive_path.name}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
