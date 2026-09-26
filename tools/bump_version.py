#!/usr/bin/env python3

import argparse
import re
import tomllib
from pathlib import Path


VERSION_PATTERN = re.compile(
	r'(?ms)(^\[project\]\s*$.*?^version\s*=\s*")([0-9]+\.[0-9]+\.[0-9]+)("\s*$)'
)


def bump_patch(path: Path) -> str:
	contents = path.read_text(encoding="utf-8")
	with path.open("rb") as pyproject_file:
		current = tomllib.load(pyproject_file)["project"]["version"]

	match = VERSION_PATTERN.search(contents)
	if not match or match.group(2) != current:
		raise ValueError(f"could not uniquely locate project.version {current} in {path}")

	major, minor, patch = (int(part) for part in current.split("."))
	updated = f"{major}.{minor}.{patch + 1}"
	path.write_text(
		contents[: match.start(2)] + updated + contents[match.end(2) :],
		encoding="utf-8",
	)
	return updated


def main() -> None:
	parser = argparse.ArgumentParser(description="Increment AODv2's patch version")
	parser.add_argument("path", nargs="?", type=Path, default=Path("pyproject.toml"))
	args = parser.parse_args()
	print(bump_patch(args.path))


if __name__ == "__main__":
	main()