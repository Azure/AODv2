Name:           linux-diagnostics
Version:        0.1.0
Release:        1%{?dist}
Summary:        Always-on diagnostics daemon for Linux NFS and SMB filesystems
URL:            https://github.com/Azure/AODv2
License:        MIT
Source0:        %{name}-%{version}.tar.gz

BuildArch:      x86_64
BuildRequires:  systemd-rpm-macros

Requires:       python3 >= 3.11
Requires(post):   systemd
Requires(post):   python3 >= 3.11
Requires(post):   python3-pip
Requires(preun):  systemd
Requires(postun): systemd

%global _aod_root /opt/linux_diagnostics
%global _aod_etc  /etc/linux_diagnostics

%description
Always-on Diagnostics daemon for monitoring Linux NFS and SMB anomalies and automatic log-collection.

The daemon is installed under %{_aod_root} and runs from a self-contained
Python virtual environment built at install time. Configuration lives in
%{_aod_etc}/config.yaml; the output directory is user-tunable via the
`aod_output_dir` key in that file and is created by the daemon on first use.

%prep
%setup -q

%build
# Nothing to compile at RPM build time.

%install
rm -rf %{buildroot}

# Application tree under /opt/linux_diagnostics
install -d -m 0755 %{buildroot}%{_aod_root}
cp -a src %{buildroot}%{_aod_root}/src
install -m 0644 pyproject.toml %{buildroot}%{_aod_root}/pyproject.toml

# Configuration
install -D -m 0644 config/config.yaml %{buildroot}%{_aod_etc}/config.yaml

# systemd unit
install -D -m 0644 linux_diagnostics.service \
    %{buildroot}%{_unitdir}/linux_diagnostics.service

%post
# On initial install or upgrade, build/refresh the venv.
if [ $1 -eq 1 ] || [ $1 -eq 2 ]; then
    rm -rf %{_aod_root}/venv
    /usr/bin/python3 -m venv %{_aod_root}/venv
    %{_aod_root}/venv/bin/pip install --upgrade pip
    %{_aod_root}/venv/bin/pip install %{_aod_root} || { echo "venv build failed"; exit 1; }
fi
%systemd_post linux_diagnostics.service

%preun
%systemd_preun linux_diagnostics.service

%postun
%systemd_postun_with_restart linux_diagnostics.service
# $1 == 0 on final uninstall
if [ $1 -eq 0 ]; then
    rm -rf %{_aod_root}/venv
fi

%files
%dir %{_aod_root}
%{_aod_root}/src
%{_aod_root}/pyproject.toml
%dir %{_aod_etc}
%config(noreplace) %{_aod_etc}/config.yaml
%{_unitdir}/linux_diagnostics.service

%changelog
* Mon Jun 08 2026 Meetakshi Setiya <msetiya@microsoft.com> - 0.1.0-1
- Repackaged: install application tree to /opt/aod, build venv from
  pyproject.toml in %%post, run service from venv interpreter, read
  config from /etc/linux_diagnostics/config.yaml.
* Wed Apr 30 2025 Shyam Prasad N <sprasad@microsoft.com> - 1.0-1
- Initial RPM package
