"""Reproduce accidental package and installation corruption in Marketplace."""

from contextlib import suppress
import io
import json
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, patch
import zipfile

import pytest

from homeassistant.components.marketplace.base import MarketplaceManager
from homeassistant.components.marketplace.enums import RepositoryCategory
from homeassistant.components.marketplace.exceptions import MarketplaceError
from homeassistant.components.marketplace.repositories.base import Repository
from homeassistant.components.marketplace.utils.backup import (
    Backup,
    restore_interrupted_backups,
)
from homeassistant.components.marketplace.utils.storage import (
    async_load_from_storage,
    get_storage_for_key,
)
from homeassistant.components.marketplace.utils.validate import (
    VALIDATE_FETCHED_V2_REPO_DATA,
)
from homeassistant.config import async_hass_config_yaml
from homeassistant.core import HomeAssistant
from homeassistant.loader import Integration
from homeassistant.util.file import WriteError

from . import mocked_response
from .conftest import MarketplaceResponses
from .const import REPOSITORY_INTEGRATION_ID


def _zip(files: dict[str, str]) -> bytes:
    """Create a release asset with the supplied member names."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


async def _working_integration(marketplace: MarketplaceManager) -> Repository:
    """Start with real installed files and a manifest accepted by the loader."""
    repository = marketplace.repositories.get_by_id(REPOSITORY_INTEGRATION_ID)
    await repository.async_download_repository()
    local = Path(repository.localpath)
    (local / "__init__.py").write_text("# working old integration\n")
    (local / "manifest.json").write_text(
        json.dumps(
            {
                "domain": "example",
                "name": "Example",
                "version": "1.0.0",
                "config_flow": False,
            }
        )
    )
    return repository


def _release(
    responses: MarketplaceResponses,
    repository: Repository,
    files: dict[str, str],
    **manifest: str | bool,
) -> None:
    """Publish a ZIP release at the catalog's next version."""
    repository.data.last_version = "2.0.0"
    raw = (
        f"https://raw.githubusercontent.com/{repository.data.full_name}/2.0.0/hacs.json"
    )
    responses.add(
        raw,
        mocked_response(
            raw,
            json_content={
                "name": "Example",
                "zip_release": True,
                "filename": "release.zip",
                **manifest,
            },
        ),
        keep=True,
    )
    integration_manifest = f"https://raw.githubusercontent.com/{repository.data.full_name}/2.0.0/custom_components/example/manifest.json"
    responses.add(
        integration_manifest,
        mocked_response(
            integration_manifest,
            json_content={
                "domain": "example",
                "name": "Example",
                "version": "2.0.0",
                "config_flow": False,
            },
        ),
        keep=True,
    )
    asset = f"https://github.com/{repository.data.full_name}/releases/download/2.0.0/release.zip"
    responses.add(asset, mocked_response(asset, content=_zip(files)), keep=True)


async def test_wrong_zip_layout_preserves_working_integration(
    marketplace: MarketplaceManager, response_mocker: MarketplaceResponses
) -> None:
    """An extra enclosing directory must not replace a working integration."""
    repository = await _working_integration(marketplace)
    local = Path(repository.localpath)
    original = (local / "manifest.json").read_text()
    _release(
        response_mocker,
        repository,
        {"example/manifest.json": original, "example/__init__.py": ""},
    )
    with suppress(MarketplaceError):
        await repository.async_download_repository()
    assert (repository.data.installed_version, (local / "manifest.json").is_file()) == (
        "1.0.0",
        True,
    )
    assert (local / "manifest.json").read_text() == original


async def test_missing_manifest_version_preserves_working_integration(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    response_mocker: MarketplaceResponses,
) -> None:
    """Reject a manifest the Home Assistant loader cannot load before updating."""
    repository = await _working_integration(marketplace)
    root = ModuleType("custom_components")
    root.__path__ = [hass.config.path("custom_components")]
    assert (
        await hass.async_add_executor_job(
            Integration.resolve_from_root, hass, root, "example"
        )
        is not None
    )
    manifest = {"domain": "example", "name": "Example", "config_flow": False}
    _release(
        response_mocker,
        repository,
        {"manifest.json": json.dumps(manifest), "__init__.py": ""},
    )
    raw = f"https://raw.githubusercontent.com/{repository.data.full_name}/2.0.0/custom_components/example/manifest.json"
    response_mocker.add(raw, mocked_response(raw, json_content=manifest), keep=True)
    with suppress(MarketplaceError):
        await repository.async_download_repository()
    loaded = await hass.async_add_executor_job(
        Integration.resolve_from_root, hass, root, "example"
    )
    assert loaded is not None
    assert repository.data.installed_version == "1.0.0"


async def test_persistent_root_does_not_fake_successful_update(
    marketplace: MarketplaceManager, response_mocker: MarketplaceResponses
) -> None:
    """Preserving '.' must not restore all old code while recording a new version."""
    repository = await _working_integration(marketplace)
    local = Path(repository.localpath)
    manifest = json.loads((local / "manifest.json").read_text())
    manifest["version"] = "2.0.0"
    _release(
        response_mocker,
        repository,
        {
            "manifest.json": json.dumps(manifest),
            "__init__.py": "# new release\n",
        },
        persistent_directory=".",
    )
    with suppress(MarketplaceError):
        await repository.async_download_repository()
    assert (
        repository.data.installed_version != "2.0.0"
        or (local / "__init__.py").read_text() == "# new release\n"
    )


async def test_invalid_theme_yaml_preserves_loadable_configuration(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    response_mocker: MarketplaceResponses,
    config_dir: Path,
) -> None:
    """A theme update cannot leave the main Home Assistant YAML unloadable."""
    (config_dir / "configuration.yaml").write_text(
        "frontend:\n  themes: !include_dir_merge_named themes\n"
    )
    repository = marketplace.repositories.get_by_id("1296266")
    await repository.async_download_repository()
    await hass.async_block_till_done()
    assert await async_hass_config_yaml(hass)
    repository.data.last_version = "2.0.0"
    archive = _zip({"repo-2.0.0/themes/example.yaml": "Example:\n  primary-color: [\n"})
    url = f"https://github.com/{repository.data.full_name}/archive/refs/tags/2.0.0.zip"
    response_mocker.add(url, mocked_response(url, content=archive), keep=True)
    with suppress(MarketplaceError):
        await repository.async_download_repository()
    await hass.async_block_till_done()
    assert await async_hass_config_yaml(hass)


def test_interrupted_symlink_update_preserves_source(
    marketplace: MarketplaceManager, config_dir: Path
) -> None:
    """Restart recovery must restore the symlink without deleting its target."""
    source = config_dir / "development/example"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("source checkout")
    local = config_dir / "custom_components/example"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.symlink_to(source, target_is_directory=True)
    backup = Backup(marketplace, local)
    backup.create()
    local.mkdir()
    (local / "__init__.py").write_text("partial update")
    restore_interrupted_backups(marketplace)
    assert not source.is_symlink()
    assert (source / "__init__.py").read_text() == "source checkout"
    assert local.is_symlink()


async def test_failed_adoption_preserves_manual_install(
    marketplace: MarketplaceManager, response_mocker: MarketplaceResponses
) -> None:
    """A failed first Marketplace download must not destroy a manual installation."""
    repository = marketplace.repositories.get_by_id(REPOSITORY_INTEGRATION_ID)
    assert not repository.data.installed
    local = Path(repository.localpath)
    local.mkdir(parents=True)
    original = local / "__init__.py"
    original.write_text("# manually installed working version\n")
    _release(
        response_mocker,
        repository,
        {"__init__.py": "# new code\n", "broken.py": "assert 2 == 2"},
    )
    broken_archive = _zip(
        {"__init__.py": "# new code\n", "broken.py": "assert 2 == 2"}
    ).replace(b"assert 2 == 2", b"assert 2 == 3")
    url = f"https://github.com/{repository.data.full_name}/releases/download/2.0.0/release.zip"
    response_mocker.add(url, mocked_response(url, content=broken_archive), keep=True)
    with pytest.raises(MarketplaceError):
        await repository.async_download_repository()
    assert original.read_text() == "# manually installed working version\n"


async def test_storage_failure_keeps_files_and_version_consistent(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    response_mocker: MarketplaceResponses,
) -> None:
    """Do not permanently replace files when saving their installed version fails."""
    repository = await _working_integration(marketplace)
    manifest = {
        "domain": "example",
        "name": "Example",
        "version": "2.0.0",
        "config_flow": False,
    }
    _release(
        response_mocker,
        repository,
        {"manifest.json": json.dumps(manifest), "__init__.py": "# new code\n"},
    )
    store = get_storage_for_key(hass, "repositories")
    with (
        patch.object(
            store,
            "_async_write_data",
            AsyncMock(side_effect=WriteError("No space left on device")),
        ),
        suppress(MarketplaceError, WriteError),
    ):
        await repository.async_download_repository()
    stored = await async_load_from_storage(hass, "repositories")
    on_disk = json.loads((Path(repository.localpath) / "manifest.json").read_text())
    assert stored[repository.data.id]["version_installed"] == on_disk["version"]


async def test_failed_zip_download_removes_temporary_archive(
    marketplace: MarketplaceManager,
    response_mocker: MarketplaceResponses,
    config_dir: Path,
) -> None:
    """Repeated retries of an accidentally truncated release must not fill /tmp."""
    repository = await _working_integration(marketplace)
    _release(response_mocker, repository, {"__init__.py": "# update\n"})
    url = f"https://github.com/{repository.data.full_name}/releases/download/2.0.0/release.zip"
    response_mocker.add(
        url, mocked_response(url, content=b"PK\x03\x04truncated"), keep=True
    )
    scratch = config_dir / "download-temporary-directory"
    scratch.mkdir()
    with (
        patch(
            "homeassistant.components.marketplace.repositories.base.tempfile.mkdtemp",
            return_value=str(scratch),
        ),
        pytest.raises(MarketplaceError),
    ):
        await repository.async_download_repository()
    assert not scratch.exists()


async def test_theme_removal_preserves_unowned_root_yaml(
    marketplace: MarketplaceManager, config_dir: Path
) -> None:
    """Uninstall may only delete the downloaded theme's own files."""
    repository = marketplace.repositories.get_by_id("1296266")
    await repository.async_download_repository()
    manual = config_dir / "themes/theme-basic.yaml"
    manual.write_text("Handwritten Theme:\n  primary-color: red\n")
    assert manual.parent != Path(repository.localpath)
    await repository.uninstall()
    assert not Path(repository.localpath).exists()
    assert manual.exists()


async def test_integer_catalog_timestamp_does_not_break_storage(
    marketplace: MarketplaceManager,
) -> None:
    """A schema-valid integer timestamp must not poison every repository save."""
    repository = marketplace.repositories.get_by_id(REPOSITORY_INTEGRATION_ID)
    original = repository.data.last_fetched
    data = await marketplace.data_client.get_data(
        RepositoryCategory.INTEGRATION, validate=True
    )
    data[repository.data.id]["last_fetched"] = 2000000000
    VALIDATE_FETCHED_V2_REPO_DATA[RepositoryCategory.INTEGRATION](
        data[repository.data.id]
    )
    try:
        with patch.object(
            marketplace.data_client, "get_data", AsyncMock(return_value=data)
        ):
            await marketplace.async_get_category_repositories_from_catalog(
                RepositoryCategory.INTEGRATION
            )
        await marketplace.data.async_write()
    finally:
        repository.data.last_fetched = original


async def test_valid_zip_control_remains_loadable(
    hass: HomeAssistant,
    marketplace: MarketplaceManager,
    response_mocker: MarketplaceResponses,
) -> None:
    """A correctly packaged ZIP updates both loadable code and stored version."""
    repository = await _working_integration(marketplace)
    manifest = {
        "domain": "example",
        "name": "Example",
        "version": "2.0.0",
        "config_flow": False,
    }
    _release(
        response_mocker,
        repository,
        {"manifest.json": json.dumps(manifest), "__init__.py": "# new code\n"},
    )
    await repository.async_download_repository()
    root = ModuleType("custom_components")
    root.__path__ = [hass.config.path("custom_components")]
    integration = await hass.async_add_executor_job(
        Integration.resolve_from_root, hass, root, "example"
    )
    assert integration is not None
    assert integration.version == "2.0.0"
    stored = await async_load_from_storage(hass, "repositories")
    assert stored[repository.data.id]["version_installed"] == "2.0.0"
