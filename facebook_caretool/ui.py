import customtkinter as ctk
from tkinter import filedialog, messagebox
from playwright.sync_api import sync_playwright

from .automation import AutomationService
from .storage import JsonStorage
from .utils import load_json, random_delay, save_json, spin_content
import json
import os
import threading
import time
import random
from datetime import datetime
import pyotp  # Thư viện mới thêm để lấy mã 2FA

ACCOUNTS_FILE = "accounts.json"
LOGS_FILE = "logs.json"

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
        self.geometry("1300x750")
        self.minsize(1200, 700)

        self.storage = JsonStorage(ACCOUNTS_FILE, LOGS_FILE)
        self.automation_service = AutomationService()
        self.accounts = self.storage.load_accounts()
        self.logs = self.storage.load_logs()

        self.selected_index = None
        self.comment_selected_accounts = set()
        self.comment_image_path = "" # Lưu đường dẫn ảnh/video muốn comment
        self.task_pause_event = threading.Event()
        self.task_pause_event.set()
        self.task_stop_event = threading.Event()

        self.build_ui()
        self.refresh_accounts()

    def load_json(self, path, default):
        return load_json(path, default)

    def save_json(self, path, data):
        save_json(path, data)

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
        self.create_menu_btn("Trình duyệt", None)
        self.create_menu_btn("Lịch sử nuôi", None)
        self.create_menu_btn("Cài đặt", None)

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

        self.build_view_care()
        self.build_view_comment()

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

        if view_name == "care":
            self.view_comment.grid_forget()
            self.view_care.grid(row=0, column=0, sticky="nsew")
        elif view_name == "comment":
            self.view_care.grid_forget()
            self.refresh_comment_accounts()
            self.view_comment.grid(row=0, column=0, sticky="nsew")

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
        self.search_entry.bind("<KeyRelease>", lambda e: self.refresh_accounts())

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

        headers = ["", "Tên", "Proxy", "Trạng thái", "Lần cuối tương tác", "Ghi chú", "Thao tác"]
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
        self.detail = ctk.CTkFrame(self.view_care, width=340, corner_radius=0)
        self.detail.grid(row=0, column=1, sticky="nsew")
        self.detail.grid_propagate(False)

        ctk.CTkLabel(self.detail, text="Thông tin chăm sóc", font=("Arial", 20, "bold")).pack(pady=(20, 10), padx=20, anchor="w")

        self.detail_box = ctk.CTkFrame(self.detail)
        self.detail_box.pack(fill="x", padx=20, pady=8)

        self.detail_name = ctk.CTkLabel(self.detail_box, text="Chưa chọn tài khoản", font=("Arial", 18, "bold"))
        self.detail_name.pack(pady=(18, 5))

        self.detail_info = ctk.CTkLabel(self.detail_box, text="", justify="left", anchor="w")
        self.detail_info.pack(fill="x", padx=20, pady=12)

        self.settings_box = ctk.CTkFrame(self.detail)
        self.settings_box.pack(fill="x", padx=20, pady=8)

        ctk.CTkLabel(self.settings_box, text="Cài đặt thông số", font=("Arial", 16, "bold"), anchor="w").pack(fill="x", padx=15, pady=(15, 10))

        ctk.CTkLabel(self.settings_box, text="Thời gian lướt Newsfeed", anchor="w").pack(fill="x", padx=15)
        self.newsfeed_minutes_var = ctk.StringVar(value="5")
        self.newsfeed_menu = ctk.CTkOptionMenu(self.settings_box, values=["0", "1", "3", "5", "10", "15", "20", "30"], variable=self.newsfeed_minutes_var)
        self.newsfeed_menu.pack(fill="x", padx=15, pady=(4, 10))

        ctk.CTkLabel(self.settings_box, text="Thời gian lướt Reels", anchor="w").pack(fill="x", padx=15)
        self.reels_minutes_var = ctk.StringVar(value="5")
        self.reels_menu = ctk.CTkOptionMenu(self.settings_box, values=["0", "1", "3", "5", "10", "15", "20", "30"], variable=self.reels_minutes_var)
        self.reels_menu.pack(fill="x", padx=15, pady=(4, 10))

        ctk.CTkLabel(self.settings_box, text="Nghỉ giữa mỗi lần cuộn", anchor="w").pack(fill="x", padx=15)
        self.pause_seconds_var = ctk.StringVar(value="4-9")
        self.pause_menu = ctk.CTkOptionMenu(self.settings_box, values=["2-5", "4-9", "6-12", "10-20"], variable=self.pause_seconds_var)
        self.pause_menu.pack(fill="x", padx=15, pady=(4, 15))

        self.auto_like_care_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(self.settings_box, text="Tự động Like (mỗi 10-20 bài/reels)", variable=self.auto_like_care_var).pack(fill="x", padx=15, pady=(0, 15))

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
        self.comment_content.insert("1.0", "{Chào|Hi|Hello} bạn nhé. Chúc một ngày {tốt lành|vui vẻ}!\n\nHỗ trợ Spin Content.")

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
        ctk.CTkCheckBox(right_panel, text="Tự động thả Like trước khi Comment", variable=self.like_before_cmt_var).pack(anchor="w", padx=20, pady=10)

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
        path = filedialog.askopenfilename(
            title="Chọn Ảnh/Video để Comment",
            filetypes=[("Image/Video Files", "*.png *.jpg *.jpeg *.mp4 *.avi *.gif"), ("All Files", "*.*")]
        )
        if path:
            self.comment_image_path = path
            self.btn_add_image.configure(text="✅ Đã có Ảnh", fg_color="#059669")
            self.append_live_log(f"Đã chọn file ảnh/video: {os.path.basename(path)}")
        else:
            self.comment_image_path = ""
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
        threading.Thread(
            target=self.run_comment_task,
            args=(list(self.comment_selected_accounts), urls, raw_content, comment_limit),
            daemon=True
        ).start()

    def run_comment_task(self, account_indexes, urls, raw_content, comment_limit):
        comment_pool = [line.strip() for line in raw_content.split('\n') if line.strip()]

        if not comment_pool:
            self.after(0, lambda: messagebox.showwarning("Lỗi", "Không tìm thấy nội dung comment hợp lệ!"))
            return

        delay_range = self.delay_cmt_input.get()

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
                        random_line = random.choice(comment_pool)
                        final_content = spin_content(random_line)

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

                        try:
                            comment_box = page.locator('div[role="textbox"][contenteditable="true"][aria-label="Viết bình luận..."], div[role="textbox"][contenteditable="true"][data-lexical-editor="true"]').last
                            comment_box.scroll_into_view_if_needed()
                            comment_box.wait_for(state="visible", timeout=10000)

                            comment_box.click()
                            if not self.interruptible_sleep(random.uniform(1, 2)):
                                break

                            self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] Đang gõ: '{final_content[:20]}...'"))

                            page.keyboard.press("Control+A")
                            page.keyboard.press("Backspace")
                            if not self.interruptible_sleep(0.5):
                                break

                            page.keyboard.type(final_content, delay=random.uniform(50, 120))
                            if not self.interruptible_sleep(random.uniform(1.5, 2.5)):
                                break

                            if hasattr(self, 'comment_image_path') and self.comment_image_path and os.path.exists(self.comment_image_path):
                                self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] Đang tải ảnh/video đính kèm..."))
                                try:
                                    with page.expect_file_chooser(timeout=8000) as fc_info:
                                        attach_btn = page.locator("div[aria-label*='Đính kèm'], div[aria-label*='Attach']").first
                                        attach_btn.click()

                                    file_chooser = fc_info.value
                                    file_chooser.set_files(self.comment_image_path)
                                    if not self.interruptible_sleep(random.uniform(4, 7)):
                                        break

                                    self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] Đang lấy lại focus..."))
                                    comment_box.click()
                                    if not self.interruptible_sleep(1.5):
                                        break
                                except Exception as e:
                                    self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] ⚠️ Bỏ qua up ảnh (Không tìm thấy nút đính kèm)."))

                            page.keyboard.press("Enter")

                            self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] ✅ Comment thành công!"))

                        except Exception as e:
                            self.after(0, lambda n=acc_name: self.append_live_log(f"[{n}] ❌ Lỗi: Không thể tương tác với ô comment."))

                        delay_sec = self.get_pause_seconds(delay_range)
                        self.after(0, lambda n=acc_name, d=delay_sec: self.append_live_log(f"[{n}] ⏳ Đang nghỉ {int(d)} giây..."))
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
        widths = [42, 180, 190, 120, 160, 220, 150]
        for col, width in enumerate(widths):
            frame.grid_columnconfigure(col, minsize=width, weight=1 if col in (1, 2, 5) else 0)

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

    def refresh_accounts(self):
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

        for row, (i, acc) in enumerate(filtered):
            self.account_row(row, i, acc)

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

        last_touch = acc.get("last_care") or acc.get("last_open") or "Chưa tương tác"
        ctk.CTkLabel(row_frame, text=last_touch, text_color="#9ca3af", anchor="w").grid(row=0, column=4, sticky="ew", padx=8, pady=10)
        ctk.CTkLabel(row_frame, text=acc.get("note", ""), text_color="#cbd5e1", anchor="w").grid(row=0, column=5, sticky="ew", padx=8, pady=10)

        action_box = ctk.CTkFrame(row_frame, fg_color="transparent")
        action_box.grid(row=0, column=6, sticky="e", padx=8, pady=8)

        ctk.CTkButton(action_box, text="Nuôi", width=58, height=30, command=lambda: self.select_and_care(index)).pack(side="left", padx=3)
        ctk.CTkButton(action_box, text="Chi tiết", width=70, height=30, fg_color="#374151", command=lambda: self.select_account(index)).pack(side="left", padx=3)

        for widget in row_frame.winfo_children():
            if widget is not checkbox and widget is not action_box:
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

    def select_account(self, index):
        self.selected_index = index
        acc = self.accounts[index]

        self.detail_name.configure(text=acc.get("name", "Không tên"))

        info = (
            f"Trạng thái: {self.status_text(acc.get('status', 'active'))}\n\n"
            f"Cookie: {os.path.basename(acc.get('cookie_file', ''))}\n\n"
            f"Proxy: {acc.get('proxy', 'Không dùng proxy') or 'Không dùng proxy'}\n\n"
            f"Ghi chú: {acc.get('note', '')}\n\n"
            f"Ngày thêm: {acc.get('created_at', '')}\n"
            f"Lần mở cuối: {acc.get('last_open', 'Chưa mở')}\n"
            f"Lần nuôi cuối: {acc.get('last_care', 'Chưa nuôi')}"
        )
        self.detail_info.configure(text=info)

    # ĐÃ CHỈNH SỬA: Cho phép nhập UID|Pass|2FA vào Tên tài khoản
    def add_account_popup(self, edit_index=None):
        popup = ctk.CTkToplevel(self)
        popup.title("Thêm tài khoản" if edit_index is None else "Sửa tài khoản")
        popup.geometry("470x540")
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

        ctk.CTkLabel(popup, text="Ghi chú").pack(pady=(15, 5))
        note_entry = ctk.CTkEntry(popup, width=360)
        note_entry.pack()
        note_entry.insert(0, current.get("note", ""))

        ctk.CTkLabel(popup, text="Proxy (bỏ trống nếu không dùng)").pack(pady=(15, 5))
        proxy_entry = ctk.CTkEntry(popup, width=360, placeholder_text="host:port hoặc host:port:user:pass")
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
                "last_care": current.get("last_care", "Chưa nuôi")
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
        newsfeed_minutes = int(self.newsfeed_minutes_var.get())
        reels_minutes = int(self.reels_minutes_var.get())
        pause_range = self.pause_seconds_var.get()

        if newsfeed_minutes <= 0 and reels_minutes <= 0:
            messagebox.showwarning("Thông báo", "Hãy chọn thời gian Newsfeed hoặc Reels lớn hơn 0.")
            return

        self.reset_task_state()

        settings = {
            "newsfeed_minutes": newsfeed_minutes,
            "reels_minutes": reels_minutes,
            "pause_range": pause_range,
            "auto_like": self.auto_like_care_var.get()
        }
        now = datetime.now().strftime("%d/%m/%Y %H:%M")

        for index in index_list:
            if index < len(self.accounts):
                self.accounts[index]["last_care"] = now
                threading.Thread(target=self.care_account, args=(self.accounts[index], settings), daemon=True).start()

        self.save_accounts()
        self.refresh_accounts()
        self.append_live_log(f"Đã đưa {len(index_list)} tài khoản vào hàng chờ nuôi.")

    # --- BỘ LÕI BROWSER AUTOMATION (PLAYWRIGHT) ---
    def normalize_cookie(self, cookie):
        return self.automation_service.normalize_cookie(cookie)

    def load_cookies(self, account):
        return self.automation_service.load_cookies(account)

    def parse_proxy(self, proxy_text):
        return self.automation_service.parse_proxy(proxy_text)

    def create_browser_page(self, p, cookies, account=None):
        return self.automation_service.create_browser_page(p, cookies, account)

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
        cookie_file = account.get("cookie_file")
        if not cookie_file:
            if not os.path.exists("cookies"): os.makedirs("cookies")
            cookie_file = os.path.join("cookies", f"{uid}.json")
            account["cookie_file"] = cookie_file

        with open(cookie_file, "w", encoding="utf-8") as f:
            json.dump(new_cookies, f, indent=4)

        account["status"] = "active"
        self.save_accounts()
        self.after(0, self.refresh_accounts)
        return True

    def open_browser(self, account):
        try:
            cookies = self.load_cookies(account)
            with sync_playwright() as p:
                browser, context, page = self.create_browser_page(p, cookies, account)

                # GỌI HÀM KIỂM TRA ĐĂNG NHẬP Ở ĐÂY
                self.ensure_login(context, page, account)

                self.goto_facebook_home(page, account=account)
                while True: time.sleep(1)
        except Exception as e:
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

    def care_account(self, account, settings):
        try:
            cookies = self.load_cookies(account)
            start_time = datetime.now().strftime("%d/%m/%Y %H:%M")
            log_item = {"account": account.get("name", ""), "action": "care_newsfeed_reels", "status": "running", "start_time": start_time}
            self.logs.append(log_item)
            self.save_logs()
            self.after(0, lambda name=account.get("name", ""): self.append_live_log(f"Bắt đầu nuôi {name}."))

            with sync_playwright() as p:
                browser, context, page = self.create_browser_page(p, cookies, account)

                # GỌI HÀM KIỂM TRA ĐĂNG NHẬP Ở ĐÂY
                self.ensure_login(context, page, account)

                if settings["newsfeed_minutes"] > 0 and not self.is_task_stopped():
                    self.goto_facebook_home(page, account=account)
                    if self.interruptible_sleep(random.uniform(5, 8)):
                        self.scroll_page_for_minutes(page, settings["newsfeed_minutes"], settings["pause_range"], "newsfeed", account.get("name", ""), settings["auto_like"])

                if settings["reels_minutes"] > 0 and not self.is_task_stopped():
                    self.safe_goto(page, "https://www.facebook.com/reel/", account=account, fallback_urls=["https://facebook.com/reel/", "https://m.facebook.com/reel/"])
                    if self.interruptible_sleep(random.uniform(5, 8)):
                        self.scroll_page_for_minutes(page, settings["reels_minutes"], settings["pause_range"], "reels", account.get("name", ""), settings["auto_like"])

                log_item["status"] = "stopped" if self.is_task_stopped() else "done"
                log_item["end_time"] = datetime.now().strftime("%d/%m/%Y %H:%M")
                self.save_logs()
                browser.close()
                if self.is_task_stopped():
                    self.after(0, lambda name=account.get("name", ""): self.append_live_log(f"Đã dừng nuôi {name}."))
                else:
                    self.after(0, lambda name=account.get("name", ""): self.append_live_log(f"Hoàn tất nuôi {name}."))

        except Exception as e:
            self.logs.append({"account": account.get("name", ""), "status": "error", "error": str(e), "time": datetime.now().strftime("%d/%m/%Y %H:%M")})
            self.save_logs()
            self.after(0, lambda name=account.get("name", ""), err=e: self.append_live_log(f"Lỗi khi nuôi {name}: {err}"))

if __name__ == "__main__":
    app = FacebookCareTool()
    app.mainloop()
