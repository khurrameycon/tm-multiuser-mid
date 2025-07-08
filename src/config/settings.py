import os
import logging
from typing import Dict, List, Optional, Any, Union
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
import json
import yaml
from pydantic import BaseSettings, Field, validator

logger = logging.getLogger(__name__)


class Environment(str, Enum):
    """Application environment types"""
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TESTING = "testing"


class LogLevel(str, Enum):
    """Logging levels"""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass
class DatabaseConfig:
    """Database configuration"""
    enabled: bool = False
    url: Optional[str] = None
    max_connections: int = 10
    timeout_seconds: int = 30
    pool_size: int = 5
    echo_queries: bool = False


@dataclass
class RedisConfig:
    """Redis configuration for session storage and caching"""
    enabled: bool = False
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: Optional[str] = None
    max_connections: int = 20
    socket_timeout: int = 30
    socket_connect_timeout: int = 30
    health_check_interval: int = 30


@dataclass
class SecurityConfig:
    """Security configuration"""
    enable_cors: bool = True
    cors_origins: List[str] = field(default_factory=lambda: ["*"])
    cors_methods: List[str] = field(default_factory=lambda: ["*"])
    cors_headers: List[str] = field(default_factory=lambda: ["*"])
    enable_rate_limiting: bool = True
    api_rate_limit_per_minute: int = 100
    api_rate_limit_burst: int = 20
    websocket_rate_limit: int = 60
    enable_auth: bool = False
    jwt_secret: Optional[str] = None
    jwt_expiration_hours: int = 24
    admin_ips: List[str] = field(default_factory=lambda: ["127.0.0.1", "::1"])
    max_request_size_mb: int = 50


@dataclass
class MonitoringConfig:
    """Monitoring and observability configuration"""
    enable_metrics: bool = True
    enable_health_checks: bool = True
    enable_performance_monitoring: bool = True
    metrics_port: int = 9090
    metrics_endpoint: str = "/metrics"
    health_endpoint: str = "/health"
    enable_tracing: bool = False
    tracing_sample_rate: float = 0.1
    log_level: LogLevel = LogLevel.INFO
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    enable_file_logging: bool = True
    log_file_path: str = "./tmp/logs/app.log"
    log_rotation_mb: int = 10
    log_retention_days: int = 30


@dataclass
class SessionManagerConfig:
    """Session management configuration"""
    max_sessions: int = 1000
    session_timeout_minutes: int = 30
    cleanup_interval_seconds: int = 60
    max_sessions_per_ip: int = 5
    enable_session_persistence: bool = False
    session_storage_backend: str = "memory"  # memory, redis, database


@dataclass
class ResourcePoolConfig:
    """Resource pool configuration"""
    max_browser_instances: int = 50
    min_browser_instances: int = 5
    browser_idle_timeout_minutes: int = 10
    max_memory_usage_mb: int = 8000
    enable_resource_pooling: bool = True
    enable_memory_monitoring: bool = True
    enable_automatic_scaling: bool = True
    cleanup_interval_seconds: int = 30


@dataclass
class WebSocketConfig:
    """WebSocket configuration"""
    max_connections: int = 1000
    max_connections_per_ip: int = 5
    ping_interval_seconds: int = 30
    rate_limit_messages_per_minute: int = 100
    enable_compression: bool = True
    max_message_size_kb: int = 1024


@dataclass
class AgentManagerConfig:
    """Agent manager configuration"""
    max_concurrent_agents: int = 100
    max_agents_per_session: int = 3
    agent_idle_timeout_minutes: int = 15
    task_timeout_minutes: int = 60
    queue_size_limit: int = 1000
    enable_task_retry: bool = True
    enable_agent_pooling: bool = True


@dataclass
class BrowserConfig:
    """Default browser configuration"""
    default_headless: bool = True
    default_disable_security: bool = True
    default_window_width: int = 1280
    default_window_height: int = 1100
    enable_browser_pooling: bool = True
    max_contexts_per_browser: int = 10


@dataclass
class PerformanceConfig:
    """Performance tuning configuration"""
    enable_async_optimization: bool = True
    max_worker_threads: int = 4
    task_timeout_seconds: int = 300
    gc_threshold: int = 1000
    enable_memory_optimization: bool = True
    enable_connection_pooling: bool = True
    max_concurrent_requests: int = 200


@dataclass
class StorageConfig:
    """File storage configuration"""
    base_storage_path: str = "./tmp"
    downloads_path: str = "./tmp/downloads"
    recordings_path: str = "./tmp/recordings"
    traces_path: str = "./tmp/traces"
    logs_path: str = "./tmp/logs"
    sessions_path: str = "./tmp/sessions"
    max_storage_size_gb: int = 10
    cleanup_old_files_days: int = 7
    enable_file_compression: bool = True


class Settings(BaseSettings):
    """
    Centralized application settings with environment variable support.
    
    Settings are loaded from:
    1. Environment variables
    2. .env files
    3. Configuration files (JSON/YAML)
    4. Default values
    """
    
    # Application settings
    app_name: str = Field("Multi-User Browser Automation", env="APP_NAME")
    app_version: str = Field("2.0.0", env="APP_VERSION")
    environment: Environment = Field(Environment.DEVELOPMENT, env="ENVIRONMENT")
    debug: bool = Field(False, env="DEBUG")
    
    # Server settings
    host: str = Field("0.0.0.0", env="HOST")
    port: int = Field(7788, env="PORT")
    workers: int = Field(1, env="WORKERS")
    
    # API settings
    enable_api_docs: bool = Field(True, env="ENABLE_API_DOCS")
    api_prefix: str = Field("/api/v1", env="API_PREFIX")
    
    # Component configurations
    session_manager: SessionManagerConfig = Field(default_factory=SessionManagerConfig)
    resource_pool: ResourcePoolConfig = Field(default_factory=ResourcePoolConfig)
    websocket: WebSocketConfig = Field(default_factory=WebSocketConfig)
    agent_manager: AgentManagerConfig = Field(default_factory=AgentManagerConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    
    # Feature flags
    enable_deep_research: bool = Field(True, env="ENABLE_DEEP_RESEARCH")
    enable_browser_streaming: bool = Field(True, env="ENABLE_BROWSER_STREAMING")
    enable_session_recording: bool = Field(False, env="ENABLE_SESSION_RECORDING")
    enable_mcp_integration: bool = Field(True, env="ENABLE_MCP_INTEGRATION")
    
    # External service configurations
    llm_providers: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        
    @validator("environment", pre=True)
    def validate_environment(cls, v):
        if isinstance(v, str):
            return Environment(v.lower())
        return v
    
    @validator("workers")
    def validate_workers(cls, v, values):
        if v < 1:
            return 1
        # Limit workers in production
        if values.get("environment") == Environment.PRODUCTION and v > 4:
            logger.warning("Limiting workers to 4 in production")
            return 4
        return v
    
    @validator("port")
    def validate_port(cls, v):
        if not 1024 <= v <= 65535:
            raise ValueError("Port must be between 1024 and 65535")
        return v
    
    def __post_init_post_parse__(self):
        """Post-initialization configuration"""
        # Ensure storage directories exist
        self._ensure_directories()
        
        # Apply environment-specific overrides
        self._apply_environment_overrides()
        
        # Validate configuration consistency
        self._validate_configuration()
    
    def _ensure_directories(self):
        """Ensure all required directories exist"""
        directories = [
            self.storage.base_storage_path,
            self.storage.downloads_path,
            self.storage.recordings_path,
            self.storage.traces_path,
            self.storage.logs_path,
            self.storage.sessions_path,
        ]
        
        for directory in directories:
            Path(directory).mkdir(parents=True, exist_ok=True)
    
    def _apply_environment_overrides(self):
        """Apply environment-specific configuration overrides"""
        if self.environment == Environment.PRODUCTION:
            # Production optimizations
            self.debug = False
            self.monitoring.enable_metrics = True
            self.monitoring.enable_health_checks = True
            self.security.enable_rate_limiting = True
            self.performance.enable_async_optimization = True
            self.browser.default_headless = True
            self.monitoring.log_level = LogLevel.INFO
            
        elif self.environment == Environment.DEVELOPMENT:
            # Development optimizations
            self.debug = True
            self.monitoring.log_level = LogLevel.DEBUG
            self.browser.default_headless = False
            self.security.enable_auth = False
            
        elif self.environment == Environment.TESTING:
            # Testing optimizations
            self.debug = True
            self.monitoring.log_level = LogLevel.WARNING
            self.session_manager.max_sessions = 10
            self.resource_pool.max_browser_instances = 5
    
    def _validate_configuration(self):
        """Validate configuration consistency"""
        # Check memory limits
        if self.resource_pool.max_browser_instances * 200 > self.resource_pool.max_memory_usage_mb:
            logger.warning("Browser instance limit may exceed memory limit")
        
        # Check session limits
        if self.session_manager.max_sessions > self.websocket.max_connections:
            logger.warning("Session limit exceeds WebSocket connection limit")
        
        # Check Redis configuration
        if self.session_manager.session_storage_backend == "redis" and not self.redis.enabled:
            logger.error("Redis backend selected but Redis is not enabled")
            self.redis.enabled = True
    
    def get_component_config(self, component: str) -> Any:
        """Get configuration for a specific component"""
        return getattr(self, component, None)
    
    def update_config(self, updates: Dict[str, Any]):
        """Update configuration values"""
        for key, value in updates.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                logger.warning(f"Unknown configuration key: {key}")
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert settings to dictionary"""
        return self.dict()
    
    def to_json(self) -> str:
        """Convert settings to JSON"""
        return self.json(indent=2)
    
    @classmethod
    def from_file(cls, file_path: str) -> 'Settings':
        """Load settings from a file (JSON or YAML)"""
        file_path = Path(file_path)
        
        if not file_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {file_path}")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            if file_path.suffix.lower() in ['.yml', '.yaml']:
                import yaml
                data = yaml.safe_load(f)
            else:
                data = json.load(f)
        
        return cls(**data)
    
    def save_to_file(self, file_path: str):
        """Save settings to a file"""
        file_path = Path(file_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            if file_path.suffix.lower() in ['.yml', '.yaml']:
                import yaml
                yaml.dump(self.dict(), f, default_flow_style=False, indent=2)
            else:
                f.write(self.json(indent=2))
    
    def get_database_url(self) -> Optional[str]:
        """Get database connection URL"""
        if not self.database.enabled or not self.database.url:
            return None
        return self.database.url
    
    def get_redis_url(self) -> Optional[str]:
        """Get Redis connection URL"""
        if not self.redis.enabled:
            return None
        
        auth_part = f":{self.redis.password}@" if self.redis.password else ""
        return f"redis://{auth_part}{self.redis.host}:{self.redis.port}/{self.redis.db}"
    
    def get_cors_settings(self) -> Dict[str, Any]:
        """Get CORS settings"""
        return {
            "allow_origins": self.security.cors_origins,
            "allow_methods": self.security.cors_methods,
            "allow_headers": self.security.cors_headers,
            "allow_credentials": True,
        }
    
    def is_production(self) -> bool:
        """Check if running in production"""
        return self.environment == Environment.PRODUCTION
    
    def is_development(self) -> bool:
        """Check if running in development"""
        return self.environment == Environment.DEVELOPMENT


# Global settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get the global settings instance"""
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.__post_init_post_parse__()
    return _settings


def init_settings(config_file: Optional[str] = None, **overrides) -> Settings:
    """Initialize settings with optional config file and overrides"""
    global _settings
    
    if config_file:
        _settings = Settings.from_file(config_file)
    else:
        _settings = Settings()
    
    # Apply any overrides
    if overrides:
        _settings.update_config(overrides)
    
    _settings.__post_init_post_parse__()
    
    logger.info(f"Settings initialized for {_settings.environment} environment")
    return _settings


def reload_settings():
    """Reload settings from environment and files"""
    global _settings
    _settings = None
    return get_settings()


# Environment-specific configuration presets
ENVIRONMENT_PRESETS = {
    Environment.DEVELOPMENT: {
        "debug": True,
        "session_manager": {
            "max_sessions": 50,
            "session_timeout_minutes": 60,
        },
        "resource_pool": {
            "max_browser_instances": 10,
            "min_browser_instances": 2,
        },
        "browser": {
            "default_headless": False,
        },
        "monitoring": {
            "log_level": LogLevel.DEBUG,
            "enable_performance_monitoring": False,
        }
    },
    
    Environment.PRODUCTION: {
        "debug": False,
        "session_manager": {
            "max_sessions": 1000,
            "session_timeout_minutes": 30,
        },
        "resource_pool": {
            "max_browser_instances": 50,
            "min_browser_instances": 5,
            "max_memory_usage_mb": 8000,
        },
        "browser": {
            "default_headless": True,
        },
        "security": {
            "enable_rate_limiting": True,
            "enable_auth": True,
        },
        "monitoring": {
            "log_level": LogLevel.INFO,
            "enable_metrics": True,
            "enable_health_checks": True,
            "enable_performance_monitoring": True,
        },
        "performance": {
            "enable_async_optimization": True,
            "enable_memory_optimization": True,
        }
    },
    
    Environment.TESTING: {
        "debug": True,
        "session_manager": {
            "max_sessions": 10,
            "session_timeout_minutes": 5,
        },
        "resource_pool": {
            "max_browser_instances": 3,
            "min_browser_instances": 1,
        },
        "monitoring": {
            "log_level": LogLevel.WARNING,
            "enable_metrics": False,
        }
    }
}


def apply_environment_preset(environment: Environment) -> Settings:
    """Apply an environment-specific configuration preset"""
    preset = ENVIRONMENT_PRESETS.get(environment, {})
    return init_settings(**preset)