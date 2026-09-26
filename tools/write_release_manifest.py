#!/usr/bin/env python3

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


DISTRO_MARKERS = ("jammy", "noble", "azl3", "el9", "el10", "sles15")


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


def distro_for(package: Path) -> str:
	matches = [marker for marker in DISTRO_MARKERS if f".{marker}." in package.name]
	if len(matches) != 1:
		raise ValueError(f"cannot determine one distro from {package.name}")
	return matches[0]


def main() -> None:
	parser = argparse.ArgumentParser(description="Write signed AODv2 release manifest")
	parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
	parser.add_argument("--package-dir", type=Path, required=True)
	parser.add_argument("--output", type=Path, required=True)
	args = parser.parse_args()

	root = args.root.resolve()
	packages = sorted(
		path for path in args.package_dir.iterdir() if path.suffix in (".deb", ".rpm")
	)
	if len(packages) != len(DISTRO_MARKERS):
		raise ValueError(f"expected {len(DISTRO_MARKERS)} packages, found {len(packages)}")

	manifest = {
		"packages": [
			{
				"distro": distro_for(package),
				"file": package.name,
				"sha256": sha256(package),
				"signed": True,
			}
			for package in packages
		],
		"sourceCommit": git_revision(root),
		"submoduleCommit": git_revision(root, "monitoring_tools"),
	}
	args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
	main()