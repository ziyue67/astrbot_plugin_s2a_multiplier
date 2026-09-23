#!/usr/bin/env python3
"""Validate a built plugin archive against AstrBot's installer expectations.

Mirrors the checks AstrBot performs, plus the extra constraint that only the
CLI installer imposes. Run it locally before uploading, and in CI before
attaching the archive to a release.

Usage:
    python .github/scripts/validate_plugin_zip.py dist/astrbot_plugin_sub2api_rate-v1.2.0.zip
"""

from __future__ import annotations

import argparse
import posixpath
import re
import zipfile
from pathlib import Path

# AstrBot looks for either of these at the archive root (or under the single
# top-level directory when there is one).
METADATA_FILENAMES = ("metadata.yaml", "metadata.yml")

REQUIRED_METADATA_FIELDS = ("name", "version", "desc")


def _fail(message: str) -> None:
    print(f"✗ {message}")
    raise SystemExit(1)


def _read_metadata_field(text: str, key: str) -> str | None:
    match = re.search(rf"^{key}:\s*(.+?)\s*$", text, re.MULTILINE)
    return match.group(1).strip().strip("\"'") if match else None


def validate(archive_path: Path) -> dict[str, str]:
    if not archive_path.is_file():
        _fail(f"文件不存在: {archive_path}")

    with zipfile.ZipFile(archive_path) as archive:
        bad = archive.testzip()
        if bad is not None:
            _fail(f"压缩包已损坏，第一个坏成员: {bad}")
        names = archive.namelist()

    if not names:
        _fail("压缩包是空的")

    # No absolute paths and no traversal.
    for name in names:
        normalized = posixpath.normpath(name)
        if normalized.startswith(("/", "../")) or "/../" in normalized:
            _fail(f"存在不安全的路径: {name}")

    # Single top-level directory.
    roots = {name.split("/", 1)[0] for name in names if name.strip("/")}
    if len(roots) != 1:
        _fail(f"顶层必须只有一个条目，实际有 {sorted(roots)}")
    root = roots.pop()

    if names[0] != f"{root}/":
        _fail(
            "namelist[0] 必须是顶层目录本身 "
            f"'（AstrBot CLI 安装器取 Path(namelist[0]).parts[0] 作为插件目录），实际是 {names[0]!r}"
        )

    # metadata.yaml must exist under that root.
    metadata_entry = None
    for candidate in METADATA_FILENAMES:
        if f"{root}/{candidate}" in names:
            metadata_entry = f"{root}/{candidate}"
            break
    if metadata_entry is None:
        _fail(f"顶层目录 {root}/ 下找不到 metadata.yaml")

    with zipfile.ZipFile(archive_path) as archive:
        raw = archive.read(metadata_entry)
    try:
        metadata_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _fail(f"{metadata_entry} 不是 UTF-8 编码")

    # metadata.yaml 的 name 应当与顶层目录一致，否则 AstrBot 装的目录名会和插件名对不上。
    declared_name = _read_metadata_field(metadata_text, "name")
    if declared_name and declared_name != root:
        _fail(f"metadata.yaml 的 name={declared_name!r} 与顶层目录 {root!r} 不一致")

    for field in REQUIRED_METADATA_FIELDS:
        if not _read_metadata_field(metadata_text, field):
            _fail(f"{metadata_entry} 缺少必填字段 {field}")

    version = _read_metadata_field(metadata_text, "version") or ""

    if f"{root}/main.py" not in names:
        _fail(f"顶层目录 {root}/ 下没有 main.py")

    print(f"✓ 顶层目录      : {root}/")
    print(f"✓ namelist[0]   : {names[0]!r}")
    print(f"✓ metadata      : {metadata_entry}")
    print(f"✓ 插件名 / 版本  : {declared_name} / {version}")
    print(f"✓ 成员数        : {len(names)}")
    print(f"✓ 压缩包        : {archive_path.name}")

    return {"plugin_name": declared_name or root, "version": version, "root": root}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", help="待校验的 zip 路径")
    args = parser.parse_args()

    validate(Path(args.archive).resolve())
    print("校验通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
