#!/bin/bash

set -euo pipefail
shopt -s nullglob

mode="${1:-publish}"
case "$mode" in
	test|publish|rollback) ;;
	*)
		echo "usage: $0 {test|publish|rollback}" >&2
		exit 2
		;;
esac

python3 -m pip install --no-index --find-links python_dl pmc-cli
mkdir -p "$HOME/.config/pmc"
cp settings.toml "$HOME/.config/pmc/settings.toml"
command -v pmc >/dev/null
pmc --version

PACKAGE_MAP=(
	'aodv2_*_amd64.jammy.deb|microsoft-ubuntu-jammy-prod-apt|jammy'
	'aodv2_*_amd64.noble.deb|microsoft-ubuntu-noble-prod-apt|noble'
	'aodv2-*.azl3.x86_64.rpm|azurelinux-3.0-prod-ms-oss-x86_64-yum|'
	'aodv2-*.el9.x86_64.rpm|microsoft-rhel9.0-prod-yum|'
	'aodv2-*.el10.x86_64.rpm|microsoft-rhel10-prod-yum|'
	'aodv2-*.sles15.x86_64.rpm|microsoft-sles15-prod-yum|'
)

resolve_package() {
	local pattern="$1"
	local matches=(packages/$pattern)
	if (( ${#matches[@]} != 1 )); then
		echo "expected one package matching $pattern, found ${#matches[@]}" >&2
		exit 1
	fi
	printf '%s\n' "${matches[0]}"
}

check_repo() {
	local repository="$1"
	pmc repo list --name "$repository" | grep -Fq -- "$repository" || {
		echo "PMC repository not found: $repository" >&2
		exit 1
	}
}

echo "Validating package set and target repositories"
[[ -f manifest.json && -f sha256sums.txt ]]
(cd packages && sha256sum -c ../sha256sums.txt)
all_packages=(packages/*.deb packages/*.rpm)
(( ${#all_packages[@]} == ${#PACKAGE_MAP[@]} )) || {
	echo "expected ${#PACKAGE_MAP[@]} packages, found ${#all_packages[@]}" >&2
	exit 1
}
for mapping in "${PACKAGE_MAP[@]}"; do
	IFS='|' read -r pattern repository release <<< "$mapping"
	resolve_package "$pattern" >/dev/null
	check_repo "$repository"
done

if [[ "$mode" == test ]]; then
	echo "PMC preflight passed; no packages were changed"
	exit 0
fi

package_version() {
	local package_name="$(basename "$1")"
	if [[ "$package_name" == *.deb ]]; then
		package_name="${package_name#aodv2_}"
		printf '%s\n' "${package_name%%_amd64.*}"
	else
		package_name="${package_name#aodv2-}"
		printf '%s\n' "${package_name%.*.x86_64.rpm}"
	fi
}

package_format() {
	[[ "$1" == *.deb ]] && printf 'deb\n' || printf 'rpm\n'
}

publish_package() {
	local package="$1"
	local repository="$2"
	local release="$3"
	local package_id
	package_id="$(pmc --id-only package upload "$package")"
	if [[ -n "$release" ]]; then
		pmc repo package update --add-packages "$package_id" "$repository" "$release"
	else
		pmc repo package update --add-packages "$package_id" "$repository"
	fi
	pmc repo publish "$repository"
	echo "published $(basename "$package") as $package_id to $repository"
}

rollback_package() {
	local package="$1"
	local repository="$2"
	local format version package_id
	format="$(package_format "$package")"
	version="$(package_version "$package")"
	package_id="$(pmc package "$format" list --name aodv2 --version "$version" --repo "$repository" | \
		python3 -c 'import json, sys; print(json.load(sys.stdin)["results"][0]["id"])')"
	pmc repo package update --remove-packages "$package_id" "$repository"
	pmc repo publish "$repository"
	echo "removed $package_id from $repository"
}

for mapping in "${PACKAGE_MAP[@]}"; do
	IFS='|' read -r pattern repository release <<< "$mapping"
	package="$(resolve_package "$pattern")"
	if [[ "$mode" == publish ]]; then
		publish_package "$package" "$repository" "$release"
	else
		rollback_package "$package" "$repository"
	fi
done