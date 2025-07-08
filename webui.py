# webui.py - Final version with correct live logging

import logging
import asyncio
import os
import base64
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

from dotenv import load_dotenv
load_dotenv()

from src.utils import utils
from src.utils.utils import MissingAPIKeyError
from src.agent.custom_agent import CustomAgent
# *** FIX: Import the specific logger instance from the custom_agent module ***
from src.agent.custom_agent import logger as agent_logger 
from src.browser.custom_browser import CustomBrowser
from src.browser.custom_context import BrowserContextConfig
from src.controller.custom_controller import CustomController
from src.agent.custom_prompts import CustomSystemPrompt, CustomAgentMessagePrompt
from browser_use.browser.browser import BrowserConfig
from browser_use.browser.context import BrowserContextWindowSize

# Configure the main logger for this file
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global state
_global_browser_context = None
_global_agent_task = None
active_websockets: list[WebSocket] = []

app = FastAPI()

# Mount the 'static' directory to serve HTML, CSS, JS files
app.mount("/static", StaticFiles(directory="static"), name="static")

class AgentRunRequest(BaseModel):
    task: str
    llm_provider: str = "google"
    llm_model_name: str | None = None
    llm_temperature: float = 0.6
    llm_base_url: str | None = None
    llm_api_key: str | None = None

async def send_socket_message(message: dict):
    """Helper to send a JSON message to all active WebSockets."""
    for websocket in active_websockets:
        try:
            await websocket.send_text(json.dumps(message))
        except WebSocketDisconnect:
            pass

async def run_agent_logic(config: AgentRunRequest):
    global _global_browser_context
    browser = None
    original_info_handler = agent_logger.info # Store the original logger function
    
    try:
        # Define the function that will send logs to the websocket
        def log_to_socket(msg, *args, **kwargs):
            try:
                log_message = msg % args
                asyncio.create_task(send_socket_message({"type": "log", "data": log_message}))
            except Exception as e:
                # If formatting fails, send the raw message
                asyncio.create_task(send_socket_message({"type": "log", "data": str(msg)}))

        # *** FIX: Monkey-patch the correct logger instance ***
        agent_logger.info = log_to_socket

        env_var_name = f"{config.llm_provider.upper()}_API_KEY"
        llm_api_key = config.llm_api_key or os.getenv(env_var_name)
        
        if not llm_api_key:
            raise MissingAPIKeyError(provider=config.llm_provider, env_var=env_var_name)

        model_name = config.llm_model_name or utils.model_names.get(config.llm_provider, [""])[0]

        llm = utils.get_llm_model(
            provider=config.llm_provider, model_name=model_name,
            temperature=config.llm_temperature, base_url=config.llm_base_url, api_key=llm_api_key
        )
        
        await send_socket_message({"type": "log", "data": "Browser starting..."})
        browser = CustomBrowser(config=BrowserConfig(headless=False))
        _global_browser_context = await browser.new_context(
            config=BrowserContextConfig(
                no_viewport=False, browser_window_size=BrowserContextWindowSize(width=1280, height=720)
            )
        )

        agent = CustomAgent(
            task=config.task, llm=llm, browser=browser, browser_context=_global_browser_context,
            controller=CustomController(), system_prompt_class=CustomSystemPrompt,
            agent_prompt_class=CustomAgentMessagePrompt
        )
        
        # This log will now also be sent to the socket via the patched logger
        agent_logger.info(f"Agent starting for task: {config.task}")
        history = await agent.run(max_steps=100)
        
        final_result = history.final_result()
        await send_socket_message({"type": "result", "data": final_result})
        logger.info(f"✅ Agent finished. Final result: {final_result}")

    except Exception as e:
        logger.error(f"An error occurred during agent execution: {e}", exc_info=True)
        await send_socket_message({"type": "error", "data": str(e)})
    finally:
        # *** FIX: Restore the original logger function ***
        agent_logger.info = original_info_handler

        if _global_browser_context:
            await _global_browser_context.close()
            _global_browser_context = None
        if browser:
            await browser.close()
        
        await send_socket_message({"type": "log", "data": "Session closed."})
        logger.info("Browser session closed.")

async def stream_browser_view():
    """Periodically captures and sends screenshots."""
    while _global_agent_task and not _global_agent_task.done():
        if _global_browser_context and hasattr(_global_browser_context, 'browser') and _global_browser_context.browser:
            playwright_browser = _global_browser_context.browser.playwright_browser
            if playwright_browser and playwright_browser.contexts:
                pw_context = playwright_browser.contexts[0]
                if pw_context and pw_context.pages:
                    try:
                        page = next((p for p in reversed(pw_context.pages) if p.url != "about:blank"), None)
                        if page and not page.is_closed():
                            screenshot_bytes = await page.screenshot(type="jpeg", quality=70)
                            b64_img = base64.b64encode(screenshot_bytes).decode('utf-8')
                            await send_socket_message({"type": "stream", "data": b64_img})
                    except Exception:
                        pass
        await asyncio.sleep(0.5)

# --- API Endpoints ---
@app.get("/", response_class=FileResponse)
async def read_index():
    return FileResponse('static/index.html')

@app.get("/api/providers", response_class=JSONResponse)
async def get_providers():
    return utils.PROVIDER_DISPLAY_NAMES

@app.post("/agent/run")
async def start_agent_run(request: AgentRunRequest):
    global _global_agent_task
    if _global_agent_task and not _global_agent_task.done():
        return JSONResponse(status_code=409, content={"status": "An agent is already running."})
        
    _global_agent_task = asyncio.create_task(run_agent_logic(request))
    return {"status": "Agent run started.", "task": request.task}

@app.websocket("/ws/stream")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_websockets.append(websocket)
    streamer_task = None
    try:
        await asyncio.sleep(1) 
        if _global_agent_task and not _global_agent_task.done():
             streamer_task = asyncio.create_task(stream_browser_view())
        
        while _global_agent_task and not _global_agent_task.done():
            await asyncio.sleep(1)

    except WebSocketDisconnect:
        logger.info("Client disconnected.")
    finally:
        if streamer_task:
            streamer_task.cancel()
        if websocket in active_websockets:
            active_websockets.remove(websocket)

# --- Main Execution ---
def main():
    uvicorn.run(app, host="127.0.0.1", port=7788)

if __name__ == '__main__':
    main()