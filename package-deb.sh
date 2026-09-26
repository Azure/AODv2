#!/bin/bash

set -euo pipefail

release_name="${1:-}"
case "$release_name" in
	jammy|noble) ;;
	*)
		echo "usage: $0 {jammy|noble}" >&2
		exit 2
		;;
esac

if git submodule status --recursive | grep -q '^-'; then
	echo "monitoring_tools submodule is not initialized" >&2
	exit 1
fi

version="$(make --no-print-directory print-version)"
[[ -n "$version" ]]
rm -rf debbuild debs
make deb

mapfile -t packages < <(find debs -maxdepth 1 -type f -name 'aodv2_*_amd64.deb')
if (( ${#packages[@]} != 1 )); then
	echo "expected one amd64 DEB, found ${#packages[@]}" >&2
	exit 1
fi

output_dir="PACKAGES/deb/$release_name"
mkdir -p "$output_dir"
package_name="$(basename "${packages[0]}" .deb).$release_name.deb"
mv "${packages[0]}" "$output_dir/$package_name"
sha256sum "$output_dir/$package_name" > "$output_dir/sha256sums.txt"
python3 tools/write_package_manifest.py \
	--distro "$release_name" \
	--format deb \
	--package "$output_dir/$package_name" \
	--output "$output_dir/manifest-$release_name.json"