"""Non-security functional reproductions for the Marketplace review."""

import asyncio
from copy import deepcopy
import gzip
from pathlib import Path
import shutil
from threading import Event
from typing import Any, BinaryIO
from unittest.mock import patch

from homeassistant.components.marketplace.base import MarketplaceManager
from homeassistant.components.marketplace.const import DOMAIN
from homeassistant.components.marketplace.data_client import CatalogClient
from homeassistant.components.marketplace.repositories.base import FileInformation
from homeassistant.components.marketplace.repositories.plugin import PluginRepository
from homeassistant.components.marketplace.update import RepositoryUpdateEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import get_marketplace
from .const import REPOSITORY_INTEGRATION_ID, REPOSITORY_PLUGIN_ID

from tests.typing import WebSocketGenerator


async def test_reading_beta_notes_preserves_stable_version(
    marketplace: MarketplaceManager,
) -> None:
    """Opting out of prereleases still offers the stable release after reading notes."""
    repository = marketplace.repositories.get_by_id(REPOSITORY_INTEGRATION_ID)
    repository.data.installed = True
    repository.data.installed_version = "1.0.0"
    # The fixture's 2.0.0 release is a draft; 1.0.0 is the latest stable release.
    repository.data.last_version = "1.0.0"
    repository.data.prerelease = "3.0.0"
    repository.data.show_beta = True
    repository.data.published_tags = []
    entity = RepositoryUpdateEntity(marketplace, repository)
    assert await entity.async_release_notes()
    repository.data.show_beta = False
    assert repository.display_available_version == "1.0.0"


async def test_new_download_creates_prerelease_switch(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    hass_ws_client: WebSocketGenerator,
    entity_registry: er.EntityRegistry,
) -> None:
    """Create all repository entities immediately after downloading a card."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "marketplace/repository/download", "repository": REPOSITORY_PLUGIN_ID}
    )
    response = await client.receive_json()
    assert response["success"]
    await hass.async_block_till_done()
    assert entity_registry.async_get_entity_id(
        Platform.UPDATE, DOMAIN, REPOSITORY_PLUGIN_ID
    )
    switch_immediately_after_download = entity_registry.async_get_entity_id(
        Platform.SWITCH, DOMAIN, REPOSITORY_PLUGIN_ID
    )
    await hass.config_entries.async_reload(
        marketplace.configuration.config_entry.entry_id
    )
    await hass.async_block_till_done()
    assert entity_registry.async_get_entity_id(
        Platform.SWITCH, DOMAIN, REPOSITORY_PLUGIN_ID
    )
    assert switch_immediately_after_download is not None


async def test_renamed_card_remains_installed_after_reload(
    hass: HomeAssistant, marketplace: MarketplaceManager
) -> None:
    """Keep the installed directory when GitHub renames a dashboard card repository."""
    repository = marketplace.repositories.get_by_id(REPOSITORY_PLUGIN_ID)
    await repository.async_download_repository()
    installed_directory = Path(repository.localpath)
    assert installed_directory.is_dir()
    marketplace.repositories.rename(repository, "hacs-test-org/renamed-card")
    get_data = CatalogClient.get_data

    async def renamed_catalog(
        self: CatalogClient, section: str | None, *, validate: bool
    ) -> Any:
        data = await get_data(self, section, validate=validate)
        if section == "plugin":
            data = deepcopy(data)
            data[REPOSITORY_PLUGIN_ID]["full_name"] = "hacs-test-org/renamed-card"
        return data

    with patch.object(CatalogClient, "get_data", renamed_catalog):
        await hass.config_entries.async_reload(
            marketplace.configuration.config_entry.entry_id
        )
        await hass.async_block_till_done()

    assert installed_directory.is_dir()
    repository = get_marketplace(hass).repositories.get_by_id(REPOSITORY_PLUGIN_ID)
    assert repository.data.installed


async def test_release_gzip_does_not_race_generated_gzip(
    hass: HomeAssistant, marketplace: MarketplaceManager
) -> None:
    """Keep a valid gzip file when a release ships both JavaScript and its gzip."""
    repository = PluginRepository(marketplace, "review/card")
    repository.content.single = True
    javascript = b"console.log('a dashboard card');\n" * 100
    published_gzip = gzip.compress(javascript, compresslevel=0)
    generated_header_written = asyncio.Event()
    published_gzip_written = Event()
    copyfileobj = shutil.copyfileobj
    save_file = marketplace.async_save_file

    def paused_copy(source: BinaryIO, target: BinaryIO, length: int = 0) -> None:
        hass.loop.call_soon_threadsafe(generated_header_written.set)
        assert published_gzip_written.wait(timeout=5)
        copyfileobj(source, target, length)

    async def download(url: str, **kwargs: Any) -> bytes:
        if url.endswith(".gz"):
            await generated_header_written.wait()
            return published_gzip
        return javascript

    async def save(path: str, content: Any) -> bool:
        result = await save_file(path, content)
        if path.endswith(".gz"):
            published_gzip_written.set()
        return result

    try:
        with (
            patch(
                "homeassistant.components.marketplace.base.shutil.copyfileobj",
                paused_copy,
            ),
            patch.object(marketplace, "async_download_file", download),
            patch.object(marketplace, "async_save_file", save),
        ):
            await repository._async_download_files(
                [
                    FileInformation(
                        name="card.js",
                        path="card.js",
                        url="https://example.org/card.js",
                    ),
                    FileInformation(
                        name="card.js.gz",
                        path="card.js.gz",
                        url="https://example.org/card.js.gz",
                    ),
                ]
            )
    finally:
        published_gzip_written.set()

    assert not repository.validate.errors
    assert (
        gzip.decompress((Path(repository.localpath) / "card.js.gz").read_bytes())
        == javascript
    )
