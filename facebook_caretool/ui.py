from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import customtkinter as ctk
from tkinter import filedialog, messagebox
from playwright.sync_api import sync_playwright

from .account_io import (
    backup_accounts_file,
    load_full_backup_file,
    load_import_accounts,
    merge_accounts,
    parse_bulk_account_lines,
    persist_imported_cookie_files,
    restore_full_backup,
    save_export_file,
    save_full_backup_file,
)
from .analytics import summarize_accounts, summarize_logs
from .automation import AutomationService
from .care_planner import CARE_PROFILE_LABELS, build_care_plan, format_care_plan, profile_label
from .models import is_proxy_action_locked, mark_proxy_changed, proxy_lock_remaining_label, proxy_lock_until_label
from .storage import JsonStorage
from .utils import (
    build_ai_comment_prompt,
    build_comment_payloads,
    generate_totp_code,
    load_json,
    validate_ai_comment,
    random_delay,
    save_json,
    spin_content,
)
from .fb_scraper import (
    find_main_post_element,
    expand_post_text,
    build_full_post_context,
    scan_reply_target as fb_scan_reply_target,
    scan_and_click_reply_target as fb_scan_and_click_reply_target,
    find_first_comment_text,
    extract_post_images,
    download_post_images_as_base64,
)
import os
import threading
import time
import random
import re
from datetime import datetime
ACCOUNTS_FILE = "accounts.json"
LOGS_FILE = "logs.json"
DEFAULT_COMMENT_CONTENT = ""
ACCOUNT_RENDER_BATCH_SIZE = 15  # render từng lô nhỏ để không đứng UI
BROWSER_RENDER_BATCH_SIZE = 80
HISTORY_RENDER_BATCH_SIZE = 60
DEFAULT_CHATGPT_PROFILE_DIR = "chatgpt_profile"


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class FacebookCareTool(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Facebook Account Care Tool & Automation")
        self.geometry("1400x820")
        self.minsize(1200, 700)

        self.storage = JsonStorage(ACCOUNTS_FILE, LOGS_FILE)
        self.automation_service = AutomationService()
        self.accounts = self.storage.load_accounts()
        self.logs = self.storage.load_logs()

        self.selected_index = None
        self.comment_selected_accounts = set()
        self.comment_image_paths = [] # Lưu danh sách đường dẫn ảnh/video ghép đi kèm từng comment
        self.scanned_post_image_paths = []
        self.task_pause_event = threading.Event()
        self.task_pause_event.set()
        self.task_stop_event = threading.Event()
        self.log_lock = threading.Lock()
        self.chatgpt_browser_lock = threading.Lock()
        self.browser_selected_index = None
        self.app_settings = self.load_json("settings.json", {
            "appearance": "dark",
            "default_home_url": "https://www.facebook.com/",
            "export_sensitive_default": False,
            "import_overwrite_default": False,
            "comment_content": DEFAULT_COMMENT_CONTENT,
            "ai_comment_enabled": True,
            "chatgpt_profile_dir": DEFAULT_CHATGPT_PROFILE_DIR,
        })
        self.comment_content_save_job = None
        self.account_refresh_job = None
        self.account_render_job = None
        self.account_render_generation = 0
        self.history_refresh_job = None
        self.history_render_job = None
        self.history_render_generation = 0
        self.browser_render_job = None
        self.browser_render_generation = 0
        self.is_closing = False
        self.playwright_sessions = {}
        self.playwright_sessions_lock = threading.Lock()
        self.protocol("WM_DELETE_WINDOW", self.on_app_close)

        self.build_ui()
        self.refresh_accounts()

    def load_json(self, path, default):
        return load_json(path, default)

    def save_json(self, path, data):
        save_json(path, data)

    def schedule_comment_content_save(self, event=None):
        if self.comment_content_save_job:
            self.after_cancel(self.comment_content_save_job)
        self.comment_content_save_job = self.after(500, self.save_comment_content)

    def save_comment_content(self, event=None, show_message=False):
        if not hasattr(self, "comment_content"):
            return
        self.comment_content_save_job = None
        self.app_settings["comment_content"] = self.comment_content.get("1.0", "end-1c")
        self.save_json("settings.json", self.app_settings)
        if show_message:
            messagebox.showinfo("Đã lưu", "Đã lưu nội dung comment.")

    def on_app_close(self):
        """Dừng task và đóng các phiên Playwright trước khi thoát app.

        Nếu app bị destroy trong lúc Chrome/Playwright còn đang gửi event về
        Python, tiến trình driver Node có thể bị mất đầu pipe và in lỗi
        ``EPIPE: broken pipe, write``. Vì vậy khi người dùng đóng cửa sổ
        chính, ta phát tín hiệu dừng, mở pause để các worker thoát vòng lặp,
        rồi chủ động đóng context/browser đang được app quản lý.
        """
        if self.is_closing:
            return

        self.is_closing = True
        self.task_stop_event.set()
        self.task_pause_event.set()
        self.save_comment_content()

        for job_attr in (
            "comment_content_save_job",
            "account_refresh_job",
            "account_render_job",
            "history_refresh_job",
            "history_render_job",
            "browser_render_job",
        ):
            job_id = getattr(self, job_attr, None)
            if job_id:
                try:
                    self.after_cancel(job_id)
                except Exception:
                    pass
                setattr(self, job_attr, None)

        self.close_active_playwright_sessions()
        self.destroy()

    def register_playwright_session(self, browser=None, context=None):
        session = {"browser": browser, "context": context}
        with self.playwright_sessions_lock:
            token = id(session)
            self.playwright_sessions[token] = session
        return token

    def unregister_playwright_session(self, token):
        if token is None:
            return
        with self.playwright_sessions_lock:
            self.playwright_sessions.pop(token, None)

    def close_playwright_session(self, session):
        for resource_name in ("context", "browser"):
            resource = session.get(resource_name)
            if resource is None:
                continue
            try:
                resource.close()
            except Exception:
                pass

    def close_active_playwright_sessions(self):
        with self.playwright_sessions_lock:
            sessions = list(self.playwright_sessions.values())
            self.playwright_sessions.clear()

        for session in sessions:
            self.close_playwright_session(session)

    def save_accounts(self):
        self.storage.save_accounts(self.accounts)

    def save_logs(self):
        self.storage.save_logs(self.logs)

    def account_proxy_lock_message(self, account):
        remaining = proxy_lock_remaining_label(account)
        lock_until = proxy_lock_until_label(account)
        detail = f"còn {remaining}"
        if lock_until:
            detail = f"{detail}, đến {lock_until}"
        return (
            "Tài khoản vừa đổi proxy nên đang khóa các thao tác nhạy cảm trong 24h; "
            f"chỉ được lướt newsfeed/reels (không like/comment/tham gia nhóm). ({detail})."
        )

    def proxy_action_locked(self, account):
        return is_proxy_action_locked(account)

    def log_proxy_action_lock(self, account):
        account_name = account.get("name") or account.get("uid") or "Unknown"
        message = self.account_proxy_lock_message(account)
        self.after(0, lambda n=account_name, msg=message: self.append_live_log(f"[{n}] 🔒 {msg}"))

    def ensure_proxy_action_allowed(self, account):
        if self.proxy_action_locked(account):
            self.log_proxy_action_lock(account)
            return False
        return True

    def refresh_account_dependent_views(self):
        self.refresh_accounts()
        if hasattr(self, "cmt_acc_scroll"):
            self.refresh_comment_accounts()
        if hasattr(self, "browser_accounts_scroll"):
            self.refresh_browser_accounts()
        if hasattr(self, "settings_data_label"):
            self.refresh_settings_info()

    # --- UI CORE & ĐIỀU HƯỚNG ---
    def build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.selected_accounts = set()
        self.account_rows = {}
        self.log_lines = []

        # TẠO SIDEBAR
        self.sidebar = ctk.CTkFrame(self, width=220, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)

        ctk.CTkLabel(
            self.sidebar,
            text="Account Care",
            font=("Arial", 22, "bold")
        ).pack(pady=(25, 20))

        # Khởi tạo các nút menu
        self.menu_buttons = {}
        self.menu_buttons["care"] = self.create_menu_btn("Nuôi tài khoản", lambda: self.switch_view("care"))
        self.menu_buttons["comment"] = self.create_menu_btn("Cấu hình Comment", lambda: self.switch_view("comment"))
        self.menu_buttons["browser"] = self.create_menu_btn("Trình duyệt", lambda: self.switch_view("browser"))
        self.menu_buttons["history"] = self.create_menu_btn("Lịch sử nuôi", lambda: self.switch_view("history"))
        self.menu_buttons["settings"] = self.create_menu_btn("Cài đặt", lambda: self.switch_view("settings"))

        self.sidebar_stats = ctk.CTkFrame(self.sidebar)
        self.sidebar_stats.pack(side="bottom", fill="x", padx=15, pady=20)

        self.stat_label = ctk.CTkLabel(
            self.sidebar_stats,
            text="Tổng tài khoản: 0",
            anchor="w"
        )
        self.stat_label.pack(fill="x", padx=15, pady=10)

        # MAIN CONTAINER
        self.main_container = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main_container.grid(row=0, column=1, sticky="nsew")
        self.main_container.grid_columnconfigure(0, weight=1)
        self.main_container.grid_rowconfigure(0, weight=1)

        # TẠO CÁC MÀN HÌNH (VIEWS)
        self.view_care = ctk.CTkFrame(self.main_container, corner_radius=0, fg_color="#0f172a")
        self.view_comment = ctk.CTkFrame(self.main_container, corner_radius=0, fg_color="#0f172a")
        self.view_browser = ctk.CTkFrame(self.main_container, corner_radius=0, fg_color="#0f172a")
        self.view_history = ctk.CTkFrame(self.main_container, corner_radius=0, fg_color="#0f172a")
        self.view_settings = ctk.CTkFrame(self.main_container, corner_radius=0, fg_color="#0f172a")

        self.build_view_care()
        self.build_view_comment()
        self.build_view_browser()
        self.build_view_history()
        self.build_view_settings()

        # Mặc định mở màn hình Nuôi tài khoản
        self.switch_view("care")

    def create_menu_btn(self, text, command):
        btn = ctk.CTkButton(
            self.sidebar,
            text=text,
            height=42,
            anchor="w",
            fg_color="transparent",
            hover_color="#1d4ed8",
            command=command
        )
        btn.pack(fill="x", padx=15, pady=5)
        return btn

    def switch_view(self, view_name):
        for key, btn in self.menu_buttons.items():
            btn.configure(fg_color="#2563eb" if key == view_name else "transparent")

        for view in (self.view_care, self.view_comment, self.view_browser, self.view_history, self.view_settings):
            view.grid_forget()

        if view_name == "care":
            self.view_care.grid(row=0, column=0, sticky="nsew")
        elif view_name == "comment":
            self.refresh_comment_accounts()
            self.view_comment.grid(row=0, column=0, sticky="nsew")
        elif view_name == "browser":
            self.refresh_browser_accounts()
            self.view_browser.grid(row=0, column=0, sticky="nsew")
        elif view_name == "history":
            self.refresh_history_view()
            self.view_history.grid(row=0, column=0, sticky="nsew")
        elif view_name == "settings":
            self.view_settings.grid(row=0, column=0, sticky="nsew")

    # --- MÀN HÌNH 1: NUÔI TÀI KHOẢN ---
    def build_view_care(self):
        self.view_care.grid_columnconfigure(0, weight=1)
        self.view_care.grid_columnconfigure(1, weight=0)
        self.view_care.grid_rowconfigure(0, weight=1)

        # ── KHU VỰC TRÁI: Bảng danh sách tài khoản + Log ──────────────────────
        left_care = ctk.CTkFrame(self.view_care, fg_color="transparent")
        left_care.grid(row=0, column=0, sticky="nsew")
        left_care.grid_columnconfigure(0, weight=1)
        left_care.grid_rowconfigure(2, weight=1)   # bảng acc chiếm phần lớn
        left_care.grid_rowconfigure(3, weight=0)   # log realtime cố định dưới

        # ── HEADER ──────────────────────────────────────────────────────────────
        header = ctk.CTkFrame(left_care, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 6))
        header.grid_columnconfigure(0, weight=1)

        # Tiêu đề
        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            title_row, text="🐾  Nuôi tài khoản Facebook",
            font=("Arial", 24, "bold"), text_color="#f0f6ff"
        ).pack(side="left")

        # Search + nút thêm
        action_row = ctk.CTkFrame(header, fg_color="transparent")
        action_row.grid(row=0, column=1, sticky="e")

        self.search_entry = ctk.CTkEntry(
            action_row, width=240, height=38,
            placeholder_text="🔍  Tìm tên / proxy / ghi chú...",
            fg_color="#1e293b", border_color="#334155", border_width=1
        )
        self.search_entry.pack(side="left", padx=(0, 8))
        self.search_entry.bind("<KeyRelease>", self.schedule_accounts_refresh)

        ctk.CTkButton(
            action_row, text="+ Thêm lẻ", height=38, width=110,
            fg_color="#2563eb", hover_color="#1d4ed8",
            font=("Arial", 13, "bold"),
            command=self.add_account_popup
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            action_row, text="☰ Bulk", height=38, width=90,
            fg_color="#7c3aed", hover_color="#6d28d9",
            font=("Arial", 13, "bold"),
            command=self.add_bulk_accounts_popup
        ).pack(side="left")

        # ── THANH THỐNG KÊ NHANH (4 cards nhỏ gọn) ─────────────────────────────
        self.dashboard = ctk.CTkFrame(left_care, fg_color="transparent")
        self.dashboard.grid(row=1, column=0, sticky="ew", padx=20, pady=(4, 6))
        for col in range(8):
            self.dashboard.grid_columnconfigure(col, weight=1 if col in (0,1,2,3,4) else 0)

        # 5 stat cards
        self.live_card        = self._stat_card(self.dashboard, "🟢 Live",        "0", "#14532d", "#166534", 0)
        self.die_card         = self._stat_card(self.dashboard, "🔴 Die",         "0", "#7f1d1d", "#991b1b", 1)
        self.checkpoint_card  = self._stat_card(self.dashboard, "🟡 Checkpoint",  "0", "#78350f", "#92400e", 2)
        self.proxy_error_card = self._stat_card(self.dashboard, "🔌 Proxy Lỗi",  "0", "#312e81", "#3730a3", 3)
        self.selected_card    = self._stat_card(self.dashboard, "☑ Đã chọn",     "0", "#1e3a8a", "#1d4ed8", 4)

        # Separator + filter + nút hành động (bên phải dashboard)
        filter_box = ctk.CTkFrame(self.dashboard, fg_color="#1e293b", corner_radius=10)
        filter_box.grid(row=0, column=5, columnspan=3, sticky="nsew", padx=(8, 0))

        self.filter_var = ctk.StringVar(value="all")
        filter_inner = ctk.CTkFrame(filter_box, fg_color="transparent")
        filter_inner.pack(fill="x", padx=10, pady=6)

        ctk.CTkLabel(filter_inner, text="Lọc:", text_color="#94a3b8", font=("Arial", 12)).pack(side="left", padx=(0,4))
        for lbl, val in [("Tất cả", "all"), ("Live", "active"), ("Checkpoint", "checkpoint"), ("Proxy Lỗi", "proxy_error"), ("Die", "cookie_error")]:
            ctk.CTkRadioButton(
                filter_inner, text=lbl, variable=self.filter_var, value=val,
                command=self.refresh_accounts, font=("Arial", 12),
                radiobutton_width=14, radiobutton_height=14
            ).pack(side="left", padx=5)

        btn_row = ctk.CTkFrame(filter_box, fg_color="transparent")
        btn_row.pack(fill="x", padx=8, pady=(0, 6))
        ctk.CTkButton(
            btn_row, text="✓ Chọn tất cả", height=30, width=120,
            fg_color="#374151", hover_color="#4b5563", font=("Arial", 12),
            command=self.select_all_filtered_accounts
        ).pack(side="left", padx=(0, 5))
        ctk.CTkButton(
            btn_row, text="✕ Bỏ chọn", height=30, width=90,
            fg_color="#374151", hover_color="#4b5563", font=("Arial", 12),
            command=self.clear_selected_accounts
        ).pack(side="left", padx=(0, 5))
        ctk.CTkButton(
            btn_row, text="🔌 Tối ưu Proxy", height=30, width=110,
            fg_color="#059669", hover_color="#047857", font=("Arial", 12),
            command=self.optimize_proxies
        ).pack(side="left")
        # ── BẢNG DANH SÁCH TÀI KHOẢN ────────────────────────────────────────────
        self.table_outer = ctk.CTkFrame(left_care, fg_color="#0f172a", corner_radius=14)
        self.table_outer.grid(row=2, column=0, sticky="nsew", padx=20, pady=(0, 6))
        self.table_outer.grid_columnconfigure(0, weight=1)
        self.table_outer.grid_rowconfigure(1, weight=1)

        # Header bảng
        self.table_header = ctk.CTkFrame(self.table_outer, fg_color="#1e293b", corner_radius=10)
        self.table_header.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        self.configure_table_columns(self.table_header)

        col_labels = ["", "Tên tài khoản", "Proxy", "Trạng thái", "Kiểu nuôi", "Lần cuối nuôi", "Lượt xem", "Ghi chú", "Thao tác"]
        for col, text in enumerate(col_labels):
            ctk.CTkLabel(
                self.table_header, text=text,
                font=("Arial", 12, "bold"), text_color="#64748b", anchor="w"
            ).grid(row=0, column=col, sticky="ew", padx=8, pady=8)

        self.account_container = ctk.CTkScrollableFrame(
            self.table_outer, fg_color="transparent", corner_radius=0
        )
        self.account_container.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

        # ── LOG REALTIME ────────────────────────────────────────────────────────
        log_panel = ctk.CTkFrame(left_care, fg_color="#020617", corner_radius=14)
        log_panel.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 14))
        log_panel.grid_columnconfigure(0, weight=1)

        log_header = ctk.CTkFrame(log_panel, fg_color="transparent")
        log_header.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))
        log_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            log_header, text="📋  Log thời gian thực",
            font=("Arial", 13, "bold"), text_color="#38bdf8", anchor="w"
        ).grid(row=0, column=0, sticky="w")

        # Indicator trạng thái chạy
        self.log_status_badge = ctk.CTkLabel(
            log_header, text="● Chờ",
            font=("Arial", 11), text_color="#94a3b8",
            fg_color="#1e293b", corner_radius=8, padx=10
        )
        self.log_status_badge.grid(row=0, column=1, sticky="e")

        self.live_log_text = ctk.CTkTextbox(
            log_panel, height=110, fg_color="#0a0f1e",
            text_color="#94a3b8", wrap="word",
            font=("Consolas", 11), border_width=0
        )
        self.live_log_text.grid(row=1, column=0, sticky="ew", padx=10, pady=(2, 10))
        self.live_log_text.insert("end", "⬡  Hệ thống sẵn sàng. Chọn tài khoản → Bắt đầu nuôi.\n")
        self.live_log_text.configure(state="disabled")

        # ── KHU VỰC PHẢI: Panel điều khiển + thống kê ──────────────────────────
        self.detail = ctk.CTkScrollableFrame(
            self.view_care, width=330, corner_radius=0,
            fg_color="#0f172a"
        )
        self.detail.grid(row=0, column=1, sticky="nsew")
        self.detail.grid_columnconfigure(0, weight=1)

        # ── THỐNG KÊ TRẠNG THÁI CHẠY (3 badges) ────────────────────────────────
        run_stats_panel = ctk.CTkFrame(self.detail, fg_color="#1e293b", corner_radius=12)
        run_stats_panel.pack(fill="x", padx=14, pady=(16, 8))

        ctk.CTkLabel(
            run_stats_panel, text="📊  Trạng thái phiên chạy",
            font=("Arial", 13, "bold"), text_color="#e2e8f0", anchor="w"
        ).pack(fill="x", padx=14, pady=(12, 6))

        run_grid = ctk.CTkFrame(run_stats_panel, fg_color="transparent")
        run_grid.pack(fill="x", padx=10, pady=(0, 12))
        run_grid.grid_columnconfigure((0, 1, 2), weight=1)

        # Đang chạy
        run_box = ctk.CTkFrame(run_grid, fg_color="#14532d", corner_radius=10)
        run_box.grid(row=0, column=0, sticky="ew", padx=3)
        ctk.CTkLabel(run_box, text="▶ Chạy", font=("Arial", 10), text_color="#bbf7d0").pack(pady=(6,0))
        self.running_count_label = ctk.CTkLabel(run_box, text="0", font=("Arial", 22, "bold"), text_color="#4ade80")
        self.running_count_label.pack(pady=(0,6))

        # Lỗi
        err_box = ctk.CTkFrame(run_grid, fg_color="#7f1d1d", corner_radius=10)
        err_box.grid(row=0, column=1, sticky="ew", padx=3)
        ctk.CTkLabel(err_box, text="✗ Lỗi", font=("Arial", 10), text_color="#fecaca").pack(pady=(6,0))
        self.error_count_label = ctk.CTkLabel(err_box, text="0", font=("Arial", 22, "bold"), text_color="#f87171")
        self.error_count_label.pack(pady=(0,6))

        # Tạm dừng
        pause_box = ctk.CTkFrame(run_grid, fg_color="#374151", corner_radius=10)
        pause_box.grid(row=0, column=2, sticky="ew", padx=3)
        ctk.CTkLabel(pause_box, text="⏸ Dừng", font=("Arial", 10), text_color="#d1d5db").pack(pady=(6,0))
        self.paused_count_label = ctk.CTkLabel(pause_box, text="0", font=("Arial", 22, "bold"), text_color="#9ca3af")
        self.paused_count_label.pack(pady=(0,6))

        # ── NÚT BẮT ĐẦU / DỪNG ─────────────────────────────────────────────────
        ctrl_panel = ctk.CTkFrame(self.detail, fg_color="#1e293b", corner_radius=12)
        ctrl_panel.pack(fill="x", padx=14, pady=(0, 8))

        ctk.CTkLabel(
            ctrl_panel, text="⚡  Điều khiển",
            font=("Arial", 13, "bold"), text_color="#e2e8f0", anchor="w"
        ).pack(fill="x", padx=14, pady=(12, 8))

        ctk.CTkButton(
            ctrl_panel, text="▶  Bắt đầu nuôi (đã chọn)",
            height=44, font=("Arial", 13, "bold"),
            fg_color="#16a34a", hover_color="#15803d",
            command=self.start_care_selected_accounts
        ).pack(fill="x", padx=12, pady=(0, 6))

        ctk.CTkButton(
            ctrl_panel, text="▶  Nuôi acc đang xem",
            height=38, font=("Arial", 12),
            fg_color="#0d9488", hover_color="#0f766e",
            command=self.start_care_selected_account
        ).pack(fill="x", padx=12, pady=(0, 6))

        pause_stop_row = ctk.CTkFrame(ctrl_panel, fg_color="transparent")
        pause_stop_row.pack(fill="x", padx=12, pady=(0, 12))
        self.care_pause_button = ctk.CTkButton(
            pause_stop_row, text="⏸ Tạm dừng",
            height=36, font=("Arial", 12),
            fg_color="#475569", hover_color="#334155",
            command=self.toggle_pause_task
        )
        self.care_pause_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(
            pause_stop_row, text="⏹ Dừng",
            height=36, font=("Arial", 12),
            fg_color="#991b1b", hover_color="#7f1d1d",
            command=self.stop_task
        ).pack(side="left", fill="x", expand=True, padx=(4, 0))

        # ── THÔNG TIN TÀI KHOẢN ĐANG CHỌN ──────────────────────────────────────
        self.detail_box = ctk.CTkFrame(self.detail, fg_color="#1e293b", corner_radius=12)
        self.detail_box.pack(fill="x", padx=14, pady=(0, 8))

        detail_title_row = ctk.CTkFrame(self.detail_box, fg_color="transparent")
        detail_title_row.pack(fill="x", padx=14, pady=(12, 4))
        ctk.CTkLabel(
            detail_title_row, text="👤  Tài khoản đang xem",
            font=("Arial", 13, "bold"), text_color="#e2e8f0", anchor="w"
        ).pack(side="left")

        self.detail_name = ctk.CTkLabel(
            self.detail_box, text="— Chưa chọn tài khoản —",
            font=("Arial", 15, "bold"), text_color="#38bdf8"
        )
        self.detail_name.pack(pady=(2, 4))

        self.detail_info = ctk.CTkLabel(
            self.detail_box, text="",
            justify="left", anchor="w",
            wraplength=275, text_color="#94a3b8",
            font=("Arial", 11)
        )
        self.detail_info.pack(fill="x", padx=14, pady=(0, 10))

        # Nút hành động với acc đang chọn
        acc_action_row = ctk.CTkFrame(self.detail_box, fg_color="transparent")
        acc_action_row.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(
            acc_action_row, text="🌐 Mở FB",
            height=34, width=90, font=("Arial", 12),
            fg_color="#2563eb", hover_color="#1d4ed8",
            command=self.open_selected_account
        ).pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            acc_action_row, text="✎ Sửa",
            height=34, width=78, font=("Arial", 12),
            fg_color="#374151", hover_color="#4b5563",
            command=self.edit_selected_account
        ).pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            acc_action_row, text="🗑 Xóa",
            height=34, width=78, font=("Arial", 12),
            fg_color="#7f1d1d", hover_color="#991b1b",
            command=self.delete_selected_account
        ).pack(side="left")

        acc_action_row2 = ctk.CTkFrame(self.detail_box, fg_color="transparent")
        acc_action_row2.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(
            acc_action_row2, text="🎭 Đổi tên: NetfIix Tiệm Phim",
            height=34, font=("Arial", 12, "bold"),
            fg_color="#8b5cf6", hover_color="#7c3aed",
            command=self.change_name_selected_account
        ).pack(side="left", fill="x", expand=True)

        # ── CÀI ĐẶT THÔNG SỐ NUÔI ───────────────────────────────────────────────
        self.settings_box = ctk.CTkFrame(self.detail, fg_color="#1e293b", corner_radius=12)
        self.settings_box.pack(fill="x", padx=14, pady=(0, 8))

        ctk.CTkLabel(
            self.settings_box, text="⚙  Cài đặt thông số nuôi",
            font=("Arial", 13, "bold"), text_color="#e2e8f0", anchor="w"
        ).pack(fill="x", padx=14, pady=(12, 8))

        def _setting_row(parent, label, var_name, values, default):
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=(0, 6))
            row.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(row, text=label, font=("Arial", 11), text_color="#94a3b8", anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 8))
            var = ctk.StringVar(value=default)
            setattr(self, var_name, var)
            menu = ctk.CTkOptionMenu(
                row, values=values, variable=var, height=30,
                fg_color="#0f172a", button_color="#334155",
                button_hover_color="#475569", text_color="#e2e8f0",
                dropdown_fg_color="#1e293b",
                command=lambda _: self.refresh_selected_account_plan()
            )
            menu.grid(row=0, column=1, sticky="ew")
            return menu

        self.newsfeed_menu = _setting_row(self.settings_box, "Newsfeed (phút):", "newsfeed_minutes_var", ["0","1","3","5","10","15","20","30"], "5")
        self.reels_menu    = _setting_row(self.settings_box, "Reels (phút):",    "reels_minutes_var",    ["0","1","3","5","10","15","20","30"], "5")
        self.pause_menu    = _setting_row(self.settings_box, "Nghỉ cuộn (giây):","pause_seconds_var",    ["2-5","4-9","6-12","10-20"],         "4-9")

        # Số acc đồng thời
        parallel_row = ctk.CTkFrame(self.settings_box, fg_color="transparent")
        parallel_row.pack(fill="x", padx=12, pady=(0, 8))
        parallel_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(parallel_row, text="Chạy đồng thời:", font=("Arial", 11), text_color="#94a3b8", anchor="w").grid(row=0, column=0, sticky="w", padx=(0,8))
        self.max_parallel_care_var = ctk.StringVar(value="2")
        self.max_parallel_care_menu = ctk.CTkOptionMenu(
            parallel_row, values=["1","2","3","4","5"],
            variable=self.max_parallel_care_var, height=30,
            fg_color="#0f172a", button_color="#334155",
            button_hover_color="#475569", text_color="#e2e8f0",
            dropdown_fg_color="#1e293b"
        )
        self.max_parallel_care_menu.grid(row=0, column=1, sticky="ew")

        # Checkboxes
        chk_frame = ctk.CTkFrame(self.settings_box, fg_color="transparent")
        chk_frame.pack(fill="x", padx=12, pady=(2, 10))

        self.auto_like_care_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            chk_frame, text="Tự động Like (10-20 bài/reels)",
            variable=self.auto_like_care_var,
            font=("Arial", 11), text_color="#cbd5e1",
            checkmark_color="#4ade80", hover_color="#14532d",
            command=self.refresh_selected_account_plan
        ).pack(anchor="w", pady=3)

        self.smart_care_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            chk_frame, text="Nuôi thông minh theo từng acc",
            variable=self.smart_care_var,
            font=("Arial", 11), text_color="#cbd5e1",
            checkmark_color="#4ade80", hover_color="#14532d",
            command=self.refresh_selected_account_plan
        ).pack(anchor="w", pady=3)

        self.read_notifications_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            chk_frame, text="Đọc thông báo trong lúc nuôi",
            variable=self.read_notifications_var,
            font=("Arial", 11), text_color="#cbd5e1",
            checkmark_color="#4ade80", hover_color="#14532d",
            command=self.refresh_selected_account_plan
        ).pack(anchor="w", pady=3)

        self.join_groups_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            chk_frame, text="Tham gia 1-2 group (ngẫu nhiên)",
            variable=self.join_groups_var,
            font=("Arial", 11), text_color="#cbd5e1",
            checkmark_color="#4ade80", hover_color="#14532d",
            command=self.refresh_selected_account_plan
        ).pack(anchor="w", pady=3)

        # Preview kế hoạch nuôi
        self.care_plan_preview = ctk.CTkLabel(
            self.settings_box,
            text="Chọn tài khoản để xem gợi ý nuôi riêng.",
            text_color="#a7f3d0",
            wraplength=280,
            justify="left",
            anchor="w",
        )
        self.care_plan_preview.pack(fill="x", padx=15, pady=(0, 15))




    # --- MÀN HÌNH 2: CẤU HÌNH COMMENT ---
    def build_view_comment(self):
        self.view_comment.grid_columnconfigure(0, weight=3)
        self.view_comment.grid_columnconfigure(1, weight=1)
        self.view_comment.grid_rowconfigure(1, weight=1)

        # Header Comment
        header_cmt = ctk.CTkFrame(self.view_comment, fg_color="transparent")
        header_cmt.grid(row=0, column=0, columnspan=2, sticky="ew", padx=20, pady=(20, 10))
        ctk.CTkLabel(header_cmt, text="Cấu Hình Chiến Dịch Comment", font=("Arial", 26, "bold")).pack(side="left")

        # KHU VỰC TRÁI: SETUP CHIẾN DỊCH
        setup_frame = ctk.CTkFrame(self.view_comment, fg_color="transparent")
        setup_frame.grid(row=1, column=0, sticky="nsew", padx=(20, 10), pady=(0, 20))
        setup_frame.grid_columnconfigure(0, weight=1)
        setup_frame.grid_columnconfigure(1, weight=1)
        setup_frame.grid_rowconfigure(1, weight=1)

        # 1. Nguồn bài viết
        source_frame = ctk.CTkFrame(setup_frame, corner_radius=10, fg_color="#1e293b")
        source_frame.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

        ctk.CTkLabel(source_frame, text="Nguồn Bài Viết", font=("Arial", 16, "bold")).pack(anchor="w", padx=15, pady=(15, 0))

        self.tab_source = ctk.CTkTabview(source_frame, height=200)
        self.tab_source.pack(fill="both", expand=True, padx=15, pady=10)

        tab_manual = self.tab_source.add("Nhập Link Thủ Công")
        tab_manual.grid_columnconfigure(0, weight=1)
        tab_manual.grid_rowconfigure(0, weight=1)
        self.url_input = ctk.CTkTextbox(tab_manual, height=120)
        self.url_input.grid(row=0, column=0, sticky="nsew", pady=(0, 5))
        self.url_input.insert("1.0", "Dán danh sách URL bài viết vào đây (Mỗi dòng 1 link)...\n")
        ctk.CTkButton(tab_manual, text="🔍 Kiểm tra Link", height=30, fg_color="#059669", hover_color="#047857").grid(row=1, column=0, sticky="e")

        tab_auto = self.tab_source.add("Tự Động Quét Bài")
        ctk.CTkLabel(tab_auto, text="ID Group / Fanpage:", anchor="w").pack(fill="x", pady=(5,0))
        self.target_id_input = ctk.CTkEntry(tab_auto, placeholder_text="Nhập UID hoặc Link Page/Group")
        self.target_id_input.pack(fill="x", pady=5)
        row_auto = ctk.CTkFrame(tab_auto, fg_color="transparent")
        row_auto.pack(fill="x", pady=5)
        ctk.CTkLabel(row_auto, text="Số bài cần lấy:").pack(side="left")
        self.post_limit_input = ctk.CTkEntry(row_auto, width=60)
        self.post_limit_input.pack(side="left", padx=10)
        self.post_limit_input.insert(0, "5")
        ctk.CTkButton(tab_auto, text="🔄 Quét Thử", fg_color="#3b82f6", hover_color="#2563eb").pack(pady=10, fill="x")

        # 2. Nội dung Comment + Terminal Log
        content_frame = ctk.CTkFrame(setup_frame, corner_radius=10, fg_color="#1e293b")
        content_frame.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=5, pady=5)
        content_frame.grid_columnconfigure(0, weight=1)
        content_frame.grid_rowconfigure(1, weight=1)   # content box mở rộng vừa phải
        content_frame.grid_rowconfigure(5, weight=2)   # terminal log chiếm nhiều hơn

        # --- Header ---
        header_row = ctk.CTkFrame(content_frame, fg_color="transparent")
        header_row.grid(row=0, column=0, sticky="ew", padx=15, pady=(15, 0))
        header_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header_row, text="Nội dung Comment / Fallback", font=("Arial", 15, "bold")).grid(row=0, column=0, sticky="w")

        # --- Textbox nội dung ---
        self.comment_content = ctk.CTkTextbox(content_frame, wrap="word", height=110)
        self.comment_content.grid(row=1, column=0, sticky="nsew", padx=15, pady=(6, 3))
        self.comment_content.insert("1.0", self.app_settings.get("comment_content", DEFAULT_COMMENT_CONTENT))
        self.comment_content.bind("<KeyRelease>", self.schedule_comment_content_save)
        self.comment_content.bind("<FocusOut>", self.save_comment_content)
        self.comment_content.bind("<<Paste>>", lambda event: self.after(50, self.save_comment_content))

        ctk.CTkLabel(
            content_frame,
            text=(
                "Để trống + bật AI → tool tự sinh comment theo nội dung bài. "
                "Hoặc nhập thủ công (hỗ trợ {spin|nội dung|xoay vòng})."
            ),
            text_color="#64748b",
            wraplength=420,
            justify="left",
            font=("Arial", 11),
        ).grid(row=2, column=0, sticky="ew", padx=15, pady=(0, 4))

        tool_cmt = ctk.CTkFrame(content_frame, fg_color="transparent")
        tool_cmt.grid(row=3, column=0, sticky="ew", padx=15, pady=(0, 6))
        self.btn_add_image = ctk.CTkButton(tool_cmt, text="📷 Thêm Ảnh/Video", width=130, fg_color="#475569", command=self.choose_comment_image)
        self.btn_add_image.pack(side="left", padx=(0, 8))
        ctk.CTkButton(tool_cmt, text="🔄 Xem thử Spin", width=120, fg_color="#0d9488", command=self.preview_spin_content).pack(side="left")

        self.spin_preview_label = ctk.CTkLabel(content_frame, text="", text_color="#a7f3d0", justify="left", wraplength=380, font=("Arial", 11))
        self.spin_preview_label.grid(row=4, column=0, sticky="w", padx=15, pady=(0, 4))

        # --- Terminal Log chiến dịch comment ---
        log_header = ctk.CTkFrame(content_frame, fg_color="transparent")
        log_header.grid(row=5, column=0, sticky="nsew", padx=15, pady=(0, 10))
        log_header.grid_columnconfigure(0, weight=1)
        log_header.grid_rowconfigure(1, weight=1)

        log_title_row = ctk.CTkFrame(log_header, fg_color="transparent")
        log_title_row.grid(row=0, column=0, sticky="ew")
        log_title_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(log_title_row, text="📋 Nhật ký chiến dịch", font=("Arial", 13, "bold"), text_color="#94a3b8").grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            log_title_row, text="Xóa log", width=70, height=24,
            fg_color="#1e293b", hover_color="#334155", text_color="#64748b", font=("Arial", 11),
            command=lambda: (
                self.comment_log_text.configure(state="normal"),
                self.comment_log_text.delete("1.0", "end"),
                self.comment_log_text.configure(state="disabled")
            )
        ).grid(row=0, column=1, sticky="e")

        self.comment_log_text = ctk.CTkTextbox(
            log_header,
            fg_color="#0a0f1a",
            text_color="#a3e635",
            font=("Consolas", 11),
            state="disabled",
            wrap="word",
            corner_radius=8,
        )
        self.comment_log_text.grid(row=1, column=0, sticky="nsew")

        # 3. Danh sách tài khoản chạy
        acc_list_frame = ctk.CTkFrame(setup_frame, corner_radius=10, fg_color="#1e293b")
        acc_list_frame.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)
        acc_list_frame.grid_columnconfigure(0, weight=1)
        acc_list_frame.grid_rowconfigure(2, weight=1)

        # --- Header + bulk-select toolbar ---
        acc_header = ctk.CTkFrame(acc_list_frame, fg_color="transparent")
        acc_header.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))
        acc_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(acc_header, text="Chọn Tài Khoản Chạy", font=("Arial", 15, "bold")).grid(
            row=0, column=0, sticky="w"
        )

        bulk_row = ctk.CTkFrame(acc_list_frame, fg_color="transparent")
        bulk_row.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))

        def _cmt_select_all():
            for i, acc in enumerate(self.accounts):
                if not self.proxy_action_locked(acc):
                    self.comment_selected_accounts.add(i)
            self.refresh_comment_accounts()

        def _cmt_deselect_all():
            self.comment_selected_accounts.clear()
            self.refresh_comment_accounts()

        def _cmt_select_live():
            for i, acc in enumerate(self.accounts):
                if acc.get("status", "active") == "active" and not self.proxy_action_locked(acc):
                    self.comment_selected_accounts.add(i)
                else:
                    self.comment_selected_accounts.discard(i)
            self.refresh_comment_accounts()

        def _cmt_select_filtered():
            """Chọn tất cả acc đang hiển thị trong ô tìm kiếm."""
            kw = self.cmt_search_var.get().lower().strip()
            for i, acc in enumerate(self.accounts):
                name = (acc.get("name") or acc.get("uid") or "").lower()
                proxy = (acc.get("proxy") or "").lower()
                if kw and kw not in name and kw not in proxy:
                    continue
                if not self.proxy_action_locked(acc):
                    self.comment_selected_accounts.add(i)
            self.refresh_comment_accounts()

        btn_kw = dict(height=26, font=("Arial", 11), corner_radius=6)
        ctk.CTkButton(bulk_row, text="✅ Tất cả",    width=72, fg_color="#1d4ed8", hover_color="#1e40af", command=_cmt_select_all,      **btn_kw).pack(side="left", padx=(0, 4))
        ctk.CTkButton(bulk_row, text="🟢 Live",      width=68, fg_color="#065f46", hover_color="#064e3b", command=_cmt_select_live,     **btn_kw).pack(side="left", padx=(0, 4))
        ctk.CTkButton(bulk_row, text="🔍 Kết quả",  width=80, fg_color="#0f766e", hover_color="#115e59", command=_cmt_select_filtered, **btn_kw).pack(side="left", padx=(0, 4))
        ctk.CTkButton(bulk_row, text="✖ Bỏ chọn",  width=80, fg_color="#374151", hover_color="#1f2937", command=_cmt_deselect_all,    **btn_kw).pack(side="left")

        # Số đã chọn — cập nhật khi refresh
        self.cmt_selected_label = ctk.CTkLabel(bulk_row, text="0 đã chọn", text_color="#94a3b8", font=("Arial", 11))
        self.cmt_selected_label.pack(side="right", padx=6)

        # --- Ô tìm kiếm / lọc ---
        self.cmt_search_var = ctk.StringVar()
        self.cmt_search_var.trace_add("write", lambda *_: self.refresh_comment_accounts())
        cmt_search_entry = ctk.CTkEntry(
            acc_list_frame,
            textvariable=self.cmt_search_var,
            placeholder_text="🔍  Tìm tên / proxy...",
            height=30,
            corner_radius=8,
        )
        cmt_search_entry.grid(row=1, column=0, sticky="ew", padx=12, pady=(30, 4))

        self.cmt_acc_scroll = ctk.CTkScrollableFrame(acc_list_frame, fg_color="#0f172a", corner_radius=5)
        self.cmt_acc_scroll.grid(row=2, column=0, sticky="nsew", padx=15, pady=(0, 15))

        # KHU VỰC PHẢI: CÀI ĐẶT THÔNG SỐ
        right_panel = ctk.CTkFrame(self.view_comment, corner_radius=0, width=320)
        right_panel.grid(row=1, column=1, sticky="nsew")
        right_panel.grid_propagate(False)

        ctk.CTkLabel(right_panel, text="Cài Đặt Thông Số", font=("Arial", 20, "bold")).pack(pady=(20, 20), padx=20, anchor="w")

        def create_setting_row(parent, label_text, default_val):
            ctk.CTkLabel(parent, text=label_text).pack(anchor="w", padx=20)
            entry = ctk.CTkEntry(parent)
            entry.pack(fill="x", padx=20, pady=(0, 15))
            entry.insert(0, default_val)
            return entry

        self.delay_cmt_input = create_setting_row(right_panel, "Nghỉ giữa mỗi comment (giây):", "60-120")
        self.limit_cmt_input = create_setting_row(right_panel, "Giới hạn comment / tài khoản:", "5")
        self.comment_parallel_input = create_setting_row(right_panel, "Số tab chạy song song:", "1")
        self.like_before_cmt_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(right_panel, text="Tự động thả Like trước khi Comment", variable=self.like_before_cmt_var).pack(anchor="w", padx=20, pady=(10, 4))

        self.scan_before_cmt_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            right_panel,
            text="Quét bài rồi tự nghĩ comment phù hợp",
            variable=self.scan_before_cmt_var,
        ).pack(anchor="w", padx=20, pady=(4, 4))

        self.direct_comment_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            right_panel,
            text="Bình luận thẳng vào bài (không reply comment khác)",
            variable=self.direct_comment_var,
        ).pack(anchor="w", padx=20, pady=(4, 4))

        ctk.CTkLabel(
            right_panel,

            text="Khi bật: tool đọc nội dung bài post chính, mở chatgpt.com trong Chrome/profile ChatGPT riêng mà bạn đã tự đăng nhập, paste prompt để lấy comment mới bám ý cụ thể. Nếu ChatGPT chưa đăng nhập hoặc trả comment không hợp lệ thì bỏ qua link; không tự bịa fallback.",
            text_color="#a7f3d0",
            wraplength=280,
            justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 10))

        # GẮN COMMAND CHẠY COMMENT TẠI ĐÂY
        ctk.CTkButton(
            right_panel,
            text="▶ BẮT ĐẦU COMMENT",
            height=50,
            font=("Arial", 16, "bold"),
            fg_color="#16a34a",
            hover_color="#15803d",
            command=self.start_comment_campaign
        ).pack(fill="x", padx=20, pady=(30, 10))

        comment_control_row = ctk.CTkFrame(right_panel, fg_color="transparent")
        comment_control_row.pack(fill="x", padx=20, pady=5)
        self.comment_pause_button = ctk.CTkButton(comment_control_row, text="⏸ Tạm Dừng", height=40, fg_color="#475569", command=self.toggle_pause_task)
        self.comment_pause_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(comment_control_row, text="⏹ Dừng", height=40, fg_color="#991b1b", hover_color="#7f1d1d", command=self.stop_task).pack(side="left", fill="x", expand=True, padx=(4, 0))

        self.comment_stats = {
            "total_links": 0,
            "success": 0,
            "failed": 0,
        }
        self.comment_stats_label = ctk.CTkLabel(
            right_panel,
            text="Link đã dán: 0\nComment thành công: 0\nComment thất bại: 0",
            justify="left",
            anchor="w",
            text_color="#a7f3d0",
        )
        self.comment_stats_label.pack(fill="x", padx=20, pady=(10, 0))

    # --- MÀN HÌNH 3: TRÌNH DUYỆT ---
    def build_view_browser(self):
        self.view_browser.grid_columnconfigure(0, weight=1)
        self.view_browser.grid_columnconfigure(1, weight=0)
        self.view_browser.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self.view_browser, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=25, pady=(25, 10))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="Trình duyệt tài khoản", font=("Arial", 28, "bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(header, text="Làm mới danh sách", width=150, fg_color="#374151", command=self.refresh_browser_accounts).grid(row=0, column=1)

        left = ctk.CTkFrame(self.view_browser, fg_color="#111827", corner_radius=15)
        left.grid(row=1, column=0, sticky="nsew", padx=(25, 10), pady=(5, 25))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(left, text="Chọn account để mở bằng profile/cookie riêng", font=("Arial", 16, "bold"), anchor="w").grid(row=0, column=0, sticky="ew", padx=15, pady=(15, 8))
        self.browser_accounts_scroll = ctk.CTkScrollableFrame(left, fg_color="#0f172a", corner_radius=10)
        self.browser_accounts_scroll.grid(row=1, column=0, sticky="nsew", padx=15, pady=(0, 15))

        right = ctk.CTkFrame(self.view_browser, width=360, corner_radius=0)
        right.grid(row=1, column=1, sticky="nsew")
        right.grid_propagate(False)
        ctk.CTkLabel(right, text="Điều khiển trình duyệt", font=("Arial", 20, "bold"), anchor="w").pack(fill="x", padx=20, pady=(22, 12))
        self.browser_selected_label = ctk.CTkLabel(right, text="Chưa chọn tài khoản", wraplength=300, justify="left")
        self.browser_selected_label.pack(fill="x", padx=20, pady=(0, 15))

        ctk.CTkLabel(right, text="URL mở nhanh", anchor="w").pack(fill="x", padx=20)
        self.browser_url_entry = ctk.CTkEntry(right)
        self.browser_url_entry.pack(fill="x", padx=20, pady=(4, 12))
        self.browser_url_entry.insert(0, self.app_settings.get("default_home_url", "https://www.facebook.com/"))

        ctk.CTkButton(right, text="🌐 Mở URL", height=42, fg_color="#2563eb", command=self.open_browser_selected_url).pack(fill="x", padx=20, pady=6)
        ctk.CTkButton(right, text="🏠 Facebook Home", height=42, fg_color="#374151", command=lambda: self.open_browser_selected_url("https://www.facebook.com/")).pack(fill="x", padx=20, pady=6)
        ctk.CTkButton(right, text="🎬 Reels", height=42, fg_color="#374151", command=lambda: self.open_browser_selected_url("https://www.facebook.com/reel/")).pack(fill="x", padx=20, pady=6)
        ctk.CTkButton(right, text="💬 Messenger", height=42, fg_color="#374151", command=lambda: self.open_browser_selected_url("https://www.facebook.com/messages/")).pack(fill="x", padx=20, pady=6)
        ctk.CTkLabel(
            right,
            text="Sau khi bạn đăng nhập thủ công, tool sẽ tự phát hiện cookie Facebook và lưu vào file cookie của account khi phiên trình duyệt còn mở.",
            text_color="#a7f3d0",
            wraplength=300,
            justify="left",
        ).pack(fill="x", padx=20, pady=(12, 0))
        ctk.CTkLabel(
            right,
            text="Nút mở chỉ mở trình duyệt để bạn thao tác thủ công; tool không tự đăng nhập, không tự chạy nuôi và không tự tắt cửa sổ.",
            text_color="#9ca3af",
            wraplength=300,
            justify="left",
        ).pack(fill="x", padx=20, pady=(18, 0))

    # --- MÀN HÌNH 4: LỊCH SỬ NUÔI + DASHBOARD ---
    def build_view_history(self):
        self.view_history.grid_columnconfigure(0, weight=1)
        self.view_history.grid_rowconfigure(3, weight=1)

        header = ctk.CTkFrame(self.view_history, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=25, pady=(25, 10))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="Lịch sử nuôi & Dashboard thống kê", font=("Arial", 28, "bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(header, text="Làm mới", width=110, fg_color="#374151", command=self.refresh_history_view).grid(row=0, column=1, padx=6)
        ctk.CTkButton(header, text="Xuất CSV", width=110, fg_color="#0d9488", command=self.export_history_csv).grid(row=0, column=2)

        self.history_cards = ctk.CTkFrame(self.view_history, fg_color="transparent")
        self.history_cards.grid(row=1, column=0, sticky="ew", padx=25, pady=(4, 8))
        for col in range(4):
            self.history_cards.grid_columnconfigure(col, weight=1)
        self.history_total_card = self.dashboard_card(self.history_cards, "Tổng log", "0", "#1e3a8a", 0)
        self.history_done_card = self.dashboard_card(self.history_cards, "Done", "0", "#14532d", 1)
        self.history_error_card = self.dashboard_card(self.history_cards, "Error/Die", "0", "#7f1d1d", 2)
        self.history_today_card = self.dashboard_card(self.history_cards, "Hôm nay", "0", "#78350f", 3)

        filters = ctk.CTkFrame(self.view_history, fg_color="transparent")
        filters.grid(row=2, column=0, sticky="ew", padx=25, pady=8)
        ctk.CTkLabel(filters, text="Lọc account/status:").pack(side="left", padx=(0, 8))
        self.history_filter_entry = ctk.CTkEntry(filters, width=320, placeholder_text="Nhập tên account hoặc trạng thái...")
        self.history_filter_entry.pack(side="left")
        self.history_filter_entry.bind("<KeyRelease>", self.schedule_history_refresh)

        body = ctk.CTkFrame(self.view_history, fg_color="transparent")
        body.grid(row=3, column=0, sticky="nsew", padx=25, pady=(0, 25))
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=0)
        body.grid_rowconfigure(0, weight=1)

        self.history_scroll = ctk.CTkScrollableFrame(body, fg_color="#111827", corner_radius=15)
        self.history_scroll.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        summary = ctk.CTkFrame(body, fg_color="#111827", width=360, corner_radius=15)
        summary.grid(row=0, column=1, sticky="nsew")
        summary.grid_propagate(False)
        ctk.CTkLabel(summary, text="Thống kê theo ngày/account/trạng thái", font=("Arial", 16, "bold"), wraplength=310, justify="left").pack(fill="x", padx=15, pady=(15, 8))
        self.history_summary_text = ctk.CTkTextbox(summary, fg_color="#0f172a", text_color="#d1d5db", wrap="word")
        self.history_summary_text.pack(fill="both", expand=True, padx=15, pady=(0, 15))

    # --- MÀN HÌNH 5: CÀI ĐẶT + IMPORT/EXPORT ---
    def build_view_settings(self):
        self.view_settings.grid_columnconfigure(0, weight=1)
        self.view_settings.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(self.view_settings, text="Cài đặt", font=("Arial", 28, "bold"), anchor="w").grid(row=0, column=0, sticky="ew", padx=25, pady=(25, 10))

        body = ctk.CTkScrollableFrame(self.view_settings, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=25, pady=(0, 25))
        body.grid_columnconfigure(0, weight=1)

        general = ctk.CTkFrame(body, fg_color="#111827", corner_radius=15)
        general.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ctk.CTkLabel(general, text="Thiết lập chung", font=("Arial", 18, "bold"), anchor="w").pack(fill="x", padx=18, pady=(16, 10))
        ctk.CTkLabel(general, text="URL mặc định khi mở Trình duyệt", anchor="w").pack(fill="x", padx=18)
        self.default_url_entry = ctk.CTkEntry(general)
        self.default_url_entry.pack(fill="x", padx=18, pady=(4, 12))
        self.default_url_entry.insert(0, self.app_settings.get("default_home_url", "https://www.facebook.com/"))
        ctk.CTkButton(general, text="Lưu cài đặt", width=140, fg_color="#16a34a", command=self.save_app_settings).pack(anchor="w", padx=18, pady=(0, 16))

        ai_frame = ctk.CTkFrame(body, fg_color="#111827", corner_radius=15)
        ai_frame.grid(row=1, column=0, sticky="ew", pady=12)
        ctk.CTkLabel(ai_frame, text="ChatGPT thủ công tạo comment", font=("Arial", 18, "bold"), anchor="w").pack(fill="x", padx=18, pady=(16, 8))
        ctk.CTkLabel(
            ai_frame,
            text="Không gọi API OpenAI/Gemini và không cần nhập cookie ChatGPT. Bạn bấm nút mở Chrome ChatGPT, tự đăng nhập một lần; tool sẽ dùng đúng Chrome/profile RIÊNG đó để gửi prompt và lấy comment trả về.",
            text_color="#a7f3d0",
            wraplength=850,
            justify="left",
        ).pack(fill="x", padx=18, pady=(0, 10))
        self.ai_comment_enabled_var = ctk.BooleanVar(value=bool(self.app_settings.get("ai_comment_enabled", True)))
        ctk.CTkCheckBox(ai_frame, text="Bật ChatGPT thủ công tự nghĩ comment theo bài viết", variable=self.ai_comment_enabled_var).pack(anchor="w", padx=18, pady=4)
        ctk.CTkLabel(ai_frame, text="Thư mục profile Chrome riêng cho ChatGPT", anchor="w").pack(fill="x", padx=18, pady=(8, 2))
        self.chatgpt_profile_entry = ctk.CTkEntry(ai_frame)
        self.chatgpt_profile_entry.pack(fill="x", padx=18, pady=(0, 8))
        self.chatgpt_profile_entry.insert(0, self.app_settings.get("chatgpt_profile_dir", DEFAULT_CHATGPT_PROFILE_DIR))
        action_row = ctk.CTkFrame(ai_frame, fg_color="transparent")
        action_row.pack(fill="x", padx=18, pady=(8, 16))
        ctk.CTkButton(action_row, text="🌐 Mở Chrome ChatGPT để đăng nhập", width=260, fg_color="#2563eb", command=self.open_chatgpt_login_browser).pack(side="left", padx=(0, 10))
        ctk.CTkButton(action_row, text="Lưu cài đặt ChatGPT", width=180, fg_color="#16a34a", command=self.save_app_settings).pack(side="left")

        io_frame = ctk.CTkFrame(body, fg_color="#111827", corner_radius=15)
        io_frame.grid(row=2, column=0, sticky="ew", pady=12)
        ctk.CTkLabel(io_frame, text="Backup / Import dữ liệu", font=("Arial", 18, "bold"), anchor="w").pack(fill="x", padx=18, pady=(16, 8))
        ctk.CTkLabel(
            io_frame,
            text="Backup đầy đủ sẽ gom account, password/2FA, lịch sử, cài đặt và file cookie vào 1 file JSON để đổi máy chỉ cần import lại. Hãy lưu file ở nơi an toàn.",
            text_color="#a7f3d0",
            wraplength=850,
            justify="left",
        ).pack(fill="x", padx=18, pady=(0, 8))
        ctk.CTkLabel(io_frame, text="Export account riêng lẻ mặc định sẽ bỏ password và mã 2FA. Chỉ bật tùy chọn bên dưới khi bạn thật sự cần export đầy đủ account.", text_color="#fbbf24", wraplength=850, justify="left").pack(fill="x", padx=18, pady=(0, 10))
        self.export_sensitive_var = ctk.BooleanVar(value=bool(self.app_settings.get("export_sensitive_default", False)))
        ctk.CTkCheckBox(io_frame, text="Bao gồm password và 2FA trong file export", variable=self.export_sensitive_var).pack(anchor="w", padx=18, pady=4)
        self.import_overwrite_var = ctk.BooleanVar(value=bool(self.app_settings.get("import_overwrite_default", False)))
        ctk.CTkCheckBox(io_frame, text="Khi import, ghi đè account trùng UID/tên", variable=self.import_overwrite_var).pack(anchor="w", padx=18, pady=4)
        action_row = ctk.CTkFrame(io_frame, fg_color="transparent")
        action_row.pack(fill="x", padx=18, pady=(12, 18))
        ctk.CTkButton(action_row, text="💾 Backup đầy đủ", width=170, fg_color="#16a34a", command=self.backup_all_data).pack(side="left", padx=(0, 10))
        ctk.CTkButton(action_row, text="📥 Import backup", width=170, fg_color="#7c3aed", command=self.import_full_backup_file).pack(side="left", padx=(0, 10))
        ctk.CTkButton(action_row, text="⬇ Export accounts", width=170, fg_color="#0d9488", command=self.export_accounts_safe).pack(side="left", padx=(0, 10))
        ctk.CTkButton(action_row, text="⬆ Import accounts", width=170, fg_color="#2563eb", command=self.import_accounts_safe).pack(side="left")

        info = ctk.CTkFrame(body, fg_color="#111827", corner_radius=15)
        info.grid(row=3, column=0, sticky="ew", pady=12)
        ctk.CTkLabel(info, text="Dữ liệu", font=("Arial", 18, "bold"), anchor="w").pack(fill="x", padx=18, pady=(16, 8))
        self.settings_data_label = ctk.CTkLabel(info, text="", justify="left", anchor="w")
        self.settings_data_label.pack(fill="x", padx=18, pady=(0, 16))
        self.refresh_settings_info()

    # --- CÁC HÀM TIỆN ÍCH DÀNH CHO TAB COMMENT ---
    def refresh_comment_accounts(self):
        for widget in self.cmt_acc_scroll.winfo_children():
            widget.destroy()

        if not self.accounts:
            ctk.CTkLabel(self.cmt_acc_scroll, text="Chưa có tài khoản nào.").pack(pady=20)
            return

        # Filter theo ô tìm kiếm
        kw = ""
        if hasattr(self, "cmt_search_var"):
            kw = self.cmt_search_var.get().lower().strip()

        # Chỉ show acc dùng được (active / proxy_error), ẩn die + checkpoint
        HIDDEN_STATUSES = {"cookie_error", "checkpoint"}

        shown = 0
        hidden_count = 0
        for index, acc in enumerate(self.accounts):
            status = acc.get("status", "active")

            # Ẩn acc die / checkpoint hoàn toàn
            if status in HIDDEN_STATUSES:
                hidden_count += 1
                self.comment_selected_accounts.discard(index)
                continue

            name = (acc.get("name") or acc.get("uid") or "").lower()
            proxy = (acc.get("proxy") or "").lower()
            if kw and kw not in name and kw not in proxy:
                continue
            shown += 1

            row = ctk.CTkFrame(self.cmt_acc_scroll, fg_color="transparent")
            row.pack(fill="x", pady=2)

            account_label = acc.get("name", "Không tên")
            if (acc.get("care_profile") or "auto") == "manual":
                account_label = f"{account_label} • {profile_label('manual')}"
            if self.proxy_action_locked(acc):
                account_label = f"🔒 {account_label} • chỉ đăng nhập {proxy_lock_remaining_label(acc)}"

            is_checked = ctk.BooleanVar(value=index in self.comment_selected_accounts and not self.proxy_action_locked(acc))
            chk = ctk.CTkCheckBox(
                row, text=account_label,
                variable=is_checked,
                state="disabled" if self.proxy_action_locked(acc) else "normal",
                command=lambda idx=index, var=is_checked: self.toggle_cmt_acc(idx, var.get())
            )
            chk.pack(side="left", padx=5, pady=5)

            ctk.CTkLabel(row, text=self.status_text(status), fg_color=self.status_color(status), corner_radius=5, padx=8).pack(side="right", padx=5)

        # Cập nhật label số đã chọn
        if hasattr(self, "cmt_selected_label"):
            sel = len(self.comment_selected_accounts)
            total_usable = len(self.accounts) - hidden_count
            label = f"{sel} đã chọn  •  {total_usable} live"
            if hidden_count:
                label += f"  ({hidden_count} ẩn)"
            if kw:
                label += f"  •  {shown} kết quả"
            self.cmt_selected_label.configure(text=label)

    def toggle_cmt_acc(self, index, checked):
        if checked:
            self.comment_selected_accounts.add(index)
        else:
            self.comment_selected_accounts.discard(index)
        # Cập nhật counter ngay mà không rebuild toàn bộ list
        if hasattr(self, "cmt_selected_label"):
            self.cmt_selected_label.configure(text=f"{len(self.comment_selected_accounts)} đã chọn")

    def preview_spin_content(self):
        raw_text = self.comment_content.get("1.0", "end-1c")

        try:
            result = spin_content(raw_text)
            self.spin_preview_label.configure(text=f"Mẫu xem thử:\n> {result}", text_color="#fcd34d")
        except Exception:
            self.spin_preview_label.configure(text=f"Lỗi cú pháp Spin!", text_color="#ef4444")

    def choose_comment_image(self):
        paths = filedialog.askopenfilenames(
            title="Chọn Ảnh/Video để Comment",
            filetypes=[("Image/Video Files", "*.png *.jpg *.jpeg *.mp4 *.avi *.gif"), ("All Files", "*.*")]
        )
        if paths:
            self.comment_image_paths = list(paths)
            file_count = len(self.comment_image_paths)
            self.btn_add_image.configure(text=f"✅ Đã có {file_count} file", fg_color="#059669")
            preview_names = ", ".join(os.path.basename(path) for path in self.comment_image_paths[:3])
            if file_count > 3:
                preview_names += f", ... (+{file_count - 3})"
            self.append_live_log(f"Đã chọn {file_count} file ảnh/video để ghép kèm comment: {preview_names}")
        else:
            self.comment_image_paths = []
            self.btn_add_image.configure(text="📷 Thêm Ảnh/Video", fg_color="#475569")

    # --- LOGIC CHẠY CHIẾN DỊCH COMMENT ---
    def start_comment_campaign(self):
        if not self.comment_selected_accounts:
            messagebox.showwarning("Thông báo", "Vui lòng tick chọn ít nhất 1 tài khoản bên cột 'Chọn Tài Khoản Chạy'!")
            return

        urls_text = self.url_input.get("1.0", "end").strip()
        urls = [url.strip() for url in urls_text.split("\n") if url.strip() and "http" in url]

        if not urls:
            messagebox.showwarning("Thông báo", "Vui lòng dán ít nhất 1 link bài viết Facebook hợp lệ!")
            return
        self.update_comment_stats(total_links=len(urls), success=0, failed=0)

        raw_content = self.comment_content.get("1.0", "end-1c").strip()
        self.save_comment_content()
        self.sync_ai_comment_settings_from_widgets()
        scan_before_comment = self.scan_before_cmt_var.get()

        if not raw_content and not scan_before_comment:
            messagebox.showwarning(
                "Thông báo",
                "Muốn để trống nội dung comment thì hãy bật chế độ tự tạo comment theo nội dung bài. "
                "Tool sẽ quét bài viết rồi paste vào ChatGPT trên web để tự sinh comment phù hợp; nếu ChatGPT chưa đăng nhập thì bỏ qua link.",
            )
            return
        try:
            comment_limit = int(self.limit_cmt_input.get().strip())
            if comment_limit <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Thông báo", "Giới hạn comment / tài khoản phải là số nguyên lớn hơn 0!")
            return

        try:
            max_parallel_tabs = int(self.comment_parallel_input.get().strip())
            if max_parallel_tabs <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Thông báo", "Số tab chạy song song phải là số nguyên lớn hơn 0!")
            return

        self.reset_task_state()
        comment_image_paths = [
            path for path in getattr(self, "comment_image_paths", [])
            if path and os.path.exists(path)
        ]

        selected_indexes = list(self.comment_selected_accounts)
        locked_names = [
            self.accounts[index].get("name") or self.accounts[index].get("uid") or "Unknown"
            for index in selected_indexes
            if index < len(self.accounts) and self.proxy_action_locked(self.accounts[index])
        ]
        selected_indexes = [
            index for index in selected_indexes
            if index < len(self.accounts) and not self.proxy_action_locked(self.accounts[index])
        ]
        if locked_names:
            self.append_live_log(
                "🔒 Bỏ qua account vừa đổi proxy trong chiến dịch comment: "
                f"{', '.join(locked_names)}. Các account này chỉ được mở/đăng nhập sau 24h."
            )
        if not selected_indexes:
            messagebox.showwarning("Thông báo", "Các tài khoản đang chọn đều vừa đổi proxy và chỉ được đăng nhập trong 24h, chưa được comment.")
            return
        max_parallel_tabs = min(max_parallel_tabs, len(selected_indexes))
        like_before_comment = self.like_before_cmt_var.get()
        direct_comment = self.direct_comment_var.get()

        threading.Thread(
            target=self.run_comment_task,
            args=(
                selected_indexes,
                urls,
                raw_content,
                comment_limit,
                comment_image_paths,
                max_parallel_tabs,
                like_before_comment,
                scan_before_comment,
                direct_comment,
            ),
            daemon=True
        ).start()

    def update_comment_stats(self, total_links=None, success=None, failed=None):
        if not hasattr(self, "comment_stats"):
            return
        if total_links is not None:
            self.comment_stats["total_links"] = max(0, int(total_links))
        if success is not None:
            self.comment_stats["success"] = max(0, int(success))
        if failed is not None:
            self.comment_stats["failed"] = max(0, int(failed))
        if hasattr(self, "comment_stats_label"):
            self.comment_stats_label.configure(
                text=(
                    f"Link đã dán: {self.comment_stats['total_links']}\n"
                    f"Comment thành công: {self.comment_stats['success']}\n"
                    f"Comment thất bại: {self.comment_stats['failed']}"
                )
            )

    def browse_during_comment_pause(self, page, account, seconds):
        account_name = account.get("name", "")
        end_time = time.time() + seconds
        mode = random.choice(["reels", "newsfeed"])
        mode_text = "Reels" if mode == "reels" else "Newsfeed"
        start_url = "https://www.facebook.com/reel/" if mode == "reels" else "https://www.facebook.com/"
        fallback_urls = ["https://facebook.com/reel/", "https://m.facebook.com/reel/"] if mode == "reels" else ["https://facebook.com/", "https://m.facebook.com/"]

        self.after(0, lambda n=account_name, m=mode_text, d=int(seconds): self.append_live_log(f"[{n}] 🧭 Đang lướt {m} trong thời gian nghỉ {d} giây..."))
        try:
            self.safe_goto(page, start_url, account=account, fallback_urls=fallback_urls)
            if not self.interruptible_sleep(random.uniform(3, 5)):
                return False
        except Exception as exc:
            self.after(0, lambda n=account_name, err=str(exc): self.append_live_log(f"[{n}] ⚠️ Không mở được {mode_text}, chuyển sang nghỉ thường: {err[:60]}..."))
            return self.interruptible_sleep(max(0, end_time - time.time()))

        while time.time() < end_time and not self.is_task_stopped():
            if not self.wait_if_paused():
                return False
            try:
                if mode == "reels":
                    page.keyboard.press("ArrowDown")
                else:
                    page.mouse.wheel(0, random.randint(350, 900))
            except Exception as exc:
                self.after(0, lambda n=account_name, err=str(exc): self.append_live_log(f"[{n}] ⚠️ Lướt trong lúc nghỉ bị lỗi: {err[:60]}..."))
                return self.interruptible_sleep(max(0, end_time - time.time()))

            remaining = max(0, end_time - time.time())
            if not self.interruptible_sleep(min(self.get_pause_seconds("3-7"), remaining)):
                return False

        return not self.is_task_stopped()

    def scan_facebook_content_before_comment(self, page, acc_name):
        """Quét nội dung bài Facebook trước khi tạo comment.

        Trả về (post_text: str, post_element) — post_element dùng cho scan tiếp theo.
        Trả về (None, None) nếu bị interrupt.
        """
        self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] 🔎 Đang quét nội dung bài Facebook..."))

        post_element = None
        try:
            # 1. Tìm phần tử bài viết đúng (có scoring, ưu tiên dialog/popup)
            post_element = find_main_post_element(page)

            # 2. Mở rộng "Xem thêm" / "See more" (nhanh, không timeout lâu)
            if not self.wait_if_paused():
                return None, None
            try:
                expand_post_text(page, post_element)
            except Exception:
                pass

            # 3. Scroll nhẹ để load nội dung
            if not self.wait_if_paused():
                return None, None
            try:
                page.mouse.wheel(0, 300)
            except Exception:
                pass
            if not self.interruptible_sleep(random.uniform(0.5, 0.8)):
                return None, None

            # 4. Extract post text — tự động fallback sang page-level nếu cần
            full_context = build_full_post_context(page, post_element)

        except Exception as exc:
            self.after(0, lambda n=acc_name, err=str(exc): self.append_live_log(
                f"[{n}] ⚠️ Lỗi khi quét bài: {err[:100]}"
            ))
            # Thử page-level fallback ngay cả khi lỗi
            try:
                from .fb_scraper import extract_post_data_from_page
                full_context = extract_post_data_from_page(page)
            except Exception:
                full_context = ""

        if full_context and len(full_context) >= 8:
            self.after(0, lambda n=acc_name, text=full_context, chars=len(full_context): self.append_live_log(
                f"[{n}] ✅ Đã quét được {chars} ký tự nội dung bài:\n"
                f"{text}\n"
                f"========================"
            ))
            return full_context, post_element

        # Không lấy được text — vẫn trả về post_element để comment box có thể tìm được
        self.after(0, lambda n=acc_name: self.append_live_log(
            f"[{n}] ⚠️ Không quét được nội dung bài rõ ràng (sẽ dùng nội dung mặc định)."
        ))
        return "", post_element






    def scan_comment_to_reply(self, page, acc_name, post_element=None):
        """Quét comment có phản hồi và click Reply button của đúng comment đó.

        Trả về (comment_text, already_clicked):
        - comment_text: text comment ('' nếu không tìm được)
        - already_clicked: True nếu đã click Reply button trong lúc scan
        - Trả None nếu bị ngắt (interrupted)
        """
        self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] 🔎 Bắt đầu quét comment cần trả lời..."))

        if not self.wait_if_paused():
            return None

        scan_root = post_element if post_element is not None else page
        comment_text = ""
        already_clicked = False
        try:
            text, clicked = fb_scan_and_click_reply_target(page, scan_root, click=True)
            comment_text = text
            already_clicked = clicked
        except Exception as exc:
            self.after(0, lambda n=acc_name, e=str(exc): self.append_live_log(
                f"[{n}] ⚠️ Lỗi khi quét comment: {e[:80]}"
            ))
            comment_text = ""

        if comment_text:
            if already_clicked:
                self.after(0, lambda n=acc_name, text=comment_text[:140]: self.append_live_log(
                    f"[{n}] 💬 Đã quét và click Reply vào comment: {text}{'...' if len(comment_text) > 140 else ''}"
                ))
            else:
                self.after(0, lambda n=acc_name, text=comment_text[:140]: self.append_live_log(
                    f"[{n}] 💬 Đã quét comment để reply: {text}{'...' if len(comment_text) > 140 else ''}"
                ))
            return (comment_text, already_clicked)

        # Fallback: lấy comment đầu tiên hiện thấy (không click)
        try:
            from facebook_caretool.fb_scraper import find_first_comment_text
            first_text = find_first_comment_text(scan_root)
        except Exception:
            first_text = ""

        if first_text:
            self.after(0, lambda n=acc_name, text=first_text[:140]: self.append_live_log(
                f"[{n}] 💬 Không thấy comment có phản hồi sẵn; dùng comment đầu tiên: {text}{'...' if len(first_text) > 140 else ''}"
            ))
            return (first_text, False)

        self.after(0, lambda n=acc_name: self.append_live_log(
            f"[{n}] ⚠️ Không quét được comment nào để trả lời."
        ))
        return ("", False)





    def extract_comment_text_near_reply_button(self, reply_button):
        try:
            raw_text = reply_button.evaluate(
                r"""
                (button) => {
                    const normalize = (text) => String(text || '').replace(/\s+/g, ' ').trim();
                    const actionNoise = /^(?:thích|like|bình luận|comment|chia sẻ|share|gửi|send|phản hồi|reply|trả lời|xem thêm|see more|ẩn bớt|see less)$/i;
                    const metaNoise = /^(?:\d+\s*(?:giây|phút|giờ|ngày|tuần|tháng|năm|s|m|h|d|w|mo|y)\s*(?:trước)?|vừa xong|just now|top fan|author)$/i;
                    const threadedReplyNoise = /^(?:(?:xem|view|see|ẩn|hide)\s*(?:tất cả|all)?\s*)?(?:\d+[.,]?\d*\s*)?(?:phản hồi|repl(?:y|ies)|trả lời|câu trả lời)(?:\s*(?:trước|older|mới hơn|newer))?$/i;
                    const isVisible = (element) => {
                        if (!(element instanceof HTMLElement)) return false;
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const cleanLines = (text) => String(text || '')
                        .split(/\n+/)
                        .map(normalize)
                        .filter((line) => line && line.length >= 2 && !actionNoise.test(line) && !metaNoise.test(line) && !threadedReplyNoise.test(line))
                        .filter((line) => !/^\d+[.,]?\d*\s*(k|m|n|tr)?\s*(thích|likes?|phản hồi|repl(?:y|ies))$/i.test(line));
                    let node = button.parentElement;
                    let best = '';
                    for (let depth = 0; node && depth < 8; depth += 1) {
                        if (!isVisible(node)) {
                            node = node.parentElement;
                            continue;
                        }
                        const lines = cleanLines(node.innerText || node.textContent || '');
                        const candidate = lines
                            .filter((line) => !/^(phản hồi|reply|trả lời|thích|like)$/i.test(line))
                            .sort((a, b) => b.length - a.length)[0] || '';
                        if (candidate.length > best.length) best = candidate;
                        if (best.length >= 12 && best.length <= 500) break;
                        node = node.parentElement;
                    }
                    return best.slice(0, 1200);
                }
                """
            )
        except Exception:
            return ""

        return re.sub(r"\s+", " ", (raw_text or "")).strip()[:1200]


    def extract_first_visible_comment_text(self, page):
        try:
            raw_text = page.evaluate(
                r"""
                () => {
                    const normalize = (text) => String(text || '').replace(/\s+/g, ' ').trim();
                    const actionNoise = /^(?:thích|like|bình luận|comment|chia sẻ|share|gửi|send|phản hồi|reply|trả lời|xem thêm|see more|ẩn bớt|see less)$/i;
                    const metaNoise = /^(?:\d+\s*(?:giây|phút|giờ|ngày|tuần|tháng|năm|s|m|h|d|w|mo|y)\s*(?:trước)?|vừa xong|just now|top fan|author)$/i;
                    const threadedReplyNoise = /^(?:(?:xem|view|see|ẩn|hide)\s*(?:tất cả|all)?\s*)?(?:\d+[.,]?\d*\s*)?(?:phản hồi|repl(?:y|ies)|trả lời|câu trả lời)(?:\s*(?:trước|older|mới hơn|newer))?$/i;
                    const isVisible = (element) => {
                        if (!(element instanceof HTMLElement)) return false;
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const cleanLines = (text) => String(text || '')
                        .split(/\n+/)
                        .map(normalize)
                        .filter((line) => line && line.length >= 2 && !actionNoise.test(line) && !metaNoise.test(line) && !threadedReplyNoise.test(line))
                        .filter((line) => !/^\d+[.,]?\d*\s*(k|m|n|tr)?\s*(thích|likes?|phản hồi|repl(?:y|ies)|bình luận|comments?)$/i.test(line));
                    const extractText = (root) => {
                        const textNodes = Array.from(root.querySelectorAll('div[dir="auto"], span[dir="auto"]'))
                            .filter((element) => isVisible(element) && !element.closest('[role="button"]'))
                            .flatMap((element) => cleanLines(element.innerText || element.textContent || ''));
                        const lines = textNodes.length ? textNodes : cleanLines(root.innerText || root.textContent || '');
                        return (lines
                            .filter((line) => !/^(phản hồi|reply|trả lời|thích|like)$/i.test(line))
                            .sort((a, b) => b.length - a.length)[0] || '').slice(0, 1200);
                    };
                    const selectors = [
                        'div[aria-label*="Comment by" i]',
                        'div[aria-label*="Bình luận của" i]',
                        'div[role="article"]',
                    ];
                    const seen = new Set();
                    for (const selector of selectors) {
                        const nodes = Array.from(document.querySelectorAll(selector));
                        const candidates = selector === 'div[role="article"]' && nodes.length > 1 ? nodes.slice(1) : nodes;
                        for (const node of candidates.slice(0, 30)) {
                            if (!isVisible(node)) continue;
                            const rect = node.getBoundingClientRect();
                            const key = `${Math.round(rect.top)}:${Math.round(rect.left)}:${Math.round(rect.width)}:${(node.textContent || '').trim().slice(0, 80)}`;
                            if (seen.has(key)) continue;
                            seen.add(key);
                            const text = extractText(node).replace(/\s+/g, ' ').trim();
                            if (text && text.length >= 2) return text;
                        }
                    }
                    return '';
                }
                """
            )
        except Exception:
            return ""

        return re.sub(r"\s+", " ", (raw_text or "")).strip()[:1200]

    def click_first_comment_reply_button(self, page, acc_name):
        """Fallback: nếu chưa có nút Reply trong danh sách đã quét, hover comment đầu tiên để nút Reply hiện ra rồi bấm."""
        try:
            labelled_comments = page.locator('div[aria-label*="Comment by" i], div[aria-label*="Bình luận của" i]')
            if labelled_comments.count() > 0:
                first_comment = labelled_comments.first
            else:
                article_comments = page.locator('div[role="article"]')
                first_comment = article_comments.nth(1) if article_comments.count() > 1 else article_comments.first
            first_comment.wait_for(state="visible", timeout=4000)
            first_comment.scroll_into_view_if_needed()
            first_comment.hover(timeout=3000)
            if not self.interruptible_sleep(random.uniform(0.5, 1.0)):
                return False

            reply_selectors = [
                "div[role='button']:has-text('Phản hồi')",
                "div[role='button']:has-text('Reply')",
                "span:has-text('Phản hồi')",
                "span:has-text('Reply')",
                "text=Phản hồi",
                "text=Reply",
            ]
            for selector in reply_selectors:
                try:
                    button = first_comment.locator(selector).first
                    if button.is_visible(timeout=1000):
                        return self.click_reply_button(button, acc_name, "Đã bấm Phản hồi vào comment đầu tiên.")
                except Exception:
                    continue

            for selector in reply_selectors:
                try:
                    button = page.locator(selector).first
                    if button.is_visible(timeout=1000):
                        return self.click_reply_button(button, acc_name, "Đã bấm Phản hồi vào comment đầu tiên.")
                except Exception:
                    continue
        except Exception:
            return False

        return False

    def click_existing_comment_reply_button(self, page, acc_name):
        """Chọn vị trí comment theo ưu tiên nghiệp vụ.

        Ưu tiên trả lời comment đã có phản hồi. Nếu không thấy comment như vậy,
        trả lời comment đầu tiên; tuyệt đối không chuyển sang comment thẳng vào bài.
        """
        reply_selectors = [
            "div[role='button'][aria-label='Phản hồi'], div[aria-label='Phản hồi']",
            "div[role='button'][aria-label='Reply'], div[aria-label='Reply']",
            "div[role='button']:has-text('Phản hồi')",
            "div[role='button']:has-text('Reply')",
            "text=Phản hồi",
            "text=Reply",
        ]
        # Giảm 6 → 4 vòng scroll; mỗi vòng sleep 0.6-1.0s thay vì 1.0-1.8s → tiết kiệm ~5s
        for scroll_round in range(4):
            if not self.wait_if_paused():
                return False

            for selector in reply_selectors:
                try:
                    reply_buttons = page.locator(selector)
                    button_count = min(reply_buttons.count(), 10)
                    for index in range(button_count):
                        button = reply_buttons.nth(index)
                        if not button.is_visible():
                            continue

                        if not self.is_comment_with_existing_replies(button):
                            continue

                        if self.click_reply_button(button, acc_name, "Đã bấm Phản hồi vào comment đã có phản hồi sẵn."):
                            return True
                        return False
                except Exception:
                    continue

            if scroll_round < 3:
                self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] Đang tìm comment đã có phản hồi sẵn..."))
                page.mouse.wheel(0, random.randint(500, 800))
                if not self.interruptible_sleep(random.uniform(0.6, 1.0)):
                    return False

        # Không tìm được comment có phản hồi → comment thẳng vào bài
        self.after(0, lambda n=acc_name: self.append_live_log(
            f"[{n}] ⚠️ Không quét được comment có phản hồi, sẽ comment thẳng vào bài."
        ))
        return ("", False)

    def remember_nearby_reply_button(self, reply_buttons: list[Any], button: Any) -> list[Any]:
        try:
            button_key = button.evaluate(
                r"""
                (button) => {
                    const rect = button.getBoundingClientRect();
                    const parentText = (button.parentElement?.parentElement?.innerText || '').slice(0, 120);
                    return `${Math.round(rect.top)}:${Math.round(rect.left)}:${parentText}`;
                }
                """
            )
        except Exception:
            button_key = str(id(button))

        for existing in reply_buttons:
            try:
                existing_key = existing.evaluate(
                    r"""
                    (button) => {
                        const rect = button.getBoundingClientRect();
                        const parentText = (button.parentElement?.parentElement?.innerText || '').slice(0, 120);
                        return `${Math.round(rect.top)}:${Math.round(rect.left)}:${parentText}`;
                    }
                    """
                )
            except Exception:
                existing_key = str(id(existing))
            if existing_key == button_key:
                return reply_buttons

        reply_buttons.append(button)
        return reply_buttons[-20:]

    def detect_post_comment_count(self, page):
        try:
            return page.evaluate(
                r"""
                () => {
                    const parseCompactNumber = (rawNumber) => {
                        const compact = String(rawNumber || '').trim().toLowerCase();
                        const multiplier = compact.includes('k') || compact.includes('n') ? 1000 : compact.includes('m') || compact.includes('tr') ? 1000000 : 1;
                        const numeric = compact.replace(/[,\.](?=\d{3}(\D|$))/g, '').replace(',', '.').replace(/[^\d.]/g, '');
                        const value = Number.parseFloat(numeric);
                        if (Number.isNaN(value)) return null;
                        return Math.round(value * multiplier);
                    };
                    const patterns = [
                        /(\d+(?:[.,]\d+)?\s*(?:k|m|n|tr)?)[\s\u00a0]*(?:bình luận|comments?)/i,
                        /(?:bình luận|comments?)[\s\u00a0]*(\d+(?:[.,]\d+)?\s*(?:k|m|n|tr)?)/i,
                    ];
                    const roots = [
                        document.querySelector('div[role="article"]'),
                        document.querySelector('div[role="main"]'),
                        document.body,
                    ].filter(Boolean);

                    for (const root of roots) {
                        const text = (root.innerText || root.textContent || '').replace(/\s+/g, ' ');
                        for (const pattern of patterns) {
                            const match = text.match(pattern);
                            if (!match) continue;
                            const value = parseCompactNumber(match[1]);
                            if (value !== null) return value;
                        }
                    }
                    return null;
                }
                """
            )
        except Exception:
            return None

    def is_comment_with_existing_replies(self, reply_button):
        try:
            return bool(reply_button.evaluate(
                r"""
                (button) => {
                    const replyWords = /(phản hồi|reply|replies|trả lời|câu trả lời)/i;
                    const threadedReplyText = /(xem|view|ẩn|hide|more|thêm|previous|trước|khác|other).{0,80}(phản hồi|repl(?:y|ies)|trả lời|câu trả lời)|\b\d+\s+(phản hồi|repl(?:y|ies)|trả lời|câu trả lời)\b/i;
                    let node = button;
                    for (let depth = 0; node && depth < 7; depth += 1) {
                        const text = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
                        if (text && text !== 'Phản hồi' && text !== 'Reply' && threadedReplyText.test(text)) {
                            return true;
                        }
                        node = node.parentElement;
                    }
                    const nearby = button.parentElement?.parentElement?.innerText || '';
                    return replyWords.test(nearby) && threadedReplyText.test(nearby);
                }
                """
            ))
        except Exception:
            return False

    def click_reply_button(self, button, acc_name, success_message):
        button.scroll_into_view_if_needed()
        if not self.interruptible_sleep(random.uniform(0.4, 0.9)):
            return False

        button.click()
        self.after(0, lambda n=acc_name, msg=success_message: self.append_live_log(f"[{n}] {msg}"))
        return True

    def find_reply_comment_box(self, page):
        reply_box_selectors = [
            'div[role="textbox"][contenteditable="true"][aria-label*="phản hồi" i]',
            'div[role="textbox"][contenteditable="true"][aria-label*="reply" i]',
            'div[role="textbox"][contenteditable="true"][aria-label*="trả lời" i]',
            'div[role="textbox"][contenteditable="true"][data-lexical-editor="true"]',
        ]

        for selector in reply_box_selectors:
            try:
                reply_box = page.locator(selector).last
                reply_box.wait_for(state="visible", timeout=5000)
                return reply_box
            except Exception:
                continue

        raise RuntimeError("Không tìm thấy ô nhập phản hồi")

    def focus_post_comment_box(self, page, acc_name):
        # Thêm :not() guard để tránh click nhầm ô reply
        comment_entry_selectors = [
            'div[role="textbox"][contenteditable="true"][aria-label*="bình luận" i]:not([aria-label*="phản hồi" i])',
            'div[role="textbox"][contenteditable="true"][aria-label*="comment" i]:not([aria-label*="reply" i])',
            'div[role="textbox"][contenteditable="true"][aria-label*="viết" i]',
            'div[role="textbox"][contenteditable="true"][data-lexical-editor="true"]:not([aria-label*="phản hồi" i]):not([aria-label*="reply" i]):not([aria-label*="tìm kiếm" i])',
            'div[role="textbox"][contenteditable="true"]:not([aria-label*="phản hồi" i]):not([aria-label*="reply" i]):not([aria-label*="tìm kiếm" i])',
        ]
        comment_button_selectors = [
            "div[role='button'][aria-label='Bình luận'], div[aria-label='Bình luận']",
            "div[role='button'][aria-label='Comment'], div[aria-label='Comment']",
            "span:has-text('Bình luận')",
            "span:has-text('Comment')",
            "div[role='button']:has-text('Bình luận')",
            "div[role='button']:has-text('Comment')",
        ]

        # Giảm 4 → 2 vòng; sleep 0.5-1.0s thay vì 1.0-1.8s → tiết kiệm ~5s
        for scroll_round in range(2):
            if not self.wait_if_paused():
                return None

            for selector in comment_entry_selectors:
                try:
                    comment_boxes = page.locator(selector)
                    box_count = min(comment_boxes.count(), 6)
                    for index in range(box_count):
                        comment_box = comment_boxes.nth(index)
                        if not comment_box.is_visible():
                            continue
                        # Kiểm tra lại aria-label để tránh nhầm reply box
                        aria = (comment_box.get_attribute("aria-label") or "").lower()
                        if "phản hồi" in aria or "reply" in aria or "trả lời" in aria:
                            continue
                        comment_box.scroll_into_view_if_needed()
                        comment_box.click()
                        self.after(0, lambda n=acc_name: self.append_live_log(
                            f"[{n}] Tìm thấy ô bình luận, bắt đầu nhập comment..."
                        ))
                        return comment_box
                except Exception:
                    continue

            for selector in comment_button_selectors:
                try:
                    comment_buttons = page.locator(selector)
                    button_count = min(comment_buttons.count(), 5)
                    for index in range(button_count):
                        button = comment_buttons.nth(index)
                        if not button.is_visible():
                            continue
                        label = ((button.text_content() or "") + " " + (button.get_attribute("aria-label") or "")).lower()
                        if "phản hồi" in label or "reply" in label or "trả lời" in label:
                            continue
                        button.scroll_into_view_if_needed()
                        button.click()
                        if not self.interruptible_sleep(random.uniform(0.6, 1.0)):
                            return None
                        for box_selector in comment_entry_selectors:
                            try:
                                comment_box = page.locator(box_selector).last
                                comment_box.wait_for(state="visible", timeout=4000)
                                comment_box.click()
                                self.after(0, lambda n=acc_name: self.append_live_log(
                                    f"[{n}] Tìm thấy ô bình luận (sau click button), bắt đầu nhập..."
                                ))
                                return comment_box
                            except Exception:
                                continue
                except Exception:
                    continue

            if scroll_round < 1:
                self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] Đang tìm ô comment của bài viết..."))
                page.mouse.wheel(0, -500)
                if not self.interruptible_sleep(random.uniform(0.5, 1.0)):
                    return None

        raise RuntimeError("Không tìm thấy ô nhập comment của bài viết")

    def attach_media_to_comment(self, page, selected_image_path):
        """Đính kèm ảnh/video vào ô comment hiện tại với nhiều selector Facebook khác nhau."""
        attach_button_selectors = [
            "div[role='button'][aria-label*='Ảnh/video' i]",
            "div[role='button'][aria-label*='Photo/video' i]",
            "div[role='button'][aria-label*='Thêm ảnh' i]",
            "div[role='button'][aria-label*='Add photo' i]",
            "div[role='button'][aria-label*='Đính kèm' i]",
            "div[role='button'][aria-label*='Attach' i]",
            "div[role='button'][aria-label*='Camera' i]",
            "div[aria-label*='Ảnh/video' i]",
            "div[aria-label*='Photo/video' i]",
            "div[aria-label*='Đính kèm' i]",
            "div[aria-label*='Attach' i]",
        ]

        for selector in attach_button_selectors:
            try:
                buttons = page.locator(selector)
                button_count = min(buttons.count(), 8)
                for index in range(button_count):
                    button = buttons.nth(index)
                    if not button.is_visible():
                        continue
                    button.scroll_into_view_if_needed()
                    with page.expect_file_chooser(timeout=5000) as fc_info:
                        button.click()
                    fc_info.value.set_files(selected_image_path)
                    return True
            except Exception:
                continue

        file_input_selectors = [
            "input[type='file'][accept*='image' i]",
            "input[type='file'][accept*='video' i]",
            "input[type='file']",
        ]
        for selector in file_input_selectors:
            try:
                file_inputs = page.locator(selector)
                input_count = min(file_inputs.count(), 8)
                for index in range(input_count):
                    file_inputs.nth(index).set_input_files(selected_image_path, timeout=5000)
                    return True
            except Exception:
                continue

        return False

    def type_and_submit_comment(self, page, comment_box, final_content, selected_image_path, acc_name, action_name):
        comment_box.scroll_into_view_if_needed()
        comment_box.wait_for(state="visible", timeout=8000)

        comment_box.click()
        # Giảm sleep sau click: 0.4-0.8s thay vì 1-2s
        if not self.interruptible_sleep(random.uniform(0.4, 0.8)):
            return False

        self.after(0, lambda n=acc_name, action=action_name: self.append_live_log(
            f"[{n}] Đang nhập {action}: '{final_content[:30]}...'"
        ))

        # Dùng fill() thay vì keyboard.type(delay=...) → nhanh hơn ~10x
        try:
            comment_box.fill(final_content)
        except Exception:
            # Fallback: Ctrl+A → Backspace → type nhanh nếu fill() không khả dụng
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            if not self.interruptible_sleep(0.3):
                return False
            page.keyboard.type(final_content, delay=random.uniform(25, 60))

        # Giảm sleep sau nhập: 0.8-1.5s thay vì 1.5-2.5s
        if not self.interruptible_sleep(random.uniform(0.8, 1.5)):
            return False

        if selected_image_path:
            image_name = os.path.basename(selected_image_path)
            self.after(0, lambda n=acc_name, img=image_name, action=action_name: self.append_live_log(
                f"[{n}] Đang đính kèm ảnh/video cùng {action}: {img}"
            ))
            try:
                if not self.attach_media_to_comment(page, selected_image_path):
                    raise RuntimeError("Không tìm thấy nút hoặc input tải ảnh/video trong khung comment")
                if not self.interruptible_sleep(random.uniform(3.5, 5.5)):
                    return False

                self.after(0, lambda n=acc_name, action=action_name: self.append_live_log(
                    f"[{n}] Đã đính kèm, chuẩn bị gửi {action} chung text + ảnh/video..."
                ))
                comment_box.click()
                if not self.interruptible_sleep(1.0):
                    return False
            except Exception as exc:
                self.after(0, lambda n=acc_name, err=str(exc): self.append_live_log(
                    f"[{n}] ❌ Không gửi comment vì ảnh/video đi kèm chưa đính kèm được: {err[:80]}"
                ))
                raise

        page.keyboard.press("Enter")
        return True


    def get_ai_comment_settings(self):
        """Lấy cấu hình AI comment mới nhất từ UI/settings."""
        enabled = bool(self.app_settings.get("ai_comment_enabled", True))
        if hasattr(self, "ai_comment_enabled_var"):
            enabled = bool(self.ai_comment_enabled_var.get())
        profile_dir = str(self.app_settings.get("chatgpt_profile_dir") or DEFAULT_CHATGPT_PROFILE_DIR).strip()
        if hasattr(self, "chatgpt_profile_entry"):
            profile_dir = self.chatgpt_profile_entry.get().strip() or DEFAULT_CHATGPT_PROFILE_DIR
        return {
            "enabled": enabled,
            "profile_dir": profile_dir,
            "provider": str(self.app_settings.get("ai_provider") or "").strip(),
            "api_key": str(self.app_settings.get("ai_api_key") or "").strip(),
            "model": str(self.app_settings.get("ai_model") or "").strip(),
        }

    def _get_ai_generator(self):
        """Khởi tạo AI generator 1 lần rồi cache lại. Trả về None nếu chưa cài đặt."""
        provider = str(self.app_settings.get("ai_provider") or "").strip()
        api_key  = str(self.app_settings.get("ai_api_key") or "").strip()
        model    = str(self.app_settings.get("ai_model") or "").strip()
        if not provider or not api_key or api_key == "PASTE_YOUR_API_KEY_HERE":
            return None
        cache_key = (provider, api_key, model)
        if getattr(self, "_ai_gen_cache_key", None) != cache_key:
            try:
                from .ai_comment import make_ai_generator
                self._ai_gen = make_ai_generator(provider=provider, api_key=api_key, model=model)
                self._ai_gen_cache_key = cache_key
                self.after(0, lambda p=provider: self.append_live_log(f"✅ AI provider '{p}' sẵn sàng."))
            except Exception as exc:
                self.after(0, lambda e=exc: self.append_live_log(f"⚠️ Khởi tạo AI provider lỗi: {e}"))
                return None
        return getattr(self, "_ai_gen", None)

    def resolve_chatgpt_profile_dir(self, profile_dir):
        """Chuẩn hoá profile ChatGPT để log rõ Chrome đang dùng đúng thư mục nào."""
        profile_dir = str(profile_dir or DEFAULT_CHATGPT_PROFILE_DIR).strip() or DEFAULT_CHATGPT_PROFILE_DIR
        return os.path.abspath(os.path.expanduser(profile_dir))

    def acquire_chatgpt_browser_lock(self, acc_name, profile_dir):
        """Chờ Chrome ChatGPT rảnh, đồng thời log định kỳ để người dùng biết vì sao chưa thấy paste."""
        waited_seconds = 0
        while not self.task_stop_event.is_set():
            if self.chatgpt_browser_lock.acquire(timeout=1):
                if waited_seconds:
                    self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] ✅ Chrome ChatGPT đã rảnh, bắt đầu mở profile để paste prompt."))
                return True
            waited_seconds += 1
            if waited_seconds == 5 or waited_seconds % 15 == 0:
                self.after(0, lambda n=acc_name, d=profile_dir, w=waited_seconds: self.append_live_log(
                    f"[{n}] ⏳ Vẫn đang chờ Chrome ChatGPT/profile rảnh ({w}s): {d}. "
                    "Nếu bạn đang mở cửa sổ ChatGPT đăng nhập thủ công, hãy đăng nhập xong rồi đóng cửa sổ đó để tool paste prompt."
                ))
        return False

    def extract_images_for_ai(self, page, post_element, acc_name: str) -> list[str]:
        """Trích xuất ảnh bài viết thành base64 để truyền vào AI.

        PHẢI chạy trong main Playwright thread (không dùng trong executor).
        Trả về list base64 string (an toàn để pass across threads).
        """
        self.after(0, lambda n=acc_name: self.append_live_log(
            f"[{n}] 🔍 Đang tìm ảnh trong bài..."
        ))
        try:
            import time as _ti
            # Scroll để trigger lazy-load (Facebook dùng IntersectionObserver)
            try:
                page.mouse.wheel(0, 600)
                _ti.sleep(0.8)
                page.mouse.wheel(0, -200)
                _ti.sleep(0.4)
                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                except Exception:
                    pass
            except Exception:
                pass

            # extract_post_images trả về (filtered_list, total_raw)
            img_result = extract_post_images(post_element, page=page)
            img_infos, total_raw = img_result if isinstance(img_result, tuple) else (img_result, 0)

            self.after(0, lambda n=acc_name, c=len(img_infos), r=total_raw: self.append_live_log(
                f"[{n}] 🖼️ Tìm thấy {c} ảnh CDN / {r} ảnh thô trong bài."
            ))

            if img_infos:
                images_b64 = download_post_images_as_base64(page, img_infos, max_images=2)
                if images_b64:
                    self.after(0, lambda n=acc_name, c=len(images_b64): self.append_live_log(
                        f"[{n}] 🖼️ Đã chụp {c} ảnh (screenshot), AI sẽ nhận cả ảnh + text."
                    ))
                    return images_b64
                else:
                    self.after(0, lambda n=acc_name: self.append_live_log(
                        f"[{n}] ⚠️ Không chụp được ảnh (element không tìm thấy / ẩn), tiếp tục với text."
                    ))
            elif total_raw > 0:
                self.after(0, lambda n=acc_name, r=total_raw: self.append_live_log(
                    f"[{n}] ℹ️ Tìm thấy {r} ảnh nhưng không phải FB CDN (link preview?), bỏ qua."
                ))
            else:
                self.after(0, lambda n=acc_name: self.append_live_log(
                    f"[{n}] ℹ️ Bài không có ảnh (text-only post)."
                ))
        except Exception as img_exc:
            self.after(0, lambda n=acc_name, e=str(img_exc)[:80]: self.append_live_log(
                f"[{n}] ⚠️ Lỗi extract ảnh: {e}"
            ))
        return []

    def build_comment_from_scanned_content(self, page, scanned_post_text, fallback_content, acc_name, ai_comment_settings, target_comment_text="", post_element=None, post_images=None):
        """Sinh comment bằng AI.
        NOTE: hàm này chạy trong ThreadPoolExecutor (thread khác).
        TUYTỆT ĐỐI không được gọi page/post_element Playwright API ở đây!
        Tham số post_images là list base64 string đã được extract trước từ main thread.
        """
        if not ai_comment_settings.get("enabled"):
            self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] ❌ AI comment đang tắt, bỏ qua link."))
            return None

        post_images = post_images or []


        # ——— Ưu tiên AI API (Groq/Gemini/OpenRouter/Ollama) ———
        ai_gen = self._get_ai_generator()
        if ai_gen is not None:
            provider = self.app_settings.get("ai_provider", "AI")
            img_note = f" + {len(post_images)} ảnh" if post_images else ""
            self.after(0, lambda n=acc_name, p=provider, note=img_note: self.append_live_log(
                f"[{n}] 🧠 Sinh comment bằng {p.upper()} API{note}..."
            ))
            try:
                comment = ai_gen(scanned_post_text, target_comment_text or None, post_images or None)
            except Exception as exc:
                self.after(0, lambda n=acc_name, e=str(exc): self.append_live_log(
                    f"[{n}] ⚠️ AI API lỗi: {e[:120]}. Fallback sang ChatGPT web..."
                ))
                comment = None

            if comment and comment != "SKIP_COMMENT":
                # Validate trước khi chấp nhận
                from .utils import validate_ai_comment as _vld
                ok, reason = _vld(comment)
                if not ok:
                    reason_vi = {
                        "no_diacritics": "tiếng Việt không dấu",
                        "too_long": "quá dài",
                        "too_short": "quá ngắn",
                        "generic": "comment chung chung/banned",
                        "spam_filter": "spam filter",
                    }.get(reason, reason)
                    self.after(0, lambda n=acc_name, c=comment[:60], r=reason_vi: self.append_live_log(
                        f"[{n}] ⚠️ AI comment bị loại ({r}): '{c}' → thử lại fallback."
                    ))
                    comment = None
                else:
                    self.after(0, lambda n=acc_name, c=comment: self.append_live_log(
                        f"[{n}] ✅ AI sinh comment: {c}"
                    ))
                    return comment

            if comment == "SKIP_COMMENT":
                self.after(0, lambda n=acc_name: self.append_live_log(
                    f"[{n}] ⚠️ AI trả về SKIP_COMMENT, bỏ qua bài."
                ))
                return None

        # --- Fallback: ChatGPT web thủ công ---
        profile_dir = ai_comment_settings.get("profile_dir") or DEFAULT_CHATGPT_PROFILE_DIR
        self.after(0, lambda n=acc_name, d=profile_dir: self.append_live_log(
            f"[{n}] 🧠 Gửi request sang Chrome ChatGPT riêng ({d}), paste prompt..."
        ))
        try:
            chat_comment = self.generate_comment_with_manual_chatgpt(
                scanned_post_text, acc_name, target_comment_text, profile_dir
            )
        except Exception as exc:
            self.after(0, lambda n=acc_name, err=str(exc): self.append_live_log(
                f"[{n}] ❌ ChatGPT thủ công lỗi: {err[:160]}"
            ))
            return None

        if chat_comment == "SKIP_COMMENT":
            self.after(0, lambda n=acc_name: self.append_live_log(
                f"[{n}] ⚠️ ChatGPT trả về SKIP_COMMENT, bỏ qua bài."
            ))
            return None
        if chat_comment:
            return chat_comment

        self.after(0, lambda n=acc_name: self.append_live_log(
            f"[{n}] ⚠️ Không tạo được comment hợp lệ, bỏ qua bài."
        ))
        return None

    def generate_comment_with_manual_chatgpt(self, scanned_post_text, acc_name, target_comment_text="", chatgpt_profile_dir=DEFAULT_CHATGPT_PROFILE_DIR):
        prompt = build_ai_comment_prompt(scanned_post_text, target_comment_text)
        chatgpt_profile_dir = self.resolve_chatgpt_profile_dir(chatgpt_profile_dir)
        self.after(0, lambda n=acc_name, d=chatgpt_profile_dir: self.append_live_log(f"[{n}] 🔐 Dùng phiên đăng nhập ChatGPT đã lưu trong Chrome/profile riêng '{d}' (không nạp cookie thủ công)."))
        self.after(0, lambda n=acc_name, d=chatgpt_profile_dir: self.append_live_log(f"[{n}] 🔒 Chờ Chrome ChatGPT riêng nhận request: {d}"))
        if not self.acquire_chatgpt_browser_lock(acc_name, chatgpt_profile_dir):
            return None
        try:
            # Luồng comment Facebook cũng đang dùng Playwright Sync API. Nếu mở thêm
            # ChatGPT trong cùng worker đó, Playwright sẽ báo đang chạy trong asyncio
            # loop. Tách riêng một thread cho phiên ChatGPT để tránh lồng sync API.
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="chatgpt-playwright") as executor:
                future = executor.submit(
                    self._generate_comment_with_manual_chatgpt_locked,
                    prompt,
                    acc_name,
                    chatgpt_profile_dir,
                )
                return future.result()
        finally:
            try:
                self.chatgpt_browser_lock.release()
            except RuntimeError:
                pass

    def _generate_comment_with_manual_chatgpt_locked(self, prompt, acc_name, chatgpt_profile_dir):
        with sync_playwright() as chat_playwright:
            self.after(0, lambda n=acc_name, d=chatgpt_profile_dir: self.append_live_log(f"[{n}] 🌐 Đang mở Chrome ChatGPT bằng profile: {d}"))
            chat_context = chat_playwright.chromium.launch_persistent_context(
                chatgpt_profile_dir,
                channel="chrome",
                headless=False,
                viewport={"width": 1280, "height": 900},
                locale="vi-VN",
                timezone_id="Asia/Ho_Chi_Minh",
                args=[
                    "--disable-features=Translate",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            chat_session_token = self.register_playwright_session(context=chat_context)
            try:
                chat_page = chat_context.pages[0] if chat_context.pages else chat_context.new_page()
                chat_page.goto("https://chatgpt.com/?temporary-chat=true", wait_until="domcontentloaded", timeout=90000)
                try:
                    chat_page.wait_for_load_state("networkidle", timeout=25000)
                except Exception:
                    pass
                if not self.interruptible_sleep(3):
                    return None

                composer_selectors = [
                    "#prompt-textarea",
                    "div[contenteditable='true'][id='prompt-textarea']",
                    "textarea[data-testid='prompt-textarea']",
                    "textarea[placeholder*='Message' i]",
                    "textarea[placeholder*='Nhắn' i]",
                    "div[contenteditable='true']",
                ]
                composer = None
                for selector in composer_selectors:
                    try:
                        candidate = chat_page.locator(selector).last
                        if candidate.is_visible(timeout=3000):
                            composer = candidate
                            break
                    except Exception:
                        continue
                if composer is None:
                    raise RuntimeError(f"Không tìm thấy ô nhập ChatGPT. Hãy đăng nhập https://chatgpt.com trong Chrome profile riêng '{chatgpt_profile_dir}' rồi chạy lại.")

                self.attach_images_to_chatgpt(chat_page, self.scanned_post_image_paths, acc_name)

                assistant_selector = "[data-message-author-role='assistant'], div.markdown.prose, .markdown"
                try:
                    before_count = chat_page.locator(assistant_selector).count()
                except Exception:
                    before_count = 0

                self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] 📋 Đã thấy ô nhập ChatGPT, đang paste prompt + dữ liệu bài/comment đã quét..."))
                composer.click(timeout=10000)
                chat_page.keyboard.press("Control+A")
                chat_page.keyboard.insert_text(prompt)
                if not self.interruptible_sleep(random.uniform(0.5, 1.2)):
                    return None

                send_selectors = "[data-testid='send-button'], button[aria-label*='Send' i], button[aria-label*='Gửi' i]"
                try:
                    send_button = chat_page.locator(send_selectors).last
                    if send_button.is_visible(timeout=2500):
                        send_button.click(timeout=10000)
                        self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] ✅ Đã bấm nút gửi prompt sang ChatGPT."))
                    else:
                        chat_page.keyboard.press("Enter")
                        self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] ✅ Đã gửi prompt sang ChatGPT bằng phím Enter."))
                except Exception:
                    chat_page.keyboard.press("Enter")
                    self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] ✅ Đã gửi prompt sang ChatGPT bằng phím Enter."))

                self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] ⏳ Đang chờ ChatGPT trả comment trên web..."))
                try:
                    chat_page.wait_for_function(
                        "([selector, count]) => document.querySelectorAll(selector).length > count",
                        arg=[assistant_selector, before_count],
                        timeout=180000,
                    )
                except Exception:
                    pass
                try:
                    chat_page.wait_for_function(
                        "() => !document.querySelector('[data-testid=\"stop-button\"], button[aria-label*=\"Stop\" i], button[aria-label*=\"Dừng\" i]')",
                        timeout=180000,
                    )
                except Exception:
                    pass
                if not self.interruptible_sleep(1.5):
                    return None

                responses = chat_page.locator(assistant_selector).evaluate_all(
                    "nodes => nodes.map(node => (node.innerText || node.textContent || '').trim()).filter(Boolean)"
                )
                raw_comment = responses[-1] if responses else ""
                is_valid, reason = validate_ai_comment(raw_comment, min_words=7)
                if is_valid:
                    return raw_comment.strip().strip('"“”')
                if reason == "skip":
                    return "SKIP_COMMENT"
                self.after(0, lambda n=acc_name, r=reason, c=raw_comment[:120]: self.append_live_log(f"[{n}] ⚠️ Comment ChatGPT không hợp lệ ({r}): {c}"))
                return None
            finally:
                self.unregister_playwright_session(chat_session_token)
                try:
                    chat_context.close()
                except Exception:
                    pass

    def attach_images_to_chatgpt(self, chat_page, image_paths, acc_name):
        valid_paths = [path for path in (image_paths or []) if os.path.exists(path)]
        if not valid_paths:
            return
        file_selectors = [
            "input[type='file'][accept*='image' i]",
            "input[type='file'][multiple]",
        ]
        for selector in file_selectors:
            try:
                nodes = chat_page.locator(selector)
                for index in range(min(nodes.count(), 3)):
                    file_input = nodes.nth(index)
                    try:
                        file_input.set_input_files(valid_paths, timeout=10000)
                        self.after(0, lambda n=acc_name, count=len(valid_paths): self.append_live_log(f"[{n}] 📎 Đã đính kèm {count} ảnh vào ChatGPT."))
                        return
                    except Exception:
                        continue
            except Exception:
                continue
        self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] ⚠️ Không tìm thấy ô tải file ảnh trên ChatGPT, chỉ gửi dữ liệu chữ đã quét."))

    def run_comment_task(
        self,
        account_indexes,
        urls,
        raw_content,
        comment_limit,
        comment_image_paths=None,
        max_parallel_tabs=1,
        like_before_comment=True,
        scan_before_comment=True,
        direct_comment=False,
    ):
        delay_range = self.delay_cmt_input.get()
        comment_image_paths = [path for path in (comment_image_paths or []) if os.path.exists(path)]
        ai_comment_settings = self.get_ai_comment_settings()
        auto_contextual_mode = scan_before_comment and not (raw_content or "").strip()
        stats_lock = threading.Lock()
        success_count = 0
        failed_count = 0

        comment_payloads = build_comment_payloads(raw_content, comment_image_paths)

        if auto_contextual_mode:
            comment_payloads = [{"text": "", "media_path": comment_image_paths[0] if comment_image_paths else ""}]

            if ai_comment_settings.get("enabled"):
                log_message = "🤖 Đang chạy chế độ ChatGPT thủ công: mỗi bài sẽ quét nội dung bài + comment cần trả lời, paste vào chatgpt.com trong Chrome/profile ChatGPT riêng bạn đã tự đăng nhập rồi lấy reply trả về."
            else:
                log_message = "❌ ChatGPT thủ công đang tắt, bỏ qua link vì không thể tạo comment."

            self.after(0, lambda msg=log_message: self.append_live_log(msg))

        if not comment_payloads:
            self.after(0, lambda: messagebox.showwarning("Lỗi", "Không tìm thấy nội dung comment hợp lệ!"))
            return

        if comment_image_paths:
            self.after(
                0,
                lambda count=len(comment_image_paths): self.append_live_log(
                    f"📷 Ảnh/video sẽ đi kèm từng comment (không gửi tách riêng). Đang dùng {count} file."
                ),
            )

        max_parallel_tabs = max(1, min(max_parallel_tabs, len(account_indexes)))
        self.after(
            0,
            lambda tabs=max_parallel_tabs: self.append_live_log(
                f"🧵 Chiến dịch comment chạy song song tối đa {tabs} tab/account."
            ),
        )

        acc_tasks: dict[int, list[str]] = {acc_idx: [] for acc_idx in account_indexes}
        skipped_urls = 0
        if account_indexes:
            account_cursor = 0
            total_accounts = len(account_indexes)
            for url in urls:
                assigned_any = False
                checked_accounts = 0
                while checked_accounts < total_accounts:
                    acc_idx = account_indexes[account_cursor]
                    account_cursor = (account_cursor + 1) % total_accounts
                    checked_accounts += 1
                    if len(acc_tasks[acc_idx]) >= comment_limit:
                        continue
                    acc_tasks[acc_idx].append(url)
                    assigned_any = True
                    break
                if not assigned_any:
                    skipped_urls += 1
                    self.after(0, lambda u=url: self.append_live_log(f"⚠️ Tất cả tài khoản đã đạt giới hạn, bỏ qua link: {u[:60]}..."))

        if skipped_urls:
            self.after(
                0,
                lambda count=skipped_urls, limit=comment_limit: self.append_live_log(
                    f"⚠️ Đã bỏ qua {count} link vì toàn bộ tài khoản đã đạt giới hạn {limit} link/tài khoản."
                ),
            )

        runnable_tasks = [
            (acc_idx, acc_urls)
            for acc_idx, acc_urls in acc_tasks.items()
            if acc_urls and acc_idx < len(self.accounts)
        ]

        def run_account_comment_task(acc_idx, acc_urls):
            nonlocal success_count, failed_count
            if self.is_task_stopped():
                return

            account = self.accounts[acc_idx]
            acc_name = account.get("name", "Unknown")
            if not self.ensure_proxy_action_allowed(account):
                return
            self.after(0, lambda n=acc_name, count=len(acc_urls): self.append_live_log(f"🚀 [{n}] Được phân công chạy {count} link."))

            browser = None
            try:
                cookies = self.load_cookies(account)
                with sync_playwright() as p:
                    browser, context, page = self.create_browser_page(p, cookies, account)

                    # KIỂM TRA & AUTO ĐĂNG NHẬP NẾU CHƯA CÓ COOKIE
                    self.ensure_login(context, page, account)

                    # Kiểm tra checkpoint ngay sau login (FB đôi khi redirect về checkpoint)
                    if self.is_checkpoint_url(page.url):
                        self.set_account_state(account, "checkpoint",
                            reason=f"Phát hiện checkpoint sau đăng nhập: {page.url[:80]}",
                            log_name=acc_name)
                        return

                    if not self.ensure_proxy_action_allowed(account):
                        return
                    dashboard_url = "https://www.facebook.com/professional_dashboard/"
                    self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] 🧭 Mở Professional Dashboard để lấy lượt xem..."))
                    self.safe_goto(page, dashboard_url, account=account)
                    
                    if self.is_checkpoint_url(page.url):
                        self.set_account_state(account, "checkpoint",
                            reason=f"Phát hiện checkpoint khi vào dashboard: {page.url[:80]}",
                            log_name=acc_name)
                        return
                        
                    # Lấy lượt xem bằng JS
                    try:
                        page.wait_for_timeout(3000) # Đợi load
                        views_text = page.evaluate('''() => {
                            let walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
                            let node;
                            while(node = walker.nextNode()) {
                                if (node.nodeValue.trim().toLowerCase() === "lượt xem") {
                                    let parent = node.parentElement;
                                    while (parent && parent.innerText) {
                                        let lines = parent.innerText.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
                                        let idx = lines.findIndex(l => l.toLowerCase() === "lượt xem");
                                        if (idx > 0) {
                                            for(let k = idx - 1; k >= 0; k--) {
                                                if (/^[\\d,.]+$/.test(lines[k])) {
                                                    return lines[k];
                                                }
                                            }
                                        }
                                        parent = parent.parentElement;
                                        if (parent && parent.tagName === 'BODY') break;
                                    }
                                }
                            }
                            return null;
                        }''')
                        if views_text:
                            old_views_raw = account.get("views_count", "").split(" ")[0].replace(",", "").replace(".", "")
                            new_views_raw = views_text.replace(",", "").replace(".", "")
                            
                            diff_str = ""
                            try:
                                if old_views_raw and new_views_raw.isdigit() and old_views_raw.isdigit():
                                    diff = int(new_views_raw) - int(old_views_raw)
                                    if diff > 0:
                                        diff_str = f" (+{diff})"
                                    elif diff < 0:
                                        diff_str = f" ({diff})"
                            except Exception:
                                pass
                                
                            final_views_text = f"{views_text}{diff_str}"
                            self.after(0, lambda n=acc_name, v=final_views_text: self.append_live_log(f"[{n}] 👁️ Lượt xem từ dashboard: {v}"))
                            account["views_count"] = final_views_text
                            self.save_accounts()
                            try:
                                idx = self.accounts.index(account)
                                self.after(0, lambda i=idx: self.refresh_account_row(i))
                            except ValueError:
                                pass
                        else:
                            self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] ⚠️ Không tìm thấy số lượt xem trên dashboard."))
                    except Exception as e:
                        self.after(0, lambda n=acc_name, err=str(e): self.append_live_log(f"[{n}] ⚠️ Lỗi lấy lượt xem: {err[:100]}"))
                    
                    if not self.interruptible_sleep(random.uniform(2, 4)):
                        return

                    for url in acc_urls:
                        if not self.wait_if_paused():
                            break
                        comment_payload = random.choice(comment_payloads)
                        fallback_content = spin_content(comment_payload["text"])
                        final_content = fallback_content
                        selected_image_path = comment_payload.get("media_path") or None

                        self.after(0, lambda n=acc_name, u=url: self.append_live_log(f"[{n}] Đang vào bài: {u[:40]}..."))
                        try:
                            self.safe_goto(page, url, account=account)
                        except Exception as nav_exc:
                            nav_err = str(nav_exc)
                            if self.is_proxy_error(nav_err):
                                # Proxy lỗi → đã được set_account_state trong safe_goto
                                self.after(0, lambda n=acc_name: self.append_live_log(
                                    f"[{n}] 🔌 Proxy lỗi khi vào bài, dừng tài khoản này."
                                ))
                                break
                            self.after(0, lambda n=acc_name, e=nav_err[:80]: self.append_live_log(
                                f"[{n}] ⚠️ Không vào được bài: {e}"
                            ))
                            continue

                        # Kiểm tra checkpoint / đăng xuất / block ngay sau khi vào bài
                        page_problem = self.detect_page_problem(page)
                        if page_problem == "checkpoint":
                            self.set_account_state(account, "checkpoint",
                                reason="Phát hiện trang checkpoint sau khi vào bài",
                                log_name=acc_name)
                            break
                        elif page_problem == "logged_out":
                            self.after(0, lambda n=acc_name: self.append_live_log(
                                f"[{n}] 🔓 Phát hiện đăng xuất giữa phiên comment, đang đăng nhập lại..."
                            ))
                            try:
                                self.ensure_login(context, page, account)
                                if self.is_checkpoint_url(page.url):
                                    self.set_account_state(account, "checkpoint",
                                        reason="Checkpoint sau khi đăng nhập lại",
                                        log_name=acc_name)
                                    break
                                self.after(0, lambda n=acc_name: self.append_live_log(
                                    f"[{n}] ✅ Đăng nhập lại thành công, thử lại bài hiện tại..."
                                ))
                                # Quay lại URL bài để thử lại
                                try:
                                    self.safe_goto(page, url, account=account)
                                except Exception:
                                    pass
                            except Exception as login_exc:
                                self.after(0, lambda n=acc_name, e=str(login_exc)[:80]: self.append_live_log(
                                    f"[{n}] ❌ Đăng nhập lại thất bại: {e}. Dừng tài khoản này."
                                ))
                                break
                            continue
                        elif page_problem == "spam_block":
                            self.after(0, lambda n=acc_name: self.append_live_log(
                                f"[{n}] 🚫 Tài khoản bị block/giới hạn, bỏ qua bài này."
                            ))
                            continue

                        # Giảm sleep sau goto: 2.5-4s thay vì 4-7s
                        if not self.interruptible_sleep(random.uniform(2.5, 4.0)):
                            break

                        # Ép cứng: Nếu URL có chứa "/photo" thì ưu tiên tìm nút Xem bài viết
                        if "/photo/" in page.url or "/photo" in page.url or "/photo.php" in page.url:
                            self.after(0, lambda n=acc_name: self.append_live_log(
                                f"[{n}] 📸 Link ảnh (/photo), ép tìm nút 'Xem bài viết'..."
                            ))
                            try:
                                # Sử dụng Regex không phân biệt chữ hoa chữ thường
                                import re
                                view_post_btn = page.get_by_role("link", name=re.compile(r"xem bài viết|view post", re.IGNORECASE)).first
                                
                                # Nếu vẫn không có, tìm bằng CSS selectors
                                if not view_post_btn.is_visible(timeout=1000):
                                    view_post_btn = page.locator("div[role='link']:has-text('Xem bài viết'), div[role='link']:has-text('View post'), a:has-text('Xem bài viết'), a:has-text('View post')").locator("visible=true").last

                                if view_post_btn.is_visible(timeout=3000):
                                    view_post_btn.click(timeout=5000, force=True)
                                    # Chờ URL chuyển sang bài viết gốc
                                    try:
                                        page.wait_for_load_state("domcontentloaded", timeout=5000)
                                    except: pass
                                    if not self.interruptible_sleep(random.uniform(2.5, 4.0)):
                                        break
                                else:
                                    self.after(0, lambda n=acc_name: self.append_live_log(
                                        f"[{n}] ℹ️ Không có nút 'Xem bài viết', tiếp tục xử lý trực tiếp trên giao diện ảnh..."
                                    ))
                            except Exception as e:
                                self.after(0, lambda n=acc_name, err=str(e): self.append_live_log(
                                    f"[{n}] ℹ️ Bỏ qua click Xem bài viết: {err[:50]}"
                                ))
                        else:
                            # Giữ phương án dự phòng cho các link không phải /photo nhưng vẫn bật popup ảnh
                            try:
                                view_post_btn = page.locator(
                                    "text='Xem bài viết', text='View post'"
                                ).locator("visible=true").last
                                
                                if view_post_btn.is_visible(timeout=1500):
                                    self.after(0, lambda n=acc_name: self.append_live_log(
                                        f"[{n}] 📸 Phát hiện chế độ xem ảnh popup, đang mở bài viết gốc..."
                                    ))
                                    view_post_btn.click(timeout=3000, force=True)
                                    if not self.interruptible_sleep(random.uniform(2.5, 4.0)):
                                        break
                            except Exception:
                                pass


                        page.mouse.wheel(0, 400)
                        # Giảm sleep sau scroll: 0.8s thay vì 2s cố định
                        if not self.interruptible_sleep(0.8):
                            break

                        target_comment_text = ""
                        post_element = None
                        reply_already_clicked = False
                        if scan_before_comment:
                            # scan_facebook_content_before_comment giờ trả về (text, post_element)
                            scan_result = self.scan_facebook_content_before_comment(page, acc_name)
                            if scan_result is None or (isinstance(scan_result, tuple) and scan_result[0] is None):
                                break
                            if isinstance(scan_result, tuple):
                                scanned_post_text, post_element = scan_result
                            else:
                                scanned_post_text, post_element = scan_result, None

                            # Scan comment scoped vào post_element nếu có
                            # scan_comment_to_reply giờ trả về (text, already_clicked)
                            if direct_comment:
                                scan_comment_result = ("", False)
                                self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] 🎯 Đang bật chế độ bình luận thẳng vào bài..."))
                            else:
                                scan_comment_result = self.scan_comment_to_reply(page, acc_name, post_element)
                            if scan_comment_result is None:
                                break
                            if isinstance(scan_comment_result, tuple):
                                target_comment_text, reply_already_clicked = scan_comment_result
                            else:
                                target_comment_text, reply_already_clicked = scan_comment_result, False

                            if target_comment_text is None:
                                break

                            if not target_comment_text and not scanned_post_text:
                                with stats_lock:
                                    failed_count += 1
                                    self.after(0, lambda s=success_count, f=failed_count: self.update_comment_stats(success=s, failed=f))
                                delay_sec = self.get_pause_seconds(delay_range)
                                self.after(0, lambda n=acc_name, d=delay_sec: self.append_live_log(
                                    f"[{n}] ⏳ Bỏ qua link vì không quét được nội dung, nghỉ {int(d)} giây..."
                                ))
                                if not self.interruptible_sleep(delay_sec):
                                    break
                                continue

                            # --- EXTRACT ẢNH: phải làm TRONG main Playwright thread trước khi submit executor ---
                            post_images_for_ai: list[str] = []
                            if post_element is not None and scanned_post_text:
                                post_images_for_ai = self.extract_images_for_ai(
                                    page, post_element, acc_name
                                )

                            # --- AI SONG SONG: gọi AI trong khi chuẩn bị click reply ---
                            _ai_future = None
                            if scanned_post_text:
                                _exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-parallel")
                                _ai_future = _exec.submit(
                                    self.build_comment_from_scanned_content,
                                    page, scanned_post_text, fallback_content,
                                    acc_name, ai_comment_settings, target_comment_text,
                                    None,   # post_element KHÔNG truyền (sai thread)
                                    post_images_for_ai,   # truyền base64 list đã sẵn
                                )

                            # Like bài (trong khi AI đang chạy ngầm)
                            if like_before_comment:
                                try:
                                    like_scope = post_element if post_element is not None else page
                                    like_btn = like_scope.locator("div[aria-label='Thích'], div[aria-label='Like']").first
                                    if like_btn.is_visible(timeout=2000):
                                        like_btn.click()
                                        if not self.interruptible_sleep(random.uniform(1.0, 2.0)):
                                            break
                                except Exception:
                                    pass

                            # Lấy kết quả AI (đã chạy song song ở trên)
                            if _ai_future is not None:
                                try:
                                    final_content = _ai_future.result(timeout=30)
                                except Exception as ai_exc:
                                    self.after(0, lambda n=acc_name, e=str(ai_exc): self.append_live_log(
                                        f"[{n}] ⚠️ AI timeout/lỗi: {e[:80]}"
                                    ))
                                    final_content = None
                                finally:
                                    _exec.shutdown(wait=False)

                            if not final_content:
                                with stats_lock:
                                    failed_count += 1
                                    self.after(0, lambda s=success_count, f=failed_count: self.update_comment_stats(success=s, failed=f))
                                delay_sec = self.get_pause_seconds(delay_range)
                                self.after(0, lambda n=acc_name, d=delay_sec: self.append_live_log(
                                    f"[{n}] ⏳ Bỏ qua link, nghỉ {int(d)} giây trước link tiếp theo..."
                                ))
                                if not self.interruptible_sleep(delay_sec):
                                    break
                                continue
                            self.after(
                                0,
                                lambda n=acc_name, text=final_content: self.append_live_log(
                                    f"[{n}] 💬 AI đề xuất: {text}"
                                ),
                            )

                        # Like bài đã được xử lý song song với AI ở phía trên (scan_before_comment path).
                        # Nếu không chạy scan_before_comment, xử lý like ở đây.
                        if like_before_comment and not scan_before_comment:
                            try:
                                like_scope = post_element if post_element is not None else page
                                like_btn = like_scope.locator("div[aria-label='Thích'], div[aria-label='Like']").first
                                if like_btn.is_visible(timeout=2000):
                                    like_btn.click()
                                    if not self.interruptible_sleep(random.uniform(1.0, 2.0)):
                                        break
                            except Exception:
                                pass

                        comment_success = False
                        try:
                            if target_comment_text:
                                # Nếu scan đã click đúng button rồi → không click lại
                                if not reply_already_clicked:
                                    if not self.click_existing_comment_reply_button(page, acc_name):
                                        raise RuntimeError("Không tìm thấy nút Phản hồi để đăng reply")
                                comment_box = self.find_reply_comment_box(page)
                                action_name = "reply"
                            else:
                                comment_box = self.focus_post_comment_box(page, acc_name)
                                action_name = "comment"
                            if comment_box is None:
                                break

                            if not self.type_and_submit_comment(
                                page,
                                comment_box,
                                final_content,
                                selected_image_path,
                                acc_name,
                                action_name,
                            ):
                                break

                            comment_success = True
                            with stats_lock:
                                success_count += 1
                                self.after(0, lambda s=success_count, f=failed_count: self.update_comment_stats(success=s, failed=f))
                            self.after(0, lambda n=acc_name, action=action_name: self.append_live_log(f"[{n}] ✅ Đã đăng {action} thành công."))

                            # Kiểm tra checkpoint sau khi submit comment
                            # (FB đôi khi redirect về checkpoint sau khi nhấn Enter)
                            try:
                                post_problem = self.detect_page_problem(page)
                                if post_problem == "checkpoint":
                                    self.set_account_state(account, "checkpoint",
                                        reason="Phát hiện checkpoint ngay sau khi đăng comment",
                                        log_name=acc_name)
                                    break
                                elif post_problem == "spam_block":
                                    self.after(0, lambda n=acc_name: self.append_live_log(
                                        f"[{n}] 🚫 Phát hiện bị block sau comment, dừng chiến dịch cho tài khoản này."
                                    ))
                                    break
                            except Exception:
                                pass

                        except Exception as e:
                            with stats_lock:
                                failed_count += 1
                                self.after(0, lambda s=success_count, f=failed_count: self.update_comment_stats(success=s, failed=f))
                            err_str = str(e)
                            if "Không tìm thấy ô nhập comment" in err_str:
                                self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] ⏭️ Bỏ qua: Không có ô nhập comment (Có thể bài viết bị khóa comment)."))
                            else:
                                self.after(0, lambda n=acc_name, err=err_str[:80]: self.append_live_log(f"[{n}] ❌ Lỗi khi gửi comment: {err}"))
                            # Kiểm tra nếu lỗi là do checkpoint/proxy
                            if self.is_proxy_error(err_str):
                                self.set_account_state(account, "proxy_error",
                                    reason=err_str[:120], log_name=acc_name)
                                break
                            try:
                                err_problem = self.detect_page_problem(page)
                                if err_problem == "checkpoint":
                                    self.set_account_state(account, "checkpoint",
                                        reason="Phát hiện checkpoint khi đăng comment", log_name=acc_name)
                                    break
                            except Exception:
                                pass

                        delay_sec = self.get_pause_seconds(delay_range)
                        if comment_success:
                            if not self.browse_during_comment_pause(page, account, delay_sec):
                                break
                        else:
                            self.after(0, lambda n=acc_name, d=delay_sec: self.append_live_log(f"[{n}] ⏳ Đang nghỉ {int(d)} giây trước link tiếp theo..."))
                            if not self.interruptible_sleep(delay_sec):
                                break
            except Exception as e:
                self.after(0, lambda n=acc_name, err=str(e): self.append_live_log(f"[{n}] ❌ Lỗi profile: {err}"))
            finally:
                if browser:
                    try:
                        browser.close()
                    except Exception:
                        pass

        with ThreadPoolExecutor(max_workers=max_parallel_tabs) as executor:
            futures = [
                executor.submit(run_account_comment_task, acc_idx, acc_urls)
                for acc_idx, acc_urls in runnable_tasks
            ]
            for future in as_completed(futures):
                if self.is_task_stopped():
                    break
                try:
                    future.result()
                except Exception as exc:
                    self.after(0, lambda err=str(exc): self.append_live_log(f"❌ Lỗi tab comment: {err}"))

        if self.is_task_stopped():
            self.after(0, lambda: self.append_live_log("=== ⏹ ĐÃ DỪNG CHIẾN DỊCH COMMENT ==="))
        else:
            self.after(0, lambda: self.append_live_log("=== 🎉 HOÀN THÀNH CHIẾN DỊCH COMMENT ==="))
            self.after(0, lambda: messagebox.showinfo("Hoàn thành", "Đã chạy xong chiến dịch comment!"))

    # --- LOGIC TRÌNH DUYỆT / LỊCH SỬ / CÀI ĐẶT ---
    def _cancel_browser_render_job(self):
        if self.browser_render_job:
            self.after_cancel(self.browser_render_job)
            self.browser_render_job = None

    def refresh_browser_accounts(self):
        if not hasattr(self, "browser_accounts_scroll"):
            return
        self._cancel_browser_render_job()
        self.browser_render_generation += 1
        generation = self.browser_render_generation

        for widget in self.browser_accounts_scroll.winfo_children():
            widget.destroy()
        if not self.accounts:
            ctk.CTkLabel(self.browser_accounts_scroll, text="Chưa có tài khoản nào.").pack(pady=20)
            return

        accounts_snapshot = list(enumerate(self.accounts))
        total = len(accounts_snapshot)

        def render_browser_row(index, acc):
            row = ctk.CTkFrame(self.browser_accounts_scroll, fg_color="#1f2937" if index != self.browser_selected_index else "#263244", corner_radius=10)
            row.pack(fill="x", padx=4, pady=4)
            row.grid_columnconfigure(0, weight=1)
            title = f"{acc.get('name', 'Không tên')}  •  {self.status_text(acc.get('status', 'active'))}"
            ctk.CTkLabel(row, text=title, font=("Arial", 14, "bold"), anchor="w").grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 0))
            subtitle = f"Proxy: {acc.get('proxy') or 'Không dùng'} | Cookie: {os.path.basename(acc.get('cookie_file', '')) or 'Tự tạo khi login'}"
            ctk.CTkLabel(row, text=subtitle, text_color="#9ca3af", anchor="w").grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
            ctk.CTkButton(row, text="Chọn", width=70, command=lambda idx=index: self.select_browser_account(idx)).grid(row=0, column=1, rowspan=2, padx=12, pady=8)

        def render_batch(start=0):
            if generation != self.browser_render_generation:
                return
            end = min(start + BROWSER_RENDER_BATCH_SIZE, total)
            for index, acc in accounts_snapshot[start:end]:
                render_browser_row(index, acc)
            if end < total:
                self.browser_render_job = self.after(1, lambda: render_batch(end))
            else:
                self.browser_render_job = None

        render_batch()

    def select_browser_account(self, index):
        self.browser_selected_index = index
        account = self.accounts[index]
        self.browser_selected_label.configure(
            text=f"Đang chọn: {account.get('name', 'Không tên')}\nTrạng thái: {self.status_text(account.get('status', 'active'))}\nProxy: {account.get('proxy') or 'Không dùng'}"
        )
        self.refresh_browser_accounts()

    def open_browser_selected_url(self, url=None):
        if self.browser_selected_index is None:
            messagebox.showwarning("Thông báo", "Hãy chọn tài khoản ở màn Trình duyệt trước.")
            return
        target_url = url or self.browser_url_entry.get().strip() or self.app_settings.get("default_home_url", "https://www.facebook.com/")
        account = self.accounts[self.browser_selected_index]
        account["last_open"] = datetime.now().strftime("%d/%m/%Y %H:%M")
        self.save_accounts()
        if self.browser_selected_index is not None and self.browser_selected_index < len(self.accounts):
            self.refresh_account_row(self.browser_selected_index)
        self.refresh_browser_accounts()
        threading.Thread(target=self.open_browser, args=(account, target_url), daemon=True).start()

    def schedule_history_refresh(self, event=None):
        if self.history_refresh_job:
            self.after_cancel(self.history_refresh_job)
        self.history_refresh_job = self.after(220, lambda: self.refresh_history_view(reload_logs=False))

    def _cancel_history_render_job(self):
        if self.history_render_job:
            self.after_cancel(self.history_render_job)
            self.history_render_job = None

    def refresh_history_view(self, reload_logs=True):
        if not hasattr(self, "history_scroll"):
            return
        self.history_refresh_job = None
        self._cancel_history_render_job()
        self.history_render_generation += 1
        generation = self.history_render_generation

        if reload_logs:
            self.logs = self.storage.load_logs()
        summary = summarize_logs(self.logs)
        account_summary = summarize_accounts(self.accounts)
        status_counts = summary["by_status"]
        today_label = datetime.now().strftime("%d/%m/%Y")
        self.history_total_card.configure(text=str(summary["total"]))
        self.history_done_card.configure(text=str(status_counts.get("done", 0)))
        self.history_error_card.configure(text=str(status_counts.get("error", 0) + status_counts.get("cookie_error", 0)))
        self.history_today_card.configure(text=str(summary["by_day"].get(today_label, 0)))

        filter_text = self.history_filter_entry.get().strip().lower() if hasattr(self, "history_filter_entry") else ""
        for widget in self.history_scroll.winfo_children():
            widget.destroy()

        logs = summary["latest"]
        if filter_text:
            logs = [log for log in logs if filter_text in str(log.get("account", "")).lower() or filter_text in str(log.get("status", "")).lower()]

        if not logs:
            ctk.CTkLabel(self.history_scroll, text="Chưa có lịch sử phù hợp.", font=("Arial", 16)).pack(pady=30)
        else:
            total = len(logs)

            def render_batch(start=0):
                if generation != self.history_render_generation:
                    return
                end = min(start + HISTORY_RENDER_BATCH_SIZE, total)
                for log in logs[start:end]:
                    self.history_log_row(log)
                if end < total:
                    self.history_render_job = self.after(1, lambda: render_batch(end))
                else:
                    self.history_render_job = None

            render_batch()

        text = [
            "TÀI KHOẢN HIỆN TẠI",
            f"- Tổng: {account_summary['total']}",
            f"- Live: {account_summary['active']}",
            f"- Checkpoint: {account_summary['checkpoint']}",
            f"- Die: {account_summary['cookie_error']}",
            "",
            "THEO TRẠNG THÁI LOG",
        ]
        text.extend(f"- {status}: {count}" for status, count in sorted(summary["by_status"].items()))
        text.extend(["", "THEO NGÀY"])
        text.extend(f"- {day}: {count}" for day, count in sorted(summary["by_day"].items(), reverse=True)[:20])
        text.extend(["", "TOP ACCOUNT"])
        text.extend(f"- {account}: {count}" for account, count in sorted(summary["by_account"].items(), key=lambda item: item[1], reverse=True)[:20])
        self.history_summary_text.configure(state="normal")
        self.history_summary_text.delete("1.0", "end")
        self.history_summary_text.insert("end", "\n".join(text))
        self.history_summary_text.configure(state="disabled")

    def history_log_row(self, log):
        row = ctk.CTkFrame(self.history_scroll, fg_color="#1f2937", corner_radius=10)
        row.pack(fill="x", padx=10, pady=5)
        row.grid_columnconfigure(1, weight=1)
        status = log.get("status", "unknown")
        color = "#16a34a" if status == "done" else "#dc2626" if status in ("error", "cookie_error") else "#d97706" if status == "stopped" else "#475569"
        ctk.CTkLabel(row, text=status, fg_color=color, corner_radius=8, padx=10, pady=5).grid(row=0, column=0, rowspan=2, padx=10, pady=10, sticky="w")
        title = f"{log.get('account', 'Không tên')} • {log.get('action', 'care')}"
        ctk.CTkLabel(row, text=title, font=("Arial", 14, "bold"), anchor="w").grid(row=0, column=1, sticky="ew", padx=8, pady=(8, 0))
        time_text = log.get("start_time") or log.get("time") or "Không rõ thời gian"
        if log.get("end_time"):
            time_text += f" → {log.get('end_time')}"
        if log.get("error"):
            time_text += f" | Lỗi: {log.get('error')}"
        ctk.CTkLabel(row, text=time_text, text_color="#9ca3af", anchor="w", wraplength=850).grid(row=1, column=1, sticky="ew", padx=8, pady=(0, 8))

    def export_history_csv(self):
        path = filedialog.asksaveasfilename(title="Xuất lịch sử CSV", defaultextension=".csv", filetypes=[("CSV Files", "*.csv")])
        if not path:
            return
        import csv
        fields = ["account", "status", "action", "start_time", "end_time", "time", "error"]
        with open(path, "w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self.logs)
        messagebox.showinfo("Thành công", f"Đã xuất lịch sử: {path}")

    def refresh_settings_info(self):
        if hasattr(self, "settings_data_label"):
            self.settings_data_label.configure(text=f"Accounts file: {ACCOUNTS_FILE}\nLogs file: {LOGS_FILE}\nTổng account: {len(self.accounts)}\nTổng log: {len(self.logs)}")

    def sync_ai_comment_settings_from_widgets(self):
        if not hasattr(self, "ai_comment_enabled_var"):
            return
        self.app_settings["ai_comment_enabled"] = self.ai_comment_enabled_var.get()
        if hasattr(self, "chatgpt_profile_entry"):
            self.app_settings["chatgpt_profile_dir"] = self.chatgpt_profile_entry.get().strip() or DEFAULT_CHATGPT_PROFILE_DIR
        self.app_settings.pop("ai_comment_api_key", None)
        self.app_settings.pop("ai_comment_model", None)
        self.app_settings.pop("ai_comment_base_url", None)

    def save_app_settings(self):
        self.save_app_settings_without_popup()
        self.refresh_settings_widgets()
        messagebox.showinfo("Đã lưu", "Đã lưu cài đặt.")

    def backup_all_data(self):
        if not messagebox.askyesno(
            "Xác nhận backup",
            "File backup đầy đủ sẽ chứa account, password, 2FA, cookie, lịch sử và cài đặt. Chỉ lưu ở nơi an toàn. Tiếp tục?",
        ):
            return
        default_name = f"facebook-caretool-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        path = filedialog.asksaveasfilename(
            title="Backup đầy đủ dữ liệu",
            initialfile=default_name,
            defaultextension=".json",
            filetypes=[("JSON Files", "*.json")],
        )
        if not path:
            return
        try:
            self.save_app_settings_without_popup()
            save_full_backup_file(path, self.accounts, self.logs, self.app_settings, include_cookie_files=True)
            messagebox.showinfo("Backup hoàn tất", f"Đã lưu toàn bộ dữ liệu vào file:\n{path}")
        except Exception as exc:
            messagebox.showerror("Backup lỗi", str(exc))

    def import_full_backup_file(self):
        path = filedialog.askopenfilename(title="Import backup đầy đủ", filetypes=[("JSON Files", "*.json")])
        if not path:
            return
        if not messagebox.askyesno(
            "Xác nhận import",
            "Import backup sẽ tự động thêm lại tài khoản, khôi phục cookie/lịch sử/cài đặt và có thể ghi đè account trùng nếu bạn bật tùy chọn ghi đè. Tiếp tục?",
        ):
            return
        try:
            backup_payload = load_full_backup_file(path)
            backup_path = backup_accounts_file(ACCOUNTS_FILE)
            self.accounts, self.logs, self.app_settings, stats = restore_full_backup(
                backup_payload,
                self.accounts,
                self.logs,
                self.app_settings,
                overwrite_accounts=self.import_overwrite_var.get(),
            )
            self.save_accounts()
            self.save_logs()
            self.save_json("settings.json", self.app_settings)
            self.refresh_accounts()
            self.refresh_comment_accounts()
            self.refresh_browser_accounts()
            self.refresh_history_view()
            self.refresh_settings_widgets()
            self.refresh_settings_info()
            backup_note = f"\nBackup accounts cũ: {backup_path}" if backup_path else ""
            messagebox.showinfo(
                "Import backup hoàn tất",
                (
                    f"Account thêm: {stats['added']} | Cập nhật: {stats['updated']} | Bỏ qua trùng: {stats['skipped']}\n"
                    f"Log khôi phục: {stats['logs_added']} | File cookie khôi phục: {stats['files_restored']}"
                    f" | File bỏ qua: {stats['files_skipped']}{backup_note}"
                ),
            )
        except Exception as exc:
            messagebox.showerror("Import backup lỗi", str(exc))

    def save_app_settings_without_popup(self):
        self.save_comment_content(show_message=False)
        if hasattr(self, "default_url_entry"):
            self.app_settings["default_home_url"] = self.default_url_entry.get().strip() or "https://www.facebook.com/"
        if hasattr(self, "export_sensitive_var"):
            self.app_settings["export_sensitive_default"] = self.export_sensitive_var.get()
        if hasattr(self, "import_overwrite_var"):
            self.app_settings["import_overwrite_default"] = self.import_overwrite_var.get()
        self.sync_ai_comment_settings_from_widgets()
        self.save_json("settings.json", self.app_settings)

    def refresh_settings_widgets(self):
        if hasattr(self, "default_url_entry"):
            self.default_url_entry.delete(0, "end")
            self.default_url_entry.insert(0, self.app_settings.get("default_home_url", "https://www.facebook.com/"))
        if hasattr(self, "export_sensitive_var"):
            self.export_sensitive_var.set(bool(self.app_settings.get("export_sensitive_default", False)))
        if hasattr(self, "import_overwrite_var"):
            self.import_overwrite_var.set(bool(self.app_settings.get("import_overwrite_default", False)))
        if hasattr(self, "ai_comment_enabled_var"):
            self.ai_comment_enabled_var.set(bool(self.app_settings.get("ai_comment_enabled", True)))
        if hasattr(self, "chatgpt_profile_entry"):
            self.chatgpt_profile_entry.delete(0, "end")
            self.chatgpt_profile_entry.insert(0, self.app_settings.get("chatgpt_profile_dir", DEFAULT_CHATGPT_PROFILE_DIR))
        if hasattr(self, "browser_url_entry"):
            self.browser_url_entry.delete(0, "end")
            self.browser_url_entry.insert(0, self.app_settings.get("default_home_url", "https://www.facebook.com/"))


    def open_chatgpt_login_browser(self):
        """Mở Chrome/profile ChatGPT để người dùng tự đăng nhập và lưu phiên."""
        self.save_app_settings_without_popup()
        profile_dir = self.resolve_chatgpt_profile_dir(self.get_ai_comment_settings().get("profile_dir") or DEFAULT_CHATGPT_PROFILE_DIR)

        def worker():
            try:
                self.after(0, lambda d=profile_dir: self.append_live_log(f"[ChatGPT] 🌐 Đang mở Chrome ChatGPT để bạn tự đăng nhập: {d}"))
                with self.chatgpt_browser_lock:
                    with sync_playwright() as playwright:
                        context = playwright.chromium.launch_persistent_context(
                            profile_dir,
                            channel="chrome",
                            headless=False,
                            viewport={"width": 1280, "height": 900},
                            locale="vi-VN",
                            timezone_id="Asia/Ho_Chi_Minh",
                            args=[
                                "--disable-features=Translate",
                                "--disable-dev-shm-usage",
                                "--disable-blink-features=AutomationControlled",
                            ],
                        )
                        session_token = self.register_playwright_session(context=context)
                        try:
                            page = context.pages[0] if context.pages else context.new_page()
                            page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=90000)
                            self.after(0, lambda d=profile_dir: messagebox.showinfo(
                                "Đăng nhập ChatGPT",
                                f"Chrome ChatGPT đã mở. Hãy đăng nhập trong cửa sổ đó.\n\n"
                                f"Sau khi thấy ô chat ChatGPT, bạn có thể đóng cửa sổ Chrome. Phiên đăng nhập sẽ được lưu trong profile:\n{d}",
                            ))
                            while context.pages and not self.is_task_stopped():
                                time.sleep(1)
                        finally:
                            self.unregister_playwright_session(session_token)
                            try:
                                context.close()
                            except Exception:
                                pass
                self.after(0, lambda d=profile_dir: self.append_live_log(f"[ChatGPT] ✅ Đã đóng Chrome ChatGPT. Phiên đăng nhập đã được lưu trong profile: {d}"))
            except Exception as exc:
                self.after(0, lambda e=str(exc): messagebox.showerror("Không mở được ChatGPT", e))
                self.after(0, lambda e=str(exc): self.append_live_log(f"[ChatGPT] ❌ Không mở được Chrome ChatGPT: {e[:160]}"))

        threading.Thread(target=worker, daemon=True).start()
    def optimize_proxies(self):
        all_proxies = set()
        for acc in self.accounts:
            p = (acc.get("proxy") or "").strip()
            if p:
                all_proxies.add(p)
                
        if not all_proxies:
            messagebox.showinfo("Thông báo", "Hệ thống chưa có proxy nào được sử dụng.")
            return

        dialog = ctk.CTkInputDialog(text="Mỗi proxy nên có tối đa bao nhiêu acc Live (khuyên dùng: 3 hoặc 4)?", title="Tối ưu Proxy")
        try:
            val = dialog.get_input()
            if val is None:
                return
            target_count = int(val)
        except Exception:
            messagebox.showwarning("Lỗi", "Vui lòng nhập một số hợp lệ.")
            return

        if target_count < 1:
            return

        proxy_live_count = {p: 0 for p in all_proxies}
        for acc in self.accounts:
            if acc.get("status") == "active":
                p = (acc.get("proxy") or "").strip()
                if p in proxy_live_count:
                    proxy_live_count[p] += 1
                    
        underutilized_proxies = [p for p, c in proxy_live_count.items() if c < target_count]
        
        if not underutilized_proxies:
            messagebox.showinfo("Thông báo", f"Tất cả proxy đều đã đủ {target_count} acc Live! Không cần tối ưu thêm.")
            return
            
        available_accs = [acc for acc in self.accounts if acc.get("status") == "active" and not (acc.get("proxy") or "").strip()]
        
        if not available_accs:
            messagebox.showinfo("Thông báo", "Không có tài khoản Live nào trống proxy để điền vào chỗ thiếu.")
            return
            
        assigned_count = 0
        acc_idx = 0
        for proxy in underutilized_proxies:
            needed = target_count - proxy_live_count[proxy]
            while needed > 0 and acc_idx < len(available_accs):
                acc = available_accs[acc_idx]
                acc["proxy"] = proxy
                mark_proxy_changed(acc)
                assigned_count += 1
                needed -= 1
                acc_idx += 1
                
        self.save_accounts()
        self.refresh_accounts()
        messagebox.showinfo("Thành công", f"Đã tự động phân bổ {assigned_count} tài khoản trống proxy vào các proxy bị thiếu slot!")

    def export_accounts_safe(self):
        include_sensitive = self.export_sensitive_var.get()
        if include_sensitive and not messagebox.askyesno("Xác nhận bảo mật", "File export sẽ chứa password và 2FA. Chỉ lưu ở nơi an toàn. Tiếp tục?"):
            return
        path = filedialog.asksaveasfilename(title="Export accounts", defaultextension=".json", filetypes=[("JSON Files", "*.json")])
        if not path:
            return
        save_export_file(path, self.accounts, include_sensitive=include_sensitive)
        messagebox.showinfo("Thành công", "Đã export account. Mặc định dữ liệu nhạy cảm được loại bỏ nếu bạn không bật tùy chọn.")

    def import_accounts_safe(self):
        path = filedialog.askopenfilename(title="Import accounts", filetypes=[("JSON Files", "*.json")])
        if not path:
            return
        try:
            imported = load_import_accounts(path)
            backup_path = backup_accounts_file(ACCOUNTS_FILE)
            self.accounts, stats = merge_accounts(self.accounts, imported, overwrite=self.import_overwrite_var.get())
            self.save_accounts()
            self.refresh_accounts()
            self.refresh_comment_accounts()
            self.refresh_browser_accounts()
            self.refresh_settings_info()
            backup_note = f"\nBackup cũ: {backup_path}" if backup_path else ""
            messagebox.showinfo("Import hoàn tất", f"Thêm: {stats['added']} | Cập nhật: {stats['updated']} | Bỏ qua trùng: {stats['skipped']}{backup_note}")
        except Exception as exc:
            messagebox.showerror("Import lỗi", str(exc))

    # --- ĐIỀU KHIỂN TASK: PAUSE / STOP ---
    def reset_task_state(self):
        self.task_stop_event.clear()
        self.task_pause_event.set()
        self.update_pause_buttons(False)
        self._set_log_badge("idle")
        # Reset running stats counters
        self._care_running_count = 0
        self._care_error_count = 0
        self._care_paused = False
        self._update_care_run_stats()

    def _set_log_badge(self, state: str):
        """Cập nhật badge trạng thái trong log panel. state: 'idle'|'running'|'paused'|'done'"""
        config = {
            "idle":    ("\u25cf Chờ",        "#94a3b8"),
            "running": ("\u25cf Đang chạy", "#4ade80"),
            "paused":  ("\u23f8 Tạm dừng", "#fbbf24"),
            "done":    ("\u2713 Hoàn thành", "#38bdf8"),
            "error":   ("\u26a0 Lỗi",        "#f87171"),
        }
        text, color = config.get(state, config["idle"])
        if hasattr(self, "log_status_badge"):
            self.after(0, lambda t=text, c=color:
                self.log_status_badge.configure(text=t, text_color=c))

    def _update_care_run_stats(self):
        """Cập nhật 3 badges: Chạy / Lỗi / Dừng trong sidebar phải."""
        r = getattr(self, "_care_running_count", 0)
        e = getattr(self, "_care_error_count", 0)
        p = 1 if getattr(self, "_care_paused", False) and r > 0 else 0
        if hasattr(self, "running_count_label"):
            self.after(0, lambda rv=r: self.running_count_label.configure(text=str(rv)))
        if hasattr(self, "error_count_label"):
            self.after(0, lambda ev=e: self.error_count_label.configure(text=str(ev)))
        if hasattr(self, "paused_count_label"):
            self.after(0, lambda pv=p: self.paused_count_label.configure(text=str(pv)))

    def update_pause_buttons(self, paused):
        text = "▶ Tiếp tục" if paused else "⏸ Tạm dừng"
        color = "#16a34a" if paused else "#475569"
        for button_name in ("care_pause_button", "comment_pause_button"):
            button = getattr(self, button_name, None)
            if button:
                try:
                    button.configure(text=text, fg_color=color)
                except Exception:
                    pass

    def toggle_pause_task(self):
        if self.task_pause_event.is_set():
            self.task_pause_event.clear()
            self.update_pause_buttons(True)
            self._care_paused = True
            self._update_care_run_stats()
            self._set_log_badge("paused")
            self.append_live_log("⏸ Đã tạm dừng task đang chạy.")
        else:
            self.task_pause_event.set()
            self.update_pause_buttons(False)
            self._care_paused = False
            self._update_care_run_stats()
            self._set_log_badge("running" if getattr(self, "_care_running_count", 0) > 0 else "idle")
            self.append_live_log("▶ Đã tiếp tục task đang chạy.")

    def stop_task(self):
        self.task_stop_event.set()
        self.task_pause_event.set()
        self.update_pause_buttons(False)
        self._care_paused = False
        self._set_log_badge("idle")
        self._update_care_run_stats()
        self.append_live_log("⏹ Đã gửi lệnh dừng task. Task sẽ dừng sau bước hiện tại.")

    def is_task_stopped(self):
        return self.task_stop_event.is_set()

    def wait_if_paused(self):
        while not self.task_pause_event.is_set():
            if self.is_task_stopped():
                return False
            time.sleep(0.2)
        return not self.is_task_stopped()

    def interruptible_sleep(self, seconds):
        end_time = time.time() + seconds
        while time.time() < end_time:
            if not self.wait_if_paused():
                return False
            # Poll 500ms thay vì 200ms — giảm CPU khi nhiều acc chạy song song
            time.sleep(min(0.5, max(0, end_time - time.time())))
        return not self.is_task_stopped()

    # --- CÁC HÀM TIỆN ÍCH CHUNG ---
    def configure_table_columns(self, frame):
        widths = [42, 150, 140, 115, 160, 130, 90, 150, 140]
        for col, width in enumerate(widths):
            frame.grid_columnconfigure(col, minsize=width, weight=1 if col in (1, 2, 7) else 0)

    def get_care_profile_menu_values(self):
        return list(CARE_PROFILE_LABELS.values())

    def get_care_profile_key_from_label(self, label):
        for profile_key, profile_text in CARE_PROFILE_LABELS.items():
            if profile_text == label:
                return profile_key
        return "auto"

    def update_account_care_profile(self, index, selected_label):
        if index < 0 or index >= len(self.accounts):
            return

        profile_key = self.get_care_profile_key_from_label(selected_label)
        self.accounts[index]["care_profile"] = profile_key
        self.save_accounts()

        if self.selected_index == index:
            self.select_account(index)
        else:
            self.refresh_selected_account_plan()

        self.append_live_log(
            f"[{self.accounts[index].get('name', 'Không tên')}] Đổi kiểu nuôi: {profile_label(profile_key)}"
        )

    def dashboard_card(self, parent, title, value, color, col):
        card = ctk.CTkFrame(parent, fg_color=color, corner_radius=14)
        card.grid(row=0, column=col, sticky="ew", padx=6)
        label_title = ctk.CTkLabel(card, text=title, text_color="#e5e7eb", anchor="w")
        label_title.pack(fill="x", padx=16, pady=(10, 0))
        label_value = ctk.CTkLabel(card, text=value, font=("Arial", 26, "bold"), anchor="w")
        label_value.pack(fill="x", padx=16, pady=(0, 10))
        return label_value

    def _stat_card(self, parent, title, value, bg_color, border_color, col):
        """Compact stat card used by the redesigned care view dashboard."""
        card = ctk.CTkFrame(
            parent, fg_color=bg_color, corner_radius=12,
            border_width=1, border_color=border_color
        )
        card.grid(row=0, column=col, sticky="nsew", padx=4)
        ctk.CTkLabel(
            card, text=title, font=("Arial", 11), text_color="#cbd5e1", anchor="w"
        ).pack(fill="x", padx=10, pady=(8, 0))
        lbl = ctk.CTkLabel(
            card, text=value, font=("Arial", 24, "bold"), text_color="#f0fdf4", anchor="w"
        )
        lbl.pack(fill="x", padx=10, pady=(0, 8))
        return lbl

    def status_color(self, status):
        return {
            "active":      "#16a34a",
            "checkpoint":  "#d97706",
            "cookie_error":"#dc2626",
            "proxy_error": "#7c3aed",
        }.get(status, "#6b7280")

    def status_text(self, status):
        return {
            "active":       "Live",
            "checkpoint":   "Checkpoint",
            "cookie_error": "Die",
            "proxy_error":  "Proxy Lỗi",
        }.get(status, "Không rõ")

    def append_live_log(self, message):
        """Thêm dòng log — dùng buffer để batch write, giảm áp lực lên Tkinter event loop."""
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        self.log_lines.append(line)
        if len(self.log_lines) > 80:
            self.log_lines.pop(0)

        # Buffer: gom nhiều dòng, flush 1 lần sau 80ms thay vì write ngay mỗi dòng
        if not hasattr(self, "_log_buffer"):
            self._log_buffer = []
        self._log_buffer.append(line)
        if not getattr(self, "_log_flush_scheduled", False):
            self._log_flush_scheduled = True
            self.after(80, self._flush_log_buffer)

    def _flush_log_buffer(self):
        """Ghi toàn bộ buffer log vào Text widget trong 1 lần duy nhất."""
        self._log_flush_scheduled = False
        if not self._log_buffer:
            return
        lines_to_write = "\n".join(self._log_buffer) + "\n"
        self._log_buffer.clear()

        if hasattr(self, "live_log_text"):
            w = self.live_log_text
            try:
                w.configure(state="normal")
                w.insert("end", lines_to_write)
                # Giữ tối đa 80 dòng
                line_count = int(w.index("end-1c").split(".")[0])
                if line_count > 80:
                    w.delete("1.0", f"{line_count - 80}.0")
                w.see("end")
                w.configure(state="disabled")
            except Exception:
                pass

        if hasattr(self, "comment_log_text"):
            cw = self.comment_log_text
            try:
                cw.configure(state="normal")
                cw.insert("end", lines_to_write)
                line_count = int(cw.index("end-1c").split(".")[0])
                if line_count > 200:
                    cw.delete("1.0", f"{line_count - 200}.0")
                cw.see("end")
                cw.configure(state="disabled")
            except Exception:
                pass

    def get_filtered_accounts(self):
        keyword = self.search_entry.get().lower() if hasattr(self, "search_entry") else ""
        filter_status = self.filter_var.get() if hasattr(self, "filter_var") else "all"
        filtered = []

        for i, acc in enumerate(self.accounts):
            name = acc.get("name", "").lower()
            note = acc.get("note", "").lower()
            proxy = acc.get("proxy", "").lower()
            status = acc.get("status", "active")

            if keyword and keyword not in name and keyword not in note and keyword not in proxy:
                continue

            if filter_status != "all" and status != filter_status:
                continue

            filtered.append((i, acc))

        return filtered

    def update_dashboard(self):
        live       = sum(1 for acc in self.accounts if acc.get("status", "active") == "active")
        die        = sum(1 for acc in self.accounts if acc.get("status") == "cookie_error")
        checkpoint = sum(1 for acc in self.accounts if acc.get("status") == "checkpoint")
        proxy_err  = sum(1 for acc in self.accounts if acc.get("status") == "proxy_error")

        self.live_card.configure(text=str(live))
        self.die_card.configure(text=str(die))
        self.checkpoint_card.configure(text=str(checkpoint))
        if hasattr(self, "proxy_error_card"):
            self.proxy_error_card.configure(text=str(proxy_err))
        self.selected_card.configure(text=str(len(self.selected_accounts)))
        if hasattr(self, "stat_label"):
            self.stat_label.configure(text=f"Tổng tài khoản: {len(self.accounts)}")

    def schedule_accounts_refresh(self, event=None):
        if self.account_refresh_job:
            self.after_cancel(self.account_refresh_job)
        # Tăng debounce lên 500ms: gộp nhiều lần gọi liên tiếp vào 1 lần render
        self.account_refresh_job = self.after(500, self.refresh_accounts)

    def _cancel_account_render_job(self):
        if self.account_render_job:
            self.after_cancel(self.account_render_job)
            self.account_render_job = None

    def refresh_accounts(self):
        self.account_refresh_job = None
        self._cancel_account_render_job()
        self.account_render_generation += 1
        generation = self.account_render_generation

        for widget in self.account_container.winfo_children():
            widget.destroy()

        self.account_rows = {}
        filtered = self.get_filtered_accounts()
        valid_indexes = set(range(len(self.accounts)))
        self.selected_accounts = {i for i in self.selected_accounts if i in valid_indexes}
        self.update_dashboard()

        if not filtered:
            ctk.CTkLabel(
                self.account_container, text="Chưa có tài khoản nào.", font=("Arial", 16)
            ).pack(pady=40)
            return

        total = len(filtered)
        if total > ACCOUNT_RENDER_BATCH_SIZE:
            ctk.CTkLabel(
                self.account_container,
                text=f"Đang tải {total} tài khoản theo từng lô để giao diện không bị đứng...",
                text_color="#9ca3af",
            ).pack(fill="x", padx=4, pady=(0, 8))

        def render_batch(start=0):
            if generation != self.account_render_generation:
                return
            end = min(start + ACCOUNT_RENDER_BATCH_SIZE, total)
            for row, (i, acc) in enumerate(filtered[start:end], start=start):
                self.account_row(row, i, acc)
            if end < total:
                self.account_render_job = self.after(1, lambda: render_batch(end))
            else:
                self.account_render_job = None

        render_batch()

    def account_row(self, row, index, acc):
        row_frame = ctk.CTkFrame(
            self.account_container,
            fg_color="#1f2937" if index != self.selected_index else "#263244",
            corner_radius=10
        )
        row_frame.pack(fill="x", padx=0, pady=4)
        self.account_rows[index] = row_frame
        self._render_account_row_content(row_frame, index, acc)

    def refresh_account_row(self, index):
        if not hasattr(self, "account_rows") or index not in self.account_rows:
            return
        row_frame = self.account_rows[index]
        for widget in row_frame.winfo_children():
            widget.destroy()
        row_frame.configure(fg_color="#1f2937" if index != self.selected_index else "#263244")
        if index < len(self.accounts):
            self._render_account_row_content(row_frame, index, self.accounts[index])

    def _render_account_row_content(self, row_frame, index, acc):
        self.configure_table_columns(row_frame)

        locked = self.proxy_action_locked(acc)

        is_checked = ctk.BooleanVar(value=index in self.selected_accounts)
        checkbox = ctk.CTkCheckBox(
            row_frame, text="", width=28, variable=is_checked,
            command=lambda idx=index, var=is_checked: self.toggle_account_selection(idx, var.get())
        )
        checkbox.grid(row=0, column=0, sticky="w", padx=8, pady=10)

        name_label = ctk.CTkLabel(row_frame, text=acc.get("name", "Không tên"), font=("Arial", 14, "bold"), anchor="w")
        name_label.grid(row=0, column=1, sticky="ew", padx=8, pady=10)

        proxy_text = acc.get("proxy", "") or "Không dùng proxy"
        if locked:
            proxy_text = f"🔒 {proxy_text} • khóa thao tác {proxy_lock_remaining_label(acc)}"
        ctk.CTkLabel(row_frame, text=proxy_text, text_color="#fbbf24" if locked else "#cbd5e1", anchor="w").grid(row=0, column=2, sticky="ew", padx=8, pady=10)

        status_text = self.status_text(acc.get("status", "active"))
        if locked:
            status_text = "Chỉ đăng nhập"
        ctk.CTkLabel(
            row_frame, text=status_text,
            fg_color="#92400e" if locked else self.status_color(acc.get("status", "active")),
            corner_radius=8, padx=10, pady=5
        ).grid(row=0, column=3, sticky="w", padx=8, pady=10)

        profile_var = ctk.StringVar(value=profile_label(acc.get("care_profile")))
        profile_menu = ctk.CTkOptionMenu(
            row_frame,
            values=self.get_care_profile_menu_values(),
            variable=profile_var,
            width=165,
            height=30,
            fg_color="#0f766e",
            button_color="#115e59",
            button_hover_color="#134e4a",
            text_color="#ecfeff",
            command=lambda selected, idx=index: self.update_account_care_profile(idx, selected),
        )
        profile_menu.grid(row=0, column=4, sticky="ew", padx=8, pady=10)

        last_touch = acc.get("last_care") or acc.get("last_open") or "Chưa tương tác"
        ctk.CTkLabel(row_frame, text=last_touch, text_color="#9ca3af", anchor="w").grid(row=0, column=5, sticky="ew", padx=8, pady=10)
        
        views_count = acc.get("views_count", "-")
        ctk.CTkLabel(row_frame, text=str(views_count), text_color="#38bdf8", anchor="w").grid(row=0, column=6, sticky="ew", padx=8, pady=10)
        
        ctk.CTkLabel(row_frame, text=acc.get("note", ""), text_color="#cbd5e1", anchor="w").grid(row=0, column=7, sticky="ew", padx=8, pady=10)

        action_box = ctk.CTkFrame(row_frame, fg_color="transparent")
        action_box.grid(row=0, column=8, sticky="e", padx=8, pady=8)

        care_button_state = "disabled" if locked else "normal"
        ctk.CTkButton(action_box, text="Nuôi", width=58, height=30, state=care_button_state, command=lambda idx=index: self.select_and_care(idx)).pack(side="left", padx=3)
        ctk.CTkButton(action_box, text="Chi tiết", width=70, height=30, fg_color="#374151", command=lambda idx=index: self.select_account(idx)).pack(side="left", padx=3)

        for widget in row_frame.winfo_children():
            if widget is not checkbox and widget is not action_box and widget is not profile_menu:
                widget.bind("<Button-1>", lambda e, idx=index: self.select_account(idx))

    def toggle_account_selection(self, index, checked):
        if checked:
            self.selected_accounts.add(index)
        else:
            self.selected_accounts.discard(index)
        self.update_dashboard()

    def select_all_filtered_accounts(self):
        to_refresh = []
        for index, _ in self.get_filtered_accounts():
            if index not in self.selected_accounts:
                self.selected_accounts.add(index)
                to_refresh.append(index)
        for index in to_refresh:
            self.refresh_account_row(index)
        self.update_dashboard()

    def clear_selected_accounts(self):
        old_selected = list(self.selected_accounts)
        self.selected_accounts.clear()
        for index in old_selected:
            self.refresh_account_row(index)
        self.update_dashboard()

    def get_current_care_settings(self):
        return {
            "newsfeed_minutes": int(self.newsfeed_minutes_var.get()),
            "reels_minutes": int(self.reels_minutes_var.get()),
            "pause_range": self.pause_seconds_var.get(),
            "auto_like": self.auto_like_care_var.get(),
            "read_notifications": self.read_notifications_var.get(),
            "join_groups": self.join_groups_var.get(),
            "join_group_chance": 0.35,
            "max_join_groups": 2,
            "max_parallel_care": int(self.max_parallel_care_var.get()),
        }

    def get_account_care_plan(self, account, use_smart=None):
        settings = self.get_current_care_settings()
        if use_smart is None:
            use_smart = getattr(self, "smart_care_var", None) is None or self.smart_care_var.get()
        if use_smart:
            return build_care_plan(account, settings)
        manual_account = dict(account)
        manual_account["care_profile"] = "manual"
        return build_care_plan(manual_account, settings)

    def refresh_selected_account_plan(self):
        if self.selected_index is None or self.selected_index >= len(self.accounts):
            if hasattr(self, "care_plan_preview"):
                self.care_plan_preview.configure(text="Chọn tài khoản để xem gợi ý nuôi riêng.")
            return
        acc = self.accounts[self.selected_index]
        plan = self.get_account_care_plan(acc)
        if hasattr(self, "care_plan_preview"):
            self.care_plan_preview.configure(text=f"Gợi ý: {format_care_plan(plan)}\nLý do: {plan.get('reason', '')}")

    def select_account(self, index):
        self.selected_index = index
        acc = self.accounts[index]
        plan = self.get_account_care_plan(acc)
        proxy_lock_info = ""
        if self.proxy_action_locked(acc):
            proxy_lock_info = f"\n🔒 Đổi proxy: chỉ lướt newsfeed/reels (không like/comment/nhóm), còn {proxy_lock_remaining_label(acc)} (đến {proxy_lock_until_label(acc)})\n"

        self.detail_name.configure(text=acc.get("name", "Không tên"))

        info = (
            f"Trạng thái: {self.status_text(acc.get('status', 'active'))}{proxy_lock_info}\n\n"
            f"Kiểu nuôi riêng: {profile_label(acc.get('care_profile'))}\n"
            f"Gợi ý hiện tại: {format_care_plan(plan)}\n"
            f"Lý do: {plan.get('reason', '')}\n\n"
            f"Cookie: {os.path.basename(acc.get('cookie_file', ''))}\n\n"
            f"Proxy: {acc.get('proxy', 'Không dùng proxy') or 'Không dùng proxy'}\n\n"
            f"Ghi chú: {acc.get('note', '')}\n\n"
            f"Ngày thêm: {acc.get('created_at', '')}\n"
            f"Lần mở cuối: {acc.get('last_open', 'Chưa mở')}\n"
            f"Lần nuôi cuối: {acc.get('last_care', 'Chưa nuôi')}"
        )
        self.detail_info.configure(text=info)
        self.refresh_selected_account_plan()

    # ĐÃ CHỈNH SỬA: Cho phép nhập UID|Pass|2FA vào Tên tài khoản
    def add_account_popup(self, edit_index=None):
        popup = ctk.CTkToplevel(self)
        popup.title("Thêm tài khoản" if edit_index is None else "Sửa tài khoản")
        popup.geometry("470x720")
        popup.grab_set()

        current = self.accounts[edit_index] if edit_index is not None else {}

        ctk.CTkLabel(popup, text="UID / Tên hoặc dán nhanh UID|Pass|2FA").pack(pady=(20, 5))
        name_entry = ctk.CTkEntry(popup, width=360, placeholder_text="Ví dụ: 1000... hoặc 1000...|abc123|ABCDEF...")
        name_entry.pack()
        name_entry.insert(0, current.get("uid") or current.get("name", ""))

        ctk.CTkLabel(popup, text="Mật khẩu").pack(pady=(15, 5))
        password_entry = ctk.CTkEntry(popup, width=360, show="*", placeholder_text="Có thể bỏ trống nếu dùng cookie")
        password_entry.pack()
        password_entry.insert(0, current.get("password", ""))

        ctk.CTkLabel(popup, text="2FA secret").pack(pady=(15, 5))
        two_fa_entry = ctk.CTkEntry(popup, width=360, show="*", placeholder_text="Ví dụ: ABCDEF... (không bắt buộc)")
        two_fa_entry.pack()
        two_fa_entry.insert(0, current.get("two_fa", ""))

        ctk.CTkLabel(popup, text="Trạng thái").pack(pady=(15, 5))
        status_var = ctk.StringVar(value=current.get("status", "active"))
        status_menu = ctk.CTkOptionMenu(popup, width=360, variable=status_var, values=["active", "checkpoint", "cookie_error", "proxy_error"])
        status_menu.pack()

        ctk.CTkLabel(popup, text="Kiểu nuôi riêng").pack(pady=(15, 5))
        profile_values = [f"{key} - {label}" for key, label in CARE_PROFILE_LABELS.items()]
        profile_lookup = {value: value.split(" - ", 1)[0] for value in profile_values}
        current_profile = current.get("care_profile", "auto")
        profile_var = ctk.StringVar(value=next((value for value in profile_values if value.startswith(f"{current_profile} - ")), profile_values[0]))
        ctk.CTkOptionMenu(popup, width=360, variable=profile_var, values=profile_values).pack()

        ctk.CTkLabel(popup, text="Ghi chú").pack(pady=(15, 5))
        note_entry = ctk.CTkEntry(popup, width=360)
        note_entry.pack()
        note_entry.insert(0, current.get("note", ""))

        ctk.CTkLabel(popup, text="Proxy (bỏ trống nếu không dùng)").pack(pady=(15, 5))
        proxy_entry = ctk.CTkEntry(popup, width=360, placeholder_text="host:port, host:port:user:pass hoặc socks5://host:port")
        proxy_entry.pack()
        proxy_entry.insert(0, current.get("proxy", ""))

        cookie_var = ctk.StringVar(value=current.get("cookie_file", ""))

        def choose_cookie():
            path = filedialog.askopenfilename(title="Chọn file cookie JSON", filetypes=[("JSON Files", "*.json")])
            if path: cookie_var.set(path)

        ctk.CTkButton(popup, text="Chọn file cookie (Bỏ trống sẽ tự động tạo)", width=360, command=choose_cookie).pack(pady=(20, 5))
        ctk.CTkLabel(popup, textvariable=cookie_var, wraplength=380, text_color="#9ca3af").pack(pady=5)

        def save():
            raw_data = name_entry.get().strip()
            if not raw_data:
                messagebox.showerror("Lỗi", "Vui lòng nhập Dữ liệu / Tên")
                return

            explicit_password = password_entry.get().strip()
            explicit_two_fa = two_fa_entry.get().strip()
            uid, password, two_fa, name = "", explicit_password, explicit_two_fa, raw_data
            imported_cookies = None

            # Tự động trích xuất thông tin nếu nhập định dạng UID|Pass|2FA(|cookie|proxy).
            # Ô mật khẩu/2FA riêng luôn được ưu tiên nếu người dùng có nhập/sửa tại popup.
            if "|" in raw_data:
                parsed_accounts, _ = parse_bulk_account_lines(raw_data)
                if parsed_accounts:
                    parsed_account = parsed_accounts[0]
                    uid = parsed_account.get("uid", "")
                    name = parsed_account.get("name", uid or raw_data)
                    password = explicit_password or parsed_account.get("password", "")
                    two_fa = explicit_two_fa or parsed_account.get("two_fa", "")
                    imported_cookies = parsed_account.get("_import_cookies")
                    if parsed_account.get("proxy") and not proxy_entry.get().strip():
                        proxy_entry.delete(0, "end")
                        proxy_entry.insert(0, parsed_account.get("proxy", ""))
                else:
                    parts = [part.strip() for part in raw_data.split("|")]
                    uid = parts[0] if parts else ""
                    name = uid or raw_data
                    password = explicit_password or (parts[1] if len(parts) > 1 else "")
                    two_fa = explicit_two_fa or (parts[2] if len(parts) > 2 else "")
            else:
                uid = raw_data

            # Khi sửa account cũ, không xóa nhầm mật khẩu/2FA nếu người dùng chỉ sửa tên/UID.
            if edit_index is not None:
                password = password or current.get("password", "")
                two_fa = two_fa or current.get("two_fa", "")

            # Xử lý tự động tạo đường dẫn cookie nếu người dùng không chọn file
            cookie_path = cookie_var.get().strip()
            if not cookie_path and uid:
                if not os.path.exists("cookies"):
                    os.makedirs("cookies")
                cookie_path = os.path.join("cookies", f"{uid}.json")

            new_proxy = proxy_entry.get().strip()
            account = {
                "name": name,
                "uid": uid,
                "password": password,
                "two_fa": two_fa,
                "status": status_var.get(),
                "note": note_entry.get().strip(),
                "proxy": new_proxy,
                "proxy_changed_at": current.get("proxy_changed_at", ""),
                "proxy_action_locked_until": current.get("proxy_action_locked_until", ""),
                "cookie_file": cookie_path,
                "created_at": current.get("created_at", datetime.now().strftime("%d/%m/%Y %H:%M")),
                "last_open": current.get("last_open", "Chưa mở"),
                "last_care": current.get("last_care", "Chưa nuôi"),
                "care_profile": profile_lookup.get(profile_var.get(), "auto"),
                "care_plan_note": current.get("care_plan_note", "")
            }
            old_proxy = str(current.get("proxy") or "").strip()
            proxy_changed = (edit_index is not None and old_proxy != new_proxy)

            if proxy_changed:
                mark_proxy_changed(account)

            if imported_cookies:
                account["_import_cookies"] = imported_cookies
                persist_imported_cookie_files([account])

            if edit_index is None:
                self.accounts.append(account)
            else:
                self.accounts[edit_index] = account
                if self.proxy_action_locked(account):
                    self.append_live_log(
                        f"🔒 [{account.get('name') or account.get('uid') or 'Unknown'}] Proxy đã thay đổi; "
                        "trong 24h chỉ lướt newsfeed/reels, không like/comment/tham gia nhóm."
                    )

            # ——— Đồng bộ proxy sang các tài khoản cùng proxy cũ ———
            if proxy_changed and old_proxy:
                affected = [
                    acc for i, acc in enumerate(self.accounts)
                    if i != edit_index
                    and str(acc.get("proxy") or "").strip() == old_proxy
                ]
                if affected:
                    confirm = messagebox.askyesno(
                        "Đồng bộ proxy",
                        f"Có {len(affected)} tài khoản khác đang dùng proxy cũ:\n"
                        f"  {old_proxy}\n\n"
                        f"Bạn có muốn đổi proxy của tất cả sang:\n"
                        f"  {new_proxy or '(không dùng proxy)'}\n\n"
                        f"không?",
                        parent=popup
                    )
                    if confirm:
                        for acc in affected:
                            acc["proxy"] = new_proxy
                            mark_proxy_changed(acc)
                        self.append_live_log(
                            f"🔄 Đã đồng bộ proxy mới '{new_proxy or 'trống'}' "
                            f"cho {len(affected)} tài khoản cùng proxy cũ."
                        )

            self.save_accounts()
            self.refresh_account_dependent_views()
            popup.destroy()

        ctk.CTkButton(popup, text="Lưu", width=360, height=40, command=save).pack(pady=25)

    def add_bulk_accounts_popup(self):
        popup = ctk.CTkToplevel(self)
        popup.title("Thêm tài khoản hàng loạt")
        popup.geometry("620x700")
        popup.grab_set()

        ctk.CTkLabel(
            popup,
            text="Dán danh sách tài khoản, mỗi dòng một tài khoản",
            font=("Arial", 18, "bold"),
        ).pack(pady=(20, 6))
        ctk.CTkLabel(
            popup,
            text=(
                "Định dạng: uid|pass|2fa|proxy, uid|pass|2fa|cookies|proxy, hoặc dán riêng chuỗi cookies\n"
                "Ví dụ cookies: 1000123456789|matkhau|ABC123|c_user=...;xs=...;fr=...;datr=...;|proxy.local:3128:user:pass"
            ),
            text_color="#cbd5e1",
            justify="left",
            wraplength=560,
        ).pack(pady=(0, 12))

        bulk_text = ctk.CTkTextbox(popup, width=560, height=300, wrap="none")
        bulk_text.pack(padx=20, pady=(0, 12), fill="both", expand=True)

        options = ctk.CTkFrame(popup, fg_color="transparent")
        options.pack(fill="x", padx=20)
        options.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(options, text="Trạng thái").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=6)
        status_var = ctk.StringVar(value="active")
        ctk.CTkOptionMenu(options, width=220, variable=status_var, values=["active", "checkpoint", "cookie_error", "proxy_error"]).grid(row=0, column=1, sticky="w", pady=6)

        ctk.CTkLabel(options, text="Kiểu nuôi").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=6)
        profile_values = [f"{key} - {label}" for key, label in CARE_PROFILE_LABELS.items()]
        profile_lookup = {value: value.split(" - ", 1)[0] for value in profile_values}
        profile_var = ctk.StringVar(value=profile_values[0])
        ctk.CTkOptionMenu(options, width=320, variable=profile_var, values=profile_values).grid(row=1, column=1, sticky="w", pady=6)

        ctk.CTkLabel(options, text="Ghi chú chung").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=6)
        note_entry = ctk.CTkEntry(options, width=360, placeholder_text="Ví dụ: nhập hàng loạt 02/06")
        note_entry.grid(row=2, column=1, sticky="ew", pady=6)

        overwrite_var = ctk.BooleanVar(value=bool(self.app_settings.get("import_overwrite_default", False)))
        ctk.CTkCheckBox(
            popup,
            text="Ghi đè account trùng UID (nếu tắt sẽ bỏ qua account trùng)",
            variable=overwrite_var,
        ).pack(anchor="w", padx=20, pady=(12, 4))

        preview_label = ctk.CTkLabel(popup, text="", text_color="#fbbf24", justify="left", wraplength=560)
        preview_label.pack(fill="x", padx=20, pady=(8, 0))

        def preview():
            accounts, parse_stats = parse_bulk_account_lines(
                bulk_text.get("1.0", "end-1c"),
                status=status_var.get(),
                note=note_entry.get().strip(),
                care_profile=profile_lookup.get(profile_var.get(), "auto"),
            )
            invalid_preview = ""
            if parse_stats["invalid_lines"]:
                invalid_items = ", ".join(str(item["line"]) for item in parse_stats["invalid_lines"][:8])
                invalid_preview = f" | Dòng lỗi: {invalid_items}"
            preview_label.configure(text=f"Hợp lệ: {len(accounts)} | Lỗi: {parse_stats['invalid']}{invalid_preview}")
            return accounts, parse_stats

        def save_bulk():
            accounts, parse_stats = preview()
            if not accounts:
                messagebox.showerror("Lỗi", "Không có tài khoản hợp lệ. Vui lòng nhập uid|pass|2fa|proxy, uid|pass|2fa|cookies|proxy hoặc dán chuỗi cookies có c_user.")
                return
            if parse_stats["invalid"] and not messagebox.askyesno(
                "Có dòng lỗi",
                f"Có {parse_stats['invalid']} dòng thiếu UID hoặc không hợp lệ. Bạn có muốn bỏ qua các dòng lỗi và tiếp tục thêm {len(accounts)} account hợp lệ không?",
            ):
                return

            if not os.path.exists("cookies"):
                os.makedirs("cookies")
            backup_path = backup_accounts_file(ACCOUNTS_FILE)
            self.accounts, merge_stats = merge_accounts(self.accounts, accounts, overwrite=overwrite_var.get())
            saved_cookie_count = persist_imported_cookie_files(self.accounts)
            self.save_accounts()
            self.refresh_account_dependent_views()
            popup.destroy()
            backup_note = f"\nBackup cũ: {backup_path}" if backup_path else ""
            messagebox.showinfo(
                "Thêm hàng loạt hoàn tất",
                f"Hợp lệ: {parse_stats['valid']} | Dòng lỗi: {parse_stats['invalid']}\n"
                f"Thêm: {merge_stats['added']} | Cập nhật: {merge_stats['updated']} | Bỏ qua trùng: {merge_stats['skipped']}\n"
                f"Đã lưu file cookie: {saved_cookie_count}"
                f"{backup_note}",
            )

        action_row = ctk.CTkFrame(popup, fg_color="transparent")
        action_row.pack(fill="x", padx=20, pady=20)
        ctk.CTkButton(action_row, text="Xem số lượng", width=150, fg_color="#374151", command=preview).pack(side="left", padx=(0, 10))
        ctk.CTkButton(action_row, text="Thêm vào tool", width=190, height=42, fg_color="#16a34a", command=save_bulk).pack(side="right")

    def edit_selected_account(self):
        if self.selected_index is None:
            messagebox.showwarning("Thông báo", "Hãy chọn tài khoản trước.")
            return
        self.add_account_popup(self.selected_index)

    def delete_selected_account(self):
        if self.selected_index is None:
            messagebox.showwarning("Thông báo", "Hãy chọn tài khoản trước.")
            return
        if not messagebox.askyesno("Xác nhận", "Bạn có chắc muốn xóa tài khoản này?"): return

        del self.accounts[self.selected_index]
        self.selected_accounts.discard(self.selected_index)
        self.selected_index = None
        self.save_accounts()
        self.refresh_accounts()
        self.detail_name.configure(text="Chưa chọn tài khoản")
        self.detail_info.configure(text="")

    def select_and_care(self, index):
        self.select_account(index)
        self.start_care_selected_account()

    def open_selected_account(self):
        if self.selected_index is None:
            messagebox.showwarning("Thông báo", "Hãy chọn tài khoản trước.")
            return

        account = self.accounts[self.selected_index]
        account["last_open"] = datetime.now().strftime("%d/%m/%Y %H:%M")
        self.save_accounts()
        self.select_account(self.selected_index)
        self.refresh_account_row(self.selected_index)

        threading.Thread(target=self.open_browser, args=(account,), daemon=True).start()

    def change_name_selected_account(self):
        if self.selected_index is None:
            messagebox.showwarning("Thông báo", "Hãy chọn tài khoản trước.")
            return

        account = self.accounts[self.selected_index]
        if not account.get("uid"):
            messagebox.showwarning("Thông báo", "Tài khoản này chưa có UID!")
            return
            
        account["last_open"] = datetime.now().strftime("%d/%m/%Y %H:%M")
        self.save_accounts()
        self.select_account(self.selected_index)
        self.refresh_account_row(self.selected_index)

        threading.Thread(target=self._change_name_worker, args=(account,), daemon=True).start()

    def _change_name_worker(self, account):
        uid = account.get("uid")
        try:
            cookies = self.load_cookies(account)
            with sync_playwright() as p:
                browser, context, page = self.create_browser_page(p, cookies, account)
                
                self.after(0, lambda n=account.get("name", "Unknown"): self.append_live_log(f"[{n}] 🔄 Bắt đầu đổi tên: NetfIix Tiệm Phim..."))
                
                # 1. Đảm bảo đăng nhập
                self.safe_goto(page, "https://www.facebook.com/", account=account)
                self.ensure_login(context, page, account)
                
                # 2. Vào form đổi tên
                url = f"https://accountscenter.facebook.com/profiles/{uid}/name"
                self.safe_goto(page, url, account=account)
                
                # 3. Điền Tên và Họ
                # Đợi có textbox xuất hiện trên trang
                page.get_by_role("textbox").first.wait_for(timeout=30000, state="visible")
                page.wait_for_timeout(1000) # Đợi DOM ổn định
                
                textboxes = page.get_by_role("textbox")
                count = textboxes.count()
                
                if count >= 3:
                    first_name_input = textboxes.nth(0)
                    last_name_input = textboxes.nth(2)
                elif count == 2:
                    first_name_input = textboxes.nth(0)
                    last_name_input = textboxes.nth(1)
                else:
                    first_name_input = textboxes.first
                    last_name_input = textboxes.last
                
                first_name_input.fill("NetfIix")
                last_name_input.fill("Tiệm Phim")
                
                # 4. Bấm Xem lại thay đổi
                review_btn_selectors = 'button:has-text("Xem lại thay đổi"), button:has-text("Review Change"), button:has-text("Review change")'
                page.locator(review_btn_selectors).first.click()
                
                # 5. Chờ nút Xong / Save changes hiển thị và bấm
                done_btn_selectors = 'div[role="button"]:has-text("Xong"), button:has-text("Xong"), div[role="button"]:has-text("Done"), button:has-text("Done"), div[role="button"]:has-text("Lưu thay đổi"), button:has-text("Lưu thay đổi"), div[role="button"]:has-text("Save changes"), button:has-text("Save changes")'
                page.wait_for_selector(done_btn_selectors, timeout=20000)
                done_btn = page.locator(done_btn_selectors).first
                done_btn.click()
                
                page.wait_for_timeout(4000)
                
                self.after(0, lambda n=account.get("name", "Unknown"): self.append_live_log(f"[{n}] ✅ Đổi tên thành công: NetfIix Tiệm Phim!"))
                
                account["name"] = "NetfIix Tiệm Phim"
                self.save_accounts()
                self.after(0, self.schedule_accounts_refresh)
                
                try:
                    context.close()
                except:
                    pass
        except Exception as e:
            self.after(0, lambda n=account.get("name", "Unknown"), err=e: self.append_live_log(f"[{n}] ❌ Lỗi khi đổi tên: {err}"))

    def start_care_selected_account(self):
        if self.selected_index is None:
            messagebox.showwarning("Thông báo", "Hãy chọn tài khoản trước.")
            return
        self._trigger_care([self.selected_index])

    def start_care_selected_accounts(self):
        if not self.selected_accounts:
            messagebox.showwarning("Thông báo", "Hãy tick chọn ít nhất 1 tài khoản trong bảng.")
            return
        self._trigger_care(list(self.selected_accounts))

    def _trigger_care(self, index_list):
        global_settings = self.get_current_care_settings()

        has_extra_care = global_settings.get("read_notifications") or global_settings.get("join_groups")
        if global_settings["newsfeed_minutes"] <= 0 and global_settings["reels_minutes"] <= 0 and not has_extra_care:
            messagebox.showwarning("Thông báo", "Hãy chọn thời gian Newsfeed/Reels hoặc bật đọc thông báo/tham gia group.")
            return

        self.reset_task_state()

        now = datetime.now().strftime("%d/%m/%Y %H:%M")
        queued_count = 0
        skipped_count = 0
        use_smart = self.smart_care_var.get()

        care_jobs = []
        for index in index_list:
            if index < len(self.accounts):
                account = self.accounts[index]
                if self.proxy_action_locked(account):
                    # Vẫn nuôi nhưng dùng plan thu hẹp: chỉ lướt newsfeed + reels
                    # Không được like, join group, đọc thông báo, comment
                    base_plan = self.get_account_care_plan(account, use_smart=use_smart)
                    restricted_plan = dict(base_plan)
                    restricted_plan["auto_like"] = False
                    restricted_plan["join_groups"] = False
                    restricted_plan["read_notifications"] = False
                    restricted_plan["max_join_groups"] = 0
                    restricted_plan["_proxy_restricted"] = True
                    if restricted_plan.get("newsfeed_minutes", 0) == 0 and restricted_plan.get("reels_minutes", 0) == 0:
                        restricted_plan["newsfeed_minutes"] = max(base_plan.get("newsfeed_minutes", 0), 5)
                    account["care_plan_note"] = format_care_plan(restricted_plan) + " [chỉ lướt]"
                    account["last_care"] = now
                    queued_count += 1
                    care_jobs.append((account, restricted_plan))
                    self.append_live_log(
                        f"🔒 [{account.get('name', 'Unknown')}] Đổi proxy — chỉ lướt newsfeed/reels "
                        f"(còn {proxy_lock_remaining_label(account)}), không like/comment/nhóm."
                    )
                    continue
                plan = self.get_account_care_plan(account, use_smart=use_smart)
                account["care_plan_note"] = format_care_plan(plan)
                has_plan_action = (
                    plan["newsfeed_minutes"] > 0
                    or plan["reels_minutes"] > 0
                    or plan.get("read_notifications")
                    or (plan.get("join_groups") and plan.get("max_join_groups", 0) > 0)
                )
                if not has_plan_action:
                    if len(index_list) == 1:
                        self.append_live_log(f"[{account.get('name', 'Unknown')}] Chuyển sang mở thủ công để xem trạng thái checkpoint/lỗi...")
                        threading.Thread(target=self.open_browser, args=(account,), daemon=True).start()
                    else:
                        skipped_count += 1
                        self.append_live_log(f"Bỏ qua {account.get('name', 'Unknown')}: {plan.get('reason', '')}")
                    continue
                account["last_care"] = now
                queued_count += 1
                care_jobs.append((account, plan))

        self.save_accounts()
        for index in index_list:
            if index < len(self.accounts):
                self.refresh_account_row(index)
                
        if self.selected_index is not None:
            self.select_account(self.selected_index)

        max_parallel = max(1, min(global_settings.get("max_parallel_care", 2), queued_count or 1))
        self.append_live_log(
            f"Đã đưa {queued_count} tài khoản vào hàng chờ nuôi thông minh; "
            f"chạy tối đa {max_parallel} acc cùng lúc. Bỏ qua {skipped_count} acc cần nghỉ/không chạy."
        )
        if care_jobs:
            threading.Thread(target=self.run_care_queue, args=(care_jobs, max_parallel), daemon=True).start()

    def run_care_queue(self, care_jobs, max_parallel):
        """Chạy queue nuôi acc với giới hạn song song để tránh mở quá nhiều Chrome gây lag."""
        import threading as _threading
        _lock = _threading.Lock()

        def _wrapped_care(account, plan):
            with _lock:
                self._care_running_count = getattr(self, "_care_running_count", 0) + 1
            self._update_care_run_stats()
            self._set_log_badge("running")
            success = False
            try:
                self.care_account(account, plan)
                success = True
            except Exception as exc:
                self.after(0, lambda err=exc: self.append_live_log(f"⚠️ Lỗi worker nuôi acc: {err}"))
            finally:
                with _lock:
                    self._care_running_count = max(0, getattr(self, "_care_running_count", 1) - 1)
                    if not success:
                        self._care_error_count = getattr(self, "_care_error_count", 0) + 1
                self._update_care_run_stats()
                if getattr(self, "_care_running_count", 0) == 0:
                    self._set_log_badge("done")

        futures = []
        with ThreadPoolExecutor(max_workers=max_parallel) as executor:
            for account, plan in care_jobs:
                if self.is_task_stopped():
                    break
                futures.append(executor.submit(_wrapped_care, account, plan))
                time.sleep(1.5)

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    self.after(0, lambda err=exc: self.append_live_log(f"⚠️ Lỗi worker nuôi acc: {err}"))

        self.after(0, lambda: self.append_live_log("✅ Hàng chờ nuôi tài khoản đã kết thúc."))
        self._set_log_badge("done")

    # --- BỘ LÕI BROWSER AUTOMATION (PLAYWRIGHT) ---
    def normalize_cookie(self, cookie):
        return self.automation_service.normalize_cookie(cookie)

    def load_cookies(self, account):
        return self.automation_service.load_cookies(account)

    def parse_proxy(self, proxy_text):
        return self.automation_service.parse_proxy(proxy_text)

    def create_browser_page(self, p, cookies, account=None):
        browser, context, page = self.automation_service.create_browser_page(p, cookies, account)
        self.register_playwright_session(browser=browser, context=context)
        return browser, context, page

    def save_account_cookies(self, account, cookies):
        cookie_file = self.automation_service.save_cookies(account, cookies)
        self.save_accounts()
        self.after(0, self.schedule_accounts_refresh)
        self.after(0, self.refresh_browser_accounts)
        return cookie_file

    def has_facebook_login_cookie(self, cookies):
        return self.automation_service.has_facebook_login_cookie(cookies)

    def is_real_facebook_checkpoint_url(self, url):
        return self.automation_service.is_real_facebook_checkpoint_url(url)

    def is_facebook_login_or_security_url(self, url):
        return self.automation_service.is_facebook_login_or_security_url(url)

    def is_facebook_success_url(self, url):
        return self.automation_service.is_facebook_success_url(url)

    def build_cookie_snapshot(self, cookies):
        tracked_fields = ("name", "value", "domain", "path", "expires", "expirationDate")
        return tuple(
            sorted(
                tuple(str(cookie.get(field, "")) for field in tracked_fields)
                for cookie in cookies
            )
        )

    # -----------------------------------------------------------------------
    # Hàm nhận diện trạng thái tài khoản thông minh
    # -----------------------------------------------------------------------

    # Pattern proxy lỗi từ Playwright
    _PROXY_ERROR_PATTERNS = [
        "ERR_PROXY_CONNECTION_FAILED",
        "ERR_PROXY_AUTH_UNSUPPORTED",
        "ERR_NO_SUPPORTED_PROXIES",
        "ERR_TUNNEL_CONNECTION_FAILED",
        "ERR_PROXY_CERTIFICATE_INVALID",
        "ERR_SOCKS_CONNECTION_FAILED",
        "SOCKS connection failed",
        "407 Proxy Authentication",
        "Proxy server refused",
        "net::ERR_PROXY",
        "ProxyConnectionFailed",
        "Cannot connect to proxy",
        "ERR_HTTP_RESPONSE_CODE_FAILURE",
    ]

    # Pattern checkpoint URL và nội dung trang
    _CHECKPOINT_URL_PATTERNS = [
        "/checkpoint/",           # facebook.com/checkpoint/1501092823...
        "/login/device-based/",
        "/login/checkpoint/",
        "/confirmemail/",
        "/security/emailconfirm/",
        "/recover/",
        "/login/save-device/",
        "/two_step_verification/",
        "/login/two_factor/",
        "checkpoint?next",
        "/identity/confirm",
    ]

    _CHECKPOINT_PAGE_PATTERNS = [
        "xác nhận danh tính",
        "confirm your identity",
        "we'll send you a security code",
        "enter your birthday",
        "identity confirmation",
        "xác thực danh tính",
        "kiểm tra bảo mật",
        "tài khoản bị khóa",
        "account locked",
        "account is locked",
        "suspicious activity",
        "hoạt động đáng ngờ",
    ]

    # Pattern spam/block
    _SPAM_BLOCK_PATTERNS = [
        "hành động này hiện không có sẵn",
        "this action isn't available right now",
        "bạn đang bị chặn khỏi",
        "you're blocked from",
        "your account has been disabled",
        "tài khoản bị vô hiệu hóa",
        "we restrict certain activity",
        "bạn đã bị giới hạn",
    ]

    def is_proxy_error(self, error_text: str) -> bool:
        """Kiểm tra error_text có phải lỗi proxy không."""
        low = error_text.lower()
        return any(p.lower() in low for p in self._PROXY_ERROR_PATTERNS)

    def is_checkpoint_url(self, url: str) -> bool:
        """Kiểm tra URL có phải trang checkpoint không."""
        if not url:
            return False
        low = url.lower()
        # Loại trừ "/?checkpoint_src=any" — đây là trang chủ sau login thành công
        if "/checkpoint_src=any" in low or "checkpoint_src=any" in low:
            return False
        return any(p in low for p in self._CHECKPOINT_URL_PATTERNS)

    def detect_page_problem(self, page) -> str:
        """Phân tích trang hiện tại và trả về loại vấn đề.

        Returns:
            '' = bình thường
            'checkpoint' = trang checkpoint/xác minh
            'spam_block' = bị block comment
            'logged_out' = bị đăng xuất
        """
        try:
            url = (page.url or "").lower()
            if self.is_checkpoint_url(url):
                return "checkpoint"

            # Phát hiện đăng xuất qua URL
            _LOGOUT_URL_PATTERNS = [
                "facebook.com/login",
                "facebook.com/r.php",
                "facebook.com/reg",
                "facebook.com/?refsrc=deprecated",
            ]
            if any(p in url for p in _LOGOUT_URL_PATTERNS):
                return "logged_out"

            # Phát hiện đăng xuất qua DOM (form đăng nhập hiện trên trang)
            try:
                has_login_form = page.evaluate(
                    """() => {
                        const email = document.querySelector('input[name="email"],input[id="email"],input[type="email"]');
                        const pass  = document.querySelector('input[name="pass"],input[id="pass"],input[type="password"]');
                        return !!(email && pass);
                    }"""
                )
                if has_login_form:
                    return "logged_out"
            except Exception:
                pass

            # Lấy text trang (dùng timeout ngắn để không chặn quá)
            try:
                content = page.evaluate("() => document.body?.innerText?.toLowerCase() || ''")
            except Exception:
                content = ""

            if content:
                if any(p in content for p in self._CHECKPOINT_PAGE_PATTERNS):
                    return "checkpoint"
                if any(p in content for p in self._SPAM_BLOCK_PATTERNS):
                    return "spam_block"
                # Fallback text-based: form đăng nhập xuất hiện nhưng URL chưa match
                login_signals = [
                    "email or phone", "mật khẩu", "phone number",
                    "log in to facebook", "log into facebook",
                    "đăng nhập vào facebook",
                ]
                if any(s in content for s in login_signals):
                    return "logged_out"
        except Exception:
            pass
        return ""

    def set_account_state(self, account: dict, status: str, reason: str = "", log_name: str = "") -> None:
        """Cập nhật trạng thái tài khoản và lưu + refresh UI.

        Chỉ cập nhật nếu khác status hiện tại để tránh spam save.
        """
        old_status = account.get("status", "active")
        if old_status == status and not reason:
            return

        account["status"] = status
        if reason:
            account["last_error_reason"] = reason

        name = log_name or account.get("name") or account.get("uid") or "?"
        emoji_map = {
            "active":       "✅",
            "checkpoint":   "🟡",
            "cookie_error": "🔴",
            "proxy_error":  "🔌",
        }
        emoji = emoji_map.get(status, "⚠️")
        label = self.status_text(status)
        msg = f"[{name}] {emoji} Trạng thái → {label}"
        if reason:
            msg += f": {reason[:120]}"

        self.after(0, lambda m=msg: self.append_live_log(m))
        self.save_accounts()
        
        try:
            idx = self.accounts.index(account)
            self.after(0, lambda: self._update_account_state_ui(idx))
        except ValueError:
            self.after(0, self.schedule_accounts_refresh)

    def _update_account_state_ui(self, index):
        self.update_dashboard()
        self.refresh_account_row(index)

    def safe_goto(self, page, url, account=None, wait_until="domcontentloaded", timeout=60000, retries=2, fallback_urls=None):
        """
        Điều hướng ổn định hơn khi Facebook/proxy trả về ERR_EMPTY_RESPONSE.
        Thử lại URL hiện tại, sau đó thử domain dự phòng (m/www) trước khi báo lỗi rõ ràng.
        """
        urls_to_try = [url]
        for fallback_url in (fallback_urls or []):
            if fallback_url and fallback_url not in urls_to_try:
                urls_to_try.append(fallback_url)

        account_name = (account or {}).get("name") or (account or {}).get("uid") or "Unknown"
        last_error = None

        for current_url in urls_to_try:
            for attempt in range(1, retries + 2):
                try:
                    result = page.goto(current_url, wait_until=wait_until, timeout=timeout)
                    # Kết nối thành công → tự động xóa proxy_error nếu có
                    if account is not None and account.get("status") == "proxy_error":
                        self.set_account_state(
                            account, "active",
                            log_name=(account.get("name") or account.get("uid") or "Unknown")
                        )
                        self.after(0, lambda n=account_name: self.append_live_log(
                            f"[{n}] ✅ Proxy kết nối lại được, đã xóa trạng thái lỗi."
                        ))
                    return result
                except Exception as e:
                    last_error = e
                    error_text = str(e)
                    is_retryable = any(err in error_text for err in [
                        "ERR_EMPTY_RESPONSE",
                        "ERR_CONNECTION_RESET",
                        "ERR_CONNECTION_CLOSED",
                        "ERR_TIMED_OUT",
                        "Timeout"
                    ])

                    if not is_retryable or attempt > retries:
                        break

                    wait_seconds = 2 + attempt * 2
                    self.after(0, lambda n=account_name, u=current_url, a=attempt, w=wait_seconds: self.append_live_log(
                        f"[{n}] Mạng/proxy chưa trả dữ liệu khi mở {u}. Thử lại lần {a} sau {w}s..."
                    ))
                    time.sleep(wait_seconds)

        proxy_text = (account or {}).get("proxy", "") or "không dùng proxy"
        has_proxy = bool((account or {}).get("proxy", "").strip())

        # Nếu lỗi cuối cùng là do proxy → cập nhật trạng thái tài khoản ngay
        if last_error is not None and account is not None:
            err_str = str(last_error)
            if self.is_proxy_error(err_str):
                # Lỗi proxy rõ ràng (ERR_PROXY_*, SOCKS failed, 407...)
                reason = f"Proxy [{proxy_text}] không kết nối được: {err_str[:120]}"
                self.set_account_state(account, "proxy_error", reason, log_name=account_name)
            elif has_proxy and any(x in err_str for x in [
                "ERR_EMPTY_RESPONSE",
                "ERR_CONNECTION_RESET",
                "ERR_CONNECTION_CLOSED",
                "ERR_CONNECTION_REFUSED",
                "ERR_ADDRESS_UNREACHABLE",
                "ERR_NETWORK_CHANGED",
                "ERR_TIMED_OUT",
                "Timeout",
            ]):
                # Proxy sống nhưng không đi được → cũng là lỗi proxy sau khi đã thử lại hết
                reason = f"Proxy [{proxy_text}] không phản hồi sau nhiều lần thử: {err_str[:120]}"
                self.set_account_state(account, "proxy_error", reason, log_name=account_name)
            elif any(x in err_str for x in ["ERR_EMPTY_RESPONSE", "ERR_CONNECTION", "ERR_TIMED_OUT", "Timeout"]):
                # Không dùng proxy — có thể mạng tạm thời, chỉ log cảnh báo
                self.after(0, lambda n=account_name, p=proxy_text: self.append_live_log(
                    f"[{n}] ⚠️ Mạng [{p}] chậm hoặc không phản hồi, đã thử lại nhưng thất bại."
                ))

        raise Exception(
            f"Không mở được Facebook ({last_error}). "
            f"Nếu đang dùng proxy [{proxy_text}], hãy đổi/tắt proxy hoặc kiểm tra mạng."
        )

    def goto_facebook_home(self, page, account=None, mobile=False):
        if mobile:
            return self.safe_goto(
                page,
                "https://m.facebook.com/",
                account=account,
                fallback_urls=["https://www.facebook.com/", "https://facebook.com/"]
            )

        return self.safe_goto(
            page,
            "https://www.facebook.com/",
            account=account,
            fallback_urls=["https://facebook.com/", "https://m.facebook.com/"]
        )

    def resolve_account_profile_url(self, account):
        raw_uid = str((account or {}).get("uid") or "").strip()
        if not raw_uid:
            return "https://www.facebook.com/me/"
        if raw_uid.startswith("http://") or raw_uid.startswith("https://"):
            return raw_uid
        if raw_uid.isdigit():
            return f"https://www.facebook.com/profile.php?id={raw_uid}"
        return f"https://www.facebook.com/{raw_uid}"


    def is_captcha_visible(self, page):
        verification_selectors = (
            'iframe[src*="captcha" i], iframe[title*="captcha" i], iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i]',
            '[id*="captcha" i], [class*="captcha" i], [data-testid*="captcha" i]',
            'text=/captcha|recaptcha|hcaptcha|security check|kiểm tra bảo mật|xác minh bảo mật/i',
            'text=/complete a challenge|verify you[’\']?re a human|solve a puzzle|try audio challenge/i',
            'text=/hoàn thành thử thách|xác minh bạn là người thật|giải câu đố|thử thách âm thanh/i',
        )
        for selector in verification_selectors:
            try:
                if page.locator(selector).first.is_visible(timeout=1000):
                    return True
            except Exception:
                continue
        return False

    def wait_for_captcha_resolution(self, page, uid, timeout_seconds=60):
        if not self.is_captcha_visible(page):
            return True

        self.after(0, lambda: self.append_live_log(
            f"[{uid}] ⚠️ Phát hiện CAPTCHA/thử thách xác minh người thật. "
            f"Vui lòng xác minh thủ công trong {timeout_seconds} giây..."
        ))
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self.is_task_stopped():
                return False
            if not self.is_captcha_visible(page):
                self.after(0, lambda: self.append_live_log(
                    f"[{uid}] ✅ Thử thách xác minh đã được xử lý, tiếp tục xử lý 2FA..."
                ))
                return True
            try:
                if self.has_facebook_login_cookie(page.context.cookies()):
                    self.after(0, lambda: self.append_live_log(
                        f"[{uid}] ✅ Đã có cookie đăng nhập sau khi xác minh người thật."
                    ))
                    return True
            except Exception:
                pass
            time.sleep(2)

        self.after(0, lambda: self.append_live_log(
            f"[{uid}] ❌ Chưa xác minh người thật sau {timeout_seconds} giây. Đóng phiên đăng nhập này."
        ))
        try:
            page.context.close()
        except Exception:
            pass
        return False

    def get_two_fa_box(self, page):
        return page.locator(
            'input[aria-label="Mã"], '
            'input[aria-label="Login code"], '
            'input[aria-label="Code"], '
            'input[autocomplete="one-time-code"], '
            'input[id="approvals_code"], '
            'input[type="text"]'
        ).locator("visible=true").first

    def wait_for_two_fa_box_or_login(self, page, context, uid, timeout_seconds=60):
        self.after(0, lambda: self.append_live_log(
            f"[{uid}] Đang chờ form nhập mã 2FA hiển thị tối đa {timeout_seconds} giây..."
        ))
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self.is_task_stopped():
                return None
            try:
                if self.is_facebook_success_url(page.url) and self.has_facebook_login_cookie(context.cookies()):
                    self.after(0, lambda: self.append_live_log(
                        f"[{uid}] ✅ Đã đăng nhập thành công trong lúc chờ 2FA."
                    ))
                    return "logged_in"
            except Exception:
                pass

            two_fa_box = self.get_two_fa_box(page)
            try:
                if two_fa_box.is_visible(timeout=1000):
                    return two_fa_box
            except Exception:
                pass
            time.sleep(2)

        self.after(0, lambda: self.append_live_log(
            f"[{uid}] ❌ Không thấy form 2FA sau {timeout_seconds} giây. Đóng phiên đăng nhập này."
        ))
        try:
            page.context.close()
        except Exception:
            pass
        return None

    # --- HÀM MỚI: KIỂM TRA VÀ TỰ ĐỘNG ĐĂNG NHẬP NẾU CHƯA CÓ COOKIE ---

    def click_post_2fa_white_choice(self, page, uid, timeout_seconds=15):
        """Click the white/secondary choice on Facebook's post-2FA trust-device prompt.

        Facebook may show a screen like "Trust this device?" after a valid 2FA
        code.  The requested action is to choose the white/secondary option
        (keep confirming this login instead of trusting/saving the device).
        """
        secondary_choice_selectors = (
            'div[role="button"]:has-text("Toujours confirmer")',
            'span:has-text("Toujours confirmer")',
            'button:has-text("Toujours confirmer")',
            'div[role="button"]:has-text("Always confirm")',
            'span:has-text("Always confirm")',
            'button:has-text("Always confirm")',
            'div[role="button"]:has-text("Luôn xác nhận")',
            'span:has-text("Luôn xác nhận")',
            'button:has-text("Luôn xác nhận")',
            'div[role="button"]:has-text("Tiếp tục xác nhận")',
            'span:has-text("Tiếp tục xác nhận")',
            'button:has-text("Tiếp tục xác nhận")',
        )
        checkbox_selectors = (
            'input[type="checkbox"]:visible',
            'div[role="checkbox"]:visible',
            '[aria-checked]:visible',
        )
        continue_selectors = (
            'div[role="button"]:has-text("Tiếp tục")',
            'button:has-text("Tiếp tục")',
            'div[aria-label="Tiếp tục"]',
            'div[role="button"]:has-text("Continue")',
            'button:has-text("Continue")',
            'div[aria-label="Continue"]',
            'div[role="button"]:has-text("Continuer")',
            'button:has-text("Continuer")',
            'div[aria-label="Continuer"]',
        )

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self.is_task_stopped():
                return False

            for selector in secondary_choice_selectors:
                try:
                    choice = page.locator(selector).locator("visible=true").first
                    if choice.is_visible(timeout=700):
                        self.after(0, lambda: self.append_live_log(
                            f"[{uid}] Đã thấy màn hình tin cậy thiết bị, đang tick ô trắng/luôn xác nhận..."
                        ))
                        choice.click(no_wait_after=True)
                        time.sleep(3)
                        return True
                except Exception:
                    continue

            for selector in checkbox_selectors:
                try:
                    checkbox = page.locator(selector).first
                    if checkbox.is_visible(timeout=500):
                        self.after(0, lambda: self.append_live_log(
                            f"[{uid}] Đã thấy ô chọn sau 2FA, đang tick ô màu trắng..."
                        ))
                        checkbox.click(no_wait_after=True)
                        time.sleep(1)
                        for continue_selector in continue_selectors:
                            try:
                                continue_button = page.locator(continue_selector).locator("visible=true").first
                                if continue_button.is_visible(timeout=500):
                                    continue_button.click(no_wait_after=True)
                                    time.sleep(3)
                                    break
                            except Exception:
                                continue
                        return True
                except Exception:
                    continue

            try:
                if self.is_facebook_success_url(page.url) and self.has_facebook_login_cookie(page.context.cookies()):
                    return False
            except Exception:
                pass
            time.sleep(1)

        return False


    def is_logged_out_landing_page(self, page):
        """Nhận diện màn hình Facebook đã đăng xuất nhưng vẫn ở facebook.com.

        Ví dụ màn hình có nút Continue/Tiếp tục theo profile đã lưu và nút
        Use another profile. Màn này không có ô email nên check URL/ô email cũ
        dễ nhầm là đã đăng nhập.
        """
        try:
            body_text = page.locator("body").inner_text(timeout=2000)
            if self.automation_service.looks_like_logged_out_landing_text(body_text):
                return True
        except Exception:
            pass

        logged_out_selectors = (
            'text=/Use another profile|Dùng trang cá nhân khác|Sử dụng tài khoản khác|Log into another account/i',
            'text=/Create new account|Tạo tài khoản mới/i',
        )
        for selector in logged_out_selectors:
            try:
                if page.locator(selector).first.is_visible(timeout=700):
                    return True
            except Exception:
                continue
        return False

    def open_normal_login_form(self, page, account):
        """Đưa Facebook về form email/pass nếu đang ở màn Continue profile."""
        switch_selectors = (
            'div[role="button"]:has-text("Use another profile")',
            'button:has-text("Use another profile")',
            'text=/Use another profile/i',
            'div[role="button"]:has-text("Dùng trang cá nhân khác")',
            'button:has-text("Dùng trang cá nhân khác")',
            'text=/Dùng trang cá nhân khác|Sử dụng tài khoản khác|Đăng nhập bằng tài khoản khác/i',
            'text=/Log into another account/i',
        )
        email_selector = 'input[name="email"], input[id="email"]'
        pass_selector = 'input[name="pass"], input[id="pass"]'

        for selector in switch_selectors:
            try:
                button = page.locator(selector).locator("visible=true").first
                if button.is_visible(timeout=700):
                    button.click(no_wait_after=True)
                    time.sleep(1)  # 1s đủ để trang chuyển
                    break
            except Exception:
                continue

        try:
            if page.locator(email_selector).first.is_visible(timeout=2000) and page.locator(pass_selector).first.is_visible(timeout=2000):
                return True
        except Exception:
            pass

        self.safe_goto(page, "https://facebook.com/login/", account=account, fallback_urls=["https://www.facebook.com/login/", "https://m.facebook.com/login/"])
        time.sleep(1)
        return True

    def ensure_login(self, context, page, account):
        uid = account.get("uid", "")
        password = account.get("password", "")
        two_fa = account.get("two_fa", "")

        # Vào thử trang chủ FB để check xem cookie có hoạt động không
        self.goto_facebook_home(page, account=account, mobile=True)
        # Chờ trang load xong thay vì sleep cứng — thoát sớm nếu proxy nhanh
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

        # -------------------------------------------------------------
        # Kiểm tra Checkpoint ngay từ bước này trước khi cố gắng điền login
        # -------------------------------------------------------------
        prob = self.detect_page_problem(page)
        if prob == "checkpoint":
            self.set_account_state(account, "checkpoint", "Phát hiện Checkpoint ngay khi mở Facebook.")
            raise Exception("Tài khoản đang bị Checkpoint (xác minh bảo mật).")

        # Nếu không bị đá ra trang đăng nhập/checkpoint/two_step -> Cookie vẫn sống.
        # Lưu ý: Facebook có thể trả về /?checkpoint_src=any sau khi login thành công;
        # query này không được tính là checkpoint thật.
        if self.is_facebook_success_url(page.url):
            if page.locator("input[name='email'], input[id='email']").is_hidden() and not self.is_logged_out_landing_page(page):
                return True
            if self.is_logged_out_landing_page(page):
                self.after(0, lambda: self.append_live_log(f"[{uid}] Phát hiện Facebook đã đăng xuất (màn Continue profile), đang đăng nhập lại..."))

        # Nếu rơi xuống đây tức là Cookie đã chết HOẶC chưa từng có Cookie
        if not uid or not password:
            account["status"] = "cookie_error"
            self.save_accounts()
            self.after(0, self.schedule_accounts_refresh)
            raise Exception("Không có Cookie và không có UID/Pass để tự động đăng nhập!")

        self.after(0, lambda: self.append_live_log(f"[{uid}] Cookie trống/die, đang tự động đăng nhập..."))

        self.open_normal_login_form(page, account)

        # 1. Điền tài khoản, mật khẩu
        page.locator('input[name="email"], input[id="email"]').first.fill(uid)
        time.sleep(0.5) # Nghỉ 0.5s cho giống người thật

        pass_input = page.locator('input[name="pass"], input[id="pass"]').first
        pass_input.fill(password)
        time.sleep(1) # Nghỉ 1s trước khi bấm

        # 2. Bấm phím Enter ngay tại ô mật khẩu để Submit Form
        pass_input.press("Enter")

        self.after(0, lambda: self.append_live_log(f"[{uid}] Đã ấn Enter, chờ FB phản hồi..."))
        # Chờ FB navigate (thoát sớm khi URL thay đổi), tối đa 8s
        try:
            page.wait_for_url("**/facebook.com/**", timeout=8000)
        except Exception:
            pass

        if not self.wait_for_captcha_resolution(page, uid, timeout_seconds=60):
            account["status"] = "checkpoint"
            self.save_accounts()
            self.after(0, self.schedule_accounts_refresh)
            raise Exception("Captcha chưa được xác minh sau 60 giây, đã đóng phiên đăng nhập.")

        # Nếu CAPTCHA được xác minh xong và đã đăng nhập luôn, lưu cookie ngay.
        if self.is_facebook_success_url(page.url) and self.has_facebook_login_cookie(context.cookies()):
            self.after(0, lambda: self.append_live_log(f"[{uid}] Đăng nhập thành công sau CAPTCHA! Đang lưu cookie mới..."))
            account["status"] = "active"
            self.save_account_cookies(account, context.cookies())
            self.save_accounts()
            self.after(0, self.schedule_accounts_refresh)
            return True

        # 3. Quét form 2FA. Nếu không thấy 2FA thì chờ thêm 60 giây rồi đóng phiên.
        two_fa_box = self.wait_for_two_fa_box_or_login(page, context, uid, timeout_seconds=60)
        if two_fa_box == "logged_in":
            account["status"] = "active"
            self.save_account_cookies(account, context.cookies())
            self.save_accounts()
            self.after(0, self.schedule_accounts_refresh)
            return True

        if two_fa_box is None:
            account["status"] = "checkpoint"
            self.save_accounts()
            self.after(0, self.schedule_accounts_refresh)
            raise Exception("Không thấy form 2FA sau 60 giây, đã đóng phiên đăng nhập.")

        try:
            # Nếu code tới đây, nghĩa là ô 2FA chắc chắn đã hiển thị
            code = generate_totp_code(two_fa)
            if code:
                self.after(0, lambda: self.append_live_log(f"[{uid}] Đã sinh mã: {code}. Đang nhập..."))

                # Click vào ô trước khi nhập để kích hoạt con trỏ chuột (giống người thật)
                two_fa_box.click()
                time.sleep(0.5)

                # Điền mã
                two_fa_box.fill(code)
                time.sleep(1)

                # Ấn Enter để Submit
                two_fa_box.press("Enter")
                # Chờ FB navigate sau 2FA — thoát sớm khi URL thay đổi, tối đa 8s
                try:
                    page.wait_for_url("**/facebook.com/**", timeout=8000)
                except Exception:
                    pass

                # Nếu Facebook hỏi "Tin cậy thiết bị này?", chọn ô/nút trắng
                # (luôn xác nhận đây là tôi) thay vì nút xanh lưu/tin cậy thiết bị.
                self.click_post_2fa_white_choice(page, uid, timeout_seconds=15)
            else:
                self.after(0, lambda: self.append_live_log(f"[{uid}] ❌ Lỗi: Mã Secret 2FA không tạo được 6 số. Kiểm tra lại chuỗi 2FA!"))

        except Exception:
            self.after(0, lambda: self.append_live_log(f"[{uid}] Không nhập được mã 2FA. Đóng phiên đăng nhập này..."))
            try:
                page.context.close()
            except Exception:
                pass
            account["status"] = "checkpoint"
            self.save_accounts()
            self.after(0, self.schedule_accounts_refresh)
            raise Exception("Không nhập được mã 2FA, đã đóng phiên đăng nhập.")

        try:
            page.wait_for_url("**/facebook.com/**", timeout=15000)
        except:
            pass

        # Kiểm tra lại URL xem đã vào được bên trong chưa.
        # /?checkpoint_src=any là trang chủ sau login thành công, không phải checkpoint thật.
        if self.is_facebook_login_or_security_url(page.url):
            account["status"] = "checkpoint"
            self.save_accounts()
            self.after(0, self.schedule_accounts_refresh)
            raise Exception("Đăng nhập thất bại (Sai pass hoặc dính Checkpoint/2FA).")

        self.after(0, lambda: self.append_live_log(f"[{uid}] Đăng nhập thành công! Đang lưu cookie mới..."))

        # Lưu Cookie Mới
        new_cookies = context.cookies()
        account["status"] = "active"
        self.save_account_cookies(account, new_cookies)
        self.save_accounts()
        self.after(0, self.schedule_accounts_refresh)
        return True

    def wait_for_manual_browser_close(self, browser, context, page, account):
        account_name = account.get("name") or account.get("uid") or "Unknown"
        saved_cookie_path = None
        saved_cookie_snapshot = None
        self.after(0, lambda n=account_name: self.append_live_log(
            f"[{n}] 🌐 Đã mở trình duyệt thủ công. Tool sẽ tự lưu cookie sau khi bạn đăng nhập; hãy tự đóng Chrome khi xong."
        ))

        while browser.is_connected():
            open_pages = [ctx_page for ctx_page in context.pages if not ctx_page.is_closed()]
            if not open_pages:
                break

            try:
                # Cập nhật trạng thái
                current_status = account.get("status")
                new_status = current_status
                
                if open_pages:
                    prob = self.detect_page_problem(open_pages[-1])
                    if prob == "checkpoint" or prob == "spam_block":
                        new_status = "checkpoint"
                    elif prob == "logged_out":
                        new_status = "cookie_error"
                    else:
                        current_cookies = context.cookies()
                        if self.has_facebook_login_cookie(current_cookies):
                            new_status = "active"

                if new_status != current_status:
                    account["status"] = new_status
                    self.save_accounts()
                    
                    try:
                        idx = self.accounts.index(account)
                        self.after(0, lambda i=idx: self._update_account_state_ui(i))
                    except ValueError:
                        self.after(0, self.schedule_accounts_refresh)

                # Lưu cookie nếu có
                current_cookies = context.cookies()
                if self.has_facebook_login_cookie(current_cookies):
                    cookie_snapshot = self.build_cookie_snapshot(current_cookies)
                    if cookie_snapshot != saved_cookie_snapshot:
                        cookie_path = self.save_account_cookies(account, current_cookies)
                        if cookie_path != saved_cookie_path:
                            self.after(0, lambda n=account_name, path=cookie_path: self.append_live_log(
                                f"[{n}] ✅ Đã phát hiện thay đổi cookie và lưu: {path}"
                            ))
                        saved_cookie_path = cookie_path
                        saved_cookie_snapshot = cookie_snapshot
            except Exception as e:
                pass

            time.sleep(3)

        if saved_cookie_path:
            self.after(0, lambda n=account_name, path=saved_cookie_path: self.append_live_log(
                f"[{n}] Đã đóng phiên trình duyệt thủ công. Cookie mới nhất đã lưu tại: {path}"
            ))
        else:
            self.after(0, lambda n=account_name: self.append_live_log(
                f"[{n}] Đã đóng phiên trình duyệt thủ công. Chưa phát hiện cookie đăng nhập Facebook để lưu."
            ))

    def open_browser(self, account, start_url=None):
        """Mở browser thủ công, không kiểm tra cookie/UID/pass hay ép auto-login."""
        browser = None
        context = None
        page = None

        try:
            cookies = self.load_cookies(account)
            with sync_playwright() as p:
                browser, context, page = self.create_browser_page(p, cookies, account)
                target_url = start_url or self.app_settings.get("default_home_url", "https://www.facebook.com/")

                try:
                    self.safe_goto(page, target_url, account=account)
                    
                    uid = account.get("uid")
                    pwd = account.get("password")
                    if uid and pwd:
                        # Kiểm tra xem có đang ở form đăng nhập không
                        if page.locator("input[name='email'], input[id='email']").is_visible(timeout=2000):
                            account_name = account.get("name") or uid or "Unknown"
                            self.after(0, lambda n=account_name: self.append_live_log(f"[{n}] Đang tự động điền tài khoản/mật khẩu trong phiên thủ công..."))
                            self.open_normal_login_form(page, account)
                            page.locator('input[name="email"], input[id="email"]').first.fill(uid)
                            page.locator('input[name="pass"], input[id="pass"]').first.fill(pwd)
                            page.locator('input[name="pass"], input[id="pass"]').first.press("Enter")
                            
                            # Chờ 2FA và tự động điền nếu có
                            two_fa = account.get("two_fa")
                            if two_fa:
                                two_fa_box = self.get_two_fa_box(page)
                                if two_fa_box.is_visible(timeout=5000):
                                    from utils.totp_utils import generate_totp_code
                                    code = generate_totp_code(two_fa)
                                    if code:
                                        self.after(0, lambda n=account_name: self.append_live_log(f"[{n}] Đã tự động điền mã 2FA trong phiên thủ công..."))
                                        two_fa_box.click()
                                        page.wait_for_timeout(500)
                                        two_fa_box.fill(code)
                                        two_fa_box.press("Enter")
                except Exception as navigation_error:
                    account_name = account.get("name") or account.get("uid") or "Unknown"
                    self.after(0, lambda n=account_name, err=navigation_error: self.append_live_log(
                        f"[{n}] ⚠️ Lỗi trong quá trình thao tác ban đầu (nhưng Chrome vẫn mở để bạn xử lý): {err}"
                    ))

                self.wait_for_manual_browser_close(browser, context, page, account)
        except Exception as e:
            missing_login_error = "Không có Cookie và không có UID/Pass"
            if missing_login_error in str(e) and browser and context and page:
                self.wait_for_manual_browser_close(browser, context, page, account)
                return
            self.after(0, lambda err=e: messagebox.showerror("Lỗi mở Facebook", str(err)))

    def get_pause_seconds(self, pause_range):
        return random_delay(pause_range)

    def scroll_page_for_minutes(self, page, minutes, pause_range, mode="newsfeed", account_name="", auto_like=False):
        end_time = time.time() + minutes * 60
        mode_text = "Reels" if mode == "reels" else "Newsfeed"
        self.after(0, lambda: self.append_live_log(f"Đang lướt {mode_text} cho {account_name}..."))

        # Khởi tạo bộ đếm và random số lần cuộn cần đạt để Like (từ 10 đến 20)
        scroll_count = 0
        next_like_threshold = random.randint(10, 20)

        while time.time() < end_time and not self.is_task_stopped():
            if not self.wait_if_paused():
                break
            if mode == "reels":
                page.keyboard.press("ArrowDown")
            else:
                page.mouse.wheel(0, random.randint(350, 900))

            scroll_count += 1

            # --- LOGIC THẢ LIKE AUTO BẢN NÂNG CẤP ---
            if auto_like and scroll_count >= next_like_threshold:
                try:
                    # Dừng 1 giây để giao diện FB kịp render nút Like sau khi cuộn
                    if not self.interruptible_sleep(1):
                        break

                    # Bộ chọn bao quát hơn cho cả Newsfeed, Reels và Group (Tiếng Việt + Tiếng Anh)
                    like_btn = page.locator(
                        "div[aria-label='Thích'], div[aria-label='Like'], "
                        "span[aria-label='Thích'], span[aria-label='Like']"
                    ).locator("visible=true").first

                    if like_btn.is_visible():
                        # force=True: Bỏ qua kiểm tra che khuất của Playwright, ép click thẳng vào toạ độ đó
                        like_btn.click(timeout=3000, force=True)
                        self.after(0, lambda n=account_name: self.append_live_log(f"[{n}] ❤️ Đã thả Like ngẫu nhiên!"))
                        if not self.interruptible_sleep(random.uniform(1.5, 3)):
                            break
                    else:
                        # Log ra nếu tới lúc cần Like nhưng không tìm thấy nút trên màn hình
                        self.after(0, lambda n=account_name: self.append_live_log(f"[{n}] ⚠️ Muốn Like nhưng không thấy nút trên màn hình."))
                except Exception as e:
                    # Bắt và in lỗi ra log để biết chính xác nó kẹt ở đâu (nếu có)
                    error_msg = str(e).split('\n')[0] # Lấy dòng lỗi ngắn gọn
                    self.after(0, lambda n=account_name, err=error_msg: self.append_live_log(f"[{n}] ❌ Thả Like thất bại: {err[:50]}..."))

                # Reset lại bộ đếm và bốc ngẫu nhiên số lần cuộn mới cho lần Like tiếp theo
                scroll_count = 0
                next_like_threshold = random.randint(10, 20)
            # ---------------------------

            if not self.interruptible_sleep(self.get_pause_seconds(pause_range)):
                break

    def read_notifications_for_account(self, page, account, pause_range):
        account_name = account.get("name", "")
        self.after(0, lambda n=account_name: self.append_live_log(f"[{n}] 🔔 Đang mở thông báo để đọc tự nhiên..."))
        self.safe_goto(
            page,
            "https://www.facebook.com/notifications",
            account=account,
            fallback_urls=["https://facebook.com/notifications", "https://m.facebook.com/notifications"],
        )
        if not self.interruptible_sleep(random.uniform(4, 7)):
            return

        read_rounds = random.randint(2, 4)
        for _ in range(read_rounds):
            if self.is_task_stopped() or not self.wait_if_paused():
                break
            page.mouse.wheel(0, random.randint(250, 650))
            if not self.interruptible_sleep(self.get_pause_seconds(pause_range)):
                break

        if self.is_task_stopped():
            return

        try:
            notification_items = page.locator(
                "a[href*='notif'], a[href*='notifications'], div[role='article'] a"
            ).locator("visible=true")
            visible_count = min(notification_items.count(), 5)
            if visible_count > 0 and random.random() < 0.45:
                item_index = random.randint(0, visible_count - 1)
                notification_items.nth(item_index).click(timeout=3000)
                self.after(0, lambda n=account_name: self.append_live_log(f"[{n}] 🔔 Đã mở thử một thông báo ngẫu nhiên."))
                self.interruptible_sleep(random.uniform(5, 9))
        except Exception as exc:
            error_msg = str(exc).split("\n")[0]
            self.after(0, lambda n=account_name, err=error_msg: self.append_live_log(f"[{n}] ⚠️ Không mở được thông báo cụ thể: {err[:60]}..."))

    def maybe_join_groups_for_account(self, page, account, settings):
        account_name = account.get("name", "")
        if not settings.get("join_groups") or settings.get("max_join_groups", 0) <= 0:
            return
        if random.random() > settings.get("join_group_chance", 0.35):
            self.after(0, lambda n=account_name: self.append_live_log(f"[{n}] Bỏ qua tham gia group ở lượt này để hành vi tự nhiên hơn."))
            return

        target_count = random.randint(1, int(settings.get("max_join_groups", 2)))
        joined_count = 0
        self.after(0, lambda n=account_name, c=target_count: self.append_live_log(f"[{n}] 👥 Tìm group gợi ý để tham gia tối đa {c} group..."))
        self.safe_goto(
            page,
            "https://www.facebook.com/groups/discover/",
            account=account,
            fallback_urls=["https://facebook.com/groups/discover/", "https://m.facebook.com/groups/"],
        )
        if not self.interruptible_sleep(random.uniform(5, 8)):
            return

        for _ in range(8):
            if joined_count >= target_count or self.is_task_stopped() or not self.wait_if_paused():
                break
            try:
                join_button = page.locator(
                    "div[aria-label='Tham gia nhóm'], div[aria-label='Join group'], "
                    "span:has-text('Tham gia nhóm'), span:has-text('Join group'), "
                    "div[role='button']:has-text('Tham gia nhóm'), div[role='button']:has-text('Join group')"
                ).locator("visible=true").first

                if join_button.is_visible(timeout=2500):
                    join_button.click(timeout=4000, force=True)
                    joined_count += 1
                    self.after(0, lambda n=account_name, c=joined_count: self.append_live_log(f"[{n}] 👥 Đã gửi yêu cầu/tham gia group #{c}."))
                    if not self.interruptible_sleep(random.uniform(6, 12)):
                        break
                    continue
            except Exception:
                pass

            page.mouse.wheel(0, random.randint(500, 950))
            if not self.interruptible_sleep(random.uniform(3, 6)):
                break

        if joined_count == 0:
            self.after(0, lambda n=account_name: self.append_live_log(f"[{n}] Không tìm thấy nút tham gia group phù hợp trên màn hình."))

    def care_account(self, account, settings):
        if self.is_task_stopped():
            return
        try:
            cookies = self.load_cookies(account)
            start_time = datetime.now().strftime("%d/%m/%Y %H:%M")
            log_item = {
                "account": account.get("name", ""),
                "action": "care_smart_profile",
                "status": "running",
                "start_time": start_time,
                "profile": settings.get("profile_label", "Theo cấu hình"),
                "plan": format_care_plan(settings),
            }
            with self.log_lock:
                self.logs.append(log_item)
                self.save_logs()
            self.after(0, lambda name=account.get("name", ""), plan=format_care_plan(settings): self.append_live_log(f"Bắt đầu nuôi {name} theo kế hoạch: {plan}"))

            with sync_playwright() as p:
                browser, context, page = self.create_browser_page(p, cookies, account)

                # GỌI HÀM KIỂM TRA ĐĂNG NHẬP Ở ĐÂY
                self.ensure_login(context, page, account)
                # Nếu plan bị thu hẹp do đổi proxy → log rõ nhưng vẫn tiếp tục
                if settings.get("_proxy_restricted"):
                    self.after(0, lambda name=account.get("name", ""), rem=proxy_lock_remaining_label(account): self.append_live_log(
                        f"🔒 [{name}] Chế độ hạn chế (đổi proxy): chỉ lướt newsfeed/reels, "
                        f"không like/comment/nhóm. Còn {rem}."
                    ))

                if settings.get("read_notifications") and not self.is_task_stopped():
                    self.read_notifications_for_account(page, account, settings["pause_range"])

                if settings["newsfeed_minutes"] > 0 and not self.is_task_stopped():
                    self.goto_facebook_home(page, account=account)
                    if self.interruptible_sleep(random.uniform(5, 8)):
                        self.scroll_page_for_minutes(page, settings["newsfeed_minutes"], settings["pause_range"], "newsfeed", account.get("name", ""), settings["auto_like"])

                if settings["reels_minutes"] > 0 and not self.is_task_stopped():
                    self.safe_goto(page, "https://www.facebook.com/reel/", account=account, fallback_urls=["https://facebook.com/reel/", "https://m.facebook.com/reel/"])
                    if self.interruptible_sleep(random.uniform(5, 8)):
                        self.scroll_page_for_minutes(page, settings["reels_minutes"], settings["pause_range"], "reels", account.get("name", ""), settings["auto_like"])

                if settings.get("join_groups") and not self.is_task_stopped():
                    self.maybe_join_groups_for_account(page, account, settings)

                log_item["status"] = "stopped" if self.is_task_stopped() else "done"
                log_item["end_time"] = datetime.now().strftime("%d/%m/%Y %H:%M")
                with self.log_lock:
                    self.save_logs()

                # Lưu lại cookie sau mỗi lần nuôi — FB thường rotate token trong phiên
                try:
                    fresh_cookies = context.cookies()
                    if fresh_cookies and self.build_cookie_snapshot(fresh_cookies) != self.build_cookie_snapshot(cookies):
                        self.save_account_cookies(account, fresh_cookies)
                        self.after(0, lambda name=account.get("name", ""): self.append_live_log(
                            f"[{name}] 💾 Đã lưu lại trình duyệt (cookie mới sau phiên nuôi)."
                        ))
                    else:
                        self.after(0, lambda name=account.get("name", ""): self.append_live_log(
                            f"[{name}] 💾 Cookie không thay đổi, bỏ qua lưu."
                        ))
                except Exception as save_err:
                    self.after(0, lambda name=account.get("name", ""), e=str(save_err)[:60]: self.append_live_log(
                        f"[{name}] ⚠️ Không lưu được cookie sau nuôi: {e}"
                    ))

                browser.close()
                if self.is_task_stopped():
                    self.after(0, lambda name=account.get("name", ""): self.append_live_log(f"Đã dừng nuôi {name}."))
                else:
                    self.after(0, lambda name=account.get("name", ""), plan=format_care_plan(settings): self.append_live_log(f"Hoàn tất nuôi {name}. Kế hoạch đã chạy: {plan}"))

        except Exception as e:
            with self.log_lock:
                self.logs.append({"account": account.get("name", ""), "status": "error", "error": str(e), "time": datetime.now().strftime("%d/%m/%Y %H:%M")})
                self.save_logs()
            self.after(0, lambda name=account.get("name", ""), err=e: self.append_live_log(f"Lỗi khi nuôi {name}: {err}"))

if __name__ == "__main__":
    app = FacebookCareTool()
    app.mainloop()
