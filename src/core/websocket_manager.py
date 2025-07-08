import asyncio
import json
import logging
import time
import weakref
from typing import Dict, List, Optional, Set, Any, Callable
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class ConnectionState(Enum):
    """WebSocket connection states"""
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class MessageType(Enum):
    """Standard message types"""
    # Agent related
    AGENT_STEP = "agent_step"
    AGENT_STATUS = "agent_status"
    AGENT_COMPLETED = "agent_completed"
    AGENT_HELP_REQUEST = "agent_help_request"
    
    # Chat and interaction
    CHAT_MESSAGE = "chat_message"
    USER_RESPONSE = "user_response"
    
    # Browser streaming
    BROWSER_STREAM = "browser_stream"
    BROWSER_STATUS = "browser_status"
    
    # System messages
    SESSION_STATUS = "session_status"
    ERROR = "error"
    PING = "ping"
    PONG = "pong"


@dataclass
class ConnectionInfo:
    """Information about a WebSocket connection"""
    session_id: str
    websocket: WebSocket
    client_ip: str
    connected_at: datetime
    last_activity: datetime
    state: ConnectionState
    message_count: int = 0
    error_count: int = 0
    user_agent: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'session_id': self.session_id,
            'client_ip': self.client_ip,
            'connected_at': self.connected_at.isoformat(),
            'last_activity': self.last_activity.isoformat(),
            'state': self.state.value,
            'message_count': self.message_count,
            'error_count': self.error_count,
            'user_agent': self.user_agent,
            'duration_seconds': (datetime.now() - self.connected_at).total_seconds()
        }


@dataclass
class WebSocketConfig:
    """Configuration for WebSocket management"""
    max_connections: int = 1000
    max_connections_per_ip: int = 5
    ping_interval_seconds: int = 30
    connection_timeout_seconds: int = 60
    max_message_size: int = 1024 * 1024  # 1MB
    rate_limit_messages_per_minute: int = 100
    enable_compression: bool = True
    heartbeat_timeout_seconds: int = 90


class WebSocketManager:
    """
    Manages WebSocket connections for multi-user sessions.
    Provides connection lifecycle, message routing, and broadcasting capabilities.
    """
    
    def __init__(self, config: WebSocketConfig = None):
        self.config = config or WebSocketConfig()
        
        # Connection storage
        self._connections: Dict[str, ConnectionInfo] = {}  # session_id -> connection_info
        self._ip_connections: Dict[str, Set[str]] = {}  # ip -> set of session_ids
        self._websockets: Dict[WebSocket, str] = {}  # websocket -> session_id
        
        # Message handlers
        self._message_handlers: Dict[str, Callable] = {}
        self._broadcast_handlers: List[Callable] = []
        
        # Background tasks
        self._ping_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._is_running = False
        
        # Rate limiting
        self._rate_limits: Dict[str, List[float]] = {}  # session_id -> list of timestamps
        
        # Statistics
        self._stats = {
            'total_connections': 0,
            'total_disconnections': 0,
            'total_messages_sent': 0,
            'total_messages_received': 0,
            'total_errors': 0,
            'current_connections': 0,
            'peak_connections': 0
        }
        
        # Weak references for automatic cleanup
        self._weak_refs: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
        
        logger.info(f"WebSocketManager initialized with config: {self.config}")

    async def start(self):
        """Start the WebSocket manager and background tasks"""
        if self._is_running:
            logger.warning("WebSocketManager is already running")
            return
        
        self._is_running = True
        
        # Start background tasks
        self._ping_task = asyncio.create_task(self._ping_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        
        logger.info("WebSocketManager started with background tasks")

    async def stop(self):
        """Stop the WebSocket manager and close all connections"""
        if not self._is_running:
            return
        
        self._is_running = False
        
        # Cancel background tasks
        for task in [self._ping_task, self._cleanup_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Close all connections
        await self._close_all_connections()
        
        logger.info("WebSocketManager stopped and all connections closed")

    async def connect(self, websocket: WebSocket, session_id: str, client_ip: str, 
                     user_agent: str = None) -> bool:
        """
        Register a new WebSocket connection.
        
        Args:
            websocket: FastAPI WebSocket instance
            session_id: Session identifier
            client_ip: Client IP address
            user_agent: User agent string
            
        Returns:
            bool: True if connection successful, False otherwise
        """
        # Check connection limits
        if len(self._connections) >= self.config.max_connections:
            logger.warning(f"Maximum connections ({self.config.max_connections}) exceeded")
            return False
        
        # Check per-IP limits
        ip_connection_count = len(self._ip_connections.get(client_ip, set()))
        if ip_connection_count >= self.config.max_connections_per_ip:
            logger.warning(f"Maximum connections per IP ({self.config.max_connections_per_ip}) exceeded for {client_ip}")
            return False
        
        # Check if session already has a connection
        if session_id in self._connections:
            logger.warning(f"Session {session_id} already has an active connection")
            # Close the existing connection
            await self.disconnect(session_id, reason="duplicate_connection")
        
        try:
            # Accept the WebSocket connection
            await websocket.accept()
            
            # Create connection info
            connection_info = ConnectionInfo(
                session_id=session_id,
                websocket=websocket,
                client_ip=client_ip,
                connected_at=datetime.now(),
                last_activity=datetime.now(),
                state=ConnectionState.CONNECTED,
                user_agent=user_agent
            )
            
            # Store connection data
            self._connections[session_id] = connection_info
            self._websockets[websocket] = session_id
            self._weak_refs[session_id] = connection_info
            
            # Track IP connections
            if client_ip not in self._ip_connections:
                self._ip_connections[client_ip] = set()
            self._ip_connections[client_ip].add(session_id)
            
            # Update statistics
            self._stats['total_connections'] += 1
            self._stats['current_connections'] = len(self._connections)
            if self._stats['current_connections'] > self._stats['peak_connections']:
                self._stats['peak_connections'] = self._stats['current_connections']
            
            # Send welcome message
            await self._send_to_session(session_id, {
                "type": MessageType.SESSION_STATUS.value,
                "data": {
                    "status": "connected",
                    "session_id": session_id,
                    "server_time": datetime.now().isoformat()
                }
            })
            
            logger.info(f"WebSocket connected for session {session_id} from {client_ip}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect WebSocket for session {session_id}: {e}", exc_info=True)
            return False

    async def disconnect(self, session_id: str, reason: str = "unknown") -> bool:
        """
        Disconnect a WebSocket connection.
        
        Args:
            session_id: Session to disconnect
            reason: Reason for disconnection
            
        Returns:
            bool: True if disconnected successfully
        """
        if session_id not in self._connections:
            return False
        
        connection_info = self._connections[session_id]
        
        try:
            # Close the WebSocket
            if connection_info.websocket:
                await connection_info.websocket.close()
            
            # Remove from tracking
            self._remove_connection(session_id, reason)
            
            logger.info(f"WebSocket disconnected for session {session_id} (reason: {reason})")
            return True
            
        except Exception as e:
            logger.error(f"Error disconnecting session {session_id}: {e}", exc_info=True)
            self._remove_connection(session_id, f"error: {e}")
            return False

    async def send_to_session(self, session_id: str, message: Dict[str, Any]) -> bool:
        """
        Send a message to a specific session.
        
        Args:
            session_id: Target session
            message: Message to send
            
        Returns:
            bool: True if sent successfully
        """
        return await self._send_to_session(session_id, message)

    async def broadcast_to_all(self, message: Dict[str, Any], exclude_sessions: Set[str] = None) -> int:
        """
        Broadcast a message to all connected sessions.
        
        Args:
            message: Message to broadcast
            exclude_sessions: Sessions to exclude from broadcast
            
        Returns:
            int: Number of sessions that received the message
        """
        exclude_sessions = exclude_sessions or set()
        sent_count = 0
        
        for session_id in list(self._connections.keys()):
            if session_id not in exclude_sessions:
                if await self._send_to_session(session_id, message):
                    sent_count += 1
        
        return sent_count

    async def broadcast_to_sessions(self, session_ids: List[str], message: Dict[str, Any]) -> int:
        """
        Broadcast a message to specific sessions.
        
        Args:
            session_ids: Target sessions
            message: Message to broadcast
            
        Returns:
            int: Number of sessions that received the message
        """
        sent_count = 0
        
        for session_id in session_ids:
            if await self._send_to_session(session_id, message):
                sent_count += 1
        
        return sent_count

    async def handle_websocket_communication(self, session_id: str):
        """
        Handle incoming messages for a WebSocket connection.
        Should be called in a background task after connection is established.
        
        Args:
            session_id: Session to handle communication for
        """
        if session_id not in self._connections:
            logger.error(f"No connection found for session {session_id}")
            return
        
        connection_info = self._connections[session_id]
        websocket = connection_info.websocket
        
        try:
            while self._is_running and connection_info.state == ConnectionState.CONNECTED:
                try:
                    # Receive message with timeout
                    message = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=self.config.connection_timeout_seconds
                    )
                    
                    # Update activity
                    connection_info.last_activity = datetime.now()
                    connection_info.message_count += 1
                    self._stats['total_messages_received'] += 1
                    
                    # Check rate limiting
                    if not await self._check_rate_limit(session_id):
                        await self._send_to_session(session_id, {
                            "type": MessageType.ERROR.value,
                            "data": {"error": "Rate limit exceeded"}
                        })
                        continue
                    
                    # Process the message
                    await self._process_incoming_message(session_id, message)
                    
                except asyncio.TimeoutError:
                    # Connection timeout
                    logger.warning(f"Connection timeout for session {session_id}")
                    break
                    
                except WebSocketDisconnect:
                    # Client disconnected
                    logger.info(f"Client disconnected for session {session_id}")
                    break
                    
                except Exception as e:
                    # Handle other errors
                    logger.error(f"Error handling message for session {session_id}: {e}", exc_info=True)
                    connection_info.error_count += 1
                    self._stats['total_errors'] += 1
                    
                    if connection_info.error_count > 10:  # Too many errors
                        logger.warning(f"Too many errors for session {session_id}, disconnecting")
                        break
        
        finally:
            # Ensure connection is cleaned up
            await self.disconnect(session_id, reason="communication_ended")

    def register_message_handler(self, message_type: str, handler: Callable):
        """
        Register a handler for incoming messages of a specific type.
        
        Args:
            message_type: Type of message to handle
            handler: Async function to handle the message
        """
        self._message_handlers[message_type] = handler
        logger.debug(f"Registered message handler for type: {message_type}")

    def register_broadcast_handler(self, handler: Callable):
        """
        Register a handler that will be called for all broadcast messages.
        
        Args:
            handler: Async function to handle broadcast messages
        """
        self._broadcast_handlers.append(handler)
        logger.debug("Registered broadcast handler")

    async def get_connection_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get information about a connection"""
        if session_id not in self._connections:
            return None
        
        return self._connections[session_id].to_dict()

    async def get_all_connections(self) -> List[Dict[str, Any]]:
        """Get information about all connections"""
        return [info.to_dict() for info in self._connections.values()]

    async def get_statistics(self) -> Dict[str, Any]:
        """Get WebSocket manager statistics"""
        return {
            **self._stats,
            'current_connections': len(self._connections),
            'connections_by_ip': {ip: len(sessions) for ip, sessions in self._ip_connections.items()},
            'rate_limited_sessions': len(self._rate_limits),
            'manager_uptime_seconds': (datetime.now() - datetime.now()).total_seconds() if self._is_running else 0
        }

    async def is_connected(self, session_id: str) -> bool:
        """Check if a session has an active WebSocket connection"""
        return (session_id in self._connections and 
                self._connections[session_id].state == ConnectionState.CONNECTED)

    # Private methods
    
    async def _send_to_session(self, session_id: str, message: Dict[str, Any]) -> bool:
        """Internal method to send message to a session"""
        if session_id not in self._connections:
            return False
        
        connection_info = self._connections[session_id]
        
        if connection_info.state != ConnectionState.CONNECTED:
            return False
        
        try:
            # Add metadata to message
            message_with_meta = {
                **message,
                "timestamp": datetime.now().isoformat(),
                "session_id": session_id
            }
            
            # Send message
            await connection_info.websocket.send_text(json.dumps(message_with_meta))
            
            # Update statistics
            self._stats['total_messages_sent'] += 1
            connection_info.last_activity = datetime.now()
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to send message to session {session_id}: {e}", exc_info=True)
            # Mark connection as errored
            connection_info.state = ConnectionState.ERROR
            connection_info.error_count += 1
            
            # Disconnect if too many errors
            if connection_info.error_count > 5:
                await self.disconnect(session_id, reason="send_errors")
            
            return False

    async def _process_incoming_message(self, session_id: str, raw_message: str):
        """Process an incoming message from a client"""
        try:
            message = json.loads(raw_message)
            message_type = message.get("type")
            
            if not message_type:
                logger.warning(f"Message without type from session {session_id}")
                return
            
            # Handle special message types
            if message_type == MessageType.PING.value:
                await self._handle_ping(session_id, message)
                return
            elif message_type == MessageType.USER_RESPONSE.value:
                await self._handle_user_response(session_id, message)
                return
            
            # Check if we have a registered handler
            if message_type in self._message_handlers:
                handler = self._message_handlers[message_type]
                await handler(session_id, message)
            else:
                logger.warning(f"No handler for message type '{message_type}' from session {session_id}")
                
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON from session {session_id}: {e}")
            await self._send_to_session(session_id, {
                "type": MessageType.ERROR.value,
                "data": {"error": "Invalid JSON format"}
            })
        except Exception as e:
            logger.error(f"Error processing message from session {session_id}: {e}", exc_info=True)
            await self._send_to_session(session_id, {
                "type": MessageType.ERROR.value,
                "data": {"error": "Message processing failed"}
            })

    async def _handle_ping(self, session_id: str, message: Dict[str, Any]):
        """Handle ping message"""
        await self._send_to_session(session_id, {
            "type": MessageType.PONG.value,
            "data": {"timestamp": datetime.now().isoformat()}
        })

    async def _handle_user_response(self, session_id: str, message: Dict[str, Any]):
        """Handle user response message (for agent help requests)"""
        # This should be handled by the session manager
        # We'll forward it to the session
        try:
            from .session_manager import get_session_manager
            session_manager = get_session_manager()
            session = await session_manager.get_session(session_id)
            
            if session:
                response_text = message.get("data", {}).get("response", "")
                await session.submit_help_response(response_text)
            
        except Exception as e:
            logger.error(f"Error handling user response for session {session_id}: {e}", exc_info=True)

    async def _check_rate_limit(self, session_id: str) -> bool:
        """Check if session is within rate limits"""
        if session_id not in self._rate_limits:
            self._rate_limits[session_id] = []
        
        now = time.time()
        window_start = now - 60  # 1 minute window
        
        # Remove old timestamps
        self._rate_limits[session_id] = [
            ts for ts in self._rate_limits[session_id] if ts > window_start
        ]
        
        # Check if under limit
        if len(self._rate_limits[session_id]) >= self.config.rate_limit_messages_per_minute:
            return False
        
        # Add current timestamp
        self._rate_limits[session_id].append(now)
        return True

    def _remove_connection(self, session_id: str, reason: str):
        """Remove connection from all tracking structures"""
        if session_id not in self._connections:
            return
        
        connection_info = self._connections[session_id]
        
        # Remove from connections
        del self._connections[session_id]
        
        # Remove from websocket mapping
        if connection_info.websocket in self._websockets:
            del self._websockets[connection_info.websocket]
        
        # Remove from IP tracking
        if connection_info.client_ip in self._ip_connections:
            self._ip_connections[connection_info.client_ip].discard(session_id)
            if not self._ip_connections[connection_info.client_ip]:
                del self._ip_connections[connection_info.client_ip]
        
        # Remove rate limiting data
        self._rate_limits.pop(session_id, None)
        
        # Update statistics
        self._stats['total_disconnections'] += 1
        self._stats['current_connections'] = len(self._connections)
        
        logger.debug(f"Removed connection tracking for session {session_id} (reason: {reason})")

    async def _ping_loop(self):
        """Background task to send ping messages"""
        logger.info("Starting WebSocket ping loop")
        
        while self._is_running:
            try:
                await self._send_ping_to_all()
                await asyncio.sleep(self.config.ping_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in ping loop: {e}", exc_info=True)
                await asyncio.sleep(self.config.ping_interval_seconds)

    async def _cleanup_loop(self):
        """Background task to cleanup stale connections"""
        logger.info("Starting WebSocket cleanup loop")
        
        while self._is_running:
            try:
                await self._cleanup_stale_connections()
                await asyncio.sleep(30)  # Check every 30 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}", exc_info=True)
                await asyncio.sleep(30)

    async def _send_ping_to_all(self):
        """Send ping to all connected sessions"""
        ping_message = {
            "type": MessageType.PING.value,
            "data": {"timestamp": datetime.now().isoformat()}
        }
        
        for session_id in list(self._connections.keys()):
            await self._send_to_session(session_id, ping_message)

    async def _cleanup_stale_connections(self):
        """Clean up connections that haven't been active"""
        now = datetime.now()
        stale_threshold = timedelta(seconds=self.config.heartbeat_timeout_seconds)
        
        stale_sessions = []
        
        for session_id, connection_info in self._connections.items():
            if now - connection_info.last_activity > stale_threshold:
                stale_sessions.append(session_id)
        
        for session_id in stale_sessions:
            await self.disconnect(session_id, reason="stale_connection")

    async def _close_all_connections(self):
        """Close all WebSocket connections"""
        session_ids = list(self._connections.keys())
        
        for session_id in session_ids:
            await self.disconnect(session_id, reason="manager_shutdown")
        
        # Clear all tracking data
        self._connections.clear()
        self._ip_connections.clear()
        self._websockets.clear()
        self._rate_limits.clear()


# Global instance (to be initialized in main app)
websocket_manager: Optional[WebSocketManager] = None


def get_websocket_manager() -> WebSocketManager:
    """Get the global WebSocket manager instance"""
    global websocket_manager
    if websocket_manager is None:
        raise RuntimeError("WebSocketManager not initialized. Call init_websocket_manager() first.")
    return websocket_manager


async def init_websocket_manager(config: WebSocketConfig = None) -> WebSocketManager:
    """Initialize the global WebSocket manager"""
    global websocket_manager
    if websocket_manager is not None:
        logger.warning("WebSocketManager already initialized")
        return websocket_manager
    
    websocket_manager = WebSocketManager(config)
    await websocket_manager.start()
    return websocket_manager


async def shutdown_websocket_manager():
    """Shutdown the global WebSocket manager"""
    global websocket_manager
    if websocket_manager is not None:
        await websocket_manager.stop()
        websocket_manager = None