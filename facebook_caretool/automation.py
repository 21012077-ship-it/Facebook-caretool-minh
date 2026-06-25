from __future__ import annotations

import json
import os
import random
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .utils import parse_proxy

USER_AGENTS = [
    # Chrome 131-136 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 131-136 macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    # Edge 135-136
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0",
    # Safari macOS 17.x
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


def apply_playwright_stealth(page: Any) -> None:
    try:
        # 1. Ẩn webdriver flag
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        # 2. Languages phù hợp Vietnamese user
        page.add_init_script("""
            Object.defineProperty(navigator, 'languages', {
                get: () => ['vi-VN', 'vi', 'en-US', 'en']
            });
        """)
        # 3. Plugins giả lập giống trình duyệt thật (có .item(), .namedItem(), .refresh())
        page.add_init_script("""
            const makePlugin = (name, desc, filename, mimeTypes) => ({
                name, description: desc, filename,
                length: mimeTypes.length,
                item: (i) => mimeTypes[i] || null,
                namedItem: (n) => mimeTypes.find(m => m.type === n) || null,
            });
            const plugins = [
                makePlugin('PDF Viewer', 'Portable Document Format', 'internal-pdf-viewer', []),
                makePlugin('Chrome PDF Viewer', 'Portable Document Format', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', []),
                makePlugin('Chromium PDF Viewer', 'Portable Document Format', 'internal-pdf-viewer', []),
            ];
            Object.defineProperty(navigator, 'plugins', {
                get: () => Object.assign(plugins, { length: plugins.length, item: i => plugins[i], namedItem: n => plugins.find(p => p.name === n), refresh: () => {} })
            });
        """)
        # 4. window.chrome – cần có để qua fingerprint check cơ bản
        page.add_init_script("""
            if (!window.chrome) {
                window.chrome = { runtime: {}, loadTimes: () => null, csi: () => null, app: {} };
            }
        """)
        # 5. Notifications permission behavior
        page.add_init_script("""
            const origQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) =>
                parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : origQuery(parameters);
        """)
        # 6. Hardware concurrency & deviceMemory realistic values
        page.add_init_script("""
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            if ('deviceMemory' in navigator) {
                Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
            }
        """)
    except Exception as exc:
        print(f"Lỗi khi apply stealth: {str(exc)}")


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
        except json.JSONDecodeError as exc:
            print(f"[WARN] Cookie file bị corrupt ({cookie_file}): {exc}")
            return []
        except OSError as exc:
            print(f"[WARN] Không đọc được cookie file ({cookie_file}): {exc}")
            return []
        except Exception as exc:
            print(f"[WARN] Lỗi không xác định khi load cookie ({cookie_file}): {exc}")
            return []

    def build_cookie_path(self, account: Dict[str, Any], cookie_dir: str = "cookies") -> str:
        cookie_file = str(account.get("cookie_file") or "").strip()
        if cookie_file:
            return cookie_file

        raw_name = str(account.get("uid") or account.get("name") or "facebook_account").strip()
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_name).strip("._") or "facebook_account"
        return os.path.join(cookie_dir, f"{safe_name}.json")

    def save_cookies(self, account: Dict[str, Any], cookies: List[Dict[str, Any]], cookie_dir: str = "cookies") -> str:
        cookie_file = self.build_cookie_path(account, cookie_dir=cookie_dir)
        parent_dir = os.path.dirname(cookie_file)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        import json as _json, tempfile as _tempfile
        payload = _json.dumps(cookies, indent=4, ensure_ascii=False)
        target_path = cookie_file
        target_dir = os.path.dirname(os.path.abspath(target_path)) or "."
        try:
            with _tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target_dir, delete=False, suffix=".tmp") as tf:
                temp_name = tf.name
                tf.write(payload)
            os.replace(temp_name, target_path)
        except Exception:
            try:
                import pathlib
                pathlib.Path(temp_name).unlink(missing_ok=True)
            except Exception:
                pass
            raise

        account["cookie_file"] = cookie_file
        return cookie_file


    def is_real_facebook_checkpoint_url(self, url: str | None) -> bool:
        """Return True only for actual Facebook checkpoint routes.

        Facebook can append ``?checkpoint_src=any`` to the normal homepage after a
        successful login.  That query parameter alone must not be treated as a
        checkpoint.
        """
        if not url:
            return False

        parsed = urlparse(url)
        path = (parsed.path or "/").lower()
        if "checkpoint" in path:
            return True

        return False

    def is_facebook_login_or_security_url(self, url: str | None) -> bool:
        if not url:
            return False

        parsed = urlparse(url)
        path = (parsed.path or "/").lower()
        full_url = url.lower()
        return (
            "login" in path
            or "two_step_verification" in full_url
            or self.is_real_facebook_checkpoint_url(url)
        )

    def is_facebook_success_url(self, url: str | None) -> bool:
        if not url:
            return False

        parsed = urlparse(url)
        host = (parsed.netloc or "").lower().split(":")[0]  # strip port
        is_facebook = host == "facebook.com" or host.endswith(".facebook.com")
        return is_facebook and not self.is_facebook_login_or_security_url(url)


    def looks_like_logged_out_landing_text(self, text: str | None) -> bool:
        """Detect Facebook's logged-out saved-profile landing screen from text.

        This page can live on ``facebook.com`` and has no email input, so URL-only
        checks may incorrectly treat it as an authenticated home page.
        """
        compact = " ".join(str(text or "").lower().split())
        if not compact:
            return False

        continue_terms = ("continue", "tiếp tục", "continuer")
        switch_terms = ("use another profile", "dùng trang cá nhân khác", "sử dụng tài khoản khác", "log into another account")
        create_terms = ("create new account", "tạo tài khoản mới", "créer un compte")
        brand_terms = ("meta", "facebook")

        return (
            any(term in compact for term in continue_terms)
            and (any(term in compact for term in switch_terms) or any(term in compact for term in create_terms))
            and any(term in compact for term in brand_terms)
        )

    def has_facebook_login_cookie(self, cookies: List[Dict[str, Any]]) -> bool:
        return any(
            cookie.get("name") == "c_user" and "facebook.com" in str(cookie.get("domain", ""))
            for cookie in cookies
        )

    def parse_proxy(self, proxy_text: str | None) -> Optional[Dict[str, str]]:
        return parse_proxy(proxy_text)

    def create_browser_page(self, playwright: Any, cookies: List[Dict[str, Any]], account: Optional[Dict[str, Any]] = None):
        proxy_config = self.parse_proxy((account or {}).get("proxy", ""))
        launch_options: Dict[str, Any] = {
            "channel": "chrome",
            "headless": False,
            "slow_mo": 30,
            "args": [
                "--disable-extensions",
                "--disable-features=Translate",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--ignore-certificate-errors",
                # Giảm lag proxy
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
                "--metrics-recording-only",
                "--no-first-run",
                # ➡ Giảm CPU mạnh: tắt GPU rendering (dùng software rasterizer nhẹ hơn)
                "--disable-gpu",
                "--disable-gpu-compositing",
                # "--disable-software-rasterizer", # Removed because it can cause crashes when GPU is also disabled
                # Tắt WebGL và hiệu ứng 3D — Facebook không cần
                "--disable-webgl",
                "--disable-webgl2",
                # Tắt các render worker nguồn CPU nguồn
                "--disable-accelerated-2d-canvas",
                "--disable-accelerated-jpeg-decoding",
                "--disable-accelerated-video-decode",
                # Tắt các tính năng hạt nhân nặng không cần thiết
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-ipc-flooding-protection",
                # Giảm process
                "--process-per-site",
                "--disable-hang-monitor",
            ],
        }
        if proxy_config:
            launch_options["proxy"] = proxy_config

        browser = playwright.chromium.launch(**launch_options)
        try:
            context = browser.new_context(
                viewport={"width": 900, "height": 650},  # nhỏ hơn = ít pixel render hơn
                user_agent=random.choice(self.user_agents),
                # Tắt animation/transition — giảm JS timer và paint cycle của Facebook
                reduced_motion="reduce",
            )
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()

            # Chặn resource nặng qua proxy: font, tracker, media, stylesheet không cần thiết
            _BLOCKED_RESOURCE_TYPES = {"font", "media"}
            _BLOCKED_DOMAINS = (
                "fonts.googleapis.com", "fonts.gstatic.com",
                "connect.facebook.net/signals", "pixel.facebook.com",
                "analytics.facebook.com", "graph.facebook.com/logging",
                "graph.facebook.com/logger",
                "www.google-analytics.com",
            )
            def _route_handler(route):
                try:
                    req = route.request
                    rtype = req.resource_type
                    url = req.url
                    if rtype in _BLOCKED_RESOURCE_TYPES:
                        route.abort()
                        return
                    if any(d in url for d in _BLOCKED_DOMAINS):
                        route.abort()
                        return
                    route.continue_()
                except Exception:
                    pass

            page.route("**/*", _route_handler)

            # Timeout 30s — đủ cho proxy; 15s có thể làm nhiều selector time out giữa trang
            page.set_default_timeout(30000)

            apply_playwright_stealth(page)
            return browser, context, page
        except Exception:
            try:
                browser.close()
            except Exception:
                pass
            raise


