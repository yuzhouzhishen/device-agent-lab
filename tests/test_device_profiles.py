from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from pydantic import SecretStr

from device_agent_lab.audit import InMemoryAuditLog
from device_agent_lab.device_profiles import (
    DeviceActivationError,
    DeviceProfile,
    DeviceProfileCatalog,
    DeviceProfileSelectionStore,
    DeviceRuntimeCoordinator,
    load_device_profiles,
)
from device_agent_lab.runtime import RuntimeSettings, create_runtime


class DeviceProfileConfigurationTest(unittest.TestCase):
    def test_public_inventory_never_serializes_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = _settings(Path(temporary_directory))
            profile = DeviceProfile(
                id="private-device",
                label="CP02S 测试设备",
                backend="xdp",
                device_id="INTERNAL-DEVICE",
                allowed_ports=[1, 2, 3, 4, 5],
                xdp_mcp_url=SecretStr(
                    "https://private.example/1234567890/token/mcp"
                ),
            )

            self.assertNotIn("private.example", repr(profile))
            self.assertNotIn("1234567890", repr(profile))
            self.assertEqual(
                profile.apply(settings).xdp_mcp_url,
                "https://private.example/1234567890/token/mcp",
            )

    def test_catalog_rejects_duplicate_profile_ids(self) -> None:
        profile = _mock_profile("same", "设备 A", "MOCK-A")

        with self.assertRaisesRegex(ValueError, "unique"):
            DeviceProfileCatalog(profiles=[profile, profile])

    def test_missing_private_catalog_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = _settings(Path(temporary_directory))
            missing = Path(temporary_directory) / "missing.json"

            with self.assertRaisesRegex(ValueError, "does not exist"):
                load_device_profiles(
                    {"DEVICE_PROFILES_FILE": str(missing)},
                    settings,
                )


class DeviceRuntimeCoordinatorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = _settings(self.root)
        self.catalog = DeviceProfileCatalog(
            active_profile="device-a",
            profiles=[
                _mock_profile("device-a", "设备 A", "MOCK-A"),
                _mock_profile("device-b", "设备 B", "MOCK-B"),
            ],
        )

    async def test_successful_switch_is_persisted_and_audited(self) -> None:
        state_path = self.root / "active-profile"
        audit = InMemoryAuditLog()
        coordinator = await DeviceRuntimeCoordinator.create(
            self.settings,
            self.catalog,
            selection_store=DeviceProfileSelectionStore(state_path),
            audit_log=audit,
        )

        result = await coordinator.activate("device-b")
        inventory = await coordinator.inventory()

        self.assertTrue(result.switched)
        self.assertTrue(result.snapshot.online)
        self.assertEqual(result.snapshot.profile_id, "device-b")
        self.assertEqual(
            [port.port_id for port in result.snapshot.ports],
            [1, 2, 3],
        )
        self.assertEqual(inventory.active_profile_id, "device-b")
        self.assertEqual(state_path.read_text().strip(), "device-b")
        self.assertEqual(audit.records[-1].status, "completed")
        self.assertEqual(audit.records[-1].to_profile, "device-b")
        await coordinator.aclose()

    async def test_current_status_is_sanitized(self) -> None:
        coordinator = await DeviceRuntimeCoordinator.create(
            self.settings,
            self.catalog,
        )

        snapshot = await coordinator.status()
        payload = snapshot.model_dump_json()

        self.assertEqual(snapshot.profile_id, "device-a")
        self.assertEqual(snapshot.total_power_w, 45.0)
        self.assertNotIn("MOCK-A", payload)
        self.assertNotIn("device_id", payload)
        await coordinator.aclose()

    async def test_failed_switch_keeps_previous_runtime_and_hides_error(self) -> None:
        async def failing_factory(settings: RuntimeSettings):
            if settings.device_id == "MOCK-B":
                raise RuntimeError(
                    "https://private.example/1234567890/secret/mcp"
                )
            return await create_runtime(settings)

        audit = InMemoryAuditLog()
        coordinator = await DeviceRuntimeCoordinator.create(
            self.settings,
            self.catalog,
            runtime_factory=failing_factory,
            audit_log=audit,
        )

        with self.assertRaisesRegex(
            DeviceActivationError,
            "已保留当前设备",
        ) as captured:
            await coordinator.activate("device-b")

        inventory = await coordinator.inventory()
        self.assertEqual(inventory.active_profile_id, "device-a")
        self.assertNotIn("private.example", str(captured.exception))
        self.assertEqual(audit.records[-1].status, "failed")
        await coordinator.aclose()

    async def test_switch_waits_for_an_in_flight_runtime_lease(self) -> None:
        factory_called = asyncio.Event()

        async def observed_factory(settings: RuntimeSettings):
            if settings.device_id == "MOCK-B":
                factory_called.set()
            return await create_runtime(settings)

        coordinator = await DeviceRuntimeCoordinator.create(
            self.settings,
            self.catalog,
            runtime_factory=observed_factory,
        )
        lease_entered = asyncio.Event()
        release_lease = asyncio.Event()

        async def hold_lease() -> None:
            async with coordinator.lease():
                lease_entered.set()
                await release_lease.wait()

        holder = asyncio.create_task(hold_lease())
        await lease_entered.wait()
        switcher = asyncio.create_task(coordinator.activate("device-b"))
        await asyncio.sleep(0)

        self.assertFalse(factory_called.is_set())
        release_lease.set()
        await holder
        await switcher
        self.assertTrue(factory_called.is_set())
        await coordinator.aclose()


def _settings(root: Path) -> RuntimeSettings:
    return RuntimeSettings.from_mapping(
        {
            "DEVICE_BACKEND": "mock",
            "DEVICE_PLANNER": "rules",
            "DEVICE_AUDIT_PATH": str(root / "audit.jsonl"),
            "DEVICE_SESSION_DB": str(root / "sessions.sqlite3"),
        }
    )


def _mock_profile(
    profile_id: str,
    label: str,
    device_id: str,
) -> DeviceProfile:
    return DeviceProfile(
        id=profile_id,
        label=label,
        backend="mock",
        device_id=device_id,
        allowed_ports=[1, 2, 3],
    )


if __name__ == "__main__":
    unittest.main()
