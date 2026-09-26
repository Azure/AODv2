#!/usr/bin/env python3

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def git_revision(root: Path, path: str = ".") -> str:
	return subprocess.check_output(
		["git", "-C", str(root / path), "rev-parse", "HEAD"], text=True
	).strip()


def sha256(path: Path) -> str:
	digest = hashlib.sha256()
	with path.open("rb") as package_file:
		for chunk in iter(lambda: package_file.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()


def main() -> None:
	parser = argparse.ArgumentParser(description="Write AODv2 package provenance")
	parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
	parser.add_argument("--distro", required=True)
	parser.add_argument("--format", choices=("deb", "rpm"), required=True)
	parser.add_argument("--package", type=Path, required=True)
	parser.add_argument("--output", type=Path, required=True)
	args = parser.parse_args()

	root = args.root.resolve()
	package = args.package.resolve()
	manifest = {
		"architecture": "amd64" if args.format == "deb" else "x86_64",
		"distro": args.distro,
		"format": args.format,
		"package": package.name,
		"sha256": sha256(package),
		"signed": False,
		"sourceCommit": git_revision(root),
		"submoduleCommit": git_revision(root, "monitoring_tools"),
	}
	args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
	main()