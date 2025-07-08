import asyncio
import base64
import json
import logging
from typing import Dict, Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Depends, Query
from fastapi.responses import JSONResponse

from src.core.session_manager import get_session_manager
from src.core.websocket_manager import get_websocket_manager, MessageType
from src.utils.auth import get_client_ip_from_websocket

logger = logging.getLogger(__name__)

# Create the router
websocket_router = APIRouter(
    tags=["WebSocket Communication"]
)


@websocket_router.websocket("/session/{session_id}")
async def websocket_session_endpoint(
    websocket: WebSocket,
    session_id: str,
    user_agent: Optional[str] = Query(None, alias="user-agent")
):
    """
    WebSocket endpoint for real-time communication with a specific session.
    
    This handles:
    - Agent step updates and screenshots
    - Chat messages
    - Help requests from agents
    - Browser streaming (if enabled)
    - Session status updates
    """
    client_ip = get_client_ip_from_websocket(websocket)
    
    try:
        # Get managers
        session_manager = get_session_manager()
        websocket_manager = get_websocket_manager()
        
        # Verify session exists and is accessible
        session = await session_manager.get_session(session_id)
        if not session:
            await websocket.close(code=4004, reason="Session not found")
            return
        
        # Verify session belongs to this IP (basic security)
        if session.client_ip and session.client_ip != client_ip:
            logger.warning(f"IP mismatch for session {session_id}: {client_ip} vs {session.client_ip}")
            await websocket.close(code=4003, reason="Access denied")
            return
        
        # Connect to WebSocket manager
        success = await websocket_manager.connect(
            websocket=websocket,
            session_id=session_id,
            client_ip=client_ip,
            user_agent=user_agent
        )
        
        if not success:
            await websocket.close(code=4001, reason="Connection failed")
            return
        
        # Set WebSocket in session
        await session.set_websocket(websocket)
        
        logger.info(f"WebSocket connected for session {session_id} from {client_ip}")
        
        # Start browser streaming task if needed
        streaming_task = None
        if session.browser_context and not session.browser_context.config.headless:
            streaming_task = asyncio.create_task(
                _stream_browser_view(session_id, session)
            )
        
        # Handle WebSocket communication
        await websocket_manager.handle_websocket_communication(session_id)
        
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for session {session_id}")
    except Exception as e:
        logger.error(f"WebSocket error for session {session_id}: {e}", exc_info=True)
        try:
            await websocket.close(code=4000, reason="Internal error")
        except:
            pass
    finally:
        # Cleanup
        try:
            # Cancel streaming task
            if 'streaming_task' in locals() and streaming_task and not streaming_task.done():
                streaming_task.cancel()
                try:
                    await streaming_task
                except asyncio.CancelledError:
                    pass
            
            # Remove WebSocket from session
            if 'session' in locals() and session:
                await session.remove_websocket()
            
            # Disconnect from WebSocket manager
            if 'websocket_manager' in locals():
                await websocket_manager.disconnect(session_id, reason="websocket_closed")
            
        except Exception as e:
            logger.error(f"Error during WebSocket cleanup for session {session_id}: {e}")


async def _stream_browser_view(session_id: str, session):
    """
    Stream browser screenshots to the WebSocket client.
    Only runs for non-headless browsers.
    """
    try:
        websocket_manager = get_websocket_manager()
        
        while True:
            # Check if session and browser context are still valid
            if not session or not session.browser_context:
                break
            
            # Check if WebSocket is still connected
            if not await websocket_manager.is_connected(session_id):
                break
            
            try:
                # Take screenshot
                screenshot_b64 = await session.browser_context.take_screenshot()
                
                if screenshot_b64:
                    # Send screenshot via WebSocket
                    await websocket_manager.send_to_session(session_id, {
                        "type": MessageType.BROWSER_STREAM.value,
                        "data": {
                            "screenshot": screenshot_b64,
                            "url": await _get_current_url(session),
                            "timestamp": asyncio.get_event_loop().time()
                        }
                    })
                
            except Exception as e:
                logger.debug(f"Screenshot capture failed for session {session_id}: {e}")
                # Send status message instead
                await websocket_manager.send_to_session(session_id, {
                    "type": MessageType.BROWSER_STATUS.value,
                    "data": {"status": "screenshot_unavailable", "error": str(e)}
                })
            
            # Wait before next screenshot
            await asyncio.sleep(0.5)  # 2 FPS for browser streaming
            
    except asyncio.CancelledError:
        logger.debug(f"Browser streaming cancelled for session {session_id}")
    except Exception as e:
        logger.error(f"Browser streaming error for session {session_id}: {e}", exc_info=True)


async def _get_current_url(session) -> Optional[str]:
    """Get the current URL from the browser context"""
    try:
        if session.browser_context:
            # Get current page URL
            browser = session.browser_context.browser
            if browser and browser.playwright_browser:
                contexts = browser.playwright_browser.contexts
                if contexts and contexts[0].pages:
                    # Get the most recent page
                    page = contexts[0].pages[-1]
                    return page.url
        return None
    except Exception:
        return None


# Message handlers for WebSocket manager

async def handle_user_message(session_id: str, message: Dict[str, Any]):
    """Handle incoming user messages"""
    try:
        session_manager = get_session_manager()
        session = await session_manager.get_session(session_id)
        
        if not session:
            return
        
        message_type = message.get("type")
        data = message.get("data", {})
        
        if message_type == "chat_message":
            # Add user message to chat history
            await session.add_chat_message(
                role="user",
                content=data.get("content", "")
            )
            
        elif message_type == "agent_command":
            # Handle agent control commands
            command = data.get("command")
            
            if command == "pause":
                await session.pause_agent_task()
            elif command == "resume":
                await session.resume_agent_task()
            elif command == "stop":
                await session.stop_agent_task(reason="user_command")
                
        elif message_type == "help_response":
            # Handle help response from user
            response = data.get("response", "")
            await session.submit_help_response(response)
            
        # Update session activity
        session.update_activity()
        
    except Exception as e:
        logger.error(f"Error handling user message for session {session_id}: {e}", exc_info=True)


async def handle_system_message(session_id: str, message: Dict[str, Any]):
    """Handle system messages"""
    try:
        websocket_manager = get_websocket_manager()
        
        message_type = message.get("type")
        data = message.get("data", {})
        
        if message_type == "session_update":
            # Send session status update
            session_manager = get_session_manager()
            session = await session_manager.get_session(session_id)
            
            if session:
                status = await session.get_status()
                await websocket_manager.send_to_session(session_id, {
                    "type": MessageType.SESSION_STATUS.value,
                    "data": status
                })
                
    except Exception as e:
        logger.error(f"Error handling system message for session {session_id}: {e}", exc_info=True)


# Register message handlers with WebSocket manager
def register_websocket_handlers():
    """Register message handlers with the WebSocket manager"""
    try:
        websocket_manager = get_websocket_manager()
        
        # Register handlers for different message types
        websocket_manager.register_message_handler("chat_message", handle_user_message)
        websocket_manager.register_message_handler("agent_command", handle_user_message)
        websocket_manager.register_message_handler("help_response", handle_user_message)
        websocket_manager.register_message_handler("system_message", handle_system_message)
        
        logger.info("WebSocket message handlers registered")
        
    except Exception as e:
        logger.error(f"Error registering WebSocket handlers: {e}", exc_info=True)


# Broadcast endpoints (for admin use)

@websocket_router.post("/broadcast/all")
async def broadcast_to_all_sessions(
    message: Dict[str, Any]
) -> JSONResponse:
    """
    Broadcast a message to all connected sessions.
    Note: In production, this should require admin authentication.
    """
    try:
        websocket_manager = get_websocket_manager()
        
        # Add timestamp and source
        broadcast_message = {
            **message,
            "broadcast": True,
            "timestamp": asyncio.get_event_loop().time()
        }
        
        sent_count = await websocket_manager.broadcast_to_all(broadcast_message)
        
        return JSONResponse(content={
            "status": "sent",
            "recipients": sent_count,
            "message": broadcast_message
        })
        
    except Exception as e:
        logger.error(f"Error broadcasting to all sessions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to broadcast message")


@websocket_router.post("/broadcast/sessions")
async def broadcast_to_specific_sessions(
    session_ids: list[str],
    message: Dict[str, Any]
) -> JSONResponse:
    """
    Broadcast a message to specific sessions.
    Note: In production, this should require admin authentication.
    """
    try:
        websocket_manager = get_websocket_manager()
        
        # Add timestamp and source
        broadcast_message = {
            **message,
            "broadcast": True,
            "timestamp": asyncio.get_event_loop().time(),
            "target_sessions": len(session_ids)
        }
        
        sent_count = await websocket_manager.broadcast_to_sessions(session_ids, broadcast_message)
        
        return JSONResponse(content={
            "status": "sent",
            "target_sessions": len(session_ids),
            "successful_sends": sent_count,
            "message": broadcast_message
        })
        
    except Exception as e:
        logger.error(f"Error broadcasting to specific sessions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to broadcast message")


# WebSocket status endpoints

@websocket_router.get("/status")
async def get_websocket_status() -> Dict[str, Any]:
    """Get WebSocket manager status and statistics"""
    try:
        websocket_manager = get_websocket_manager()
        stats = await websocket_manager.get_statistics()
        
        return {
            "websocket_manager": "active",
            "statistics": stats,
            "message_types": [msg_type.value for msg_type in MessageType]
        }
        
    except Exception as e:
        logger.error(f"Error getting WebSocket status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get WebSocket status")


@websocket_router.get("/connections")
async def get_active_connections() -> Dict[str, Any]:
    """
    Get information about active WebSocket connections.
    Note: In production, this should require admin authentication.
    """
    try:
        websocket_manager = get_websocket_manager()
        connections = await websocket_manager.get_all_connections()
        
        # Remove sensitive information
        safe_connections = []
        for conn in connections:
            safe_conn = {
                "session_id": conn["session_id"],
                "connected_at": conn["connected_at"],
                "last_activity": conn["last_activity"],
                "state": conn["state"],
                "message_count": conn["message_count"],
                "duration_seconds": conn["duration_seconds"]
            }
            safe_connections.append(safe_conn)
        
        return {
            "total_connections": len(safe_connections),
            "connections": safe_connections
        }
        
    except Exception as e:
        logger.error(f"Error getting active connections: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get active connections")


@websocket_router.get("/session/{session_id}/connection")
async def get_session_connection_info(session_id: str) -> Dict[str, Any]:
    """Get WebSocket connection info for a specific session"""
    try:
        websocket_manager = get_websocket_manager()
        connection_info = await websocket_manager.get_connection_info(session_id)
        
        if not connection_info:
            raise HTTPException(status_code=404, detail="Session connection not found")
        
        # Remove sensitive information
        safe_info = {
            "session_id": connection_info["session_id"],
            "connected_at": connection_info["connected_at"],
            "last_activity": connection_info["last_activity"],
            "state": connection_info["state"],
            "message_count": connection_info["message_count"],
            "duration_seconds": connection_info["duration_seconds"]
        }
        
        return safe_info
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting session connection info {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get connection info")


# Initialize handlers on module import
# This will be called when the module is imported by the main app
def init_websocket_routes():
    """Initialize WebSocket routes and handlers"""
    register_websocket_handlers()
    logger.info("WebSocket routes initialized")