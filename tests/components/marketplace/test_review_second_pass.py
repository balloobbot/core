"""Regression reproductions for the Marketplace PR review."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

from homeassistant.components.marketplace.base import MarketplaceManager
from homeassistant.components.marketplace.repositories.base import (
    FileInformation,
    RepositoryManifest,
)
from homeassistant.components.marketplace.repositories.integration import (
    IntegrationRepository,
)
from homeassistant.components.marketplace.repositories.plugin import PluginRepository
from homeassistant.components.marketplace.repositories.template import (
    TemplateRepository,
)

from .const import REPOSITORY_INTEGRATION


async def test_selected_version_checks_final_domain_ownership(
    marketplace: MarketplaceManager,
) -> None:
    """A selected older version cannot overwrite another installed integration."""
    owner = IntegrationRepository(marketplace, "owner/existing")
    owner.data.id = "8001"
    owner.data.domain = "existing_domain"
    owner.data.installed = True
    marketplace.repositories.register(owner)
    original = Path(owner.localpath) / "__init__.py"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"existing integration")

    candidate = IntegrationRepository(marketplace, "other/renamed")
    candidate.data.id = "8002"
    candidate.data.domain = "new_domain"
    candidate.data.last_version = "2.0.0"
    candidate.data.releases = True
    candidate.content.path.remote = "custom_components/existing_domain"
    marketplace.repositories.register(candidate)

    async def download(version: str) -> None:
        await candidate._async_write_file(
            FileInformation(
                name="__init__.py",
                path="custom_components/existing_domain/__init__.py",
                url="https://example.org/integration",
            ),
            b"other integration",
        )

    with (
        patch.object(candidate, "common_update", AsyncMock(return_value=True)),
        patch.object(
            candidate,
            "get_repository_manifest",
            AsyncMock(return_value=RepositoryManifest()),
        ),
        patch.object(
            candidate,
            "async_get_integration_manifest",
            AsyncMock(return_value={"domain": "existing_domain", "name": "Old name"}),
        ),
        patch.object(candidate, "_async_download_version", download),
        patch.object(candidate, "async_post_installation", AsyncMock()),
    ):
        await candidate.async_download_repository(ref="1.0.0")

    assert original.read_bytes() == b"existing integration"


async def test_template_metadata_refresh_preserves_installed_filename(
    marketplace: MarketplaceManager,
) -> None:
    """Removing the installed version must not delete the latest version's filename."""
    repository = TemplateRepository(marketplace, "owner/template")
    repository.data.id = "8003"
    repository.data.installed = True
    repository.data.file_name = "old.jinja"
    folder = Path(repository.localpath)
    folder.mkdir(parents=True)
    (folder / "old.jinja").write_text("installed template")
    other = folder / "new.jinja"
    other.write_text("another repository's template")

    async def refresh(*args: object, **kwargs: object) -> bool:
        repository.repository_manifest.filename = "new.jinja"
        return True

    with patch.object(repository, "common_update", refresh):
        await repository.update_repository(force=True)
    await repository.uninstall()

    assert other.exists()
    assert not (folder / "old.jinja").exists()


def test_resource_url_changes_between_beta_and_rc(
    marketplace: MarketplaceManager,
) -> None:
    """Different release contents need distinct browser cache keys."""
    repository = PluginRepository(marketplace, "owner/card")
    repository.data.id = "8004"
    repository.data.file_name = "card.js"
    repository.data.installed_version = "v1.0.0-beta.1"
    before = repository.generate_dashboard_resource_url()
    repository.data.installed_version = "v1.0.0-rc.1"
    assert repository.generate_dashboard_resource_url() != before


async def test_compatible_old_release_is_supported_by_backend(
    marketplace: MarketplaceManager,
) -> None:
    """An incompatible latest release does not prevent an explicit older install."""
    repository = marketplace.repositories.get_by_full_name(REPOSITORY_INTEGRATION)
    repository.data.last_version = "2.0.0"
    repository.data.releases = True
    repository.repository_manifest.homeassistant = "9999.1.0"
    assert not repository.can_download
    await repository.async_download_repository(ref="1.0.0")
    assert repository.data.installed_version == "1.0.0"
