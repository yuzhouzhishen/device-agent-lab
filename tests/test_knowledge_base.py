from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from device_agent_lab.knowledge_base import LocalKnowledgeBase


class LocalKnowledgeBaseTest(unittest.TestCase):
    def test_search_returns_the_most_relevant_device_guide(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "port-not-charging.json").write_text(
                json.dumps(
                    {
                        "id": "port-not-charging",
                        "title": "端口无法充电",
                        "keywords": ["端口", "无法充电", "线缆"],
                        "content": "先检查端口状态、线缆和快充协议协商。",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (root / "temperature.json").write_text(
                json.dumps(
                    {
                        "id": "temperature",
                        "title": "设备温度偏高",
                        "keywords": ["温度", "过热", "散热"],
                        "content": "检查环境温度和设备散热条件。",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            knowledge = LocalKnowledgeBase(root)

            results = knowledge.search("2号端口为什么无法充电，是否是线缆问题？")

        self.assertEqual(results[0]["id"], "port-not-charging")
        self.assertIn("快充协议", results[0]["content"])


if __name__ == "__main__":
    unittest.main()
