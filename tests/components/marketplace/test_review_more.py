"""Additional functional regression reproductions for the Marketplace review."""

from contextlib import suppress
from datetime import timedelta
import io
from pathlib import Path
from unittest.mock import AsyncMock, patch
import zipfile

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.marketplace.base import MarketplaceManager
from homeassistant.components.marketplace.exceptions import MarketplaceError
from homeassistant.components.marketplace.repositories.base import (
    RepositoryArchive,
    RepositoryManifest,
)
from homeassistant.components.marketplace.repositories.plugin import PluginRepository
from homeassistant.components.marketplace.repositories.template import (
    TemplateRepository,
)
from homeassistant.components.marketplace.repositories.theme import ThemeRepository
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import get_marketplace
from .const import REPOSITORY_INTEGRATION_ID

from tests.common import async_fire_time_changed


@pytest.mark.parametrize("github_token", [None])
async def test_config_flow_survives_reload(
    hass: HomeAssistant, marketplace: MarketplaceManager
) -> None:
    """A downloaded integration keeps its ability to be configured from the UI."""
    repository = marketplace.repositories.get_by_id(REPOSITORY_INTEGRATION_ID)
    await repository.async_download_repository()
    assert repository.data.config_flow
    await hass.config_entries.async_reload(
        marketplace.configuration.config_entry.entry_id
    )
    await hass.async_block_till_done()
    repository = get_marketplace(hass).repositories.get_by_id(REPOSITORY_INTEGRATION_ID)
    assert repository.data.installed
    assert repository.data.config_flow


async def test_pending_restart_survives_entry_reload(
    hass: HomeAssistant, marketplace: MarketplaceManager
) -> None:
    """Reloading Marketplace does not reload downloaded Python integrations."""
    repository = marketplace.repositories.get_by_id(REPOSITORY_INTEGRATION_ID)
    await repository.async_download_repository()
    assert repository.pending_restart
    await hass.config_entries.async_reload(
        marketplace.configuration.config_entry.entry_id
    )
    await hass.async_block_till_done()
    repository = get_marketplace(hass).repositories.get_by_id(REPOSITORY_INTEGRATION_ID)
    assert repository.pending_restart


async def test_readme_returns_installed_version(
    marketplace: MarketplaceManager,
) -> None:
    """Control for the version the frontend must use for relative README links."""
    repository = marketplace.repositories.get_by_id(REPOSITORY_INTEGRATION_ID)
    repository.data.installed = True
    repository.data.installed_version = "1.0.0"
    repository.data.last_version = "2.0.0"
    with patch.object(
        marketplace, "async_download_file", AsyncMock(return_value=b"![old](old.png)")
    ) as download:
        assert (
            await repository.get_documentation(filename="README.md")
            == "![old](old.png)"
        )
    assert "/1.0.0/README.md" in download.call_args.args[0]


def _archive(files: dict[str, str]) -> RepositoryArchive:
    """Make an archive with the top-level directory GitHub adds."""
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        for name, value in files.items():
            archive.writestr(f"repo-main/{name}", value)
    return RepositoryArchive(content.getvalue())


async def test_template_only_installs_root_file(
    marketplace: MarketplaceManager,
) -> None:
    """An example with the same basename must not replace the real template."""
    repository = TemplateRepository(marketplace, "owner/template")
    repository.repository_manifest.filename = "a.jinja"
    archive = _archive(
        {"a.jinja": "real template", "examples/a.jinja": "example template"}
    )
    repository.tree = archive.tree
    repository.treefiles = [entry.path for entry in archive.tree]
    repository.resolve_archive_content()
    await repository._async_write_archive_content(archive)
    assert (Path(repository.localpath) / "a.jinja").read_text() == "real template"


async def test_theme_content_in_root_validates(marketplace: MarketplaceManager) -> None:
    """A content_in_root theme need not also contain a themes subdirectory."""
    repository = ThemeRepository(marketplace, "owner/theme")
    repository.repository_manifest.content_in_root = True
    archive = _archive(
        {"theme.yaml": "My Theme: {}", "hacs.json": '{"content_in_root": true}'}
    )
    repository.tree = archive.tree
    repository.treefiles = [entry.path for entry in archive.tree]
    with patch.object(repository, "common_validate", AsyncMock()):
        assert await repository.validate_repository()


async def test_catalog_refresh_notices_removal(
    hass: HomeAssistant, marketplace: MarketplaceManager, freezer: FrozenDateTimeFactory
) -> None:
    """An ordinary catalog removal is noticed without restarting Home Assistant."""
    repository = marketplace.repositories.get_by_id(REPOSITORY_INTEGRATION_ID)
    await repository.async_download_repository()
    get_data = marketplace.data_client.get_data
    fetched: list[str | None] = []

    async def catalog(section: str | None, *, validate: bool):
        fetched.append(section)
        if section == "removed":
            return [
                {
                    "repository": repository.data.full_name,
                    "removal_type": "removed",
                    "reason": "Unmaintained",
                }
            ]
        data = await get_data(section, validate=validate)
        if section == "integration":
            data.pop(REPOSITORY_INTEGRATION_ID, None)
        return data

    with patch.object(marketplace.data_client, "get_data", catalog):
        freezer.tick(timedelta(hours=6, seconds=1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    assert "integration" in fetched
    assert marketplace.repositories.is_removed(repository.data.full_name)


async def test_missing_root_card_does_not_report_success(
    marketplace: MarketplaceManager,
) -> None:
    """A root filename absent from the release cannot count as a successful update."""
    repository = PluginRepository(marketplace, "owner/card")
    repository.data.id = "8100"
    repository.data.file_name = "card.js"
    repository.data.releases = True
    repository.data.last_version = "2.0.0"
    repository.data.installed = True
    repository.data.installed_version = "1.0.0"
    repository.content.path.remote = ""
    local = Path(repository.localpath)
    local.mkdir(parents=True)
    (local / "card.js").write_text("working card")
    repository.tree = _archive({"other.js": "unrelated asset"}).tree
    repository.repository_manifest = RepositoryManifest.from_dict(
        {"content_in_root": True, "filename": "card.js"}
    )
    with (
        patch.object(repository, "common_update", AsyncMock(return_value=True)),
        patch.object(
            repository,
            "get_repository_manifest",
            AsyncMock(return_value=repository.repository_manifest),
        ),
        suppress(MarketplaceError),
    ):
        await repository.async_download_repository(ref="2.0.0")
    assert (repository.data.installed_version, (local / "card.js").exists()) == (
        "1.0.0",
        True,
    )
    assert (local / "card.js").read_text() == "working card"
