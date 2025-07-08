import asyncio
import logging
import time
import uuid
from typing import Dict, List, Optional, Any, Callable, Set
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
import weakref
import psutil

logger = logging.getLogger(__name__)


class AgentState(Enum):
    """Agent execution states"""
    IDLE = "idle"
    INITIALIZING = "initializing"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    CLEANUP = "cleanup"


class TaskPriority(Enum):
    """Task priority levels"""
    LOW = 1
    NORMAL = 2
    HIGH = 3
    URGENT = 4


@dataclass
class AgentTaskConfig:
    """Configuration for an agent task"""
    task_id: str
    session_id: str
    task_description: str
    llm_provider: str
    llm_model_name: str
    llm_temperature: float = 0.6
    llm_base_url: Optional[str] = None
    llm_api_key: Optional[str] = None
    use_vision: bool = True
    max_actions_per_step: int = 10
    max_steps: int = 100
    tool_calling_method: Optional[str] = "auto"
    priority: TaskPriority = TaskPriority.NORMAL
    timeout_minutes: int = 60
    retry_count: int = 0
    max_retries: int = 2
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentInstance:
    """Information about an agent instance"""
    agent_id: str
    session_id: str
    state: AgentState
    current_task: Optional[AgentTaskConfig] = None
    agent_object: Optional[Any] = None  # Actual agent instance
    browser_id: Optional[str] = None
    context_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    total_tasks_completed: int = 0
    total_tasks_failed: int = 0
    total_steps_executed: int = 0
    memory_usage_mb: float = 0.0
    error_count: int = 0
    last_error: Optional[str] = None


@dataclass
class AgentManagerConfig:
    """Configuration for the agent manager"""
    max_concurrent_agents: int = 100
    max_agents_per_session: int = 3
    agent_idle_timeout_minutes: int = 15
    task_timeout_minutes: int = 60
    queue_size_limit: int = 1000
    enable_task_retry: bool = True
    cleanup_interval_seconds: int = 30
    monitoring_interval_seconds: int = 60
    enable_performance_monitoring: bool = True
    enable_agent_pooling: bool = True
    min_idle_agents: int = 2
    max_idle_agents: int = 10


class AgentManager:
    """
    Enhanced agent lifecycle management for multi-user sessions.
    Handles agent creation, task queuing, execution monitoring, and cleanup.
    Integrates with browser pool and session management.
    """
    
    def __init__(self, config: AgentManagerConfig = None):
        self.config = config or AgentManagerConfig()
        
        # Agent storage and tracking
        self._agents: Dict[str, AgentInstance] = {}  # agent_id -> agent_instance
        self._session_agents: Dict[str, Set[str]] = {}  # session_id -> set of agent_ids
        self._task_queue: asyncio.Queue = asyncio.Queue(maxsize=self.config.queue_size_limit)
        self._running_tasks: Dict[str, asyncio.Task] = {}  # task_id -> asyncio.Task
        
        # Agent pooling for reuse
        self._idle_agents: Dict[str, AgentInstance] = {}  # Available for reuse
        self._agent_types: Dict[str, str] = {}  # agent_id -> agent_type (for pooling)
        
        # Callbacks for external integration
        self._step_callbacks: List[Callable] = []
        self._completion_callbacks: List[Callable] = []
        self._error_callbacks: List[Callable] = []
        
        # Background tasks
        self._queue_processor_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._monitoring_task: Optional[asyncio.Task] = None
        self._pool_maintenance_task: Optional[asyncio.Task] = None
        self._is_running = False
        
        # Statistics and monitoring
        self._stats = {
            'total_agents_created': 0,
            'total_agents_destroyed': 0,
            'total_tasks_queued': 0,
            'total_tasks_completed': 0,
            'total_tasks_failed': 0,
            'total_steps_executed': 0,
            'current_queue_size': 0,
            'peak_concurrent_agents': 0,
            'average_task_duration_seconds': 0.0,
            'agent_pool_hits': 0,  # Reused agents
            'agent_pool_misses': 0,  # New agents created
        }
        
        # Weak references for automatic cleanup
        self._weak_refs: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
        
        logger.info(f"AgentManager initialized with config: {self.config}")

    async def start(self):
        """Start the agent manager and background tasks"""
        if self._is_running:
            logger.warning("AgentManager is already running")
            return
        
        self._is_running = True
        
        # Start background tasks
        self._queue_processor_task = asyncio.create_task(self._process_task_queue())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        
        if self.config.enable_performance_monitoring:
            self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        
        if self.config.enable_agent_pooling:
            self._pool_maintenance_task = asyncio.create_task(self._pool_maintenance_loop())
        
        logger.info("AgentManager started with background tasks")

    async def stop(self):
        """Stop the agent manager and cleanup all agents"""
        if not self._is_running:
            return
        
        self._is_running = False
        
        # Cancel background tasks
        for task in [self._queue_processor_task, self._cleanup_task, 
                    self._monitoring_task, self._pool_maintenance_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Stop all running tasks
        await self._stop_all_tasks()
        
        # Cleanup all agents
        await self._cleanup_all_agents()
        
        logger.info("AgentManager stopped and all agents cleaned up")

    async def create_agent(self, session_id: str, agent_type: str = "default", 
                          browser_id: str = None, context_id: str = None) -> str:
        """
        Create a new agent instance for a session.
        
        Args:
            session_id: Session that owns the agent
            agent_type: Type of agent (for pooling compatibility)
            browser_id: Associated browser instance
            context_id: Associated browser context
            
        Returns:
            str: Agent ID
            
        Raises:
            ValueError: If limits are exceeded
        """
        # Check limits
        if len(self._agents) >= self.config.max_concurrent_agents:
            raise ValueError(f"Maximum concurrent agents ({self.config.max_concurrent_agents}) exceeded")
        
        session_agent_count = len(self._session_agents.get(session_id, set()))
        if session_agent_count >= self.config.max_agents_per_session:
            raise ValueError(f"Maximum agents per session ({self.config.max_agents_per_session}) exceeded")
        
        # Try to reuse an idle agent if pooling is enabled
        if self.config.enable_agent_pooling:
            reused_agent_id = await self._try_reuse_agent(session_id, agent_type)
            if reused_agent_id:
                self._stats['agent_pool_hits'] += 1
                return reused_agent_id
        
        self._stats['agent_pool_misses'] += 1
        
        # Generate agent ID
        agent_id = f"agent_{int(time.time() * 1000000)}"
        
        # Create agent instance
        agent_instance = AgentInstance(
            agent_id=agent_id,
            session_id=session_id,
            state=AgentState.IDLE,
            browser_id=browser_id,
            context_id=context_id
        )
        
        # Store agent
        self._agents[agent_id] = agent_instance
        self._weak_refs[agent_id] = agent_instance
        self._agent_types[agent_id] = agent_type
        
        # Track session agents
        if session_id not in self._session_agents:
            self._session_agents[session_id] = set()
        self._session_agents[session_id].add(agent_id)
        
        # Update statistics
        self._stats['total_agents_created'] += 1
        current_count = len(self._agents)
        if current_count > self._stats['peak_concurrent_agents']:
            self._stats['peak_concurrent_agents'] = current_count
        
        logger.info(f"Created agent {agent_id} for session {session_id}")
        return agent_id

    async def queue_task(self, task_config: AgentTaskConfig) -> str:
        """
        Queue a task for execution.
        
        Args:
            task_config: Task configuration
            
        Returns:
            str: Task ID
            
        Raises:
            ValueError: If queue is full or agent not found
        """
        # Validate session has agents
        session_agents = self._session_agents.get(task_config.session_id, set())
        if not session_agents:
            # Auto-create an agent for the session if none exists
            await self.create_agent(task_config.session_id)
        
        # Check queue capacity
        if self._task_queue.qsize() >= self.config.queue_size_limit:
            raise ValueError(f"Task queue is full ({self.config.queue_size_limit} tasks)")
        
        # Set task timestamps
        task_config.created_at = datetime.now()
        
        # Add to queue with priority
        await self._task_queue.put((task_config.priority.value, task_config))
        
        # Update statistics
        self._stats['total_tasks_queued'] += 1
        self._stats['current_queue_size'] = self._task_queue.qsize()
        
        logger.info(f"Queued task {task_config.task_id} for session {task_config.session_id}")
        return task_config.task_id

    async def stop_task(self, task_id: str, reason: str = "user_request") -> bool:
        """
        Stop a running task.
        
        Args:
            task_id: Task to stop
            reason: Reason for stopping
            
        Returns:
            bool: True if task was stopped
        """
        # Find the agent with this task
        agent_instance = None
        for agent in self._agents.values():
            if agent.current_task and agent.current_task.task_id == task_id:
                agent_instance = agent
                break
        
        if not agent_instance:
            logger.warning(f"Task {task_id} not found or not running")
            return False
        
        try:
            # Update agent state
            agent_instance.state = AgentState.STOPPING
            
            # Stop the agent if it has a stop method
            if agent_instance.agent_object and hasattr(agent_instance.agent_object, 'stop'):
                await agent_instance.agent_object.stop()
            
            # Cancel the asyncio task if it exists
            if task_id in self._running_tasks:
                running_task = self._running_tasks[task_id]
                running_task.cancel()
                
                try:
                    await asyncio.wait_for(running_task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                
                del self._running_tasks[task_id]
            
            # Update task completion
            if agent_instance.current_task:
                agent_instance.current_task.completed_at = datetime.now()
            
            # Move agent back to idle pool if enabled
            if self.config.enable_agent_pooling:
                await self._return_agent_to_pool(agent_instance)
            else:
                # Reset agent state
                agent_instance.current_task = None
                agent_instance.state = AgentState.IDLE
                agent_instance.last_activity = datetime.now()
            
            logger.info(f"Stopped task {task_id} (reason: {reason})")
            return True
            
        except Exception as e:
            logger.error(f"Error stopping task {task_id}: {e}", exc_info=True)
            agent_instance.error_count += 1
            agent_instance.last_error = str(e)
            return False

    async def pause_task(self, task_id: str) -> bool:
        """Pause a running task"""
        agent_instance = self._find_agent_by_task(task_id)
        if not agent_instance or agent_instance.state != AgentState.RUNNING:
            return False
        
        try:
            if agent_instance.agent_object and hasattr(agent_instance.agent_object, 'pause'):
                await agent_instance.agent_object.pause()
                agent_instance.state = AgentState.PAUSED
                logger.info(f"Paused task {task_id}")
                return True
        except Exception as e:
            logger.error(f"Error pausing task {task_id}: {e}", exc_info=True)
        
        return False

    async def resume_task(self, task_id: str) -> bool:
        """Resume a paused task"""
        agent_instance = self._find_agent_by_task(task_id)
        if not agent_instance or agent_instance.state != AgentState.PAUSED:
            return False
        
        try:
            if agent_instance.agent_object and hasattr(agent_instance.agent_object, 'resume'):
                await agent_instance.agent_object.resume()
                agent_instance.state = AgentState.RUNNING
                logger.info(f"Resumed task {task_id}")
                return True
        except Exception as e:
            logger.error(f"Error resuming task {task_id}: {e}", exc_info=True)
        
        return False

    async def get_agent_status(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get status of a specific agent"""
        if agent_id not in self._agents:
            return None
        
        agent = self._agents[agent_id]
        
        # Update memory usage
        await self._update_agent_memory_usage(agent)
        
        status = {
            'agent_id': agent_id,
            'session_id': agent.session_id,
            'state': agent.state.value,
            'agent_type': self._agent_types.get(agent_id, 'unknown'),
            'created_at': agent.created_at.isoformat(),
            'last_activity': agent.last_activity.isoformat(),
            'total_tasks_completed': agent.total_tasks_completed,
            'total_tasks_failed': agent.total_tasks_failed,
            'total_steps_executed': agent.total_steps_executed,
            'memory_usage_mb': agent.memory_usage_mb,
            'error_count': agent.error_count,
            'browser_id': agent.browser_id,
            'context_id': agent.context_id,
            'current_task': None
        }
        
        if agent.current_task:
            task = agent.current_task
            status['current_task'] = {
                'task_id': task.task_id,
                'description': task.task_description,
                'started_at': task.started_at.isoformat() if task.started_at else None,
                'progress': self._calculate_task_progress(task),
                'priority': task.priority.value,
                'retry_count': task.retry_count
            }
        
        return status

    async def get_session_agents(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all agents for a session"""
        session_agents = self._session_agents.get(session_id, set())
        
        agents_status = []
        for agent_id in session_agents:
            if agent_id in self._agents:
                status = await self.get_agent_status(agent_id)
                if status:
                    agents_status.append(status)
        
        return agents_status

    async def get_statistics(self) -> Dict[str, Any]:
        """Get enhanced agent manager statistics"""
        pool_stats = {
            'idle_agents': len(self._idle_agents),
            'pool_hit_ratio': (
                self._stats['agent_pool_hits'] / 
                max(1, self._stats['agent_pool_hits'] + self._stats['agent_pool_misses'])
            ) * 100,
            'min_idle_target': self.config.min_idle_agents,
            'max_idle_target': self.config.max_idle_agents,
        }
        
        return {
            **self._stats,
            'current_agents': len(self._agents),
            'current_queue_size': self._task_queue.qsize(),
            'running_tasks': len(self._running_tasks),
            'agents_by_state': self._get_agents_by_state(),
            'agents_by_session': {
                session_id: len(agent_ids) 
                for session_id, agent_ids in self._session_agents.items()
            },
            'pool_statistics': pool_stats,
            'memory_usage': await self._get_memory_statistics(),
        }

    # Private methods for enhanced functionality
    
    async def _try_reuse_agent(self, session_id: str, agent_type: str) -> Optional[str]:
        """Try to reuse an idle agent from the pool"""
        # Look for compatible idle agents
        compatible_agents = [
            agent_id for agent_id, agent in self._idle_agents.items()
            if self._agent_types.get(agent_id) == agent_type
        ]
        
        if not compatible_agents:
            return None
        
        # Get the least recently used agent
        agent_id = min(compatible_agents, 
                      key=lambda aid: self._idle_agents[aid].last_activity)
        
        agent_instance = self._idle_agents.pop(agent_id)
        
        # Update agent for new session
        agent_instance.session_id = session_id
        agent_instance.state = AgentState.IDLE
        agent_instance.last_activity = datetime.now()
        agent_instance.current_task = None
        
        # Move back to active agents
        self._agents[agent_id] = agent_instance
        
        # Track session agents
        if session_id not in self._session_agents:
            self._session_agents[session_id] = set()
        self._session_agents[session_id].add(agent_id)
        
        logger.info(f"Reused agent {agent_id} for session {session_id}")
        return agent_id

    async def _return_agent_to_pool(self, agent_instance: AgentInstance):
        """Return an agent to the idle pool for reuse"""
        agent_id = agent_instance.agent_id
        
        # Clean up agent state
        agent_instance.current_task = None
        agent_instance.state = AgentState.IDLE
        agent_instance.last_activity = datetime.now()
        
        # Remove from session tracking
        session_id = agent_instance.session_id
        if session_id in self._session_agents:
            self._session_agents[session_id].discard(agent_id)
            if not self._session_agents[session_id]:
                del self._session_agents[session_id]
        
        # Check pool capacity
        if len(self._idle_agents) >= self.config.max_idle_agents:
            # Pool is full, destroy oldest agent
            oldest_agent_id = min(self._idle_agents.keys(),
                                 key=lambda aid: self._idle_agents[aid].last_activity)
            await self._destroy_agent(oldest_agent_id, "pool_full")
        
        # Move to idle pool
        self._idle_agents[agent_id] = agent_instance
        del self._agents[agent_id]
        
        logger.debug(f"Returned agent {agent_id} to idle pool")

    async def _pool_maintenance_loop(self):
        """Background task to maintain optimal pool size"""
        logger.info("Starting agent pool maintenance loop")
        
        while self._is_running:
            try:
                await self._maintain_pool_size()
                await asyncio.sleep(60)  # Check every minute
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in pool maintenance loop: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _maintain_pool_size(self):
        """Maintain optimal number of idle agents"""
        current_idle = len(self._idle_agents)
        target_min = self.config.min_idle_agents
        target_max = self.config.max_idle_agents
        
        if current_idle < target_min:
            # Create more idle agents
            agents_to_create = target_min - current_idle
            logger.info(f"Creating {agents_to_create} idle agents for pool")
            
            for _ in range(agents_to_create):
                try:
                    # Create agent with dummy session for pool
                    pool_session_id = f"pool_agent_{uuid.uuid4().hex[:8]}"
                    agent_id = await self.create_agent(pool_session_id, "pooled")
                    
                    # Move directly to idle pool
                    agent_instance = self._agents.pop(agent_id)
                    self._session_agents[pool_session_id].remove(agent_id)
                    if not self._session_agents[pool_session_id]:
                        del self._session_agents[pool_session_id]
                    
                    self._idle_agents[agent_id] = agent_instance
                    
                except Exception as e:
                    logger.error(f"Error creating pool agent: {e}")
                    break
        
        elif current_idle > target_max:
            # Remove excess idle agents
            excess_agents = current_idle - target_max
            agents_to_remove = list(self._idle_agents.keys())[:excess_agents]
            
            for agent_id in agents_to_remove:
                await self._destroy_agent(agent_id, "pool_excess")

    async def _update_agent_memory_usage(self, agent_instance: AgentInstance):
        """Update memory usage for an agent"""
        try:
            if agent_instance.agent_object:
                # Simplified memory calculation
                process = psutil.Process()
                agent_instance.memory_usage_mb = process.memory_info().rss / (1024 * 1024) / len(self._agents)
        except Exception:
            agent_instance.memory_usage_mb = 0.0

    async def _get_memory_statistics(self) -> Dict[str, float]:
        """Get memory usage statistics"""
        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            
            total_agent_memory = sum(agent.memory_usage_mb for agent in self._agents.values())
            
            return {
                'total_process_memory_mb': memory_info.rss / (1024 * 1024),
                'total_agent_memory_mb': total_agent_memory,
                'average_memory_per_agent_mb': total_agent_memory / max(1, len(self._agents)),
                'memory_efficiency_percent': (total_agent_memory / (memory_info.rss / (1024 * 1024))) * 100
            }
        except Exception:
            return {
                'total_process_memory_mb': 0.0,
                'total_agent_memory_mb': 0.0,
                'average_memory_per_agent_mb': 0.0,
                'memory_efficiency_percent': 0.0
            }

    async def _process_task_queue(self):
        """Enhanced task queue processor with priority handling"""
        logger.info("Starting task queue processor")
        
        while self._is_running:
            try:
                # Get next task from priority queue
                try:
                    priority, task_config = await asyncio.wait_for(
                        self._task_queue.get(), 
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                
                # Find available agent for the session
                agent_id = await self._find_available_agent(task_config.session_id)
                
                if not agent_id:
                    # No available agent, requeue task or fail
                    if task_config.retry_count < task_config.max_retries:
                        task_config.retry_count += 1
                        await asyncio.sleep(min(2 ** task_config.retry_count, 10))  # Exponential backoff
                        await self._task_queue.put((priority, task_config))
                        continue
                    else:
                        logger.error(f"No available agent for task {task_config.task_id} after retries")
                        await self._handle_task_failure(task_config, "no_agent_available")
                        continue
                
                # Execute the task
                await self._execute_task(agent_id, task_config)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in task queue processor: {e}", exc_info=True)
                await asyncio.sleep(1)

    # ... [Include other existing methods from the original with enhancements]
    
    def register_step_callback(self, callback: Callable):
        """Register callback for agent step events"""
        self._step_callbacks.append(callback)

    def register_completion_callback(self, callback: Callable):
        """Register callback for task completion events"""
        self._completion_callbacks.append(callback)

    def register_error_callback(self, callback: Callable):
        """Register callback for agent error events"""
        self._error_callbacks.append(callback)

    async def cleanup_session_agents(self, session_id: str):
        """Cleanup all agents for a session"""
        if session_id not in self._session_agents:
            return
        
        agent_ids = self._session_agents[session_id].copy()
        
        for agent_id in agent_ids:
            if self.config.enable_agent_pooling:
                # Try to return to pool instead of destroying
                agent_instance = self._agents.get(agent_id)
                if agent_instance and agent_instance.state == AgentState.IDLE:
                    await self._return_agent_to_pool(agent_instance)
                    continue
            
            await self._destroy_agent(agent_id, "session_cleanup")
        
        # Remove session tracking
        if session_id in self._session_agents:
            del self._session_agents[session_id]
        
        logger.info(f"Cleaned up all agents for session {session_id}")

    # ... [Additional helper methods would continue here]
    
    def _find_agent_by_task(self, task_id: str) -> Optional[AgentInstance]:
        """Find agent by task ID"""
        for agent in self._agents.values():
            if agent.current_task and agent.current_task.task_id == task_id:
                return agent
        return None

    def _calculate_task_progress(self, task_config: AgentTaskConfig) -> float:
        """Calculate task progress percentage"""
        if not task_config.started_at:
            return 0.0
        
        elapsed = (datetime.now() - task_config.started_at).total_seconds()
        estimated_total = task_config.timeout_minutes * 60
        
        return min(100.0, (elapsed / estimated_total) * 100)

    def _get_agents_by_state(self) -> Dict[str, int]:
        """Get count of agents by state"""
        state_counts = {}
        for state in AgentState:
            state_counts[state.value] = 0
        
        for agent in self._agents.values():
            state_counts[agent.state.value] += 1
        
        return state_counts

    async def _find_available_agent(self, session_id: str) -> Optional[str]:
        """Find an available agent for a session"""
        session_agents = self._session_agents.get(session_id, set())
        
        for agent_id in session_agents:
            if agent_id in self._agents:
                agent = self._agents[agent_id]
                if agent.state == AgentState.IDLE:
                    return agent_id
        
        return None

    async def _execute_task(self, agent_id: str, task_config: AgentTaskConfig):
        """Execute a task with the specified agent"""
        agent_instance = self._agents.get(agent_id)
        if not agent_instance:
            await self._handle_task_failure(task_config, "agent_not_found")
            return
        
        try:
            # Update agent state
            agent_instance.state = AgentState.INITIALIZING
            agent_instance.current_task = task_config
            task_config.started_at = datetime.now()
            
            # Create the actual agent instance
            agent_object = await self._create_agent_instance(agent_instance, task_config)
            
            if not agent_object:
                await self._handle_task_failure(task_config, "agent_creation_failed")
                return
            
            agent_instance.agent_object = agent_object
            agent_instance.state = AgentState.RUNNING
            
            # Start the task execution
            task_coroutine = self._run_agent_task(agent_instance, task_config)
            running_task = asyncio.create_task(task_coroutine)
            self._running_tasks[task_config.task_id] = running_task
            
            logger.info(f"Started executing task {task_config.task_id} with agent {agent_id}")
            
        except Exception as e:
            logger.error(f"Error executing task {task_config.task_id}: {e}", exc_info=True)
            await self._handle_task_failure(task_config, f"execution_error: {e}")

    async def _run_agent_task(self, agent_instance: AgentInstance, task_config: AgentTaskConfig):
        """Run the actual agent task"""
        try:
            # Run the agent with timeout
            result = await asyncio.wait_for(
                agent_instance.agent_object.run(max_steps=task_config.max_steps),
                timeout=task_config.timeout_minutes * 60
            )
            
            # Task completed successfully
            await self._handle_task_completion(agent_instance, task_config, result)
            
        except asyncio.TimeoutError:
            logger.warning(f"Task {task_config.task_id} timed out")
            await self._handle_task_failure(task_config, "timeout")
        except asyncio.CancelledError:
            logger.info(f"Task {task_config.task_id} was cancelled")
            await self._handle_task_failure(task_config, "cancelled")
        except Exception as e:
            logger.error(f"Task {task_config.task_id} failed: {e}", exc_info=True)
            await self._handle_task_failure(task_config, f"runtime_error: {e}")
        finally:
            # Cleanup
            if task_config.task_id in self._running_tasks:
                del self._running_tasks[task_config.task_id]
            
            # Handle agent cleanup or return to pool
            if self.config.enable_agent_pooling and agent_instance.error_count < 3:
                await self._return_agent_to_pool(agent_instance)
            else:
                # Reset agent state
                agent_instance.current_task = None
                agent_instance.agent_object = None
                agent_instance.state = AgentState.IDLE
                agent_instance.last_activity = datetime.now()

    async def _create_agent_instance(self, agent_instance: AgentInstance, 
                                   task_config: AgentTaskConfig) -> Optional[Any]:
        """Create the actual agent instance for task execution"""
        try:
            # Import here to avoid circular imports
            from src.core.session_manager import get_session_manager
            from src.core.resource_pool import get_resource_pool
            from src.utils.llm_provider import get_llm_model
            from src.agent.custom_agent import CustomAgent
            from src.agent.custom_prompts import CustomSystemPrompt, CustomAgentMessagePrompt
            
            # Get session and resources
            session_manager = get_session_manager()
            session = await session_manager.get_session(agent_instance.session_id)
            
            if not session or not session.browser or not session.browser_context:
                logger.error(f"Session {agent_instance.session_id} not ready for agent execution")
                return None
            
            # Initialize LLM
            llm = get_llm_model(
                provider=task_config.llm_provider,
                model_name=task_config.llm_model_name,
                temperature=task_config.llm_temperature,
                base_url=task_config.llm_base_url,
                api_key=task_config.llm_api_key
            )
            
            # Create step callback
            async def step_callback(state, output, step_num):
                await self._handle_agent_step(agent_instance, state, output, step_num)
            
            # Create completion callback
            def completion_callback(history):
                asyncio.create_task(
                    self._handle_agent_completion(agent_instance, history)
                )
            
            # Create agent instance
            agent = CustomAgent(
                task=task_config.task_description,
                llm=llm,
                browser=session.browser,
                browser_context=session.browser_context,
                controller=session.controller,
                register_new_step_callback=step_callback,
                register_done_callback=completion_callback,
                use_vision=task_config.use_vision,
                max_actions_per_step=task_config.max_actions_per_step,
                system_prompt_class=CustomSystemPrompt,
                agent_prompt_class=CustomAgentMessagePrompt,
                tool_calling_method=task_config.tool_calling_method
            )
            
            return agent
            
        except Exception as e:
            logger.error(f"Error creating agent instance: {e}", exc_info=True)
            return None

    async def _handle_agent_step(self, agent_instance: AgentInstance, state, output, step_num: int):
        """Handle agent step callback"""
        agent_instance.total_steps_executed += 1
        agent_instance.last_activity = datetime.now()
        self._stats['total_steps_executed'] += 1
        
        # Call registered step callbacks
        for callback in self._step_callbacks:
            try:
                await callback(agent_instance.session_id, agent_instance.agent_id, state, output, step_num)
            except Exception as e:
                logger.error(f"Error in step callback: {e}", exc_info=True)

    async def _handle_agent_completion(self, agent_instance: AgentInstance, history):
        """Handle agent completion callback"""
        # Call registered completion callbacks
        for callback in self._completion_callbacks:
            try:
                await callback(agent_instance.session_id, agent_instance.agent_id, history)
            except Exception as e:
                logger.error(f"Error in completion callback: {e}", exc_info=True)

    async def _handle_task_completion(self, agent_instance: AgentInstance, 
                                    task_config: AgentTaskConfig, result):
        """Handle successful task completion"""
        task_config.completed_at = datetime.now()
        agent_instance.total_tasks_completed += 1
        agent_instance.state = AgentState.COMPLETED
        
        # Update statistics
        self._stats['total_tasks_completed'] += 1
        
        # Calculate duration
        if task_config.started_at:
            duration = (task_config.completed_at - task_config.started_at).total_seconds()
            # Update average duration
            total_completed = self._stats['total_tasks_completed']
            current_avg = self._stats['average_task_duration_seconds']
            self._stats['average_task_duration_seconds'] = (
                (current_avg * (total_completed - 1) + duration) / total_completed
            )
        
        logger.info(f"Task {task_config.task_id} completed successfully")

    async def _handle_task_failure(self, task_config: AgentTaskConfig, reason: str):
        """Handle task failure"""
        task_config.completed_at = datetime.now()
        
        # Find agent if exists
        agent_instance = None
        for agent in self._agents.values():
            if agent.current_task and agent.current_task.task_id == task_config.task_id:
                agent_instance = agent
                break
        
        if agent_instance:
            agent_instance.total_tasks_failed += 1
            agent_instance.state = AgentState.FAILED
            agent_instance.last_error = reason
            agent_instance.error_count += 1
        
        # Update statistics
        self._stats['total_tasks_failed'] += 1
        
        # Call error callbacks
        for callback in self._error_callbacks:
            try:
                await callback(task_config.session_id, task_config.task_id, reason)
            except Exception as e:
                logger.error(f"Error in error callback: {e}", exc_info=True)
        
        logger.error(f"Task {task_config.task_id} failed: {reason}")

    async def _cleanup_loop(self):
        """Background task to cleanup idle agents"""
        logger.info("Starting agent cleanup loop")
        
        while self._is_running:
            try:
                await self._cleanup_idle_agents()
                await asyncio.sleep(self.config.cleanup_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}", exc_info=True)
                await asyncio.sleep(self.config.cleanup_interval_seconds)

    async def _monitoring_loop(self):
        """Background task for performance monitoring"""
        logger.info("Starting agent monitoring loop")
        
        while self._is_running:
            try:
                await self._update_agent_metrics()
                await asyncio.sleep(self.config.monitoring_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}", exc_info=True)
                await asyncio.sleep(self.config.monitoring_interval_seconds)

    async def _cleanup_idle_agents(self):
        """Cleanup agents that have been idle too long"""
        now = datetime.now()
        idle_timeout = timedelta(minutes=self.config.agent_idle_timeout_minutes)
        
        agents_to_cleanup = []
        
        for agent_id, agent in self._agents.items():
            if (agent.state == AgentState.IDLE and 
                now - agent.last_activity > idle_timeout and
                not self.config.enable_agent_pooling):  # Don't cleanup if pooling enabled
                agents_to_cleanup.append(agent_id)
        
        for agent_id in agents_to_cleanup:
            await self._destroy_agent(agent_id, "idle_timeout")

    async def _update_agent_metrics(self):
        """Update agent performance metrics"""
        try:
            for agent in self._agents.values():
                await self._update_agent_memory_usage(agent)
        except Exception as e:
            logger.debug(f"Error updating agent metrics: {e}")

    async def _stop_all_tasks(self):
        """Stop all running tasks"""
        task_ids = list(self._running_tasks.keys())
        
        for task_id in task_ids:
            await self.stop_task(task_id, "manager_shutdown")

    async def _destroy_agent(self, agent_id: str, reason: str):
        """Destroy an agent instance"""
        # Check both active and idle agents
        agent_instance = self._agents.get(agent_id) or self._idle_agents.get(agent_id)
        
        if not agent_instance:
            return
        
        try:
            # Stop any running task
            if agent_instance.current_task:
                await self.stop_task(agent_instance.current_task.task_id, reason)
            
            # Cleanup agent object
            if agent_instance.agent_object:
                try:
                    if hasattr(agent_instance.agent_object, 'cleanup'):
                        await agent_instance.agent_object.cleanup()
                except Exception as e:
                    logger.warning(f"Error cleaning up agent object: {e}")
                agent_instance.agent_object = None
            
            # Remove from tracking
            self._agents.pop(agent_id, None)
            self._idle_agents.pop(agent_id, None)
            self._agent_types.pop(agent_id, None)
            
            # Remove from session tracking
            session_id = agent_instance.session_id
            if session_id in self._session_agents:
                self._session_agents[session_id].discard(agent_id)
                if not self._session_agents[session_id]:
                    del self._session_agents[session_id]
            
            # Update statistics
            self._stats['total_agents_destroyed'] += 1
            
            logger.debug(f"Destroyed agent {agent_id} (reason: {reason})")
            
        except Exception as e:
            logger.error(f"Error destroying agent {agent_id}: {e}", exc_info=True)

    async def _cleanup_all_agents(self):
        """Cleanup all agents during shutdown"""
        logger.info("Cleaning up all agents")
        
        # Get all agent IDs from both active and idle pools
        all_agent_ids = list(self._agents.keys()) + list(self._idle_agents.keys())
        
        for agent_id in all_agent_ids:
            await self._destroy_agent(agent_id, "manager_shutdown")
        
        # Clear all tracking data
        self._agents.clear()
        self._idle_agents.clear()
        self._session_agents.clear()
        self._running_tasks.clear()
        self._agent_types.clear()
        
        logger.info("All agents cleaned up")


# Global instance (to be initialized in main app)
agent_manager: Optional[AgentManager] = None


def get_agent_manager() -> AgentManager:
    """Get the global agent manager instance"""
    global agent_manager
    if agent_manager is None:
        raise RuntimeError("AgentManager not initialized. Call init_agent_manager() first.")
    return agent_manager


async def init_agent_manager(config: AgentManagerConfig = None) -> AgentManager:
    """Initialize the global agent manager"""
    global agent_manager
    if agent_manager is not None:
        logger.warning("AgentManager already initialized")
        return agent_manager
    
    agent_manager = AgentManager(config)
    await agent_manager.start()
    return agent_manager


async def shutdown_agent_manager():
    """Shutdown the global agent manager"""
    global agent_manager
    if agent_manager is not None:
        await agent_manager.stop()
        agent_manager = None