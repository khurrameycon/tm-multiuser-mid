from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field, validator


class SessionCreateRequest(BaseModel):
    """Request model for creating a new session"""
    initialize_browser: bool = Field(True, description="Whether to initialize browser resources")
    initialize_controller: bool = Field(True, description="Whether to initialize controller")
    browser_config: Optional[Dict[str, Any]] = Field(None, description="Browser configuration options")
    mcp_config: Optional[Dict[str, Any]] = Field(None, description="MCP server configuration")


class AgentRunRequest(BaseModel):
    """Request model for running an agent task"""
    task: str = Field(..., min_length=1, max_length=5000, description="Task description for the agent")
    
    # LLM Configuration
    llm_provider: str = Field("openai", description="LLM provider")
    llm_model_name: Optional[str] = Field(None, description="LLM model name")
    llm_temperature: float = Field(0.6, ge=0.0, le=2.0, description="LLM temperature")
    llm_base_url: Optional[str] = Field(None, description="Custom LLM base URL")
    llm_api_key: Optional[str] = Field(None, description="LLM API key")
    
    # Ollama specific
    ollama_num_ctx: Optional[int] = Field(16000, ge=256, le=65536, description="Ollama context length")
    
    # Agent Configuration
    use_vision: bool = Field(True, description="Enable vision capabilities")
    max_actions_per_step: int = Field(10, ge=1, le=50, description="Maximum actions per step")
    max_steps: int = Field(100, ge=1, le=1000, description="Maximum steps for the task")
    tool_calling_method: Optional[str] = Field("auto", description="Tool calling method")
    
    # Override prompts (optional)
    override_system_prompt: Optional[str] = Field(None, max_length=10000, description="Override system prompt")
    extend_system_prompt: Optional[str] = Field(None, max_length=5000, description="Extend system prompt")
    
    @validator('llm_provider')
    def validate_llm_provider(cls, v):
        valid_providers = [
            'openai', 'azure_openai', 'anthropic', 'deepseek', 'google', 
            'ollama', 'mistral', 'alibaba', 'moonshot', 'unbound', 'groq', 'ibm'
        ]
        if v not in valid_providers:
            raise ValueError(f"Provider must be one of: {', '.join(valid_providers)}")
        return v
    
    @validator('tool_calling_method')
    def validate_tool_calling_method(cls, v):
        if v is not None:
            valid_methods = ['auto', 'json_schema', 'function_calling', 'None']
            if v not in valid_methods:
                raise ValueError(f"Tool calling method must be one of: {', '.join(valid_methods)}")
        return v


class AgentStopRequest(BaseModel):
    """Request model for stopping an agent task"""
    reason: Optional[str] = Field("user_request", max_length=200, description="Reason for stopping")


class AgentPauseRequest(BaseModel):
    """Request model for pausing an agent task"""
    pass  # No additional fields needed


class AgentResumeRequest(BaseModel):
    """Request model for resuming an agent task"""
    pass  # No additional fields needed


class UserHelpResponse(BaseModel):
    """Request model for submitting help response to agent"""
    response: str = Field(..., min_length=1, max_length=2000, description="User's response to agent help request")


class ChatMessageRequest(BaseModel):
    """Request model for adding a chat message"""
    role: str = Field(..., description="Message role")
    content: str = Field(..., min_length=1, max_length=10000, description="Message content")
    
    @validator('role')
    def validate_role(cls, v):
        valid_roles = ['user', 'assistant', 'system']
        if v not in valid_roles:
            raise ValueError(f"Role must be one of: {', '.join(valid_roles)}")
        return v


class BrowserConfigRequest(BaseModel):
    """Request model for browser configuration"""
    headless: bool = Field(True, description="Run browser in headless mode")
    disable_security: bool = Field(True, description="Disable browser security features")
    window_width: int = Field(1280, ge=800, le=3840, description="Browser window width")
    window_height: int = Field(1100, ge=600, le=2160, description="Browser window height")
    user_data_dir: Optional[str] = Field(None, max_length=500, description="Browser user data directory")
    binary_path: Optional[str] = Field(None, max_length=500, description="Custom browser binary path")
    extra_args: Optional[List[str]] = Field(None, description="Additional browser arguments")


class DeepResearchRequest(BaseModel):
    """Request model for deep research agent"""
    research_topic: str = Field(..., min_length=10, max_length=2000, description="Research topic or question")
    max_iterations: int = Field(5, ge=1, le=20, description="Maximum research iterations")
    max_queries_per_iteration: int = Field(3, ge=1, le=10, description="Maximum queries per iteration")
    max_parallel_browsers: int = Field(1, ge=1, le=5, description="Maximum parallel browser instances")
    save_intermediate_results: bool = Field(True, description="Save intermediate research results")
    resume_task_id: Optional[str] = Field(None, description="Task ID to resume from")
    
    # LLM Configuration for research
    research_llm_provider: str = Field("openai", description="LLM provider for research")
    research_llm_model: Optional[str] = Field(None, description="LLM model for research")
    research_llm_temperature: float = Field(0.7, ge=0.0, le=2.0, description="LLM temperature for research")
    
    # Browser configuration for research
    browser_config: Optional[BrowserConfigRequest] = Field(None, description="Browser configuration")


class MaintenanceRequest(BaseModel):
    """Request model for maintenance operations"""
    task: str = Field(..., description="Maintenance task to execute")
    parameters: Optional[Dict[str, Any]] = Field(None, description="Additional parameters for the task")
    force: bool = Field(False, description="Force execution even if risky")
    
    @validator('task')
    def validate_task(cls, v):
        valid_tasks = [
            'cleanup_sessions', 'cleanup_resources', 'cleanup_websockets',
            'force_gc', 'reset_statistics', 'restart_managers'
        ]
        if v not in valid_tasks:
            raise ValueError(f"Task must be one of: {', '.join(valid_tasks)}")
        return v


class WebSocketMessage(BaseModel):
    """Model for WebSocket messages"""
    type: str = Field(..., description="Message type")
    data: Dict[str, Any] = Field(..., description="Message data")
    timestamp: Optional[str] = Field(None, description="Message timestamp")
    session_id: Optional[str] = Field(None, description="Associated session ID")


class BroadcastRequest(BaseModel):
    """Request model for broadcasting messages"""
    message: WebSocketMessage = Field(..., description="Message to broadcast")
    target_sessions: Optional[List[str]] = Field(None, description="Specific sessions to target")
    exclude_sessions: Optional[List[str]] = Field(None, description="Sessions to exclude")


class ResourceAllocationRequest(BaseModel):
    """Request model for resource allocation"""
    resource_type: str = Field(..., description="Type of resource to allocate")
    config: Optional[Dict[str, Any]] = Field(None, description="Resource configuration")
    priority: int = Field(0, ge=0, le=10, description="Allocation priority")
    
    @validator('resource_type')
    def validate_resource_type(cls, v):
        valid_types = ['browser', 'context', 'controller']
        if v not in valid_types:
            raise ValueError(f"Resource type must be one of: {', '.join(valid_types)}")
        return v


class SessionConfigRequest(BaseModel):
    """Request model for session configuration updates"""
    max_browser_tabs: Optional[int] = Field(None, ge=1, le=50, description="Maximum browser tabs")
    max_chat_history: Optional[int] = Field(None, ge=10, le=1000, description="Maximum chat history length")
    enable_vision: Optional[bool] = Field(None, description="Enable vision capabilities")
    max_steps: Optional[int] = Field(None, ge=1, le=1000, description="Maximum steps per task")
    session_timeout_minutes: Optional[int] = Field(None, ge=5, le=480, description="Session timeout in minutes")


class HealthCheckRequest(BaseModel):
    """Request model for health check configuration"""
    check_system_resources: bool = Field(True, description="Check system resource usage")
    check_application_health: bool = Field(True, description="Check application component health")
    check_external_dependencies: bool = Field(False, description="Check external service dependencies")
    detailed_response: bool = Field(False, description="Include detailed diagnostic information")


class LogQueryRequest(BaseModel):
    """Request model for log queries"""
    level: str = Field("INFO", description="Minimum log level")
    component: Optional[str] = Field(None, description="Filter by component name")
    session_id: Optional[str] = Field(None, description="Filter by session ID")
    start_time: Optional[str] = Field(None, description="Start time for log query (ISO format)")
    end_time: Optional[str] = Field(None, description="End time for log query (ISO format)")
    limit: int = Field(100, ge=1, le=10000, description="Maximum number of log entries")
    offset: int = Field(0, ge=0, description="Offset for pagination")
    search_query: Optional[str] = Field(None, max_length=500, description="Search within log messages")
    
    @validator('level')
    def validate_level(cls, v):
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        if v.upper() not in valid_levels:
            raise ValueError(f"Level must be one of: {', '.join(valid_levels)}")
        return v.upper()


class ConfigurationUpdateRequest(BaseModel):
    """Request model for configuration updates"""
    component: str = Field(..., description="Component to update")
    settings: Dict[str, Any] = Field(..., description="Settings to update")
    restart_component: bool = Field(False, description="Whether to restart the component")
    
    @validator('component')
    def validate_component(cls, v):
        valid_components = ['session_manager', 'resource_pool', 'websocket_manager', 'application']
        if v not in valid_components:
            raise ValueError(f"Component must be one of: {', '.join(valid_components)}")
        return v


# Response models (for documentation and validation)

class SessionResponse(BaseModel):
    """Response model for session operations"""
    session_id: str
    status: str
    browser_initialized: bool = False
    controller_initialized: bool = False
    websocket_url: Optional[str] = None
    created_at: str


class AgentTaskResponse(BaseModel):
    """Response model for agent task operations"""
    session_id: str
    task_id: str
    status: str
    task_description: Optional[str] = None
    max_steps: Optional[int] = None


class HealthResponse(BaseModel):
    """Response model for health checks"""
    status: str
    health_score: float
    response_time_ms: float
    timestamp: str
    system: Dict[str, Any]
    resources: Dict[str, Any]
    sessions: Dict[str, Any]
    browser_pool: Dict[str, Any]
    websockets: Dict[str, Any]


class MetricsResponse(BaseModel):
    """Response model for metrics"""
    timestamp: str
    system: Dict[str, Any]
    process: Dict[str, Any]
    application: Dict[str, Any]


class AlertsResponse(BaseModel):
    """Response model for system alerts"""
    timestamp: str
    alert_level: str
    summary: Dict[str, int]
    alerts: List[Dict[str, Any]]
    warnings: List[Dict[str, Any]]
    info: List[Dict[str, Any]]