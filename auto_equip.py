# -*- coding: utf-8 -*-
"""
三角洲行动 · 自动配装（固定坐标版）v1.0
- 从 orzice.com 拉取配装方案
- 固定坐标点击军需处/武器分类/滚动条/购买/返回
- easyocr 识别装备列表
用法:
    python auto_equip.py [档位] [分组名|-) [方案序号]
    例: python auto_equip.py            # 默认 11W 档第一个分组第一个方案
        python auto_equip.py 1 花费最少-单枪 0
环境变量:
    PREVIEW_MODE=1  只移动鼠标不点击（试跑用）
    MAX_ATTEMPTS=N  列表滚动找装备的最大次数（默认20）
"""
import json
import time
import os
import sys
import cv2
import api_client

# Windows 中文环境 stdout 编码保护（避免管道/重定向时 GBK 编码报错）
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
import numpy as np
import win32gui
import win32ui
import win32con
import win32api
import ctypes
from ctypes import wintypes
from ctypes import windll

# 高 DPI 感知（否则屏幕截图坐标会偏移）
try:
    windll.user32.SetProcessDPIAware()
except Exception:
    pass

# ==================== 配置路径 ====================
BASE_DIR = r"C:\dflppeizhuang"
CONFIG_FILE = os.path.join(BASE_DIR, "maa_config.json")
COORD_FILE = os.path.join(BASE_DIR, "config_abs.json")
TOKEN = os.environ.get("KZB_TOKEN", "")  # ⚠️ 填入你自己的 token 或设置环境变量 KZB_TOKEN
LOADOUT_URL = "https://orzice.com/workApi/v1/sjz_api/jzv4_zb"
MAX_PRICE = 600000
GAME_TITLE = "三角洲行动"
PREVIEW_MODE = os.environ.get("PREVIEW_MODE", "0") == "1"
PREVIEW_DELAY = float(os.environ.get("PREVIEW_DELAY", "0"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "20"))
LV_MAP = {0: "11W", 1: "18W", 2: "55W", 3: "60W", 5: "78W"}
# =================================================

if not os.path.exists(COORD_FILE):
    print(f"错误：坐标文件 {COORD_FILE} 未找到")
    sys.exit(1)
with open(COORD_FILE, "r", encoding="utf-8") as f:
    _coord_data = json.load(f)
SLOTS = _coord_data["slots"]
CALIB = _coord_data.get("calib", {})
CALIB_W = int(CALIB.get("w", 2560))
CALIB_H = int(CALIB.get("h", 1440))

# ==================== 坐标缩放（校准分辨率 -> 当前窗口） ====================
SCALE_X = 1.0
SCALE_Y = 1.0
WIN_LEFT = 0
WIN_TOP = 0

def setup_scale(width, height, win_left, win_top):
    """根据当前窗口尺寸计算坐标缩放"""
    global SCALE_X, SCALE_Y, WIN_LEFT, WIN_TOP
    WIN_LEFT, WIN_TOP = win_left, win_top
    SCALE_X = width / CALIB_W
    SCALE_Y = height / CALIB_H
    print(f"[缩放] 校准 {CALIB_W}x{CALIB_H} -> 当前 {width}x{height} @({win_left},{win_top}) (x{SCALE_X:.3f}, y{SCALE_Y:.3f})")

# ==================== API 数据层（合并自 main.py） ====================
def get_loadout_data(lv: int = 0):
    """获取指定档位的配装数据（原始分组列表）"""
    groups, err = api_client.fetch_loadout(lv, TOKEN)
    if err:
        print(f"⚠️ {err}")
        return []
    return groups

def get_loadout(lv: int = 0, group_name=None, plan_index: int = 0):
    """快速获取单个配装方案"""
    groups = get_loadout_data(lv)
    if not groups:
        print("无配装数据")
        return None

    target_group = None
    if group_name is None:
        target_group = groups[0]
    else:
        for g in groups:
            if g.get("name") == group_name:
                target_group = g
                break
    if target_group is None:
        print(f"未找到分组: {group_name}，可用分组: {[g.get('name') for g in groups]}")
        return None

    plans = target_group.get("list", [])
    if not plans:
        print("该分组下没有方案")
        return None
    if plan_index >= len(plans):
        print(f"方案索引超出范围，共 {len(plans)} 个方案")
        return None

    return plans[plan_index]

# ==================== OCR 模块 ====================
# win = Windows 自带 OCR（快，初始化<0.1s）；easyocr = 慢但兼容性强
OCR_BACKEND = os.environ.get("OCR_BACKEND", "win")
_reader = None
_win_ocr_engine = None

def get_reader():
    global _reader
    if _reader is None:
        print("[OCR] 正在初始化 easyocr（首次约10-30秒）...")
        import easyocr
        _reader = easyocr.Reader(['ch_sim', 'en'], gpu=False)
        print("[OCR] 初始化完成")
    return _reader

def normalize_gear_name(text):
    """去掉空白字符，便于名称匹配"""
    return "".join(str(text).split())

def ocr_region(screenshot, roi):
    """对截图中的 ROI 区域做 OCR，返回 [(text, conf, (x1,y1,x2,y2))]，坐标为全图坐标"""
    if OCR_BACKEND == "easyocr":
        return _ocr_region_easyocr(screenshot, roi)
    return _ocr_region_win(screenshot, roi)

def _ocr_region_easyocr(screenshot, roi):
    """easyocr 后端（慢）"""
    x, y, w, h = [int(v) for v in roi]
    h_img, w_img = screenshot.shape[:2]
    x = max(0, min(x, w_img - 1))
    y = max(0, min(y, h_img - 1))
    w = max(1, min(w, w_img - x))
    h = max(1, min(h, h_img - y))
    crop = screenshot[y:y + h, x:x + w]
    if crop.size == 0:
        return []
    reader = get_reader()
    results = reader.readtext(crop, detail=1, paragraph=False)
    out = []
    for box, text, conf in results:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x1, x2 = int(min(xs)), int(max(xs))
        y1, y2 = int(min(ys)), int(max(ys))
        out.append((text, conf, (x + x1, y + y1, x + x2, y + y2)))
    return out

def _ocr_region_win(screenshot, roi):
    """Windows 自带 OCR（WinRT）后端：极快，无模型加载"""
    import asyncio
    global _win_ocr_engine
    x, y, w, h = [int(v) for v in roi]
    h_img, w_img = screenshot.shape[:2]
    x = max(0, min(x, w_img - 1))
    y = max(0, min(y, h_img - 1))
    w = max(1, min(w, w_img - x))
    h = max(1, min(h, h_img - y))
    crop = screenshot[y:y + h, x:x + w]
    if crop.size == 0:
        return []
    # JPEG95 编码：比 PNG 快 ~5x（7.8ms→1.5ms），文本 OCR 质量几乎无损。
    ok, buf = cv2.imencode('.jpg', crop, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        return []

    async def _run():
        global _win_ocr_engine
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.globalization import Language
        from winsdk.windows.graphics.imaging import BitmapDecoder
        from winsdk.windows.storage.streams import InMemoryRandomAccessStream, DataWriter

        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(buf.tobytes())
        await writer.store_async()
        await writer.flush_async()
        writer.detach_stream()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        if _win_ocr_engine is None:
            _win_ocr_engine = OcrEngine.try_create_from_language(Language('zh-Hans-CN'))
            if _win_ocr_engine is None:
                _win_ocr_engine = OcrEngine.try_create_from_user_profile_languages()
        if _win_ocr_engine is None:
            return []
        result = await _win_ocr_engine.recognize_async(bitmap)
        out = []
        for line in result.lines:
            ws = list(line.words)
            if not ws:
                continue
            text = "".join(w.text for w in ws)
            x0 = min(w.bounding_rect.x for w in ws)
            y0 = min(w.bounding_rect.y for w in ws)
            x1 = max(w.bounding_rect.x + w.bounding_rect.width for w in ws)
            y1 = max(w.bounding_rect.y + w.bounding_rect.height for w in ws)
            out.append((text, 0.9, (int(x + x0), int(y + y0), int(x + x1), int(y + y1))))
        return out

    return asyncio.run(_run())

# ==================== 输入控制模块 ====================
class MaaInputController:
    def __init__(self):
        self.VK_DOWN = 0x28
        self.VK_UP = 0x26
        self.VK_PGDN = 0x22
        self.VK_PGUP = 0x21
        self._init_sendinput()

    def _init_sendinput(self):
        INPUT_KEYBOARD = 1
        KEYEVENTF_KEYUP = 0x0002
        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))
            ]
        class INPUT(ctypes.Structure):
            _fields_ = [
                ("type", wintypes.DWORD),
                ("ki", KEYBDINPUT),
                ("padding", ctypes.c_ubyte * 8)
            ]
        self.INPUT = INPUT
        self.INPUT_KEYBOARD = INPUT_KEYBOARD
        self.KEYEVENTF_KEYUP = KEYEVENTF_KEYUP

    def press_key(self, vk_code):
        inp = self.INPUT()
        inp.type = self.INPUT_KEYBOARD
        inp.ki.wVk = vk_code
        inp.ki.dwFlags = 0
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
        time.sleep(0.02)
        inp.ki.dwFlags = self.KEYEVENTF_KEYUP
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
        time.sleep(0.02)

    def click(self, x, y, offset_range=5):
        import random
        x += random.randint(-offset_range, offset_range)
        y += random.randint(-offset_range, offset_range)
        win32api.SetCursorPos((x, y))
        time.sleep(0.02)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.03)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(0.02)

input_ctrl = MaaInputController()

# ==================== 窗口与坐标工具 ====================
def find_game_window():
    """优先找 Unreal 渲染窗口（反作弊游戏 BitBlt 无效），回退到标题匹配
    注意：游戏可能有多个 UnrealWindow（含隐藏废句柄 rect=-8000），必须选有实际尺寸的可见窗口"""
    hwnd = win32gui.FindWindow("UnrealWindow", None)
    # 校验：窗口必须有实际尺寸（>300x200）且不位于隐藏位置
    def valid(h):
        if not h:
            return False
        try:
            l, t, r, b = win32gui.GetWindowRect(h)
            return (r - l) > 300 and (b - t) > 200 and l > -1000
        except Exception:
            return False
    if valid(hwnd):
        return hwnd
    # 枚举所有 UnrealWindow 找有效窗口
    best = 0
    def cb(h, _):
        nonlocal best
        if win32gui.GetClassName(h) == "UnrealWindow" and valid(h):
            if best == 0:
                best = h
        return True
    win32gui.EnumWindows(cb, None)
    if best:
        return best
    # 回退到标题匹配
    hwnd = win32gui.FindWindow(None, GAME_TITLE)
    return hwnd if valid(hwnd) else 0

def get_game_hwnd_and_size():
    hwnd = find_game_window()
    if hwnd == 0:
        print(f"[错误] 未找到游戏窗口（标题应为 {GAME_TITLE}）")
        return None, None, None, None
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return hwnd, right - left, bottom - top, (left, top)

def activate_game_window(hwnd):
    """把游戏窗口切到前台（截图/点击前调用）"""
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.8)
    except Exception as e:
        print(f"[警告] 激活窗口失败: {e}")

def capture_window(hwnd, width, height):
    """屏幕截图抓取窗口区域（BitBlt 对反作弊游戏无效，必须用屏幕抓取）"""
    left, top, _, _ = win32gui.GetWindowRect(hwnd)
    screen = windll.user32.GetDC(0)
    mfc_dc = win32ui.CreateDCFromHandle(screen)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(bitmap)
    save_dc.BitBlt((0, 0), (width, height), mfc_dc, (left, top), win32con.SRCCOPY)
    bmpstr = bitmap.GetBitmapBits(True)
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    windll.user32.ReleaseDC(0, screen)
    img = np.frombuffer(bmpstr, dtype=np.uint8).reshape((height, width, 4))
    img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return img

def get_abs_pos(slot_name):
    if slot_name not in SLOTS:
        return None
    pos = SLOTS[slot_name]
    return (int(WIN_LEFT + pos["x"] * SCALE_X), int(WIN_TOP + pos["y"] * SCALE_Y))

def get_window_pos(slot_name):
    """返回缩放后的窗口内坐标，供以窗口坐标为契约的控制器使用。"""
    if slot_name not in SLOTS:
        return None
    pos = SLOTS[slot_name]
    return (int(pos["x"] * SCALE_X), int(pos["y"] * SCALE_Y))

def get_list_roi(width, height):
    """装备列表 ROI：优先 config_abs.json 的 列表区域(x,y,w,h)，其次 maa_config 比例，最后默认比例"""
    if "列表区域" in SLOTS:
        p = SLOTS["列表区域"]
        if "w" in p and "h" in p:
            return (int(p["x"]), int(p["y"]), int(p["w"]), int(p["h"]))
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        r = cfg["tasks"]["find_gear_in_list"]["roi"]
        return (int(width * r[0]), int(height * r[1]), int(width * r[2]), int(height * r[3]))
    except Exception:
        return (int(width * 0.2), int(height * 0.12), int(width * 0.5), int(height * 0.6))

def preview_or_click(slot_name, x, y):
    win32api.SetCursorPos((x, y))
    print(f"[位置] {slot_name} -> ({x}, {y})")
    if PREVIEW_MODE:
        input("按 Enter 继续...")
        if PREVIEW_DELAY > 0:
            time.sleep(PREVIEW_DELAY)
    else:
        # 点击前把游戏窗口拉回前台（防止用户中途切走导致点击/截图失效）
        hwnd = find_game_window()
        if hwnd:
            activate_game_window(hwnd)
        input_ctrl.click(x, y)
        print(f"[点击] {slot_name}")

# ==================== 固定坐标操作 ====================
def click_weapon_category(gear_name):
    """根据装备名称选择对应的武器分类（固定坐标）"""
    category_map = {
        "步枪": "分类-步枪",
        "冲锋枪": "分类-冲锋枪",
        "霰弹枪": "分类-霰弹枪",
        "轻机枪": "分类-轻机枪",
        "精确射手步枪": "分类-精确射手步枪",
        "狙击步枪": "分类-狙击步枪",
        "特殊武器": "分类-特殊武器",
    }
    target_key = None
    for cat, key in category_map.items():
        if cat in gear_name or gear_name in cat:
            target_key = key
            break
    if target_key is None:
        print(f"[分类] 未找到 {gear_name} 的分类映射")
        return False

    pos = get_abs_pos(target_key)
    if pos is None:
        print(f"[分类] 坐标未标定: {target_key}")
        return False

    preview_or_click(target_key, pos[0], pos[1])
    time.sleep(0.8)
    return True

def scroll_down_fixed():
    """固定坐标点击滚动条下方"""
    pos = get_abs_pos("滚动条下方")
    if pos is None:
        print("[滚动] 滚动条下方坐标未标定，使用估算")
        scroll_pos = get_abs_pos("列表滚动条")
        if scroll_pos:
            x, y = scroll_pos
            y += 60
        else:
            hwnd, width, height, (win_left, win_top) = get_game_hwnd_and_size()
            if hwnd is None:
                return
            x = win_left + int(width * 0.85)
            y = win_top + int(height * 0.5) + 60
    else:
        x, y = pos
    preview_or_click("滚动条下方", x, y)
    time.sleep(0.3)

def click_military_label(hwnd, width, height):
    """使用固定坐标点击军需处标签"""
    pos = get_abs_pos("军需处标签")
    if pos:
        preview_or_click("军需处标签", pos[0], pos[1])
        time.sleep(1.0)
        return True
    else:
        print("[错误] 军需处标签未标定")
        return False

# ==================== 任务函数 ====================
def ensure_shop_open(hwnd, width, height):
    """确保军需处商店已打开：OCR 检查分类栏文字，没看到则点军需处标签（防开关点反）"""
    img = capture_window(hwnd, width, height)
    matched = ocr_region(img, (0, 0, width, height))
    joined = "".join(normalize_gear_name(t) for t, c, b in matched if c > 0.2)
    if any(k in joined for k in ["步枪", "冲锋枪", "霰弹枪", "轻机枪", "狙击枪"]):
        print("[军需处] 商店已在打开状态")
        return True
    pos = get_abs_pos("军需处标签")
    if pos is None:
        print("[错误] 军需处标签未标定")
        return False
    preview_or_click("军需处标签", pos[0], pos[1])
    time.sleep(1.2)
    return True

def slot_for_item(item):
    """装备类型 -> 配装界面槽位"""
    t = str(item.get("type", ""))
    if "头盔" in t:
        return "头盔"
    if "护甲" in t:
        return "护甲"
    if t.startswith("枪1"):
        return "主武器"
    if t.startswith("枪2"):
        return "副武器"
    return None  # 配件等暂不支持

def click_slot(slot_name):
    """点击配装界面的装备槽位，弹出购买列表"""
    pos = get_abs_pos(slot_name)
    if pos is None:
        print(f"[错误] 槽位坐标未标定: {slot_name}")
        return False
    preview_or_click(slot_name, pos[0], pos[1])
    time.sleep(1.2)
    return True

def find_gear_in_list(gear_name, slot_name, hwnd, win_left, win_top, width, height):
    """点槽位弹出列表 -> OCR 全窗口搜索装备并点击"""
    if not click_slot(slot_name):
        return False

    gear_norm = normalize_gear_name(gear_name)
    min_len = max(2, len(gear_norm) // 2)  # OCR 文本至少要有装备名一半长度

    for attempt in range(MAX_ATTEMPTS):
        hwnd = find_game_window()
        if hwnd:
            activate_game_window(hwnd)
        screenshot = capture_window(hwnd, width, height)
        matched = ocr_region(screenshot, (0, 0, width, height))
        for text, conf, bbox in matched:
            core = normalize_gear_name(text)
            if len(core) < min_len or conf < 0.2:
                continue
            if core == gear_norm or gear_norm in core or core in gear_norm:
                x_center = (bbox[0] + bbox[2]) // 2
                y_center = (bbox[1] + bbox[3]) // 2
                screen_x = win_left + x_center
                screen_y = win_top + y_center
                print(f"[命中] OCR: '{text}' (conf={conf:.2f}) == {gear_name}")
                preview_or_click(gear_name, screen_x, screen_y)
                time.sleep(0.5)
                return True

        # 滚动（固定点击滚动条下方）
        print(f"[滚动] 第 {attempt + 1} 次...")
        scroll_down_fixed()
        time.sleep(0.5)

    print(f"[失败] 未找到 {gear_name}")
    return False

def buy_current_item():
    """点击购买按钮，然后返回列表"""
    pos = get_abs_pos("购买按钮")
    if pos is None:
        print("[错误] 购买按钮未标定")
        return False
    preview_or_click("购买按钮", pos[0], pos[1])
    time.sleep(1.5)
    back = get_abs_pos("返回按钮")
    if back:
        preview_or_click("返回按钮", back[0], back[1])
        time.sleep(1.0)
    return True

def auto_equip(lv=0, group_name=None, plan_index=0):
    """主流程：拉取方案 → 逐件购买"""
    plan = get_loadout(lv, group_name, plan_index)
    if not plan:
        print("[错误] 获取配装方案失败")
        return

    items = plan.get("data", [])
    if not items:
        print("[错误] 方案没有装备明细")
        return

    # 类型过滤：FILTER_TYPES=头盔,护甲 只买指定类型的装备
    filter_types = os.environ.get("FILTER_TYPES", "").strip()
    if filter_types:
        kws = [k.strip() for k in filter_types.split(",") if k.strip()]
        items = [it for it in items if any(kw in str(it.get("type", "")) for kw in kws)]
        print(f"[过滤] 只购买类型含 {kws} 的装备，共 {len(items)} 件")

    print(f"✅ 配装方案: {plan.get('name')}")
    print(f"   总价: {plan.get('price')} | 战备: {plan.get('jz')} | 节省: {plan.get('cz')}")
    print(f"   共 {len(items)} 件装备")

    hwnd, width, height, (win_left, win_top) = get_game_hwnd_and_size()
    if hwnd is None:
        return
    activate_game_window(hwnd)
    setup_scale(width, height, win_left, win_top)

    ok = fail = 0
    for i, item in enumerate(items, 1):
        name = item.get("name", "")
        price = item.get("price", 0)
        slot = slot_for_item(item)
        if slot is None:
            print(f"\n[{i}/{len(items)}] [跳过] {name} (类型:{item.get('type')}) 暂不支持")
            continue
        print(f"\n[{i}/{len(items)}] 购买: {name} (价格 {price}) [槽位:{slot}]")
        if find_gear_in_list(name, slot, hwnd, win_left, win_top, width, height):
            buy_current_item()
            ok += 1
        else:
            fail += 1

    print(f"\n{'=' * 40}")
    print(f"完成: 成功 {ok} 件, 失败 {fail} 件")
    print(f"{'=' * 40}")

def main():
    print("=" * 60)
    print("  三角洲行动 · 自动配装（固定坐标版）v1.0")
    print(f"  预览模式: {'开启（只移动鼠标，不点击）' if PREVIEW_MODE else '关闭（正常点击购买）'}")
    print(f"  单件限价: {MAX_PRICE:,} | 最大滚动次数: {MAX_ATTEMPTS}")
    print("=" * 60)

    lv = 0
    group_name = None
    plan_index = 0
    if len(sys.argv) > 1:
        try:
            lv = int(sys.argv[1])
        except ValueError:
            pass
    if len(sys.argv) > 2 and sys.argv[2] != "-":
        group_name = sys.argv[2]
    if len(sys.argv) > 3:
        try:
            plan_index = int(sys.argv[3])
        except ValueError:
            pass

    print(f"档位: {LV_MAP.get(lv, lv)} | 分组: {group_name or '（默认第一个）'} | 方案: 第{plan_index}个")
    auto_equip(lv, group_name, plan_index)

if __name__ == "__main__":
    main()
