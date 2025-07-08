import asyncio
import logging
import time
import psutil
import platform
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from collections import defaultdict, deque
import threading
import weakref
import json
import os

logger = logging.getLogger(__name__)


@dataclass
class MetricPoint:
    """A single metric data point"""
    timestamp: datetime
    value: float
    tags: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'timestamp': self.timestamp.isoformat(),
            'value': self.value,
            'tags': self.tags
        }


@dataclass
class MetricSeries:
    """A time series of metric points"""
    name: str
    description: str
    unit: str
    points: deque = field(default_factory=lambda: deque(maxlen=1000))
    
    def add_point(self, value: float, tags: Dict[str, str] = None):
        """Add a new metric point"""
        point = MetricPoint(
            timestamp=datetime.now(),
            value=value,
            tags=tags or {}
        )
        self.points.append(point)
    
    def get_latest(self) -> Optional[MetricPoint]:
        """Get the most recent metric point"""
        return self.points[-1] if self.points else None
    
    def get_average(self, duration: timedelta = None) -> float:
        """Get average value over a time period"""
        if not self.points:
            return 0.0
        
        if duration is None:
            values = [p.value for p in self.points]
        else:
            cutoff = datetime.now() - duration
            values = [p.value for p in self.points if p.timestamp >= cutoff]
        
        return sum(values) / len(values) if values else 0.0
    
    def get_max(self, duration: timedelta = None) -> float:
        """Get maximum value over a time period"""
        if not self.points:
            return 0.0
        
        if duration is None:
            values = [p.value for p in self.points]
        else:
            cutoff = datetime.now() - duration
            values = [p.value for p in self.points if p.timestamp >= cutoff]
        
        return max(values) if values else 0.0
    
    def get_points_since(self, since: datetime) -> List[MetricPoint]:
        """Get all points since a specific time"""
        return [p for p in self.points if p.timestamp >= since]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'description': self.description,
            'unit': self.unit,
            'points': [p.to_dict() for p in self.points],
            'latest_value': self.get_latest().value if self.get_latest() else None,
            'average_1h': self.get_average(timedelta(hours=1)),
            'max_1h': self.get_max(timedelta(hours=1))
        }


class MetricsCollector:
    """
    Centralized metrics collection and monitoring system.
    Collects system, application, and custom metrics.
    """
    
    def __init__(self, retention_hours: int = 24):
        self.retention_hours = retention_hours
        self.metrics: Dict[str, MetricSeries] = {}
        
        # Collection intervals
        self.system_metrics_interval = 10  # seconds
        self.app_metrics_interval = 30  # seconds
        
        # Background tasks
        self._system_collection_task: Optional[asyncio.Task] = None
        self._app_collection_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._is_running = False
        
        # Callbacks for metric events
        self._metric_callbacks: Dict[str, List[Callable]] = defaultdict(list)
        self._threshold_alerts: Dict[str, Dict[str, float]] = {}
        
        # System information
        self._system_info = self._get_system_info()
        
        # Initialize standard metrics
        self._init_standard_metrics()
        
        logger.info("MetricsCollector initialized")

    def _get_system_info(self) -> Dict[str, Any]:
        """Get static system information"""
        return {
            'platform': platform.platform(),
            'python_version': platform.python_version(),
            'cpu_count': psutil.cpu_count(),
            'cpu_count_logical': psutil.cpu_count(logical=True),
            'memory_total_gb': round(psutil.virtual_memory().total / (1024**3), 2),
            'boot_time': datetime.fromtimestamp(psutil.boot_time()).isoformat()
        }

    def _init_standard_metrics(self):
        """Initialize standard system and application metrics"""
        # System metrics
        self.register_metric('system.cpu.percent', 'CPU usage percentage', '%')
        self.register_metric('system.memory.percent', 'Memory usage percentage', '%')
        self.register_metric('system.memory.available_gb', 'Available memory', 'GB')
        self.register_metric('system.disk.percent', 'Disk usage percentage', '%')
        self.register_metric('system.disk.free_gb', 'Free disk space', 'GB')
        self.register_metric('system.load.1min', '1-minute load average', 'load')
        self.register_metric('system.network.bytes_sent', 'Network bytes sent', 'bytes')
        self.register_metric('system.network.bytes_recv', 'Network bytes received', 'bytes')
        
        # Process metrics
        self.register_metric('process.memory.rss_mb', 'Process memory RSS', 'MB')
        self.register_metric('process.memory.vms_mb', 'Process memory VMS', 'MB')
        self.register_metric('process.cpu.percent', 'Process CPU usage', '%')
        self.register_metric('process.threads', 'Number of threads', 'count')
        self.register_metric('process.open_files', 'Open file descriptors', 'count')
        self.register_metric('process.connections', 'Network connections', 'count')
        
        # Application metrics
        self.register_metric('app.sessions.active', 'Active sessions', 'count')
        self.register_metric('app.sessions.total', 'Total sessions created', 'count')
        self.register_metric('app.browsers.active', 'Active browsers', 'count')
        self.register_metric('app.contexts.active', 'Active browser contexts', 'count')
        self.register_metric('app.websockets.active', 'Active WebSocket connections', 'count')
        self.register_metric('app.agents.running', 'Running agents', 'count')
        self.register_metric('app.memory.pool_mb', 'Browser pool memory usage', 'MB')
        self.register_metric('app.requests.per_minute', 'Requests per minute', 'req/min')
        self.register_metric('app.errors.per_minute', 'Errors per minute', 'err/min')
        
        # Performance metrics
        self.register_metric('perf.response_time.avg', 'Average response time', 'ms')
        self.register_metric('perf.response_time.p95', '95th percentile response time', 'ms')
        self.register_metric('perf.gc.collections', 'Garbage collections', 'count')
        self.register_metric('perf.gc.time_ms', 'GC time', 'ms')

    async def start(self):
        """Start metrics collection"""
        if self._is_running:
            logger.warning("MetricsCollector is already running")
            return
        
        self._is_running = True
        
        # Start collection tasks
        self._system_collection_task = asyncio.create_task(self._collect_system_metrics_loop())
        self._app_collection_task = asyncio.create_task(self._collect_app_metrics_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_old_metrics_loop())
        
        logger.info("MetricsCollector started")

    async def stop(self):
        """Stop metrics collection"""
        if not self._is_running:
            return
        
        self._is_running = False
        
        # Cancel tasks
        for task in [self._system_collection_task, self._app_collection_task, self._cleanup_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        logger.info("MetricsCollector stopped")

    def register_metric(self, name: str, description: str, unit: str) -> MetricSeries:
        """Register a new metric"""
        if name in self.metrics:
            logger.warning(f"Metric {name} already exists")
            return self.metrics[name]
        
        metric = MetricSeries(name=name, description=description, unit=unit)
        self.metrics[name] = metric
        logger.debug(f"Registered metric: {name}")
        return metric

    def record_metric(self, name: str, value: float, tags: Dict[str, str] = None):
        """Record a metric value"""
        if name not in self.metrics:
            logger.warning(f"Metric {name} not registered")
            return
        
        self.metrics[name].add_point(value, tags)
        
        # Check thresholds and trigger callbacks
        self._check_thresholds(name, value)
        self._trigger_callbacks(name, value, tags)

    def set_threshold_alert(self, metric_name: str, warning: float = None, critical: float = None):
        """Set threshold alerts for a metric"""
        if metric_name not in self.metrics:
            logger.warning(f"Metric {metric_name} not registered")
            return
        
        self._threshold_alerts[metric_name] = {}
        if warning is not None:
            self._threshold_alerts[metric_name]['warning'] = warning
        if critical is not None:
            self._threshold_alerts[metric_name]['critical'] = critical

    def add_metric_callback(self, metric_name: str, callback: Callable):
        """Add a callback for metric updates"""
        self._metric_callbacks[metric_name].append(callback)

    def get_metric(self, name: str) -> Optional[MetricSeries]:
        """Get a metric by name"""
        return self.metrics.get(name)

    def get_all_metrics(self) -> Dict[str, MetricSeries]:
        """Get all metrics"""
        return self.metrics.copy()

    def get_metrics_summary(self) -> Dict[str, Any]:
        """Get a summary of all metrics"""
        summary = {
            'timestamp': datetime.now().isoformat(),
            'system_info': self._system_info,
            'metrics': {},
            'alerts': self._get_active_alerts()
        }
        
        for name, metric in self.metrics.items():
            latest = metric.get_latest()
            if latest:
                summary['metrics'][name] = {
                    'latest_value': latest.value,
                    'unit': metric.unit,
                    'avg_1h': metric.get_average(timedelta(hours=1)),
                    'max_1h': metric.get_max(timedelta(hours=1)),
                    'timestamp': latest.timestamp.isoformat()
                }
        
        return summary

    def get_metrics_for_period(self, since: datetime) -> Dict[str, List[Dict[str, Any]]]:
        """Get metrics data for a specific time period"""
        result = {}
        for name, metric in self.metrics.items():
            points = metric.get_points_since(since)
            result[name] = [p.to_dict() for p in points]
        return result

    def export_metrics(self, format: str = 'json') -> str:
        """Export metrics in specified format"""
        if format == 'json':
            return json.dumps(self.get_metrics_summary(), indent=2)
        elif format == 'prometheus':
            return self._export_prometheus_format()
        else:
            raise ValueError(f"Unsupported export format: {format}")

    def _export_prometheus_format(self) -> str:
        """Export metrics in Prometheus format"""
        lines = []
        for name, metric in self.metrics.items():
            latest = metric.get_latest()
            if latest:
                # Convert metric name to Prometheus format
                prom_name = name.replace('.', '_').replace('-', '_')
                lines.append(f'# HELP {prom_name} {metric.description}')
                lines.append(f'# TYPE {prom_name} gauge')
                
                # Add tags if present
                tags_str = ''
                if latest.tags:
                    tag_pairs = [f'{k}="{v}"' for k, v in latest.tags.items()]
                    tags_str = '{' + ','.join(tag_pairs) + '}'
                
                lines.append(f'{prom_name}{tags_str} {latest.value}')
                lines.append('')
        
        return '\n'.join(lines)

    async def _collect_system_metrics_loop(self):
        """Background task to collect system metrics"""
        logger.info("Starting system metrics collection loop")
        
        while self._is_running:
            try:
                await self._collect_system_metrics()
                await asyncio.sleep(self.system_metrics_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error collecting system metrics: {e}", exc_info=True)
                await asyncio.sleep(self.system_metrics_interval)

    async def _collect_app_metrics_loop(self):
        """Background task to collect application metrics"""
        logger.info("Starting application metrics collection loop")
        
        while self._is_running:
            try:
                await self._collect_app_metrics()
                await asyncio.sleep(self.app_metrics_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error collecting application metrics: {e}", exc_info=True)
                await asyncio.sleep(self.app_metrics_interval)

    async def _collect_system_metrics(self):
        """Collect system-level metrics"""
        try:
            # CPU metrics
            cpu_percent = psutil.cpu_percent(interval=1)
            self.record_metric('system.cpu.percent', cpu_percent)
            
            # Memory metrics
            memory = psutil.virtual_memory()
            self.record_metric('system.memory.percent', memory.percent)
            self.record_metric('system.memory.available_gb', memory.available / (1024**3))
            
            # Disk metrics
            disk = psutil.disk_usage('/')
            self.record_metric('system.disk.percent', (disk.used / disk.total) * 100)
            self.record_metric('system.disk.free_gb', disk.free / (1024**3))
            
            # Load average (Unix only)
            if hasattr(psutil, 'getloadavg'):
                load = psutil.getloadavg()
                self.record_metric('system.load.1min', load[0])
            
            # Network metrics
            net = psutil.net_io_counters()
            self.record_metric('system.network.bytes_sent', net.bytes_sent)
            self.record_metric('system.network.bytes_recv', net.bytes_recv)
            
            # Process metrics
            process = psutil.Process()
            process_memory = process.memory_info()
            self.record_metric('process.memory.rss_mb', process_memory.rss / (1024**2))
            self.record_metric('process.memory.vms_mb', process_memory.vms / (1024**2))
            self.record_metric('process.cpu.percent', process.cpu_percent())
            self.record_metric('process.threads', process.num_threads())
            
            try:
                self.record_metric('process.open_files', len(process.open_files()))
                self.record_metric('process.connections', len(process.connections()))
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass  # Some systems restrict access to this info
                
        except Exception as e:
            logger.error(f"Error collecting system metrics: {e}")

    async def _collect_app_metrics(self):
        """Collect application-specific metrics"""
        try:
            # Import here to avoid circular imports
            from src.core.session_manager import get_session_manager
            from src.core.resource_pool import get_resource_pool
            from src.core.websocket_manager import get_websocket_manager
            
            # Session metrics
            try:
                session_manager = get_session_manager()
                session_stats = await session_manager.get_statistics()
                self.record_metric('app.sessions.active', session_stats['current_active_sessions'])
                self.record_metric('app.sessions.total', session_stats['total_sessions_created'])
            except Exception as e:
                logger.debug(f"Could not collect session metrics: {e}")
            
            # Resource pool metrics
            try:
                resource_pool = get_resource_pool()
                resource_stats = await resource_pool.get_resource_stats()
                self.record_metric('app.browsers.active', resource_stats['current_browsers'])
                self.record_metric('app.contexts.active', resource_stats['current_contexts'])
                self.record_metric('app.memory.pool_mb', resource_stats['current_memory_usage_mb'])
            except Exception as e:
                logger.debug(f"Could not collect resource pool metrics: {e}")
            
            # WebSocket metrics
            try:
                websocket_manager = get_websocket_manager()
                ws_stats = await websocket_manager.get_statistics()
                self.record_metric('app.websockets.active', ws_stats['current_connections'])
            except Exception as e:
                logger.debug(f"Could not collect WebSocket metrics: {e}")
            
            # Garbage collection metrics
            import gc
            gc_stats = gc.get_stats()
            if gc_stats:
                total_collections = sum(stat['collections'] for stat in gc_stats)
                self.record_metric('perf.gc.collections', total_collections)
                
        except Exception as e:
            logger.error(f"Error collecting application metrics: {e}")

    async def _cleanup_old_metrics_loop(self):
        """Background task to cleanup old metric data"""
        logger.info("Starting metrics cleanup loop")
        
        while self._is_running:
            try:
                await self._cleanup_old_metrics()
                await asyncio.sleep(3600)  # Cleanup every hour
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error during metrics cleanup: {e}", exc_info=True)
                await asyncio.sleep(3600)

    async def _cleanup_old_metrics(self):
        """Remove old metric data points"""
        cutoff_time = datetime.now() - timedelta(hours=self.retention_hours)
        cleaned_count = 0
        
        for name, metric in self.metrics.items():
            # Create new deque with only recent points
            recent_points = deque(
                (p for p in metric.points if p.timestamp >= cutoff_time),
                maxlen=metric.points.maxlen
            )
            
            removed_count = len(metric.points) - len(recent_points)
            if removed_count > 0:
                metric.points = recent_points
                cleaned_count += removed_count
        
        if cleaned_count > 0:
            logger.debug(f"Cleaned up {cleaned_count} old metric points")

    def _check_thresholds(self, metric_name: str, value: float):
        """Check if metric value exceeds thresholds"""
        if metric_name not in self._threshold_alerts:
            return
        
        thresholds = self._threshold_alerts[metric_name]
        
        if 'critical' in thresholds and value >= thresholds['critical']:
            self._trigger_alert(metric_name, 'critical', value, thresholds['critical'])
        elif 'warning' in thresholds and value >= thresholds['warning']:
            self._trigger_alert(metric_name, 'warning', value, thresholds['warning'])

    def _trigger_alert(self, metric_name: str, level: str, value: float, threshold: float):
        """Trigger an alert for threshold breach"""
        alert = {
            'metric': metric_name,
            'level': level,
            'value': value,
            'threshold': threshold,
            'timestamp': datetime.now().isoformat(),
            'message': f"Metric {metric_name} is {value} (threshold: {threshold})"
        }
        
        logger.warning(f"ALERT [{level.upper()}]: {alert['message']}")
        
        # Store alert for retrieval
        if not hasattr(self, '_recent_alerts'):
            self._recent_alerts = deque(maxlen=100)
        self._recent_alerts.append(alert)

    def _trigger_callbacks(self, metric_name: str, value: float, tags: Dict[str, str] = None):
        """Trigger registered callbacks for metric updates"""
        if metric_name in self._metric_callbacks:
            for callback in self._metric_callbacks[metric_name]:
                try:
                    callback(metric_name, value, tags)
                except Exception as e:
                    logger.error(f"Error in metric callback: {e}")

    def _get_active_alerts(self) -> List[Dict[str, Any]]:
        """Get recent alerts"""
        if not hasattr(self, '_recent_alerts'):
            return []
        
        # Return alerts from last hour
        one_hour_ago = datetime.now() - timedelta(hours=1)
        return [
            alert for alert in self._recent_alerts
            if datetime.fromisoformat(alert['timestamp']) >= one_hour_ago
        ]

    def get_health_score(self) -> float:
        """Calculate overall system health score (0.0 to 1.0)"""
        health_factors = []
        
        # CPU health (inverse of usage)
        cpu_metric = self.get_metric('system.cpu.percent')
        if cpu_metric:
            cpu_latest = cpu_metric.get_latest()
            if cpu_latest:
                cpu_health = max(0, 1 - (cpu_latest.value / 100))
                health_factors.append(cpu_health * 0.3)  # 30% weight
        
        # Memory health
        memory_metric = self.get_metric('system.memory.percent')
        if memory_metric:
            memory_latest = memory_metric.get_latest()
            if memory_latest:
                memory_health = max(0, 1 - (memory_latest.value / 100))
                health_factors.append(memory_health * 0.3)  # 30% weight
        
        # Disk health
        disk_metric = self.get_metric('system.disk.percent')
        if disk_metric:
            disk_latest = disk_metric.get_latest()
            if disk_latest:
                disk_health = max(0, 1 - (disk_latest.value / 100))
                health_factors.append(disk_health * 0.2)  # 20% weight
        
        # Application responsiveness (based on error rate)
        error_metric = self.get_metric('app.errors.per_minute')
        if error_metric:
            error_latest = error_metric.get_latest()
            if error_latest:
                # Assume 10+ errors per minute is very bad
                error_health = max(0, 1 - (error_latest.value / 10))
                health_factors.append(error_health * 0.2)  # 20% weight
        
        if not health_factors:
            return 1.0  # No metrics available, assume healthy
        
        return sum(health_factors) / len(health_factors)

    def get_performance_summary(self) -> Dict[str, Any]:
        """Get performance summary with key metrics"""
        summary = {
            'timestamp': datetime.now().isoformat(),
            'health_score': self.get_health_score(),
            'system': {},
            'application': {},
            'performance': {}
        }
        
        # System metrics
        for metric_name in ['system.cpu.percent', 'system.memory.percent', 'system.disk.percent']:
            metric = self.get_metric(metric_name)
            if metric:
                latest = metric.get_latest()
                if latest:
                    key = metric_name.split('.')[-1]
                    summary['system'][key] = {
                        'current': latest.value,
                        'avg_1h': metric.get_average(timedelta(hours=1)),
                        'max_1h': metric.get_max(timedelta(hours=1))
                    }
        
        # Application metrics
        for metric_name in ['app.sessions.active', 'app.browsers.active', 'app.websockets.active']:
            metric = self.get_metric(metric_name)
            if metric:
                latest = metric.get_latest()
                if latest:
                    key = metric_name.split('.')[-1]
                    summary['application'][key] = latest.value
        
        # Performance metrics
        for metric_name in ['perf.response_time.avg', 'process.memory.rss_mb', 'process.cpu.percent']:
            metric = self.get_metric(metric_name)
            if metric:
                latest = metric.get_latest()
                if latest:
                    key = metric_name.split('.')[-1]
                    summary['performance'][key] = latest.value
        
        return summary


# Global metrics collector instance
_metrics_collector: Optional[MetricsCollector] = None


def get_metrics_collector() -> MetricsCollector:
    """Get the global metrics collector instance"""
    global _metrics_collector
    if _metrics_collector is None:
        raise RuntimeError("MetricsCollector not initialized. Call init_metrics_collector() first.")
    return _metrics_collector


async def init_metrics_collector(retention_hours: int = 24) -> MetricsCollector:
    """Initialize the global metrics collector"""
    global _metrics_collector
    if _metrics_collector is not None:
        logger.warning("MetricsCollector already initialized")
        return _metrics_collector
    
    _metrics_collector = MetricsCollector(retention_hours=retention_hours)
    await _metrics_collector.start()
    
    # Set up default threshold alerts
    _metrics_collector.set_threshold_alert('system.cpu.percent', warning=80.0, critical=95.0)
    _metrics_collector.set_threshold_alert('system.memory.percent', warning=85.0, critical=95.0)
    _metrics_collector.set_threshold_alert('system.disk.percent', warning=90.0, critical=98.0)
    _metrics_collector.set_threshold_alert('app.errors.per_minute', warning=5.0, critical=10.0)
    
    return _metrics_collector


async def shutdown_metrics_collector():
    """Shutdown the global metrics collector"""
    global _metrics_collector
    if _metrics_collector is not None:
        await _metrics_collector.stop()
        _metrics_collector = None


# Utility functions for easy metric recording
def record_metric(name: str, value: float, tags: Dict[str, str] = None):
    """Record a metric value"""
    try:
        collector = get_metrics_collector()
        collector.record_metric(name, value, tags)
    except RuntimeError:
        # Metrics collector not initialized - silently ignore
        pass


def record_request_time(endpoint: str, duration_ms: float, status_code: int = 200):
    """Record request timing metric"""
    tags = {
        'endpoint': endpoint,
        'status_code': str(status_code)
    }
    record_metric('perf.request_duration_ms', duration_ms, tags)


def record_error(error_type: str, endpoint: str = None):
    """Record an error occurrence"""
    tags = {'error_type': error_type}
    if endpoint:
        tags['endpoint'] = endpoint
    record_metric('app.errors.count', 1, tags)


def record_agent_action(action_type: str, duration_ms: float = None):
    """Record agent action metrics"""
    tags = {'action_type': action_type}
    record_metric('app.agent.actions_count', 1, tags)
    
    if duration_ms is not None:
        record_metric('app.agent.action_duration_ms', duration_ms, tags)


class MetricsMiddleware:
    """Middleware for automatic request metrics collection"""
    
    def __init__(self, app):
        self.app = app
    
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        start_time = time.time()
        
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                duration_ms = (time.time() - start_time) * 1000
                status_code = message["status"]
                endpoint = scope["path"]
                
                record_request_time(endpoint, duration_ms, status_code)
                
                if status_code >= 400:
                    record_error("http_error", endpoint)
            
            await send(message)
        
        await self.app(scope, receive, send_wrapper)


# Export main components
__all__ = [
    'MetricPoint',
    'MetricSeries', 
    'MetricsCollector',
    'get_metrics_collector',
    'init_metrics_collector',
    'shutdown_metrics_collector',
    'record_metric',
    'record_request_time',
    'record_error',
    'record_agent_action',
    'MetricsMiddleware'
]