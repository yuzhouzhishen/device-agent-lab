from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from device_agent_lab.device_ops_service import DeviceOpsRun
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


def create_app(runtime: DeviceOpsRuntime | None = None) -> FastAPI:
    web_directory = Path(__file__).with_name("web")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        current_runtime: DeviceOpsRuntime
        if runtime is None:
            load_dotenv(
                Path(__file__).resolve().parents[2] / ".env",
                override=True,
            )
            current_runtime = await create_runtime(
                RuntimeSettings.from_mapping()
            )
        else:
            current_runtime = runtime
        app.state.runtime = current_runtime
        try:
            yield
        finally:
            await current_runtime.aclose()

    app = FastAPI(
        title="DeviceOps Agent API",
        version="0.5.0",
        lifespan=lifespan,
    )
    app.mount(
        "/static",
        StaticFiles(directory=web_directory),
        name="static",
    )
    if runtime is not None:
        app.state.runtime = runtime

    @app.get("/", include_in_schema=False)
    async def console() -> FileResponse:
        return FileResponse(web_directory / "index.html")

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        current = _runtime(request)
        return {
            "status": "ok",
            "backend": current.settings.backend,
            "planner": current.settings.planner_mode,
        }

    @app.post("/v1/requests", response_model=DeviceOpsRun)
    async def start_device_request(
        body: StartRequest,
        request: Request,
    ) -> DeviceOpsRun:
        current = _runtime(request)
        try:
            return await current.service.start(
                body.request,
                current.context,
                thread_id=body.thread_id,
                conversation_id=body.conversation_id,
            )
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
        current = _runtime(request)
        try:
            return await current.service.resume(
                thread_id,
                approved=body.approved,
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


def _runtime(request: Request) -> DeviceOpsRuntime:
    try:
        return request.app.state.runtime
    except AttributeError as exc:
        raise HTTPException(
            status_code=503,
            detail="runtime is not initialized",
        ) from exc


app = create_app()
