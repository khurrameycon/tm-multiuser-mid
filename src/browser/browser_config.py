# src/browser/browser_config.py

from dataclasses import dataclass, field
from typing import Optional, List, Dict

@dataclass
class BrowserConfig:
    """Configuration for a single browser instance."""
    user_agent: Optional[str] = None
    headless: bool = True
    proxy: Optional[str] = None
    # Add the two fields below
    extra_browser_args: List[str] = field(default_factory=list)
    disable_security: bool = True 
    viewport: Dict[str, int] = field(default_factory=lambda: {"width": 1920, "height": 1080})
    download_path: Optional[str] = None
    user_data_dir: Optional[str] = None

@dataclass
class PlaywrightConfig:
    """Configuration for Playwright."""
    browser: str = "chromium"
    timeout: int = 30000  # 30 seconds
    navigation_timeout: int = 60000 # 60 seconds