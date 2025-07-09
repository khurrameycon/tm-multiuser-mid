import asyncio
import logging
import time
import uuid
from typing import Optional, Dict, Any, List
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class SessionState(Enum):
    """Session lifecycle states"""
    CREATED = "created"
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"
    CLEANUP = "cleanup"
    CLOSED = "closed"


class AgentState(Enum):
    """Agent execution states"""
    IDLE = "idle"
    INITIALIZING = "initializing"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class AgentTaskInfo:
    """Information about a running agent task"""
    task_id: str
    task_description: str
    started_at: datetime
    agent_state: AgentState
    current_step: int = 0
    max_steps: int = 100
    error_message: Optional[str] = None
    result: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'task_id': self.task_id,
            'task_description': self.task_description,
            'started_at': self.started_at.isoformat(),
            'agent_state': self.agent_state.value,
            'current_step': self.current_step,
            'max_steps': self.max_steps,
            'error_message': self.error_message,
            'result': self.result,
            'progress_percentage': (self.current_step / self.max_steps) * 100 if self.max_steps > 0 else 0
        }


class UserSession:
    """
    Represents an individual user session with isolated resources.
    Each session maintains its own browser context, agent instances, and state.
    """
    
    def __init__(self, session_id: str, client_ip: str = None):
        self.session_id = session_id
        self.client_ip = client_ip
        self.created_at = datetime.now()
        self.last_activity = datetime.now()
        
        # Session state
        self.state = SessionState.CREATED
        self.is_cleanup_started = False
        
        # Browser resources (isolated per session)
        self.browser = None  # CustomBrowser instance
        self.browser_context = None  # CustomBrowserContext instance
        self.controller = None  # CustomController instance
        
        # Agent management
        self.agent = None  # Current agent instance
        self.current_task: Optional[asyncio.Task] = None
        self.agent_task_info: Optional[AgentTaskInfo] = None
        
        # WebSocket connection
        self.websocket = None
        self.websocket_connected = False
        
        # Chat history and interaction
        self.chat_history: List[Dict[str, Any]] = []
        self.response_event: Optional[asyncio.Event] = None
        self.user_help_response: Optional[str] = None
        
        # Resource tracking
        self.memory_usage_mb = 0.0
        self.browser_tabs_count = 0
        self.active_connections = 0
        
        # Configuration (can be customized per session)
        self.max_browser_tabs = 10
        self.max_chat_history = 100
        self.enable_vision = True
        self.max_steps = 100
        
        # Statistics
        self.stats = {
            'tasks_completed': 0,
            'tasks_failed': 0,
            'total_steps_executed': 0,
            'total_messages_sent': 0,
            'total_messages_received': 0,
            'browser_navigations': 0
        }
        
        logger.info(f"Created UserSession {session_id} for IP {client_ip}")

    # In src/core/user_session.py, replace the existing initialize_browser function with this one.

    # In src/core/user_session.py, replace the existing initialize_browser function

    async def initialize_browser(self, browser_config: Dict[str, Any] = None) -> bool:
        """
        Initialize browser resources for this session.
        """
        if self.browser is not None:
            logger.warning(f"Browser already initialized for session {self.session_id}")
            return True
        
        try:
            from src.browser.custom_browser import CustomBrowser
            from src.browser.browser_config import BrowserConfig 
            from src.browser.custom_context import BrowserContextConfig
            from browser_use.browser.context import BrowserContextWindowSize
            
            default_config = {
                'headless': True,
                'disable_security': True,
                'window_width': 1280,
                'window_height': 1100,
                'user_data_dir': None
            }
            
            if browser_config:
                default_config.update(browser_config)
            
            extra_args = [f"--window-size={default_config['window_width']},{default_config['window_height']}"]
            
            self.browser = CustomBrowser(
                config=BrowserConfig(
                    headless=default_config['headless'],
                    disable_security=default_config['disable_security'],
                    extra_browser_args=extra_args
                )
            )
            
            # *** FIX: The 'force_new_context' argument has been removed ***
            context_config = BrowserContextConfig(
                save_downloads_path=f"./tmp/downloads/session_{self.session_id}",
                browser_window_size=BrowserContextWindowSize(
                    width=default_config['window_width'],
                    height=default_config['window_height']
                )
            )
            
            self.browser_context = await self.browser.new_context(config=context_config)
            
            logger.info(f"Browser initialized for session {self.session_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize browser for session {self.session_id}: {e}", exc_info=True)
            await self._cleanup_browser()
            return False

    async def initialize_controller(self, mcp_config: Dict[str, Any] = None) -> bool:
        """
        Initialize controller with optional MCP configuration.
        
        Args:
            mcp_config: MCP server configuration
            
        Returns:
            bool: True if successful, False otherwise
        """
        if self.controller is not None:
            logger.warning(f"Controller already initialized for session {self.session_id}")
            return True
        
        try:
            from src.controller.custom_controller import CustomController
            
            # Create session-specific ask callback
            async def ask_callback_wrapper(query: str, browser_context) -> Dict[str, Any]:
                return await self._handle_agent_help_request(query, browser_context)
            
            self.controller = CustomController(ask_assistant_callback=ask_callback_wrapper)
            
            if mcp_config:
                await self.controller.setup_mcp_client(mcp_config)
            
            logger.info(f"Controller initialized for session {self.session_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize controller for session {self.session_id}: {e}", exc_info=True)
            return False

    # In src/core/user_session.py, replace the existing start_agent_task function

    # In src/core/user_session.py, replace the existing start_agent_task function

    async def start_agent_task(self, task_description: str, llm, agent_config: Dict[str, Any] = None) -> str:
        """
        Start a new agent task.
        """
        if self.current_task and not self.current_task.done():
            raise RuntimeError("Agent task already running")
        
        if not self.browser or not self.browser_context or not self.controller:
            raise RuntimeError("Browser and controller must be initialized before starting agent")
        
        task_id = str(uuid.uuid4())
        
        try:
            from src.agent.custom_agent import CustomAgent
            from src.agent.custom_prompts import CustomSystemPrompt
            
            # --- THE FIX IS HERE ---
            # The 'agent_prompt_class' has been removed from the config dictionary
            # because the CustomAgent defines its own default.
            
            self.max_steps = agent_config.pop('max_steps', 100)
            
            default_config = {
                'use_vision': self.enable_vision,
                'max_actions_per_step': 10,
                'system_prompt_class': CustomSystemPrompt,
                # 'agent_prompt_class' was here and has been removed.
            }
            
            if agent_config:
                default_config.update(agent_config)

            self.agent = CustomAgent(
                task=task_description,
                llm=llm,
                browser=self.browser,
                browser_context=self.browser_context,
                controller=self.controller,
                **default_config
            )
            
            self.agent_task_info = AgentTaskInfo(
                task_id=task_id,
                task_description=task_description,
                started_at=datetime.now(),
                agent_state=AgentState.INITIALIZING,
                max_steps=self.max_steps
            )
            
            self.current_task = asyncio.create_task(self._run_agent())
            
            logger.info(f"Started agent task {task_id} for session {self.session_id}")
            return task_id
            
        except Exception as e:
            logger.error(f"Failed to start agent task for session {self.session_id}: {e}", exc_info=True)
            raise

    async def stop_agent_task(self, reason: str = "user_request") -> bool:
        """
        Stop the current agent task.
        
        Args:
            reason: Reason for stopping
            
        Returns:
            bool: True if stopped successfully
        """
        if not self.current_task or self.current_task.done():
            return False
        
        try:
            if self.agent:
                self.agent.state.stopped = True
                self.agent.state.paused = False
            
            # Cancel the task
            self.current_task.cancel()
            
            try:
                await asyncio.wait_for(self.current_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            
            if self.agent_task_info:
                self.agent_task_info.agent_state = AgentState.STOPPED
            
            logger.info(f"Stopped agent task for session {self.session_id} (reason: {reason})")
            return True
            
        except Exception as e:
            logger.error(f"Error stopping agent task for session {self.session_id}: {e}", exc_info=True)
            return False

    async def pause_agent_task(self) -> bool:
        """Pause the current agent task"""
        if not self.agent or not self.current_task or self.current_task.done():
            return False
        
        self.agent.state.paused = True
        if self.agent_task_info:
            self.agent_task_info.agent_state = AgentState.PAUSED
        
        await self._send_websocket_message({
            "type": "agent_status",
            "data": {"status": "paused", "task_id": self.agent_task_info.task_id}
        })
        
        return True

    async def resume_agent_task(self) -> bool:
        """Resume a paused agent task"""
        if not self.agent or not self.current_task or self.current_task.done():
            return False
        
        self.agent.state.paused = False
        if self.agent_task_info:
            self.agent_task_info.agent_state = AgentState.RUNNING
        
        await self._send_websocket_message({
            "type": "agent_status", 
            "data": {"status": "resumed", "task_id": self.agent_task_info.task_id}
        })
        
        return True

    async def set_websocket(self, websocket):
        """Set the WebSocket connection for this session"""
        self.websocket = websocket
        self.websocket_connected = True
        self.active_connections += 1

    async def remove_websocket(self):
        """Remove the WebSocket connection"""
        self.websocket = None
        self.websocket_connected = False
        if self.active_connections > 0:
            self.active_connections -= 1

    async def send_message(self, message: Dict[str, Any]) -> bool:
        """
        Send a message to the client via WebSocket.
        
        Args:
            message: Message to send
            
        Returns:
            bool: True if sent successfully
        """
        return await self._send_websocket_message(message)

    async def add_chat_message(self, role: str, content: str):
        """Add a message to chat history"""
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat()
        }
        
        self.chat_history.append(message)
        
        # Limit chat history size
        if len(self.chat_history) > self.max_chat_history:
            self.chat_history = self.chat_history[-self.max_chat_history:]
        
        # Send to client
        await self._send_websocket_message({
            "type": "chat_message",
            "data": message
        })
        
        self.stats['total_messages_sent'] += 1

    async def get_status(self) -> Dict[str, Any]:
        """Get current session status"""
        return {
            'session_id': self.session_id,
            'state': self.state.value,
            'created_at': self.created_at.isoformat(),
            'last_activity': self.last_activity.isoformat(),
            'client_ip': self.client_ip,
            'websocket_connected': self.websocket_connected,
            'browser_initialized': self.browser is not None,
            'controller_initialized': self.controller is not None,
            'agent_task_info': self.agent_task_info.to_dict() if self.agent_task_info else None,
            'chat_history_length': len(self.chat_history),
            'memory_usage_mb': self.memory_usage_mb,
            'browser_tabs_count': self.browser_tabs_count,
            'active_connections': self.active_connections,
            'stats': self.stats.copy()
        }

    async def cleanup(self):
        """Cleanup all session resources"""
        if self.is_cleanup_started:
            return
        
        self.is_cleanup_started = True
        self.state = SessionState.CLEANUP
        
        logger.info(f"Starting cleanup for session {self.session_id}")
        
        try:
            # Stop any running agent task
            if self.current_task and not self.current_task.done():
                await self.stop_agent_task("session_cleanup")
            
            # Clear agent references
            self.agent = None
            self.current_task = None
            self.agent_task_info = None
            
            # Cleanup browser resources
            await self._cleanup_browser()
            
            # Cleanup controller
            if self.controller:
                try:
                    await self.controller.close_mcp_client()
                except Exception as e:
                    logger.warning(f"Error closing MCP client: {e}")
                self.controller = None
            
            # Clear WebSocket
            if self.websocket:
                try:
                    await self.websocket.close()
                except:
                    pass
                self.websocket = None
                self.websocket_connected = False
            
            # Clear chat history and events
            self.chat_history.clear()
            self.response_event = None
            self.user_help_response = None
            
            self.state = SessionState.CLOSED
            
            logger.info(f"Session {self.session_id} cleanup completed")
            
        except Exception as e:
            logger.error(f"Error during session {self.session_id} cleanup: {e}", exc_info=True)

    # Private methods
    
    async def _run_agent(self):
        """Internal method to run the agent task"""
        try:
            if self.agent_task_info:
                self.agent_task_info.agent_state = AgentState.RUNNING
            
            history = await self.agent.run(max_steps=self.max_steps)
            
            if self.agent_task_info:
                self.agent_task_info.agent_state = AgentState.COMPLETED
                self.agent_task_info.result = history.final_result()
            
            self.stats['tasks_completed'] += 1
            
        except asyncio.CancelledError:
            if self.agent_task_info:
                self.agent_task_info.agent_state = AgentState.STOPPED
            raise
        except Exception as e:
            if self.agent_task_info:
                self.agent_task_info.agent_state = AgentState.FAILED
                self.agent_task_info.error_message = str(e)
            
            self.stats['tasks_failed'] += 1
            logger.error(f"Agent task failed for session {self.session_id}: {e}", exc_info=True)

    async def _handle_agent_step(self, state, output, step_num: int):
        """Handle agent step callback"""
        if self.agent_task_info:
            self.agent_task_info.current_step = step_num
        
        self.stats['total_steps_executed'] += 1
        
        # Send step update to client
        await self._send_websocket_message({
            "type": "agent_step",
            "data": {
                "step_number": step_num,
                "task_id": self.agent_task_info.task_id if self.agent_task_info else None,
                "screenshot": getattr(state, 'screenshot', None),
                "url": getattr(state, 'url', None)
            }
        })

    def _handle_agent_completion(self, history):
        """Handle agent completion callback"""
        final_result = history.final_result() if history else "No result"
        
        asyncio.create_task(self._send_websocket_message({
            "type": "agent_completed",
            "data": {
                "task_id": self.agent_task_info.task_id if self.agent_task_info else None,
                "result": final_result,
                "duration_seconds": history.total_duration_seconds() if history else 0,
                "total_steps": self.agent_task_info.current_step if self.agent_task_info else 0
            }
        }))

    async def _handle_agent_help_request(self, query: str, browser_context) -> Dict[str, Any]:
        """Handle agent's request for user assistance"""
        logger.info(f"Agent needs help for session {self.session_id}: {query}")
        
        # Send help request to client
        await self._send_websocket_message({
            "type": "agent_help_request",
            "data": {
                "query": query,
                "task_id": self.agent_task_info.task_id if self.agent_task_info else None
            }
        })
        
        # Set up response event
        self.response_event = asyncio.Event()
        self.user_help_response = None
        
        try:
            # Wait for user response with timeout
            await asyncio.wait_for(self.response_event.wait(), timeout=3600.0)
            
            response = self.user_help_response or "No response provided"
            
            # Send confirmation to client
            await self._send_websocket_message({
                "type": "agent_help_response_received",
                "data": {"response": response}
            })
            
            return {"response": response}
            
        except asyncio.TimeoutError:
            logger.warning(f"Timeout waiting for user help response in session {self.session_id}")
            return {"response": "Timeout: No user response received"}
        finally:
            self.response_event = None
            self.user_help_response = None

    async def submit_help_response(self, response: str) -> bool:
        """Submit a response to agent's help request"""
        if not self.response_event or self.response_event.is_set():
            return False
        
        self.user_help_response = response
        self.response_event.set()
        return True

    async def _send_websocket_message(self, message: Dict[str, Any]) -> bool:
        """Send a message via WebSocket if connected"""
        if not self.websocket or not self.websocket_connected:
            return False
        
        try:
            import json
            await self.websocket.send_text(json.dumps(message))
            return True
        except Exception as e:
            logger.warning(f"Failed to send WebSocket message for session {self.session_id}: {e}")
            self.websocket_connected = False
            return False

    async def _cleanup_browser(self):
        """Cleanup browser resources"""
        try:
            if self.browser_context:
                await self.browser_context.close()
                self.browser_context = None
                
            if self.browser:
                await self.browser.close()
                self.browser = None
                
            self.browser_tabs_count = 0
            
        except Exception as e:
            logger.warning(f"Error cleaning up browser for session {self.session_id}: {e}")

    def update_activity(self):
        """Update last activity timestamp"""
        self.last_activity = datetime.now()