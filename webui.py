# webui.py - Final version that works with the multi-user backend

import logging
import asyncio
import json
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn
from dotenv import load_dotenv

from src.utils import utils

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration ---
BACKEND_API_URL = "http://0.0.0.0:8000"

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- Pydantic Models ---
class AgentRunRequest(BaseModel):
    task: str
    llm_provider: str = "google"
    llm_model_name: str | None = None
    llm_temperature: float = 0.6
    llm_base_url: str | None = None

# --- Helper Functions ---
async def send_socket_message(message: dict):
    """Sends a JSON message to all connected WebSocket clients."""
    for websocket in list(active_websockets):
        try:
            await websocket.send_text(json.dumps(message))
        except (WebSocketDisconnect, RuntimeError):
            active_websockets.remove(websocket)

# This will store active connections
active_websockets: list[WebSocket] = []


# --- API Endpoints for the Frontend Server ---
@app.get("/", response_class=FileResponse)
async def read_index():
    return FileResponse('static/index.html')

@app.get("/api/providers", response_class=JSONResponse)
async def get_providers():
    return utils.PROVIDER_DISPLAY_NAMES

# In webui.py, replace the existing start_agent_run function with this one.

# In webui.py, replace the existing start_agent_run function

# In webui.py, replace the existing start_agent_run function

@app.post("/agent/run")
async def start_agent_run(request: AgentRunRequest):
    """
    Handles the agent run request from the UI by validating the model name
    and sending the complete, correct payload to the backend.
    """
    logger.info(f"Received task: '{request.task}'. Initiating session with backend.")
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            # --- STEP 1: Create a new session ---
            create_session_url = f"{BACKEND_API_URL}/api/v1/agent/sessions"
            session_payload = {"initialize_browser": True, "initialize_controller": True}
            
            await send_socket_message({"type": "log", "data": "Requesting new session from backend..."})
            session_response = await client.post(create_session_url, json=session_payload)
            session_response.raise_for_status()
            
            session_data = session_response.json()
            session_id = session_data.get("session_id")
            
            if not session_id:
                raise HTTPException(status_code=500, detail="Backend did not return a valid session_id.")
            
            await send_socket_message({"type": "log", "data": f"✅ Session created: {session_id}"})

            # --- STEP 2: Run the agent task with a valid model name ---
            run_task_url = f"{BACKEND_API_URL}/api/v1/agent/sessions/{session_id}/run"
            
            # *** FIX: Ensure a model name is always provided ***
            model_name = request.llm_model_name
            if not model_name:
                # If no model is selected in the UI, use the first available model for the provider as a default.
                model_name = utils.model_names.get(request.llm_provider, [""])[0]

            if not model_name:
                # If there's still no model, we cannot proceed.
                raise HTTPException(status_code=400, detail=f"No default model available for provider '{request.llm_provider}'. Please select a model in the UI.")

            task_payload = {
                "task": request.task,
                "llm_provider": request.llm_provider,
                "llm_model_name": model_name, # Using the validated model name
                "llm_temperature": request.llm_temperature,
                "llm_base_url": request.llm_base_url,
                "llm_api_key": None,
                "use_vision": True,
                "max_actions_per_step": 10,
                "max_steps": 100,
                "tool_calling_method": "auto"
            }
            
            await send_socket_message({"type": "log", "data": f"Starting task '{request.task}'..."})
            run_response = await client.post(run_task_url, json=task_payload)
            run_response.raise_for_status()

            run_data = run_response.json()
            
            return JSONResponse(status_code=200, content={
                "status": "Agent run successfully started on backend.",
                "session_id": session_id,
                "task_id": run_data.get("task_id")
            })

        except httpx.RequestError as e:
            error_message = f"Could not connect to backend API at {e.request.url}. Is it running?"
            logger.error(error_message, exc_info=True)
            await send_socket_message({"type": "error", "data": error_message})
            return JSONResponse(status_code=502, content={"error": error_message, "details": str(e)})
        except httpx.HTTPStatusError as e:
            error_message = f"Backend returned an error: {e.response.status_code} {e.response.text}"
            logger.error(error_message, exc_info=True)
            await send_socket_message({"type": "error", "data": error_message})
            return JSONResponse(status_code=e.response.status_code, content={"error": error_message})
        except Exception as e:
            logger.error(f"An unexpected error occurred: {e}", exc_info=True)
            await send_socket_message({"type": "error", "data": "An unexpected error occurred."})
            return JSONResponse(status_code=500, content={"error": str(e)})

@app.websocket("/ws/stream")
async def websocket_endpoint(websocket: WebSocket):
    """Handles WebSocket connections from the browser client."""
    await websocket.accept()
    active_websockets.append(websocket)
    logger.info("Browser client connected via WebSocket.")
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info("Browser client disconnected.")
    finally:
        if websocket in active_websockets:
            active_websockets.remove(websocket)

# --- Main Execution ---
def main():
    logger.info("Starting Web UI server on http://0.0.0.0:7788")
    uvicorn.run(app, host="0.0.0.0", port=7788, log_level="info")

if __name__ == '__main__':
    main()