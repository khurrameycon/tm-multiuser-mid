import pyperclip
from typing import Optional, Type, Callable, Any, Dict, Awaitable
from pydantic import BaseModel
from browser_use.agent.views import ActionResult
from browser_use.browser.context import BrowserContext
from browser_use.controller.service import Controller
import logging

logger = logging.getLogger(__name__)

# A type hint for the async callback function we are adding
AskCallback = Callable[[str, Any], Awaitable[Dict[str, Any]]]


class CustomController(Controller):
    def __init__(self,
                 exclude_actions: list[str] = [],
                 output_model: Optional[Type[BaseModel]] = None,
                 ask_assistant_callback: AskCallback | None = None,
                 **kwargs):
        """
        The __init__ method is updated to accept the 'ask_assistant_callback'.
        """
        # Pass original arguments to the parent class
        super().__init__(exclude_actions=exclude_actions, output_model=output_model, **kwargs)
        # Store our new custom callback
        self.ask_assistant_callback = ask_assistant_callback
        # Register your custom actions
        self._register_custom_actions()

    async def ask_assistant(self, query: str, browser_context) -> dict[str, Any]:
        """
        This method is added to handle the agent's help requests.

        It checks if a session-specific callback exists. If so, it uses it.
        Otherwise, it reverts to the default behavior.
        """
        if self.ask_assistant_callback:
            # Use the callback provided by the session
            return await self.ask_assistant_callback(query, browser_context)
        else:
            # Fall back to the default behavior if no callback is set
            return await super().ask_assistant(query, browser_context)

    def _register_custom_actions(self):
        """
        Your existing custom actions are preserved here.
        """
        @self.registry.action("Copy text to clipboard")
        def copy_to_clipboard(text: str):
            pyperclip.copy(text)
            return ActionResult(extracted_content=text)

        @self.registry.action("Paste text from clipboard")
        async def paste_from_clipboard(browser: BrowserContext):
            text = pyperclip.paste()
            page = await browser.get_current_page()
            await page.keyboard.type(text)
            return ActionResult(extracted_content=text)