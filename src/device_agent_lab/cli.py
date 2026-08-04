from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping

from dotenv import load_dotenv

from device_agent_lab.device_ops_service import DeviceOpsRun
from device_agent_lab.runtime import RuntimeSettings, create_runtime


def main() -> None:
    load_dotenv()
    args = _build_parser().parse_args()
    values = dict(os.environ)
    if args.backend is not None:
        values["DEVICE_BACKEND"] = args.backend
    if args.planner is not None:
        values["DEVICE_PLANNER"] = args.planner
    request = args.request or input("请输入设备问题或操作：").strip()
    if not request:
        raise SystemExit("request must not be empty")
    asyncio.run(_run(request, values))


async def _run(request: str, values: Mapping[str, str]) -> None:
    settings = RuntimeSettings.from_mapping(values)
    runtime = await create_runtime(settings)
    try:
        run = await runtime.service.start(request, runtime.context)
        _print_run(run)

        if run.status == "confirmation_required":
            answer = input("确认执行这个设备操作吗？[y/N]: ").strip().lower()
            approved = answer in {"y", "yes", "是", "确认"}
            run = await runtime.service.resume(run.thread_id, approved=approved)
            _print_run(run)
    finally:
        await runtime.aclose()


def _print_run(run: DeviceOpsRun) -> None:
    print("\nstructured command:")
    print(
        json.dumps(
            run.command.model_dump(),
            ensure_ascii=False,
            indent=2,
        )
    )
    if run.confirmation is not None:
        print("\nconfirmation required:")
        print(
            json.dumps(
                run.confirmation,
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    print("\nworkflow trace:")
    print(" -> ".join(run.trace))
    if run.knowledge:
        print("\nretrieved knowledge:")
        for document in run.knowledge:
            print(f"- {document['title']} ({document['id']})")
    print("\nresult:")
    print(json.dumps(run.result, ensure_ascii=False, indent=2))
    print("\nsummary:")
    print(run.summary)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the DeviceOps Agent from the terminal."
    )
    parser.add_argument(
        "request",
        nargs="?",
        help="Natural-language device request.",
    )
    parser.add_argument(
        "--backend",
        choices=("mock", "local_mcp", "xdp"),
        help="Override DEVICE_BACKEND for this run.",
    )
    parser.add_argument(
        "--planner",
        choices=("auto", "rules", "gemini"),
        help="Override DEVICE_PLANNER for this run.",
    )
    return parser


if __name__ == "__main__":
    main()
