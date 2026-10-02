"""Tests for integrations installed from an uploaded ZIP archive."""

from collections.abc import Awaitable, Callable, Generator
import io
import json
from pathlib import Path
from random import getrandbits
from typing import Any
from unittest.mock import AsyncMock, patch
import zipfile

from aiohttp import FormData
import pytest

from homeassistant.components import file_upload
from homeassistant.components.marketplace.base import MarketplaceManager
from homeassistant.components.marketplace.const import DOMAIN
from homeassistant.components.marketplace.repositories.base import Repository
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from . import get_marketplace

from tests.common import MockConfigEntry
from tests.typing import ClientSessionGenerator, WebSocketGenerator

type Upload = Callable[[bytes], Awaitable[str]]

ZIPPED_DOMAIN = "zipped"


def _manifest(**changes: Any) -> str:
    """Return the manifest.json of the zipped integration."""
    return json.dumps(
        {
            "domain": ZIPPED_DOMAIN,
            "name": "Zipped",
            "version": "1.0.0",
            "config_flow": True,
            "codeowners": [],
            "documentation": "https://example.com",
        }
        | changes
    )


def _zip(files: dict[str, str]) -> bytes:
    """Return a ZIP archive with the files."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def mock_reload() -> Generator[AsyncMock]:
    """Have the loader find every domain that is installed."""
    with patch(
        "homeassistant.components.marketplace.archive.async_reload_custom_components",
        return_value={ZIPPED_DOMAIN},
    ) as reload:
        yield reload


@pytest.fixture
def upload(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> Generator[Upload]:
    """Return a function that uploads a file and returns its id."""

    async def _upload(content: bytes) -> str:
        client = await hass_client()
        data = FormData()
        data.add_field("file", content, filename="integration.zip")
        response = await client.post("/api/file_upload", data=data)
        assert response.status == 200
        return (await response.json())["file_id"]

    # Tests running in parallel would share the upload directory
    with patch(
        "homeassistant.components.file_upload.TEMP_DIR_NAME",
        f"{file_upload.TEMP_DIR_NAME}-{getrandbits(10):03x}",
    ):
        yield _upload


async def _install(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    upload: Upload,
    files: dict[str, str],
    **options: Any,
) -> dict[str, Any]:
    """Upload an archive and install it, return the response."""
    file_id = await upload(_zip(files))
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "marketplace/archive/install", "file_id": file_id, **options}
    )
    return await client.receive_json()


@pytest.mark.parametrize(
    ("prefix", "outside"),
    [
        pytest.param("", {}, id="root"),
        pytest.param(
            f"{ZIPPED_DOMAIN}/", {"README.md": "not installed"}, id="directory"
        ),
        pytest.param(
            f"repo-1.0/custom_components/{ZIPPED_DOMAIN}/",
            {"repo-1.0/README.md": "not installed"},
            id="repository",
        ),
    ],
)
async def test_install(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    hass_ws_client: WebSocketGenerator,
    upload: Upload,
    config_dir: Path,
    prefix: str,
    outside: dict[str, str],
) -> None:
    """Test the integration of an archive is installed, wherever it is in it."""
    response = await _install(
        hass,
        hass_ws_client,
        upload,
        {
            f"{prefix}manifest.json": _manifest(),
            f"{prefix}__init__.py": "# zipped\n",
            f"{prefix}translations/en.json": "{}",
        }
        | outside,
    )

    assert response["success"]
    assert response["result"] == {
        "domain": ZIPPED_DOMAIN,
        "name": "Zipped",
        "version": "1.0.0",
        "config_flow": True,
        "installed_at": response["result"]["installed_at"],
        "pending_restart": False,
    }
    local = config_dir / "custom_components" / ZIPPED_DOMAIN
    assert sorted(path.relative_to(local).as_posix() for path in local.rglob("*")) == [
        "__init__.py",
        "manifest.json",
        "translations",
        "translations/en.json",
    ]

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "marketplace/archives/list"})
    assert (await client.receive_json())["result"] == [response["result"]]


async def test_update_replaces_the_files(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    hass_ws_client: WebSocketGenerator,
    upload: Upload,
    config_dir: Path,
) -> None:
    """Test a newer archive replaces what the older one installed."""
    await _install(
        hass,
        hass_ws_client,
        upload,
        {"manifest.json": _manifest(), "old.py": "", "__init__.py": "# 1.0.0\n"},
    )

    response = await _install(
        hass,
        hass_ws_client,
        upload,
        {"manifest.json": _manifest(version="2.0.0"), "__init__.py": "# 2.0.0\n"},
    )

    assert response["result"]["version"] == "2.0.0"
    local = config_dir / "custom_components" / ZIPPED_DOMAIN
    assert not (local / "old.py").exists()
    assert (local / "__init__.py").read_text() == "# 2.0.0\n"
    assert [i.version for i in marketplace.archives.list_installed] == ["2.0.0"]


@pytest.mark.parametrize(
    ("manifest", "pending_restart"),
    [
        pytest.param(_manifest(config_flow=False), True, id="yaml_only"),
        pytest.param(_manifest(), False, id="config_flow"),
    ],
)
async def test_restart_repair(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    hass_ws_client: WebSocketGenerator,
    upload: Upload,
    issue_registry: ir.IssueRegistry,
    manifest: str,
    pending_restart: bool,
) -> None:
    """Test a restart is asked for code the loader can not pick up on its own."""
    response = await _install(hass, hass_ws_client, upload, {"manifest.json": manifest})

    assert response["result"]["pending_restart"] is pending_restart
    assert (
        issue_registry.async_get_issue(
            DOMAIN, f"restart_required_archive_{ZIPPED_DOMAIN}"
        )
        is not None
    ) is pending_restart


@pytest.mark.parametrize(
    ("files", "translation_key"),
    [
        pytest.param({"__init__.py": ""}, "archive_without_manifest", id="no_manifest"),
        pytest.param(
            {"a/manifest.json": _manifest(), "b/manifest.json": _manifest()},
            "archive_manifest_ambiguous",
            id="two_integrations",
        ),
        pytest.param(
            {"manifest.json": _manifest(domain="../escape")},
            "invalid_domain",
            id="invalid_domain",
        ),
        pytest.param(
            {"manifest.json": "not json"},
            "installed_manifest_unusable",
            id="invalid_json",
        ),
        pytest.param(
            {"manifest.json": _manifest(version="not a version!")},
            "installed_manifest_without_version",
            id="invalid_version",
        ),
    ],
)
async def test_install_refuses_an_archive(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    hass_ws_client: WebSocketGenerator,
    upload: Upload,
    config_dir: Path,
    files: dict[str, str],
    translation_key: str,
) -> None:
    """Test an archive without one loadable integration installs nothing."""
    response = await _install(hass, hass_ws_client, upload, files)

    assert response["error"]["code"] == "error"
    assert response["error"]["translation_key"] == translation_key
    assert not (config_dir / "custom_components" / ZIPPED_DOMAIN).exists()
    assert marketplace.archives.list_installed == []


async def test_install_refuses_what_is_not_a_zip(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    hass_ws_client: WebSocketGenerator,
    upload: Upload,
) -> None:
    """Test an upload that is no ZIP archive is refused."""
    file_id = await upload(b"not a zip")
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "marketplace/archive/install", "file_id": file_id}
    )

    assert (await client.receive_json())["error"]["translation_key"] == (
        "archive_not_zip"
    )


async def test_install_of_an_unknown_upload(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test an id that names no upload is refused."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "marketplace/archive/install", "file_id": "unknown"}
    )

    assert (await client.receive_json())["error"]["translation_key"] == (
        "uploaded_file_not_found"
    )


async def test_failed_update_keeps_the_installed_version(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    hass_ws_client: WebSocketGenerator,
    upload: Upload,
    config_dir: Path,
) -> None:
    """Test an archive the loader would refuse leaves the installed one in place."""
    await _install(
        hass,
        hass_ws_client,
        upload,
        {"manifest.json": _manifest(), "__init__.py": "# 1.0.0\n"},
    )

    response = await _install(
        hass,
        hass_ws_client,
        upload,
        {"manifest.json": json.dumps({"domain": ZIPPED_DOMAIN}), "__init__.py": ""},
    )

    assert not response["success"]
    local = config_dir / "custom_components" / ZIPPED_DOMAIN
    assert (local / "__init__.py").read_text() == "# 1.0.0\n"
    assert [i.version for i in marketplace.archives.list_installed] == ["1.0.0"]


async def test_replacing_a_built_in_integration_is_confirmed(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    hass_ws_client: WebSocketGenerator,
    upload: Upload,
) -> None:
    """Test an archive that replaces a built-in integration needs a confirmation."""
    files = {"manifest.json": _manifest(domain="sun")}

    response = await _install(hass, hass_ws_client, upload, files)
    assert response["error"]["code"] == "replaces_built_in"
    assert response["error"]["translation_placeholders"] == {
        "repository": "integration.zip",
        "domain": "sun",
    }

    response = await _install(
        hass, hass_ws_client, upload, files, confirm_replace_built_in=True
    )
    assert response["success"]


@pytest.mark.usefixtures("stored_repositories")
async def test_install_refuses_a_domain_of_a_repository(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    hass_ws_client: WebSocketGenerator,
    upload: Upload,
) -> None:
    """Test an archive can not take over what a repository installed."""
    response = await _install(
        hass, hass_ws_client, upload, {"manifest.json": _manifest(domain="example")}
    )

    assert response["error"]["translation_key"] == "integration_owned"
    assert response["error"]["translation_placeholders"] == {
        "domain": "example",
        "owner": "hacs-test-org/integration-basic",
    }


async def test_repository_takes_over_from_an_archive(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    mock_repository_integration: Repository,
    hass_ws_client: WebSocketGenerator,
    upload: Upload,
) -> None:
    """Test a repository installed over an archive owns the integration."""
    await _install(hass, hass_ws_client, upload, {"manifest.json": _manifest()})
    mock_repository_integration.data.domain = ZIPPED_DOMAIN

    await mock_repository_integration.async_post_installation()

    assert marketplace.archives.list_installed == []


@pytest.mark.parametrize("warning_accepted", [None])
async def test_install_needs_the_accepted_warning(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    hass_ws_client: WebSocketGenerator,
    upload: Upload,
) -> None:
    """Test installing waits for the warning to be accepted."""
    response = await _install(
        hass, hass_ws_client, upload, {"manifest.json": _manifest()}
    )

    assert response["error"]["code"] == "warning_not_accepted"


async def test_uninstall(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    hass_ws_client: WebSocketGenerator,
    upload: Upload,
    config_dir: Path,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Test uninstalling removes the files and asks for a restart of loaded code."""
    await _install(hass, hass_ws_client, upload, {"manifest.json": _manifest()})
    client = await hass_ws_client(hass)

    with patch(
        "homeassistant.components.marketplace.archive.is_known_to_the_loader",
        return_value=True,
    ):
        await client.send_json_auto_id(
            {"type": "marketplace/archive/uninstall", "domain": ZIPPED_DOMAIN}
        )
        response = await client.receive_json()

    assert response["success"]
    assert not (config_dir / "custom_components" / ZIPPED_DOMAIN).exists()
    assert marketplace.archives.list_installed == []
    assert issue_registry.async_get_issue(
        DOMAIN, f"restart_required_archive_{ZIPPED_DOMAIN}_uninstall"
    )


async def test_uninstall_refuses_what_is_set_up(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    hass_ws_client: WebSocketGenerator,
    upload: Upload,
    config_dir: Path,
) -> None:
    """Test an integration with config entries stays installed."""
    await _install(hass, hass_ws_client, upload, {"manifest.json": _manifest()})
    MockConfigEntry(domain=ZIPPED_DOMAIN).add_to_hass(hass)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": "marketplace/archive/uninstall", "domain": ZIPPED_DOMAIN}
    )

    assert (await client.receive_json())["error"]["code"] == "repository_in_use"
    assert (config_dir / "custom_components" / ZIPPED_DOMAIN).is_dir()


async def test_uninstall_of_what_no_archive_installed(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test a domain no archive installed is refused."""
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": "marketplace/archive/uninstall", "domain": ZIPPED_DOMAIN}
    )

    assert (await client.receive_json())["error"]["code"] == "archive_not_installed"


async def test_installs_survive_a_reload(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    upload: Upload,
    config_dir: Path,
) -> None:
    """Test what is installed is stored, unless its files are gone."""
    await _install(hass, hass_ws_client, upload, {"manifest.json": _manifest()})
    await _install(
        hass,
        hass_ws_client,
        upload,
        {"manifest.json": _manifest(domain="removed_by_hand")},
    )
    (config_dir / "custom_components" / "removed_by_hand" / "manifest.json").unlink()
    (config_dir / "custom_components" / "removed_by_hand").rmdir()

    assert await hass.config_entries.async_reload(init_integration.entry_id)

    assert [
        integration.domain
        for integration in get_marketplace(hass).archives.list_installed
    ] == [ZIPPED_DOMAIN]
