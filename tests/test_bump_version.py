import tomllib

import pytest

from tools.bump_version import bump_patch


def test_bump_patch_updates_project_version_only(tmp_path):
	pyproject = tmp_path / "pyproject.toml"
	pyproject.write_text(
		'[project]\nname = "aodv2"\nversion = "2.7.9"\n\n'
		'[tool.example]\nversion = "unchanged"\n',
		encoding="utf-8",
	)

	assert bump_patch(pyproject) == "2.7.10"

	with pyproject.open("rb") as pyproject_file:
		metadata = tomllib.load(pyproject_file)
	assert metadata["project"]["version"] == "2.7.10"
	assert metadata["tool"]["example"]["version"] == "unchanged"


def test_bump_patch_rejects_non_semantic_version(tmp_path):
	pyproject = tmp_path / "pyproject.toml"
	pyproject.write_text(
		'[project]\nname = "aodv2"\nversion = "2.7"\n', encoding="utf-8"
	)

	with pytest.raises(ValueError, match="could not uniquely locate"):
		bump_patch(pyproject)