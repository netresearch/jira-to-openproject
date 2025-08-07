#!/usr/bin/env python3
"""FastAPI web server for real-time migration progress dashboard."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psutil
import redis.asyncio as redis
from fastapi import (
    BackgroundTasks,
    FastAPI,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from src.display import configure_logging

# Import our migration components
from src.utils.error_recovery import ErrorRecoverySystem

# Configure logger for dashboard
logger = configure_logging("INFO", None)

# Initialize FastAPI app
app = FastAPI(
    title="Jira to OpenProject Migration Dashboard",
    description="Real-time dashboard for monitoring migration progress",
    version="2.0.0",
)

# Setup templates and static files
templates = Jinja2Templates(directory="src/dashboard/templates")
app.mount("/static", StaticFiles(directory="src/dashboard/static"), name="static")

# Redis connection
redis_client: redis.Redis | None = None

# Global state for migration tracking
migration_state = {
    "is_running": False,
    "migration_id": None,
    "start_time": None,
    "pause_time": None,
    "total_pause_time": 0,
    "current_component": None,
    "error_recovery_system": None,
}


# Pydantic models for API responses
class MigrationProgress(BaseModel):
    """Migration progress data model."""

    migration_id: str
    total_entities: int = 0
    processed_entities: int = 0
    failed_entities: int = 0
    current_entity: str | None = None
    current_entity_type: str | None = None
    current_component: str | None = None
    status: str = "idle"  # 'idle', 'running', 'completed', 'failed', 'paused'
    start_time: datetime | None = None
    last_update: datetime = Field(default_factory=lambda: datetime.now(UTC))
    error_count: int = 0
    success_rate: float = 0.0
    estimated_time_remaining: str | None = None
    pause_time: datetime | None = None
    total_pause_time: int = 0


class MigrationMetrics(BaseModel):
    """Migration metrics data model."""

    migration_id: str
    entities_per_second: float = 0.0
    average_processing_time: float = 0.0
    memory_usage_mb: float = 0.0
    cpu_usage_percent: float = 0.0
    network_requests_per_second: float = 0.0
    error_rate: float = 0.0
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
            f"WebSocket client connected. Total clients: {len(self.active_connections)}",
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
            f"WebSocket client disconnected. Total clients: {len(self.active_connections)}",
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
        except Exception as e:
            logger.exception(f"Error sending personal message: {e}")
            self.disconnect(websocket)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Broadcast a message to all connected WebSocket clients."""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message, default=str))
            except WebSocketDisconnect:
                disconnected.append(connection)
            except Exception as e:
                logger.exception(f"Error broadcasting message: {e}")
                disconnected.append(connection)

        # Remove disconnected clients
        for connection in disconnected:
            self.disconnect(connection)

    async def broadcast_progress(self, progress: MigrationProgress) -> None:
        """Broadcast progress update to all clients."""
        await self.broadcast(
            {
                "type": "progress_update",
                "data": progress.dict(),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    async def broadcast_metrics(self, metrics: MigrationMetrics) -> None:
        """Broadcast metrics update to all clients."""
        await self.broadcast(
            {
                "type": "metrics_update",
                "data": metrics.dict(),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

    async def broadcast_event(self, event: MigrationEvent) -> None:
        """Broadcast event to all clients."""
        await self.broadcast(
            {
                "type": "event",
                "data": event.dict(),
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )


# Initialize connection manager
manager = ConnectionManager()


# Background task for system metrics collection
async def collect_system_metrics() -> None:
    """Collect system metrics for dashboard display."""
    while True:
        try:
            # Get system metrics
            memory = psutil.virtual_memory()
            cpu_percent = psutil.cpu_percent(interval=1)

            # Update metrics in Redis if available
            if redis_client:
                metrics_data = {
                    "memory_usage_mb": memory.used / 1024 / 1024,
                    "cpu_usage_percent": cpu_percent,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                await redis_client.setex(
                    "system_metrics",
                    60,
                    json.dumps(metrics_data),  # 1 minute TTL
                )

            await asyncio.sleep(5)  # Update every 5 seconds

        except Exception as e:
            logger.exception(f"Error collecting system metrics: {e}")
            await asyncio.sleep(10)  # Wait longer on error


# Background task for migration progress updates
async def update_migration_progress() -> None:
    """Update migration progress and broadcast to clients."""
    while True:
        try:
            if migration_state["is_running"] and migration_state["migration_id"]:
                # Get current progress from error recovery system
                if migration_state["error_recovery_system"]:
                    progress_data = await migration_state[
                        "error_recovery_system"
                    ].get_progress()

                    # Create progress object
                    progress = MigrationProgress(
                        migration_id=migration_state["migration_id"],
                        total_entities=progress_data.get("total_entities", 0),
                        processed_entities=progress_data.get("processed_entities", 0),
                        failed_entities=progress_data.get("failed_entities", 0),
                        current_entity=progress_data.get("current_entity"),
                        current_entity_type=progress_data.get("current_entity_type"),
                        current_component=migration_state["current_component"],
                        status="running",
                        start_time=migration_state["start_time"],
                        last_update=datetime.now(UTC),
                        error_count=progress_data.get("error_count", 0),
                        success_rate=progress_data.get("success_rate", 0.0),
                        pause_time=migration_state.get("pause_time"),
                        total_pause_time=migration_state.get("total_pause_time", 0),
                    )

                    # Broadcast progress update
                    await manager.broadcast_progress(progress)

            await asyncio.sleep(1)  # Update every second during migration

        except Exception as e:
            logger.exception(f"Error updating migration progress: {e}")
            await asyncio.sleep(5)


@app.on_event("startup")
async def startup_event() -> None:
    """Initialize dashboard on startup."""
    global redis_client

    try:
        # Initialize Redis connection
        redis_client = redis.Redis(
            host="localhost",
            port=6379,
            db=0,
            decode_responses=True,
        )
        await redis_client.ping()
        logger.info("Connected to Redis")
    except Exception as e:
        logger.warning(f"Could not connect to Redis: {e}")
        redis_client = None

    # Start background tasks
    asyncio.create_task(collect_system_metrics())
    asyncio.create_task(update_migration_progress())

    logger.info("Dashboard started successfully")


@app.on_event("shutdown")
async def shutdown_event() -> None:
    """Cleanup on shutdown."""
    if redis_client:
        await redis_client.close()
    logger.info("Dashboard shutdown complete")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request):
    """Serve the main dashboard page."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.websocket("/ws/progress")
async def websocket_progress(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time progress updates."""
    await manager.connect(websocket)

    try:
        while True:
            # Handle incoming messages (heartbeats, control commands)
            data = await websocket.receive_text()
            try:
                message = json.loads(data)

                if message.get("type") == "heartbeat":
                    # Update heartbeat timestamp
                    if websocket in manager.connection_metadata:
                        manager.connection_metadata[websocket]["last_heartbeat"] = (
                            datetime.now(UTC)
                        )

                    # Send heartbeat response
                    await manager.send_personal_message(
                        {
                            "type": "heartbeat_response",
                            "timestamp": datetime.now(UTC).isoformat(),
                        },
                        websocket,
                    )

                elif message.get("type") == "control_command":
                    # Handle control commands from frontend
                    await handle_control_command(message.get("data", {}), websocket)

            except json.JSONDecodeError:
                logger.warning("Received invalid JSON from WebSocket")

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.exception(f"WebSocket error: {e}")
        manager.disconnect(websocket)


async def handle_control_command(command: dict[str, Any], websocket: WebSocket) -> None:
    """Handle control commands from the frontend."""
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
            {"type": "error", "message": f"Unknown command: {action}"},
            websocket,
        )


async def start_migration_background(config: dict[str, Any]) -> None:
    """Start migration in background."""
    try:
        migration_state["is_running"] = True
        migration_state["migration_id"] = str(uuid4())
        migration_state["start_time"] = datetime.now(UTC)
        migration_state["current_component"] = config.get("component", "all")

        # Initialize error recovery system
        migration_state["error_recovery_system"] = ErrorRecoverySystem()

        # Broadcast migration start event
        event = MigrationEvent(
            level="info",
            message="Migration started",
            component=migration_state["current_component"],
            details=config,
        )
        await manager.broadcast_event(event)

        logger.info(f"Migration started: {migration_state['migration_id']}")

    except Exception as e:
        logger.exception(f"Error starting migration: {e}")
        migration_state["is_running"] = False

        event = MigrationEvent(
            level="error",
            message=f"Failed to start migration: {e!s}",
        )
        await manager.broadcast_event(event)


async def stop_migration_background() -> None:
    """Stop migration in background."""
    try:
        migration_state["is_running"] = False

        # Broadcast migration stop event
        event = MigrationEvent(level="info", message="Migration stopped by user")
        await manager.broadcast_event(event)

        logger.info("Migration stopped by user")

    except Exception as e:
        logger.exception(f"Error stopping migration: {e}")


async def pause_migration_background() -> None:
    """Pause migration in background."""
    try:
        migration_state["pause_time"] = datetime.now(UTC)

        # Broadcast migration pause event
        event = MigrationEvent(level="info", message="Migration paused")
        await manager.broadcast_event(event)

        logger.info("Migration paused")

    except Exception as e:
        logger.exception(f"Error pausing migration: {e}")


async def resume_migration_background() -> None:
    """Resume migration in background."""
    try:
        if migration_state["pause_time"]:
            # Calculate total pause time
            pause_duration = (
                datetime.now(UTC) - migration_state["pause_time"]
            ).total_seconds()
            migration_state["total_pause_time"] += int(pause_duration)
            migration_state["pause_time"] = None

        # Broadcast migration resume event
        event = MigrationEvent(level="info", message="Migration resumed")
        await manager.broadcast_event(event)

        logger.info("Migration resumed")

    except Exception as e:
        logger.exception(f"Error resuming migration: {e}")


@app.get("/api/progress")
async def get_progress(migration_id: str | None = None) -> JSONResponse:
    """Get current migration progress."""
    try:
        if migration_state["is_running"] and migration_state["error_recovery_system"]:
            progress_data = await migration_state[
                "error_recovery_system"
            ].get_progress()

            progress = MigrationProgress(
                migration_id=migration_state["migration_id"],
                total_entities=progress_data.get("total_entities", 0),
                processed_entities=progress_data.get("processed_entities", 0),
                failed_entities=progress_data.get("failed_entities", 0),
                current_entity=progress_data.get("current_entity"),
                current_entity_type=progress_data.get("current_entity_type"),
                current_component=migration_state["current_component"],
                status="running" if migration_state["is_running"] else "idle",
                start_time=migration_state["start_time"],
                last_update=datetime.now(UTC),
                error_count=progress_data.get("error_count", 0),
                success_rate=progress_data.get("success_rate", 0.0),
                pause_time=migration_state.get("pause_time"),
                total_pause_time=migration_state.get("total_pause_time", 0),
            )
        else:
            progress = MigrationProgress(
                migration_id=migration_id or "none",
                status="idle",
            )

        return JSONResponse(content=progress.dict())

    except Exception as e:
        logger.exception(f"Error getting progress: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/metrics")
async def get_metrics(migration_id: str | None = None) -> JSONResponse:
    """Get current migration metrics."""
    try:
        # Get system metrics
        memory = psutil.virtual_memory()
        cpu_percent = psutil.cpu_percent(interval=1)

        # Calculate migration-specific metrics
        entities_per_second = 0.0
        average_processing_time = 0.0
        error_rate = 0.0

        if migration_state["is_running"] and migration_state["error_recovery_system"]:
            progress_data = await migration_state[
                "error_recovery_system"
            ].get_progress()

            if migration_state["start_time"]:
                elapsed_time = (
                    datetime.now(UTC) - migration_state["start_time"]
                ).total_seconds()
                if elapsed_time > 0:
                    entities_per_second = (
                        progress_data.get("processed_entities", 0) / elapsed_time
                    )

        metrics = MigrationMetrics(
            migration_id=migration_id or migration_state.get("migration_id", "none"),
            entities_per_second=entities_per_second,
            average_processing_time=average_processing_time,
            memory_usage_mb=memory.used / 1024 / 1024,
            cpu_usage_percent=cpu_percent,
            network_requests_per_second=0.0,  # TODO: Implement network monitoring
            error_rate=error_rate,
        )

        return JSONResponse(content=metrics.dict())

    except Exception as e:
        logger.exception(f"Error getting metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/metrics/csv")
async def get_metrics_csv(migration_id: str | None = None) -> JSONResponse:
    """Export metrics as CSV."""
    try:
        # Get current metrics
        metrics_response = await get_metrics(migration_id)
        metrics_data = metrics_response.body

        # Generate CSV content
        csv_content = "timestamp,metric,value\n"
        timestamp = datetime.now(UTC).isoformat()

        for key, value in metrics_data.items():
            if key not in {"migration_id", "throughput_history"}:
                csv_content += f"{timestamp},{key},{value}\n"

        return JSONResponse(
            content={
                "csv_content": csv_content,
                "filename": (
                    f"migration_metrics_{migration_id or 'current'}_"
                    f"{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.csv"
                ),
            },
        )

    except Exception as e:
        logger.exception(f"Error generating CSV: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/migration/status")
async def get_migration_status() -> JSONResponse:
    """Get current migration status."""
    try:
        status = {
            "is_running": migration_state["is_running"],
            "migration_id": migration_state["migration_id"],
            "current_component": migration_state["current_component"],
            "start_time": (
                migration_state["start_time"].isoformat()
                if migration_state["start_time"]
                else None
            ),
            "pause_time": (
                migration_state["pause_time"].isoformat()
                if migration_state["pause_time"]
                else None
            ),
            "total_pause_time": migration_state["total_pause_time"],
            "status": "running" if migration_state["is_running"] else "idle",
        }

        return JSONResponse(content=status)

    except Exception as e:
        logger.exception(f"Error getting migration status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/migration/start")
async def start_migration(
    control: MigrationControl,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """Start migration with specified configuration."""
    try:
        if migration_state["is_running"]:
            raise HTTPException(status_code=400, detail="Migration is already running")

        # Start migration in background
        background_tasks.add_task(start_migration_background, control.config or {})

        return JSONResponse(
            content={
                "message": "Migration started",
                "migration_id": migration_state["migration_id"],
            },
        )

    except Exception as e:
        logger.exception(f"Error starting migration: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/migration/stop")
async def stop_migration(background_tasks: BackgroundTasks) -> JSONResponse:
    """Stop current migration."""
    try:
        if not migration_state["is_running"]:
            raise HTTPException(status_code=400, detail="No migration is running")

        # Stop migration in background
        background_tasks.add_task(stop_migration_background)

        return JSONResponse(content={"message": "Migration stop requested"})

    except Exception as e:
        logger.exception(f"Error stopping migration: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/migration/pause")
async def pause_migration(background_tasks: BackgroundTasks) -> JSONResponse:
    """Pause current migration."""
    try:
        if not migration_state["is_running"]:
            raise HTTPException(status_code=400, detail="No migration is running")

        if migration_state["pause_time"]:
            raise HTTPException(status_code=400, detail="Migration is already paused")

        # Pause migration in background
        background_tasks.add_task(pause_migration_background)

        return JSONResponse(content={"message": "Migration pause requested"})

    except Exception as e:
        logger.exception(f"Error pausing migration: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/migration/resume")
async def resume_migration(background_tasks: BackgroundTasks) -> JSONResponse:
    """Resume paused migration."""
    try:
        if not migration_state["is_running"]:
            raise HTTPException(status_code=400, detail="No migration is running")

        if not migration_state["pause_time"]:
            raise HTTPException(status_code=400, detail="Migration is not paused")

        # Resume migration in background
        background_tasks.add_task(resume_migration_background)

        return JSONResponse(content={"message": "Migration resume requested"})

    except Exception as e:
        logger.exception(f"Error resuming migration: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/ws/dashboard")
async def websocket_dashboard(websocket: WebSocket) -> None:
    """WebSocket endpoint for dashboard updates."""
    await manager.connect(websocket)

    try:
        while True:
            # Handle incoming messages
            data = await websocket.receive_text()
            try:
                message = json.loads(data)

                if message.get("type") == "heartbeat":
                    # Update heartbeat timestamp
                    if websocket in manager.connection_metadata:
                        manager.connection_metadata[websocket]["last_heartbeat"] = (
                            datetime.now(UTC)
                        )

                    # Send heartbeat response
                    await manager.send_personal_message(
                        {
                            "type": "heartbeat_response",
                            "timestamp": datetime.now(UTC).isoformat(),
                        },
                        websocket,
                    )

                elif message.get("type") == "request_status":
                    # Send current status
                    status = await get_migration_status()
                    await manager.send_personal_message(
                        {
                            "type": "status_update",
                            "data": status.body,
                            "timestamp": datetime.now(UTC).isoformat(),
                        },
                        websocket,
                    )

            except json.JSONDecodeError:
                logger.warning("Received invalid JSON from WebSocket")

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.exception(f"WebSocket error: {e}")
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
