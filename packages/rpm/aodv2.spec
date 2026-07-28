%global debug_package %{nil}

Name:           aodv2
Version:        0.1.0
Release:        1%{?dist}
Summary:        Always-on diagnostics daemon for Linux NFS and SMB filesystems
URL:            https://github.com/Azure/AODv2
License:        MIT
# Sources available at https://github.com/Azure/AODv2
Source0:        %{name}-%{version}.tar.gz

%global _aod_root /opt/aodv2
%global _aod_etc  /etc/aodv2

ExclusiveArch:  x86_64
BuildRequires:  systemd-rpm-macros, make, clang, bpftool, libbpf-devel
Requires:       (python3 >= 3.11 or python3.11)
Requires:       systemd
Requires:       python3-numpy, python3-pyyaml, python3-zstandard
Recommends:     trace-cmd
Recommends:     tcpdump

%description
Always-on Diagnostics daemon for monitoring Linux NFS and SMB anomalies with
automatic log collection.

The daemon is installed under %{_aod_root} and runs from the system Python
interpreter by default. Set AOD_PYTHON in %{_aod_etc}/aodv2.env to point it
at a user-managed virtual environment instead. Configuration lives in
%{_aod_etc}/config.yaml; the output directory is user-tunable via the
`aod_output_dir` key in that file and is created by the daemon on first use.

%prep
%setup -q
%build

%install
rm -rf %{buildroot}

# Application tree under /opt/aodv2
install -d -m 0755 %{buildroot}%{_aod_root}
cp -a src %{buildroot}%{_aod_root}/src

# Configuration
install -D -m 0644 config/config.yaml %{buildroot}%{_aod_etc}/config.yaml
install -D -m 0644 aodv2.env       %{buildroot}%{_aod_etc}/aodv2.env

# systemd unit
install -D -m 0644 aodv2.service \
    %{buildroot}%{_unitdir}/aodv2.service

%post
%systemd_post aodv2.service

%preun
%systemd_preun aodv2.service

%postun
%systemd_postun_with_restart aodv2.service
# $1 == 0 on final uninstall
if [ $1 -eq 0 ]; then
    rm -rf %{_aod_root}
fi

%files
%dir %{_aod_root}
%{_aod_root}/src
%dir %{_aod_etc}
%config(noreplace) %{_aod_etc}/config.yaml
%config(noreplace) %{_aod_etc}/aodv2.env
%{_unitdir}/aodv2.service

%changelog
* Mon Jun 08 2026 Meetakshi Setiya <msetiya@microsoft.com> - 0.1.0-1
- Renamed package and systemd unit to aodv2; install tree under
  /opt/aodv2, config under /etc/aodv2, build venv from pyproject.toml
  in %%post, run service from venv interpreter.
* Wed Apr 30 2025 Shyam Prasad N <sprasad@microsoft.com> - 1.0-1
- Initial RPM package
