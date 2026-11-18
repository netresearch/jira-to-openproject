# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""FastAPI web server for real-time migration progress dashboard."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, cast
from uuid import uuid4

import psutil
import redis.asyncio as redis
from fastapi import (
    BackgroundTasks,
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, TemplateNotFound
from pydantic import BaseModel, Field, ValidationError

from src.config import bootstrap
from src.config import logger as _migration_logger
from src.dashboard.progress import MigrationProgress, progress_file, read_progress
from src.display import ExtendedLogger

_INTERNAL_SERVER_ERROR = "Internal server error"
_NO_ACTIVE_MIGRATION = "No migration is running"
_EXTERNAL_CONTROL = "The migration is controlled by its publishing process."
_SNAPSHOT_HEADERS = {"Cache-Control": "no-store"}
_MISSING_RUN = "No snapshot is available for this migration."
_UNAVAILABLE_RUN = "Published migration progress is unavailable."


# No migration component imports needed for dashboard

# Phase 6a (ADR-002): the dashboard logger is the same lazy ``migration``
# logger exposed by ``src.config``. Until ``bootstrap()`` runs (in the
# FastAPI lifespan), no FileHandler is attached — so importing this
# module is side-effect-free and safe even when ``var/logs`` is not
# writable.
logger: ExtendedLogger = cast("ExtendedLogger", _migration_logger)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Initialize shared resources on startup and clean them up on shutdown."""
    global redis_client  # noqa: PLW0603

    # Phase 6a (ADR-002): bootstrap var/ dirs and logging on app start.
    # Idempotent — safe even if main.py also called it.
    bootstrap()

    try:
        redis_client = redis.Redis.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6379"),
            decode_responses=True,
        )
        await redis_client.ping()
        logger.info("Connected to the dashboard cache")
    except Exception:
        logger.warning("Could not connect to the dashboard cache")
        redis_client = None

    t_metrics: asyncio.Task[object] = asyncio.create_task(collect_system_metrics())
    BACKGROUND_TASKS.add(t_metrics)
    t_metrics.add_done_callback(BACKGROUND_TASKS.discard)

    t_progress: asyncio.Task[object] = asyncio.create_task(update_migration_progress())
    BACKGROUND_TASKS.add(t_progress)
    t_progress.add_done_callback(BACKGROUND_TASKS.discard)

    logger.info("Dashboard started successfully")

    try:
        yield
    finally:
        for task in (t_progress, t_metrics):
            task.cancel()
        await asyncio.gather(t_progress, t_metrics, return_exceptions=True)
        if redis_client:
            await redis_client.aclose()
        logger.info("Dashboard shutdown complete")


# Initialize FastAPI app
app = FastAPI(
    title="Jira to OpenProject Migration Dashboard",
    description="Real-time dashboard for monitoring migration progress",
    version="2.0.0",
    lifespan=lifespan,
)

_DASHBOARD_DIR = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(env=Environment(loader=FileSystemLoader(_DASHBOARD_DIR / "templates"), autoescape=True))


# Static assets directory may be absent in minimal runtime layouts; only
# mount the route when the directory actually exists.
if (_DASHBOARD_DIR / "static").is_dir():
    app.mount(
        "/static",
        StaticFiles(directory=str(_DASHBOARD_DIR / "static")),
        name="static",
    )

# Redis connection
redis_client: redis.Redis | None = None

# Track background tasks to satisfy linter and keep references
BACKGROUND_TASKS: set[asyncio.Task[object]] = set()

# Global state for migration tracking
_LOCAL_STATE_LOCK = Lock()
migration_state = {
    "is_running": False,
    "migration_id": None,
    "start_time": None,
    "pause_time": None,
    "total_pause_time": 0,
    "current_component": None,
}


# Pydantic models for API responses
class MigrationMetrics(BaseModel):
    """Migration metrics data model."""

    migration_id: str
    entities_per_second: float | None = None
    average_processing_time: float | None = None
    memory_usage_mb: float | None = None
    pilot_memory_usage_mb: float | None = None
    cpu_usage_percent: float | None = None
    network_requests_per_second: float | None = None
    error_rate: float | None = None
    throughput_history: list[dict[str, Any]] = Field(default_factory=list)


class MigrationEvent(BaseModel):
    """Migration event data model."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    level: str  # 'info', 'warning', 'error', 'success'
    message: str
    entity: str | None = None
    component: str | None = None
    details: dict[str, Any] | None = None


class MigrationControl(BaseModel):
    """Migration control request model."""

    action: str  # 'start', 'stop', 'pause', 'resume'
    components: list[str] | None = None
    config: dict[str, Any] | None = None


# WebSocket connection manager with enhanced features
class ConnectionManager:
    """Manages WebSocket connections for real-time updates."""

    def __init__(self) -> None:
        """Initialize connection manager state."""
        self.active_connections: list[WebSocket] = []
        self.connection_metadata: dict[WebSocket, dict[str, Any]] = {}

    async def connect(self, websocket: WebSocket) -> None:
        """Connect a new WebSocket client."""
        await websocket.accept()
        self.active_connections.append(websocket)
        self.connection_metadata[websocket] = {
            "connected_at": datetime.now(UTC),
            "last_heartbeat": datetime.now(UTC),
            "client_id": str(uuid4()),
        }
        logger.info(
            "WebSocket client connected. Total clients: %d",
            len(self.active_connections),
        )

        # Send initial state
        await self.send_personal_message(
            {
                "type": "connection_established",
                "client_id": self.connection_metadata[websocket]["client_id"],
                "timestamp": datetime.now(UTC).isoformat(),
            },
            websocket,
        )

    def disconnect(self, websocket: WebSocket) -> None:
        """Disconnect a WebSocket client."""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        if websocket in self.connection_metadata:
            del self.connection_metadata[websocket]
        logger.info(
            "WebSocket client disconnected. Total clients: %d",
            len(self.active_connections),
        )

    async def send_personal_message(
        self,
        message: dict[str, Any],
        websocket: WebSocket,
    ) -> None:
        """Send a message to a specific WebSocket client."""
        try:
            await websocket.send_text(json.dumps(message, default=str))
        except WebSocketDisconnect:
            self.disconnect(websocket)
        except Exception:
            logger.exception("Error sending personal message")
            self.disconnect(websocket)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Broadcast a message to all connected WebSocket clients."""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message, default=str))
            except WebSocketDisconnect:
                disconnected.append(connection)
            except Exception:
                logger.exception("Error broadcasting message")
                disconnected.append(connection)

        # Remove disconnected clients
        for connection in disconnected:
            self.disconnect(connection)

    async def broadcast_progress(self, progress: MigrationProgress) -> None:
        """Broadcast progress update to all clients."""
        await self.broadcast(
            {
                "type": "progress_update",
                "data": progress.public_data(),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    async def broadcast_metrics(self, metrics: MigrationMetrics) -> None:
        """Broadcast metrics update to all clients."""
        await self.broadcast(
            {
                "type": "metrics_update",
                "data": metrics.model_dump(),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    async def broadcast_event(self, event: MigrationEvent) -> None:
        """Broadcast event to all clients."""
        await self.broadcast(
            {
                "type": "event",
                "data": event.model_dump(),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )


# Initialize connection manager
manager = ConnectionManager()


def _progress() -> MigrationProgress:
    target = progress_file()
    if target is not None:
        try:
            return read_progress(target)
        except (OSError, ValidationError) as exc:
            raise HTTPException(status_code=503, detail=_UNAVAILABLE_RUN, headers=_SNAPSHOT_HEADERS) from exc
    with _LOCAL_STATE_LOCK:
        state = migration_state.copy()
    if not state["is_running"]:
        return MigrationProgress(migration_id="none", status="idle")
    return MigrationProgress(
        migration_id=state["migration_id"],
        status="paused" if state["pause_time"] else "running",
        current_component=state["current_component"],
        start_time=state["start_time"],
        last_update=state["pause_time"] or state["start_time"],
    )


def _requested_progress(migration_id: str | None) -> MigrationProgress:
    progress = _progress()
    if migration_id and migration_id != progress.migration_id:
        raise HTTPException(status_code=404, detail=_MISSING_RUN, headers=_SNAPSHOT_HEADERS)
    return progress


def _status(progress: MigrationProgress) -> dict[str, Any]:
    return {
        **progress.public_data(),
        "is_running": progress.status in {"running", "paused"} and progress.finished_at is None,
        "pause_time": None,
        "total_pause_time": None,
    }


def _metrics(progress: MigrationProgress) -> MigrationMetrics:
    return MigrationMetrics(
        migration_id=progress.migration_id,
        memory_usage_mb=psutil.virtual_memory().used / (1024 * 1024),
        pilot_memory_usage_mb=progress.memory_usage_mb,
        cpu_usage_percent=psutil.cpu_percent(interval=0.1),
    )


async def collect_system_metrics() -> None:
    """Publish measured host metrics and maintain the optional Redis cache."""
    while True:
        try:
            progress = await asyncio.to_thread(_progress)
            metrics = await asyncio.to_thread(_metrics, progress)
            await manager.broadcast_metrics(metrics)
            if redis_client:
                await redis_client.set("system_metrics", metrics.model_dump_json(), ex=60)
        except HTTPException:
            pass  # The progress watcher reports unavailable snapshots to clients.
        except Exception:
            logger.exception("Error collecting system metrics")
        await asyncio.sleep(5)


async def update_migration_progress() -> None:
    """Broadcast each complete publication, including the final snapshot."""
    previous: MigrationProgress | None = None
    unavailable = False
    while True:
        try:
            progress = await asyncio.to_thread(_progress)
            if unavailable or progress != previous:
                await manager.broadcast_progress(progress)
                previous = progress
            unavailable = False
        except HTTPException as exc:
            if not unavailable:
                await manager.broadcast({"type": "error", "message": exc.detail})
            unavailable = True
        await asyncio.sleep(1)


def _summary_context(progress: MigrationProgress) -> dict[str, Any]:
    if progress.status == "idle":
        return {"notice": "No migration run has been published."}
    phase = progress.current_component or next(
        (item.name for item in reversed(progress.phases) if item.status != "pending"), None
    )
    facts = [("Status", progress.status.capitalize())]
    if phase:
        facts.append(("Latest phase", phase))
    if progress.last_update:
        facts.append(("Last update", progress.last_update.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")))
    counts = list(progress.verified_counts.items())
    for name, amount in (
        ("Processed entities", progress.processed_entities),
        ("Total entities", progress.total_entities),
        ("Failed entities", progress.failed_entities),
    ):
        if amount is not None:
            counts.append((name, amount))
    return {"facts": facts, "counts": counts, "pending_checks": progress.pending_checks}


@app.get(
    "/", response_class=HTMLResponse, responses={503: {"description": "Dashboard or published progress unavailable"}}
)
def dashboard(request: Request) -> HTMLResponse:
    """Render plain observations through the packaged, autoescaping HTML template."""
    code = 200
    try:
        context = _summary_context(_progress())
    except HTTPException as exc:
        context = {"notice": "The latest migration progress is unavailable."}
        code = exc.status_code
    try:
        return _TEMPLATES.TemplateResponse(
            request, "dashboard.html", context, status_code=code, headers=_SNAPSHOT_HEADERS
        )
    except TemplateNotFound as exc:
        raise HTTPException(
            status_code=503, detail="Dashboard template not available in this installation.", headers=_SNAPSHOT_HEADERS
        ) from exc


@app.websocket("/ws/dashboard")
@app.websocket("/ws/progress")
async def websocket_progress(websocket: WebSocket) -> None:
    """Serve an initial snapshot, live changes, and the existing request/heartbeat protocol."""
    await manager.connect(websocket)
    try:
        await _send_progress(websocket)
        while True:
            try:
                message = json.loads(await websocket.receive_text())
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                await _websocket_message(websocket, message)
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)


async def _send_progress(websocket: WebSocket, *, status_only: bool = False) -> None:
    try:
        progress = await asyncio.to_thread(_progress)
    except HTTPException as exc:
        await manager.send_personal_message({"type": "error", "message": exc.detail}, websocket)
        return
    message = {
        "type": "status_update" if status_only else "progress_update",
        "data": _status(progress) if status_only else progress.public_data(),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    await manager.send_personal_message(message, websocket)


async def _websocket_message(websocket: WebSocket, message: dict[str, Any]) -> None:
    match message.get("type"):
        case "heartbeat":
            now = datetime.now(UTC)
            if websocket in manager.connection_metadata:
                manager.connection_metadata[websocket]["last_heartbeat"] = now
            await manager.send_personal_message({"type": "heartbeat_response", "timestamp": now.isoformat()}, websocket)
        case "request_status":
            await _send_progress(websocket, status_only=True)
        case "control_command":
            command = message.get("data")
            if isinstance(command, dict):
                await handle_control_command(command, websocket)


async def handle_control_command(command: dict[str, Any], websocket: WebSocket) -> None:
    """Handle control commands from the frontend."""
    if progress_file() is not None:
        await manager.send_personal_message({"type": "error", "message": _EXTERNAL_CONTROL}, websocket)
        return
    action = command.get("action")

    if action == "start_migration":
        await start_migration_background(command.get("config", {}))
    elif action == "stop_migration":
        await stop_migration_background()
    elif action == "pause_migration":
        await pause_migration_background()
    elif action == "resume_migration":
        await resume_migration_background()
    else:
        await manager.send_personal_message(
            {"type": "error", "message": "Unknown command"},
            websocket,
        )


async def start_migration_background(config: dict[str, Any], *, run_id: str | None = None) -> None:
    """Start migration in background."""
    try:
        run_id = run_id or str(uuid4())
        started = datetime.now(UTC)
        component = config.get("component", "all")
        event = MigrationEvent(level="info", message="Migration started", component=component)
        with _LOCAL_STATE_LOCK:
            migration_state.update(
                is_running=True,
                migration_id=run_id,
                start_time=started,
                current_component=component,
                pause_time=None,
                total_pause_time=0,
            )
        await manager.broadcast_event(event)

        logger.info("Migration started: %s", run_id)

    except Exception:
        logger.exception("Error starting migration")
        with _LOCAL_STATE_LOCK:
            if migration_state["migration_id"] == run_id:
                migration_state["is_running"] = False

        event = MigrationEvent(level="error", message="Failed to start migration")
        await manager.broadcast_event(event)


async def stop_migration_background() -> None:
    """Stop migration in background."""
    try:
        with _LOCAL_STATE_LOCK:
            migration_state["is_running"] = False

        # Broadcast migration stop event
        event = MigrationEvent(level="info", message="Migration stopped by user")
        await manager.broadcast_event(event)

        logger.info("Migration stopped by user")

    except Exception:
        logger.exception("Error stopping migration")


async def pause_migration_background() -> None:
    """Pause migration in background."""
    try:
        with _LOCAL_STATE_LOCK:
            migration_state["pause_time"] = datetime.now(UTC)

        # Broadcast migration pause event
        event = MigrationEvent(level="info", message="Migration paused")
        await manager.broadcast_event(event)

        logger.info("Migration paused")

    except Exception:
        logger.exception("Error pausing migration")


async def resume_migration_background() -> None:
    """Resume migration in background."""
    try:
        with _LOCAL_STATE_LOCK:
            if migration_state["pause_time"]:
                # Calculate total pause time
                pause_duration = (datetime.now(UTC) - migration_state["pause_time"]).total_seconds()
                migration_state["total_pause_time"] += int(pause_duration)
                migration_state["pause_time"] = None

        # Broadcast migration resume event
        event = MigrationEvent(level="info", message="Migration resumed")
        await manager.broadcast_event(event)

        logger.info("Migration resumed")

    except Exception:
        logger.exception("Error resuming migration")


@app.get("/api/progress", responses={404: {"description": _MISSING_RUN}, 503: {"description": _UNAVAILABLE_RUN}})
def get_progress(migration_id: str | None = None) -> JSONResponse:
    """Return the latest published run, even after the producer has exited."""
    return JSONResponse(_requested_progress(migration_id).public_data(), headers=_SNAPSHOT_HEADERS)


@app.get("/api/metrics", responses={404: {"description": _MISSING_RUN}, 503: {"description": _UNAVAILABLE_RUN}})
def get_metrics(migration_id: str | None = None) -> JSONResponse:
    """Measure host usage; leave unmeasured processing rates unknown."""
    return JSONResponse(_metrics(_requested_progress(migration_id)).model_dump(mode="json"), headers=_SNAPSHOT_HEADERS)


def _csv_text(value: object) -> object:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


@app.get("/api/metrics/csv", responses={404: {"description": _MISSING_RUN}, 503: {"description": _UNAVAILABLE_RUN}})
def get_metrics_csv(migration_id: str | None = None) -> JSONResponse:
    """Export measurements and observations from a single generation."""
    progress = _requested_progress(migration_id)
    data = progress.public_data()
    metrics = _metrics(progress).model_dump()
    when = datetime.now(UTC).isoformat()
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(("timestamp", "metric", "value"))

    def row(stamp: str | None, name: str, value: object) -> None:
        if value is not None:
            writer.writerow((stamp, _csv_text(name), _csv_text(value)))

    measured = data["last_update"]
    for name in (
        "migration_id",
        "status",
        "current_component",
        "start_time",
        "last_update",
        "finished_at",
        "elapsed_seconds",
        "total_entities",
        "processed_entities",
        "failed_entities",
        "error_count",
    ):
        row(measured, name, data[name])
    row(measured, "pilot_memory_usage_mb", progress.memory_usage_mb)
    for name, value in progress.verified_counts.items():
        row(measured, f"verified_counts.{name}", value)
    for phase in progress.phases:
        row(measured, f"phase.{phase.name}", phase.status)
        if phase.started_at:
            row(measured, f"phase.{phase.name}.started_at", phase.started_at.isoformat())
        if phase.finished_at:
            row(measured, f"phase.{phase.name}.finished_at", phase.finished_at.isoformat())
    for check in progress.pending_checks:
        row(measured, "pending_check", check)
    for name in ("memory_usage_mb", "cpu_usage_percent"):
        row(when, name, metrics[name])
    return JSONResponse(
        {"csv_content": buffer.getvalue(), "filename": "migration_metrics.csv"}, headers=_SNAPSHOT_HEADERS
    )


@app.get("/api/migration/status", responses={503: {"description": _UNAVAILABLE_RUN}})
def get_migration_status() -> JSONResponse:
    """Report process activity and the latest recorded outcome."""
    return JSONResponse(_status(_progress()), headers=_SNAPSHOT_HEADERS)


def _require_local_controls() -> None:
    if progress_file() is not None:
        raise HTTPException(status_code=409, detail=_EXTERNAL_CONTROL)


@app.post(
    "/api/migration/start",
    responses={
        400: {"description": "A migration is already running"},
        409: {"description": _EXTERNAL_CONTROL},
        500: {"description": "Could not schedule the migration start"},
    },
)
async def start_migration(
    control: MigrationControl,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """Start migration with specified configuration."""
    _require_local_controls()
    if migration_state["is_running"]:
        raise HTTPException(status_code=400, detail="Migration is already running")

    try:
        # Start migration in background
        run_id = str(uuid4())
        background_tasks.add_task(start_migration_background, control.config or {}, run_id=run_id)

        return JSONResponse(
            content={
                "message": "Migration started",
                "migration_id": run_id,
            },
        )

    except Exception as e:
        logger.exception("Error starting migration")
        raise HTTPException(status_code=500, detail=_INTERNAL_SERVER_ERROR) from e


@app.post(
    "/api/migration/stop",
    responses={
        400: {"description": "There is no running migration"},
        409: {"description": _EXTERNAL_CONTROL},
        500: {"description": "Could not schedule the migration stop"},
    },
)
async def stop_migration(background_tasks: BackgroundTasks) -> JSONResponse:
    """Stop current migration."""
    _require_local_controls()
    if not migration_state["is_running"]:
        raise HTTPException(status_code=400, detail=_NO_ACTIVE_MIGRATION)

    try:
        # Stop migration in background
        background_tasks.add_task(stop_migration_background)

        return JSONResponse(content={"message": "Migration stop requested"})

    except Exception as e:
        logger.exception("Error stopping migration")
        raise HTTPException(status_code=500, detail=_INTERNAL_SERVER_ERROR) from e


@app.post(
    "/api/migration/pause",
    responses={
        400: {"description": "There is no active migration to pause"},
        409: {"description": _EXTERNAL_CONTROL},
        500: {"description": "Could not schedule the migration pause"},
    },
)
async def pause_migration(background_tasks: BackgroundTasks) -> JSONResponse:
    """Pause current migration."""
    _require_local_controls()
    if not migration_state["is_running"]:
        raise HTTPException(status_code=400, detail=_NO_ACTIVE_MIGRATION)

    if migration_state["pause_time"]:
        raise HTTPException(status_code=400, detail="Migration is already paused")

    try:
        # Pause migration in background
        background_tasks.add_task(pause_migration_background)

        return JSONResponse(content={"message": "Migration pause requested"})

    except Exception as e:
        logger.exception("Error pausing migration")
        raise HTTPException(status_code=500, detail=_INTERNAL_SERVER_ERROR) from e


@app.post(
    "/api/migration/resume",
    responses={
        400: {"description": "There is no paused migration to resume"},
        409: {"description": _EXTERNAL_CONTROL},
        500: {"description": "Could not schedule the migration resume"},
    },
)
async def resume_migration(background_tasks: BackgroundTasks) -> JSONResponse:
    """Resume paused migration."""
    _require_local_controls()
    if not migration_state["is_running"]:
        raise HTTPException(status_code=400, detail=_NO_ACTIVE_MIGRATION)

    if not migration_state["pause_time"]:
        raise HTTPException(status_code=400, detail="Migration is not paused")

    try:
        # Resume migration in background
        background_tasks.add_task(resume_migration_background)

        return JSONResponse(content={"message": "Migration resume requested"})

    except Exception as e:
        logger.exception("Error resuming migration")
        raise HTTPException(status_code=500, detail=_INTERNAL_SERVER_ERROR) from e


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app, host=os.environ.get("J2O_DASHBOARD_HOST", "127.0.0.1"), port=8000)
