import asyncio
import platform
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from typing import Dict, Any

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

# Core infrastructure
from src.core.session_manager import init_session_manager, shutdown_session_manager, SessionConfig
from src.core.resource_pool import init_resource_pool, shutdown_resource_pool, ResourceConfig
from src.core.websocket_manager import init_websocket_manager, shutdown_websocket_manager, WebSocketConfig
from src.api.routes.agent import agent_router
from src.api.routes.websocket import websocket_router
from src.api.routes.monitoring import monitoring_router
from src.api.middleware.rate_limiting import RateLimitMiddleware
from src.api.middleware.logging import LoggingMiddleware
from src.config.settings import get_settings

# This platform-specific fix MUST be the first piece of code to run.
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('./tmp/logs/app.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# Global state
app_state: Dict[str, Any] = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("🚀 Starting Multi-User Browser Automation Application")
    settings = get_settings()

    for path in ['./tmp/logs', './tmp/sessions', './tmp/downloads', './tmp/recordings', './tmp/traces']:
        os.makedirs(path, exist_ok=True)

    logger.info("Initializing core infrastructure...")

    # --- THE FIX IS HERE ---
    # Reverted to direct attribute access for configuration. This is the correct way.
    session_config = SessionConfig(
        max_sessions=settings.session_manager.max_sessions,
        session_timeout_minutes=settings.session_manager.session_timeout_minutes,
        max_sessions_per_ip=settings.session_manager.max_sessions_per_ip,
        cleanup_interval_seconds=settings.session_manager.cleanup_interval_seconds
    )
    await init_session_manager(session_config)
    logger.info("✅ Session Manager initialized")

    resource_config = ResourceConfig(
        max_browser_instances=settings.resource_pool.max_browser_instances,
        min_browser_instances=settings.resource_pool.min_browser_instances,
        browser_idle_timeout_minutes=settings.resource_pool.browser_idle_timeout_minutes,
        browser_max_age_hours=settings.resource_pool.browser_max_age_hours,
        max_contexts_per_browser=settings.resource_pool.max_contexts_per_browser,
        context_idle_timeout_minutes=settings.resource_pool.context_idle_timeout_minutes,
        max_memory_usage_mb=settings.resource_pool.max_memory_usage_mb,
        max_memory_per_browser_mb=settings.resource_pool.max_memory_per_browser_mb,
        cleanup_interval_seconds=settings.resource_pool.cleanup_interval_seconds,
        health_check_interval_seconds=settings.resource_pool.health_check_interval_seconds,
        enable_resource_pooling=settings.resource_pool.enable_resource_pooling,
        enable_memory_monitoring=settings.resource_pool.enable_memory_monitoring,
        enable_automatic_scaling=settings.resource_pool.enable_automatic_scaling
    )
    await init_resource_pool(resource_config)
    logger.info("✅ Resource Pool initialized")

    websocket_config = WebSocketConfig(
        max_connections=settings.websocket.max_connections,
        max_connections_per_ip=settings.websocket.max_connections_per_ip,
        ping_interval_seconds=settings.websocket.ping_interval_seconds,
        connection_timeout_seconds=settings.websocket.connection_timeout_seconds,
        max_message_size=settings.websocket.max_message_size,
        rate_limit_messages_per_minute=settings.websocket.rate_limit_messages_per_minute,
        enable_compression=settings.websocket.enable_compression,
        heartbeat_timeout_seconds=settings.websocket.heartbeat_timeout_seconds
    )
    await init_websocket_manager(websocket_config)
    logger.info("✅ WebSocket Manager initialized")

    app_state['healthy'] = True
    app_state['startup_time'] = asyncio.get_event_loop().time()
    logger.info("🎉 Application startup complete!")

    yield

    # Shutdown
    logger.info("🔄 Shutting down application")
    await shutdown_websocket_manager()
    await shutdown_resource_pool()
    await shutdown_session_manager()
    logger.info("✅ Application shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Multi-User Browser Automation API",
        version="2.0.0",
        docs_url="/docs" if settings.enable_api_docs else None,
        redoc_url="/redoc" if settings.enable_api_docs else None,
        lifespan=lifespan
    )

    # Middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.security.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(LoggingMiddleware)
    if settings.security.enable_rate_limiting:
        app.add_middleware(
            RateLimitMiddleware,
            requests_per_minute=settings.security.api_rate_limit_per_minute,
            burst_size=settings.security.api_rate_limit_burst
        )

    # Routers
    app.include_router(agent_router, prefix="/api/v1")
    app.include_router(websocket_router, prefix="/ws")
    app.include_router(monitoring_router, prefix="/api/v1")

    return app

app = create_app()

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level="info"
    )