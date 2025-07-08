import asyncio
import logging
import time
import psutil
import weakref
from typing import Dict, List, Optional, Set, Any, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
from contextlib import asynccontextmanager

from src.browser.custom_browser import CustomBrowser
from src.browser.custom_context import CustomBrowserContext, CustomBrowserContextConfig
from browser_use.browser.browser import BrowserConfig
from browser_use.browser.context import BrowserContextWindowSize

logger = logging.getLogger(__name__)


class BrowserState(Enum):
    """Browser lifecycle states"""
    CREATING = "creating"
    AVAILABLE = "available"
    ALLOCATED = "allocated"
    CLEANUP = "cleanup"
    FAILED = "failed"
    MAINTENANCE = "maintenance"


class ContextState(Enum):
    """Context lifecycle states"""
    CREATING = "creating"
    AVAILABLE = "available"
    ALLOCATED = "allocated"
    CLEANUP = "cleanup"
    FAILED = "failed"


@dataclass
class BrowserPoolConfig:
    """Configuration for browser pool management"""
    # Pool sizing
    max_browser_instances: int = 50
    min_browser_instances: int = 5
    max_contexts_per_browser: int = 10
    target_available_browsers: int = 3
    
    # Lifecycle timeouts
    browser_idle_timeout_minutes: int = 10
    context_idle_timeout_minutes: int = 5
    browser_max_age_hours: int = 4
    
    # Resource limits
    max_memory_usage_mb: int = 8000  # 8GB total
    max_memory_per_browser_mb: int = 200  # 200MB per browser
    memory_warning_threshold: float = 0.8  # 80% of max
    
    # Background task intervals
    cleanup_interval_seconds: int = 30
    health_check_interval_seconds: int = 60
    pool_maintenance_interval_seconds: int = 120
    
    # Performance settings
    enable_browser_pooling: bool = True
    enable_context_pooling: bool = True
    enable_memory_monitoring: bool = True
    enable_automatic_scaling: bool = True
    enable_health_monitoring: bool = True
    
    # Browser configuration
    default_headless: bool = True
    default_disable_security: bool = True
    default_window_width: int = 1280
    default_window_height: int = 1100


@dataclass
class BrowserInfo:
    """Information about a managed browser instance"""
    browser_id: str
    browser_instance: CustomBrowser
    state: BrowserState
    created_at: datetime
    last_used: datetime
    allocated_to: Optional[str] = None  # session_id
    contexts: Dict[str, 'ContextInfo'] = None
    memory_usage_mb: float = 0.0
    allocation_count: int = 0
    error_count: int = 0
    health_score: float = 1.0
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.contexts is None:
            self.contexts = {}
        if self.metadata is None:
            self.metadata = {}


@dataclass
class ContextInfo:
    """Information about a managed browser context"""
    context_id: str
    context_instance: CustomBrowserContext
    browser_id: str
    state: ContextState
    created_at: datetime
    last_used: datetime
    allocated_to: Optional[str] = None  # session_id
    memory_usage_mb: float = 0.0
    page_count: int = 0
    error_count: int = 0
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class BrowserPool:
    """
    Enhanced browser instance pool manager for multi-user sessions.
    Provides efficient browser and context allocation, lifecycle management, and monitoring.
    """
    
    def __init__(self, config: BrowserPoolConfig = None):
        self.config = config or BrowserPoolConfig()
        
        # Core storage
        self._browsers: Dict[str, BrowserInfo] = {}  # browser_id -> browser_info
        self._contexts: Dict[str, ContextInfo] = {}  # context_id -> context_info
        self._session_allocations: Dict[str, Set[str]] = {}  # session_id -> {browser_ids, context_ids}
        
        # Pool management
        self._available_browsers: Set[str] = set()  # Available browser IDs
        self._available_contexts: Set[str] = set()  # Available context IDs
        self._allocation_queue: asyncio.Queue = asyncio.Queue()
        
        # Background tasks
        self._cleanup_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._pool_maintenance_task: Optional[asyncio.Task] = None
        self._memory_monitor_task: Optional[asyncio.Task] = None
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
            'peak_memory_usage_mb': 0.0,
            'average_browser_age_minutes': 0.0,
            'pool_efficiency_percent': 0.0,
            'health_check_failures': 0,
            'memory_warnings': 0,
        }
        
        # Weak references for automatic cleanup
        self._weak_refs: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
        
        # Lock for thread-safe operations
        self._allocation_lock = asyncio.Lock()
        
        logger.info(f"BrowserPool initialized with config: {self.config}")

    async def start(self):
        """Start the browser pool and background tasks"""
        if self._is_running:
            logger.warning("BrowserPool is already running")
            return
        
        self._is_running = True
        
        # Start background tasks
        if self.config.enable_browser_pooling:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            self._pool_maintenance_task = asyncio.create_task(self._pool_maintenance_loop())
            
        if self.config.enable_health_monitoring:
            self._health_check_task = asyncio.create_task(self._health_check_loop())
            
        if self.config.enable_memory_monitoring:
            self._memory_monitor_task = asyncio.create_task(self._memory_monitor_loop())
        
        # Pre-warm the pool with minimum browser instances
        await self._prewarm_pool()
        
        logger.info("BrowserPool started with background tasks")

    async def stop(self):
        """Stop the browser pool and cleanup all resources"""
        if not self._is_running:
            return
        
        self._is_running = False
        
        # Cancel background tasks
        for task in [self._cleanup_task, self._health_check_task, 
                    self._pool_maintenance_task, self._memory_monitor_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Cleanup all resources
        await self._cleanup_all_resources()
        
        logger.info("BrowserPool stopped and all resources cleaned up")

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
            raise RuntimeError("BrowserPool is not running")
        
        async with self._allocation_lock:
            # Check resource limits
            session_allocation_count = len(self._session_allocations.get(session_id, set()))
            if session_allocation_count >= 5:  # Limit per session
                logger.warning(f"Session {session_id} has too many resource allocations")
                return None
            
            # Check memory constraints
            if await self._check_memory_constraints():
                logger.warning("Memory constraints prevent new browser allocation")
                return None
            
            # Try to get an available browser from pool
            browser_id = await self._get_available_browser(browser_config)
            
            if not browser_id:
                # Create new browser if pool is empty and under limits
                if len(self._browsers) < self.config.max_browser_instances:
                    browser_id = await self._create_browser(browser_config)
                else:
                    logger.warning("Browser pool exhausted and at maximum capacity")
                    return None
            
            if browser_id:
                # Allocate browser to session
                await self._allocate_resource(browser_id, session_id, 'browser')
                
            return browser_id

    async def allocate_context(self, session_id: str, browser_id: str = None, 
                             context_config: Dict[str, Any] = None) -> Optional[str]:
        """
        Allocate a browser context for a session.
        
        Args:
            session_id: Session requesting the context
            browser_id: Specific browser to create context in (optional)
            context_config: Context configuration options
            
        Returns:
            str: Context ID if successful, None otherwise
        """
        async with self._allocation_lock:
            # If no browser specified, find the best available browser
            if not browser_id:
                browser_id = await self._find_best_browser_for_context()
                
            if not browser_id or browser_id not in self._browsers:
                logger.error(f"Browser {browser_id} not found for context allocation")
                return None
            
            browser_info = self._browsers[browser_id]
            
            # Check context limits per browser
            if len(browser_info.contexts) >= self.config.max_contexts_per_browser:
                logger.warning(f"Browser {browser_id} has reached maximum context limit")
                return None
            
            # Create new context
            context_id = await self._create_context(browser_id, context_config)
            
            if context_id:
                # Allocate context to session
                await self._allocate_resource(context_id, session_id, 'context')
                
            return context_id

    async def deallocate_resource(self, resource_id: str, session_id: str = None):
        """
        Deallocate a resource from a session.
        
        Args:
            resource_id: Resource to deallocate (browser_id or context_id)
            session_id: Session that owns the resource (optional)
        """
        async with self._allocation_lock:
            # Determine resource type
            is_browser = resource_id in self._browsers
            is_context = resource_id in self._contexts
            
            if not (is_browser or is_context):
                logger.warning(f"Resource {resource_id} not found")
                return
            
            if is_browser:
                browser_info = self._browsers[resource_id]
                
                # Verify ownership if session_id provided
                if session_id and browser_info.allocated_to != session_id:
                    logger.warning(f"Session {session_id} trying to deallocate browser {resource_id} not owned by them")
                    return
                
                # Deallocate all contexts in this browser first
                for context_id in list(browser_info.contexts.keys()):
                    await self.deallocate_resource(context_id, session_id)
                
                # Return browser to available pool
                browser_info.state = BrowserState.AVAILABLE
                browser_info.allocated_to = None
                browser_info.last_used = datetime.now()
                self._available_browsers.add(resource_id)
                
            elif is_context:
                context_info = self._contexts[resource_id]
                
                # Verify ownership if session_id provided
                if session_id and context_info.allocated_to != session_id:
                    logger.warning(f"Session {session_id} trying to deallocate context {resource_id} not owned by them")
                    return
                
                # Clean up context
                await self._cleanup_context(context_info)
                
                # Return context to available pool or destroy
                if self.config.enable_context_pooling and context_info.error_count == 0:
                    context_info.state = ContextState.AVAILABLE
                    context_info.allocated_to = None
                    context_info.last_used = datetime.now()
                    self._available_contexts.add(resource_id)
                else:
                    await self._destroy_context(resource_id)
            
            # Remove from session allocations
            if session_id and session_id in self._session_allocations:
                self._session_allocations[session_id].discard(resource_id)
                if not self._session_allocations[session_id]:
                    del self._session_allocations[session_id]
            
            self._stats['total_deallocations'] += 1
            
            logger.debug(f"Deallocated resource {resource_id} from session {session_id}")

    async def deallocate_all_for_session(self, session_id: str):
        """Deallocate all resources for a session"""
        if session_id not in self._session_allocations:
            return
        
        resource_ids = self._session_allocations[session_id].copy()
        
        for resource_id in resource_ids:
            await self.deallocate_resource(resource_id, session_id)
        
        logger.info(f"Deallocated all resources for session {session_id}")

    async def get_browser(self, browser_id: str) -> Optional[CustomBrowser]:
        """Get browser instance by ID"""
        browser_info = self._browsers.get(browser_id)
        return browser_info.browser_instance if browser_info else None

    async def get_context(self, context_id: str) -> Optional[CustomBrowserContext]:
        """Get context instance by ID"""
        context_info = self._contexts.get(context_id)
        return context_info.context_instance if context_info else None

    async def get_resource_stats(self) -> Dict[str, Any]:
        """Get comprehensive resource pool statistics"""
        current_memory = await self._calculate_memory_usage()
        
        # Calculate efficiency metrics
        total_browsers = len(self._browsers)
        available_browsers = len(self._available_browsers)
        allocated_browsers = total_browsers - available_browsers
        
        efficiency = 0.0
        if total_browsers > 0:
            efficiency = (allocated_browsers / total_browsers) * 100
        
        # Calculate average browser age
        avg_age = 0.0
        if self._browsers:
            total_age = sum(
                (datetime.now() - browser.created_at).total_seconds() / 60
                for browser in self._browsers.values()
            )
            avg_age = total_age / len(self._browsers)
        
        return {
            **self._stats,
            'current_browsers': total_browsers,
            'current_contexts': len(self._contexts),
            'available_browsers': available_browsers,
            'allocated_browsers': allocated_browsers,
            'available_contexts': len(self._available_contexts),
            'allocated_contexts': len(self._contexts) - len(self._available_contexts),
            'current_memory_usage_mb': current_memory,
            'memory_limit_mb': self.config.max_memory_usage_mb,
            'memory_usage_percentage': (current_memory / self.config.max_memory_usage_mb) * 100,
            'pool_efficiency_percent': efficiency,
            'average_browser_age_minutes': avg_age,
            'active_sessions': len(self._session_allocations),
            'pool_health': await self._check_pool_health(),
            'browser_states': self._get_browsers_by_state(),
            'context_states': self._get_contexts_by_state(),
        }

    async def get_session_resources(self, session_id: str) -> Dict[str, List[str]]:
        """Get all resources allocated to a session"""
        if session_id not in self._session_allocations:
            return {'browsers': [], 'contexts': []}
        
        resource_ids = self._session_allocations[session_id]
        result = {'browsers': [], 'contexts': []}
        
        for resource_id in resource_ids:
            if resource_id in self._browsers:
                result['browsers'].append(resource_id)
            elif resource_id in self._contexts:
                result['contexts'].append(resource_id)
        
        return result

    async def force_cleanup(self, resource_type: str = "all"):
        """Force cleanup of resources"""
        if resource_type in ["all", "contexts"]:
            await self._cleanup_idle_contexts()
        
        if resource_type in ["all", "browsers"]:
            await self._cleanup_idle_browsers()
            
        if resource_type in ["all", "failed"]:
            await self._cleanup_failed_resources()

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

    async def _get_available_browser(self, browser_config: Dict[str, Any] = None) -> Optional[str]:
        """Get an available browser from the pool"""
        if not self._available_browsers:
            return None
        
        # Find the best available browser based on health score and age
        best_browser_id = None
        best_score = -1
        
        for browser_id in self._available_browsers:
            if browser_id not in self._browsers:
                continue
                
            browser_info = self._browsers[browser_id]
            
            # Calculate composite score (health + age factor)
            age_factor = min(1.0, (datetime.now() - browser_info.created_at).total_seconds() / 3600)  # Hours
            score = browser_info.health_score * (1 - age_factor * 0.1)  # Slight preference for newer browsers
            
            if score > best_score:
                best_score = score
                best_browser_id = browser_id
        
        if best_browser_id:
            self._available_browsers.remove(best_browser_id)
            return best_browser_id
        
        return None

    async def _find_best_browser_for_context(self) -> Optional[str]:
        """Find the best browser for creating a new context"""
        best_browser_id = None
        best_score = -1
        
        for browser_id, browser_info in self._browsers.items():
            if browser_info.state != BrowserState.ALLOCATED:
                continue
                
            # Calculate score based on context count and health
            context_count = len(browser_info.contexts)
            if context_count >= self.config.max_contexts_per_browser:
                continue
                
            # Prefer browsers with fewer contexts
            context_factor = 1.0 - (context_count / self.config.max_contexts_per_browser)
            score = browser_info.health_score * context_factor
            
            if score > best_score:
                best_score = score
                best_browser_id = browser_id
        
        return best_browser_id

    async def _create_browser(self, browser_config: Dict[str, Any] = None) -> Optional[str]:
        """Create a new browser instance"""
        browser_id = f"browser_{int(time.time() * 1000000)}"
        
        try:
            # Default browser configuration
            default_config = {
                'headless': self.config.default_headless,
                'disable_security': self.config.default_disable_security,
                'window_width': self.config.default_window_width,
                'window_height': self.config.default_window_height
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
            
            # Create browser info
            browser_info = BrowserInfo(
                browser_id=browser_id,
                browser_instance=browser,
                state=BrowserState.AVAILABLE,
                created_at=datetime.now(),
                last_used=datetime.now(),
                metadata={'config': default_config}
            )
            
            # Store browser
            self._browsers[browser_id] = browser_info
            self._weak_refs[browser_id] = browser_info
            self._available_browsers.add(browser_id)
            
            self._stats['total_browsers_created'] += 1
            
            logger.debug(f"Created browser {browser_id}")
            return browser_id
            
        except Exception as e:
            logger.error(f"Failed to create browser {browser_id}: {e}", exc_info=True)
            
            # Mark as failed if we created browser info
            if browser_id in self._browsers:
                self._browsers[browser_id].state = BrowserState.FAILED
                self._browsers[browser_id].error_count += 1
            
            return None

    async def _create_context(self, browser_id: str, context_config: Dict[str, Any] = None) -> Optional[str]:
        """Create a new browser context"""
        context_id = f"context_{int(time.time() * 1000000)}"
        
        try:
            browser_info = self._browsers.get(browser_id)
            if not browser_info:
                logger.error(f"Browser {browser_id} not found for context creation")
                return None
            
            browser = browser_info.browser_instance
            
            # Default context configuration
            default_config = {
                'save_downloads_path': f"./tmp/downloads/context_{context_id}",
                'window_width': self.config.default_window_width,
                'window_height': self.config.default_window_height
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
            
            # Create context info
            context_info = ContextInfo(
                context_id=context_id,
                context_instance=context,
                browser_id=browser_id,
                state=ContextState.AVAILABLE,
                created_at=datetime.now(),
                last_used=datetime.now(),
                metadata={'config': default_config}
            )
            
            # Store context
            self._contexts[context_id] = context_info
            self._weak_refs[context_id] = context_info
            browser_info.contexts[context_id] = context_info
            self._available_contexts.add(context_id)
            
            self._stats['total_contexts_created'] += 1
            
            logger.debug(f"Created context {context_id} for browser {browser_id}")
            return context_id
            
        except Exception as e:
            logger.error(f"Failed to create context {context_id}: {e}", exc_info=True)
            
            # Mark as failed if we created context info
            if context_id in self._contexts:
                self._contexts[context_id].state = ContextState.FAILED
                self._contexts[context_id].error_count += 1
            
            return None

    async def _allocate_resource(self, resource_id: str, session_id: str, resource_type: str):
        """Allocate a resource to a session"""
        if resource_type == 'browser':
            if resource_id not in self._browsers:
                logger.error(f"Browser {resource_id} not found for allocation")
                return
            
            browser_info = self._browsers[resource_id]
            browser_info.state = BrowserState.ALLOCATED
            browser_info.allocated_to = session_id
            browser_info.last_used = datetime.now()
            browser_info.allocation_count += 1
            
        elif resource_type == 'context':
            if resource_id not in self._contexts:
                logger.error(f"Context {resource_id} not found for allocation")
                return
            
            context_info = self._contexts[resource_id]
            context_info.state = ContextState.ALLOCATED
            context_info.allocated_to = session_id
            context_info.last_used = datetime.now()
            self._available_contexts.discard(resource_id)
        
        # Track session allocations
        if session_id not in self._session_allocations:
            self._session_allocations[session_id] = set()
        self._session_allocations[session_id].add(resource_id)
        
        self._stats['total_allocations'] += 1
        
        logger.debug(f"Allocated {resource_type} {resource_id} to session {session_id}")

    async def _cleanup_loop(self):
        """Background task to cleanup idle and old resources"""
        logger.info("Starting resource cleanup loop")
        
        while self._is_running:
            try:
                await self._cleanup_idle_contexts()
                await self._cleanup_idle_browsers()
                await self._cleanup_old_browsers()
                await self._cleanup_failed_resources()
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
                await self._check_browser_health()
                await self._check_context_health()
                await asyncio.sleep(self.config.health_check_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in health check loop: {e}", exc_info=True)
                await asyncio.sleep(self.config.health_check_interval_seconds)

    async def _pool_maintenance_loop(self):
        """Background task to maintain optimal pool size"""
        logger.info("Starting pool maintenance loop")
        
        while self._is_running:
            try:
                await self._maintain_pool_size()
                await self._balance_browser_contexts()
                await asyncio.sleep(self.config.pool_maintenance_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in pool maintenance loop: {e}", exc_info=True)
                await asyncio.sleep(self.config.pool_maintenance_interval_seconds)

    async def _memory_monitor_loop(self):
        """Background task to monitor memory usage"""
        logger.info("Starting memory monitoring loop")
        
        while self._is_running:
            try:
                await self._update_memory_usage()
                await self._check_memory_constraints()
                await asyncio.sleep(30)  # Check every 30 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in memory monitor loop: {e}", exc_info=True)
                await asyncio.sleep(30)

    async def _cleanup_idle_contexts(self):
        """Cleanup contexts that have been idle too long"""
        now = datetime.now()
        timeout = timedelta(minutes=self.config.context_idle_timeout_minutes)
        
        contexts_to_cleanup = []
        
        for context_id, context_info in self._contexts.items():
            if (context_info.state == ContextState.AVAILABLE and 
                now - context_info.last_used > timeout):
                contexts_to_cleanup.append(context_id)
        
        for context_id in contexts_to_cleanup:
            await self._destroy_context(context_id)

    async def _cleanup_idle_browsers(self):
        """Cleanup browsers that have been idle too long"""
        now = datetime.now()
        timeout = timedelta(minutes=self.config.browser_idle_timeout_minutes)
        
        browsers_to_cleanup = []
        
        for browser_id, browser_info in self._browsers.items():
            if (browser_info.state == BrowserState.AVAILABLE and 
                now - browser_info.last_used > timeout and
                len(self._browsers) > self.config.min_browser_instances):
                browsers_to_cleanup.append(browser_id)
        
        for browser_id in browsers_to_cleanup:
            await self._destroy_browser(browser_id)

    async def _cleanup_old_browsers(self):
        """Cleanup browsers that are too old"""
        now = datetime.now()
        max_age = timedelta(hours=self.config.browser_max_age_hours)
        
        old_browsers = []
        
        for browser_id, browser_info in self._browsers.items():
            age = now - browser_info.created_at
            if (age > max_age and 
                browser_info.state == BrowserState.AVAILABLE and
                len(self._browsers) > self.config.min_browser_instances):
                old_browsers.append(browser_id)
        
        for browser_id in old_browsers:
            await self._destroy_browser(browser_id)

    async def _cleanup_failed_resources(self):
        """Cleanup resources that have failed"""
        # Cleanup failed browsers
        failed_browsers = [
            browser_id for browser_id, browser_info in self._browsers.items()
            if browser_info.state == BrowserState.FAILED
        ]
        
        for browser_id in failed_browsers:
            await self._destroy_browser(browser_id)
        
        # Cleanup failed contexts
        failed_contexts = [
            context_id for context_id, context_info in self._contexts.items()
            if context_info.state == ContextState.FAILED
        ]
        
        for context_id in failed_contexts:
            await self._destroy_context(context_id)

    async def _maintain_pool_size(self):
        """Maintain optimal pool size"""
        current_available = len(self._available_browsers)
        target_available = self.config.target_available_browsers
        
        if current_available < target_available:
            # Create more browsers
            browsers_to_create = min(
                target_available - current_available,
                self.config.max_browser_instances - len(self._browsers)
            )
            
            if browsers_to_create > 0:
                logger.info(f"Creating {browsers_to_create} additional browsers for pool")
                tasks = [self._create_browser() for _ in range(browsers_to_create)]
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _balance_browser_contexts(self):
        """Balance contexts across browsers"""
        # Find browsers with too many contexts
        overloaded_browsers = [
            browser_id for browser_id, browser_info in self._browsers.items()
            if len(browser_info.contexts) > self.config.max_contexts_per_browser * 0.8
        ]
        
        # Log warning if browsers are getting overloaded
        if overloaded_browsers:
            logger.warning(f"Browsers with high context load: {len(overloaded_browsers)}")

    async def _check_browser_health(self):
        """Check health of all browsers"""
        unhealthy_browsers = []
        
        for browser_id, browser_info in self._browsers.items():
            if browser_info.error_count > 5:  # Too many errors
                browser_info.health_score = max(0.1, browser_info.health_score - 0.1)
                if browser_info.health_score < 0.3:
                    unhealthy_browsers.append(browser_id)
            elif await self._is_browser_responsive(browser_info.browser_instance):
                browser_info.health_score = min(1.0, browser_info.health_score + 0.05)
            else:
                browser_info.health_score = max(0.1, browser_info.health_score - 0.2)
                if browser_info.health_score < 0.3:
                    unhealthy_browsers.append(browser_id)
        
        # Cleanup unhealthy browsers
        for browser_id in unhealthy_browsers:
            if self._browsers[browser_id].state == BrowserState.AVAILABLE:
                await self._destroy_browser(browser_id)
            else:
                # Mark for cleanup when available
                self._browsers[browser_id].state = BrowserState.MAINTENANCE

    async def _check_context_health(self):
        """Check health of all contexts"""
        unhealthy_contexts = []
        
        for context_id, context_info in self._contexts.items():
            if context_info.error_count > 3:  # Too many errors
                unhealthy_contexts.append(context_id)
        
        for context_id in unhealthy_contexts:
            if self._contexts[context_id].state == ContextState.AVAILABLE:
                await self._destroy_context(context_id)

    async def _is_browser_responsive(self, browser: CustomBrowser) -> bool:
        """Check if a browser instance is responsive"""
        try:
            if hasattr(browser, 'playwright_browser') and browser.playwright_browser:
                return browser.playwright_browser.is_connected()
            return True
        except Exception:
            return False

    async def _update_memory_usage(self):
        """Update memory usage statistics"""
        total_memory = await self._calculate_memory_usage()
        self._stats['current_memory_usage_mb'] = total_memory
        
        if total_memory > self._stats['peak_memory_usage_mb']:
            self._stats['peak_memory_usage_mb'] = total_memory

    async def _calculate_memory_usage(self) -> float:
        """Calculate current memory usage"""
        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            return memory_info.rss / (1024 * 1024)  # Convert to MB
        except Exception:
            return 0.0

    async def _check_memory_constraints(self) -> bool:
        """Check if memory usage is within constraints"""
        current_memory = await self._calculate_memory_usage()
        memory_limit = self.config.max_memory_usage_mb
        
        if current_memory > memory_limit * self.config.memory_warning_threshold:
            self._stats['memory_warnings'] += 1
            logger.warning(f"High memory usage: {current_memory:.1f}MB / {memory_limit}MB")
            
            if current_memory > memory_limit:
                # Emergency cleanup
                await self._emergency_memory_cleanup()
                return True
        
        return False

    async def _emergency_memory_cleanup(self):
        """Emergency cleanup when memory limit exceeded"""
        logger.warning("Executing emergency memory cleanup")
        
        # Clean up available contexts first
        available_contexts = [
            context_id for context_id in self._available_contexts
            if context_id in self._contexts
        ]
        
        for context_id in available_contexts[:5]:  # Clean up to 5 contexts
            await self._destroy_context(context_id)
        
        # Clean up available browsers if still over limit
        if await self._calculate_memory_usage() > self.config.max_memory_usage_mb:
            available_browsers = list(self._available_browsers)
            for browser_id in available_browsers[:2]:  # Clean up to 2 browsers
                if len(self._browsers) > self.config.min_browser_instances:
                    await self._destroy_browser(browser_id)

    async def _cleanup_context(self, context_info: ContextInfo):
        """Clean up a context before deallocation"""
        try:
            # Close all pages in the context
            if hasattr(context_info.context_instance, 'playwright_context'):
                pw_context = context_info.context_instance.playwright_context
                if pw_context:
                    for page in pw_context.pages:
                        try:
                            await page.close()
                        except Exception:
                            pass
        except Exception as e:
            logger.debug(f"Error cleaning up context {context_info.context_id}: {e}")

    async def _destroy_browser(self, browser_id: str):
        """Destroy a browser instance and all its contexts"""
        browser_info = self._browsers.get(browser_id)
        if not browser_info:
            return
        
        try:
            # First destroy all contexts for this browser
            context_ids = list(browser_info.contexts.keys())
            for context_id in context_ids:
                await self._destroy_context(context_id)
            
            # Destroy the browser
            browser = browser_info.browser_instance
            if browser:
                try:
                    await browser.close()
                except Exception as e:
                    logger.warning(f"Error closing browser {browser_id}: {e}")
            
            # Remove from tracking
            del self._browsers[browser_id]
            self._available_browsers.discard(browser_id)
            self._stats['total_browsers_destroyed'] += 1
            
            logger.debug(f"Destroyed browser {browser_id}")
            
        except Exception as e:
            logger.error(f"Error destroying browser {browser_id}: {e}", exc_info=True)

    async def _destroy_context(self, context_id: str):
        """Destroy a browser context"""
        context_info = self._contexts.get(context_id)
        if not context_info:
            return
        
        try:
            # Clean up context
            await self._cleanup_context(context_info)
            
            # Close the context
            context = context_info.context_instance
            if context:
                try:
                    await context.close()
                except Exception as e:
                    logger.warning(f"Error closing context {context_id}: {e}")
            
            # Remove from browser tracking
            browser_id = context_info.browser_id
            if browser_id in self._browsers:
                self._browsers[browser_id].contexts.pop(context_id, None)
            
            # Remove from global tracking
            del self._contexts[context_id]
            self._available_contexts.discard(context_id)
            self._stats['total_contexts_destroyed'] += 1
            
            logger.debug(f"Destroyed context {context_id}")
            
        except Exception as e:
            logger.error(f"Error destroying context {context_id}: {e}", exc_info=True)

    async def _check_pool_health(self) -> str:
        """Check overall pool health"""
        total_browsers = len(self._browsers)
        available_browsers = len(self._available_browsers)
        memory_usage = self._stats['current_memory_usage_mb']
        memory_percentage = (memory_usage / self.config.max_memory_usage_mb) * 100
        
        failed_browsers = len([b for b in self._browsers.values() if b.state == BrowserState.FAILED])
        
        if total_browsers == 0:
            return "critical"
        elif failed_browsers > total_browsers * 0.3:  # More than 30% failed
            return "critical"
        elif available_browsers == 0 and total_browsers >= self.config.max_browser_instances:
            return "overloaded"
        elif memory_percentage > 90:
            return "high_memory"
        elif memory_percentage > 70 or available_browsers < 1:
            return "warning"
        else:
            return "healthy"

    def _get_browsers_by_state(self) -> Dict[str, int]:
        """Get count of browsers by state"""
        state_counts = {}
        for state in BrowserState:
            state_counts[state.value] = 0
        
        for browser in self._browsers.values():
            state_counts[browser.state.value] += 1
        
        return state_counts

    def _get_contexts_by_state(self) -> Dict[str, int]:
        """Get count of contexts by state"""
        state_counts = {}
        for state in ContextState:
            state_counts[state.value] = 0
        
        for context in self._contexts.values():
            state_counts[context.state.value] += 1
        
        return state_counts

    async def _cleanup_all_resources(self):
        """Cleanup all resources during shutdown"""
        logger.info("Cleaning up all browser pool resources")
        
        # Get all resource IDs
        browser_ids = list(self._browsers.keys())
        context_ids = list(self._contexts.keys())
        
        # Destroy all contexts first
        for context_id in context_ids:
            await self._destroy_context(context_id)
        
        # Destroy all browsers
        for browser_id in browser_ids:
            await self._destroy_browser(browser_id)
        
        # Clear all tracking data
        self._browsers.clear()
        self._contexts.clear()
        self._session_allocations.clear()
        self._available_browsers.clear()
        self._available_contexts.clear()
        
        logger.info("All browser pool resources cleaned up")

    @asynccontextmanager
    async def browser_context(self, session_id: str, browser_config: Dict[str, Any] = None):
        """
        Context manager for automatic browser allocation and cleanup.
        
        Usage:
            async with browser_pool.browser_context(session_id) as browser_id:
                browser = await browser_pool.get_browser(browser_id)
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
browser_pool: Optional[BrowserPool] = None


def get_browser_pool() -> BrowserPool:
    """Get the global browser pool instance"""
    global browser_pool
    if browser_pool is None:
        raise RuntimeError("BrowserPool not initialized. Call init_browser_pool() first.")
    return browser_pool


async def init_browser_pool(config: BrowserPoolConfig = None) -> BrowserPool:
    """Initialize the global browser pool"""
    global browser_pool
    if browser_pool is not None:
        logger.warning("BrowserPool already initialized")
        return browser_pool
    
    browser_pool = BrowserPool(config)
    await browser_pool.start()
    return browser_pool


async def shutdown_browser_pool():
    """Shutdown the global browser pool"""
    global browser_pool
    if browser_pool is not None:
        await browser_pool.stop()
        browser_pool = None