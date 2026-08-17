# -*- coding: utf-8 -*-
"""卡战备自动配装 - 大厂风格三步向导界面。"""
import json
import os
import sys
import threading
import subprocess
import ctypes
from io import BytesIO
from tkinter import messagebox
import customtkinter as ctk
from PIL import Image
import api_client


__version__ = "4.5.0"

BASE_DIR = r"C:\dflppeizhuang"
SCRIPT = os.path.join(BASE_DIR, "maa_kazhanbei.py")
LOG_FILE = os.path.join(BASE_DIR, "maa_kazhanbei_log.txt")
UI_STATE_FILE = os.path.join(BASE_DIR, "kbe_ui_state.json")
SINGLE_INSTANCE_NAME = "Local\\KazhanbeiAutoEquipGui"

PYTHON = r"C:\Program Files\Python311\python.exe"
if not os.path.exists(PYTHON):
    import shutil
    found = shutil.which("python")
    PYTHON = found or sys.executable

LV_MAP = {0: "11W机密", 1: "18W机密", 2: "55W绝密巴克什",
          3: "60W绝密航天", 5: "78W绝密监狱"}
LV_DESC = {0: "11W 机密", 1: "18W 机密", 2: "55W 绝密巴克什",
           3: "60W 绝密航天", 5: "78W 绝密监狱"}
MODE_MAP = [("预览", "PREVIEW"), ("试跑", "DRY"), ("购买", "REAL")]

# ============ Orzice 鼠鼠卡战备 V4 风格(紫蓝渐变 + 深色卡片) ============
BG = "#16181D"          # 主背景(深蓝黑)
PANEL = "#1F2229"       # 导航/面板
PANEL2 = "#262A33"      # 卡片
PANEL3 = "#2F3440"      # hover
BORDER = "#3A3F4C"
TEXT = "#E8EAEF"
DIM = "#9AA3B2"
DIM2 = "#6B7280"
ACCENT = "#667EEA"      # V4 主色(紫蓝)
ACCENT_D = "#5A6FD6"    # 主色深
ACCENT_2 = "#F093FB"    # V4 强调(粉紫渐变端)
GRADIENT = "#A88CF0"    # 渐变中段(紫蓝→粉紫)
WARN = "#E8B465"
DANGER = "#FF5F5F"
OK = "#37D67A"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def _font(size, weight="normal"):
    return ctk.CTkFont(family="Segoe UI", size=size, weight=weight)


class KazhanbeiApp(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=BG)
        self.title("卡战备自动配装")
        self.geometry("1152x720")
        self.minsize(960, 600)

        self.proc = None
        self.tail_pos = 0
        self.groups = []
        self.group_details = {}
        self.categories = []
        self.current_category = None
        self.level_buttons = {}
        self.plan_buttons = []
        self.plan_page = 0
        self.category_page = 0
        self.plans_per_page = 4
        self.image_cache = {}
        self._last_level = None
        self.stderr_file = os.path.join(BASE_DIR, "kbe_app_stderr.log")
        self._err_handle = None
        self._pulse_id = None
        self._anim_jobs = {}
        self._pulse_on = False

        self._build()
        self._load_state()
        self._init_log()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._poll_log)

    # ---------- 基础组件 ----------
    def _label(self, parent, text, size=14, weight="normal", color=DIM,
               anchor="w"):
        return ctk.CTkLabel(parent, text=text, text_color=color,
                            font=_font(size, weight), anchor=anchor)

    def _button(self, parent, text, command, primary=False, width=0,
                height=44, **kwargs):
        font = kwargs.pop("font", _font(14, "bold"))
        return ctk.CTkButton(
            parent, text=text, command=command, height=height,
            width=width, corner_radius=14, border_width=1,
            border_color=BORDER,
            fg_color=ACCENT if primary else PANEL2,
            hover_color=GRADIENT if primary else PANEL3,
            text_color="#FFFFFF" if primary else TEXT,
            font=font, **kwargs)

    def _bind_click(self, widget, command):
        widget.bind("<Button-1>", lambda _event: command())
        for child in widget.winfo_children():
            self._bind_click(child, command)

    # ---------- 动画 ----------
    @staticmethod
    def _hex_to_rgb(color):
        color = str(color).lstrip("#")
        return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))

    @staticmethod
    def _mix_color(c1, c2, t):
        a, b = KazhanbeiApp._hex_to_rgb(c1), KazhanbeiApp._hex_to_rgb(c2)
        rgb = tuple(int(x + (y - x) * t) for x, y in zip(a, b))
        return "#%02x%02x%02x" % rgb

    def _animate_color(self, widget, attr, start, end, steps=10,
                       interval=16, on_done=None):
        """把 widget 的 attr 颜色从 start 平滑渐变到 end（帧动画）。

        同一 widget 同一 attr 的新动画会取消旧的，避免快速悬停时动画堆积。
        """
        if not self._alive(widget):
            return
        key = (id(widget), attr)
        existing = getattr(self, "_anim_jobs", {}).pop(key, None)
        if existing:
            try:
                widget.after_cancel(existing)
            except Exception:
                pass

        def _step(i):
            if not self._alive(widget):
                return
            t = (i + 1) / steps
            try:
                widget.configure(**{attr: self._mix_color(start, end, t)})
            except Exception:
                return
            if i + 1 < steps:
                job = widget.after(interval, lambda: _step(i + 1))
                self._anim_jobs[key] = job
            elif on_done is not None:
                on_done()

        _step(0)

    def _fade_in_widget(self, widget, delay=0):
        """入场动画：透明度模拟（背景色从深到面板色渐变）。"""
        if not self._alive(widget):
            return
        self.after(delay, lambda: self._animate_color(
            widget, "fg_color", PANEL3, PANEL, steps=14, interval=14))

    def _hover_border(self, widget, entering):
        """悬停边框平滑渐变（苹果风）。"""
        if entering:
            self._animate_color(widget, "border_color", BORDER, ACCENT_D,
                                steps=8, interval=12)
        else:
            self._animate_color(widget, "border_color", ACCENT_D, BORDER,
                                steps=10, interval=14)

    def _start_pulse(self):
        """运行中状态徽章呼吸闪烁。"""
        self._stop_pulse()
        self._pulse_on = False

        def _tick():
            if self.proc is None or not self._alive(self.status_badge):
                return
            self._pulse_on = not self._pulse_on
            self.status_badge.configure(
                text_color=WARN if self._pulse_on else "#FFD9A0")
            self._pulse_id = self.after(520, _tick)

        _tick()

    def _stop_pulse(self):
        if self._pulse_id is not None:
            try:
                self.after_cancel(self._pulse_id)
            except Exception:
                pass
            self._pulse_id = None

    def _pulse_gear(self, widget, delay=0):
        """装备/方案卡图片加载时边框轻微脉冲，表示内容更新。"""
        if not self._alive(widget):
            return
        self.after(delay, lambda: self._animate_color(
            widget, "border_color", BORDER, ACCENT, steps=8, interval=12,
            on_done=lambda: self._animate_color(
                widget, "border_color", ACCENT, BORDER, steps=8,
                interval=12)))

    def _card(self, parent, title, meta, height=300, image_height=170,
              image_url=None, image_urls=None, command=None):
        card = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=14,
                            border_width=1, border_color=BORDER, height=height)
        card.grid_propagate(False)
        card.grid_columnconfigure(0, weight=1)
        image_targets = []
        if image_urls:
            image_row = ctk.CTkFrame(card, fg_color=PANEL3, corner_radius=10,
                                     height=image_height)
            image_row.grid(row=0, column=0, sticky="ew",
                           padx=14, pady=(14, 8))
            image_row.grid_propagate(False)
            for index, url in enumerate(image_urls[:4]):
                image_row.grid_columnconfigure(index, weight=1)
                label = ctk.CTkLabel(
                    image_row, text="图片", fg_color=PANEL3, corner_radius=14,
                    text_color=DIM2, font=_font(11))
                label.grid(row=0, column=index, sticky="nsew",
                           padx=3, pady=3)
                if url:
                    image_targets.append((label, url))
            if len(image_urls) > 4:
                ctk.CTkLabel(image_row, text=f"+{len(image_urls) - 4}",
                             text_color=DIM2, font=_font(12, "bold")).grid(
                    row=0, column=4, padx=3)
        elif image_url:
            image_label = ctk.CTkLabel(
                card, text="图片加载中", height=image_height, fg_color=PANEL3,
                corner_radius=12, text_color=DIM2, font=_font(12))
            image_label.grid(row=0, column=0, sticky="ew",
                             padx=14, pady=(14, 8))
            image_targets.append((image_label, image_url))
        else:
            ctk.CTkLabel(card, text="暂无图片", height=image_height,
                         fg_color=PANEL3, corner_radius=12,
                         text_color=DIM2, font=_font(12)).grid(
                row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
        title_label = self._label(card, title, size=17, weight="bold",
                                  color=TEXT)
        title_label.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 2))
        meta_label = self._label(card, meta, size=13, color=DIM)
        meta_label.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 14))
        if command:
            self._bind_click(card, command)
            card.configure(cursor="hand2")
            # 悬停高亮边框（苹果风微交互，平滑渐变）
            card.bind("<Enter>",
                      lambda _event, c=card: self._hover_border(c, True))
            card.bind("<Leave>",
                      lambda _event, c=card: self._hover_border(c, False))
        return card, image_targets

    # ---------- 顶层布局 ----------
    def _build(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_nav()
        self.pages = []
        self._build_level_page()
        self._build_plan_page()
        self._build_console_page()
        self.show_page(0)

    def _build_nav(self):
        """MAA 风格左侧导航栏：品牌 + 导航项 + 底部状态。"""
        nav = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0,
                           width=200, border_width=0)
        nav.grid(row=0, column=0, sticky="nsw")
        nav.grid_propagate(False)
        nav.grid_rowconfigure(2, weight=1)

        # 品牌区
        brand = ctk.CTkFrame(nav, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=18, pady=(22, 26))
        logo = ctk.CTkFrame(brand, width=34, height=34, fg_color=ACCENT,
                            corner_radius=10)
        logo.grid(row=0, column=0, padx=(0, 10))
        logo.grid_propagate(False)
        ctk.CTkLabel(logo, text="◆", text_color="#FFFFFF",
                     font=_font(15, "bold")).place(relx=0.5, rely=0.5,
                                                   anchor="center")
        ctk.CTkLabel(brand, text="卡战备自动配装", text_color=TEXT,
                     font=_font(15, "bold")).grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(brand, text="DELTA FORCE LOADOUT", text_color=DIM2,
                     font=_font(10)).grid(row=1, column=1, sticky="w")

        # 导航项(图标 + 文字 + 选中高亮)
        self.nav_items = []
        nav_specs = [("◈", "战备价值"), ("▤", "配装方案"), ("❯", "控制台")]
        for index, (icon, text) in enumerate(nav_specs):
            item = ctk.CTkFrame(nav, fg_color="transparent", height=44,
                                corner_radius=10, cursor="hand2")
            item.grid(row=index + 1, column=0, sticky="ew",
                      padx=10, pady=3)
            item.grid_propagate(False)
            item.grid_columnconfigure(1, weight=1)
            item.bind("<Button-1>",
                      lambda _e, i=index: self.show_page(i))
            icon_lb = ctk.CTkLabel(item, text=icon, width=32,
                                   text_color=DIM2, font=_font(15))
            icon_lb.grid(row=0, column=0, padx=(12, 4))
            name_lb = ctk.CTkLabel(item, text=text, text_color=DIM2,
                                   font=_font(13, "bold"), anchor="w")
            name_lb.grid(row=0, column=1, sticky="w")
            bar = ctk.CTkFrame(item, width=3, fg_color="transparent",
                               corner_radius=0)
            bar.grid(row=0, column=0, sticky="ns", padx=(0, 8))
            self.nav_items.append((item, icon_lb, name_lb, bar))

        # 底部设置区：运行模式(集成在边栏, MAA 风格)
        set_box = ctk.CTkFrame(nav, fg_color="transparent")
        set_box.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 8))
        set_box.grid_columnconfigure(0, weight=1)
        mode_row = ctk.CTkFrame(set_box, fg_color="transparent")
        mode_row.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        mode_row.grid_columnconfigure(0, weight=1)
        self._label(mode_row, "运行模式", size=11, weight="bold",
                    color=DIM2).grid(row=0, column=0, sticky="w")
        self.mode_var = ctk.StringVar(value="DRY")
        self.mode_seg = ctk.CTkSegmentedButton(
            set_box, values=[label for label, _ in MODE_MAP],
            command=self._on_mode_picked, corner_radius=6,
            fg_color=PANEL2, selected_color=ACCENT,
            selected_hover_color=ACCENT_D, text_color=TEXT,
            font=_font(11, "bold"), height=30)
        self.mode_seg.grid(row=1, column=0, sticky="ew")
        self.mode_hint = self._label(set_box, "真实导航，购买前停止",
                                     size=10, color=WARN)
        self.mode_hint.grid(row=2, column=0, sticky="ew", pady=(4, 0))

        # 胸挂背包使用仓库的开关
        wh_row = ctk.CTkFrame(set_box, fg_color="transparent")
        wh_row.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        wh_row.grid_columnconfigure(0, weight=1)
        self._label(wh_row, "胸挂背包用仓库的", size=11, weight="bold",
                    color=DIM2).grid(row=0, column=0, sticky="w")
        self.warehouse_var = ctk.BooleanVar(value=False)
        self.warehouse_switch = ctk.CTkSwitch(
            wh_row, text="", variable=self.warehouse_var, width=44,
            progress_color=ACCENT, fg_color=PANEL3,
            button_color=TEXT, button_hover_color=DIM)
        self.warehouse_switch.grid(row=0, column=1, sticky="e")

        # 底部状态徽章
        self.status_badge = ctk.CTkLabel(
            nav, text="● 空闲", fg_color=PANEL2, text_color=DIM2,
            corner_radius=10, font=_font(11, "bold"), height=32)
        self.status_badge.grid(row=4, column=0, sticky="ew",
                               padx=12, pady=(0, 14))

    def _page_header(self, parent, eyebrow, title, subtitle, back=None):
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=56, pady=(40, 24))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text=eyebrow, text_color=ACCENT,
                     font=_font(13, "bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header, text=title, text_color=TEXT,
                     font=_font(24, "bold")).grid(row=1, column=0, sticky="w")
        ctk.CTkLabel(header, text=subtitle, text_color=DIM2,
                     font=_font(14)).grid(row=2, column=0, sticky="w",
                                          pady=(4, 0))
        if back is not None:
            self._button(header, "‹ 上一步", back, width=110, height=40,
                         font=_font(13, "bold")).grid(
                row=0, column=1, rowspan=3, sticky="e", padx=(24, 0))

    def show_page(self, index):
        for i, page in enumerate(self.pages):
            page.grid() if i == index else page.grid_remove()
        for i, (item, icon_lb, name_lb, bar) in enumerate(self.nav_items):
            active = i == index
            item.configure(fg_color=PANEL3 if active else "transparent")
            icon_lb.configure(text_color=TEXT if active else DIM2)
            name_lb.configure(text_color=TEXT if active else DIM2)
            bar.configure(fg_color=ACCENT if active else "transparent")

    # ---------- 第 1 页 ----------
    def _build_level_page(self):
        page = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        page.grid(row=0, column=1, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        self._page_header(page, "STEP 01", "选择战备价值",
                          "点击一张卡片，进入对应的配装方案")
        grid = ctk.CTkFrame(page, fg_color="transparent")
        grid.grid(row=1, column=0, sticky="nsew", padx=56, pady=(8, 40))
        grid.grid_columnconfigure((0, 1), weight=1)
        self.lv_var = ctk.IntVar(value=0)
        for i, (level, name) in enumerate(LV_MAP.items()):
            card = ctk.CTkFrame(grid, fg_color=PANEL, corner_radius=14,
                                border_width=1, border_color=BORDER,
                                height=168)
            card.grid_propagate(False)
            card.grid(row=i // 2, column=i % 2, sticky="nsew", padx=10, pady=10)
            card.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(card, text=name, text_color=TEXT,
                         font=_font(19, "bold")).grid(
                row=0, column=0, sticky="w", padx=22, pady=(24, 4))
            ctk.CTkLabel(card, text=LV_DESC[level], text_color=DIM,
                         font=_font(13)).grid(
                row=1, column=0, sticky="w", padx=22)
            ctk.CTkLabel(card, text="进入方案  ›", text_color=ACCENT,
                         font=_font(13, "bold")).grid(
                row=2, column=0, sticky="w", padx=22, pady=(14, 0))
            self._bind_click(
                card,
                lambda value=level: self._on_level_changed(value, advance=True))
            card.configure(cursor="hand2")
            card.bind("<Enter>",
                      lambda _event, c=card: self._hover_border(c, True))
            card.bind("<Leave>",
                      lambda _event, c=card: self._hover_border(c, False))
            # 错峰入场动画
            self._fade_in_widget(card, delay=i * 60)
            self.level_buttons[level] = card
        self.pages.append(page)

    # ---------- 第 2 页 ----------
    def _build_plan_page(self):
        page = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        page.grid(row=0, column=1, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        self._page_header(page, "STEP 02", "选择配装方案",
                          "先选类别，再选具体套装；点卡片进入控制台",
                          back=self._back_to_level)
        self.group_var = ctk.StringVar(value="请先选择战备价值")
        self.plan_list = ctk.CTkScrollableFrame(
            page, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=ACCENT_D)
        self.plan_list.grid(row=1, column=0, sticky="nsew",
                            padx=56, pady=(8, 14))
        self.plan_list.grid_columnconfigure((0, 1), weight=1)
        pager = ctk.CTkFrame(page, fg_color="transparent")
        pager.grid(row=2, column=0, sticky="ew", padx=56, pady=(0, 34))
        pager.grid_columnconfigure(2, weight=1)
        self.btn_back_category = self._button(
            pager, "返回类别", self.show_categories, width=110, height=38)
        self.btn_back_category.grid(row=0, column=0, padx=(0, 12))
        self.btn_back_category.grid_remove()
        self.btn_prev = self._button(pager, "‹", self.prev_page,
                                     width=42, height=38, state="disabled")
        self.btn_prev.grid(row=0, column=1)
        self.plan_page_label = ctk.CTkLabel(
            pager, text="", text_color=DIM2, font=_font(13))
        self.plan_page_label.grid(row=0, column=2, sticky="ew")
        self.btn_next = self._button(pager, "›", self.next_page,
                                     width=42, height=38, state="disabled")
        self.btn_next.grid(row=0, column=3)
        self.pages.append(page)

    def _back_to_level(self):
        self.show_page(0)

    # ---------- 第 3 页 ----------
    def _build_console_page(self):
        page = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        page.grid(row=0, column=1, sticky="nsew")
        page.grid_columnconfigure(0, weight=3)
        page.grid_columnconfigure(1, weight=2)
        page.grid_rowconfigure(2, weight=1)
        self._page_header(page, "STEP 03", "配装控制台",
                          "确认方案后开始配装，可在下方查看装备与日志",
                          back=self._back_to_plans)

        summary = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=14,
                               border_width=1, border_color=BORDER)
        summary.grid(row=1, column=0, sticky="nsew", padx=(56, 7), pady=(0, 14))
        summary.grid_columnconfigure(0, weight=1)
        self.top_task = self._label(summary, "未选择方案", size=18,
                                    weight="bold", color=TEXT)
        self.top_task.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 3))
        self.foot_detail = self._label(summary, "", size=13)
        self.foot_detail.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 3))
        self.foot_mode = self._label(summary, "DRY · 试跑", size=13,
                                     weight="bold", color=WARN)
        self.foot_mode.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 16))

        controls = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=14,
                                border_width=1, border_color=BORDER)
        controls.grid(row=1, column=1, sticky="nsew", padx=(7, 56), pady=(0, 14))
        controls.grid_columnconfigure(0, weight=1)
        self._label(controls, "配装操作", size=14, weight="bold",
                    color=TEXT).grid(row=0, column=0, sticky="w",
                                     padx=18, pady=(16, 8))
        mode_hint_line = self._label(
            controls, "运行模式在左侧边栏底部选择", size=12, color=DIM2)
        mode_hint_line.grid(row=1, column=0, sticky="ew",
                            padx=18, pady=(0, 12))
        actions = ctk.CTkFrame(controls, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 16))
        actions.grid_columnconfigure((0, 1), weight=1)
        self.btn_run = self._button(actions, "开始配装", self.start_run,
                                    primary=True, height=48)
        self.btn_run.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.btn_stop = self._button(actions, "停止", self.stop_run,
                                     height=48, state="disabled")
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        self.detail_frame = ctk.CTkFrame(page, fg_color=PANEL,
                                         corner_radius=14,
                                         border_width=1, border_color=BORDER)
        self.detail_frame.grid(row=2, column=0, columnspan=2, sticky="nsew",
                               padx=56, pady=(0, 14))
        self.detail_frame.grid_columnconfigure(0, weight=1)
        head = ctk.CTkFrame(self.detail_frame, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 6))
        head.grid_columnconfigure(0, weight=1)
        self.detail_title = self._label(head, "装备清单", size=18,
                                        weight="bold", color=TEXT)
        self.detail_title.grid(row=0, column=0, sticky="w")
        self.detail_total = self._label(head, "等待选择方案", size=13)
        self.detail_total.grid(row=0, column=1, sticky="e")
        self.gear_list = ctk.CTkScrollableFrame(
            self.detail_frame, fg_color=PANEL2, corner_radius=12,
            scrollbar_button_color=BORDER,
            scrollbar_button_hover_color=ACCENT_D)
        self.gear_list.grid(row=1, column=0, sticky="nsew",
                            padx=14, pady=(0, 14))
        self.gear_list.grid_columnconfigure((0, 1), weight=1)

        log_frame = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=12,
                                 border_width=1, border_color=BORDER)
        log_frame.grid(row=3, column=0, columnspan=2, sticky="ew",
                       padx=56, pady=(0, 20))
        log_frame.grid_columnconfigure(0, weight=1)
        self.btn_clear = self._button(log_frame, "清空日志", self.clear_log,
                                      width=88, height=30, font=_font(12))
        self.btn_clear.grid(row=0, column=1, sticky="ne", padx=12, pady=10)
        self.log_box = ctk.CTkTextbox(
            log_frame, wrap="word", fg_color=PANEL2, border_color=BORDER,
            border_width=1, text_color="#D7DADE", corner_radius=14,
            height=116, font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        self.log_box.configure(state="disabled")
        for tag, color in [("warn", WARN), ("err", DANGER), ("ok", OK),
                           ("info", ACCENT), ("dim", DIM2)]:
            self.log_box.tag_config(tag, foreground=color)
        self.pages.append(page)

    def _back_to_plans(self):
        self.show_page(1)

    # ---------- 页面内容 ----------
    def _clear_plans(self):
        for widget in self.plan_list.winfo_children():
            widget.destroy()
        self.plan_buttons = []

    def _clear_gear(self):
        for widget in self.gear_list.winfo_children():
            widget.destroy()

    def _show_plan_empty(self, text):
        self._clear_plans()
        self._label(self.plan_list, text, size=16, color=DIM2,
                    anchor="center").grid(
            row=0, column=0, columnspan=2, sticky="nsew", pady=40)

    def show_categories(self):
        self.current_category = None
        self.category_page = 0
        self._render_plan_area()

    def open_category(self, category):
        self.current_category = category
        self.plan_page = 0
        self._render_plan_area()

    def prev_page(self):
        if self.current_category is None:
            self.category_page = max(0, self.category_page - 1)
        else:
            self.plan_page = max(0, self.plan_page - 1)
        self._render_plan_area()

    def next_page(self):
        if self.current_category is None:
            self.category_page += 1
        else:
            self.plan_page += 1
        self._render_plan_area()

    def _render_plan_area(self):
        self._clear_plans()
        if self.current_category is None:
            self._render_categories()
            return
        self._render_plans()

    def _render_categories(self):
        if not self.categories:
            self._show_plan_empty("该档位暂无方案")
            return
        total = max(1, (len(self.categories) + self.plans_per_page - 1) //
                    self.plans_per_page)
        self.category_page = min(max(0, self.category_page), total - 1)
        start = self.category_page * self.plans_per_page
        page_categories = self.categories[start:start + self.plans_per_page]
        for i, category in enumerate(page_categories):
            desc = category.get("desc", "") or ""
            card = ctk.CTkFrame(self.plan_list, fg_color=PANEL,
                                corner_radius=14, border_width=1,
                                border_color=BORDER, height=190)
            card.grid_propagate(False)
            card.grid(row=i // 2, column=i % 2, sticky="nsew",
                      padx=8, pady=8)
            card.grid_columnconfigure(0, weight=1)
            self._label(card, category["name"], size=18, weight="bold",
                        color=TEXT).grid(row=0, column=0, sticky="w",
                                         padx=20, pady=(22, 4))
            self._label(card, f"{len(category['keys'])} 个方案", size=13,
                        color=DIM).grid(row=1, column=0, sticky="w", padx=20)
            self._label(card, desc[:20], size=12, color=DIM2).grid(
                row=2, column=0, sticky="w", padx=20, pady=(10, 0))
            self._bind_click(card, lambda c=category: self.open_category(c))
            card.configure(cursor="hand2")
            card.bind("<Enter>",
                      lambda _event, c=card: self._hover_border(c, True))
            card.bind("<Leave>",
                      lambda _event, c=card: self._hover_border(c, False))
            self._fade_in_widget(card, delay=i * 60)
            self.plan_buttons.append((category["name"], card))
        self._update_pager(start, len(page_categories),
                           len(self.categories), "类别")
        self.btn_back_category.grid_remove()

    def _render_plans(self):
        keys = self.current_category["keys"]
        if not keys:
            self._show_plan_empty("该类别暂无方案")
            return
        total = max(1, (len(keys) + self.plans_per_page - 1) //
                    self.plans_per_page)
        self.plan_page = min(max(0, self.plan_page), total - 1)
        start = self.plan_page * self.plans_per_page
        page_keys = keys[start:start + self.plans_per_page]
        for i, key in enumerate(page_keys):
            plan = self.group_details[key]
            items = plan.get("data", []) or []
            main_pics = [
                item.get("pic") for item in items
                if item.get("pic") and not str(item.get("type", "")).startswith(
                    ("枪1-", "枪2-"))
            ]
            title = plan.get("name") or self.current_category["name"]
            meta = (f"{len(items)} 件 · ¥{plan.get('price', 0):,}"
                    f" · 战备 {plan.get('jz', 0):,}")
            card, image_targets = self._card(
                self.plan_list, title[:16], meta, height=320,
                image_height=200, image_urls=main_pics[:4],
                command=lambda selected=key: self.on_group_changed(selected))
            card.grid(row=i // 2, column=i % 2, sticky="nsew",
                      padx=8, pady=8)
            self.plan_buttons.append((key, card))
            self._load_images(image_targets, (190, 110))
            self._pulse_gear(card, delay=200 + i * 100)
        self._update_pager(start, len(page_keys), len(keys), "方案")
        self.btn_back_category.grid()

    def _update_pager(self, start, page_count, total, kind):
        self.plan_page_label.configure(
            text=f"{kind} {start + 1}-{start + page_count} / {total}")
        self.btn_prev.configure(
            state="normal" if (self.category_page > 0 if
                               self.current_category is None else
                               self.plan_page > 0) else "disabled")
        max_page = (max(1, (total + self.plans_per_page - 1) //
                        self.plans_per_page) - 1)
        current = self.category_page if self.current_category is None \
            else self.plan_page
        self.btn_next.configure(
            state="normal" if current < max_page else "disabled")

    # ---------- 档位与方案数据 ----------
    def _on_level_changed(self, choice, advance=True):
        level = int(choice)
        self.lv_var.set(level)
        self._paint_levels()
        if self._last_level == level and self.groups:
            if advance:
                self.show_page(1)
            return
        self.groups = []
        self.group_details = {}
        self.categories = []
        self.current_category = None
        self.group_var.set("正在获取方案...")
        self._show_plan_empty("正在获取对应方案...")
        self._set_status("加载中", WARN)
        self.after(80, self._load_groups)
        if advance:
            self.show_page(1)

    def _paint_levels(self):
        selected = self.lv_var.get()
        for level, card in self.level_buttons.items():
            active = level == selected
            card.configure(border_color=ACCENT if active else BORDER,
                           fg_color=PANEL if active else PANEL)

    def _load_groups(self):
        threading.Thread(target=self._fetch_worker,
                         args=(self.lv_var.get(),), daemon=True).start()

    def _fetch_worker(self, level):
        try:
            token = self._token()
            if not token:
                raise RuntimeError("无法提取完整 TOKEN")
            data, err = api_client.fetch_loadout(level, token, retries=3)
            if err:
                raise RuntimeError(err)
            categories = []
            details = {}
            for group in data:
                plans = group.get("list", []) or []
                category = {"name": group.get("name") or "其他方案",
                            "desc": group.get("desc") or "", "keys": []}
                for position, plan in enumerate(plans):
                    plan["_group_name"] = category["name"]
                    plan["_plan_position"] = position
                    name = plan.get("name") or category["name"]
                    key = f"{category['name']}::{name}::{position}"
                    details[key] = plan
                    category["keys"].append(key)
                if category["keys"]:
                    categories.append(category)
            self.after(0, lambda: self._apply_groups(categories, details))
        except Exception as exc:
            self.after(0, lambda: self._apply_groups([], {}, str(exc)))

    def _apply_groups(self, categories, details, err=None):
        self.categories = categories
        self.group_details = details
        self.groups = [key for cat in categories for key in cat["keys"]]
        self.current_category = None
        self._last_level = self.lv_var.get()
        if not self.groups:
            self._show_plan_empty(
                f"拉取失败: {err}" if err else "该档位暂无方案")
            self._set_status("错误" if err else "无方案",
                             DANGER if err else DIM2)
            return
        self.show_categories()
        self._set_status("就绪", OK)
        self._append("[GUI] 拉到 %d 个方案组\n" % len(self.categories), "ok")

    def on_group_changed(self, key):
        if self.proc is not None:
            return
        self.group_var.set(key)
        self._show_group_summary(key)
        for candidate, card in self.plan_buttons:
            card.configure(border_color=ACCENT if candidate == key else BORDER)
        self.show_page(2)

    def _show_group_summary(self, key):
        plan = self.group_details.get(key)
        if plan is None:
            return
        items = plan.get("data", []) or []
        grades = {}
        for item in items:
            grade = item.get("grade", "?")
            grades[grade] = grades.get(grade, 0) + 1
        grade_text = " ".join(f"档{g}×{count}"
                              for g, count in sorted(grades.items()))
        self.top_task.configure(
            text=f"{LV_MAP[self.lv_var.get()]} | {plan.get('name', '未命名')}")
        self.foot_detail.configure(
            text=f"方案装备 {len(items)} 件 | {grade_text}")
        self.detail_title.configure(text=plan.get("name", "装备清单"))
        self.detail_total.configure(
            text=f"{len(items)} 件 | ¥{plan.get('price', 0):,}"
                 f" | 战备 {plan.get('jz', 0):,}")
        self._clear_gear()
        targets = []
        for i, item in enumerate(items):
            meta = (f"{item.get('type', '未分类')} · 档{item.get('grade', '?')}"
                    f" · ¥{item.get('price', 0):,}"
                    f" · 战备 {item.get('jz', 0):,}")
            card, image_targets = self._card(
                self.gear_list, item.get("name", "未知装备"), meta,
                height=290, image_height=190, image_url=item.get("pic"))
            card.grid(row=i // 2, column=i % 2, sticky="nsew",
                      padx=8, pady=8)
            self._fade_in_widget(card, delay=i * 40)
            self._pulse_gear(card, delay=300 + i * 80)
            targets.extend(image_targets)
        self._load_images(targets, (380, 190))

    # ---------- 图片 ----------
    def _alive(self, widget):
        try:
            return bool(widget.winfo_exists())
        except Exception:
            return False

    def _load_images(self, targets, size):
        targets = [(label, url) for label, url in targets if url]
        if targets:
            threading.Thread(target=self._image_worker,
                             args=(targets, size), daemon=True).start()

    def _image_worker(self, targets, size):
        for label, url in targets:
            try:
                source = self._get_image(url)
                thumb = source.copy()
                thumb.thumbnail(size, Image.Resampling.LANCZOS)
                image = ctk.CTkImage(light_image=thumb, dark_image=thumb,
                                     size=thumb.size)
                self.after(0, lambda target=label, img=image:
                           self._set_image(target, img))
            except Exception:
                continue

    def _get_image(self, url):
        if url not in self.image_cache:
            content, err = api_client.download_bytes(url, timeout=12)
            if err or not content:
                raise RuntimeError(err or "empty image")
            source = Image.open(BytesIO(content)).convert("RGBA")
            self.image_cache[url] = source.copy()
        return self.image_cache[url]

    def _set_image(self, label, image):
        if self._alive(label):
            label.configure(text="", image=image)
            label.image = image

    # ---------- 日志 ----------
    def _init_log(self):
        if os.path.exists(LOG_FILE):
            self.tail_pos = os.path.getsize(LOG_FILE)

    def _append(self, text, tag=None):
        self.log_box.configure(state="normal")
        lines = text.split("\n")
        import re
        for line in lines:
            if not line:
                continue
            # 实时进度：解析后端日志的 [N/M] 购买进度，更新状态徽章
            prog = re.search(r"\[(\d+)/(\d+)\]", line)
            if prog:
                self.status_badge.configure(
                    text=f"● 运行中 {prog.group(1)}/{prog.group(2)}")
            if not tag:
                low = line.lower()
                if "失败" in line or "错误" in line or "异常" in line:
                    tag = "err"
                elif "成功" in line or "完成" in line or "就绪" in line:
                    tag = "ok"
                elif "无法" in line or "警告" in line:
                    tag = "warn"
                elif low.startswith("[gui]"):
                    tag = "info"
                else:
                    tag = "dim"
            self.log_box.insert("end", line + "\n", tag)
        # 防长时间运行卡顿：超过 MAX_LOG_LINES 行时截断前半段。
        if int(self.log_box.index("end-1c").split(".")[0]) > 3000:
            self.log_box.delete("1.0", "1500.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self._append("[GUI] 日志已清空\n", "info")

    def _poll_log(self):
        if self.proc is not None:
            try:
                size = os.path.getsize(LOG_FILE)
                if size > self.tail_pos:
                    with open(LOG_FILE, "r", encoding="utf-8",
                              errors="replace") as f:
                        f.seek(self.tail_pos)
                        chunk = f.read()
                    self.tail_pos = size
                    if chunk:
                        self._append(chunk)
            except Exception:
                pass
            if self.proc.poll() is not None:
                self._on_run_finished(self.proc.returncode)
                return
        self.after(250, self._poll_log)

    # ---------- 运行 ----------
    def start_run(self):
        if self.proc is not None:
            self._append("[GUI] 已有任务在运行\n", "warn")
            return
        selected = self.group_var.get()
        if selected not in self.group_details:
            self._append("[GUI] 请先选择有效方案\n", "warn")
            self._set_status("待选择", WARN)
            return
        plan = self.group_details[selected]
        level = self.lv_var.get()
        group = plan.get("_group_name", "").strip()
        plan_index = int(plan.get("_plan_position", 0))
        mode = self.mode_var.get()
        if mode == "REAL" and not messagebox.askyesno(
                "确认真实购买",
                f"即将按档位 {level}、方案“{group}”执行真实购买。\n\n"
                "此操作会消耗游戏币，是否继续？", icon="warning"):
            return
        env = dict(os.environ)
        env.pop("PREVIEW_MODE", None)
        env.pop("DRY_RUN_API", None)
        if mode == "PREVIEW":
            env["PREVIEW_MODE"] = "1"
            label = "预览"
        elif mode == "DRY":
            env["DRY_RUN_API"] = "1"
            label = "试跑"
        else:
            label = "真实购买"
        if self.warehouse_var.get():
            env["USE_WAREHOUSE_RIG_BAG"] = "1"
        self.tail_pos = os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0
        # ⚠️ 启动前校验 TOKEN：残缺 token 会导致整轮 API 白跑，先拦下。
        if not self._token():
            self._append("[GUI] 无法启动：API TOKEN 不完整，请检查 maa_kazhanbei.py\\n",
                         "err")
            self._set_status("TOKEN 异常", DANGER)
            return
        self.foot_mode.configure(text=f"{label} · {group}")
        self.top_task.configure(text=f"{label} | 档位{level} | {group}"[:44])
        self._set_status("运行中", WARN)
        self._start_pulse()
        self.btn_run.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.mode_seg.configure(state="disabled")
        self._save_state()
        self._err_handle = open(self.stderr_file, "w", encoding="utf-8",
                                errors="replace")
        cmd = [PYTHON, SCRIPT, str(level), group, str(plan_index)]
        self.proc = subprocess.Popen(
            cmd, env=env, cwd=BASE_DIR,
            stdout=subprocess.DEVNULL, stderr=self._err_handle,
            creationflags=0x08000000)

    def stop_run(self):
        if self.proc is not None:
            self.proc.terminate()
            self._append("[GUI] 已请求停止\n", "warn")
            self._stop_pulse()
            self._set_status("已停止", DANGER)
            self._close_err_handle()

    def _close_err_handle(self):
        if getattr(self, "_err_handle", None):
            try:
                self._err_handle.close()
            except Exception:
                pass
            self._err_handle = None

    def _on_run_finished(self, rc):
        self._close_err_handle()
        error = ""
        try:
            if os.path.exists(self.stderr_file):
                with open(self.stderr_file, "r", encoding="utf-8",
                          errors="replace") as f:
                    error = f.read().strip()[-600:]
        except Exception:
            pass
        self._append(f"[GUI] 配装任务结束 (退出码 {rc})\n", "info")
        self._stop_pulse()
        if rc != 0 and error:
            self._append(f"[GUI] ⚠️ 错误输出:\n{error}\n", "err")
            self._set_status("异常退出", DANGER)
        else:
            self._set_status("已完成" if rc == 0 else "已结束",
                             OK if rc == 0 else DIM2)
        self.proc = None
        self.btn_run.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.mode_seg.configure(state="normal")

    def _set_status(self, text, color):
        self.status_badge.configure(text=f"● {text}", text_color=color)

    def _on_mode_picked(self, label):
        mode = dict((name, key) for name, key in MODE_MAP)[label]
        self.mode_var.set(mode)
        hints = {"PREVIEW": ("只识别页面，不执行导航或购买", DIM2),
                 "DRY": ("真实导航，购买前停止", WARN),
                 "REAL": ("会消耗游戏币，启动时需确认", DANGER)}
        text, color = hints[mode]
        self.mode_hint.configure(text=text, text_color=color)
        self.foot_mode.configure(
            text={"PREVIEW": "预览", "DRY": "试跑",
                  "REAL": "真实购买"}[mode], text_color=color)
        self._save_state()

    # ---------- 状态 ----------
    def _load_state(self):
        try:
            with open(UI_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            level = state.get("level")
            if level in LV_MAP:
                self.lv_var.set(level)
            mode = state.get("mode")
            if mode:
                self.mode_var.set(mode)
                label = next((name for name, key in MODE_MAP
                              if key == mode), "试跑")
                self.mode_seg.set(label)
            if state.get("warehouse"):
                self.warehouse_var.set(True)
        except (OSError, ValueError, TypeError):
            pass
        self._paint_levels()
        self._on_mode_picked(next(name for name, key in MODE_MAP
                                  if key == self.mode_var.get()))
        self.after(150, lambda: self._on_level_changed(self.lv_var.get(),
                                                       advance=False))

    def _save_state(self):
        try:
            with open(UI_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"level": self.lv_var.get(),
                           "mode": self.mode_var.get(),
                           "warehouse": self.warehouse_var.get()},
                          f, ensure_ascii=False, indent=2)
        except (OSError, ValueError):
            pass

    def _token(self):
        try:
            import re
            with open(SCRIPT, encoding="utf-8") as f:
                match = re.search(r'TOKEN\s*=\s*"([^"]+)"', f.read())
            if match and len(match.group(1)) >= 32:
                return match.group(1)
        except Exception:
            pass
        self._append("[GUI] ⚠️ 无法从 maa_kazhanbei.py 提取完整 TOKEN\n",
                     "warn")
        return None

    def on_close(self):
        self._save_state()
        if self.proc is not None:
            self.proc.terminate()
        self.destroy()


def acquire_single_instance():
    handle = ctypes.windll.kernel32.CreateMutexW(None, False,
                                                  SINGLE_INSTANCE_NAME)
    if not handle or ctypes.windll.kernel32.GetLastError() == 183:
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
        return None
    return handle


if __name__ == "__main__":
    instance_handle = acquire_single_instance()
    if instance_handle is None:
        ctypes.windll.user32.MessageBoxW(
            None, "卡战备自动配装已在运行。", "无法重复启动", 0x40)
        raise SystemExit(0)
    try:
        app = KazhanbeiApp()
        app.mainloop()
    finally:
        ctypes.windll.kernel32.CloseHandle(instance_handle)
