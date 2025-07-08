import asyncio
import logging
import time
import psutil
import weakref
from typing import Dict, List, Optional, Set, Any
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class ResourceType(Enum):
    """Types of resources managed by the pool"""
    BROWSER = "browser"
    CONTEXT = "context"
    CONTROLLER = "controller"


class ResourceState(Enum):
    """Resource lifecycle states"""
    CREATING = "creating"
    AVAILABLE = "available"
    ALLOCATED = "allocated"
    CLEANUP = "cleanup"
    FAILED = "failed"


@dataclass
class ResourceConfig:
    """Configuration for resource pool management"""
    # Browser pool settings
    max_browser_instances: int = 50
    min_browser_instances: int = 5
    browser_idle_timeout_minutes: int = 10
    browser_max_age_hours: int = 4
    
    # Context pool settings
    max_contexts_per_browser: int = 10
    context_idle_timeout_minutes: int = 5
    
    # Resource limits
    max_memory_usage_mb: int = 8000  # 8GB total
    max_memory_per_browser_mb: int = 200  # 200MB per browser
    
    # Cleanup and monitoring
    cleanup_interval_seconds: int = 30
    health_check_interval_seconds: int = 60
    
    # Performance settings
    enable_resource_pooling: bool = True
    enable_memory_monitoring: bool = True
    enable_automatic_scaling: bool = True


@dataclass
class ResourceInfo:
    """Information about a managed resource"""
    resource_id: str
    resource_type: ResourceType
    state: ResourceState
    created_at: datetime
    last_used: datetime
    allocated_to: Optional[str] = None  # session_id
    memory_usage_mb: float = 0.0
    allocation_count: int = 0
    error_count: int = 0
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class ResourcePool:
    """
    Manages browser instances and other resources for multi-user sessions.
    Provides resource pooling, allocation, and cleanup capabilities.
    """
    
    def __init__(self, config: ResourceConfig = None):
        self.config = config or ResourceConfig()
        
        # Resource storage
        self._browsers: Dict[str, Any] = {}  # browser_id -> browser instance
        self._contexts: Dict[str, Any] = {}  # context_id -> context instance
        self._controllers: Dict[str, Any] = {}  # controller_id -> controller instance
        
        # Resource metadata
        self._resource_info: Dict[str, ResourceInfo] = {}
        self._allocations: Dict[str, Set[str]] = {}  # session_id -> set of resource_ids
        self._browser_contexts: Dict[str, Set[str]] = {}  # browser_id -> set of context_ids
        
        # Background tasks
        self._cleanup_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._scaling_task: Optional[asyncio.Task] = None
        self._is_running = False
        
        # Statistics and monitoring
        self._stats = {
            'total_browsers_created': 0,
            'total_browsers_destroyed': 0,
            'total_contexts_created': 0,
            'total_contexts_destroyed': 0,
            'total_allocations': 0,
            'total_deallocations': 0,
            'current_memory_usage_mb': 0.0,
            'peak_memory_usage_mb': 0.0
        }
        
        # Weak references for automatic cleanup
        self._weak_refs: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
        
        logger.info(f"ResourcePool initialized with config: {self.config}")

    async def start(self):
        """Start the resource pool and background tasks"""
        if self._is_running:
            logger.warning("ResourcePool is already running")
            return
        
        self._is_running = True
        
        # Start background tasks
        if self.config.enable_resource_pooling:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            
        if self.config.enable_memory_monitoring:
            self._health_check_task = asyncio.create_task(self._health_check_loop())
            
        if self.config.enable_automatic_scaling:
            self._scaling_task = asyncio.create_task(self._scaling_loop())
        
        # Pre-warm the pool with minimum browser instances
        await self._prewarm_pool()
        
        logger.info("ResourcePool started with background tasks")

    async def stop(self):
        """Stop the resource pool and cleanup all resources"""
        if not self._is_running:
            return
        
        self._is_running = False
        
        # Cancel background tasks
        for task in [self._cleanup_task, self._health_check_task, self._scaling_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Cleanup all resources
        await self._cleanup_all_resources()
        
        logger.info("ResourcePool stopped and all resources cleaned up")

    async def allocate_browser(self, session_id: str, browser_config: Dict[str, Any] = None) -> Optional[str]:
        """
        Allocate a browser instance for a session.
        
        Args:
            session_id: Session requesting the browser
            browser_config: Browser configuration options
            
        Returns:
            str: Browser ID if successful, None otherwise
        """
        if not self._is_running:
            raise RuntimeError("ResourcePool is not running")
        
        # Check if session already has too many allocations
        session_allocations = len(self._allocations.get(session_id, set()))
        if session_allocations >= 5:  # Limit per session
            logger.warning(f"Session {session_id} has too many resource allocations")
            return None
        
        # Try to get an available browser from pool
        browser_id = await self._get_available_browser()
        
        if not browser_id:
            # Create new browser if pool is empty and under limits
            if len(self._browsers) < self.config.max_browser_instances:
                browser_id = await self._create_browser(browser_config)
            else:
                logger.warning("Browser pool exhausted and at maximum capacity")
                return None
        
        if browser_id:
            # Allocate browser to session
            await self._allocate_resource(browser_id, session_id)
            
        return browser_id

    async def allocate_context(self, session_id: str, browser_id: str, context_config: Dict[str, Any] = None) -> Optional[str]:
        """
        Allocate a browser context for a session.
        
        Args:
            session_id: Session requesting the context
            browser_id: Browser to create context in
            context_config: Context configuration options
            
        Returns:
            str: Context ID if successful, None otherwise
        """
        if browser_id not in self._browsers:
            logger.error(f"Browser {browser_id} not found for context allocation")
            return None
        
        # Check context limits per browser
        browser_contexts = len(self._browser_contexts.get(browser_id, set()))
        if browser_contexts >= self.config.max_contexts_per_browser:
            logger.warning(f"Browser {browser_id} has reached maximum context limit")
            return None
        
        # Create new context
        context_id = await self._create_context(browser_id, context_config)
        
        if context_id:
            # Allocate context to session
            await self._allocate_resource(context_id, session_id)
            
            # Track browser-context relationship
            if browser_id not in self._browser_contexts:
                self._browser_contexts[browser_id] = set()
            self._browser_contexts[browser_id].add(context_id)
        
        return context_id

    async def deallocate_resource(self, resource_id: str, session_id: str = None):
        """
        Deallocate a resource from a session.
        
        Args:
            resource_id: Resource to deallocate
            session_id: Session that owns the resource (optional)
        """
        if resource_id not in self._resource_info:
            return
        
        resource_info = self._resource_info[resource_id]
        
        # Verify ownership if session_id provided
        if session_id and resource_info.allocated_to != session_id:
            logger.warning(f"Session {session_id} trying to deallocate resource {resource_id} not owned by them")
            return
        
        # Update resource state
        resource_info.state = ResourceState.AVAILABLE
        resource_info.allocated_to = None
        resource_info.last_used = datetime.now()
        
        # Remove from session allocations
        if resource_info.allocated_to and resource_info.allocated_to in self._allocations:
            self._allocations[resource_info.allocated_to].discard(resource_id)
        
        self._stats['total_deallocations'] += 1
        
        logger.debug(f"Deallocated resource {resource_id} from session {session_id}")

    async def deallocate_all_for_session(self, session_id: str):
        """Deallocate all resources for a session"""
        if session_id not in self._allocations:
            return
        
        resource_ids = self._allocations[session_id].copy()
        
        for resource_id in resource_ids:
            await self.deallocate_resource(resource_id, session_id)
        
        # Clean up session allocations
        if session_id in self._allocations:
            del self._allocations[session_id]
        
        logger.info(f"Deallocated all resources for session {session_id}")

    async def get_browser(self, browser_id: str) -> Optional[Any]:
        """Get browser instance by ID"""
        return self._browsers.get(browser_id)

    async def get_context(self, context_id: str) -> Optional[Any]:
        """Get context instance by ID"""
        return self._contexts.get(context_id)

    async def get_controller(self, controller_id: str) -> Optional[Any]:
        """Get controller instance by ID"""
        return self._controllers.get(controller_id)

    async def get_resource_stats(self) -> Dict[str, Any]:
        """Get resource pool statistics"""
        current_memory = await self._calculate_memory_usage()
        
        return {
            **self._stats,
            'current_browsers': len(self._browsers),
            'current_contexts': len(self._contexts),
            'current_controllers': len(self._controllers),
            'available_browsers': len([r for r in self._resource_info.values() 
                                     if r.resource_type == ResourceType.BROWSER and r.state == ResourceState.AVAILABLE]),
            'allocated_browsers': len([r for r in self._resource_info.values() 
                                     if r.resource_type == ResourceType.BROWSER and r.state == ResourceState.ALLOCATED]),
            'current_memory_usage_mb': current_memory,
            'memory_limit_mb': self.config.max_memory_usage_mb,
            'memory_usage_percentage': (current_memory / self.config.max_memory_usage_mb) * 100,
            'active_sessions': len(self._allocations),
            'pool_health': await self._check_pool_health()
        }

    async def get_session_resources(self, session_id: str) -> Dict[str, List[str]]:
        """Get all resources allocated to a session"""
        if session_id not in self._allocations:
            return {'browsers': [], 'contexts': [], 'controllers': []}
        
        resource_ids = self._allocations[session_id]
        result = {'browsers': [], 'contexts': [], 'controllers': []}
        
        for resource_id in resource_ids:
            if resource_id in self._resource_info:
                resource_type = self._resource_info[resource_id].resource_type
                if resource_type == ResourceType.BROWSER:
                    result['browsers'].append(resource_id)
                elif resource_type == ResourceType.CONTEXT:
                    result['contexts'].append(resource_id)
                elif resource_type == ResourceType.CONTROLLER:
                    result['controllers'].append(resource_id)
        
        return result

    # Private methods
    
    async def _prewarm_pool(self):
        """Pre-warm the pool with minimum browser instances"""
        current_browsers = len(self._browsers)
        needed_browsers = max(0, self.config.min_browser_instances - current_browsers)
        
        if needed_browsers > 0:
            logger.info(f"Pre-warming pool with {needed_browsers} browser instances")
            
            tasks = []
            for _ in range(needed_browsers):
                task = asyncio.create_task(self._create_browser())
                tasks.append(task)
            
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _get_available_browser(self) -> Optional[str]:
        """Get an available browser from the pool"""
        available_browsers = [
            resource_id for resource_id, info in self._resource_info.items()
            if (info.resource_type == ResourceType.BROWSER and 
                info.state == ResourceState.AVAILABLE and
                resource_id in self._browsers)
        ]
        
        if available_browsers:
            # Return the least recently used browser
            return min(available_browsers, 
                      key=lambda rid: self._resource_info[rid].last_used)
        
        return None

    async def _create_browser(self, browser_config: Dict[str, Any] = None) -> Optional[str]:
        """Create a new browser instance"""
        browser_id = f"browser_{int(time.time() * 1000000)}"
        
        try:
            from src.browser.custom_browser import CustomBrowser
            from browser_use.browser.browser import BrowserConfig
            
            # Default browser configuration
            default_config = {
                'headless': True,
                'disable_security': True,
                'window_width': 1280,
                'window_height': 1100
            }
            
            if browser_config:
                default_config.update(browser_config)
            
            # Create browser
            extra_args = [f"--window-size={default_config['window_width']},{default_config['window_height']}"]
            
            browser = CustomBrowser(
                config=BrowserConfig(
                    headless=default_config['headless'],
                    disable_security=default_config['disable_security'],
                    extra_browser_args=extra_args
                )
            )
            
            # Store browser instance
            self._browsers[browser_id] = browser
            self._weak_refs[browser_id] = browser
            
            # Create resource info
            resource_info = ResourceInfo(
                resource_id=browser_id,
                resource_type=ResourceType.BROWSER,
                state=ResourceState.AVAILABLE,
                created_at=datetime.now(),
                last_used=datetime.now(),
                metadata={'config': default_config}
            )
            
            self._resource_info[browser_id] = resource_info
            self._stats['total_browsers_created'] += 1
            
            logger.debug(f"Created browser {browser_id}")
            return browser_id
            
        except Exception as e:
            logger.error(f"Failed to create browser {browser_id}: {e}", exc_info=True)
            
            # Mark as failed if we created resource info
            if browser_id in self._resource_info:
                self._resource_info[browser_id].state = ResourceState.FAILED
                self._resource_info[browser_id].error_count += 1
            
            return None

    async def _create_context(self, browser_id: str, context_config: Dict[str, Any] = None) -> Optional[str]:
        """Create a new browser context"""
        context_id = f"context_{int(time.time() * 1000000)}"
        
        try:
            browser = self._browsers.get(browser_id)
            if not browser:
                logger.error(f"Browser {browser_id} not found for context creation")
                return None
            
            from src.browser.custom_context import CustomBrowserContextConfig
            from browser_use.browser.context import BrowserContextWindowSize
            
            # Default context configuration
            default_config = {
                'save_downloads_path': f"./tmp/downloads/context_{context_id}",
                'window_width': 1280,
                'window_height': 1100
            }
            
            if context_config:
                default_config.update(context_config)
            
            # Create context
            config = CustomBrowserContextConfig(
                save_downloads_path=default_config['save_downloads_path'],
                browser_window_size=BrowserContextWindowSize(
                    width=default_config['window_width'],
                    height=default_config['window_height']
                ),
                force_new_context=True
            )
            
            context = await browser.new_context(config=config)
            
            # Store context instance
            self._contexts[context_id] = context
            self._weak_refs[context_id] = context
            
            # Create resource info
            resource_info = ResourceInfo(
                resource_id=context_id,
                resource_type=ResourceType.CONTEXT,
                state=ResourceState.AVAILABLE,
                created_at=datetime.now(),
                last_used=datetime.now(),
                metadata={'browser_id': browser_id, 'config': default_config}
            )
            
            self._resource_info[context_id] = resource_info
            self._stats['total_contexts_created'] += 1
            
            logger.debug(f"Created context {context_id} for browser {browser_id}")
            return context_id
            
        except Exception as e:
            logger.error(f"Failed to create context {context_id}: {e}", exc_info=True)
            
            # Mark as failed if we created resource info
            if context_id in self._resource_info:
                self._resource_info[context_id].state = ResourceState.FAILED
                self._resource_info[context_id].error_count += 1
            
            return None

    async def _allocate_resource(self, resource_id: str, session_id: str):
        """Allocate a resource to a session"""
        if resource_id not in self._resource_info:
            logger.error(f"Resource {resource_id} not found for allocation")
            return
        
        resource_info = self._resource_info[resource_id]
        resource_info.state = ResourceState.ALLOCATED
        resource_info.allocated_to = session_id
        resource_info.last_used = datetime.now()
        resource_info.allocation_count += 1
        
        # Track session allocations
        if session_id not in self._allocations:
            self._allocations[session_id] = set()
        self._allocations[session_id].add(resource_id)
        
        self._stats['total_allocations'] += 1
        
        logger.debug(f"Allocated resource {resource_id} to session {session_id}")

    async def _cleanup_loop(self):
        """Background task to cleanup idle and old resources"""
        logger.info("Starting resource cleanup loop")
        
        while self._is_running:
            try:
                await self._cleanup_idle_resources()
                await self._cleanup_old_resources()
                await asyncio.sleep(self.config.cleanup_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}", exc_info=True)
                await asyncio.sleep(self.config.cleanup_interval_seconds)

    async def _health_check_loop(self):
        """Background task to monitor resource health"""
        logger.info("Starting resource health check loop")
        
        while self._is_running:
            try:
                await self._update_memory_usage()
                await self._check_resource_health()
                await asyncio.sleep(self.config.health_check_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in health check loop: {e}", exc_info=True)
                await asyncio.sleep(self.config.health_check_interval_seconds)

    async def _scaling_loop(self):
        """Background task to automatically scale resources"""
        logger.info("Starting resource scaling loop")
        
        while self._is_running:
            try:
                await self._auto_scale_resources()
                await asyncio.sleep(60)  # Check every minute
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in scaling loop: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _cleanup_idle_resources(self):
        """Cleanup resources that have been idle too long"""
        now = datetime.now()
        browser_timeout = timedelta(minutes=self.config.browser_idle_timeout_minutes)
        context_timeout = timedelta(minutes=self.config.context_idle_timeout_minutes)
        
        resources_to_cleanup = []
        
        for resource_id, info in self._resource_info.items():
            if info.state != ResourceState.AVAILABLE:
                continue
            
            idle_time = now - info.last_used
            should_cleanup = False
            
            if info.resource_type == ResourceType.BROWSER and idle_time > browser_timeout:
                # Only cleanup browsers if we have more than minimum
                if len([r for r in self._resource_info.values() 
                       if r.resource_type == ResourceType.BROWSER]) > self.config.min_browser_instances:
                    should_cleanup = True
            elif info.resource_type == ResourceType.CONTEXT and idle_time > context_timeout:
                should_cleanup = True
            
            if should_cleanup:
                resources_to_cleanup.append(resource_id)
        
        for resource_id in resources_to_cleanup:
            await self._destroy_resource(resource_id, reason="idle_timeout")

    async def _cleanup_old_resources(self):
        """Cleanup resources that are too old"""
        now = datetime.now()
        max_age = timedelta(hours=self.config.browser_max_age_hours)
        
        old_resources = []
        
        for resource_id, info in self._resource_info.items():
            if info.resource_type == ResourceType.BROWSER:
                age = now - info.created_at
                if age > max_age and info.state == ResourceState.AVAILABLE:
                    old_resources.append(resource_id)
        
        for resource_id in old_resources:
            await self._destroy_resource(resource_id, reason="max_age_exceeded")

    async def _auto_scale_resources(self):
        """Automatically scale resources based on demand"""
        # Calculate demand metrics
        available_browsers = len([r for r in self._resource_info.values() 
                                if r.resource_type == ResourceType.BROWSER and r.state == ResourceState.AVAILABLE])
        allocated_browsers = len([r for r in self._resource_info.values() 
                                if r.resource_type == ResourceType.BROWSER and r.state == ResourceState.ALLOCATED])
        total_browsers = available_browsers + allocated_browsers
        
        # Scale up if utilization is high
        if available_browsers < 2 and total_browsers < self.config.max_browser_instances:
            logger.info("Scaling up: Creating additional browser instances")
            await self._create_browser()
        
        # Scale down if we have too many idle browsers
        elif available_browsers > self.config.min_browser_instances + 5:
            logger.info("Scaling down: Too many idle browsers")
            # Cleanup will handle this in the next cycle

    async def _update_memory_usage(self):
        """Update memory usage statistics"""
        total_memory = await self._calculate_memory_usage()
        self._stats['current_memory_usage_mb'] = total_memory
        
        if total_memory > self._stats['peak_memory_usage_mb']:
            self._stats['peak_memory_usage_mb'] = total_memory
        
        # Log warning if memory usage is high
        if total_memory > self.config.max_memory_usage_mb * 0.8:
            logger.warning(f"High memory usage: {total_memory:.1f}MB / {self.config.max_memory_usage_mb}MB")

    async def _calculate_memory_usage(self) -> float:
        """Calculate current memory usage"""
        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            return memory_info.rss / (1024 * 1024)  # Convert to MB
        except Exception:
            return 0.0

    async def _check_resource_health(self):
        """Check health of all resources"""
        unhealthy_resources = []
        
        for resource_id, info in self._resource_info.items():
            if info.error_count > 5:  # Too many errors
                unhealthy_resources.append(resource_id)
            elif info.resource_type == ResourceType.BROWSER:
                # Check if browser is still responsive
                browser = self._browsers.get(resource_id)
                if browser and not await self._is_browser_healthy(browser):
                    unhealthy_resources.append(resource_id)
        
        for resource_id in unhealthy_resources:
            await self._destroy_resource(resource_id, reason="health_check_failed")

    async def _is_browser_healthy(self, browser) -> bool:
        """Check if a browser instance is healthy"""
        try:
            # Simple health check - try to access browser properties
            if hasattr(browser, 'playwright_browser') and browser.playwright_browser:
                return not browser.playwright_browser.is_connected() == False
            return True
        except Exception:
            return False

    async def _check_pool_health(self) -> str:
        """Check overall pool health"""
        total_browsers = len(self._browsers)
        available_browsers = len([r for r in self._resource_info.values() 
                                if r.resource_type == ResourceType.BROWSER and r.state == ResourceState.AVAILABLE])
        memory_usage = self._stats['current_memory_usage_mb']
        memory_percentage = (memory_usage / self.config.max_memory_usage_mb) * 100
        
        if total_browsers == 0:
            return "critical"
        elif available_browsers == 0 and total_browsers >= self.config.max_browser_instances:
            return "overloaded"
        elif memory_percentage > 90:
            return "high_memory"
        elif memory_percentage > 70 or available_browsers < 2:
            return "warning"
        else:
            return "healthy"

    async def _destroy_resource(self, resource_id: str, reason: str = "unknown"):
        """Destroy a resource and cleanup associated data"""
        if resource_id not in self._resource_info:
            return
        
        resource_info = self._resource_info[resource_id]
        resource_info.state = ResourceState.CLEANUP
        
        try:
            if resource_info.resource_type == ResourceType.BROWSER:
                await self._destroy_browser(resource_id)
            elif resource_info.resource_type == ResourceType.CONTEXT:
                await self._destroy_context(resource_id)
            elif resource_info.resource_type == ResourceType.CONTROLLER:
                await self._destroy_controller(resource_id)
            
            # Remove from tracking
            del self._resource_info[resource_id]
            
            # Remove from session allocations if allocated
            if resource_info.allocated_to and resource_info.allocated_to in self._allocations:
                self._allocations[resource_info.allocated_to].discard(resource_id)
            
            logger.debug(f"Destroyed resource {resource_id} (reason: {reason})")
            
        except Exception as e:
            logger.error(f"Error destroying resource {resource_id}: {e}", exc_info=True)

    async def _destroy_browser(self, browser_id: str):
        """Destroy a browser instance and all its contexts"""
        # First destroy all contexts for this browser
        if browser_id in self._browser_contexts:
            context_ids = self._browser_contexts[browser_id].copy()
            for context_id in context_ids:
                await self._destroy_context(context_id)
            del self._browser_contexts[browser_id]
        
        # Destroy the browser
        browser = self._browsers.get(browser_id)
        if browser:
            try:
                await browser.close()
            except Exception as e:
                logger.warning(f"Error closing browser {browser_id}: {e}")
            
            del self._browsers[browser_id]
            self._stats['total_browsers_destroyed'] += 1

    async def _destroy_context(self, context_id: str):
        """Destroy a browser context"""
        context = self._contexts.get(context_id)
        if context:
            try:
                await context.close()
            except Exception as e:
                logger.warning(f"Error closing context {context_id}: {e}")
            
            del self._contexts[context_id]
            self._stats['total_contexts_destroyed'] += 1
        
        # Remove from browser-context tracking
        for browser_id, context_set in self._browser_contexts.items():
            context_set.discard(context_id)

    async def _destroy_controller(self, controller_id: str):
        """Destroy a controller instance"""
        controller = self._controllers.get(controller_id)
        if controller:
            try:
                if hasattr(controller, 'close_mcp_client'):
                    await controller.close_mcp_client()
            except Exception as e:
                logger.warning(f"Error closing controller {controller_id}: {e}")
            
            del self._controllers[controller_id]

    async def _cleanup_all_resources(self):
        """Cleanup all resources during shutdown"""
        logger.info("Cleaning up all resources")
        
        # Get all resource IDs
        resource_ids = list(self._resource_info.keys())
        
        # Destroy all resources
        for resource_id in resource_ids:
            await self._destroy_resource(resource_id, reason="pool_shutdown")
        
        # Clear all tracking data
        self._browsers.clear()
        self._contexts.clear()
        self._controllers.clear()
        self._resource_info.clear()
        self._allocations.clear()
        self._browser_contexts.clear()
        
        logger.info("All resources cleaned up")

    @asynccontextmanager
    async def browser_context(self, session_id: str, browser_config: Dict[str, Any] = None):
        """
        Context manager for automatic browser allocation and cleanup.
        
        Usage:
            async with resource_pool.browser_context(session_id) as browser_id:
                browser = await resource_pool.get_browser(browser_id)
                # Use browser...
        """
        browser_id = None
        try:
            browser_id = await self.allocate_browser(session_id, browser_config)
            if not browser_id:
                raise RuntimeError("Failed to allocate browser")
            yield browser_id
        finally:
            if browser_id:
                await self.deallocate_resource(browser_id, session_id)


# Global instance (to be initialized in main app)
resource_pool: Optional[ResourcePool] = None


def get_resource_pool() -> ResourcePool:
    """Get the global resource pool instance"""
    global resource_pool
    if resource_pool is None:
        raise RuntimeError("ResourcePool not initialized. Call init_resource_pool() first.")
    return resource_pool


async def init_resource_pool(config: ResourceConfig = None) -> ResourcePool:
    """Initialize the global resource pool"""
    global resource_pool
    if resource_pool is not None:
        logger.warning("ResourcePool already initialized")
        return resource_pool
    
    resource_pool = ResourcePool(config)
    await resource_pool.start()
    return resource_pool


async def shutdown_resource_pool():
    """Shutdown the global resource pool"""
    global resource_pool
    if resource_pool is not None:
        await resource_pool.stop()
        resource_pool = None