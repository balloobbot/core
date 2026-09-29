"""Check file ownership and recovery at the boundaries of the review fixes."""

from contextlib import suppress
from pathlib import Path

from homeassistant.components.frontend import CONFIG_SCHEMA
from homeassistant.components.marketplace.base import MarketplaceManager
from homeassistant.components.marketplace.exceptions import MarketplaceError
from homeassistant.components.marketplace.repositories.template import (
    TemplateRepository,
)
from homeassistant.components.marketplace.utils.backup import (
    Backup,
    restore_interrupted_backups,
)
from homeassistant.config import async_hass_config_yaml
from homeassistant.core import HomeAssistant

from . import mocked_response
from .conftest import MarketplaceResponses
from .test_review_corruption import _zip


def test_interrupted_relative_symlink_update_restores_link(
    marketplace: MarketplaceManager, config_dir: Path
) -> None:
    """Moving a relative symlink into the backup must not discard its recovery."""
    source = config_dir / "development/example"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("source checkout")
    local = config_dir / "custom_components/example"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.symlink_to("../development/example", target_is_directory=True)
    assert local.is_dir()

    Backup(marketplace, local).create()
    local.mkdir()
    (local / "__init__.py").write_text("partial update")
    restore_interrupted_backups(marketplace)

    assert (source / "__init__.py").read_text() == "source checkout"
    assert local.is_symlink()
    assert local.resolve() == source


async def test_template_rename_preserves_another_installed_template(
    marketplace: MarketplaceManager,
) -> None:
    """Updating to a new filename cannot take another repository's template."""
    owner = TemplateRepository(marketplace, "owner/template")
    owner.data.id = "8801"
    owner.data.installed = True
    owner.data.file_name = "shared.jinja"
    marketplace.repositories.register(owner)
    folder = Path(owner.localpath)
    folder.mkdir(parents=True)
    (folder / "shared.jinja").write_text("owner's installed macros")

    candidate = TemplateRepository(marketplace, "other/template")
    candidate.data.id = "8802"
    candidate.data.installed = True
    candidate.data.file_name = "old.jinja"
    marketplace.repositories.register(candidate)
    (folder / "old.jinja").write_text("candidate's old macros")
    candidate.repository_manifest.filename = "shared.jinja"
    candidate.treefiles = ["shared.jinja"]
    candidate.resolve_content()

    async def download() -> None:
        (folder / "shared.jinja").write_text("candidate's new macros")

    with suppress(MarketplaceError):
        await candidate._async_write_content(download)

    assert (folder / "shared.jinja").read_text() == "owner's installed macros"
    assert (folder / "old.jinja").read_text() == "candidate's old macros"


async def test_parseable_invalid_theme_preserves_frontend_configuration(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    response_mocker: MarketplaceResponses,
    config_dir: Path,
) -> None:
    """Parsing YAML is insufficient when its theme variables fail frontend setup."""
    (config_dir / "configuration.yaml").write_text(
        "frontend:\n  themes: !include_dir_merge_named themes\n"
    )
    repository = marketplace.repositories.get_by_id("1296266")
    await repository.async_download_repository()
    await hass.async_block_till_done()
    CONFIG_SCHEMA(await async_hass_config_yaml(hass))

    repository.data.last_version = "2.0.0"
    archive = _zip(
        {"repo-2.0.0/themes/example.yaml": "Example:\n  primary-color: [red, blue]\n"}
    )
    url = f"https://github.com/{repository.data.full_name}/archive/refs/tags/2.0.0.zip"
    response_mocker.add(url, mocked_response(url, content=archive), keep=True)
    with suppress(MarketplaceError):
        await repository.async_download_repository()
    await hass.async_block_till_done()

    CONFIG_SCHEMA(await async_hass_config_yaml(hass))
