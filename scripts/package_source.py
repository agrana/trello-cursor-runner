#!/usr/bin/env python3
"""Package an explicit set of portable source files, never the whole checkout."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent.parent
PREFIX = "trello-cursor-runner"
FILES = (
    ".github/workflows/ci.yml",
    ".gitignore",
    "README.md",
    "config.example.json",
    "runner.py",
    "pull_requests.py",
    "tests/test_runner.py",
    "tests/test_pull_requests.py",
    "docs/architecture.md",
    "docs/sharing.md",
    "skills/runner-execution/SKILL.md",
    "grafana/trello-cursor-runner.json",
    "systemd/service.env.example",
    "systemd/trello-cursor-runner.service",
    "systemd/trello-cursor-runner.timer",
    "scripts/package_source.py",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / f"{PREFIX}.zip")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    sources = {}
    for name in sorted(FILES):
        source = ROOT / name
        if not source.is_file() or source.is_symlink() or source.resolve() != source:
            parser.error(f"Missing or non-regular package source: {name}")
        sources[name] = source.read_bytes()
    if output in [(ROOT / name).resolve() for name in FILES]:
        parser.error("Output must not overwrite a package source")
    if output.suffix != ".zip":
        parser.error("Output must use the .zip extension")
    manifest = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sources.items())
    sources["SHA256SUMS"] = manifest.encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sources.items():
            info = zipfile.ZipInfo(f"{PREFIX}/{name}", date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_suffix(output.suffix + ".sha256")
    checksum.write_text(f"{digest}  {output.name}\n")
    print(f"Packaged {len(FILES)} source files: {output}")
    print(f"Checksum: {checksum}")


if __name__ == "__main__":
    main()
