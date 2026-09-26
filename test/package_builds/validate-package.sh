#!/bin/bash

set -euo pipefail

package="${1:-}"
if [[ ! -f "$package" ]]; then
	echo "usage: $0 PACKAGE" >&2
	exit 2
fi
package="$(realpath "$package")"

case "$package" in
	*.deb)
		[[ "$(dpkg-deb --field "$package" Package)" == "aodv2" ]]
		[[ "$(dpkg-deb --field "$package" Architecture)" == "amd64" ]]
		apt-get update
		DEBIAN_FRONTEND=noninteractive apt-get install -y "$package"
		;;
	*.rpm)
		[[ "$(rpm -qp --queryformat '%{NAME}' "$package")" == "aodv2" ]]
		[[ "$(rpm -qp --queryformat '%{ARCH}' "$package")" == "x86_64" ]]
		if command -v tdnf >/dev/null 2>&1; then
			tdnf install -y "$package"
		elif command -v dnf >/dev/null 2>&1; then
			dnf install -y "$package"
		elif command -v yum >/dev/null 2>&1; then
			yum install -y "$package"
		elif command -v zypper >/dev/null 2>&1; then
			zypper --non-interactive install "$package"
		else
			echo "no supported RPM package manager found" >&2
			exit 1
		fi
		;;
	*)
		echo "unsupported package format: $package" >&2
		exit 2
		;;
esac

test -r /etc/aodv2/config.yaml
test -r /etc/aodv2/aodv2.env
test -r /opt/aodv2/src/aod_entry.py
test -r /usr/lib/systemd/system/aodv2.service || \
	test -r /lib/systemd/system/aodv2.service

for binary in ioslower iosnoop nfsiosnoop nfsslower smbiosnoop smbslower; do
	test -x "/opt/aodv2/src/bin/$binary"
	"/opt/aodv2/src/bin/$binary" --help >/dev/null
done
test -r /opt/aodv2/src/bin/libringbuf_shim.so

PYTHONPATH=/opt/aodv2/src python3 -c \
	'import numpy, yaml, zstandard; from ConfigManager import ConfigManager'

if command -v systemd-analyze >/dev/null 2>&1; then
	unit=/usr/lib/systemd/system/aodv2.service
	[[ -f "$unit" ]] || unit=/lib/systemd/system/aodv2.service
	systemd-analyze verify "$unit"
fi

echo "AODv2 package validation passed: $(basename "$package")"