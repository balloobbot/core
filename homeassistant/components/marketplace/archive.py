"""Integrations installed from a ZIP archive that a user uploaded."""

from dataclasses import asdict, dataclass, field
from http import HTTPStatus
import io
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from typing import TYPE_CHECKING, Any
import zipfile

from aiohttp import web
from aiohttp.hdrs import CONTENT_DISPOSITION

from homeassistant import components
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.components.http import KEY_HASS, HomeAssistantView, require_admin
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
from homeassistant.util import dt as dt_util
from homeassistant.util.json import json_loads_object

from .const import DOMAIN, RESTART_ISSUE_PREFIX
from .enums import MarketplaceSignal, RepositoryCategory, RepositoryFile
from .exceptions import (
    MarketplaceError,
    ReplacesBuiltInNotConfirmedError,
    ReplacesRepositoryNotConfirmedError,
    RepositoryBusyError,
)
from .repositories.base import _check_archive_size
from .repositories.integration import (
    _check_loadable_manifest,
    _validated_domain,
    async_reload_custom_components,
    is_known_to_the_loader,
)
from .utils.backup import Backup
from .utils.file_system import async_remove_directory, async_run_to_completion
from .utils.logger import LOGGER
from .utils.path import resolve_in_directory
from .utils.storage import async_load_from_storage, async_save_to_storage
from .utils.validate import VALID_DOMAIN

if TYPE_CHECKING:
    from .base import MarketplaceManager
    from .repositories.base import Repository

STORAGE_KEY = "archives"

# Only a version like this goes into the file name of a download
SAFE_VERSION = re.compile(r"^[\w.+-]+$")


@dataclass(slots=True)
class ArchiveIntegration:
    """An integration installed from an uploaded ZIP archive."""

    domain: str
    name: str
    version: str
    config_flow: bool
    installed_at: str
    # Not stored, a restart is what it waits for
    pending_restart: bool = field(default=False, compare=False)

    def as_stored(self) -> dict[str, Any]:
        """Return what is kept in storage."""
        stored = asdict(self)
        del stored["pending_restart"]
        return stored


def _integration_root(names: list[str]) -> str:
    """Return the directory of the archive with the manifest.json, "" for the root.

    The shallowest manifest.json belongs to the integration. A release asset
    has it at the root, a repository archive in custom_components/<domain>.
    """
    manifests = [
        PurePosixPath(name)
        for name in names
        if PurePosixPath(name).name == RepositoryFile.MANIFEST_JSON
    ]
    if not manifests:
        raise MarketplaceError(
            translation_domain=DOMAIN, translation_key="archive_without_manifest"
        )

    depth = min(len(manifest.parts) for manifest in manifests)
    shallowest = [manifest for manifest in manifests if len(manifest.parts) == depth]
    if len(shallowest) > 1:
        raise MarketplaceError(
            translation_domain=DOMAIN,
            translation_key="archive_manifest_ambiguous",
            translation_placeholders={
                "manifests": ", ".join(sorted(str(path) for path in shallowest))
            },
        )

    parent = shallowest[0].parent.as_posix()
    return "" if parent == "." else parent


def _unpack(path: Path, staging: Path) -> dict[str, Any]:
    """Extract the integration of the archive into staging, return its manifest."""
    try:
        with zipfile.ZipFile(path) as archive:
            _check_archive_size(archive)
            root = _integration_root(archive.namelist())
            prefix = f"{root}/" if root else ""
            members = []
            for member in archive.infolist():
                if not member.filename.startswith(prefix):
                    continue
                if not (relative := member.filename.removeprefix(prefix)):
                    continue
                member.filename = relative
                resolve_in_directory(staging, relative)
                members.append(member)
            archive.extractall(staging, members)
    except zipfile.BadZipFile as exception:
        raise MarketplaceError(
            translation_domain=DOMAIN,
            translation_key="archive_not_zip",
            translation_placeholders={"error": str(exception)},
        ) from exception

    try:
        return json_loads_object(
            (staging / RepositoryFile.MANIFEST_JSON).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exception:
        raise MarketplaceError(
            translation_domain=DOMAIN,
            translation_key="installed_manifest_unusable",
            translation_placeholders={"error": str(exception)},
        ) from exception


class ArchiveIntegrations:
    """The integrations installed from uploaded ZIP archives."""

    def __init__(self, marketplace: MarketplaceManager) -> None:
        """Initialize."""
        self.marketplace = marketplace
        self.hass = marketplace.hass
        self._installed: dict[str, ArchiveIntegration] = {}

    @property
    def list_installed(self) -> list[ArchiveIntegration]:
        """Return the installed integrations."""
        return list(self._installed.values())

    def get(self, domain: str) -> ArchiveIntegration | None:
        """Return the integration of a domain, if an archive installed it."""
        return self._installed.get(domain)

    def _target(self, domain: str) -> Path:
        """Return the directory an integration is installed to."""
        return Path(self.marketplace.core.config_path, "custom_components", domain)

    async def async_load(self) -> None:
        """Load what is stored, without integrations removed from the disk."""
        stored: dict[str, dict[str, Any]] = await async_load_from_storage(
            self.hass, STORAGE_KEY
        )

        def _existing() -> set[str]:
            return {domain for domain in stored if self._target(domain).is_dir()}

        existing = await self.hass.async_add_executor_job(_existing)
        self._installed = {
            domain: ArchiveIntegration(**data)
            for domain, data in stored.items()
            if domain in existing
        }

    async def _async_save(self) -> None:
        """Store the installed integrations, and tell the panel."""
        await async_save_to_storage(
            self.hass,
            STORAGE_KEY,
            {
                domain: integration.as_stored()
                for domain, integration in self._installed.items()
            },
        )
        self.marketplace.async_dispatch(MarketplaceSignal.REPOSITORY, {})

    async def async_forget(self, domain: str) -> None:
        """Forget an integration, a repository installed over it."""
        if self._installed.pop(domain, None) is not None:
            await self._async_save()

    async def _async_replaces_built_in(self, domain: str) -> bool:
        """Return if the domain belongs to an integration of Home Assistant."""
        manifest = Path(components.__file__).parent / domain / "manifest.json"
        return await self.hass.async_add_executor_job(manifest.is_file)

    def _owner(self, domain: str) -> Repository | None:
        """Return the repository of the Marketplace that installed the domain."""
        for repository in self.marketplace.repositories.list_installed:
            if (
                repository.data.category == RepositoryCategory.INTEGRATION
                and repository.data.domain == domain
            ):
                return repository
        return None

    async def async_install(
        self,
        file_id: str,
        *,
        confirm_replace_built_in: bool = False,
        confirm_replace_repository: bool = False,
    ) -> ArchiveIntegration:
        """Install the integration in an uploaded archive, or update it."""
        staging = Path(await self.hass.async_add_executor_job(tempfile.mkdtemp))
        try:
            return await self._async_install(
                file_id,
                staging,
                confirm_replace_built_in=confirm_replace_built_in,
                confirm_replace_repository=confirm_replace_repository,
            )
        finally:
            await self.hass.async_add_executor_job(shutil.rmtree, staging, True)

    async def _async_install(
        self,
        file_id: str,
        staging: Path,
        *,
        confirm_replace_built_in: bool,
        confirm_replace_repository: bool,
    ) -> ArchiveIntegration:
        """Install from the archive, unpacked into staging first."""

        def _unpack_upload() -> tuple[str, dict[str, Any]]:
            # The context manager removes the upload, so it runs in the executor
            with process_uploaded_file(self.hass, file_id) as path:
                return path.name, _unpack(path, staging)

        try:
            file_name, manifest = await async_run_to_completion(
                self.hass, _unpack_upload
            )
        except ValueError as exception:
            raise MarketplaceError(
                translation_domain=DOMAIN,
                translation_key="uploaded_file_not_found",
                translation_placeholders={"file_id": file_id},
            ) from exception

        domain = _validated_domain(manifest.get("domain"))
        if (owner := self._owner(domain)) is not None:
            if not confirm_replace_repository:
                raise ReplacesRepositoryNotConfirmedError(owner.data.full_name, domain)
        # The install of the repository confirmed it already
        elif (
            domain not in self._installed
            and not confirm_replace_built_in
            and await self._async_replaces_built_in(domain)
        ):
            raise ReplacesBuiltInNotConfirmedError(file_name, domain)

        target = self._target(domain)

        def _write() -> None:
            target.parent.mkdir(exist_ok=True)
            backup = Backup(marketplace=self.marketplace, local_path=target)
            backup.create()
            try:
                shutil.copytree(staging, target)
                _check_loadable_manifest(target, domain)
            except BaseException:
                if backup.is_first_install:
                    backup.remove_first_install()
                backup.restore()
                backup.cleanup()
                raise
            backup.cleanup()

        async with self.marketplace.filesystem_lock:
            # Its install could have finished or started meanwhile
            if (owner := self._owner(domain)) is not None:
                if not confirm_replace_repository:
                    raise ReplacesRepositoryNotConfirmedError(
                        owner.data.full_name, domain
                    )
                if owner.installing:
                    raise RepositoryBusyError(owner.data.full_name)
            try:
                await async_run_to_completion(self.hass, _write)
            except OSError as exception:
                raise MarketplaceError(
                    translation_domain=DOMAIN,
                    translation_key="content_write_failed",
                    translation_placeholders={"error": str(exception)},
                ) from exception
            if owner is not None:
                await owner.async_release_install()

        name = manifest.get("name")
        integration = ArchiveIntegration(
            domain=domain,
            name=name if isinstance(name, str) and name else domain,
            version=str(manifest["version"]),
            config_flow=manifest.get("config_flow") is True,
            installed_at=dt_util.utcnow().isoformat(),
        )
        LOGGER.info("Installed %s %s from %s", domain, integration.version, file_name)

        integration.pending_restart = True
        if integration.config_flow:
            found = await async_reload_custom_components(self.hass)
            # New code is found like any other integration, code the loader
            # already knows keeps running until a restart
            integration.pending_restart = (
                is_known_to_the_loader(self.hass, domain) or domain not in found
            )
        if integration.pending_restart:
            async_create_issue(
                hass=self.hass,
                domain=DOMAIN,
                issue_id=f"{RESTART_ISSUE_PREFIX}archive_{domain}",
                is_fixable=True,
                issue_domain=domain,
                severity=IssueSeverity.WARNING,
                translation_key="restart_required",
                translation_placeholders={"name": integration.name},
            )

        self._installed[domain] = integration
        await self._async_save()
        if owner is not None:
            await self.marketplace.data.async_write()
        return integration

    async def async_uninstall(self, domain: str) -> None:
        """Remove an integration that an archive installed."""
        integration = self._installed[domain]

        async with self.marketplace.filesystem_lock:
            # Code this run loaded keeps running until a restart
            loaded = is_known_to_the_loader(self.hass, domain)
            await async_remove_directory(
                self.hass, self._target(domain), missing_ok=True
            )

        del self._installed[domain]
        if integration.config_flow:
            await async_reload_custom_components(self.hass)
        if loaded or not integration.config_flow:
            async_create_issue(
                hass=self.hass,
                domain=DOMAIN,
                issue_id=f"{RESTART_ISSUE_PREFIX}archive_{domain}_uninstall",
                is_fixable=True,
                issue_domain=domain,
                severity=IssueSeverity.WARNING,
                translation_key="restart_required_uninstall",
                translation_placeholders={"name": integration.name},
            )
        await self._async_save()


def _pack(directory: Path, domain: str) -> tuple[bytes, str | None]:
    """Return the integration as a ZIP archive, and its version."""
    manifest_path = directory / RepositoryFile.MANIFEST_JSON
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(directory)
            # A symlink can point anywhere on the system, bytecode is rebuilt
            if (
                path.is_symlink()
                or not path.is_file()
                or "__pycache__" in relative.parts
            ):
                continue
            archive.write(path, f"custom_components/{domain}/{relative.as_posix()}")

    try:
        version = json_loads_object(manifest_path.read_text(encoding="utf-8")).get(
            "version"
        )
    except ValueError:
        version = None
    if not isinstance(version, str) or not SAFE_VERSION.match(version):
        version = None
    return buffer.getvalue(), version


class IntegrationArchiveView(HomeAssistantView):
    """Download any installed custom integration as a ZIP archive.

    The archive installs again with an upload, also after changes to it.
    """

    url = "/api/marketplace/integration/{domain}/archive"
    name = "api:marketplace:integration_archive"

    @require_admin
    async def get(self, request: web.Request, domain: str) -> web.Response:
        """Return the ZIP archive of the integration."""
        if not VALID_DOMAIN.match(domain):
            return self.json_message("Invalid domain", HTTPStatus.NOT_FOUND)

        hass = request.app[KEY_HASS]
        directory = Path(hass.config.path("custom_components", domain))
        try:
            content, version = await hass.async_add_executor_job(
                _pack, directory, domain
            )
        except FileNotFoundError:
            return self.json_message(
                "Custom integration not found", HTTPStatus.NOT_FOUND
            )

        file_name = f"{domain}-{version}.zip" if version else f"{domain}.zip"
        return web.Response(
            body=content,
            content_type="application/zip",
            headers={CONTENT_DISPOSITION: f'attachment; filename="{file_name}"'},
        )
