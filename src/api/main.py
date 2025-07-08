import asyncio
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

# Import our core infrastructure
from src.core.session_manager import (
    init_session_manager, 
    shutdown_session_manager, 
    SessionConfig,
    get_session_manager
)
from src.core.resource_pool import (
    init_resource_pool, 
    shutdown_resource_pool, 
    ResourceConfig,
    get_resource_pool
)
from src.core.websocket_manager import (
    init_websocket_manager, 
    shutdown_websocket_manager, 
    WebSocketConfig,
    get_websocket_manager
)

# Import API routes
from src.api.routes.agent import agent_router
from src.api.routes.websocket import websocket_router
from src.api.routes.monitoring import monitoring_router

# Import middleware
from src.api.middleware.rate_limiting import RateLimitMiddleware
from src.api.middleware.logging import LoggingMiddleware

# Import configuration
from src.config.settings import Settings, get_settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('./tmp/logs/app.log')
    ]
)
logger = logging.getLogger(__name__)

# Global application state
app_state: Dict[str, Any] = {
    'startup_time': None,
    'shutdown_requested': False,
    'healthy': False
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager - handles startup and shutdown.
    """
    # Startup
    logger.info("🚀 Starting Multi-User Browser Automation Application")
    
    try:
        settings = get_settings()
        
        # Create necessary directories
        os.makedirs('./tmp/logs', exist_ok=True)
        os.makedirs('./tmp/sessions', exist_ok=True)
        os.makedirs('./tmp/downloads', exist_ok=True)
        os.makedirs('./tmp/recordings', exist_ok=True)
        os.makedirs('./tmp/traces', exist_ok=True)
        
        # Initialize core managers
        logger.info("Initializing core infrastructure...")
        
        # Session Manager
        session_config = SessionConfig(
            max_sessions=settings.max_sessions,
            session_timeout_minutes=settings.session_timeout_minutes,
            max_sessions_per_ip=settings.max_sessions_per_ip,
            cleanup_interval_seconds=settings.cleanup_interval_seconds
        )
        await init_session_manager(session_config)
        logger.info("✅ Session Manager initialized")
        
        # Resource Pool
        resource_config = ResourceConfig(
            max_browser_instances=settings.max_browser_instances,
            min_browser_instances=settings.min_browser_instances,
            browser_idle_timeout_minutes=settings.browser_idle_timeout_minutes,
            max_memory_usage_mb=settings.max_memory_usage_mb,
            enable_resource_pooling=settings.enable_resource_pooling,
            enable_memory_monitoring=settings.enable_memory_monitoring,
            enable_automatic_scaling=settings.enable_automatic_scaling
        )
        await init_resource_pool(resource_config)
        logger.info("✅ Resource Pool initialized")
        
        # WebSocket Manager
        websocket_config = WebSocketConfig(
            max_connections=settings.max_websocket_connections,
            max_connections_per_ip=settings.max_websocket_connections_per_ip,
            ping_interval_seconds=settings.websocket_ping_interval,
            rate_limit_messages_per_minute=settings.websocket_rate_limit
        )
        await init_websocket_manager(websocket_config)
        logger.info("✅ WebSocket Manager initialized")
        
        # Set application as healthy
        app_state['healthy'] = True
        app_state['startup_time'] = asyncio.get_event_loop().time()
        
        logger.info("🎉 Application startup complete!")
        logger.info(f"📊 Configuration: {settings.max_sessions} max sessions, {settings.max_browser_instances} max browsers")
        
        yield
        
    except Exception as e:
        logger.error(f"💥 Failed to start application: {e}", exc_info=True)
        app_state['healthy'] = False
        raise
    
    # Shutdown
    logger.info("🔄 Shutting down Multi-User Browser Automation Application")
    app_state['shutdown_requested'] = True
    app_state['healthy'] = False
    
    try:
        # Shutdown managers in reverse order
        logger.info("Shutting down WebSocket Manager...")
        await shutdown_websocket_manager()
        
        logger.info("Shutting down Resource Pool...")
        await shutdown_resource_pool()
        
        logger.info("Shutting down Session Manager...")
        await shutdown_session_manager()
        
        logger.info("✅ Application shutdown complete")
        
    except Exception as e:
        logger.error(f"Error during shutdown: {e}", exc_info=True)


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.
    """
    settings = get_settings()
    
    # Create FastAPI app with lifespan
    app = FastAPI(
        title="Multi-User Browser Automation API",
        description="Scalable browser automation supporting 1000+ concurrent users",
        version="2.0.0",
        docs_url="/docs" if settings.enable_api_docs else None,
        redoc_url="/redoc" if settings.enable_api_docs else None,
        lifespan=lifespan
    )
    
    # Add middleware
    
    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Gzip compression
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    
    # Custom middleware
    app.add_middleware(LoggingMiddleware)
    
    if settings.enable_rate_limiting:
        app.add_middleware(
            RateLimitMiddleware,
            requests_per_minute=settings.api_rate_limit_per_minute,
            burst_size=settings.api_rate_limit_burst
        )
    
    # Include routers
    app.include_router(agent_router, prefix="/api/v1")
    app.include_router(websocket_router, prefix="/ws")
    app.include_router(monitoring_router, prefix="/api/v1")
    
    # Static files (for web interface)
    if os.path.exists("static"):
        app.mount("/static", StaticFiles(directory="static"), name="static")
    
    # Root endpoints
    
    @app.get("/", response_class=FileResponse)
    async def read_index():
        """Serve the main web interface"""
        if os.path.exists("static/index.html"):
            return FileResponse("static/index.html")
        else:
            return JSONResponse(
                content={
                    "message": "Multi-User Browser Automation API", 
                    "version": "2.0.0",
                    "docs": "/docs",
                    "health": "/health"
                }
            )
    
    @app.get("/health")
    async def health_check():
        """Health check endpoint"""
        try:
            if not app_state['healthy']:
                raise HTTPException(status_code=503, detail="Application not healthy")
            
            # Check core managers
            session_manager = get_session_manager()
            resource_pool = get_resource_pool() 
            websocket_manager = get_websocket_manager()
            
            session_count = await session_manager.get_session_count()
            resource_stats = await resource_pool.get_resource_stats()
            ws_stats = await websocket_manager.get_statistics()
            
            return {
                "status": "healthy",
                "timestamp": asyncio.get_event_loop().time(),
                "uptime_seconds": asyncio.get_event_loop().time() - app_state['startup_time'],
                "sessions": {
                    "active": session_count,
                    "max": settings.max_sessions
                },
                "resources": {
                    "browsers": resource_stats['current_browsers'],
                    "contexts": resource_stats['current_contexts'],
                    "memory_mb": resource_stats['current_memory_usage_mb'],
                    "pool_health": resource_stats['pool_health']
                },
                "websockets": {
                    "connections": ws_stats['current_connections'],
                    "max": settings.max_websocket_connections
                }
            }
            
        except Exception as e:
            logger.error(f"Health check failed: {e}", exc_info=True)
            raise HTTPException(status_code=503, detail=f"Health check failed: {str(e)}")
    
    @app.get("/info")
    async def app_info():
        """Application information endpoint"""
        return {
            "name": "Multi-User Browser Automation API",
            "version": "2.0.0",
            "description": "Scalable browser automation supporting 1000+ concurrent users",
            "features": [
                "Multi-user session management",
                "Browser instance pooling", 
                "Real-time WebSocket communication",
                "Automatic resource cleanup",
                "Memory management and monitoring",
                "Rate limiting and security"
            ],
            "limits": {
                "max_sessions": settings.max_sessions,
                "max_sessions_per_ip": settings.max_sessions_per_ip,
                "max_browser_instances": settings.max_browser_instances,
                "max_websocket_connections": settings.max_websocket_connections
            }
        }
    
    # Global exception handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """Global exception handler for unhandled errors"""
        logger.error(f"Unhandled exception in {request.method} {request.url}: {exc}", exc_info=True)
        
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal server error",
                "message": "An unexpected error occurred",
                "request_id": getattr(request.state, 'request_id', 'unknown')
            }
        )
    
    # Graceful shutdown handler
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        app_state['shutdown_requested'] = True
    
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    return app


# Create the app instance
app = create_app()


def main():
    """
    Main entry point for running the application.
    """
    settings = get_settings()
    
    logger.info(f"Starting server on {settings.host}:{settings.port}")
    logger.info(f"Environment: {settings.environment}")
    logger.info(f"Debug mode: {settings.debug}")
    
    # Run the server
    uvicorn.run(
        "src.api.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug and settings.environment == "development",
        log_level="info" if not settings.debug else "debug",
        access_log=True,
        loop="asyncio",
        # Production settings
        workers=1,  # Single worker for shared state management
        limit_concurrency=settings.max_sessions * 2,  # Limit concurrent requests
        limit_max_requests=10000,  # Restart worker after N requests
        timeout_keep_alive=30,  # Keep-alive timeout
    )


if __name__ == "__main__":
    main()