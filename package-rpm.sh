#!/bin/bash

set -euo pipefail

distro="${1:-}"
case "$distro" in
	azl3|el9|el10|sles15) ;;
	*)
		echo "usage: $0 {azl3|el9|el10|sles15}" >&2
		exit 2
		;;
esac

if git submodule status --recursive | grep -q '^-'; then
	echo "monitoring_tools submodule is not initialized" >&2
	exit 1
fi

version="$(make --no-print-directory print-version)"
[[ -n "$version" ]]
rm -rf rpmbuild rpms
make rpm RPM_DIST=".$distro"

mapfile -t packages < <(find rpms -maxdepth 1 -type f -name 'aodv2-*.x86_64.rpm')
if (( ${#packages[@]} != 1 )); then
	echo "expected one x86_64 RPM, found ${#packages[@]}" >&2
	exit 1
fi

output_dir="PACKAGES/rpm/$distro"
mkdir -p "$output_dir"
mv "${packages[0]}" "$output_dir/"
package_name="$(basename "${packages[0]}")"
sha256sum "$output_dir/$package_name" > "$output_dir/sha256sums.txt"
python3 tools/write_package_manifest.py \
	--distro "$distro" \
	--format rpm \
	--package "$output_dir/$package_name" \
	--output "$output_dir/manifest-$distro.json"