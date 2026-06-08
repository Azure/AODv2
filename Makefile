# Makefile for building AODv2

SHELL := /bin/bash

.PHONY: build install-bins rpm prep clean cleanbins
default: build install-bins rpm

RPMBUILD := $(CURDIR)/rpmbuild
TMPLOCAL:=./tmp
SRCDIR:=./
LOCALRPMS:=./rpms
PKGNAME:=linux-diagnostics
VERSION:=0.1.0

build:
	$(MAKE) -C monitoring_tools

install-bins: build
	mkdir -p src/bin
	cp monitoring_tools/src/bin/* src/bin/

prep:
	@ mkdir -p ${TMPLOCAL} ${LOCALRPMS} ${RPMBUILD}/{BUILD,RPMS,SOURCES,SPECS,SRPMS} ${TMPLOCAL}/$(PKGNAME)-$(VERSION)

${TMPLOCAL}/$(PKGNAME)-$(VERSION).tar.gz: prep install-bins ${SRCDIR}/src/Controller.py \
	${SRCDIR}/config/config.yaml ${SRCDIR}/linux_diagnostics.service \
	${SRCDIR}/packages/rpm/linux_diagnostics.spec
	rm -rf ${TMPLOCAL}/$(PKGNAME)-$(VERSION)
	mkdir -p ${TMPLOCAL}/$(PKGNAME)-$(VERSION)
	cp -r ${SRCDIR}/src ${TMPLOCAL}/$(PKGNAME)-$(VERSION)/src
	cp -r ${SRCDIR}/config ${TMPLOCAL}/$(PKGNAME)-$(VERSION)/config
	cp ${SRCDIR}/pyproject.toml ${TMPLOCAL}/$(PKGNAME)-$(VERSION)
	cp ${SRCDIR}/linux_diagnostics.service ${TMPLOCAL}/$(PKGNAME)-$(VERSION)
	find ${TMPLOCAL}/$(PKGNAME)-$(VERSION) -type d -name "__pycache__" -prune -exec rm -rf {} +
	( cd ${TMPLOCAL}; tar -czf $(PKGNAME)-$(VERSION).tar.gz $(PKGNAME)-$(VERSION) )

rpm_prep: ${TMPLOCAL}/$(PKGNAME)-$(VERSION).tar.gz
	cp ${TMPLOCAL}/$(PKGNAME)-$(VERSION).tar.gz ${RPMBUILD}/SOURCES/
	cp ${SRCDIR}/packages/rpm/linux_diagnostics.spec ${RPMBUILD}/SPECS/
	
rpm: rpm_prep
	rpmbuild -ba ${RPMBUILD}/SPECS/linux_diagnostics.spec --define "_topdir ${RPMBUILD}"
	mv ${RPMBUILD}/RPMS/x86_64/*.rpm ${LOCALRPMS}/
	sha256sum ${LOCALRPMS}/*.rpm > ${LOCALRPMS}/sha256sums.txt

clean:
	$(MAKE) -C monitoring_tools clean
	rm -rf ${RPMBUILD}
	rm -rf ${TMPLOCAL}
	rm -rf ${LOCALRPMS}

cleanbins:
	rm -f src/bin/*

# all: debian rpm

# debian:
# 	cd packages/debian && dpkg-buildpackage -us -uc

# rpm:
# 	cd packages/rpm && rpmbuild -ba linux_diagnostics.spec

# clean:
# 	cd packages/debian && dpkg-buildpackage -k
# 	cd packages/rpm && rm -rf *.rpm *.src.rpm
