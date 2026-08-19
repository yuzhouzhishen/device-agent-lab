from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, SecretStr, model_validator

from device_agent_lab.audit import (
    AuditLog,
    DeviceSwitchAuditRecord,
    JsonlAuditLog,
)
from device_agent_lab.runtime import (
    DeviceOpsRuntime,
    RuntimeSettings,
    create_runtime,
)


RuntimeFactory = Callable[[RuntimeSettings], Awaitable[DeviceOpsRuntime]]


class DeviceProfile(BaseModel):
    """One pre-authorized device target from a private local catalog."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,47}$")
    label: str = Field(min_length=1, max_length=48)
    backend: Literal["mock", "local_mcp", "xdp"] = "xdp"
    device_id: str = Field(min_length=1, max_length=64, repr=False)
    allowed_ports: list[int] = Field(min_length=1)
    xdp_mcp_url: SecretStr | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_target(self) -> DeviceProfile:
        ports = sorted(set(self.allowed_ports))
        if any(port <= 0 for port in ports):
            raise ValueError("allowed_ports must contain positive integers")
        self.allowed_ports = ports
        if self.backend == "xdp" and self.xdp_mcp_url is None:
            raise ValueError("xdp_mcp_url is required for an xdp profile")
        if self.xdp_mcp_url is not None:
            parsed = urlparse(self.xdp_mcp_url.get_secret_value())
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or not parsed.path.rstrip("/").endswith(("/mcp", "/sse"))
            ):
                raise ValueError(
                    "xdp_mcp_url must be an absolute MCP endpoint"
                )
        return self

    def apply(self, base: RuntimeSettings) -> RuntimeSettings:
        url = (
            self.xdp_mcp_url.get_secret_value()
            if self.xdp_mcp_url is not None
            else None
        )
        return replace(
            base,
            backend=self.backend,
            device_id=self.device_id,
            allowed_ports=tuple(self.allowed_ports),
            xdp_mcp_url=url,
            conversation_namespace=self.id,
        )


class DeviceProfileCatalog(BaseModel):
    """Validated private profiles and the preferred initial selection."""

    active_profile: str | None = None
    profiles: list[DeviceProfile] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_catalog(self) -> DeviceProfileCatalog:
        profile_ids = [profile.id for profile in self.profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("device profile ids must be unique")
        if self.active_profile is not None and self.active_profile not in profile_ids:
            raise ValueError("active_profile must reference a configured profile")
        return self

    def get(self, profile_id: str) -> DeviceProfile | None:
        return next(
            (profile for profile in self.profiles if profile.id == profile_id),
            None,
        )


class DeviceProfileView(BaseModel):
    id: str
    label: str
    backend: Literal["mock", "local_mcp", "xdp"]
    allowed_ports: list[int]
    active: bool


class DeviceInventory(BaseModel):
    active_profile_id: str
    switching_enabled: bool
    devices: list[DeviceProfileView]


class DevicePortSnapshot(BaseModel):
    port_id: int
    mode: str
    connected: bool
    protocol: str | None = None
    voltage_v: float | None = None
    current_a: float | None = None
    power_w: float | None = None


class DeviceStatusSnapshot(BaseModel):
    """Credential-free device state safe for the operator console."""

    profile_id: str
    label: str
    online: bool
    model: str | None = None
    product_family: str | None = None
    firmware_version: str | None = None
    power_budget_w: float | None = None
    total_power_w: float
    ports: list[DevicePortSnapshot]
    captured_at: datetime


class DeviceActivationResult(BaseModel):
    switched: bool
    active_device: DeviceProfileView
    snapshot: DeviceStatusSnapshot
    message: str


class DeviceProfileNotFoundError(KeyError):
    pass


class DeviceActivationError(RuntimeError):
    pass


class DeviceStatusError(RuntimeError):
    pass


class DeviceProfileSelectionStore:
    """Persist only the non-sensitive active profile id."""

    def __init__(self, path: Path | None) -> None:
        self._path = path.resolve() if path is not None else None

    def load(self, known_profile_ids: set[str]) -> str | None:
        if self._path is None or not self._path.exists():
            return None
        selected = self._path.read_text(encoding="utf-8").strip()
        return selected if selected in known_profile_ids else None

    def save(self, profile_id: str) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(f"{profile_id}\n", encoding="utf-8")
        temporary.replace(self._path)


class DeviceRuntimeCoordinator:
    """Own the active runtime and switch it atomically behind one interface."""

    def __init__(
        self,
        *,
        base_settings: RuntimeSettings,
        catalog: DeviceProfileCatalog,
        active_profile_id: str,
        runtime: DeviceOpsRuntime,
        runtime_factory: RuntimeFactory,
        selection_store: DeviceProfileSelectionStore,
        audit_log: AuditLog,
    ) -> None:
        self._base_settings = base_settings
        self._catalog = catalog
        self._active_profile_id = active_profile_id
        self._runtime = runtime
        self._runtime_factory = runtime_factory
        self._selection_store = selection_store
        self._audit_log = audit_log
        # This local console intentionally serializes runs. The lock also
        # prevents closing a gateway while a stream is still consuming it.
        self._lock = asyncio.Lock()

    @classmethod
    async def create(
        cls,
        base_settings: RuntimeSettings,
        catalog: DeviceProfileCatalog,
        *,
        selection_store: DeviceProfileSelectionStore | None = None,
        runtime_factory: RuntimeFactory = create_runtime,
        audit_log: AuditLog | None = None,
    ) -> DeviceRuntimeCoordinator:
        store = selection_store or DeviceProfileSelectionStore(None)
        profile_ids = {profile.id for profile in catalog.profiles}
        active_profile_id = (
            store.load(profile_ids)
            or catalog.active_profile
            or catalog.profiles[0].id
        )
        profile = catalog.get(active_profile_id)
        if profile is None:  # Protected by catalog validation and set lookup.
            raise ValueError("active device profile is not configured")
        runtime = await runtime_factory(profile.apply(base_settings))
        return cls(
            base_settings=base_settings,
            catalog=catalog,
            active_profile_id=active_profile_id,
            runtime=runtime,
            runtime_factory=runtime_factory,
            selection_store=store,
            audit_log=audit_log or JsonlAuditLog(base_settings.audit_path),
        )

    @classmethod
    def single(cls, runtime: DeviceOpsRuntime) -> DeviceRuntimeCoordinator:
        profile = _fallback_profile(runtime.settings, {})
        catalog = DeviceProfileCatalog(
            active_profile=profile.id,
            profiles=[profile],
        )
        return cls(
            base_settings=runtime.settings,
            catalog=catalog,
            active_profile_id=profile.id,
            runtime=runtime,
            runtime_factory=create_runtime,
            selection_store=DeviceProfileSelectionStore(None),
            audit_log=JsonlAuditLog(runtime.settings.audit_path),
        )

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[DeviceOpsRuntime]:
        async with self._lock:
            yield self._runtime

    async def inventory(self) -> DeviceInventory:
        async with self._lock:
            return self._inventory_unlocked()

    async def status(self) -> DeviceStatusSnapshot:
        async with self._lock:
            profile = self._catalog.get(self._active_profile_id)
            if profile is None:  # Protected by catalog validation.
                raise DeviceStatusError("当前设备配置不可用。")
            try:
                raw_status = await self._runtime.gateway.get_status()
            except Exception as exc:
                raise DeviceStatusError("无法读取当前设备状态。") from exc
            return _status_snapshot(profile, raw_status)

    async def activate(self, profile_id: str) -> DeviceActivationResult:
        async with self._lock:
            target = self._catalog.get(profile_id)
            if target is None:
                raise DeviceProfileNotFoundError(profile_id)
            if profile_id == self._active_profile_id:
                try:
                    raw_status = await self._runtime.gateway.get_status()
                except Exception as exc:
                    raise DeviceActivationError(
                        "无法读取当前设备状态。"
                    ) from exc
                return DeviceActivationResult(
                    switched=False,
                    active_device=_profile_view(target, active=True),
                    snapshot=_status_snapshot(target, raw_status),
                    message=f"当前已连接{target.label}。",
                )

            previous_profile_id = self._active_profile_id
            candidate: DeviceOpsRuntime | None = None
            try:
                candidate = await self._runtime_factory(
                    target.apply(self._base_settings)
                )
                # A connected MCP session proves authentication; the query
                # additionally proves the selected device can return data.
                raw_status = await candidate.gateway.get_status()
                snapshot = _status_snapshot(target, raw_status)
                self._selection_store.save(profile_id)
            except asyncio.CancelledError:
                if candidate is not None:
                    await _close_quietly(candidate)
                raise
            except Exception as exc:
                if candidate is not None:
                    await _close_quietly(candidate)
                _append_audit_quietly(
                    self._audit_log,
                    DeviceSwitchAuditRecord(
                        from_profile=previous_profile_id,
                        to_profile=profile_id,
                        status="failed",
                    )
                )
                raise DeviceActivationError(
                    "无法连接目标设备，已保留当前设备。"
                ) from exc

            assert candidate is not None
            previous_runtime = self._runtime
            self._runtime = candidate
            self._active_profile_id = profile_id
            _append_audit_quietly(
                self._audit_log,
                DeviceSwitchAuditRecord(
                    from_profile=previous_profile_id,
                    to_profile=profile_id,
                    status="completed",
                )
            )
            await _close_quietly(previous_runtime)
            return DeviceActivationResult(
                switched=True,
                active_device=_profile_view(target, active=True),
                snapshot=snapshot,
                message=f"已切换至{target.label}，连接验证通过。",
            )

    async def aclose(self) -> None:
        async with self._lock:
            await self._runtime.aclose()

    def _inventory_unlocked(self) -> DeviceInventory:
        return DeviceInventory(
            active_profile_id=self._active_profile_id,
            switching_enabled=len(self._catalog.profiles) > 1,
            devices=[
                _profile_view(
                    profile,
                    active=profile.id == self._active_profile_id,
                )
                for profile in self._catalog.profiles
            ],
        )


def load_device_profiles(
    values: Mapping[str, str],
    base_settings: RuntimeSettings,
) -> tuple[DeviceProfileCatalog, DeviceProfileSelectionStore]:
    """Load private profiles, falling back to the existing single target."""
    raw_catalog_path = values.get("DEVICE_PROFILES_FILE", "").strip()
    if not raw_catalog_path:
        profile = _fallback_profile(base_settings, values)
        return (
            DeviceProfileCatalog(
                active_profile=profile.id,
                profiles=[profile],
            ),
            DeviceProfileSelectionStore(None),
        )

    catalog_path = _resolve_project_path(raw_catalog_path)
    if not catalog_path.is_file():
        raise ValueError("DEVICE_PROFILES_FILE does not exist")
    try:
        catalog = DeviceProfileCatalog.model_validate_json(
            catalog_path.read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("DEVICE_PROFILES_FILE is invalid") from exc

    raw_state_path = values.get(
        "DEVICE_PROFILE_STATE",
        "var/active-device-profile",
    ).strip()
    state_path = _resolve_project_path(raw_state_path) if raw_state_path else None
    return catalog, DeviceProfileSelectionStore(state_path)


def _fallback_profile(
    settings: RuntimeSettings,
    values: Mapping[str, str],
) -> DeviceProfile:
    return DeviceProfile(
        id="default",
        label=values.get("DEVICE_LABEL", "当前设备"),
        backend=settings.backend,
        device_id=settings.device_id,
        allowed_ports=list(settings.allowed_ports),
        xdp_mcp_url=(
            SecretStr(settings.xdp_mcp_url)
            if settings.xdp_mcp_url is not None
            else None
        ),
    )


def _profile_view(
    profile: DeviceProfile,
    *,
    active: bool,
) -> DeviceProfileView:
    return DeviceProfileView(
        id=profile.id,
        label=profile.label,
        backend=profile.backend,
        allowed_ports=profile.allowed_ports,
        active=active,
    )


def _status_snapshot(
    profile: DeviceProfile,
    status: Mapping[str, Any],
) -> DeviceStatusSnapshot:
    machine = status.get("machine")
    machine = machine if isinstance(machine, Mapping) else {}
    raw_ports = status.get("ports")
    if isinstance(raw_ports, Mapping):
        port_values = list(raw_ports.values())
    elif isinstance(raw_ports, list):
        port_values = raw_ports
    else:
        port_values = []

    allowed_ports = set(profile.allowed_ports)
    ports: list[DevicePortSnapshot] = []
    for raw_port in port_values:
        if not isinstance(raw_port, Mapping):
            continue
        port_id = _optional_int(
            raw_port.get("port_id", raw_port.get("port"))
        )
        if port_id is None or port_id not in allowed_ports:
            continue
        ports.append(
            DevicePortSnapshot(
                port_id=port_id,
                mode=str(raw_port.get("mode") or "unknown"),
                connected=bool(raw_port.get("connected", False)),
                protocol=_optional_string(raw_port.get("protocol")),
                voltage_v=_optional_float(raw_port.get("voltage_v")),
                current_a=_optional_float(raw_port.get("current_a")),
                power_w=_optional_float(raw_port.get("power_w")),
            )
        )
    ports.sort(key=lambda item: item.port_id)
    return DeviceStatusSnapshot(
        profile_id=profile.id,
        label=profile.label,
        online=bool(status.get("online", False)),
        model=_optional_string(status.get("model")),
        product_family=_optional_string(machine.get("product_family")),
        firmware_version=_optional_string(
            status.get("app_version", status.get("firmware"))
        ),
        power_budget_w=_optional_float(machine.get("max_power_budget")),
        total_power_w=round(
            sum(port.power_w or 0.0 for port in ports),
            2,
        ),
        ports=ports,
        captured_at=datetime.now(timezone.utc),
    )


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _resolve_project_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[2] / path


async def _close_quietly(runtime: DeviceOpsRuntime) -> None:
    try:
        await runtime.aclose()
    except Exception:
        # The replacement is already known-good. A stale connection failing
        # to close must not roll the active device back to a closed runtime.
        return


def _append_audit_quietly(
    audit_log: AuditLog,
    record: DeviceSwitchAuditRecord,
) -> None:
    try:
        audit_log.append(record)
    except OSError:
        # Switching has already succeeded or failed safely. An unavailable
        # audit sink must not change which device remains active.
        return
