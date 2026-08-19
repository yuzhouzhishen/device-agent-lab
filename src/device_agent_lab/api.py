from __future__ import annotations

import json
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from device_agent_lab.device_ops_service import DeviceOpsEvent, DeviceOpsRun
from device_agent_lab.device_profiles import (
    DeviceActivationError,
    DeviceActivationResult,
    DeviceInventory,
    DeviceProfileNotFoundError,
    DeviceRuntimeCoordinator,
    DeviceStatusError,
    DeviceStatusSnapshot,
    load_device_profiles,
)
from device_agent_lab.metrics import merge_run_metrics
from device_agent_lab.runtime import (
    DeviceOpsRuntime,
    RuntimeSettings,
    create_runtime,
)


class StartRequest(BaseModel):
    request: str = Field(min_length=1)
    thread_id: str | None = None
    conversation_id: str | None = None


class ResumeRequest(BaseModel):
    approved: bool


def create_app(
    runtime: DeviceOpsRuntime | None = None,
    runtime_manager: DeviceRuntimeCoordinator | None = None,
) -> FastAPI:
    if runtime is not None and runtime_manager is not None:
        raise ValueError("provide runtime or runtime_manager, not both")
    web_directory = Path(__file__).with_name("web")
    injected_manager = runtime_manager or (
        DeviceRuntimeCoordinator.single(runtime)
        if runtime is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        current_manager: DeviceRuntimeCoordinator
        if injected_manager is None:
            # Explicit environment wins over `.env`, so a one-off
            # `DEVICE_BACKEND=mock ...` really does start offline instead of
            # silently reaching the real device the file points at.
            dotenv_path = Path(__file__).resolve().parents[2] / ".env"
            file_environment = {
                key: value
                for key, value in dotenv_values(dotenv_path).items()
                if value is not None
            }
            process_environment = dict(os.environ)
            environment = {**file_environment, **process_environment}
            if (
                "DEVICE_BACKEND" in process_environment
                and "DEVICE_PROFILES_FILE" not in process_environment
            ):
                # A one-off backend override must not inherit an unrelated
                # profile catalog from `.env` (for example `/data/...`).
                environment.pop("DEVICE_PROFILES_FILE", None)
                environment.pop("DEVICE_PROFILE_STATE", None)
            settings = RuntimeSettings.from_mapping(environment)
            catalog, selection_store = load_device_profiles(
                environment,
                settings,
            )
            current_manager = await DeviceRuntimeCoordinator.create(
                settings,
                catalog,
                selection_store=selection_store,
            )
        else:
            current_manager = injected_manager
        app.state.runtime_manager = current_manager
        try:
            yield
        finally:
            await current_manager.aclose()

    app = FastAPI(
        title="DeviceOps Agent API",
        version="1.6.0",
        lifespan=lifespan,
    )
    app.mount(
        "/static",
        StaticFiles(directory=web_directory),
        name="static",
    )
    if injected_manager is not None:
        app.state.runtime_manager = injected_manager

    @app.get("/", include_in_schema=False)
    async def console() -> FileResponse:
        return FileResponse(web_directory / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/health")
    async def health(
        request: Request,
    ) -> dict[str, str | list[int]]:
        async with _runtime_manager(request).lease() as current:
            return {
                "status": "ok",
                "backend": current.settings.backend,
                "planner": current.settings.planner_mode,
                "control_mode": current.settings.control_mode,
                "knowledge": (
                    "remote-rag"
                    if current.settings.knowledge_url
                    else "local-fallback"
                ),
                "general_knowledge": (
                    "ollama"
                    if current.general_knowledge_provider is not None
                    else "disabled"
                ),
                "session_store": "sqlite",
                "allowed_ports": current.context.allowed_ports,
            }

    @app.get("/v1/devices", response_model=DeviceInventory)
    async def list_devices(request: Request) -> DeviceInventory:
        return await _runtime_manager(request).inventory()

    @app.get(
        "/v1/devices/current/status",
        response_model=DeviceStatusSnapshot,
    )
    async def get_current_device_status(
        request: Request,
    ) -> DeviceStatusSnapshot:
        try:
            return await _runtime_manager(request).status()
        except DeviceStatusError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post(
        "/v1/devices/{profile_id}/activate",
        response_model=DeviceActivationResult,
    )
    async def activate_device(
        profile_id: str,
        request: Request,
    ) -> DeviceActivationResult:
        try:
            return await _runtime_manager(request).activate(profile_id)
        except DeviceProfileNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="device profile is not configured",
            ) from exc
        except DeviceActivationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/requests", response_model=DeviceOpsRun)
    async def start_device_request(
        body: StartRequest,
        request: Request,
    ) -> DeviceOpsRun:
        async with _runtime_manager(request).lease() as current:
            try:
                run = await current.service.start(
                    body.request,
                    current.context,
                    thread_id=body.thread_id,
                    conversation_id=body.conversation_id,
                )
                if (
                    run.status == "confirmation_required"
                    and current.settings.control_mode == "automatic"
                ):
                    completed = await current.service.resume(
                        run.thread_id,
                        approved=True,
                    )
                    return _merge_automatic_runs(run, completed)
                return run
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(
        "/v1/requests/{thread_id}/resume",
        response_model=DeviceOpsRun,
    )
    async def resume_device_request(
        thread_id: str,
        body: ResumeRequest,
        request: Request,
    ) -> DeviceOpsRun:
        async with _runtime_manager(request).lease() as current:
            try:
                return await current.service.resume(
                    thread_id,
                    approved=body.approved,
                )
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/requests/stream", include_in_schema=False)
    async def stream_device_request(
        body: StartRequest,
        request: Request,
    ) -> StreamingResponse:
        return _event_stream(
            _managed_events(
                _runtime_manager(request),
                lambda current: _apply_control_policy(
                    current,
                    current.service.start_stream(
                        body.request,
                        current.context,
                        thread_id=body.thread_id,
                        conversation_id=body.conversation_id,
                    ),
                ),
            )
        )

    @app.post(
        "/v1/requests/{thread_id}/resume/stream",
        include_in_schema=False,
    )
    async def stream_resume_device_request(
        thread_id: str,
        body: ResumeRequest,
        request: Request,
    ) -> StreamingResponse:
        return _event_stream(
            _managed_events(
                _runtime_manager(request),
                lambda current: current.service.resume_stream(
                    thread_id,
                    approved=body.approved,
                ),
            )
        )

    return app


async def _apply_control_policy(
    runtime: DeviceOpsRuntime,
    events: AsyncIterator[DeviceOpsEvent],
) -> AsyncIterator[DeviceOpsEvent]:
    async for event in events:
        run = event.run
        if (
            event.type == "run"
            and run is not None
            and run.status == "confirmation_required"
            and runtime.settings.control_mode == "automatic"
        ):
            async for resumed in runtime.service.resume_stream(
                run.thread_id,
                approved=True,
            ):
                if resumed.type == "run" and resumed.run is not None:
                    yield resumed.model_copy(
                        update={
                            "run": _merge_automatic_runs(
                                run,
                                resumed.run,
                            )
                        }
                    )
                else:
                    yield resumed
            continue
        yield event


async def _managed_events(
    manager: DeviceRuntimeCoordinator,
    factory: Callable[[DeviceOpsRuntime], AsyncIterator[DeviceOpsEvent]],
) -> AsyncIterator[DeviceOpsEvent]:
    async with manager.lease() as current:
        async for event in factory(current):
            yield event


def _merge_automatic_runs(
    pending: DeviceOpsRun,
    completed: DeviceOpsRun,
) -> DeviceOpsRun:
    return completed.model_copy(
        update={
            "metrics": merge_run_metrics(
                pending.metrics,
                completed.metrics,
            )
        }
    )


def _event_stream(
    events: AsyncIterator[DeviceOpsEvent],
) -> StreamingResponse:
    return StreamingResponse(
        _sse_frames(events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _sse_frames(
    events: AsyncIterator[DeviceOpsEvent],
) -> AsyncIterator[str]:
    try:
        async for event in events:
            yield _sse_frame(event.type, _event_payload(event))
    except Exception as exc:  # noqa: BLE001
        # Response headers are already sent, so a failure can only be
        # reported inside the stream. Include the type to stay diagnosable.
        yield _sse_frame(
            "error",
            json.dumps(
                {
                    "type": "error",
                    "detail": str(exc),
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            ),
        )


def _event_payload(event: DeviceOpsEvent) -> str:
    """Serialize one event frame.

    The envelope drops empty fields because its shape varies by event type,
    but the nested run keeps its nulls so it stays byte-comparable with what
    `/v1/requests` returns.
    """
    payload = event.model_dump(mode="json", exclude_none=True)
    if event.run is not None:
        payload["run"] = event.run.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False)


def _sse_frame(name: str, data: str) -> str:
    return f"event: {name}\ndata: {data}\n\n"


def _runtime_manager(request: Request) -> DeviceRuntimeCoordinator:
    try:
        return request.app.state.runtime_manager
    except AttributeError as exc:
        raise HTTPException(
            status_code=503,
            detail="runtime manager is not initialized",
        ) from exc


app = create_app()
