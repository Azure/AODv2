# Makefile for building AODv2

.PHONY: all build install-bins clean

all: build install-bins

build:
	$(MAKE) -C monitoring_tools

install-bins:
	mkdir -p src/bin
	cp monitoring_tools/src/bin/* src/bin/

clean:
	$(MAKE) -C monitoring_tools clean
	rm -f src/bin/*

# all: debian rpm

# debian:
# 	cd packages/debian && dpkg-buildpackage -us -uc

# rpm:
# 	cd packages/rpm && rpmbuild -ba linux_diagnostics.spec

# clean:
# 	cd packages/debian && dpkg-buildpackage -k
# 	cd packages/rpm && rm -rf *.rpm *.src.rpm
