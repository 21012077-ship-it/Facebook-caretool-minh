from concurrent.futures import ThreadPoolExecutor, as_completed

import customtkinter as ctk
from tkinter import filedialog, messagebox
from playwright.sync_api import sync_playwright

from .account_io import backup_accounts_file, load_import_accounts, merge_accounts, save_export_file
from .analytics import summarize_accounts, summarize_logs
from .automation import AutomationService
from .care_planner import CARE_PROFILE_LABELS, build_care_plan, format_care_plan, profile_label
from .storage import JsonStorage
from .utils import build_comment_payloads, load_json, random_delay, save_json, spin_content
import json
import os
import threading
import time
import random
from datetime import datetime
import pyotp  # Thư viện mới thêm để lấy mã 2FA

ACCOUNTS_FILE = "accounts.json"
LOGS_FILE = "logs.json"
DEFAULT_COMMENT_CONTENT = "{Chào|Hi|Hello} bạn nhé. Chúc một ngày {tốt lành|vui vẻ}!\n\nHỗ trợ Spin Content."
ACCOUNT_RENDER_BATCH_SIZE = 60
BROWSER_RENDER_BATCH_SIZE = 80
HISTORY_RENDER_BATCH_SIZE = 60


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

def get_2fa_code(secret):
    try:
        # Làm sạch chuỗi: bỏ khoảng trắng, in hoa toàn bộ
        secret = secret.replace(" ", "").upper()

        # Thêm dấu '=' cho đủ độ dài padding của Base32
        padding = len(secret) % 8
        if padding != 0:
            secret += "=" * (8 - padding)

        totp = pyotp.TOTP(secret)
        return totp.now()
    except Exception as e:
        return None

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
        self.task_pause_event = threading.Event()
        self.task_pause_event.set()
        self.task_stop_event = threading.Event()
        self.log_lock = threading.Lock()
        self.browser_selected_index = None
        self.app_settings = self.load_json("settings.json", {
            "appearance": "dark",
            "default_home_url": "https://www.facebook.com/",
            "export_sensitive_default": False,
            "import_overwrite_default": False,
            "comment_content": DEFAULT_COMMENT_CONTENT,
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
        self.save_comment_content()
        self.destroy()

    def save_accounts(self):
        self.storage.save_accounts(self.accounts)

    def save_logs(self):
        self.storage.save_logs(self.logs)

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

        # KHU VỰC TRÁI MÀN NUÔI (Bảng danh sách)
        left_care = ctk.CTkFrame(self.view_care, fg_color="transparent")
        left_care.grid(row=0, column=0, sticky="nsew")
        left_care.grid_columnconfigure(0, weight=1)
        left_care.grid_rowconfigure(3, weight=1)
        left_care.grid_rowconfigure(4, weight=0)

        header = ctk.CTkFrame(left_care, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=25, pady=(25, 10))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(header, text="Nuôi tài khoản Facebook", font=("Arial", 28, "bold")).grid(row=0, column=0, sticky="w")

        self.search_entry = ctk.CTkEntry(
            header, width=280, height=42, placeholder_text="Tìm kiếm tên / ghi chú..."
        )
        self.search_entry.grid(row=0, column=1, padx=10)
        self.search_entry.bind("<KeyRelease>", self.schedule_accounts_refresh)

        ctk.CTkButton(
            header, text="+ Thêm tài khoản", height=42, width=160, command=self.add_account_popup
        ).grid(row=0, column=2)

        self.dashboard = ctk.CTkFrame(left_care, fg_color="transparent")
        self.dashboard.grid(row=1, column=0, sticky="ew", padx=25, pady=(5, 8))
        for col in range(4):
            self.dashboard.grid_columnconfigure(col, weight=1)

        self.live_card = self.dashboard_card(self.dashboard, "Live", "0", "#14532d", 0)
        self.die_card = self.dashboard_card(self.dashboard, "Die", "0", "#7f1d1d", 1)
        self.checkpoint_card = self.dashboard_card(self.dashboard, "Checkpoint", "0", "#78350f", 2)
        self.selected_card = self.dashboard_card(self.dashboard, "Đã chọn", "0", "#1e3a8a", 3)

        filters = ctk.CTkFrame(left_care, fg_color="transparent")
        filters.grid(row=2, column=0, sticky="ew", padx=25, pady=8)
        filters.grid_columnconfigure(5, weight=1)

        self.filter_var = ctk.StringVar(value="all")

        for text, value in [("Tất cả", "all"), ("Live", "active"), ("Checkpoint", "checkpoint"), ("Die", "cookie_error")]:
            ctk.CTkRadioButton(
                filters, text=text, variable=self.filter_var, value=value, command=self.refresh_accounts
            ).pack(side="left", padx=8)

        ctk.CTkButton(
            filters, text="✓ Chọn tất cả đang lọc", width=150, fg_color="#374151", command=self.select_all_filtered_accounts
        ).pack(side="left", padx=(20, 6))

        ctk.CTkButton(
            filters, text="Bỏ chọn", width=90, fg_color="#374151", command=self.clear_selected_accounts
        ).pack(side="left", padx=6)

        ctk.CTkButton(
            filters, text="▶ Bắt đầu nuôi acc đã chọn", width=190, fg_color="#16a34a", hover_color="#15803d", command=self.start_care_selected_accounts
        ).pack(side="right", padx=6)

        self.table_outer = ctk.CTkFrame(left_care, fg_color="#111827", corner_radius=15)
        self.table_outer.grid(row=3, column=0, sticky="nsew", padx=25, pady=(8, 10))
        self.table_outer.grid_columnconfigure(0, weight=1)
        self.table_outer.grid_rowconfigure(1, weight=1)

        self.table_header = ctk.CTkFrame(self.table_outer, fg_color="#1f2937", corner_radius=12)
        self.table_header.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        self.configure_table_columns(self.table_header)

        headers = ["", "Tên", "Proxy", "Trạng thái", "Kiểu nuôi", "Lần cuối tương tác", "Ghi chú", "Thao tác"]
        for col, text in enumerate(headers):
            ctk.CTkLabel(
                self.table_header, text=text, font=("Arial", 13, "bold"), text_color="#cbd5e1", anchor="w"
            ).grid(row=0, column=col, sticky="ew", padx=8, pady=10)

        self.account_container = ctk.CTkScrollableFrame(self.table_outer, fg_color="transparent", corner_radius=0)
        self.account_container.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        self.live_log_box = ctk.CTkFrame(left_care, fg_color="#020617", corner_radius=14)
        self.live_log_box.grid(row=4, column=0, sticky="ew", padx=25, pady=(0, 15))
        self.live_log_box.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self.live_log_box, text="Log trực tiếp", font=("Arial", 15, "bold"), anchor="w").grid(row=0, column=0, sticky="w", padx=15, pady=(10, 2))

        self.live_log_text = ctk.CTkTextbox(
            self.live_log_box, height=90, fg_color="#0f172a", text_color="#d1d5db", wrap="word"
        )
        self.live_log_text.grid(row=1, column=0, sticky="ew", padx=15, pady=(4, 12))
        self.live_log_text.insert("end", "Sẵn sàng. Chọn acc rồi bấm Bắt đầu nuôi.\n")
        self.live_log_text.configure(state="disabled")

        # KHU VỰC PHẢI MÀN NUÔI (Chi tiết chăm sóc)
        # Dùng scrollable frame để phần thông tin/cài đặt vẫn xem được hết khi chọn tài khoản
        # có nội dung gợi ý dài hoặc khi cửa sổ bị thu nhỏ chiều cao.
        self.detail = ctk.CTkScrollableFrame(self.view_care, width=340, corner_radius=0)
        self.detail.grid(row=0, column=1, sticky="nsew")
        self.detail.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self.detail, text="Thông tin chăm sóc", font=("Arial", 20, "bold")).pack(pady=(20, 10), padx=20, anchor="w")

        self.detail_box = ctk.CTkFrame(self.detail)
        self.detail_box.pack(fill="x", padx=20, pady=8)

        self.detail_name = ctk.CTkLabel(self.detail_box, text="Chưa chọn tài khoản", font=("Arial", 18, "bold"))
        self.detail_name.pack(pady=(18, 5))

        self.detail_info = ctk.CTkLabel(self.detail_box, text="", justify="left", anchor="w", wraplength=280)
        self.detail_info.pack(fill="x", padx=20, pady=12)

        self.settings_box = ctk.CTkFrame(self.detail)
        self.settings_box.pack(fill="x", padx=20, pady=8)

        ctk.CTkLabel(self.settings_box, text="Cài đặt thông số", font=("Arial", 16, "bold"), anchor="w").pack(fill="x", padx=15, pady=(15, 10))

        ctk.CTkLabel(self.settings_box, text="Thời gian lướt Newsfeed", anchor="w").pack(fill="x", padx=15)
        self.newsfeed_minutes_var = ctk.StringVar(value="5")
        self.newsfeed_menu = ctk.CTkOptionMenu(self.settings_box, values=["0", "1", "3", "5", "10", "15", "20", "30"], variable=self.newsfeed_minutes_var, command=lambda _: self.refresh_selected_account_plan())
        self.newsfeed_menu.pack(fill="x", padx=15, pady=(4, 10))

        ctk.CTkLabel(self.settings_box, text="Thời gian lướt Reels", anchor="w").pack(fill="x", padx=15)
        self.reels_minutes_var = ctk.StringVar(value="5")
        self.reels_menu = ctk.CTkOptionMenu(self.settings_box, values=["0", "1", "3", "5", "10", "15", "20", "30"], variable=self.reels_minutes_var, command=lambda _: self.refresh_selected_account_plan())
        self.reels_menu.pack(fill="x", padx=15, pady=(4, 10))

        ctk.CTkLabel(self.settings_box, text="Nghỉ giữa mỗi lần cuộn", anchor="w").pack(fill="x", padx=15)
        self.pause_seconds_var = ctk.StringVar(value="4-9")
        self.pause_menu = ctk.CTkOptionMenu(self.settings_box, values=["2-5", "4-9", "6-12", "10-20"], variable=self.pause_seconds_var, command=lambda _: self.refresh_selected_account_plan())
        self.pause_menu.pack(fill="x", padx=15, pady=(4, 10))

        ctk.CTkLabel(self.settings_box, text="Số acc chạy đồng thời", anchor="w").pack(fill="x", padx=15)
        self.max_parallel_care_var = ctk.StringVar(value="2")
        self.max_parallel_care_menu = ctk.CTkOptionMenu(
            self.settings_box,
            values=["1", "2", "3", "4", "5"],
            variable=self.max_parallel_care_var,
        )
        self.max_parallel_care_menu.pack(fill="x", padx=15, pady=(4, 15))

        self.auto_like_care_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            self.settings_box,
            text="Tự động Like (mỗi 10-20 bài/reels)",
            variable=self.auto_like_care_var,
            command=self.refresh_selected_account_plan,
        ).pack(fill="x", padx=15, pady=(0, 8))

        self.smart_care_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            self.settings_box,
            text="Nuôi thông minh theo từng acc",
            variable=self.smart_care_var,
            command=self.refresh_selected_account_plan,
        ).pack(fill="x", padx=15, pady=(0, 8))

        self.read_notifications_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            self.settings_box,
            text="Đọc thông báo trong lúc nuôi",
            variable=self.read_notifications_var,
            command=self.refresh_selected_account_plan,
        ).pack(fill="x", padx=15, pady=(0, 8))

        self.join_groups_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self.settings_box,
            text="Thi thoảng tham gia 1-2 group",
            variable=self.join_groups_var,
            command=self.refresh_selected_account_plan,
        ).pack(fill="x", padx=15, pady=(0, 15))

        self.care_plan_preview = ctk.CTkLabel(
            self.settings_box,
            text="Chọn tài khoản để xem gợi ý nuôi riêng.",
            text_color="#a7f3d0",
            wraplength=280,
            justify="left",
            anchor="w",
        )
        self.care_plan_preview.pack(fill="x", padx=15, pady=(0, 15))

        ctk.CTkButton(self.detail, text="▶ Bắt đầu nuôi acc đang xem", height=42, command=self.start_care_selected_account).pack(fill="x", padx=20, pady=(10, 8))
        care_control_row = ctk.CTkFrame(self.detail, fg_color="transparent")
        care_control_row.pack(fill="x", padx=20, pady=(0, 8))
        self.care_pause_button = ctk.CTkButton(care_control_row, text="⏸ Tạm dừng", height=38, fg_color="#475569", command=self.toggle_pause_task)
        self.care_pause_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(care_control_row, text="⏹ Dừng", height=38, fg_color="#991b1b", hover_color="#7f1d1d", command=self.stop_task).pack(side="left", fill="x", expand=True, padx=(4, 0))
        ctk.CTkButton(self.detail, text="🌐 Mở Facebook", height=42, fg_color="#2563eb", command=self.open_selected_account).pack(fill="x", padx=20, pady=8)
        ctk.CTkButton(self.detail, text="✎ Sửa tài khoản", height=42, fg_color="#374151", command=self.edit_selected_account).pack(fill="x", padx=20, pady=8)
        ctk.CTkButton(self.detail, text="🗑 Xóa tài khoản", height=42, fg_color="#991b1b", hover_color="#7f1d1d", command=self.delete_selected_account).pack(fill="x", padx=20, pady=8)

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

        # 2. Nội dung Comment
        content_frame = ctk.CTkFrame(setup_frame, corner_radius=10, fg_color="#1e293b")
        content_frame.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=5, pady=5)
        content_frame.grid_columnconfigure(0, weight=1)
        content_frame.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(content_frame, text="Nội dung Comment", font=("Arial", 16, "bold")).grid(row=0, column=0, sticky="w", padx=15, pady=(15, 5))

        self.comment_content = ctk.CTkTextbox(content_frame, wrap="word")
        self.comment_content.grid(row=1, column=0, sticky="nsew", padx=15, pady=5)
        self.comment_content.insert("1.0", self.app_settings.get("comment_content", DEFAULT_COMMENT_CONTENT))
        self.comment_content.bind("<KeyRelease>", self.schedule_comment_content_save)
        self.comment_content.bind("<FocusOut>", self.save_comment_content)
        self.comment_content.bind("<<Paste>>", lambda event: self.after(50, self.save_comment_content))

        tool_cmt = ctk.CTkFrame(content_frame, fg_color="transparent")
        tool_cmt.grid(row=2, column=0, sticky="ew", padx=15, pady=5)
        self.btn_add_image = ctk.CTkButton(tool_cmt, text="📷 Thêm Ảnh/Video", width=130, fg_color="#475569", command=self.choose_comment_image)
        self.btn_add_image.pack(side="left", padx=(0, 10))
        ctk.CTkButton(tool_cmt, text="🔄 Xem thử mẫu Spin", width=130, fg_color="#0d9488", command=self.preview_spin_content).pack(side="left")

        self.spin_preview_label = ctk.CTkLabel(content_frame, text="", text_color="#a7f3d0", justify="left", wraplength=350)
        self.spin_preview_label.grid(row=3, column=0, sticky="w", padx=15, pady=(0, 15))

        # 3. Danh sách tài khoản chạy
        acc_list_frame = ctk.CTkFrame(setup_frame, corner_radius=10, fg_color="#1e293b")
        acc_list_frame.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)
        acc_list_frame.grid_columnconfigure(0, weight=1)
        acc_list_frame.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(acc_list_frame, text="Chọn Tài Khoản Chạy", font=("Arial", 16, "bold")).grid(row=0, column=0, sticky="w", padx=15, pady=(15, 5))

        self.cmt_acc_scroll = ctk.CTkScrollableFrame(acc_list_frame, fg_color="#0f172a", corner_radius=5)
        self.cmt_acc_scroll.grid(row=1, column=0, sticky="nsew", padx=15, pady=(0, 15))

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

        self.like_before_cmt_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(right_panel, text="Tự động thả Like trước khi Comment", variable=self.like_before_cmt_var).pack(anchor="w", padx=20, pady=(10, 4))

        ctk.CTkLabel(
            right_panel,
            text="Chế độ mới: nội dung sẽ được gửi vào nút Phản hồi/Reply của một comment có sẵn, không gửi thành comment mới.",
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

        io_frame = ctk.CTkFrame(body, fg_color="#111827", corner_radius=15)
        io_frame.grid(row=1, column=0, sticky="ew", pady=12)
        ctk.CTkLabel(io_frame, text="Import / Export account an toàn", font=("Arial", 18, "bold"), anchor="w").pack(fill="x", padx=18, pady=(16, 8))
        ctk.CTkLabel(io_frame, text="Export mặc định sẽ bỏ password và mã 2FA. Chỉ bật tùy chọn bên dưới khi bạn thật sự cần sao lưu đầy đủ vào nơi an toàn.", text_color="#fbbf24", wraplength=850, justify="left").pack(fill="x", padx=18, pady=(0, 10))
        self.export_sensitive_var = ctk.BooleanVar(value=bool(self.app_settings.get("export_sensitive_default", False)))
        ctk.CTkCheckBox(io_frame, text="Bao gồm password và 2FA trong file export", variable=self.export_sensitive_var).pack(anchor="w", padx=18, pady=4)
        self.import_overwrite_var = ctk.BooleanVar(value=bool(self.app_settings.get("import_overwrite_default", False)))
        ctk.CTkCheckBox(io_frame, text="Khi import, ghi đè account trùng UID/tên", variable=self.import_overwrite_var).pack(anchor="w", padx=18, pady=4)
        action_row = ctk.CTkFrame(io_frame, fg_color="transparent")
        action_row.pack(fill="x", padx=18, pady=(12, 18))
        ctk.CTkButton(action_row, text="⬇ Export accounts", width=170, fg_color="#0d9488", command=self.export_accounts_safe).pack(side="left", padx=(0, 10))
        ctk.CTkButton(action_row, text="⬆ Import accounts", width=170, fg_color="#2563eb", command=self.import_accounts_safe).pack(side="left")

        info = ctk.CTkFrame(body, fg_color="#111827", corner_radius=15)
        info.grid(row=2, column=0, sticky="ew", pady=12)
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

        for index, acc in enumerate(self.accounts):
            row = ctk.CTkFrame(self.cmt_acc_scroll, fg_color="transparent")
            row.pack(fill="x", pady=2)

            is_checked = ctk.BooleanVar(value=index in self.comment_selected_accounts)
            chk = ctk.CTkCheckBox(
                row, text=acc.get("name", "Không tên"),
                variable=is_checked,
                command=lambda idx=index, var=is_checked: self.toggle_cmt_acc(idx, var.get())
            )
            chk.pack(side="left", padx=5, pady=5)

            status = acc.get("status", "active")
            ctk.CTkLabel(row, text=self.status_text(status), fg_color=self.status_color(status), corner_radius=5, padx=8).pack(side="right", padx=5)

    def toggle_cmt_acc(self, index, checked):
        if checked:
            self.comment_selected_accounts.add(index)
        else:
            self.comment_selected_accounts.discard(index)

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

        raw_content = self.comment_content.get("1.0", "end-1c").strip()
        self.save_comment_content()
        if not raw_content:
            messagebox.showwarning("Thông báo", "Vui lòng nhập nội dung comment!")
            return

        try:
            comment_limit = int(self.limit_cmt_input.get().strip())
            if comment_limit <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Thông báo", "Giới hạn comment / tài khoản phải là số nguyên lớn hơn 0!")
            return

        self.reset_task_state()
        comment_image_paths = [
            path for path in getattr(self, "comment_image_paths", [])
            if path and os.path.exists(path)
        ]

        threading.Thread(
            target=self.run_comment_task,
            args=(list(self.comment_selected_accounts), urls, raw_content, comment_limit, comment_image_paths),
            daemon=True
        ).start()

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

    def click_existing_comment_reply_button(self, page, acc_name):
        reply_selectors = [
            "div[role='button'][aria-label='Phản hồi'], div[aria-label='Phản hồi']",
            "div[role='button'][aria-label='Reply'], div[aria-label='Reply']",
            "text=Phản hồi",
            "text=Reply",
        ]

        for scroll_round in range(6):
            if not self.wait_if_paused():
                return False

            for selector in reply_selectors:
                try:
                    reply_buttons = page.locator(selector)
                    button_count = min(reply_buttons.count(), 8)
                    for index in range(button_count):
                        button = reply_buttons.nth(index)
                        if not button.is_visible():
                            continue

                        button.scroll_into_view_if_needed()
                        if not self.interruptible_sleep(random.uniform(0.4, 0.9)):
                            return False

                        button.click()
                        self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] Đã bấm Phản hồi vào một comment có sẵn."))
                        return True
                except Exception:
                    continue

            if scroll_round < 5:
                self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] Đang tìm comment có sẵn để phản hồi..."))
                page.mouse.wheel(0, random.randint(500, 900))
                if not self.interruptible_sleep(random.uniform(1.0, 1.8)):
                    return False

        return False

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

    def run_comment_task(self, account_indexes, urls, raw_content, comment_limit, comment_image_paths=None):
        delay_range = self.delay_cmt_input.get()
        comment_image_paths = [path for path in (comment_image_paths or []) if os.path.exists(path)]
        comment_payloads = build_comment_payloads(raw_content, comment_image_paths)

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

        acc_tasks = {acc_idx: [] for acc_idx in account_indexes}
        skipped_urls = 0
        for url in urls:
            available_accounts = [idx for idx in account_indexes if len(acc_tasks[idx]) < comment_limit]
            if not available_accounts:
                skipped_urls += 1
                continue
            acc_idx = min(available_accounts, key=lambda idx: len(acc_tasks[idx]))
            acc_tasks[acc_idx].append(url)

        if skipped_urls:
            self.after(0, lambda count=skipped_urls, limit=comment_limit: self.append_live_log(f"⚠️ Bỏ qua {count} link vì đã đạt giới hạn {limit} comment/tài khoản."))

        for acc_idx, acc_urls in acc_tasks.items():
            if self.is_task_stopped():
                break
            if not acc_urls:
                continue
            if acc_idx >= len(self.accounts): continue

            account = self.accounts[acc_idx]
            acc_name = account.get("name", "Unknown")
            self.after(0, lambda n=acc_name, count=len(acc_urls): self.append_live_log(f"🚀 [{n}] Được phân công chạy {count} link."))

            try:
                cookies = self.load_cookies(account)
                with sync_playwright() as p:
                    browser, context, page = self.create_browser_page(p, cookies, account)

                    # KIỂM TRA & AUTO ĐĂNG NHẬP NẾU CHƯA CÓ COOKIE
                    self.ensure_login(context, page, account)

                    for url in acc_urls:
                        if not self.wait_if_paused():
                            break
                        comment_payload = random.choice(comment_payloads)
                        final_content = spin_content(comment_payload["text"])
                        selected_image_path = comment_payload.get("media_path") or None

                        self.after(0, lambda n=acc_name, u=url: self.append_live_log(f"[{n}] Đang vào bài: {u[:40]}..."))
                        self.safe_goto(page, url, account=account)
                        if not self.interruptible_sleep(random.uniform(4, 7)):
                            break

                        page.mouse.wheel(0, 500)
                        if not self.interruptible_sleep(2):
                            break

                        if self.like_before_cmt_var.get():
                            try:
                                like_btn = page.locator("div[aria-label='Thích'], div[aria-label='Like']").first
                                if like_btn.is_visible():
                                    like_btn.click()
                                    if not self.interruptible_sleep(random.uniform(1.5, 3)):
                                        break
                            except: pass

                        comment_success = False
                        try:
                            if not self.click_existing_comment_reply_button(page, acc_name):
                                raise RuntimeError("Không tìm thấy nút Phản hồi/Reply của comment có sẵn")

                            if not self.interruptible_sleep(random.uniform(1, 2)):
                                break

                            comment_box = self.find_reply_comment_box(page)
                            comment_box.scroll_into_view_if_needed()
                            comment_box.wait_for(state="visible", timeout=10000)

                            comment_box.click()
                            if not self.interruptible_sleep(random.uniform(1, 2)):
                                break

                            self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] Đang gõ phản hồi: '{final_content[:20]}...'"))

                            page.keyboard.press("Control+A")
                            page.keyboard.press("Backspace")
                            if not self.interruptible_sleep(0.5):
                                break

                            page.keyboard.type(final_content, delay=random.uniform(50, 120))
                            if not self.interruptible_sleep(random.uniform(1.5, 2.5)):
                                break

                            if selected_image_path:
                                image_name = os.path.basename(selected_image_path)
                                self.after(0, lambda n=acc_name, img=image_name: self.append_live_log(f"[{n}] Đang đính kèm ảnh/video cùng phản hồi: {img}"))
                                try:
                                    with page.expect_file_chooser(timeout=8000) as fc_info:
                                        attach_btn = page.locator("div[aria-label*='Đính kèm'], div[aria-label*='Attach']").first
                                        attach_btn.click()

                                    file_chooser = fc_info.value
                                    file_chooser.set_files(selected_image_path)
                                    if not self.interruptible_sleep(random.uniform(4, 7)):
                                        break

                                    self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] Đã đính kèm, chuẩn bị gửi phản hồi chung text + ảnh/video..."))
                                    comment_box.click()
                                    if not self.interruptible_sleep(1.5):
                                        break
                                except Exception as e:
                                    self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] ❌ Không gửi comment vì ảnh/video đi kèm chưa đính kèm được."))
                                    raise

                            page.keyboard.press("Enter")
                            comment_success = True

                            self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] ✅ Phản hồi comment thành công!"))

                        except Exception as e:
                            self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] ❌ Lỗi: Không thể phản hồi vào comment có sẵn."))

                        delay_sec = self.get_pause_seconds(delay_range)
                        if comment_success:
                            if not self.browse_during_comment_pause(page, account, delay_sec):
                                break
                        else:
                            self.after(0, lambda n=acc_name, d=delay_sec: self.append_live_log(f"[{n}] ⏳ Đang nghỉ {int(d)} giây trước link tiếp theo..."))
                            if not self.interruptible_sleep(delay_sec):
                                break

                    browser.close()
            except Exception as e:
                self.after(0, lambda n=acc_name, err=str(e): self.append_live_log(f"[{n}] ❌ Lỗi profile: {err}"))

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
        self.refresh_accounts()
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

    def save_app_settings(self):
        self.save_comment_content(show_message=False)
        self.app_settings["default_home_url"] = self.default_url_entry.get().strip() or "https://www.facebook.com/"
        self.app_settings["export_sensitive_default"] = self.export_sensitive_var.get()
        self.app_settings["import_overwrite_default"] = self.import_overwrite_var.get()
        self.save_json("settings.json", self.app_settings)
        if hasattr(self, "browser_url_entry"):
            self.browser_url_entry.delete(0, "end")
            self.browser_url_entry.insert(0, self.app_settings["default_home_url"])
        messagebox.showinfo("Đã lưu", "Đã lưu cài đặt.")

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
            self.append_live_log("⏸ Đã tạm dừng task đang chạy.")
        else:
            self.task_pause_event.set()
            self.update_pause_buttons(False)
            self.append_live_log("▶ Đã tiếp tục task đang chạy.")

    def stop_task(self):
        self.task_stop_event.set()
        self.task_pause_event.set()
        self.update_pause_buttons(False)
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
            time.sleep(min(0.2, max(0, end_time - time.time())))
        return not self.is_task_stopped()

    # --- CÁC HÀM TIỆN ÍCH CHUNG ---
    def configure_table_columns(self, frame):
        widths = [42, 170, 170, 115, 185, 150, 200, 150]
        for col, width in enumerate(widths):
            frame.grid_columnconfigure(col, minsize=width, weight=1 if col in (1, 2, 6) else 0)

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

    def status_color(self, status):
        return {
            "active": "#16a34a",
            "checkpoint": "#d97706",
            "cookie_error": "#dc2626"
        }.get(status, "#6b7280")

    def status_text(self, status):
        return {
            "active": "Live",
            "checkpoint": "Checkpoint",
            "cookie_error": "Die"
        }.get(status, "Không rõ")

    def append_live_log(self, message):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-80:]

        if hasattr(self, "live_log_text"):
            self.live_log_text.configure(state="normal")
            self.live_log_text.delete("1.0", "end")
            self.live_log_text.insert("end", "\n".join(self.log_lines) + "\n")
            self.live_log_text.see("end")
            self.live_log_text.configure(state="disabled")

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
        live = sum(1 for acc in self.accounts if acc.get("status", "active") == "active")
        die = sum(1 for acc in self.accounts if acc.get("status") == "cookie_error")
        checkpoint = sum(1 for acc in self.accounts if acc.get("status") == "checkpoint")

        self.live_card.configure(text=str(live))
        self.die_card.configure(text=str(die))
        self.checkpoint_card.configure(text=str(checkpoint))
        self.selected_card.configure(text=str(len(self.selected_accounts)))
        self.stat_label.configure(text=f"Tổng tài khoản: {len(self.accounts)}")

    def schedule_accounts_refresh(self, event=None):
        if self.account_refresh_job:
            self.after_cancel(self.account_refresh_job)
        self.account_refresh_job = self.after(180, self.refresh_accounts)

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
        self.configure_table_columns(row_frame)
        self.account_rows[index] = row_frame

        is_checked = ctk.BooleanVar(value=index in self.selected_accounts)
        checkbox = ctk.CTkCheckBox(
            row_frame, text="", width=28, variable=is_checked,
            command=lambda idx=index, var=is_checked: self.toggle_account_selection(idx, var.get())
        )
        checkbox.grid(row=0, column=0, sticky="w", padx=8, pady=10)

        name_label = ctk.CTkLabel(row_frame, text=acc.get("name", "Không tên"), font=("Arial", 14, "bold"), anchor="w")
        name_label.grid(row=0, column=1, sticky="ew", padx=8, pady=10)

        proxy_text = acc.get("proxy", "") or "Không dùng proxy"
        ctk.CTkLabel(row_frame, text=proxy_text, text_color="#cbd5e1", anchor="w").grid(row=0, column=2, sticky="ew", padx=8, pady=10)

        ctk.CTkLabel(
            row_frame, text=self.status_text(acc.get("status", "active")),
            fg_color=self.status_color(acc.get("status", "active")),
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
        ctk.CTkLabel(row_frame, text=acc.get("note", ""), text_color="#cbd5e1", anchor="w").grid(row=0, column=6, sticky="ew", padx=8, pady=10)

        action_box = ctk.CTkFrame(row_frame, fg_color="transparent")
        action_box.grid(row=0, column=7, sticky="e", padx=8, pady=8)

        ctk.CTkButton(action_box, text="Nuôi", width=58, height=30, command=lambda: self.select_and_care(index)).pack(side="left", padx=3)
        ctk.CTkButton(action_box, text="Chi tiết", width=70, height=30, fg_color="#374151", command=lambda: self.select_account(index)).pack(side="left", padx=3)

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
        for index, _ in self.get_filtered_accounts():
            self.selected_accounts.add(index)
        self.refresh_accounts()

    def clear_selected_accounts(self):
        self.selected_accounts.clear()
        self.refresh_accounts()

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

        self.detail_name.configure(text=acc.get("name", "Không tên"))

        info = (
            f"Trạng thái: {self.status_text(acc.get('status', 'active'))}\n\n"
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
        popup.geometry("470x620")
        popup.grab_set()

        current = self.accounts[edit_index] if edit_index is not None else {}

        ctk.CTkLabel(popup, text="Dữ liệu (UID|Pass|2FA) hoặc Tên").pack(pady=(20, 5))
        name_entry = ctk.CTkEntry(popup, width=360, placeholder_text="Ví dụ: 1000...|abc123|ABCDEF...")
        name_entry.pack()
        name_entry.insert(0, current.get("name", ""))

        ctk.CTkLabel(popup, text="Trạng thái").pack(pady=(15, 5))
        status_var = ctk.StringVar(value=current.get("status", "active"))
        status_menu = ctk.CTkOptionMenu(popup, width=360, variable=status_var, values=["active", "checkpoint", "cookie_error"])
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

            uid, password, two_fa, name = "", "", "", raw_data
            # Tự động trích xuất thông tin nếu nhập định dạng UID|Pass|2FA
            if "|" in raw_data:
                parts = raw_data.split("|")
                uid = parts[0].strip()
                password = parts[1].strip() if len(parts) > 1 else ""
                two_fa = parts[2].strip() if len(parts) > 2 else ""
                name = uid # Gán tên hiển thị là UID luôn

            # Xử lý tự động tạo đường dẫn cookie nếu người dùng không chọn file
            cookie_path = cookie_var.get().strip()
            if not cookie_path and uid:
                if not os.path.exists("cookies"):
                    os.makedirs("cookies")
                cookie_path = os.path.join("cookies", f"{uid}.json")

            account = {
                "name": name,
                "uid": uid,
                "password": password,
                "two_fa": two_fa,
                "status": status_var.get(),
                "note": note_entry.get().strip(),
                "proxy": proxy_entry.get().strip(),
                "cookie_file": cookie_path,
                "created_at": current.get("created_at", datetime.now().strftime("%d/%m/%Y %H:%M")),
                "last_open": current.get("last_open", "Chưa mở"),
                "last_care": current.get("last_care", "Chưa nuôi"),
                "care_profile": profile_lookup.get(profile_var.get(), "auto"),
                "care_plan_note": current.get("care_plan_note", "")
            }

            if edit_index is None: self.accounts.append(account)
            else: self.accounts[edit_index] = account

            self.save_accounts()
            self.refresh_accounts()
            popup.destroy()

        ctk.CTkButton(popup, text="Lưu", width=360, height=40, command=save).pack(pady=25)

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
        self.refresh_accounts()

        threading.Thread(target=self.open_browser, args=(account,), daemon=True).start()

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
                plan = self.get_account_care_plan(account, use_smart=use_smart)
                account["care_plan_note"] = format_care_plan(plan)
                has_plan_action = (
                    plan["newsfeed_minutes"] > 0
                    or plan["reels_minutes"] > 0
                    or plan.get("read_notifications")
                    or (plan.get("join_groups") and plan.get("max_join_groups", 0) > 0)
                )
                if not has_plan_action:
                    skipped_count += 1
                    self.append_live_log(f"Bỏ qua {account.get('name', 'Unknown')}: {plan.get('reason', '')}")
                    continue
                account["last_care"] = now
                queued_count += 1
                care_jobs.append((account, plan))

        self.save_accounts()
        self.refresh_accounts()
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
        futures = []
        with ThreadPoolExecutor(max_workers=max_parallel) as executor:
            for account, plan in care_jobs:
                if self.is_task_stopped():
                    break
                futures.append(executor.submit(self.care_account, account, plan))
                time.sleep(1.5)

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    self.after(0, lambda err=exc: self.append_live_log(f"⚠️ Lỗi worker nuôi acc: {err}"))

        self.after(0, lambda: self.append_live_log("✅ Hàng chờ nuôi tài khoản đã kết thúc."))

    # --- BỘ LÕI BROWSER AUTOMATION (PLAYWRIGHT) ---
    def normalize_cookie(self, cookie):
        return self.automation_service.normalize_cookie(cookie)

    def load_cookies(self, account):
        return self.automation_service.load_cookies(account)

    def parse_proxy(self, proxy_text):
        return self.automation_service.parse_proxy(proxy_text)

    def create_browser_page(self, p, cookies, account=None):
        return self.automation_service.create_browser_page(p, cookies, account)

    def save_account_cookies(self, account, cookies):
        cookie_file = self.automation_service.save_cookies(account, cookies)
        self.save_accounts()
        self.after(0, self.refresh_accounts)
        self.after(0, self.refresh_browser_accounts)
        return cookie_file

    def has_facebook_login_cookie(self, cookies):
        return self.automation_service.has_facebook_login_cookie(cookies)

    def build_cookie_snapshot(self, cookies):
        tracked_fields = ("name", "value", "domain", "path", "expires", "expirationDate")
        return tuple(
            sorted(
                tuple(str(cookie.get(field, "")) for field in tracked_fields)
                for cookie in cookies
            )
        )

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
                    return page.goto(current_url, wait_until=wait_until, timeout=timeout)
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
        raise Exception(
            f"Không mở được Facebook ({last_error}). "
            f"Nếu đang dùng proxy [{proxy_text}], hãy đổi/tắt proxy hoặc kiểm tra mạng vì trình duyệt nhận phản hồi rỗng."
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

    # --- HÀM MỚI: KIỂM TRA VÀ TỰ ĐỘNG ĐĂNG NHẬP NẾU CHƯA CÓ COOKIE ---
    def ensure_login(self, context, page, account):
        uid = account.get("uid", "")
        password = account.get("password", "")
        two_fa = account.get("two_fa", "")

        # Vào thử trang chủ FB để check xem cookie có hoạt động không
        self.goto_facebook_home(page, account=account, mobile=True)
        time.sleep(3)

        # Nếu không bị đá ra trang đăng nhập/checkpoint/two_step -> Cookie vẫn sống
        if "login" not in page.url and "checkpoint" not in page.url and "two_step_verification" not in page.url:
            if page.locator("input[name='email'], input[id='email']").is_hidden():
                return True

        # Nếu rơi xuống đây tức là Cookie đã chết HOẶC chưa từng có Cookie
        if not uid or not password:
            account["status"] = "cookie_error"
            self.save_accounts()
            self.after(0, self.refresh_accounts)
            raise Exception("Không có Cookie và không có UID/Pass để tự động đăng nhập!")

        self.after(0, lambda: self.append_live_log(f"[{uid}] Cookie trống/die, đang tự động đăng nhập..."))

        if "login" not in page.url:
            self.safe_goto(page, "https://facebook.com/login/", account=account, fallback_urls=["https://www.facebook.com/login/", "https://m.facebook.com/login/"])
            time.sleep(2)

        # 1. Điền tài khoản, mật khẩu
        page.locator('input[name="email"], input[id="email"]').first.fill(uid)
        time.sleep(0.5) # Nghỉ 0.5s cho giống người thật

        pass_input = page.locator('input[name="pass"], input[id="pass"]').first
        pass_input.fill(password)
        time.sleep(1) # Nghỉ 1s trước khi bấm

        # 2. Bấm phím Enter ngay tại ô mật khẩu để Submit Form
        pass_input.press("Enter")

        self.after(0, lambda: self.append_live_log(f"[{uid}] Đã ấn Enter, chờ load 2FA..."))
        time.sleep(8) # Tăng thời gian chờ lên 8s để đảm bảo mạng load xong trang 2FA

        # 3. Quét form 2FA (Bản cập nhật thông minh - Chờ tối đa 15 giây)
        try:
            self.after(0, lambda: self.append_live_log(f"[{uid}] Đang chờ form nhập mã 2FA hiển thị..."))

            # Vẫn giữ 1 chút sleep để tránh việc Playwright quét quá nhanh khi form đang chớp nháy
            time.sleep(3)

            # Gom thêm các bộ chọn (locator) phổ biến của Facebook 2FA
            two_fa_box = page.locator(
                'input[aria-label="Mã"], '
                'input[aria-label="Login code"], '
                'input[aria-label="Code"], '
                'input[autocomplete="one-time-code"], '
                'input[id="approvals_code"], '
                'input[type="text"]'
            ).locator("visible=true").first

            # SỬ DỤNG WAIT_FOR: Ép trình duyệt liên tục quét tìm ô 2FA trong tối đa 15 giây
            two_fa_box.wait_for(state="visible", timeout=15000)

            # Nếu code vượt qua được dòng wait_for bên trên, nghĩa là ô 2FA chắc chắn đã hiển thị
            code = get_2fa_code(two_fa)
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
                time.sleep(8) # Tăng chờ lên 8s để FB load xong trang sau khi nhập 2FA

                # Xử lý nút "Lưu trình duyệt" (Save browser) nếu FB hỏi
                for _ in range(3):
                    btn_continue = page.locator(
                        'div[role="button"]:has-text("Lưu"), '
                        'div[role="button"]:has-text("Tiếp tục"), '
                        'div[role="button"]:has-text("Xác nhận"), '
                        'div[aria-label="Tiếp tục"], '
                        'div[aria-label="Lưu trình duyệt"]'
                    ).first

                    if btn_continue.is_visible():
                        btn_continue.click(no_wait_after=True)
                        time.sleep(3)
                    else:
                        break
            else:
                self.after(0, lambda: self.append_live_log(f"[{uid}] ❌ Lỗi: Mã Secret 2FA không tạo được 6 số. Kiểm tra lại chuỗi 2FA!"))

        except Exception as e:
            # Nếu hết 15 giây mà wait_for() vẫn không tìm thấy ô nhập 2FA, nó sẽ nhảy xuống đây
            self.after(0, lambda: self.append_live_log(f"[{uid}] Không bắt được ô 2FA (Có thể không cần hoặc timeout). Bỏ qua..."))

        try:
            page.wait_for_url("**/facebook.com/**", timeout=15000)
        except:
            pass

        # Kiểm tra lại URL xem đã vào được bên trong chưa
        if "login" in page.url or "checkpoint" in page.url or "two_step_verification" in page.url:
            account["status"] = "checkpoint"
            self.save_accounts()
            self.after(0, self.refresh_accounts)
            raise Exception("Đăng nhập thất bại (Sai pass hoặc dính Checkpoint/2FA).")

        self.after(0, lambda: self.append_live_log(f"[{uid}] Đăng nhập thành công! Đang lưu cookie mới..."))

        # Lưu Cookie Mới
        new_cookies = context.cookies()
        account["status"] = "active"
        self.save_account_cookies(account, new_cookies)
        self.save_accounts()
        self.after(0, self.refresh_accounts)
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
                current_cookies = context.cookies()
                if self.has_facebook_login_cookie(current_cookies):
                    cookie_snapshot = self.build_cookie_snapshot(current_cookies)
                    if cookie_snapshot != saved_cookie_snapshot:
                        account["status"] = "active"
                        cookie_path = self.save_account_cookies(account, current_cookies)
                        if cookie_path != saved_cookie_path:
                            self.after(0, lambda n=account_name, path=cookie_path: self.append_live_log(
                                f"[{n}] ✅ Đã phát hiện đăng nhập thủ công và lưu cookie: {path}"
                            ))
                        saved_cookie_path = cookie_path
                        saved_cookie_snapshot = cookie_snapshot
            except Exception as e:
                self.after(0, lambda n=account_name, err=e: self.append_live_log(
                    f"[{n}] ⚠️ Chưa thể lưu cookie phiên thủ công: {err}"
                ))

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
                except Exception as navigation_error:
                    account_name = account.get("name") or account.get("uid") or "Unknown"
                    self.after(0, lambda n=account_name, err=navigation_error: self.append_live_log(
                        f"[{n}] ⚠️ Không mở được URL ban đầu nhưng vẫn giữ Chrome để thao tác thủ công: {err}"
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
