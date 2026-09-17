"""Render explicit package runtime policy; never install Python or dependencies."""

import argparse
import json
import re
import tomllib
from pathlib import Path


MODES = ("system", "external-venv")
MODULES = {"numpy": "numpy", "zstandard": "zstandard", "PyYAML": "yaml"}


def runtime_policy(source, mode):
    if mode not in MODES:
        raise ValueError(f"unknown AOD_RUNTIME_MODE: {mode!r}")
    project = tomllib.loads((source / "pyproject.toml").read_text())["project"]
    minimum = re.fullmatch(r">=(\d+)\.(\d+)", project["requires-python"])
    if minimum is None:
        raise ValueError("package validation requires an explicit Python >=major.minor floor")
    requirements = []
    for requirement in project["dependencies"]:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)>=(\d+(?:\.\d+)*)", requirement)
        if match is None or match[1] not in MODULES:
            raise ValueError(f"unsupported package runtime requirement: {requirement!r}")
        requirements.append([match[1], MODULES[match[1]], match[2]])
    if not requirements:
        raise ValueError("refusing a package with an empty runtime dependency policy")
    return {
        "mode": mode,
        "minimum_python": [int(minimum[1]), int(minimum[2])],
        "requirements": requirements,
    }


def render_validator(source, policy):
    template = (source / "packages/validate_external_runtime.sh.in").read_text()
    return (
        template.replace("@PYTHON_CHECK@", (source / "packages/runtime_check.py").read_text())
        .replace("@REQUIREMENTS@", repr(policy["requirements"]))
        .replace("@MINIMUM_PYTHON@", repr(policy["minimum_python"]))
    )


def write_text(path, text):
    path.write_text(text, encoding="utf-8", newline="\n")


def prepare_stage(source, stage, mode):
    policy = runtime_policy(source, mode)
    write_text(stage / "runtime-policy.json", json.dumps(policy, indent=2) + "\n")
    validator = stage / "validate-external-runtime"
    write_text(validator, render_validator(source, policy))
    validator.chmod(0o755)
    if mode == "external-venv":
        unit = stage / "aodv2.service"
        text = unit.read_text()
        if text.count("[Service]\n") != 1:
            raise ValueError("expected exactly one service section")
        write_text(
            unit,
            text.replace(
                "[Service]\n",
                "[Service]\n"
                "ExecStartPre=/bin/sh /opt/aodv2/validate-external-runtime --config /etc/aodv2/aodv2.env\n",
            ),
        )
    return policy


def hook_body(validator):
    return (
        "(\nset -- --config /etc/aodv2/aodv2.env\n"
        + validator.removeprefix("#!/bin/sh\n")
        + "\n)\n"
    )


def prepare_debian(stage, mode):
    if json.loads((stage / "runtime-policy.json").read_text())["mode"] != mode:
        raise ValueError("staged runtime policy does not match requested Debian mode")
    directory = stage / "debian"
    control = directory / "control"
    text = control.read_text()
    if mode == "external-venv":
        text = text.replace("Package: aodv2\n", "Package: aodv2-external-venv\n")
        text = text.replace("Conflicts: aodv2-external-venv\n", "Conflicts: aodv2\n")
        text = text.replace("XB-AOD-Runtime-Mode: system\n", "XB-AOD-Runtime-Mode: external-venv\n")
        for dependency in (
            "         python3 (>= 3.11),\n",
            "         python3-numpy,\n",
            "         python3-yaml,\n",
            "         python3-zstandard,\n",
        ):
            if dependency not in text:
                raise ValueError(f"expected default Debian dependency missing: {dependency.strip()}")
            text = text.replace(dependency, "")
        text = text.replace(
            "Description: Always-on diagnostics daemon for Linux NFS and SMB filesystems",
            "Description: Always-on diagnostics with an externally managed Python venv",
        )
        text = text.replace(
            " The daemon is installed under /opt/aodv2 and runs from the system Python\n interpreter by default.",
            " The daemon is installed under /opt/aodv2 and requires an externally\n managed Python virtual environment.",
        )
        text += (
            " .\n This opt-in variant requires a pre-existing Python venv configured in\n"
            " /etc/aodv2/aodv2.env; install-time and service-start checks enforce the\n"
            " declared Python and module versions. It never provisions a runtime.\n"
        )
        manifest = directory / "aodv2.install"
        write_text(
            manifest,
            "".join(line for line in manifest.read_text().splitlines(keepends=True)
                    if not line.startswith("aodv2.env ")),
        )
        manifest.rename(directory / "aodv2-external-venv.install")
        validator = (stage / "validate-external-runtime").read_text()
        preinst = directory / "preinst"
        write_text(
            preinst,
            '#!/bin/sh\nset -e\ncase "${1:-}" in\ninstall|upgrade)\n'
            + hook_body(validator)
            + ";;\nesac\n#DEBHELPER#\nexit 0\n",
        )
        preinst.chmod(0o755)
    write_text(control, text)


def prepare_rpm(source, stage, output, mode):
    if json.loads((stage / "runtime-policy.json").read_text())["mode"] != mode:
        raise ValueError("staged runtime policy does not match requested RPM mode")
    text = (source / "packages/rpm/aodv2.spec").read_text()
    hook = ""
    if mode == "external-venv":
        text = text.replace("Name:           aodv2\n", "Name:           aodv2-external-venv\n")
        text = text.replace("Conflicts:      aodv2-external-venv\n", "Conflicts:      aodv2\n")
        text = text.replace(
            "Requires:       (python3 >= 3.11 or /usr/bin/python3.11)\n",
            "Requires(pretrans): /bin/sh\n",
        )
        text = text.replace("Recommends:     python3-numpy, python3-pyyaml, python3-zstandard\n", "")
        text = text.replace("install -D -m 0644 aodv2.env       %{buildroot}%{_aod_etc}/aodv2.env\n", "")
        text = text.replace("%config(noreplace) %{_aod_etc}/aodv2.env\n", "")
        text = text.replace(
            "Summary:        Always-on diagnostics daemon for Linux NFS and SMB filesystems",
            "Summary:        Always-on diagnostics with an externally managed Python venv",
        )
        text = text.replace(
            "The daemon is installed under %{_aod_root} and runs from the system Python\ninterpreter by default.",
            "This opt-in external-venv variant requires an existing Python venv;\n"
            "install-time and service-start checks enforce its declared dependencies.\n"
            "It never provisions Python. The daemon is installed under %{_aod_root}.",
        )
        hook = "%pretrans -p /bin/sh\nset -e\n" + hook_body(
            (stage / "validate-external-runtime").read_text()
        ).replace("%", "%%")
    if text.count("# @AOD_RUNTIME_PRETRANS@") != 1:
        raise ValueError("RPM runtime hook marker missing or duplicated")
    write_text(output, text.replace("# @AOD_RUNTIME_PRETRANS@", hook))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--format", choices=("stage", "deb", "rpm"), required=True)
    parser.add_argument("--rpm-spec", type=Path)
    args = parser.parse_args()
    if args.format == "stage":
        prepare_stage(args.source, args.stage, args.mode)
    elif args.format == "deb":
        prepare_debian(args.stage, args.mode)
    else:
        if args.rpm_spec is None:
            parser.error("--rpm-spec is required for rpm rendering")
        prepare_rpm(args.source, args.stage, args.rpm_spec, args.mode)


if __name__ == "__main__":
    main()
