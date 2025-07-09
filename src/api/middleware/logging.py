import asyncio
import logging
import time
import uuid
from typing import Callable, Dict, Any, Optional
from datetime import datetime, timedelta
import json
import traceback

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse
import psutil

logger = logging.getLogger(__name__)


class LoggingMiddleware(BaseHTTPMiddleware):
    """
    Custom logging middleware for FastAPI applications.
    Provides detailed request/response logging, performance metrics, and error tracking.
    """

    def __init__(self, app, exclude_paths: list = None, log_body: bool = False, 
                 log_headers: bool = False, max_body_size: int = 1024):
        super().__init__(app)
        self.exclude_paths = exclude_paths or ["/health", "/metrics", "/favicon.ico"]
        self.log_body = log_body
        self.log_headers = log_headers
        self.max_body_size = max_body_size
        
        # Performance tracking
        self.request_count = 0
        self.error_count = 0
        self.response_times = []
        self.start_time = time.time()
        
        # Rate limiting for logging (prevent log spam)
        self.log_rate_limit = {}
        self.log_rate_window = 60  # seconds
        self.max_logs_per_window = 100

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Generate unique request ID
        request_id = str(uuid.uuid4())[:8]
        
        # Skip logging for excluded paths
        if any(request.url.path.startswith(path) for path in self.exclude_paths):
            response = await call_next(request)
            return response
        
        # Check rate limiting for this IP
        client_ip = self._get_client_ip(request)
        if not self._check_rate_limit(client_ip):
            response = await call_next(request)
            return response
        
        start_time = time.time()
        
        # Log request
        await self._log_request(request, request_id)
        
        # Process request and handle errors
        try:
            response = await call_next(request)
            
            # Calculate response time
            end_time = time.time()
            response_time = (end_time - start_time) * 1000  # ms
            
            # Log response
            await self._log_response(request, response, request_id, response_time)
            
            # Add request ID to response headers
            response.headers["X-Request-ID"] = request_id
            
            # Update metrics
            self._update_metrics(response_time, response.status_code)
            
            return response
            
        except Exception as e:
            # Calculate response time for error
            end_time = time.time()
            response_time = (end_time - start_time) * 1000  # ms
            
            # Log error
            await self._log_error(request, e, request_id, response_time)
            
            # Update error metrics
            self.error_count += 1
            
            # Re-raise the exception
            raise

    def _get_client_ip(self, request: Request) -> str:
        """Get client IP address from request"""
        # Check for forwarded headers first
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip
        
        # Fallback to direct client
        if hasattr(request.client, 'host'):
            return request.client.host
        
        return "unknown"

    def _check_rate_limit(self, client_ip: str) -> bool:
        """Check if logging is rate limited for this IP"""
        now = time.time()
        window_start = now - self.log_rate_window
        
        # Clean old entries
        if client_ip in self.log_rate_limit:
            self.log_rate_limit[client_ip] = [
                timestamp for timestamp in self.log_rate_limit[client_ip] 
                if timestamp > window_start
            ]
        else:
            self.log_rate_limit[client_ip] = []
        
        # Check limit
        if len(self.log_rate_limit[client_ip]) >= self.max_logs_per_window:
            return False
        
        # Add current request
        self.log_rate_limit[client_ip].append(now)
        return True

    async def _log_request(self, request: Request, request_id: str):
        """Log incoming request details"""
        try:
            client_ip = self._get_client_ip(request)
            user_agent = request.headers.get("User-Agent", "Unknown")
            
            log_data = {
                "event": "request_start",
                "request_id": request_id,
                "timestamp": datetime.now().isoformat(),
                "method": request.method,
                "url": str(request.url),
                "path": request.url.path,
                "query_params": dict(request.query_params),
                "client_ip": client_ip,
                "user_agent": user_agent,
            }
            
            # Add headers if enabled
            if self.log_headers:
                log_data["headers"] = dict(request.headers)
            
            # Add body if enabled and present
            if self.log_body and request.method in ["POST", "PUT", "PATCH"]:
                try:
                    body = await self._get_request_body(request)
                    if body and len(body) <= self.max_body_size:
                        log_data["body"] = body
                except Exception as e:
                    log_data["body_error"] = str(e)
            
            logger.info(f"REQUEST {request_id}", extra={"log_data": log_data})
            
        except Exception as e:
            logger.error(f"Error logging request {request_id}: {e}")

    async def _log_response(self, request: Request, response: Response, 
                          request_id: str, response_time: float):
        """Log response details"""
        try:
            log_data = {
                "event": "request_complete",
                "request_id": request_id,
                "timestamp": datetime.now().isoformat(),
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "response_time_ms": round(response_time, 2),
                "content_length": response.headers.get("content-length", "unknown"),
            }
            
            # Add response headers if enabled
            if self.log_headers:
                log_data["response_headers"] = dict(response.headers)
            
            # Determine log level based on status code
            if response.status_code >= 500:
                log_level = logging.ERROR
            elif response.status_code >= 400:
                log_level = logging.WARNING
            elif response_time > 5000:  # Slow request (>5s)
                log_level = logging.WARNING
            else:
                log_level = logging.INFO
            
            logger.log(log_level, f"RESPONSE {request_id}", extra={"log_data": log_data})
            
        except Exception as e:
            logger.error(f"Error logging response {request_id}: {e}")

    async def _log_error(self, request: Request, error: Exception, 
                        request_id: str, response_time: float):
        """Log error details"""
        try:
            log_data = {
                "event": "request_error",
                "request_id": request_id,
                "timestamp": datetime.now().isoformat(),
                "method": request.method,
                "path": request.url.path,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "response_time_ms": round(response_time, 2),
                "traceback": traceback.format_exc(),
            }
            
            logger.error(f"ERROR {request_id}", extra={"log_data": log_data})
            
        except Exception as e:
            logger.error(f"Error logging error {request_id}: {e}")

    async def _get_request_body(self, request: Request) -> Optional[str]:
        """Get request body for logging"""
        try:
            body = await request.body()
            if not body:
                return None
            
            # Try to decode as text
            try:
                body_text = body.decode('utf-8')
                
                # Try to parse as JSON for better formatting
                try:
                    json_data = json.loads(body_text)
                    return json.dumps(json_data, indent=2)
                except json.JSONDecodeError:
                    return body_text
                    
            except UnicodeDecodeError:
                return f"<binary data: {len(body)} bytes>"
                
        except Exception:
            return None

    def _update_metrics(self, response_time: float, status_code: int):
        """Update internal metrics"""
        self.request_count += 1
        self.response_times.append(response_time)
        
        # Keep only last 1000 response times
        if len(self.response_times) > 1000:
            self.response_times = self.response_times[-1000:]

    def get_metrics(self) -> Dict[str, Any]:
        """Get middleware metrics"""
        now = time.time()
        uptime = now - self.start_time
        
        avg_response_time = (
            sum(self.response_times) / len(self.response_times) 
            if self.response_times else 0
        )
        
        # Calculate percentiles
        sorted_times = sorted(self.response_times)
        p95_response_time = 0
        p99_response_time = 0
        
        if sorted_times:
            p95_index = int(len(sorted_times) * 0.95)
            p99_index = int(len(sorted_times) * 0.99)
            p95_response_time = sorted_times[p95_index] if p95_index < len(sorted_times) else sorted_times[-1]
            p99_response_time = sorted_times[p99_index] if p99_index < len(sorted_times) else sorted_times[-1]
        
        return {
            "middleware": "logging",
            "uptime_seconds": round(uptime, 2),
            "total_requests": self.request_count,
            "total_errors": self.error_count,
            "error_rate": (self.error_count / max(self.request_count, 1)) * 100,
            "requests_per_second": self.request_count / max(uptime, 1),
            "avg_response_time_ms": round(avg_response_time, 2),
            "p95_response_time_ms": round(p95_response_time, 2),
            "p99_response_time_ms": round(p99_response_time, 2),
            "active_rate_limits": len(self.log_rate_limit),
        }

    def reset_metrics(self):
        """Reset internal metrics"""
        self.request_count = 0
        self.error_count = 0
        self.response_times = []
        self.start_time = time.time()
        self.log_rate_limit = {}


class StructuredLogger:
    """
    Structured logging utility for consistent log formatting across the application.
    """
    
    def __init__(self, name: str):
        self.logger = logging.getLogger(name)
        self.default_fields = {
            "service": "browser-automation-api",
            "version": "2.0.0"
        }

    def log_event(self, level: str, event: str, message: str = None, **fields):
        """Log a structured event"""
        log_data = {
            **self.default_fields,
            "event": event,
            "timestamp": datetime.now().isoformat(),
            **fields
        }
        
        if message:
            log_data["message"] = message
        
        log_level = getattr(logging, level.upper(), logging.INFO)
        self.logger.log(log_level, event, extra={"log_data": log_data})

    def log_session_event(self, event: str, session_id: str, **fields):
        """Log a session-related event"""
        self.log_event("info", event, session_id=session_id, **fields)

    def log_agent_event(self, event: str, agent_id: str, session_id: str = None, **fields):
        """Log an agent-related event"""
        event_fields = {"agent_id": agent_id}
        if session_id:
            event_fields["session_id"] = session_id
        event_fields.update(fields)
        
        self.log_event("info", event, **event_fields)

    def log_browser_event(self, event: str, browser_id: str, **fields):
        """Log a browser-related event"""
        self.log_event("info", event, browser_id=browser_id, **fields)

    def log_websocket_event(self, event: str, connection_id: str, **fields):
        """Log a WebSocket-related event"""
        self.log_event("info", event, connection_id=connection_id, **fields)

    def log_error(self, error: Exception, context: str = None, **fields):
        """Log an error with context"""
        error_fields = {
            "error_type": type(error).__name__,
            "error_message": str(error),
            "traceback": traceback.format_exc()
        }
        
        if context:
            error_fields["context"] = context
        
        error_fields.update(fields)
        
        self.log_event("error", "application_error", **error_fields)

    def log_performance(self, operation: str, duration_ms: float, **fields):
        """Log performance metrics"""
        self.log_event(
            "info", 
            "performance_metric",
            operation=operation,
            duration_ms=round(duration_ms, 2),
            **fields
        )

    def log_security_event(self, event: str, severity: str = "info", **fields):
        """Log security-related events"""
        self.log_event(
            severity,
            f"security_{event}",
            security_event=True,
            **fields
        )


# Utility functions

def setup_application_logging(log_level: str = "INFO", log_format: str = None):
    """Setup application-wide logging configuration"""
    if log_format is None:
        log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format=log_format,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("./tmp/logs/app.log", mode='a')
        ]
    )
    
    # Set specific loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def get_structured_logger(name: str) -> StructuredLogger:
    """Get a structured logger instance"""
    return StructuredLogger(name)


class LoggingConfig:
    """Logging configuration management"""
    
    @staticmethod
    def get_config(environment: str = "development") -> Dict[str, Any]:
        """Get logging configuration for environment"""
        base_config = {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
                },
                "detailed": {
                    "format": "%(asctime)s - %(name)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s"
                },
                "json": {
                    "class": "pythonjsonlogger.jsonlogger.JsonFormatter",
                    "format": "%(asctime)s %(name)s %(levelname)s %(message)s"
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "level": "INFO",
                    "formatter": "standard",
                    "stream": "ext://sys.stdout"
                },
                "file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "level": "INFO",
                    "formatter": "detailed",
                    "filename": "./tmp/logs/app.log",
                    "maxBytes": 10485760,  # 10MB
                    "backupCount": 5
                }
            },
            "loggers": {
                "": {  # Root logger
                    "level": "INFO",
                    "handlers": ["console", "file"]
                },
                "uvicorn": {
                    "level": "INFO",
                    "handlers": ["console"],
                    "propagate": False
                },
                "fastapi": {
                    "level": "INFO",
                    "handlers": ["console", "file"],
                    "propagate": False
                }
            }
        }
        
        # Environment-specific overrides
        if environment == "development":
            base_config["handlers"]["console"]["level"] = "DEBUG"
            base_config["loggers"][""]["level"] = "DEBUG"
        elif environment == "production":
            base_config["handlers"]["console"]["formatter"] = "json"
            base_config["handlers"]["file"]["formatter"] = "json"
            # Add structured logging for production
            base_config["handlers"]["structured"] = {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "INFO",
                "formatter": "json",
                "filename": "./tmp/logs/structured.log",
                "maxBytes": 50485760,  # 50MB
                "backupCount": 10
            }
            base_config["loggers"][""]["handlers"].append("structured")
        
        return base_config


# Export main components
__all__ = [
    'LoggingMiddleware',
    'StructuredLogger', 
    'setup_application_logging',
    'get_structured_logger',
    'LoggingConfig'
]