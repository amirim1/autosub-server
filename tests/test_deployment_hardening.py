from pathlib import Path


def test_nginx_setup_validates_inputs_and_restores_rejected_config():
    script = Path("setup_nginx.sh").read_text(encoding="utf-8")

    assert '"$(id -u)" -eq 0' in script
    assert "domain contains unsafe characters" in script
    assert "domain contains an invalid label" in script
    assert "public port must be between 1 and 65535" in script
    assert "upstream must be an HTTP loopback URL" in script
    assert "TLS paths must be absolute" in script
    assert "exists and is not a symlink" in script
    assert "points to an unexpected target" in script
    assert "exists and is not a regular file" in script
    assert 'TEMP_CONFIG="$(mktemp ' in script
    assert "if ! nginx -t; then" in script
    assert "previous state restored" in script
    assert "ln -s \"$AVAILABLE\" \"$ENABLED\"" in script
    assert "ssl_protocols TLSv1.2 TLSv1.3" in script
    assert "Strict-Transport-Security" in script
    assert "proxy_connect_timeout 5s" in script
    assert "|| true" not in script


def test_service_sandbox_keeps_only_shared_runtime_writable():
    unit = Path("autosub-server.service").read_text(encoding="utf-8")

    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=/opt/autosub-server/shared" in unit
    assert "CapabilityBoundingSet=\n" in unit
    assert "AmbientCapabilities=\n" in unit
    assert "ProtectKernelTunables=true" in unit
    assert "ProtectKernelModules=true" in unit
    assert "ProtectControlGroups=true" in unit


def test_code_scanning_and_actions_updates_are_configured():
    codeql = Path(".github/workflows/codeql.yml").read_text(encoding="utf-8")
    dependabot = Path(".github/dependabot.yml").read_text(encoding="utf-8")

    assert "github/codeql-action/init@v4" in codeql
    assert "github/codeql-action/analyze@v4" in codeql
    assert "languages: python" in codeql
    assert "queries: security-extended" in codeql
    assert "security-events: write" in codeql
    assert "package-ecosystem: github-actions" in dependabot


def test_main_and_dev_install_update_channels_are_documented_and_exact():
    install = Path("install.sh").read_text(encoding="utf-8")
    update = Path("update.sh").read_text(encoding="utf-8")

    assert 'TARGET_VER="${AUTOSUB_VERSION:-latest}"' in install
    assert 'local requested="${AUTOSUB_VERSION:-latest}"' in update
    assert "releases/latest" in install
    assert "releases/latest" in update
    assert 'git clone --quiet --depth 1 --branch "$TARGET_VER"' in install
    assert 'git clone --quiet --depth 1 --branch "$requested"' in update

    for readme_name in ("README.md", "README_EN.md"):
        readme = Path(readme_name).read_text(encoding="utf-8")
        assert "autosub-server/main/install.sh" in readme
        assert "autosub-server/dev/install.sh" in readme
        assert "autosub-server/main/update.sh" in readme
        assert "autosub-server/dev/update.sh" in readme
        assert "AUTOSUB_VERSION=dev bash" in readme
