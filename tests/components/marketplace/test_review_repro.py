"""Regression reproductions for the Marketplace PR review."""

from io import BytesIO
from pathlib import Path
from unittest.mock import patch
import zipfile

import pytest

from homeassistant.components.marketplace.base import MarketplaceManager
from homeassistant.components.marketplace.enums import RepositoryCategory
from homeassistant.components.marketplace.exceptions import MarketplaceError
from homeassistant.components.marketplace.repositories.base import (
    FileInformation,
    RepositoryArchive,
)
from homeassistant.components.marketplace.repositories.integration import (
    IntegrationRepository,
)
from homeassistant.components.marketplace.repositories.plugin import PluginRepository
from homeassistant.components.marketplace.repositories.theme import ThemeRepository
from homeassistant.core import HomeAssistant

from tests.common import MockConfigEntry
from tests.typing import WebSocketGenerator


async def test_custom_repo_survives_restart(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    init_integration: MockConfigEntry,
) -> None:
    """Keep an explicitly added custom repository after an integration reload."""
    await marketplace.async_register_repository(
        "hacs-test-org/integration-basic-custom", RepositoryCategory.INTEGRATION
    )
    repo = marketplace.repositories.get_by_full_name(
        "hacs-test-org/integration-basic-custom"
    )
    assert repo is not None
    await marketplace.data.async_write()
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await hass.async_block_till_done()
    assert (
        init_integration.runtime_data.repositories.get_by_full_name(
            "hacs-test-org/integration-basic-custom"
        )
        is not None
    )


async def test_failed_plugin_update_restores_previous_files(
    marketplace: MarketplaceManager,
) -> None:
    """Restore existing release assets when a later asset fails to download."""
    repo = PluginRepository(marketplace, "review/test-card")
    repo.data.installed = True
    repo.content.single = True
    folder = Path(repo.localpath)
    folder.mkdir(parents=True)
    (folder / "test-card.js").write_bytes(b"old working version")
    (folder / "chunk.js").write_bytes(b"old working chunk")

    async def download() -> None:
        await repo._async_write_file(
            FileInformation(
                name="test-card.js", path="test-card.js", url="https://example.org/card"
            ),
            b"new version",
        )
        repo.validate.errors.append("chunk.js failed to download")

    with pytest.raises(MarketplaceError):
        await repo._async_write_content(download)
    assert (folder / "test-card.js").read_bytes() == b"old working version"


async def test_failed_backup_does_not_modify_install(
    marketplace: MarketplaceManager,
) -> None:
    """Leave the installation intact when creating its backup fails."""
    repo = PluginRepository(marketplace, "review/test-card")
    repo.data.installed = True
    folder = Path(repo.localpath)
    folder.mkdir(parents=True)
    (folder / "test-card.js").write_bytes(b"old working version")

    async def download() -> None:
        await repo._async_write_file(
            FileInformation(
                name="test-card.js", path="test-card.js", url="https://example.org/card"
            ),
            b"new version",
        )
        repo.validate.errors.append("later file failed to download")

    with (
        patch(
            "homeassistant.components.marketplace.utils.backup.shutil.move",
            side_effect=OSError("disk full"),
        ),
        pytest.raises(MarketplaceError),
    ):
        await repo._async_write_content(download)
    assert (folder / "test-card.js").read_bytes() == b"old working version"


def test_archive_only_extracts_selected_integration(tmp_path: Path) -> None:
    """Do not extract similarly named sibling integration directories."""
    contents = BytesIO()
    with zipfile.ZipFile(contents, "w") as archive:
        archive.writestr("repo-1/custom_components/foo/__init__.py", "foo code")
        archive.writestr(
            "repo-1/custom_components/foo/bar/__init__.py", "correct submodule"
        )
        archive.writestr("repo-1/custom_components/foobar/__init__.py", "foobar code")
    RepositoryArchive(contents.getvalue()).extract_directory(
        "custom_components/foo", str(tmp_path)
    )
    assert (tmp_path / "bar" / "__init__.py").read_text() == "correct submodule"


async def test_theme_does_not_overwrite_other_repository(
    marketplace: MarketplaceManager,
) -> None:
    """Reject a theme directory already owned by an installed repository."""
    first = ThemeRepository(marketplace, "owner-one/theme-one")
    first.data.id = "111"
    first.data.file_name = "theme.yaml"
    first.data.installed = True
    marketplace.repositories.register(first)
    second = ThemeRepository(marketplace, "owner-two/theme-two")
    second.data.id = "222"
    second.data.file_name = "theme.yaml"
    assert first.localpath == second.localpath
    with pytest.raises(MarketplaceError):
        await second.async_pre_install()


async def test_invalid_custom_repository_reports_error(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Return a WebSocket error when custom repository validation fails."""
    client = await hass_ws_client(hass)

    async def invalid(self: IntegrationRepository) -> bool:
        self.validate.errors.append("Invalid repository contents")
        return False

    with patch.object(IntegrationRepository, "validate_repository", invalid):
        await client.send_json_auto_id(
            {
                "type": "marketplace/repositories/add",
                "repository": "owner/invalid",
                "category": "integration",
            }
        )
        response = await client.receive_json()
    assert response["success"] is False
