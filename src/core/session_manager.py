import asyncio
import logging
import time
import uuid
from typing import Dict, Optional, Set
import weakref
from datetime import datetime, timedelta
from dataclasses import dataclass
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

@dataclass
class SessionConfig:
    """Configuration for session management"""
    max_sessions: int = 1000
    session_timeout_minutes: int = 30
    cleanup_interval_seconds: int = 60
    max_sessions_per_ip: int = 5
    enable_session_persistence: bool = False  # For Redis backup in future


class SessionManager:
    """
    Centralized session management for multi-user support.
    Handles session lifecycle, cleanup, and resource management.
    """
    
    def __init__(self, config: SessionConfig = None):
        self.config = config or SessionConfig()
        
        # Core session storage
        self._sessions: Dict[str, 'UserSession'] = {}
        self._session_ips: Dict[str, Set[str]] = {}  # IP -> set of session_ids
        self._ip_sessions: Dict[str, str] = {}  # session_id -> IP
        
        # Session metadata
        self._session_last_activity: Dict[str, datetime] = {}
        self._session_creation_time: Dict[str, datetime] = {}
        
        # Cleanup and monitoring
        self._cleanup_task: Optional[asyncio.Task] = None
        self._is_running = False
        
        # Weak references to avoid circular dependencies
        self._session_refs: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
        
        # Statistics
        self._stats = {
            'total_sessions_created': 0,
            'total_sessions_cleaned': 0,
            'current_active_sessions': 0,
            'max_concurrent_sessions': 0
        }
        
        logger.info(f"SessionManager initialized with config: {self.config}")

    async def start(self):
        """Start the session manager and background tasks"""
        if self._is_running:
            logger.warning("SessionManager is already running")
            return
            
        self._is_running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("SessionManager started with cleanup loop")

    async def stop(self):
        """Stop the session manager and cleanup all sessions"""
        if not self._is_running:
            return
            
        self._is_running = False
        
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        
        # Cleanup all active sessions
        session_ids = list(self._sessions.keys())
        for session_id in session_ids:
            await self._cleanup_session(session_id, reason="manager_shutdown")
        
        logger.info("SessionManager stopped and all sessions cleaned up")

    async def create_session(self, client_ip: str = None) -> str:
        """
        Create a new user session.
        
        Args:
            client_ip: Client IP address for rate limiting
            
        Returns:
            session_id: Unique session identifier
            
        Raises:
            ValueError: If session limits are exceeded
        """
        # Check global session limit
        if len(self._sessions) >= self.config.max_sessions:
            raise ValueError(f"Maximum sessions ({self.config.max_sessions}) exceeded")
        
        # Check per-IP session limit
        if client_ip:
            ip_session_count = len(self._session_ips.get(client_ip, set()))
            if ip_session_count >= self.config.max_sessions_per_ip:
                raise ValueError(f"Maximum sessions per IP ({self.config.max_sessions_per_ip}) exceeded")
        
        # Generate unique session ID
        session_id = str(uuid.uuid4())
        now = datetime.now()
        
        # Import here to avoid circular imports
        from .user_session import UserSession
        
        # Create new session
        session = UserSession(session_id=session_id, client_ip=client_ip)
        
        # Store session data
        self._sessions[session_id] = session
        self._session_last_activity[session_id] = now
        self._session_creation_time[session_id] = now
        self._session_refs[session_id] = session
        
        # Track IP associations
        if client_ip:
            if client_ip not in self._session_ips:
                self._session_ips[client_ip] = set()
            self._session_ips[client_ip].add(session_id)
            self._ip_sessions[session_id] = client_ip
        
        # Update statistics
        self._stats['total_sessions_created'] += 1
        self._stats['current_active_sessions'] = len(self._sessions)
        if self._stats['current_active_sessions'] > self._stats['max_concurrent_sessions']:
            self._stats['max_concurrent_sessions'] = self._stats['current_active_sessions']
        
        logger.info(f"Created session {session_id} for IP {client_ip}. "
                   f"Active sessions: {len(self._sessions)}")
        
        return session_id

    async def get_session(self, session_id: str) -> Optional['UserSession']:
        """
        Get an existing session and update its last activity.
        
        Args:
            session_id: Session identifier
            
        Returns:
            UserSession object or None if not found
        """
        if session_id not in self._sessions:
            return None
        
        # Update last activity
        self._session_last_activity[session_id] = datetime.now()
        
        return self._sessions[session_id]

    async def has_session(self, session_id: str) -> bool:
        """Check if a session exists"""
        return session_id in self._sessions

    async def remove_session(self, session_id: str, reason: str = "manual_removal") -> bool:
        """
        Remove a session manually.
        
        Args:
            session_id: Session to remove
            reason: Reason for removal (for logging)
            
        Returns:
            bool: True if session was removed, False if not found
        """
        if session_id not in self._sessions:
            return False
        
        await self._cleanup_session(session_id, reason=reason)
        return True

    async def update_activity(self, session_id: str):
        """Update the last activity time for a session"""
        if session_id in self._sessions:
            self._session_last_activity[session_id] = datetime.now()

    async def get_session_count(self) -> int:
        """Get current number of active sessions"""
        return len(self._sessions)

    async def get_sessions_for_ip(self, client_ip: str) -> Set[str]:
        """Get all session IDs for a given IP address"""
        return self._session_ips.get(client_ip, set()).copy()

    async def get_statistics(self) -> Dict:
        """Get session manager statistics"""
        return {
            **self._stats,
            'current_active_sessions': len(self._sessions),
            'sessions_by_ip': {ip: len(sessions) for ip, sessions in self._session_ips.items()},
            'uptime_seconds': (datetime.now() - datetime.now()).total_seconds() if self._is_running else 0
        }

    async def _cleanup_loop(self):
        """Background task to cleanup expired sessions"""
        logger.info("Starting session cleanup loop")
        
        while self._is_running:
            try:
                await self._cleanup_expired_sessions()
                await asyncio.sleep(self.config.cleanup_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}", exc_info=True)
                await asyncio.sleep(self.config.cleanup_interval_seconds)
        
        logger.info("Session cleanup loop stopped")

    async def _cleanup_expired_sessions(self):
        """Clean up sessions that have exceeded the timeout"""
        now = datetime.now()
        timeout_delta = timedelta(minutes=self.config.session_timeout_minutes)
        expired_sessions = []
        
        for session_id, last_activity in self._session_last_activity.items():
            if now - last_activity > timeout_delta:
                expired_sessions.append(session_id)
        
        if expired_sessions:
            logger.info(f"Cleaning up {len(expired_sessions)} expired sessions")
            
            for session_id in expired_sessions:
                await self._cleanup_session(session_id, reason="timeout")

    async def _cleanup_session(self, session_id: str, reason: str = "unknown"):
        """
        Internal method to cleanup a single session.
        
        Args:
            session_id: Session to cleanup
            reason: Reason for cleanup (for logging)
        """
        try:
            # Get session before removal
            session = self._sessions.get(session_id)
            
            if session:
                # Cleanup session resources
                await session.cleanup()
            
            # Remove from all tracking dictionaries
            self._sessions.pop(session_id, None)
            self._session_last_activity.pop(session_id, None)
            self._session_creation_time.pop(session_id, None)
            
            # Remove IP tracking
            client_ip = self._ip_sessions.pop(session_id, None)
            if client_ip and client_ip in self._session_ips:
                self._session_ips[client_ip].discard(session_id)
                if not self._session_ips[client_ip]:
                    del self._session_ips[client_ip]
            
            # Update statistics
            self._stats['total_sessions_cleaned'] += 1
            self._stats['current_active_sessions'] = len(self._sessions)
            
            logger.info(f"Cleaned up session {session_id} (reason: {reason}). "
                       f"Active sessions: {len(self._sessions)}")
            
        except Exception as e:
            logger.error(f"Error cleaning up session {session_id}: {e}", exc_info=True)

    @asynccontextmanager
    async def session_context(self, client_ip: str = None):
        """
        Context manager for automatic session lifecycle management.
        
        Usage:
            async with session_manager.session_context() as session_id:
                session = await session_manager.get_session(session_id)
                # Use session...
        """
        session_id = None
        try:
            session_id = await self.create_session(client_ip)
            yield session_id
        finally:
            if session_id:
                await self.remove_session(session_id, reason="context_exit")


# Global instance (to be initialized in main app)
session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """Get the global session manager instance"""
    global session_manager
    if session_manager is None:
        raise RuntimeError("SessionManager not initialized. Call init_session_manager() first.")
    return session_manager


async def init_session_manager(config: SessionConfig = None) -> SessionManager:
    """Initialize the global session manager"""
    global session_manager
    if session_manager is not None:
        logger.warning("SessionManager already initialized")
        return session_manager
    
    session_manager = SessionManager(config)
    await session_manager.start()
    return session_manager


async def shutdown_session_manager():
    """Shutdown the global session manager"""
    global session_manager
    if session_manager is not None:
        await session_manager.stop()
        session_manager = None