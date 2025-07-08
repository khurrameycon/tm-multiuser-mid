import asyncio
import logging
import os
import shutil
import time
import gc
import psutil
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime, timedelta
from pathlib import Path
import weakref
import threading

logger = logging.getLogger(__name__)


class ResourceCleaner:
    """
    Centralized resource cleanup management.
    Handles cleanup of temporary files, memory, and orphaned resources.
    """
    
    def __init__(self):
        self.cleanup_tasks: Dict[str, Callable] = {}
        self.cleanup_intervals: Dict[str, int] = {}  # seconds
        self.cleanup_task: Optional[asyncio.Task] = None
        self.is_running = False
        
        # Statistics
        self.stats = {
            'files_cleaned': 0,
            'memory_freed_mb': 0,
            'cleanup_runs': 0,
            'last_cleanup': None,
            'errors': 0
        }
        
        # Register default cleanup tasks
        self._register_default_tasks()
        
        logger.info("ResourceCleaner initialized")

    def _register_default_tasks(self):
        """Register default cleanup tasks"""
        # Temporary files cleanup
        self.register_cleanup_task(
            'temp_files',
            self._cleanup_temp_files,
            interval_seconds=300  # Every 5 minutes
        )
        
        # Old downloads cleanup
        self.register_cleanup_task(
            'old_downloads',
            self._cleanup_old_downloads,
            interval_seconds=1800  # Every 30 minutes
        )
        
        # Memory cleanup
        self.register_cleanup_task(
            'memory',
            self._cleanup_memory,
            interval_seconds=600  # Every 10 minutes
        )
        
        # Log files cleanup
        self.register_cleanup_task(
            'log_files',
            self._cleanup_log_files,
            interval_seconds=3600  # Every hour
        )
        
        # Orphaned processes cleanup
        self.register_cleanup_task(
            'orphaned_processes',
            self._cleanup_orphaned_processes,
            interval_seconds=900  # Every 15 minutes
        )

    def register_cleanup_task(self, name: str, task_func: Callable, interval_seconds: int):
        """Register a cleanup task"""
        self.cleanup_tasks[name] = task_func
        self.cleanup_intervals[name] = interval_seconds
        logger.debug(f"Registered cleanup task: {name} (interval: {interval_seconds}s)")

    async def start(self):
        """Start the cleanup service"""
        if self.is_running:
            logger.warning("ResourceCleaner is already running")
            return
        
        self.is_running = True
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("ResourceCleaner started")

    async def stop(self):
        """Stop the cleanup service"""
        if not self.is_running:
            return
        
        self.is_running = False
        
        if self.cleanup_task:
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
        
        logger.info("ResourceCleaner stopped")

    async def force_cleanup(self, task_name: str = None) -> Dict[str, Any]:
        """Force run cleanup tasks"""
        if task_name:
            if task_name in self.cleanup_tasks:
                result = await self._run_cleanup_task(task_name)
                return {task_name: result}
            else:
                raise ValueError(f"Unknown cleanup task: {task_name}")
        else:
            # Run all tasks
            results = {}
            for name in self.cleanup_tasks:
                results[name] = await self._run_cleanup_task(name)
            return results

    async def _cleanup_loop(self):
        """Main cleanup loop"""
        logger.info("Starting cleanup loop")
        task_last_run = {name: 0 for name in self.cleanup_tasks}
        
        while self.is_running:
            try:
                current_time = time.time()
                
                for task_name, interval in self.cleanup_intervals.items():
                    if current_time - task_last_run[task_name] >= interval:
                        await self._run_cleanup_task(task_name)
                        task_last_run[task_name] = current_time
                
                await asyncio.sleep(60)  # Check every minute
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}", exc_info=True)
                self.stats['errors'] += 1
                await asyncio.sleep(60)

    async def _run_cleanup_task(self, task_name: str) -> Dict[str, Any]:
        """Run a specific cleanup task"""
        if task_name not in self.cleanup_tasks:
            return {'error': f'Unknown task: {task_name}'}
        
        try:
            logger.debug(f"Running cleanup task: {task_name}")
            start_time = time.time()
            
            task_func = self.cleanup_tasks[task_name]
            if asyncio.iscoroutinefunction(task_func):
                result = await task_func()
            else:
                result = await asyncio.get_event_loop().run_in_executor(None, task_func)
            
            duration = time.time() - start_time
            
            self.stats['cleanup_runs'] += 1
            self.stats['last_cleanup'] = datetime.now().isoformat()
            
            logger.debug(f"Cleanup task {task_name} completed in {duration:.2f}s")
            
            return {
                'success': True,
                'duration_seconds': duration,
                'result': result
            }
            
        except Exception as e:
            logger.error(f"Error in cleanup task {task_name}: {e}", exc_info=True)
            self.stats['errors'] += 1
            return {
                'success': False,
                'error': str(e)
            }

    async def _cleanup_temp_files(self) -> Dict[str, Any]:
        """Clean up temporary files"""
        from src.config.settings import get_settings
        settings = get_settings()
        
        temp_dirs = [
            settings.tmp_directory,
            '/tmp',
            settings.downloads_directory,
            './tmp/sessions',
            './tmp/recordings'
        ]
        
        files_removed = 0
        space_freed = 0
        
        for temp_dir in temp_dirs:
            if not os.path.exists(temp_dir):
                continue
            
            try:
                for root, dirs, files in os.walk(temp_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        
                        # Check if file is old enough to clean
                        if self._should_cleanup_file(file_path):
                            try:
                                file_size = os.path.getsize(file_path)
                                os.remove(file_path)
                                files_removed += 1
                                space_freed += file_size
                                logger.debug(f"Removed temp file: {file_path}")
                            except (OSError, PermissionError) as e:
                                logger.debug(f"Could not remove {file_path}: {e}")
                
                # Clean empty directories
                for root, dirs, files in os.walk(temp_dir, topdown=False):
                    for dir_name in dirs:
                        dir_path = os.path.join(root, dir_name)
                        try:
                            if not os.listdir(dir_path):  # Directory is empty
                                os.rmdir(dir_path)
                                logger.debug(f"Removed empty directory: {dir_path}")
                        except (OSError, PermissionError):
                            pass
                            
            except Exception as e:
                logger.warning(f"Error cleaning temp directory {temp_dir}: {e}")
        
        self.stats['files_cleaned'] += files_removed
        
        return {
            'files_removed': files_removed,
            'space_freed_bytes': space_freed,
            'space_freed_mb': round(space_freed / (1024 * 1024), 2)
        }

    def _should_cleanup_file(self, file_path: str) -> bool:
        """Check if a file should be cleaned up"""
        try:
            # Skip if file is too new (less than 1 hour old)
            file_age = time.time() - os.path.getmtime(file_path)
            if file_age < 3600:
                return False
            
            # Skip certain file types
            if file_path.endswith(('.log', '.pid', '.lock')):
                return file_age > 86400  # Only clean logs older than 1 day
            
            # Skip very large files (might be in use)
            if os.path.getsize(file_path) > 100 * 1024 * 1024:  # 100MB
                return file_age > 86400  # Only clean if older than 1 day
            
            # Clean temporary files older than 6 hours
            return file_age > 21600
            
        except (OSError, FileNotFoundError):
            return False

    async def _cleanup_old_downloads(self) -> Dict[str, Any]:
        """Clean up old download files"""
        from src.config.settings import get_settings
        settings = get_settings()
        
        downloads_dir = settings.downloads_directory
        if not os.path.exists(downloads_dir):
            return {'files_removed': 0, 'space_freed_mb': 0}
        
        files_removed = 0
        space_freed = 0
        cutoff_time = time.time() - (7 * 24 * 3600)  # 7 days ago
        
        try:
            for root, dirs, files in os.walk(downloads_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    
                    try:
                        # Remove files older than 7 days
                        if os.path.getmtime(file_path) < cutoff_time:
                            file_size = os.path.getsize(file_path)
                            os.remove(file_path)
                            files_removed += 1
                            space_freed += file_size
                            logger.debug(f"Removed old download: {file_path}")
                    except (OSError, PermissionError) as e:
                        logger.debug(f"Could not remove download {file_path}: {e}")
        
        except Exception as e:
            logger.warning(f"Error cleaning downloads directory: {e}")
        
        return {
            'files_removed': files_removed,
            'space_freed_mb': round(space_freed / (1024 * 1024), 2)
        }

    async def _cleanup_memory(self) -> Dict[str, Any]:
        """Clean up memory and force garbage collection"""
        # Get memory usage before cleanup
        process = psutil.Process()
        memory_before = process.memory_info().rss
        
        # Force garbage collection
        collected_objects = []
        for generation in range(3):
            collected = gc.collect(generation)
            collected_objects.append(collected)
        
        # Get memory usage after cleanup
        memory_after = process.memory_info().rss
        memory_freed = memory_before - memory_after
        memory_freed_mb = memory_freed / (1024 * 1024)
        
        self.stats['memory_freed_mb'] += memory_freed_mb
        
        return {
            'memory_before_mb': round(memory_before / (1024 * 1024), 2),
            'memory_after_mb': round(memory_after / (1024 * 1024), 2),
            'memory_freed_mb': round(memory_freed_mb, 2),
            'objects_collected': sum(collected_objects),
            'gc_collections': collected_objects
        }

    async def _cleanup_log_files(self) -> Dict[str, Any]:
        """Clean up old log files"""
        from src.config.settings import get_settings
        settings = get_settings()
        
        logs_dir = settings.logs_directory
        if not os.path.exists(logs_dir):
            return {'files_removed': 0, 'space_freed_mb': 0}
        
        files_removed = 0
        space_freed = 0
        cutoff_time = time.time() - (30 * 24 * 3600)  # 30 days ago
        
        try:
            for root, dirs, files in os.walk(logs_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    
                    try:
                        # Remove log files older than 30 days
                        if file.endswith('.log') and os.path.getmtime(file_path) < cutoff_time:
                            file_size = os.path.getsize(file_path)
                            os.remove(file_path)
                            files_removed += 1
                            space_freed += file_size
                            logger.debug(f"Removed old log file: {file_path}")
                    except (OSError, PermissionError) as e:
                        logger.debug(f"Could not remove log file {file_path}: {e}")
        
        except Exception as e:
            logger.warning(f"Error cleaning logs directory: {e}")
        
        return {
            'files_removed': files_removed,
            'space_freed_mb': round(space_freed / (1024 * 1024), 2)
        }

    async def _cleanup_orphaned_processes(self) -> Dict[str, Any]:
        """Clean up orphaned browser processes"""
        orphaned_count = 0
        
        try:
            # Look for Chrome/Chromium processes that might be orphaned
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
                try:
                    if proc.info['name'] and 'chrome' in proc.info['name'].lower():
                        # Check if process is old (older than 2 hours)
                        process_age = time.time() - proc.info['create_time']
                        if process_age > 7200:  # 2 hours
                            # Check if it's a potential orphan (no parent or parent is init)
                            try:
                                parent = proc.parent()
                                if parent is None or parent.pid == 1:
                                    logger.warning(f"Terminating orphaned browser process: PID {proc.info['pid']}")
                                    proc.terminate()
                                    orphaned_count += 1
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        
        except Exception as e:
            logger.warning(f"Error cleaning orphaned processes: {e}")
        
        return {
            'orphaned_processes_terminated': orphaned_count
        }

    def get_disk_usage_info(self) -> Dict[str, Any]:
        """Get disk usage information"""
        from src.config.settings import get_settings
        settings = get_settings()
        
        disk_info = {}
        
        # Check main disk
        try:
            disk_usage = shutil.disk_usage('/')
            disk_info['root'] = {
                'total_gb': round(disk_usage.total / (1024**3), 2),
                'used_gb': round(disk_usage.used / (1024**3), 2),
                'free_gb': round(disk_usage.free / (1024**3), 2),
                'used_percent': round((disk_usage.used / disk_usage.total) * 100, 1)
            }
        except Exception as e:
            logger.warning(f"Could not get root disk usage: {e}")
        
        # Check temp directory
        try:
            temp_size = self._get_directory_size(settings.tmp_directory)
            disk_info['temp'] = {
                'size_mb': round(temp_size / (1024**2), 2),
                'path': settings.tmp_directory
            }
        except Exception as e:
            logger.warning(f"Could not get temp directory size: {e}")
        
        # Check logs directory
        try:
            logs_size = self._get_directory_size(settings.logs_directory)
            disk_info['logs'] = {
                'size_mb': round(logs_size / (1024**2), 2),
                'path': settings.logs_directory
            }
        except Exception as e:
            logger.warning(f"Could not get logs directory size: {e}")
        
        return disk_info

    def _get_directory_size(self, path: str) -> int:
        """Get total size of a directory in bytes"""
        total_size = 0
        if os.path.exists(path):
            for dirpath, dirnames, filenames in os.walk(path):
                for filename in filenames:
                    filepath = os.path.join(dirpath, filename)
                    try:
                        total_size += os.path.getsize(filepath)
                    except (OSError, FileNotFoundError):
                        pass
        return total_size

    def get_memory_info(self) -> Dict[str, Any]:
        """Get memory usage information"""
        process = psutil.Process()
        system_memory = psutil.virtual_memory()
        
        return {
            'system': {
                'total_gb': round(system_memory.total / (1024**3), 2),
                'available_gb': round(system_memory.available / (1024**3), 2),
                'used_percent': system_memory.percent,
                'cached_gb': round(getattr(system_memory, 'cached', 0) / (1024**3), 2),
                'buffers_gb': round(getattr(system_memory, 'buffers', 0) / (1024**3), 2)
            },
            'process': {
                'rss_mb': round(process.memory_info().rss / (1024**2), 2),
                'vms_mb': round(process.memory_info().vms / (1024**2), 2),
                'percent': round(process.memory_percent(), 2),
                'num_fds': getattr(process, 'num_fds', lambda: 0)()
            }
        }

    def get_cleanup_statistics(self) -> Dict[str, Any]:
        """Get cleanup statistics"""
        return {
            'stats': self.stats.copy(),
            'registered_tasks': list(self.cleanup_tasks.keys()),
            'task_intervals': self.cleanup_intervals.copy(),
            'is_running': self.is_running,
            'disk_usage': self.get_disk_usage_info(),
            'memory_usage': self.get_memory_info()
        }

    async def emergency_cleanup(self) -> Dict[str, Any]:
        """Perform emergency cleanup when resources are critically low"""
        logger.warning("Performing emergency cleanup")
        
        results = {}
        
        # Force immediate cleanup of all tasks
        for task_name in self.cleanup_tasks:
            results[task_name] = await self._run_cleanup_task(task_name)
        
        # Additional aggressive cleanup
        
        # Force multiple garbage collections
        for i in range(5):
            gc.collect()
        
        # Try to free up more temp space
        temp_cleanup = await self._aggressive_temp_cleanup()
        results['aggressive_temp_cleanup'] = temp_cleanup
        
        # Log the emergency cleanup
        logger.warning(f"Emergency cleanup completed. Results: {results}")
        
        return results

    async def _aggressive_temp_cleanup(self) -> Dict[str, Any]:
        """More aggressive temporary file cleanup"""
        from src.config.settings import get_settings
        settings = get_settings()
        
        files_removed = 0
        space_freed = 0
        
        # Clean files older than 1 hour instead of 6 hours
        cutoff_time = time.time() - 3600  # 1 hour ago
        
        temp_dirs = [
            settings.tmp_directory,
            settings.downloads_directory,
            './tmp',
            '/tmp'
        ]
        
        for temp_dir in temp_dirs:
            if not os.path.exists(temp_dir):
                continue
            
            try:
                for root, dirs, files in os.walk(temp_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        
                        try:
                            if os.path.getmtime(file_path) < cutoff_time:
                                file_size = os.path.getsize(file_path)
                                os.remove(file_path)
                                files_removed += 1
                                space_freed += file_size
                        except (OSError, PermissionError, FileNotFoundError):
                            pass
            except Exception as e:
                logger.debug(f"Error in aggressive cleanup of {temp_dir}: {e}")
        
        return {
            'files_removed': files_removed,
            'space_freed_mb': round(space_freed / (1024 * 1024), 2)
        }


class SessionResourceCleaner:
    """Specialized cleaner for session-specific resources"""
    
    def __init__(self):
        self.session_cleanups: Dict[str, List[Callable]] = {}
        self.cleanup_stats = {
            'sessions_cleaned': 0,
            'resources_freed': 0
        }

    def register_session_cleanup(self, session_id: str, cleanup_func: Callable):
        """Register a cleanup function for a specific session"""
        if session_id not in self.session_cleanups:
            self.session_cleanups[session_id] = []
        self.session_cleanups[session_id].append(cleanup_func)

    async def cleanup_session(self, session_id: str) -> Dict[str, Any]:
        """Clean up all resources for a specific session"""
        if session_id not in self.session_cleanups:
            return {'status': 'no_cleanup_needed'}
        
        results = []
        errors = []
        
        for cleanup_func in self.session_cleanups[session_id]:
            try:
                if asyncio.iscoroutinefunction(cleanup_func):
                    result = await cleanup_func()
                else:
                    result = cleanup_func()
                results.append(result)
            except Exception as e:
                errors.append(str(e))
                logger.error(f"Error in session cleanup for {session_id}: {e}")
        
        # Remove cleanup functions after use
        del self.session_cleanups[session_id]
        self.cleanup_stats['sessions_cleaned'] += 1
        
        return {
            'session_id': session_id,
            'cleanup_results': results,
            'errors': errors,
            'cleanup_count': len(results)
        }

    async def cleanup_all_sessions(self) -> Dict[str, Any]:
        """Clean up all registered sessions"""
        session_ids = list(self.session_cleanups.keys())
        results = {}
        
        for session_id in session_ids:
            results[session_id] = await self.cleanup_session(session_id)
        
        return {
            'total_sessions': len(session_ids),
            'results': results
        }


# Global instances
_resource_cleaner: Optional[ResourceCleaner] = None
_session_cleaner: Optional[SessionResourceCleaner] = None


def get_resource_cleaner() -> ResourceCleaner:
    """Get the global resource cleaner instance"""
    global _resource_cleaner
    if _resource_cleaner is None:
        _resource_cleaner = ResourceCleaner()
    return _resource_cleaner


def get_session_cleaner() -> SessionResourceCleaner:
    """Get the global session resource cleaner instance"""
    global _session_cleaner
    if _session_cleaner is None:
        _session_cleaner = SessionResourceCleaner()
    return _session_cleaner


async def init_cleanup_service() -> ResourceCleaner:
    """Initialize the cleanup service"""
    cleaner = get_resource_cleaner()
    await cleaner.start()
    return cleaner


async def shutdown_cleanup_service():
    """Shutdown the cleanup service"""
    global _resource_cleaner
    if _resource_cleaner is not None:
        await _resource_cleaner.stop()
        _resource_cleaner = None


# Utility functions for common cleanup operations
async def cleanup_temp_files(max_age_hours: int = 6) -> Dict[str, Any]:
    """Clean up temporary files older than specified hours"""
    cleaner = get_resource_cleaner()
    return await cleaner._cleanup_temp_files()


async def force_memory_cleanup() -> Dict[str, Any]:
    """Force memory cleanup and garbage collection"""
    cleaner = get_resource_cleaner()
    return await cleaner._cleanup_memory()


async def emergency_disk_cleanup() -> Dict[str, Any]:
    """Emergency cleanup when disk space is critically low"""
    cleaner = get_resource_cleaner()
    return await cleaner.emergency_cleanup()


def register_session_cleanup(session_id: str, cleanup_func: Callable):
    """Register a cleanup function for a session"""
    cleaner = get_session_cleaner()
    cleaner.register_session_cleanup(session_id, cleanup_func)


async def cleanup_session_resources(session_id: str) -> Dict[str, Any]:
    """Clean up all resources for a session"""
    cleaner = get_session_cleaner()
    return await cleaner.cleanup_session(session_id)


# Context manager for automatic cleanup
class CleanupContext:
    """Context manager that ensures cleanup on exit"""
    
    def __init__(self, cleanup_func: Callable):
        self.cleanup_func = cleanup_func
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if asyncio.iscoroutinefunction(self.cleanup_func):
                await self.cleanup_func()
            else:
                self.cleanup_func()
        except Exception as e:
            logger.error(f"Error in cleanup context: {e}")


def cleanup_on_exit(cleanup_func: Callable):
    """Decorator to ensure cleanup function is called on exit"""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            finally:
                try:
                    if asyncio.iscoroutinefunction(cleanup_func):
                        await cleanup_func()
                    else:
                        cleanup_func()
                except Exception as e:
                    logger.error(f"Error in cleanup decorator: {e}")
        return wrapper
    return decorator


# Export main components
__all__ = [
    'ResourceCleaner',
    'SessionResourceCleaner',
    'CleanupContext',
    'get_resource_cleaner',
    'get_session_cleaner',
    'init_cleanup_service',
    'shutdown_cleanup_service',
    'cleanup_temp_files',
    'force_memory_cleanup',
    'emergency_disk_cleanup',
    'register_session_cleanup',
    'cleanup_session_resources',
    'cleanup_on_exit'
]