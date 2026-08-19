from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from device_agent_lab.agent_contracts import (
    ConversationTurn,
    DeviceConversationContext,
)
from device_agent_lab.conversation_store import SqliteConversationStore


class SqliteConversationStoreTest(unittest.TestCase):
    def test_context_survives_store_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "sessions.sqlite3"
            first = SqliteConversationStore(path)
            first.save(
                "conversation-1",
                DeviceConversationContext(
                    last_port=3,
                    charging_ports=[1, 3],
                    recent_turns=[
                        ConversationTurn(
                            request="诊断3号端口",
                            summary="3号端口协商异常。",
                            action="diagnose_port",
                            port=3,
                            intent="query_device",
                            evidence=["PD 请求能力不匹配"],
                            knowledge_titles=["充电问题排查"],
                            trace=["diagnose_port", "retrieve_knowledge"],
                        )
                    ],
                ),
            )

            restored = SqliteConversationStore(path).load("conversation-1")

        self.assertIsNotNone(restored)
        self.assertEqual(restored.last_port, 3)
        self.assertEqual(restored.charging_ports, [1, 3])
        self.assertEqual(restored.recent_turns[0].action, "diagnose_port")
        self.assertEqual(
            restored.recent_turns[0].evidence,
            ["PD 请求能力不匹配"],
        )
        self.assertEqual(
            restored.recent_turns[0].knowledge_titles,
            ["充电问题排查"],
        )


if __name__ == "__main__":
    unittest.main()
