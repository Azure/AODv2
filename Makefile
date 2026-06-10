# Makefile for building AODv2

SHELL := /bin/bash

.PHONY: build install-bins rpm deb prep rpm_prep deb_prep clean cleanbins
default: build install-bins rpm deb

RPMBUILD := $(CURDIR)/rpmbuild
DEBBUILD := $(CURDIR)/debbuild
TMPLOCAL:=./tmp
LOCALRPMS:=./rpms
LOCALDEBS:=./debs
SRCDIR:=./
PKGNAME:=aodv2
VERSION:=0.1.0

build:
	$(MAKE) -C monitoring_tools

install-bins: build
	mkdir -p src/bin
	cp monitoring_tools/src/bin/* src/bin/

prep:
	@ mkdir -p ${TMPLOCAL} ${TMPLOCAL}/$(PKGNAME)-$(VERSION)

rpm_prep_dirs:
	@ mkdir -p ${LOCALRPMS} ${RPMBUILD}/{BUILD,RPMS,SOURCES,SPECS,SRPMS}

deb_prep_dirs:
	@ mkdir -p ${LOCALDEBS} ${DEBBUILD}

${TMPLOCAL}/$(PKGNAME)-$(VERSION).tar.gz: prep install-bins ${SRCDIR}/src/Controller.py \
	${SRCDIR}/config/config.yaml ${SRCDIR}/aodv2.service \
	${SRCDIR}/packages/rpm/aodv2.spec
	rm -rf ${TMPLOCAL}/$(PKGNAME)-$(VERSION)
	mkdir -p ${TMPLOCAL}/$(PKGNAME)-$(VERSION)
	cp -r ${SRCDIR}/src ${TMPLOCAL}/$(PKGNAME)-$(VERSION)/src
	cp -r ${SRCDIR}/config ${TMPLOCAL}/$(PKGNAME)-$(VERSION)/config
	cp ${SRCDIR}/pyproject.toml ${TMPLOCAL}/$(PKGNAME)-$(VERSION)
	cp ${SRCDIR}/aodv2.service ${TMPLOCAL}/$(PKGNAME)-$(VERSION)
	find ${TMPLOCAL}/$(PKGNAME)-$(VERSION) -type d -name "__pycache__" -prune -exec rm -rf {} +
	( cd ${TMPLOCAL}; tar -czf $(PKGNAME)-$(VERSION).tar.gz $(PKGNAME)-$(VERSION) )

rpm_prep: ${TMPLOCAL}/$(PKGNAME)-$(VERSION).tar.gz rpm_prep_dirs
	cp ${TMPLOCAL}/$(PKGNAME)-$(VERSION).tar.gz ${RPMBUILD}/SOURCES/
	cp ${SRCDIR}/packages/rpm/aodv2.spec ${RPMBUILD}/SPECS/
	
rpm: rpm_prep
	rpmbuild -ba ${RPMBUILD}/SPECS/aodv2.spec --define "_topdir ${RPMBUILD}"
	mv ${RPMBUILD}/RPMS/x86_64/*.rpm ${LOCALRPMS}/
	sha256sum ${LOCALRPMS}/*.rpm > ${LOCALRPMS}/sha256sums.txt

deb_prep: ${TMPLOCAL}/$(PKGNAME)-$(VERSION).tar.gz deb_prep_dirs
	rm -rf ${DEBBUILD}/$(PKGNAME)-$(VERSION)
	tar -xzf ${TMPLOCAL}/$(PKGNAME)-$(VERSION).tar.gz -C ${DEBBUILD}
	cp -a ${SRCDIR}/packages/debian ${DEBBUILD}/$(PKGNAME)-$(VERSION)/debian

deb: deb_prep
	cd ${DEBBUILD}/$(PKGNAME)-$(VERSION) && dpkg-buildpackage -us -uc -b
	mv ${DEBBUILD}/$(PKGNAME)_$(VERSION)*.deb ${LOCALDEBS}/
	mv ${DEBBUILD}/$(PKGNAME)_$(VERSION)*.buildinfo ${LOCALDEBS}/ 2>/dev/null || true
	mv ${DEBBUILD}/$(PKGNAME)_$(VERSION)*.changes  ${LOCALDEBS}/ 2>/dev/null || true
	sha256sum ${LOCALDEBS}/*.deb > ${LOCALDEBS}/sha256sums.txt

clean:
	$(MAKE) -C monitoring_tools clean
	rm -rf ${RPMBUILD}
	rm -rf ${DEBBUILD}
	rm -rf ${TMPLOCAL}
	rm -rf ${LOCALRPMS}
	rm -rf ${LOCALDEBS}

cleanbins:
	rm -f src/bin/*