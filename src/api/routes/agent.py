import asyncio
import logging
import uuid
from typing import Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Depends, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from src.api.models.request_models import (
    AgentRunRequest,
    AgentStopRequest,
    AgentPauseRequest,
    AgentResumeRequest,
    UserHelpResponse,
    SessionCreateRequest
)
from src.core.session_manager import get_session_manager
from src.core.resource_pool import get_resource_pool
from src.core.websocket_manager import get_websocket_manager
from src.utils.llm_provider import get_llm_model
from src.utils.auth import get_client_ip, validate_session_access
from src.config.settings import get_settings

logger = logging.getLogger(__name__)

# Create the router
agent_router = APIRouter(
    prefix="/agent",
    tags=["Agent Management"],
    responses={
        404: {"description": "Session not found"},
        429: {"description": "Rate limit exceeded"},
        500: {"description": "Internal server error"}
    }
)


@agent_router.post("/sessions", response_model=Dict[str, Any])
async def create_session(
    request: SessionCreateRequest,
    client_request: Request,
    background_tasks: BackgroundTasks
) -> Dict[str, Any]:
    """
    Create a new user session.
    
    Returns session_id and session information.
    """
    try:
        client_ip = get_client_ip(client_request)
        session_manager = get_session_manager()
        
        # Create new session
        session_id = await session_manager.create_session(client_ip=client_ip)
        session = await session_manager.get_session(session_id)
        
        if not session:
            raise HTTPException(status_code=500, detail="Failed to create session")
        
        # Initialize browser resources if requested
        if request.initialize_browser:
            browser_config = request.browser_config or {}
            success = await session.initialize_browser(browser_config)
            if not success:
                # Cleanup session if browser initialization failed
                await session_manager.remove_session(session_id, "browser_init_failed")
                raise HTTPException(status_code=500, detail="Failed to initialize browser")
        
        # Initialize controller if requested
        if request.initialize_controller:
            mcp_config = request.mcp_config
            success = await session.initialize_controller(mcp_config)
            if not success:
                logger.warning(f"Controller initialization failed for session {session_id}")
                # Continue anyway, controller is optional
        
        logger.info(f"Created session {session_id} for {client_ip}")
        
        return {
            "session_id": session_id,
            "status": "created",
            "browser_initialized": session.browser is not None,
            "controller_initialized": session.controller is not None,
            "websocket_url": f"/ws/session/{session_id}",
            "created_at": session.created_at.isoformat()
        }
        
    except ValueError as e:
        # Session limit exceeded
        raise HTTPException(status_code=429, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating session: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create session")


@agent_router.get("/sessions/{session_id}", response_model=Dict[str, Any])
async def get_session_status(
    session_id: str,
    client_request: Request
) -> Dict[str, Any]:
    """
    Get session status and information.
    """
    try:
        # Validate session access
        session = await validate_session_access(session_id, client_request)
        
        # Get session status
        status = await session.get_status()
        
        # Get resource information
        resource_pool = get_resource_pool()
        session_resources = await resource_pool.get_session_resources(session_id)
        
        # Get WebSocket status
        websocket_manager = get_websocket_manager()
        ws_connected = await websocket_manager.is_connected(session_id)
        
        return {
            **status,
            "resources": session_resources,
            "websocket_connected": ws_connected
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting session status {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get session status")


@agent_router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    client_request: Request
) -> Dict[str, Any]:
    """
    Delete a session and cleanup all resources.
    """
    try:
        # Validate session access
        await validate_session_access(session_id, client_request)
        
        session_manager = get_session_manager()
        success = await session_manager.remove_session(session_id, "user_request")
        
        if not success:
            raise HTTPException(status_code=404, detail="Session not found")
        
        logger.info(f"Deleted session {session_id}")
        
        return {
            "session_id": session_id,
            "status": "deleted"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete session")


@agent_router.post("/sessions/{session_id}/run", response_model=Dict[str, Any])
async def run_agent_task(
    session_id: str,
    request: AgentRunRequest,
    client_request: Request,
    background_tasks: BackgroundTasks
) -> Dict[str, Any]:
    """
    Start an agent task for a session.
    """
    try:
        # Validate session access
        session = await validate_session_access(session_id, client_request)
        
        # Check if session is ready
        if not session.browser or not session.browser_context:
            raise HTTPException(
                status_code=400, 
                detail="Session browser not initialized. Create session with initialize_browser=true"
            )
        
        # Check if agent is already running
        if session.current_task and not session.current_task.done():
            raise HTTPException(status_code=409, detail="Agent task already running")
        
        # Initialize LLM
        try:
            llm = get_llm_model(
                provider=request.llm_provider,
                model_name=request.llm_model_name,
                temperature=request.llm_temperature,
                base_url=request.llm_base_url,
                api_key=request.llm_api_key,
                num_ctx=request.ollama_num_ctx if request.llm_provider == "ollama" else None
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to initialize LLM: {str(e)}")
        
        # Prepare agent configuration
        agent_config = {
            'use_vision': request.use_vision,
            'max_actions_per_step': request.max_actions_per_step,
            'max_steps': request.max_steps,
            'tool_calling_method': request.tool_calling_method
        }
        
        # Start the agent task
        task_id = await session.start_agent_task(
            task_description=request.task,
            llm=llm,
            agent_config=agent_config
        )
        
        logger.info(f"Started agent task {task_id} for session {session_id}")
        
        return {
            "session_id": session_id,
            "task_id": task_id,
            "status": "started",
            "task_description": request.task,
            "max_steps": request.max_steps
        }
        
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error starting agent task for session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to start agent task")


@agent_router.post("/sessions/{session_id}/stop")
async def stop_agent_task(
    session_id: str,
    request: AgentStopRequest,
    client_request: Request
) -> Dict[str, Any]:
    """
    Stop the current agent task for a session.
    """
    try:
        # Validate session access
        session = await validate_session_access(session_id, client_request)
        
        # Stop the agent task
        success = await session.stop_agent_task(reason=request.reason or "user_request")
        
        if not success:
            raise HTTPException(status_code=404, detail="No running agent task found")
        
        logger.info(f"Stopped agent task for session {session_id}")
        
        return {
            "session_id": session_id,
            "status": "stopped",
            "reason": request.reason or "user_request"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error stopping agent task for session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to stop agent task")


@agent_router.post("/sessions/{session_id}/pause")
async def pause_agent_task(
    session_id: str,
    request: AgentPauseRequest,
    client_request: Request
) -> Dict[str, Any]:
    """
    Pause the current agent task for a session.
    """
    try:
        # Validate session access
        session = await validate_session_access(session_id, client_request)
        
        # Pause the agent task
        success = await session.pause_agent_task()
        
        if not success:
            raise HTTPException(status_code=404, detail="No running agent task found or task cannot be paused")
        
        logger.info(f"Paused agent task for session {session_id}")
        
        return {
            "session_id": session_id,
            "status": "paused"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error pausing agent task for session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to pause agent task")


@agent_router.post("/sessions/{session_id}/resume")
async def resume_agent_task(
    session_id: str,
    request: AgentResumeRequest,
    client_request: Request
) -> Dict[str, Any]:
    """
    Resume a paused agent task for a session.
    """
    try:
        # Validate session access
        session = await validate_session_access(session_id, client_request)
        
        # Resume the agent task
        success = await session.resume_agent_task()
        
        if not success:
            raise HTTPException(status_code=404, detail="No paused agent task found")
        
        logger.info(f"Resumed agent task for session {session_id}")
        
        return {
            "session_id": session_id,
            "status": "resumed"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resuming agent task for session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to resume agent task")


@agent_router.post("/sessions/{session_id}/help-response")
async def submit_help_response(
    session_id: str,
    request: UserHelpResponse,
    client_request: Request
) -> Dict[str, Any]:
    """
    Submit a response to an agent's help request.
    """
    try:
        # Validate session access
        session = await validate_session_access(session_id, client_request)
        
        # Submit the help response
        success = await session.submit_help_response(request.response)
        
        if not success:
            raise HTTPException(status_code=404, detail="No pending help request found")
        
        logger.info(f"Submitted help response for session {session_id}")
        
        return {
            "session_id": session_id,
            "status": "response_submitted",
            "response": request.response
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error submitting help response for session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to submit help response")


@agent_router.get("/sessions/{session_id}/chat-history")
async def get_chat_history(
    session_id: str,
    client_request: Request,
    limit: int = 50,
    offset: int = 0
) -> Dict[str, Any]:
    """
    Get chat history for a session.
    """
    try:
        # Validate session access
        session = await validate_session_access(session_id, client_request)
        
        # Get chat history with pagination
        chat_history = session.chat_history[offset:offset + limit]
        total_messages = len(session.chat_history)
        
        return {
            "session_id": session_id,
            "chat_history": chat_history,
            "pagination": {
                "offset": offset,
                "limit": limit,
                "total": total_messages,
                "has_more": offset + limit < total_messages
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting chat history for session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get chat history")


@agent_router.post("/sessions/{session_id}/chat-message")
async def add_chat_message(
    session_id: str,
    message: Dict[str, Any],
    client_request: Request
) -> Dict[str, Any]:
    """
    Add a message to the chat history.
    """
    try:
        # Validate session access
        session = await validate_session_access(session_id, client_request)
        
        # Validate message format
        if "role" not in message or "content" not in message:
            raise HTTPException(status_code=400, detail="Message must have 'role' and 'content' fields")
        
        if message["role"] not in ["user", "assistant", "system"]:
            raise HTTPException(status_code=400, detail="Role must be 'user', 'assistant', or 'system'")
        
        # Add message to chat history
        await session.add_chat_message(
            role=message["role"],
            content=message["content"]
        )
        
        return {
            "session_id": session_id,
            "status": "message_added",
            "message": message
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding chat message for session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to add chat message")


# Utility endpoints

@agent_router.get("/providers")
async def get_llm_providers() -> Dict[str, str]:
    """
    Get available LLM providers.
    """
    from src.utils.config import PROVIDER_DISPLAY_NAMES
    return PROVIDER_DISPLAY_NAMES


@agent_router.get("/providers/{provider}/models")
async def get_provider_models(provider: str) -> Dict[str, Any]:
    """
    Get available models for a specific provider.
    """
    from src.utils.config import model_names
    
    if provider not in model_names:
        raise HTTPException(status_code=404, detail=f"Provider '{provider}' not found")
    
    return {
        "provider": provider,
        "models": model_names[provider]
    }


@agent_router.get("/sessions")
async def list_sessions(
    client_request: Request,
    limit: int = 20,
    offset: int = 0
) -> Dict[str, Any]:
    """
    List sessions (admin endpoint - requires special access).
    Note: In production, this should have proper authentication.
    """
    try:
        settings = get_settings()
        client_ip = get_client_ip(client_request)
        
        # Simple IP-based access control for demo
        if settings.environment == "production" and client_ip not in settings.admin_ips:
            raise HTTPException(status_code=403, detail="Access denied")
        
        session_manager = get_session_manager()
        session_stats = await session_manager.get_statistics()
        
        return {
            "total_sessions": session_stats["current_active_sessions"],
            "statistics": session_stats,
            "note": "Individual session data not included for privacy"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing sessions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list sessions")