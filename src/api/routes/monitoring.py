import asyncio
import logging
import psutil
import platform
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from fastapi.responses import JSONResponse

from src.core.session_manager import get_session_manager
from src.core.resource_pool import get_resource_pool
from src.core.websocket_manager import get_websocket_manager
from src.config.settings import get_settings
from src.utils.auth import require_admin_access

logger = logging.getLogger(__name__)

# Create the router
monitoring_router = APIRouter(
    prefix="/monitoring",
    tags=["System Monitoring"],
    responses={
        403: {"description": "Admin access required"},
        500: {"description": "Internal server error"}
    }
)


@monitoring_router.get("/health")
async def detailed_health_check() -> Dict[str, Any]:
    """
    Comprehensive health check with detailed system information.
    """
    try:
        start_time = asyncio.get_event_loop().time()
        
        # Get system information
        system_info = {
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "cpu_count": psutil.cpu_count(),
            "memory_total_gb": round(psutil.virtual_memory().total / (1024**3), 2),
            "disk_total_gb": round(psutil.disk_usage('/').total / (1024**3), 2)
        }
        
        # Get managers
        session_manager = get_session_manager()
        resource_pool = get_resource_pool()
        websocket_manager = get_websocket_manager()
        
        # Collect health data
        session_stats = await session_manager.get_statistics()
        resource_stats = await resource_pool.get_resource_stats()
        websocket_stats = await websocket_manager.get_statistics()
        
        # System metrics
        cpu_percent = psutil.cpu_percent(interval=0.1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        # Calculate health score
        health_score = _calculate_health_score(
            cpu_percent, memory.percent, resource_stats, session_stats
        )
        
        health_status = "healthy"
        if health_score < 0.5:
            health_status = "critical"
        elif health_score < 0.7:
            health_status = "degraded"
        elif health_score < 0.9:
            health_status = "warning"
        
        response_time = asyncio.get_event_loop().time() - start_time
        
        return {
            "status": health_status,
            "health_score": round(health_score, 3),
            "response_time_ms": round(response_time * 1000, 2),
            "timestamp": datetime.now().isoformat(),
            "system": system_info,
            "resources": {
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent,
                "memory_available_gb": round(memory.available / (1024**3), 2),
                "disk_percent": disk.percent,
                "disk_free_gb": round(disk.free / (1024**3), 2)
            },
            "sessions": {
                "active": session_stats["current_active_sessions"],
                "total_created": session_stats["total_sessions_created"],
                "total_cleaned": session_stats["total_sessions_cleaned"],
                "max_concurrent": session_stats["max_concurrent_sessions"]
            },
            "browser_pool": {
                "browsers": resource_stats["current_browsers"],
                "contexts": resource_stats["current_contexts"],
                "available_browsers": resource_stats["available_browsers"],
                "allocated_browsers": resource_stats["allocated_browsers"],
                "memory_mb": resource_stats["current_memory_usage_mb"],
                "pool_health": resource_stats["pool_health"]
            },
            "websockets": {
                "connections": websocket_stats["current_connections"],
                "total_messages_sent": websocket_stats["total_messages_sent"],
                "total_messages_received": websocket_stats["total_messages_received"],
                "total_errors": websocket_stats["total_errors"]
            }
        }
        
    except Exception as e:
        logger.error(f"Health check failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Health check failed: {str(e)}")


@monitoring_router.get("/metrics")
async def get_system_metrics() -> Dict[str, Any]:
    """
    Get detailed system metrics for monitoring dashboards.
    """
    try:
        # System metrics
        cpu_times = psutil.cpu_times()
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        network = psutil.net_io_counters()
        
        # Process metrics
        process = psutil.Process()
        process_memory = process.memory_info()
        
        # Get manager statistics
        session_manager = get_session_manager()
        resource_pool = get_resource_pool()
        websocket_manager = get_websocket_manager()
        
        session_stats = await session_manager.get_statistics()
        resource_stats = await resource_pool.get_resource_stats()
        websocket_stats = await websocket_manager.get_statistics()
        
        return {
            "timestamp": datetime.now().isoformat(),
            "system": {
                "cpu": {
                    "percent": psutil.cpu_percent(interval=0.1),
                    "user": cpu_times.user,
                    "system": cpu_times.system,
                    "idle": cpu_times.idle,
                    "load_average": psutil.getloadavg() if hasattr(psutil, 'getloadavg') else None
                },
                "memory": {
                    "total_bytes": memory.total,
                    "available_bytes": memory.available,
                    "used_bytes": memory.used,
                    "percent": memory.percent,
                    "cached_bytes": getattr(memory, 'cached', 0),
                    "buffers_bytes": getattr(memory, 'buffers', 0)
                },
                "disk": {
                    "total_bytes": disk.total,
                    "used_bytes": disk.used,
                    "free_bytes": disk.free,
                    "percent": disk.percent
                },
                "network": {
                    "bytes_sent": network.bytes_sent,
                    "bytes_recv": network.bytes_recv,
                    "packets_sent": network.packets_sent,
                    "packets_recv": network.packets_recv,
                    "errors_in": network.errin,
                    "errors_out": network.errout
                }
            },
            "process": {
                "memory_rss_bytes": process_memory.rss,
                "memory_vms_bytes": process_memory.vms,
                "cpu_percent": process.cpu_percent(),
                "open_files": len(process.open_files()),
                "connections": len(process.connections()),
                "threads": process.num_threads(),
                "create_time": process.create_time()
            },
            "application": {
                "sessions": session_stats,
                "resources": resource_stats,
                "websockets": websocket_stats
            }
        }
        
    except Exception as e:
        logger.error(f"Failed to get metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get metrics: {str(e)}")


@monitoring_router.get("/sessions/stats")
async def get_session_statistics(
    include_details: bool = Query(False, description="Include detailed session information")
) -> Dict[str, Any]:
    """
    Get detailed session statistics.
    """
    try:
        session_manager = get_session_manager()
        stats = await session_manager.get_statistics()
        
        result = {
            "timestamp": datetime.now().isoformat(),
            "summary": stats,
        }
        
        if include_details:
            # Add more detailed information (admin only in production)
            settings = get_settings()
            if settings.environment != "production":
                result["details"] = {
                    "session_distribution": stats.get("sessions_by_ip", {}),
                    "limits": {
                        "max_sessions": settings.max_sessions,
                        "max_sessions_per_ip": settings.max_sessions_per_ip,
                        "session_timeout_minutes": settings.session_timeout_minutes
                    }
                }
        
        return result
        
    except Exception as e:
        logger.error(f"Failed to get session statistics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get session statistics: {str(e)}")


@monitoring_router.get("/resources/stats")
async def get_resource_statistics() -> Dict[str, Any]:
    """
    Get detailed resource pool statistics.
    """
    try:
        resource_pool = get_resource_pool()
        stats = await resource_pool.get_resource_stats()
        
        # Add utilization calculations
        browser_utilization = 0
        if stats["current_browsers"] > 0:
            browser_utilization = (stats["allocated_browsers"] / stats["current_browsers"]) * 100
        
        memory_utilization = (stats["current_memory_usage_mb"] / stats["memory_limit_mb"]) * 100
        
        return {
            "timestamp": datetime.now().isoformat(),
            "summary": stats,
            "utilization": {
                "browser_percent": round(browser_utilization, 1),
                "memory_percent": round(memory_utilization, 1)
            },
            "efficiency": {
                "browsers_per_session": round(
                    stats["current_browsers"] / max(stats["active_sessions"], 1), 2
                ),
                "memory_per_browser_mb": round(
                    stats["current_memory_usage_mb"] / max(stats["current_browsers"], 1), 1
                )
            }
        }
        
    except Exception as e:
        logger.error(f"Failed to get resource statistics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get resource statistics: {str(e)}")


@monitoring_router.get("/websockets/stats")
async def get_websocket_statistics() -> Dict[str, Any]:
    """
    Get detailed WebSocket statistics.
    """
    try:
        websocket_manager = get_websocket_manager()
        stats = await websocket_manager.get_statistics()
        
        # Calculate additional metrics
        total_connections = stats["total_connections"]
        if total_connections > 0:
            error_rate = (stats["total_errors"] / total_connections) * 100
            avg_messages_per_connection = stats["total_messages_sent"] / total_connections
        else:
            error_rate = 0
            avg_messages_per_connection = 0
        
        return {
            "timestamp": datetime.now().isoformat(),
            "summary": stats,
            "performance": {
                "error_rate_percent": round(error_rate, 2),
                "avg_messages_per_connection": round(avg_messages_per_connection, 1),
                "connection_success_rate": round(
                    ((total_connections - stats["total_errors"]) / max(total_connections, 1)) * 100, 2
                )
            }
        }
        
    except Exception as e:
        logger.error(f"Failed to get WebSocket statistics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get WebSocket statistics: {str(e)}")


@monitoring_router.get("/performance")
async def get_performance_metrics(
    duration_minutes: int = Query(5, ge=1, le=60, description="Duration to monitor performance")
) -> Dict[str, Any]:
    """
    Get performance metrics over a specified duration.
    """
    try:
        start_time = asyncio.get_event_loop().time()
        
        # Collect initial metrics
        initial_cpu = psutil.cpu_percent()
        initial_memory = psutil.virtual_memory()
        
        # Get application metrics
        session_manager = get_session_manager()
        resource_pool = get_resource_pool()
        
        initial_session_stats = await session_manager.get_statistics()
        initial_resource_stats = await resource_pool.get_resource_stats()
        
        # Simulate monitoring (in production, this would query a time-series database)
        await asyncio.sleep(min(duration_minutes * 60, 10))  # Cap at 10 seconds for API response
        
        # Collect final metrics
        final_cpu = psutil.cpu_percent()
        final_memory = psutil.virtual_memory()
        
        final_session_stats = await session_manager.get_statistics()
        final_resource_stats = await resource_pool.get_resource_stats()
        
        # Calculate deltas
        session_delta = final_session_stats["current_active_sessions"] - initial_session_stats["current_active_sessions"]
        browser_delta = final_resource_stats["current_browsers"] - initial_resource_stats["current_browsers"]
        memory_delta = final_resource_stats["current_memory_usage_mb"] - initial_resource_stats["current_memory_usage_mb"]
        
        response_time = asyncio.get_event_loop().time() - start_time
        
        return {
            "timestamp": datetime.now().isoformat(),
            "monitoring_duration_seconds": response_time,
            "requested_duration_minutes": duration_minutes,
            "performance": {
                "cpu": {
                    "initial_percent": initial_cpu,
                    "final_percent": final_cpu,
                    "change": round(final_cpu - initial_cpu, 1)
                },
                "memory": {
                    "initial_percent": initial_memory.percent,
                    "final_percent": final_memory.percent,
                    "change": round(final_memory.percent - initial_memory.percent, 1)
                },
                "sessions": {
                    "initial": initial_session_stats["current_active_sessions"],
                    "final": final_session_stats["current_active_sessions"],
                    "change": session_delta
                },
                "browsers": {
                    "initial": initial_resource_stats["current_browsers"],
                    "final": final_resource_stats["current_browsers"],
                    "change": browser_delta
                },
                "memory_usage": {
                    "initial_mb": initial_resource_stats["current_memory_usage_mb"],
                    "final_mb": final_resource_stats["current_memory_usage_mb"],
                    "change_mb": round(memory_delta, 1)
                }
            }
        }
        
    except Exception as e:
        logger.error(f"Failed to get performance metrics: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get performance metrics: {str(e)}")


@monitoring_router.get("/alerts")
async def get_system_alerts() -> Dict[str, Any]:
    """
    Get current system alerts and warnings.
    """
    try:
        alerts = []
        warnings = []
        info = []
        # Memory alerts
        if memory.percent > 90:
            alerts.append({
                "type": "memory_critical",
                "message": f"Memory usage critical: {memory.percent:.1f}%",
                "value": memory.percent,
                "threshold": 90
            })
        elif memory.percent > 75:
            warnings.append({
                "type": "memory_high",
                "message": f"Memory usage high: {memory.percent:.1f}%",
                "value": memory.percent,
                "threshold": 75
            })
        
        # Disk alerts
        if disk.percent > 95:
            alerts.append({
                "type": "disk_critical",
                "message": f"Disk usage critical: {disk.percent:.1f}%",
                "value": disk.percent,
                "threshold": 95
            })
        elif disk.percent > 85:
            warnings.append({
                "type": "disk_high",
                "message": f"Disk usage high: {disk.percent:.1f}%",
                "value": disk.percent,
                "threshold": 85
            })
        
        # Application-specific alerts
        session_manager = get_session_manager()
        resource_pool = get_resource_pool()
        websocket_manager = get_websocket_manager()
        
        session_stats = await session_manager.get_statistics()
        resource_stats = await resource_pool.get_resource_stats()
        websocket_stats = await websocket_manager.get_statistics()
        
        settings = get_settings()
        
        # Session alerts
        session_utilization = (session_stats["current_active_sessions"] / settings.max_sessions) * 100
        if session_utilization > 90:
            alerts.append({
                "type": "sessions_critical",
                "message": f"Session capacity critical: {session_utilization:.1f}%",
                "value": session_stats["current_active_sessions"],
                "threshold": settings.max_sessions * 0.9
            })
        elif session_utilization > 75:
            warnings.append({
                "type": "sessions_high",
                "message": f"Session capacity high: {session_utilization:.1f}%",
                "value": session_stats["current_active_sessions"],
                "threshold": settings.max_sessions * 0.75
            })
        
        # Browser pool alerts
        if resource_stats["pool_health"] == "critical":
            alerts.append({
                "type": "browser_pool_critical",
                "message": "Browser pool in critical state",
                "value": resource_stats["pool_health"],
                "details": f"Available browsers: {resource_stats['available_browsers']}"
            })
        elif resource_stats["pool_health"] in ["overloaded", "high_memory"]:
            warnings.append({
                "type": "browser_pool_warning",
                "message": f"Browser pool health: {resource_stats['pool_health']}",
                "value": resource_stats["pool_health"],
                "details": f"Available browsers: {resource_stats['available_browsers']}"
            })
        
        # WebSocket alerts
        ws_utilization = (websocket_stats["current_connections"] / settings.max_websocket_connections) * 100
        if ws_utilization > 90:
            alerts.append({
                "type": "websocket_critical",
                "message": f"WebSocket capacity critical: {ws_utilization:.1f}%",
                "value": websocket_stats["current_connections"],
                "threshold": settings.max_websocket_connections * 0.9
            })
        elif ws_utilization > 75:
            warnings.append({
                "type": "websocket_high",
                "message": f"WebSocket capacity high: {ws_utilization:.1f}%",
                "value": websocket_stats["current_connections"],
                "threshold": settings.max_websocket_connections * 0.75
            })
        
        # Error rate alerts
        total_connections = websocket_stats["total_connections"]
        if total_connections > 0:
            error_rate = (websocket_stats["total_errors"] / total_connections) * 100
            if error_rate > 10:
                alerts.append({
                    "type": "error_rate_high",
                    "message": f"WebSocket error rate high: {error_rate:.1f}%",
                    "value": error_rate,
                    "threshold": 10
                })
            elif error_rate > 5:
                warnings.append({
                    "type": "error_rate_elevated",
                    "message": f"WebSocket error rate elevated: {error_rate:.1f}%",
                    "value": error_rate,
                    "threshold": 5
                })
        
        # Performance info
        if session_stats["current_active_sessions"] > 0:
            avg_browsers_per_session = resource_stats["current_browsers"] / session_stats["current_active_sessions"]
            info.append({
                "type": "performance_info",
                "message": f"Average browsers per session: {avg_browsers_per_session:.1f}",
                "value": avg_browsers_per_session
            })
        
        # Calculate overall alert level
        alert_level = "healthy"
        if alerts:
            alert_level = "critical"
        elif warnings:
            alert_level = "warning"
        elif info:
            alert_level = "info"
        
        return {
            "timestamp": datetime.now().isoformat(),
            "alert_level": alert_level,
            "summary": {
                "critical_alerts": len(alerts),
                "warnings": len(warnings),
                "info_messages": len(info)
            },
            "alerts": alerts,
            "warnings": warnings,
            "info": info
        }
        
    except Exception as e:
        logger.error(f"Failed to get system alerts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get system alerts: {str(e)}")


@monitoring_router.get("/logs")
async def get_recent_logs(
    level: str = Query("INFO", description="Log level filter"),
    limit: int = Query(100, ge=1, le=1000, description="Number of log entries"),
    component: Optional[str] = Query(None, description="Filter by component")
) -> Dict[str, Any]:
    """
    Get recent log entries.
    Note: This is a simplified implementation. In production, integrate with proper log aggregation.
    """
    try:
        # This is a placeholder implementation
        # In production, you would integrate with your logging system
        log_entries = [
            {
                "timestamp": datetime.now().isoformat(),
                "level": "INFO",
                "component": "session_manager",
                "message": "Session cleanup completed",
                "session_id": "example-session-123"
            },
            {
                "timestamp": (datetime.now() - timedelta(minutes=1)).isoformat(),
                "level": "INFO", 
                "component": "resource_pool",
                "message": "Browser instance created",
                "browser_id": "browser_123456"
            }
        ]
        
        return {
            "timestamp": datetime.now().isoformat(),
            "filter": {
                "level": level,
                "component": component,
                "limit": limit
            },
            "total_entries": len(log_entries),
            "entries": log_entries
        }
        
    except Exception as e:
        logger.error(f"Failed to get logs: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get logs: {str(e)}")


@monitoring_router.post("/maintenance")
async def trigger_maintenance_tasks(
    task: str = Query(..., description="Maintenance task to execute"),
    admin_access: bool = Depends(require_admin_access)
) -> Dict[str, Any]:
    """
    Trigger maintenance tasks.
    Requires admin access.
    """
    try:
        if not admin_access:
            raise HTTPException(status_code=403, detail="Admin access required")
        
        results = {}
        
        if task == "cleanup_sessions":
            session_manager = get_session_manager()
            # Force cleanup of expired sessions
            initial_count = await session_manager.get_session_count()
            await session_manager._cleanup_expired_sessions()
            final_count = await session_manager.get_session_count()
            
            results = {
                "task": "cleanup_sessions",
                "sessions_before": initial_count,
                "sessions_after": final_count,
                "sessions_cleaned": initial_count - final_count
            }
            
        elif task == "cleanup_resources":
            resource_pool = get_resource_pool()
            # Force cleanup of idle resources
            await resource_pool._cleanup_idle_resources()
            await resource_pool._cleanup_old_resources()
            
            stats = await resource_pool.get_resource_stats()
            results = {
                "task": "cleanup_resources",
                "current_browsers": stats["current_browsers"],
                "current_contexts": stats["current_contexts"],
                "pool_health": stats["pool_health"]
            }
            
        elif task == "cleanup_websockets":
            websocket_manager = get_websocket_manager()
            # Force cleanup of stale connections
            await websocket_manager._cleanup_stale_connections()
            
            stats = await websocket_manager.get_statistics()
            results = {
                "task": "cleanup_websockets",
                "current_connections": stats["current_connections"]
            }
            
        elif task == "force_gc":
            import gc
            # Force garbage collection
            collected = gc.collect()
            results = {
                "task": "force_gc",
                "objects_collected": collected
            }
            
        else:
            raise HTTPException(status_code=400, detail=f"Unknown maintenance task: {task}")
        
        logger.info(f"Maintenance task '{task}' executed successfully")
        
        return {
            "timestamp": datetime.now().isoformat(),
            "status": "completed",
            "results": results
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to execute maintenance task '{task}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to execute maintenance task: {str(e)}")


# Helper functions

def _calculate_health_score(cpu_percent: float, memory_percent: float, 
                          resource_stats: Dict[str, Any], session_stats: Dict[str, Any]) -> float:
    """Calculate an overall health score from 0.0 to 1.0"""
    
    # CPU score (inverse of utilization)
    cpu_score = max(0, 1 - (cpu_percent / 100))
    
    # Memory score (inverse of utilization)
    memory_score = max(0, 1 - (memory_percent / 100))
    
    # Resource pool health score
    pool_health = resource_stats.get("pool_health", "unknown")
    if pool_health == "healthy":
        pool_score = 1.0
    elif pool_health == "warning":
        pool_score = 0.7
    elif pool_health == "high_memory":
        pool_score = 0.6
    elif pool_health == "overloaded":
        pool_score = 0.4
    elif pool_health == "critical":
        pool_score = 0.2
    else:
        pool_score = 0.5  # unknown
    
    # Session utilization score
    settings = get_settings()
    session_utilization = session_stats["current_active_sessions"] / settings.max_sessions
    session_score = max(0, 1 - session_utilization)
    
    # Weighted average
    weights = {
        'cpu': 0.2,
        'memory': 0.3,
        'pool': 0.3,
        'sessions': 0.2
    }
    
    health_score = (
        cpu_score * weights['cpu'] +
        memory_score * weights['memory'] +
        pool_score * weights['pool'] +
        session_score * weights['sessions']
    )
    
    return min(1.0, max(0.0, health_score))