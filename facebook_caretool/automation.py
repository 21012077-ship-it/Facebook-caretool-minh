from __future__ import annotations

import json
import os
import random
from typing import Any, Dict, List, Optional

from .utils import parse_proxy

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]


def apply_playwright_stealth(page: Any) -> None:
    try:
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)
        page.add_init_script("""
            Object.defineProperty(navigator, 'languages', {
                get: () => ['vi-VN', 'vi', 'en-US', 'en']
            });
        """)
        page.add_init_script("""
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3]
            });
        """)
        page.add_init_script("""
            if (window.navigator.webdriver === true) {
                Object.defineProperty(window.navigator, 'webdriver', {get: () => false});
            }
        """)
    except Exception as exc:
        print(f"Lỗi khi apply mini stealth: {str(exc)}")


class AutomationService:
    """Các tác vụ Playwright/browser tách khỏi lớp UI."""

    def __init__(self, user_agents: Optional[List[str]] = None) -> None:
        self.user_agents = user_agents or USER_AGENTS

    def normalize_cookie(self, cookie: Dict[str, Any]) -> Dict[str, Any]:
        same_site = str(cookie.get("sameSite", "Lax")).lower()
        mapping = {"no_restriction": "None", "lax": "Lax", "strict": "Strict", "unspecified": "Lax", "none": "None"}
        item = {
            "name": cookie["name"],
            "value": cookie["value"],
            "domain": cookie.get("domain", ".facebook.com"),
            "path": cookie.get("path", "/"),
            "httpOnly": cookie.get("httpOnly", False),
            "secure": cookie.get("secure", True),
            "sameSite": mapping.get(same_site, "Lax"),
        }
        if "expirationDate" in cookie:
            item["expires"] = int(cookie["expirationDate"])
        elif "expires" in cookie:
            try:
                item["expires"] = int(cookie["expires"])
            except (TypeError, ValueError):
                pass
        return item

    def load_cookies(self, account: Dict[str, Any]) -> List[Dict[str, Any]]:
        cookie_file = account.get("cookie_file", "")
        if not cookie_file or not os.path.exists(cookie_file):
            return []
        try:
            with open(cookie_file, "r", encoding="utf-8") as file:
                data = json.load(file)
            cookies_to_process = data["cookies"] if isinstance(data, dict) and "cookies" in data else data
            return [self.normalize_cookie(cookie) for cookie in cookies_to_process]
        except Exception:
            return []

    def parse_proxy(self, proxy_text: str | None) -> Optional[Dict[str, str]]:
        return parse_proxy(proxy_text)

    def create_browser_page(self, playwright: Any, cookies: List[Dict[str, Any]], account: Optional[Dict[str, Any]] = None):
        proxy_config = self.parse_proxy((account or {}).get("proxy", ""))
        launch_options: Dict[str, Any] = {
            "channel": "chrome",
            "headless": False,
            "slow_mo": 200,
            "args": [
                "--disable-extensions",
                "--disable-features=Translate",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--ignore-certificate-errors",
            ],
        }
        if proxy_config:
            launch_options["proxy"] = proxy_config

        browser = playwright.chromium.launch(**launch_options)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=random.choice(self.user_agents),
        )
        if cookies:
            context.add_cookies(cookies)
        page = context.new_page()
        apply_playwright_stealth(page)
        return browser, context, page
