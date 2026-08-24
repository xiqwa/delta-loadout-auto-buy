# -*- coding: utf-8 -*-
"""
三角洲行动 · 卡战备自动配装 v4.3.3（MaaFramework Pipeline 版）
============================================================
核心思路：
  - 调用鼠鼠卡战备 V4 API（jzv4_zb）实时拉方案（不保存）
  - 用 MaaFramework 引擎执行：OCR 识别 + Click 动作（坐标全动态，抗窗口变化）
  - 购买顺序（重要！）：头盔 → 护甲 → 胸挂 → 背包 → 最后枪
  - 枪本体买完后，配件进入【改装界面】逐槽位购买

用法（游戏在战备/配装界面）：
    python maa_kazhanbei.py                 # 交互选档位+方案组
    python maa_kazhanbei.py 0 花费最少-兑换  # 直接指定
    PREVIEW_MODE=1 python maa_kazhanbei.py  # 试跑（只识别不购买）
    DRY_RUN_API=1 python maa_kazhanbei.py 0 花费最少-单枪  # 真实导航，停在购买前
"""
import sys, io, os, json, time, pathlib, re
from difflib import SequenceMatcher
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import win32gui, win32con
import cv2
import numpy as np
import auto_equip as ae
import detect_wear_checkbox as checkbox_detector
import api_client

from maa.library import Library
from maa.resource import Resource
from maa.controller import CustomController
from maa.tasker import Tasker

# ==================== 配置 ====================
BASE_DIR = r"C:\dflppeizhuang"
TOKEN = "74e336ed7f56add9c8fc3f789c7cb767"  # 完整密钥
LOADOUT_URL = "https://orzice.com/workApi/v1/sjz_api/jzv4_zb"
LV_MAP = {0: "11W机密", 1: "18W机密", 2: "55W绝密巴克什", 3: "60W绝密航天", 5: "78W绝密监狱"}
OCR_MODEL = pathlib.Path(r"C:\dflppeizhuang\model\ocr")
LOG_FILE = os.path.join(BASE_DIR, "maa_kazhanbei_log.txt")
PREVIEW_MODE = os.environ.get("PREVIEW_MODE", "0") == "1"
DRY_RUN_API = os.environ.get("DRY_RUN_API", "0") == "1"
DRY_RUN_CONTINUE = os.environ.get("DRY_RUN_CONTINUE", "1") == "1"
# 商店列表快速滚动：默认一次三格。识别漏行时可临时设 SHOP_SCROLL_DELTA=-240。
SHOP_SCROLL_DELTA = int(os.environ.get("SHOP_SCROLL_DELTA", "-360"))
SHOP_FINE_SCROLL_DELTA = int(os.environ.get("SHOP_FINE_SCROLL_DELTA", "-180"))
SHOP_SCROLL_DELAY = float(os.environ.get("SHOP_SCROLL_DELAY", "0.08"))
SHOP_MAX_SCROLL = max(8, int(os.environ.get("SHOP_MAX_SCROLL", "24")))
WEAR_CHECKBOX_THRESHOLD = float(os.environ.get("WEAR_CHECKBOX_THRESHOLD", "0.55"))
PURCHASE_RETRY_LIMIT = max(1, int(os.environ.get("PURCHASE_RETRY_LIMIT", "3")))
PURCHASE_VERIFY_DELAY = max(0.5, float(os.environ.get("PURCHASE_VERIFY_DELAY", "2.5")))
Library.open(pathlib.Path(r"C:\Program Files\Python311\Lib\site-packages\maa\bin"))

_log = []
def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    _log.append(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


class GameWindowUnavailable(RuntimeError):
    """游戏窗口已关闭、重建或暂时不可用；调用方必须停止发送输入。"""


# ==================== Maa 桥接控制器 ====================
class BridgeController(CustomController):
    """Maa 引擎 -> 真实游戏窗口（截图/点击全走这里）"""
    def __init__(self, hwnd):
        super().__init__()
        self.hwnd = hwnd
        self.set_screenshot_use_raw_size(True)  # 关键：关闭引擎缩放
        self._down = None

    def connect(self): return True
    def request_uuid(self): return "bridge-df"

    def ensure_window(self):
        """每次截图/点击前验证 hwnd；窗口重建时自动刷新句柄和缩放。"""
        old_hwnd = self.hwnd
        try:
            valid = bool(old_hwnd and win32gui.IsWindow(old_hwnd))
            if valid:
                left, top, right, bottom = win32gui.GetWindowRect(old_hwnd)
                valid = (right - left) > 300 and (bottom - top) > 200 and left > -1000
        except Exception:
            valid = False
        if not valid:
            new_hwnd = ae.find_game_window()
            if not new_hwnd:
                raise GameWindowUnavailable("游戏窗口句柄已失效，且无法重新找到游戏窗口")
            if new_hwnd == old_hwnd and old_hwnd:
                raise GameWindowUnavailable(f"游戏窗口句柄 {old_hwnd} 已失效，重新枚举仍返回同一无效句柄")
            try:
                left, top, right, bottom = win32gui.GetWindowRect(new_hwnd)
            except Exception as exc:
                raise GameWindowUnavailable(f"新游戏窗口句柄不可用: {exc}") from exc
            if (right - left) <= 300 or (bottom - top) <= 200:
                raise GameWindowUnavailable("重新找到的游戏窗口尺寸无效")
            self.hwnd = new_hwnd
            ae.setup_scale(right - left, bottom - top, left, top)
            log(f"  [窗口恢复] hwnd {old_hwnd} -> {new_hwnd}，已重新校准 {right-left}x{bottom-top}")
        return self.hwnd

    def ensure_foreground(self):
        """屏幕抓取依赖无遮挡窗口；不是前台时先激活，短暂重试后仍失败才安全停止。

        游戏窗口偶尔因用户切屏/弹窗抢占前台，立即中断会毁掉整轮配装；
        激活请求发出去后 Windows 可能延迟生效，重试 3 次（共约 2.5s）再放弃。
        """
        hwnd = self.ensure_window()
        last_err = None
        for attempt in range(3):
            try:
                if win32gui.GetForegroundWindow() != hwnd:
                    ae.activate_game_window(hwnd)
                if win32gui.GetForegroundWindow() == hwnd:
                    return hwnd
            except Exception as exc:
                last_err = exc
            if attempt < 2:
                time.sleep(0.6)
        if last_err is not None:
            raise GameWindowUnavailable(
                f"无法确认游戏窗口处于前台: {last_err}") from last_err
        raise GameWindowUnavailable(
            "游戏窗口未处于前台（重试 3 次仍被遮挡），拒绝截图/输入，"
            "避免识别或操作遮挡窗口")

    def screencap(self):
        hwnd = self.ensure_foreground()
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            return ae.capture_window(hwnd, right - left, bottom - top)
        except Exception as exc:
            # 截图过程中窗口也可能被游戏重建；清空后只重试一次。
            self.hwnd = 0
            hwnd = self.ensure_window()
            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                return ae.capture_window(hwnd, right - left, bottom - top)
            except Exception as retry_exc:
                raise GameWindowUnavailable(f"游戏窗口截图失败: {retry_exc}") from retry_exc

    def click(self, x, y):
        self._do_click(x, y); return True

    def touch_down(self, contact, x, y, pressure):
        self._down = (x, y); return True

    def touch_up(self, contact):
        if self._down:
            self._do_click(*self._down)
            self._down = None
        return True

    def touch_move(self, contact, x, y, pressure): return True

    def _do_click(self, x, y, precise=False):
        hwnd = self.ensure_foreground()
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception as exc:
            raise GameWindowUnavailable(f"点击前读取游戏窗口失败: {exc}") from exc
        width, height = right - left, bottom - top
        if not (0 <= x < width and 0 <= y < height):
            raise GameWindowUnavailable(f"拒绝越界点击 window({x},{y})，当前窗口 {width}x{height}")
        log(f"  [Maa点击] window({x},{y}) -> screen({left+x},{top+y})")
        if not PREVIEW_MODE:
            ae.input_ctrl.click(left + x, top + y,
                                offset_range=0 if precise else 3)
        else:
            log("  [预览] 跳过真实点击")

    def swipe(self, x1, y1, x2, y2, d): return True
    def click_key(self, k): return True
    def input_text(self, t): return True
    def key_down(self, k): return True
    def key_up(self, k): return True
    def start_app(self, i): return True
    def stop_app(self, i): return True


# ==================== Maa 任务执行器 ====================
class MaaRunner:
    """用 Maa pipeline 执行「OCR 找文字 -> Click」"""
    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.ctrl = BridgeController(hwnd)
        self.res = None
        self.tasker = None
        self._pipe_seq = 0

    def _ensure_maa(self):
        """仅在快速 OCR 失败时加载 Maa OCR，降低常见路径启动耗时。"""
        if self.tasker is not None:
            return
        started = time.perf_counter()
        self.res = Resource()
        self.res.set_cpu()
        self.res.post_ocr_model(str(OCR_MODEL)).wait()
        self.tasker = Tasker()
        self.tasker.bind(self.res, self.ctrl)
        for _ in range(100):
            if self.tasker.inited:
                break
            time.sleep(0.05)
        log(f"[Maa] OCR回退引擎就绪 ({time.perf_counter()-started:.2f}s)")

    def _pipe(self, expected, timeout=15000, click=True):
        """构造单任务 pipeline：OCR 找 expected -> 可选 Click"""
        self._ensure_maa()
        self._pipe_seq += 1
        node = {
            "recognition": "OCR",
            "expected": expected,
            "timeout": timeout,
            "action": "Click" if click else "DoNothing",
            "target": True,
        }
        pipe = {"version": {}, f"t{self._pipe_seq}": node}
        p = pathlib.Path(BASE_DIR) / f"_maa_task_{self._pipe_seq}.json"
        p.write_text(json.dumps(pipe, ensure_ascii=False), encoding="utf-8")
        self.res.post_pipeline(str(p)).wait()
        return f"t{self._pipe_seq}"

    def click_text(self, text, timeout=15000):
        """OCR 找文字并点击；返回 (成功?, 命中坐标或None)"""
        task = self._pipe([text], timeout=timeout, click=True)
        # 先探测坐标（DoNothing 任务拿 box），再决定是否真点
        t0 = time.time()
        tj = self.tasker.post_task(task)
        tj.wait()
        ok = tj.succeeded and not tj.failed
        log(f"  [OCR] 找「{text}」: {'✓命中' if ok else '✗未找到'} ({time.time()-t0:.1f}s)")
        return ok

    def find_text_pos(self, text, timeout=10000):
        """OCR 找文字返回窗口内坐标（不点击）；找不到返回 None"""
        task = self._pipe([text], timeout=timeout, click=False)
        tj = self.tasker.post_task(task)
        tj.wait()
        if tj.succeeded and not tj.failed:
            # 从最新截图 OCR 拿坐标
            img = self.screencap_now()
            for t2, c2, b2 in ae.ocr_region(img, (0, 0, img.shape[1], img.shape[0])):
                if text in t2 or text in ae.normalize_gear_name(t2):
                    cx = (b2[0] + b2[2]) // 2
                    cy = (b2[1] + b2[3]) // 2
                    return (cx, cy)
        return None

    def screencap_now(self):
        img = self.ctrl.screencap()
        self.hwnd = self.ctrl.hwnd
        return img

    def click_pos(self, x, y, precise=False):
        self.ctrl._do_click(x, y, precise=precise)
        self.hwnd = self.ctrl.hwnd
        time.sleep(0.12)

    def ensure_window(self):
        self.hwnd = self.ctrl.ensure_window()
        return self.hwnd

    def stop(self):
        if self.tasker is None:
            return
        try:
            self.tasker.post_stop()
        except Exception:
            pass


def _ocr_core(text):
    """去掉 OCR 常见的点/横线/符号，只保留可比较文字。"""
    return "".join(ch for ch in ae.normalize_gear_name(text) if ch.isalnum())


def click_text_fast(runner, expected, roi=None, min_conf=0.2, exact_only=False):
    """限定区域单帧 OCR 点击；exact_only 时只接受与目标完全一致的文字。"""
    img = runner.screencap_now()
    h, w = img.shape[:2]
    roi = roi or (0, 0, w, h)
    expected_core = _ocr_core(expected)
    candidates = []
    for text, conf, bbox in ae.ocr_region(img, roi):
        core = _ocr_core(text)
        if conf >= min_conf and (expected_core in core or core in expected_core):
            if exact_only and core != expected_core:
                continue
            candidates.append((core == expected_core, conf,
                               -abs(len(core) - len(expected_core)), text, bbox))
    if not candidates:
        return False
    _, _, _, text, bbox = max(candidates)
    pos = _center(bbox)
    log(f"  [快速OCR] 「{text}」box={bbox} -> window{pos}")
    runner.click_pos(*pos, precise=True)
    return True


def click_weapon_category(runner, label):
    """只点击顶部武器分类标签；不允许点到列表商品行。"""
    img = runner.screencap_now()
    h, w = img.shape[:2]
    top_roi = (0, int(h * 0.04), w, int(h * 0.20))
    if click_text_fast(runner, label, top_roi, exact_only=True):
        return True
    pos = runner.find_text_pos(label, timeout=6000)
    if pos and pos[1] < h * 0.20:
        log(f"  [分类] Maa回退定位「{label}」window{pos}")
        runner.click_pos(*pos, precise=True)
        return True
    log(f"  [分类] 未在顶部标签区找到「{label}」")
    return False


def _detect_box_below_text(img, bbox):
    """识别 OCR 文字下方最近的方形框；返回 (box, method) 或 (None, None)。"""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = [int(v) for v in bbox]
    text_h = max(1, y2 - y1)
    text_w = max(1, x2 - x1)
    pad_x = max(14, int(text_w * 0.5))
    x0 = max(0, x1 - pad_x)
    xw = min(w, x2 + pad_x)
    y0 = min(h - 1, y2 + 3)
    y1b = min(h, y0 + max(64, int(text_h * 4.2)))
    if y1b - y0 < 24:
        return None, None
    crop = img[y0:y1b, x0:xw]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    masks = [
        cv2.Canny(gray, 40, 120),
        cv2.Canny(gray, 80, 180),
        cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                              cv2.THRESH_BINARY, 15, -2),
    ]
    best = None
    best_score = -1.0
    for mask in masks:
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST,
                                       cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            rx, ry, rw, rh = cv2.boundingRect(contour)
            if not (18 <= rw <= 150 and 18 <= rh <= 150):
                continue
            ratio = rw / max(1, rh)
            if not 0.5 <= ratio <= 2.4:
                continue
            rect_area = max(1, rw * rh)
            rectangularity = cv2.contourArea(contour) / rect_area
            center_x = rx + rw / 2.0
            center_dx = abs(center_x - (xw - x0) / 2.0) / max(1, xw - x0)
            vertical = (ry + rh / 2.0) / max(1, y1b - y0)
            score = (rectangularity - center_dx * 0.35 +
                     min(rw, rh) / 150.0 * 0.3)
            if 0.28 < vertical < 0.92:
                score += 0.25
            if score > best_score:
                best_score = score
                best = (x0 + rx, y0 + ry, x0 + rx + rw, y0 + ry + rh)
                # 高分且位置合理的候选：提前返回，不再扫剩余 mask/轮廓。
                if score >= 0.85 and 0.4 < vertical < 0.8:
                    return best, "contour"
    if best is not None:
        return best, "contour"
    fallback_h = max(48, int(text_h * 3.6))
    fallback_w = max(42, int(text_w * 1.25))
    bx1 = max(0, x1 - int(fallback_w * 0.25))
    bx2 = min(w, x2 + int(fallback_w * 0.25))
    by1 = min(h, y2 + 4)
    by2 = min(h, by1 + fallback_h)
    if by2 - by1 >= 32 and bx2 - bx1 >= 24:
        return (bx1, by1, bx2, by2), "text-offset"
    return None, None


def find_accessory_slot(runner, keywords):
    """OCR 找配件槽文字后，点击其正下方方框的中心，不点文字本身。"""
    img = runner.screencap_now()
    results = ae.ocr_region(img, (0, 0, img.shape[1], img.shape[0]))
    candidates = []
    for text, conf, bbox in results:
        core = _ocr_core(text)
        if conf < 0.2 or not any(keyword in core for keyword in keywords):
            continue
        box, method = _detect_box_below_text(img, bbox)
        if box is not None:
            candidates.append((bbox, text, box, method))
    if not candidates:
        return None
    _, text, box, method = min(candidates, key=lambda item: item[0][1])
    log(f"  [配件槽] OCR「{text}」→ 方框{box}（{method}）")
    return _center(box)


def frame_signature(img, roi):
    x, y, w, h = roi
    crop = img[y:y + h, x:x + w]
    if crop.size == 0:
        return None
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)


def wait_for_region_change(runner, before, roi, timeout=1.4, threshold=2.2):
    """点击后等待目标区域稳定发生变化，避免写死长延迟。"""
    baseline = frame_signature(before, roi)
    if baseline is None:
        time.sleep(min(timeout, 0.5))
        return False
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        current = runner.screencap_now()
        signature = frame_signature(current, roi)
        if signature is not None:
            delta = float(np.mean(cv2.absdiff(baseline, signature)))
            if delta >= threshold:
                return True
        time.sleep(0.08)
    return False


def click_return_if_visible(runner):
    """仅在 OCR 明确看到导航区“返回”时点击；绝不猜左上角坐标。"""
    img = runner.screencap_now()
    h, w = img.shape[:2]
    if is_main_loadout_page(img):
        log("  [安全返回] 已在主配装页面，不点击“返回”")
        return True
    results = []
    for roi in ((0, 0, int(w * 0.30), h),
                (0, int(h * 0.72), w, int(h * 0.28))):
        results.extend(ae.ocr_region(img, roi))
    hits = []
    for text, conf, bbox in results:
        core = ae.normalize_gear_name(text)
        cx, cy = (bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2
        if conf >= 0.2 and "返回" in core and (cx < w * 0.25 or cy > h * 0.75):
            hits.append((cy, cx, text, bbox))
    if not hits:
        log("  [安全返回] 当前画面未识别到导航区“返回”，不发送点击")
        return False
    _, _, text, bbox = max(hits)
    pos = ((bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2)
    log(f"  [安全返回] OCR「{text}」box={bbox}，点击 window{pos}")
    runner.click_pos(*pos, precise=True)
    return True


def is_main_loadout_page(img, ocr_results=None):
    """识别购买完成后是否回到主配装页。"""
    h, w = img.shape[:2]
    if ocr_results is None:
        regions = [
            (int(w * 0.72), int(h * 0.68), int(w * 0.28), int(h * 0.22)),
            (0, int(h * 0.24), int(w * 0.40), int(h * 0.56)),
            (int(w * 0.60), int(h * 0.24), int(w * 0.40), int(h * 0.56)),
        ]
        ocr_results = []
        for roi in regions:
            ocr_results.extend(ae.ocr_region(img, roi))
    cores = []
    for text, conf, bbox in ocr_results:
        if conf >= 0.2:
            cores.append(ae.normalize_gear_name(text))
    joined = "".join(cores)
    slot_count = sum(1 for core in cores if "未装配" in core or "已装配" in core)
    strong = "确认配装" in joined
    combined = ("装备价值" in joined or "制式套装" in joined) and slot_count >= 2
    return strong or combined


def wait_for_main_loadout_page(runner, timeout=PURCHASE_VERIFY_DELAY):
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        img = runner.screencap_now()
        if is_main_loadout_page(img):
            log(f"  [购买验证] 第{attempt}次识别：已回到主配装页面")
            return True
        time.sleep(0.4)
    log(f"  [购买验证] {timeout:.1f}s 内未识别到主配装页面")
    return False


def leave_purchase_safely(runner, clean_name, max_escape=3):
    """DRY 模式仅用 Esc 退出购买链，确认主页面后才能继续下一件。"""
    if not DRY_RUN_CONTINUE:
        return True
    for attempt in range(1, max_escape + 1):
        img = runner.screencap_now()
        if is_main_loadout_page(img):
            log(f"  [DRY返回] {clean_name} 已回到主配装页面")
            return True
        if PREVIEW_MODE:
            return False
        runner.ctrl.ensure_foreground()
        ae.input_ctrl.press_key(0x1B)
        log(f"  [DRY返回] 第{attempt}/{max_escape}次发送 Esc（不会触发购买）")
        time.sleep(0.35)
    ok = is_main_loadout_page(runner.screencap_now())
    log(f"  [DRY返回] {'已确认主配装页面' if ok else '未能确认主配装页面，停止后续装备'}")
    return ok


def recover_to_main_page(runner, context, max_escape=2):
    """失败后只用 Esc 做保守恢复；无法确认主页面则停止。"""
    if is_main_loadout_page(runner.screencap_now()):
        return True
    if PREVIEW_MODE:
        return False
    for attempt in range(1, max_escape + 1):
        runner.ctrl.ensure_foreground()
        ae.input_ctrl.press_key(0x1B)
        time.sleep(0.30)
        if is_main_loadout_page(runner.screencap_now()):
            log(f"  [失败恢复] {context}：第{attempt}次 Esc 后回到主配装页面")
            return True
    log(f"  [失败恢复] {context}：未能确认主配装页面")
    return False


# ==================== API 拉取 ====================
def fetch_kazhanbei(lv):
    data, err = api_client.fetch_loadout(lv, TOKEN, logger=log)
    if err:
        log(f"[API] 获取失败: {err}")
        return None
    return data


def pick_group(data, gname=None):
    if gname:
        for g in data:
            if gname in g.get("name", ""):
                return g
    # 非交互环境(GUI 子进程 stdin 非 tty): 匹配失败直接默认第一组, 绝不等待输入
    if not sys.stdin.isatty():
        print(f"[警告] 方案组「{gname}」未匹配，默认选择第 0 组")
        return data[0] if data else None
    print("\n方案组：")
    for i, g in enumerate(data):
        print(f"  [{i}] {g.get('name')} | {g.get('desc')} | {len(g.get('list',[]))}方案")
    try:
        choice = int(input("选择序号(回车0): ").strip() or "0")
    except (ValueError, EOFError):
        choice = 0
    return data[choice] if 0 <= choice < len(data) else data[0]


def pick_plan(group, idx=None):
    lst = group.get("list", [])
    if not lst:
        return None
    if idx is None:
        # 默认最省
        return min(lst, key=lambda b: b.get("cz", 0))
    return lst[idx] if 0 <= idx < len(lst) else lst[0]


# ==================== 购买计划（严格按 API type 字段） ====================
# API 实测 type 值（码点验证）：
#   '枪1' -> 主武器 / '枪2' -> 副武器 / '手枪' -> 手枪（武器本体）
#   '枪1-瞄具' '枪1-弹匣' '枪1-枪托' '枪1-前握把' '枪1-枪口' '枪1-护木' '枪1-后握把'
#   '枪2-*' 同理（配件，进改装界面买）
#   '头盔' / '护甲' / '胸挂' / '背包'（独立槽位）
# exchange: ""=交易行市场购买；"兑换"=军需处兑换获取
def build_plan(plan):
    """
    严格按 API 返回的 type 字段排序：
      1. 头盔 / 护甲 / 胸挂 / 背包（独立槽位）
      2. 枪1 / 枪2 / 手枪（武器本体，最后）
      3. 枪1-* / 枪2-* 配件（进改装界面买）
    返回 [(item, 阶段), ...]
    """
    armor, rigs, bags, guns, parts = [], [], [], [], []
    raw = plan.get("data", [])
    if not isinstance(raw, list):
        log(f"  [计划] ⚠️ API data 不是数组({type(raw).__name__})，按空方案处理")
        raw = []
    for it in raw:
        t = str(it.get("type", "")).strip()
        if t == "头盔":
            armor.append((it, "头盔"))
        elif t == "护甲":
            armor.append((it, "护甲"))
        elif t == "胸挂":
            rigs.append((it, "胸挂"))
        elif t == "背包":
            bags.append((it, "背包"))
        elif t == "枪1":
            guns.append((it, "主武器"))
        elif t == "枪2":
            guns.append((it, "副武器"))
        elif t == "手枪":
            guns.append((it, "手枪"))
        elif t.startswith("枪1-") or t.startswith("枪2-"):
            # 配件：枪1-瞄具 / 枪2-枪托 ...
            parts.append((it, t))
        else:
            # 未知类型也归为配件处理，避免漏买
            parts.append((it, t))
    # 胸挂/背包使用仓库已有(不购买)：GUI 设置「胸挂背包使用仓库的」
    if os.environ.get("USE_WAREHOUSE_RIG_BAG", "0") == "1":
        if rigs:
            log(f"  [仓库] 胸挂 {len(rigs)} 件使用仓库已有，跳过购买: "
                + "、".join(str(it.get("name", "")) for it, _ in rigs[:3]))
            rigs = []
        if bags:
            log(f"  [仓库] 背包 {len(bags)} 件使用仓库已有，跳过购买: "
                + "、".join(str(it.get("name", "")) for it, _ in bags[:3]))
            bags = []
    # 最终顺序：头甲 → 胸挂 → 背包 → 枪 → 配件
    ordered = []
    ordered += armor
    ordered += rigs
    ordered += bags
    ordered += guns
    ordered += parts
    return ordered


# ==================== 槽位动态定位（OCR 找「未装配」） ====================
def locate_slot_pos(runner, slot, img=None):
    """
    通过 OCR 找槽位文字（未装配/槽位名）定位点击位置
    左侧列(x<600)：头盔/护甲/胸挂/背包；右侧列(x>1300)：主武器/副武器
    """
    if img is None:
        img = runner.screencap_now()
    h, w = img.shape[:2]
    candidates = []
    left_slot = slot in ("头盔", "护甲", "胸挂", "背包")
    roi = ((0, int(h * 0.18), int(w * 0.38), int(h * 0.78)) if left_slot else
           (int(w * 0.62), int(h * 0.12), int(w * 0.38), int(h * 0.72)))
    for t2, c2, b2 in ae.ocr_region(img, roi):
        core = ae.normalize_gear_name(t2)
        if not core:
            continue
        cx = (b2[0] + b2[2]) // 2
        cy = (b2[1] + b2[3]) // 2
        # 左侧槽位（头盔/护甲/胸挂/背包）
        if cx < w * 0.40:
            if slot == "头盔" and ("盔" in core or "未装配" in core):
                candidates.append((cy, cx, cy))
            elif slot == "护甲" and ("防弹衣" in core or "背心" in core or "护甲" in core):
                candidates.append((cy, cx, cy))
            elif slot == "胸挂" and "胸挂" in core:
                candidates.append((cy, cx, cy))
            elif slot == "背包" and "背包" in core:
                candidates.append((cy, cx, cy))
        # 右侧槽位（主/副武器）
        elif cx > w * 0.60 and b2[1] < h * 0.82:
            if slot == "主武器" and ("未装配" in core or "枪" in core):
                candidates.append((cy, cx, cy))
            elif slot == "副武器" and ("未装配" in core or "枪" in core):
                candidates.append((cy, cx, cy))
    if not candidates:
        return None
    candidates.sort()
    # 左侧从上到下第1=头盔 第2=护甲；右侧第1=主武器
    return (candidates[0][1], candidates[0][2])


def click_slot_dynamic(runner, slot):
    """主页面确认后优先使用归一化槽位中心，配置缺失才 OCR。"""
    before = runner.screencap_now()
    h, w = before.shape[:2]
    window_pos = ae.get_window_pos(slot)
    if window_pos and 0 <= window_pos[0] < w and 0 <= window_pos[1] < h:
        log(f"[槽位] {slot} 归一化坐标 {window_pos}")
        runner.click_pos(*window_pos)
        wait_for_region_change(runner, before, (0, int(h * 0.08), int(w * 0.40), int(h * 0.88)))
        return True
    pos = locate_slot_pos(runner, slot, before)
    if pos:
        log(f"[槽位] {slot} OCR回退定位 ({pos[0]},{pos[1]})")
        runner.click_pos(*pos)
        wait_for_region_change(runner, before, (0, int(h * 0.08), int(w * 0.40), int(h * 0.88)))
        return True
    log(f"[槽位] {slot} 无法定位")
    return False


# ==================== ⚠️ 最高优先级：军需处&交易行标签校验 ====================
# 三角洲行动只有三个真实磨损选项，不做别名或 grade 映射。
# “几乎全新”必须排在“全新”之前，避免剥离时只删掉后半段。
WEAR_WORDS = ("几乎全新", "全新", "破损")
GAME_WEAR_LEVELS = ("全新", "几乎全新", "破损")
DEFAULT_WEAR_LEVEL = "几乎全新"


def strip_wear_from_name(text):
    core = ae.normalize_gear_name(text)
    for word in WEAR_WORDS:
        core = core.replace(word, "")
    return core.translate(str.maketrans("", "", "()（）[]【】"))


def gear_name_similarity(expected, observed):
    """装备名容错评分；短名称仍要求严格，长名称允许少量 OCR 错字。"""
    expected = strip_wear_from_name(expected)
    observed = strip_wear_from_name(observed)
    if not expected or not observed:
        return 0.0
    if expected == observed:
        return 1.0
    if expected in observed:
        return 1.0
    if observed in expected:
        return min(len(expected), len(observed)) / max(len(expected), len(observed))
    return SequenceMatcher(None, expected, observed, autojunk=False).ratio()


def best_name_similarity(expected, observed):
    """整行 OCR 可能混入价格/耐久，按相近长度子串取最高名称相似度。"""
    expected = strip_wear_from_name(expected)
    observed = strip_wear_from_name(observed)
    if not expected or not observed:
        return 0.0
    if expected == observed:
        return 1.0
    if expected in observed:
        return 1.0
    if observed in expected:
        return min(len(expected), len(observed)) / max(len(expected), len(observed))
    best = SequenceMatcher(None, expected, observed, autojunk=False).ratio()
    min_len = max(2, int(len(expected) * 0.65))
    max_len = min(len(observed), max(min_len, int(len(expected) * 1.6)))
    if len(observed) > 40:
        # 超长行（整行混入价格/耐久等多段文本）：滑动窗口近似，避免二次方开销。
        for win_len in range(min_len, max_len + 1):
            for start in range(0, len(observed) - win_len + 1, 2):
                ratio = SequenceMatcher(None, expected,
                                        observed[start:start + win_len],
                                        autojunk=False).ratio()
                if ratio > best:
                    best = ratio
                    if best >= 0.99:
                        return best
    else:
        # 常规行：全起点×全长度扫描，保证最佳匹配精度。
        for start in range(len(observed)):
            for end in range(start + min_len, min(len(observed), start + max_len) + 1):
                ratio = SequenceMatcher(None, expected, observed[start:end],
                                        autojunk=False).ratio()
                if ratio > best:
                    best = ratio
                    if best >= 0.99:
                        return best
    return best


def group_ocr_rows(results, image_height):
    """合并同一行 OCR 片段，兼容装备名和磨损词被识别成两个框。"""
    rows = []
    tolerance = max(10, int(image_height * 0.012))
    for text, conf, bbox in sorted(results, key=lambda r: ((r[2][1] + r[2][3]) // 2, r[2][0])):
        cy = (bbox[1] + bbox[3]) // 2
        row = next((r for r in rows if abs(r["cy"] - cy) <= tolerance), None)
        if row is None:
            row = {"cy": cy, "parts": []}
            rows.append(row)
        row["parts"].append((text, conf, bbox))
        row["cy"] = int(sum((p[2][1] + p[2][3]) // 2 for p in row["parts"]) / len(row["parts"]))
    for row in rows:
        row["parts"].sort(key=lambda p: p[2][0])
        row["text"] = "".join(p[0] for p in row["parts"])
    return rows


def _is_junxu_tag_core(core):
    core = "".join(ch for ch in core if ch.isalnum())
    if "军需" in core:
        return True
    return SequenceMatcher(None, "军需处", core).ratio() >= 0.66


def _find_junxu_tag_boxes(results, image_width, image_height=None):
    """只认左侧“军需处&交易行”分隔标签，顶部导航里的单个“交易行”不算。"""
    if image_height is not None:
        rows = group_ocr_rows(results, image_height)
        hits = []
        for row in rows:
            valid = [(text, conf, bbox) for text, conf, bbox in row["parts"]
                     if conf >= 0.2]
            if not valid:
                continue
            core = "".join(_ocr_core(text) for text, _, _ in valid)
            if not _is_junxu_tag_core(core):
                continue
            x1 = min(bbox[0] for _, _, bbox in valid)
            y1 = min(bbox[1] for _, _, bbox in valid)
            x2 = max(bbox[2] for _, _, bbox in valid)
            y2 = max(bbox[3] for _, _, bbox in valid)
            if x1 < image_width * 0.5:
                hits.append((row["text"], (x1, y1, x2, y2)))
        return hits
    hits = []
    for text, conf, bbox in results:
        if conf >= 0.2 and bbox[0] < image_width * 0.5 and \
                _is_junxu_tag_core(_ocr_core(text)):
            hits.append((text, bbox))
    return hits


def find_junxu_tag_y(results, image_width, image_height=None):
    hits = _find_junxu_tag_boxes(results, image_width, image_height)
    return (max(bbox[3] for _, bbox in hits), hits) if hits else (None, [])


def _is_current_sale_tag_core(core):
    core = "".join(ch for ch in core if ch.isalnum())
    return ("当前在售" in core or ("当前" in core and "在售" in core) or
            core == "在售" or
            SequenceMatcher(None, "当前在售", core).ratio() >= 0.72)


def _find_current_sale_tag_boxes(results, image_width, image_height=None):
    """配件列表使用「当前在售」分隔标签，不套用军需处&交易行。"""
    if image_height is not None:
        rows = group_ocr_rows(results, image_height)
        hits = []
        for row in rows:
            valid = [(text, conf, bbox) for text, conf, bbox in row["parts"]
                     if conf >= 0.2]
            if not valid:
                continue
            core = "".join(_ocr_core(text) for text, _, _ in valid)
            if not _is_current_sale_tag_core(core):
                continue
            x1 = min(bbox[0] for _, _, bbox in valid)
            y1 = min(bbox[1] for _, _, bbox in valid)
            x2 = max(bbox[2] for _, _, bbox in valid)
            y2 = max(bbox[3] for _, _, bbox in valid)
            if x1 < image_width * 0.6:
                hits.append((row["text"], (x1, y1, x2, y2)))
        return hits
    hits = []
    for text, conf, bbox in results:
        if conf >= 0.2 and bbox[0] < image_width * 0.6 and \
                _is_current_sale_tag_core(_ocr_core(text)):
            hits.append((text, bbox))
    return hits


def find_current_sale_tag_y(results, image_width, image_height=None):
    hits = _find_current_sale_tag_boxes(results, image_width, image_height)
    return (max(bbox[3] for _, bbox in hits), hits) if hits else (None, [])


def verify_junxu_tag(runner, item_name, item_bbox=None):
    """
    ⚠️ 用户最高优先级铁律：
    买的装备必须在「军需处&交易行」标签下面！
    必须先 OCR 搜到「军需处&交易行」标签，确认装备在标签之下，才允许购买。
    搜不到标签 -> 返回 False（不买）
    """
    img = runner.screencap_now()
    h, w = img.shape[:2]
    tag_boxes = _find_junxu_tag_boxes(ae.ocr_region(img, (0, 0, w, h)), w)
    for t2, b2 in tag_boxes:
        log(f"  [标签校验] 找到「{t2}」box={b2}")
    if not tag_boxes:
        log(f"  ✗✗ [标签校验] 未找到「军需处&交易行」标签！按规则【不购买】{item_name}")
        return False
    # 标签 y 取最下面的标签（列表里的分隔标签通常在装备上方）
    tag_y = max(b[3] for b in tag_boxes)
    if item_bbox:
        item_y = item_bbox[1]
        if item_y < tag_y:
            log(f"  ✗✗ [标签校验] 装备在标签上方(y={item_y}<{tag_y})，可能是「已拥有」区！按规则【不购买】")
            return False
        log(f"  ✓ [标签校验] 装备在「军需处&交易行」标签下方 (装备y={item_y} >= 标签y={tag_y})")
    else:
        log(f"  ✓ [标签校验] 已确认存在「军需处&交易行」标签 (y={tag_y})")
    return True


def find_item_in_shop(runner, name, max_scroll=SHOP_MAX_SCROLL,
                      tag_mode="junxu"):
    """
    在商店列表用剥离磨损词后的干净名找装备，并按标签模式校验区域。
    滚动策略由 SHOP_SCROLL_DELTA/SHOP_SCROLL_DELAY 控制，默认一次三格快速滚动。
    返回命中装备的窗口内坐标 (x,y)；未通过校验返回 None
    """
    import ctypes, win32api
    # 保持原接口：旧代码可传名称，新购买链传完整 item 以校验 grade。
    item = name if isinstance(name, dict) else {"name": name}
    item_name = str(item.get("name", ""))
    gear_norm = strip_wear_from_name(item_name)
    min_len = max(2, len(gear_norm) // 2)
    tag_label = "当前在售" if tag_mode == "sale" else "军需处&交易行"
    log(f"  [列表目标] API名称「{item_name}」→ 干净名「{gear_norm}」"
        f"；标签模式={tag_label}")
    hwnd = runner.ensure_window()
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    except Exception as exc:
        raise GameWindowUnavailable(f"商店搜索前读取窗口失败: {exc}") from exc
    w, h = right - left, bottom - top
    # 鼠标放在列表区域（窗口内 x~250, y~600）——列表在左侧 x 100-450
    cursor_x = max(1, min(w - 1, int(250 * ae.SCALE_X)))
    cursor_y = max(1, min(h - 1, int(600 * ae.SCALE_Y)))
    win32api.SetCursorPos((left + cursor_x, top + cursor_y))
    time.sleep(0.08)
    # 每个槽位列表可能继承上次滚动位置；先快速归顶，确保扫描方向和标签状态确定。
    if not PREVIEW_MODE:
        previous_top = None
        top_stagnant = 0
        for _ in range(8):
            top_img = runner.screencap_now()
            roi = (0, int(top_img.shape[0] * 0.08),
                   int(top_img.shape[1] * 0.36), int(top_img.shape[0] * 0.88))
            signature = frame_signature(top_img, roi)
            if previous_top is not None:
                delta = float(np.mean(cv2.absdiff(signature, previous_top)))
                top_stagnant = top_stagnant + 1 if delta < 0.8 else 0
                # 已在列表顶部：无需继续归顶
                if top_stagnant >= 2:
                    break
            previous_top = signature
            ctypes.windll.user32.mouse_event(0x0800, 0, 0, 1200, 0)
            time.sleep(0.06)
    market_region_reached = False
    previous_signature = None
    stagnant_frames = 0
    near_misses = []
    for attempt in range(max_scroll):
        img = runner.screencap_now()
        hh, ww = img.shape[:2]
        # 商店名称与分隔标签都位于左侧；收窄 ROI 可显著减少 OCR 像素量和干扰文本。
        list_roi = (0, int(hh * 0.08), int(ww * 0.36), int(hh * 0.88))
        rx, ry, rw, rh = list_roi
        gray = cv2.cvtColor(img[ry:ry + rh, rx:rx + rw], cv2.COLOR_BGR2GRAY)
        signature = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
        if previous_signature is not None:
            delta = float(np.mean(cv2.absdiff(signature, previous_signature)))
            stagnant_frames = stagnant_frames + 1 if delta < 0.8 else 0
            if stagnant_frames >= 3:
                log(f"  [列表停止] 连续{stagnant_frames}屏几乎无变化(delta={delta:.2f})，判定已到底或滚动未生效")
                break
            if stagnant_frames >= 1:
                # 滚动尚未生效（画面与上一屏相同）：跳过本屏 OCR，直接继续滚动，
                # 避免每屏白付一次全列表 OCR 的耗时。
                log(f"  [列表跳过] 本屏与上屏几乎无变化(delta={delta:.2f})，跳过OCR直接滚动")
                if not PREVIEW_MODE:
                    delta_scroll = SHOP_FINE_SCROLL_DELTA if market_region_reached else SHOP_SCROLL_DELTA
                    ctypes.windll.user32.mouse_event(0x0800, 0, 0, delta_scroll, 0)
                time.sleep(SHOP_SCROLL_DELAY)
                continue
        previous_signature = signature
        results = ae.ocr_region(img, list_roi)
        if tag_mode == "sale":
            tag_y, _ = find_current_sale_tag_y(results, ww, hh)
        else:
            tag_y, _ = find_junxu_tag_y(results, ww, hh)
        if tag_y is not None:
            market_region_reached = True
        if tag_y is None:
            if market_region_reached:
                log(f"  [标签校验] 第 {attempt + 1}/{max_scroll} 屏标签已向上移出，但此前已进入{tag_label}区域")
            else:
                log(f"  [标签校验] 第 {attempt + 1}/{max_scroll} 屏尚未找到「{tag_label}」标签，本屏不选择")
        for row in group_ocr_rows(results, hh):
            row_text = row["text"]
            valid_parts = [(text, conf, bbox) for text, conf, bbox in row["parts"] if conf >= 0.2]
            row_core = strip_wear_from_name("".join(text for text, _, _ in valid_parts))
            if len(row_core) < min_len:
                continue
            x1 = min(bbox[0] for _, _, bbox in valid_parts)
            y1 = min(bbox[1] for _, _, bbox in valid_parts)
            x2 = max(bbox[2] for _, _, bbox in valid_parts)
            y2 = max(bbox[3] for _, _, bbox in valid_parts)
            row_bbox = (x1, y1, x2, y2)
            similarity = best_name_similarity(gear_norm, row_core)
            threshold = 0.84 if len(gear_norm) >= 6 else 0.92
            if similarity < threshold:
                if len(near_misses) < 6:
                    near_misses.append((similarity, row_text, row_bbox))
                continue
            log(f"  [列表候选] 「{row_text}」box={row_bbox}：名称相似度={similarity:.2f}")
            if not market_region_reached or (tag_y is not None and y1 < tag_y):
                log(f"  [跳过] 同名装备位于交易行标签上方或标签未知 (item_y={y1}, tag_y={tag_y})，继续寻找")
                continue
            log(f"  [列表命中] 干净名「{gear_norm}」；已确认位于{tag_label}区域")
            return ((x1 + x2) // 2, (y1 + y2) // 2)
        # 快速滚动；参数可通过环境变量调整，避免写死速度。
        if not PREVIEW_MODE:
            delta = SHOP_FINE_SCROLL_DELTA if market_region_reached else SHOP_SCROLL_DELTA
            ctypes.windll.user32.mouse_event(0x0800, 0, 0, delta, 0)
        else:
            log("  [预览] 跳过真实滚动")
        time.sleep(SHOP_SCROLL_DELAY)
    if near_misses:
        log("  [列表近误] 最接近的候选：")
        for similarity, row_text, row_bbox in sorted(
                near_misses, key=lambda item: item[0], reverse=True)[:3]:
            log(f"    {similarity:.2f} 「{row_text}」box={row_bbox}")
    log(f"  [列表失败] {max_scroll} 次滚动内未找到干净名「{gear_norm}」；"
        f"可能名称 OCR 不完整、列表未进入{tag_label}区域或商品不存在")
    return None


# ==================== 购买流程 ====================
def _center(bbox):
    return ((bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2)


def _ocr_wear_levels(img):
    """识别磨损弹窗里的档位行，返回 {档位: (text, row_box)}。

    ⚠️ 不能用 group_ocr_rows 合并整行：弹窗档位文字与右侧信息区
    （如「默认0」「131」价格）y 坐标相近会被误合并，导致 bbox 横跨
    大片空白，点击中心落到弹窗外面（“点不下去”的根因）。
    这里逐条 OCR 匹配档位词，点击位置就是档位词本身中心。
    """
    h, w = img.shape[:2]
    hits = {}
    # ROI 只覆盖弹窗区域（中部偏左），避免右侧信息区干扰
    results = ae.ocr_region(
        img, (int(w * 0.28), int(h * 0.22), int(w * 0.52), int(h * 0.52)))
    for text, conf, bbox in results:
        if conf < 0.2:
            continue
        core = ae.normalize_gear_name(text)
        # 长词优先匹配：'几乎全新' 包含 '全新' 子串，必须先判几乎全新
        if "几乎全新" in core:
            level = "几乎全新"
        elif "破损" in core:
            level = "破损"
        elif "全新" in core:
            level = "全新"
        else:
            continue
        x1, y1, x2, y2 = bbox
        # 点击区域就是档位词本身（只留 4px 容差），直接点击文字中心。
        row_box = (max(0, x1 - 4), max(0, y1 - 4),
                   min(w, x2 + 4), min(h, y2 + 4))
        hits.setdefault(level, (text, row_box))
    return hits


def open_wear_selection(runner, clean_name, api_price=None):
    """打开磨损档位选择弹窗。

    主路径：轮廓检测购买入口左侧小方框，点击后确认三档选择界面。
    回退路径：小方框检测失败/置信度不足时，直接 OCR 找「交易行/军需处/购买/
    兑换」入口文字并点击文字中心（用户实测有时轮廓不可靠，文字一定可见）。
    点击失败时按检测中心小幅偏移重试，最多 2 轮。
    """
    for round_no in range(1, 3):
        img = runner.screencap_now()
        box = None
        try:
            best, _, _ = checkbox_detector.detect_checkbox(img, api_price)
            score, box = best["score"], best["box"]
            if score >= WEAR_CHECKBOX_THRESHOLD:
                # 实测头盔成功点就是灰色外框几何中心；偏移会落到图标边缘导致弹窗不打开。
                pos = _center(box)
                log(f"  [小方框] 第{round_no}轮 干净名={clean_name}｜轮廓得分={score:.3f}"
                    f"｜阈值={WEAR_CHECKBOX_THRESHOLD:.2f}｜box={box}｜center={best['center']}")
                # 第 2 轮：位置微调（中心下方偏移，按窗口高度缩放，应对不同分辨率下
                # 小方框位置上下浮动导致的空点）。
                if round_no == 2:
                    offset_y = max(2, round(6 * img.shape[0] / 1127.0))
                    pos = (pos[0], min(img.shape[0] - 1, pos[1] + offset_y))
                log(f"  [小方框] 第{round_no}轮 安全点击点={pos}（灰色外框几何中心）")
            else:
                log(f"  [小方框] 第{round_no}轮 轮廓得分={score:.3f} 低于阈值"
                    f"{WEAR_CHECKBOX_THRESHOLD:.2f}，改用文字点击回退")
        except Exception as exc:
            log(f"  [小方框] 第{round_no}轮检测异常，改用文字点击回退: {exc}")

        if box is None:
            # 文字回退：直接点击购买入口文字中心（不用轮廓）。
            pos = _click_purchase_entry_text(runner)
            if pos is None:
                log(f"  [文字回退] 第{round_no}轮未找到购买入口文字，放弃本轮")
                return False
            log(f"  [文字回退] 第{round_no}轮 点击购买入口文字 window{pos}")
            runner.click_pos(*pos, precise=True)
        else:
            runner.click_pos(*pos, precise=True)
        opened = False
        for check in range(1, 4):
            time.sleep(0.20 if check == 1 else 0.25)
            levels = _ocr_wear_levels(runner.screencap_now())
            log(f"  [档位弹窗] 第{round_no}轮·第{check}/3次 OCR档位={list(levels)}")
            if levels:
                opened = True
                # OCR 可能漏掉全新或破损；目标档明确存在即可安全继续。
                if DEFAULT_WEAR_LEVEL in levels:
                    return True
        if opened:
            # ⚠️ 弹窗已打开但目标档 OCR 漏识：绝不能再点击（会点在弹窗上，
            # 可能关闭弹窗或选错档位），再多等一次重新 OCR 后判定失败。
            time.sleep(0.5)
            levels = _ocr_wear_levels(runner.screencap_now())
            if DEFAULT_WEAR_LEVEL in levels:
                return True
            log(f"  [档位弹窗] 弹窗已打开但连续未识别到目标“几乎全新”"
                f"(OCR档位={list(levels)})，停止（不重试点击）")
            return False
        log(f"  [档位弹窗] 第{round_no}轮未检测到任何档位（弹窗未打开），准备重试")
    log("  [小方框] 两轮点击后仍未出现档位弹窗，停止")
    return False


def _click_purchase_entry_text(runner):
    """OCR 找购买入口文字（交易行/军需处/购买/兑换）并返回点击点；找不到返回 None。

    只接受右下购买详情区（x>=0.55w, y>=0.60h）的文字，避免点到左侧列表。
    """
    img = runner.screencap_now()
    h, w = img.shape[:2]
    roi = (int(w * 0.55), int(h * 0.55), int(w * 0.44), int(h * 0.42))
    keywords = ("交易行", "军需处", "购买", "兑换")
    hits = []
    for text, conf, bbox in ae.ocr_region(img, roi):
        core = ae.normalize_gear_name(text)
        if conf < 0.2 or not any(keyword in core for keyword in keywords):
            continue
        cx = (bbox[0] + bbox[2]) // 2
        cy = (bbox[1] + bbox[3]) // 2
        # 过滤过小的碎片（可能是商品名误命中），只点右下购买区。
        if bbox[2] - bbox[0] < 30 or cy < h * 0.60:
            continue
        hits.append((cy, text, _center(bbox)))
    if not hits:
        return None
    _, text, pos = min(hits)  # 取最靠上的入口文字
    log(f"  [文字回退] OCR「{text}」box 中心={pos}")
    return pos


def _read_purchase_price(runner):
    """读取弹窗下方蓝色购买按钮上的价格数字，返回 int 或 None(读取失败)。

    先用 HSV 过滤定位蓝色横向按钮，再只对该区域 OCR，避免把档位行价格
    当成购买价；定位不到蓝色按钮时回退原固定区域 OCR。校验失败不阻塞购买。
    """
    img = runner.screencap_now()
    h, w = img.shape[:2]
    region = (int(w * 0.38), int(h * 0.58), int(w * 0.40), int(h * 0.26))
    button_regions = []

    try:
        low = tuple(int(v) for v in
                    os.environ.get("PURCHASE_BLUE_LOW", "90,60,60").split(","))
        high = tuple(int(v) for v in
                     os.environ.get("PURCHASE_BLUE_HIGH", "130,255,255").split(","))
        search = (int(w * 0.34), int(h * 0.54), int(w * 0.48), int(h * 0.34))
        sx, sy, sw, sh = search
        sx1, sy1 = min(w, sx + sw), min(h, sy + sh)
        hsv = cv2.cvtColor(img[sy:sy1, sx:sx1], cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array(low, dtype=np.uint8),
                           np.array(high, dtype=np.uint8))
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (15, 5)))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for contour in contours:
            bx, by, bw, bh = cv2.boundingRect(contour)
            if (bw < w * 0.04 or bh < h * 0.008 or bh > h * 0.12
                    or bw / max(1, bh) < 2.0):
                continue
            candidates.append((bw * bh,
                               (sx + bx, sy + by,
                                sx + bx + bw, sy + by + bh)))
        candidates.sort(key=lambda item: item[0], reverse=True)
        button_regions = [box for _, box in candidates[:8]]
    except Exception:
        button_regions = []

    if not button_regions:
        button_regions = [region]

    for bbox in button_regions:
        try:
            # 直接用按钮区域 OCR, pad 扩展会导致识别率下降
            texts = ae.ocr_region(img, bbox)
        except Exception:
            continue
        for text, _, _ in texts:
            # OCR 可能返回全角逗号(1，948), 统一转半角再解析
            clean = re.sub(r"\s+", "", text).replace("，", ",")
            nums = re.findall(r"\d{1,3}(?:,\d{3})+|\d{2,9}", clean)
            if nums:
                return max(int(num.replace(",", "")) for num in nums)
    return None


def select_wear_level(runner, wanted=DEFAULT_WEAR_LEVEL, api_price=None,
                      clean_name=None):
    """OCR 选择档位；默认几乎全新，不存在时只回退到更低的破损档。

    api_price 非空时先按“破损→几乎全新→全新”的价格顺序试档：点击档位后
    读购买按钮价格，命中即返回；未命中用 clean_name 重开磨损弹窗继续试。
    全部未命中或无法重开时，回退原 wanted 逻辑；读不到价格不阻塞购买。
    """
    fallback = {
        "全新": ("全新", "几乎全新", "破损"),
        "几乎全新": ("几乎全新", "破损"),
        "破损": ("破损",),
    }

    def _read_levels():
        return _ocr_wear_levels(runner.screencap_now())

    def _click_level(level, levels, attempt_no=1, strict=False):
        text, bbox = levels[level]
        pos = _center(bbox)
        log(f"  [选档] 目标={wanted}｜实际={level}｜OCR「{text}」box={bbox}"
            f"｜点击window{pos}（第{attempt_no}次）")
        runner.click_pos(*pos, precise=True)
        # 等弹窗关闭动画（0.25s 偏短，选错行会直接买错档位）
        time.sleep(0.45)
        if api_price is None:
            return level, True, None
        shown = _read_purchase_price(runner)
        if shown is None:
            if strict:
                # 匹配阶段: 读不到价 = 无法确认, 继续试下一个档位
                log("  [价格匹配] 未读到价格，无法确认档位，继续试下一个")
                return level, False, None
            log("  [价格校验] 未读到价格，不阻塞购买，按当前档位继续")
            return level, True, None
        if abs(shown - api_price) <= max(3, int(api_price * 0.01)):
            log(f"  [价格校验] ✓ 游戏价 {shown} == API价 {api_price}，档位确认")
            return level, True, shown
        return level, False, shown

    reopen_failed = False
    if api_price is not None:
        levels = _read_levels()
        for candidate in ("破损", "几乎全新", "全新"):
            if candidate not in levels:
                continue
            chosen, matched, shown = _click_level(candidate, levels, strict=True)
            if matched:
                return chosen
            log(f"  [价格匹配] ✗ 档位={candidate} 游戏价 {shown} != API价 {api_price}")
            if not clean_name:
                log("  [价格匹配] 未传入 clean_name，无法重开磨损弹窗，回退 wanted 档位")
                reopen_failed = True
                break
            log(f"  [价格匹配] 档位={candidate} 价格不匹配，重新打开磨损弹窗继续")
            if open_wear_selection(runner, clean_name, api_price):
                levels = _read_levels()
                continue
            log("  [价格匹配] 无法重新打开磨损弹窗，回退 wanted 档位继续")
            reopen_failed = True
            break
        if reopen_failed:
            levels = _read_levels()
        elif clean_name:
            log("  [价格匹配] 全部候选未命中，重新打开磨损弹窗后按 wanted 回退")
            if not open_wear_selection(runner, clean_name, api_price):
                log("  [价格匹配] 全部候选未命中且无法重开弹窗，直接按 wanted 回退")
                reopen_failed = True
            levels = _read_levels()
        else:
            levels = _read_levels()
        log("  [价格匹配] 未按 API 价格匹配到档位，回退原 wanted 逻辑")
    else:
        levels = _read_levels()

    chosen = next(
        (level for level in fallback.get(wanted, (DEFAULT_WEAR_LEVEL, "破损"))
         if level in levels), None)
    if not chosen:
        if reopen_failed:
            log("  [选档降级] 无法重开磨损弹窗，按 wanted 档位继续购买")
            return wanted
        log(f"  [选档失败] 目标={wanted}｜OCR档位={list(levels)}｜无可用回退档")
        return None

    for attempt in range(3):
        chosen, matched, shown = _click_level(chosen, levels, attempt + 1)
        if matched:
            return chosen
        log(f"  [价格校验] ✗ 游戏价 {shown} != API价 {api_price}"
            f"（第{attempt + 1}次），再点一次档位")
    log(f"  [选档] 多次价格不一致，按当前档位 {chosen} 继续购买")
    return chosen


def _detect_final_purchase(runner):
    """检测最终购买按钮，返回 (found, 点击坐标或None)。纯检测不点击。"""
    img = runner.screencap_now()
    h, w = img.shape[:2]
    hits = []
    for text, conf, bbox in ae.ocr_region(img, (int(w * 0.38), int(h * 0.58), int(w * 0.38), int(h * 0.28))):
        core = ae.normalize_gear_name(text)
        if conf >= 0.2 and ("购买" in core or "确认" in core):
            hits.append((bbox[1], text, bbox))
    if hits:
        _, text, bbox = max(hits)
        return True, _center(bbox), f"OCR「{text}」box={bbox}"
    # 最终按钮通常只显示动态价格。仅在已确认档位弹窗后，检测中央下方绿色按钮轮廓。
    x0, y0 = int(w * 0.38), int(h * 0.62)
    x1, y1 = int(w * 0.72), int(h * 0.82)
    crop = img[y0:y1, x0:x1]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array((35, 70, 60), dtype=np.uint8),
                      np.array((100, 255, 255), dtype=np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        if cw >= w * 0.08 and ch >= h * 0.025 and cw / max(ch, 1) >= 2.5:
            boxes.append((cw * ch, (x0 + x, y0 + y, x0 + x + cw, y0 + y + ch)))
    if not boxes:
        return False, None, "未识别到购买文字或底部绿色横向按钮"
    _, bbox = max(boxes)
    return True, _center(bbox), f"绿色按钮轮廓 box={bbox}"


def click_final_purchase(runner):
    """选档后点击最终购买；OCR 文字优先，回退检测弹窗底部绿色横向按钮。"""
    found, pos, detail = _detect_final_purchase(runner)
    if not found:
        log(f"  [最终购买] {detail}，不猜坐标")
        return False
    log(f"  [最终购买] {detail}｜点击window{pos}")
    runner.click_pos(*pos, precise=True)
    return True


def _complete_purchase_and_verify(runner, clean_name, click_action):
    """执行最终购买并确认回到主配装页面。"""
    if DRY_RUN_API:
        detected = click_action(detect_only=True)
        log(f"  [DRY_RUN_API] {'已定位' if detected else '未定位'} {clean_name} 的最终按钮；不会点击")
        return detected and leave_purchase_safely(runner, clean_name)
    for attempt in range(1, PURCHASE_RETRY_LIMIT + 1):
        log(f"  [购买尝试] {clean_name} 第{attempt}/{PURCHASE_RETRY_LIMIT}次")
        if not click_action(detect_only=False):
            log("  [购买重试] 本次未定位到最终购买按钮")
            time.sleep(0.5)
            continue
        if wait_for_main_loadout_page(runner):
            log(f"  [购买成功] {clean_name} 已回到主配装页面")
            return True
        # ⚠️ 防重复购买：若最终购买按钮已不可见，说明可能已购买成功（页面跳转中），
        # 此时绝不能再次点击。延长等待确认回主页；仍无法确认则判定结果不确定并停止，
        # 绝不落回通用点击重试（否则下一轮按钮重新可见时会重复购买）。
        found, _, _ = _detect_final_purchase(runner)
        if not found:
            log("  [购买安全] 最终购买按钮已不可见，疑似购买成功(跳转中)，延长等待确认")
            if wait_for_main_loadout_page(runner, timeout=4.5):
                log(f"  [购买成功] {clean_name} 已回到主配装页面")
                return True
            log(f"  [购买失败] {clean_name} 按钮不可见但长时间未回主页面，"
                f"结果不确定，停止重试（防误点）")
            return False
        log("  [购买重试] 点击后未回主页面，将重新识别最终按钮")
    log(f"  [购买失败] {clean_name} 连续{PURCHASE_RETRY_LIMIT}次未确认回到主页面")
    return False


def click_regular_purchase(runner, item, detect_only=False):
    """无磨损装备：直接点击交易行价格按钮或 API 指定的兑换按钮。"""
    img = runner.screencap_now()
    clean_name = strip_wear_from_name(item.get("name", ""))
    if str(item.get("exchange", "")).strip() == "兑换":
        hits = []
        h, w = img.shape[:2]
        rois = [(int(w * 0.60), int(h * 0.68), int(w * 0.38), int(h * 0.28))]
        # 小尺寸参考裁剪或窄窗口中，整张图本身就是详情按钮区域。
        if w < 900 or h < 500:
            rois = [(0, 0, w, h)]
        for roi in rois:
            for text, conf, bbox in ae.ocr_region(img, roi):
                core = ae.normalize_gear_name(text)
                core = core.translate(str.maketrans("", "", "。．.,，:：!?！？·"))
                if conf >= 0.2 and core == "兑换":
                    hits.append((bbox[1], text, bbox))
        if not hits:
            log(f"  [普通购买] {clean_name} 未识别到“兑换”按钮，不猜坐标")
            return False
        _, text, bbox = max(hits)
        pos = _center(bbox)
        if not detect_only:
            runner.click_pos(*pos, precise=True)
        log(f"  [普通购买] OCR「{text}」box={bbox}｜{'定位' if detect_only else '点击'}window{pos}")
        return True

    anchored = checkbox_detector._find_market_button_checkbox(img)
    if not anchored or "anchor_box" not in anchored:
        log(f"  [普通购买] {clean_name} 未识别到交易行价格按钮边框，不猜坐标")
        return False
    bbox = anchored["anchor_box"]
    pos = _center(bbox)
    if not detect_only:
        runner.click_pos(*pos, precise=True)
    log(f"  [普通购买] 交易行价格按钮 box={bbox}｜{'定位' if detect_only else '点击'}安全中心window{pos}")
    return True


def final_action(runner, item):
    """头盔/护甲走磨损链；其余装备直接按交易渠道购买。"""
    clean_name = strip_wear_from_name(item.get("name", ""))
    item_type = str(item.get("type", "")).strip()
    if item_type not in ("头盔", "护甲"):
        log(f"  [购买链] {clean_name}｜type={item_type} 无磨损，走普通购买")
        return _complete_purchase_and_verify(
            runner, clean_name,
            lambda detect_only=False: click_regular_purchase(
                runner, item, detect_only=detect_only))

    wanted = DEFAULT_WEAR_LEVEL
    log(f"  [购买链] 干净名={clean_name}｜type={item_type} 有磨损｜默认档位={wanted}")
    try:
        api_price = int(item.get("price"))
    except (TypeError, ValueError):
        api_price = None
    if not open_wear_selection(runner, clean_name, api_price):
        log(f"  [购买链失败] {clean_name}｜小方框或档位弹窗识别失败")
        return False
    chosen = select_wear_level(runner, wanted, api_price, clean_name)
    if not chosen:
        log(f"  [购买链失败] {clean_name}｜无法选择档位")
        return False
    if DRY_RUN_API:
        log(f"  [DRY_RUN_API] 将购买 {clean_name} (档位={chosen})；已选档，停在最终购买前")
        try:
            cv2.imwrite(os.path.join(BASE_DIR, "debug_dryrun_final.png"),
                        runner.screencap_now())
        except Exception:
            pass
        return leave_purchase_safely(runner, clean_name)
    return _complete_purchase_and_verify(runner, clean_name,
                                         lambda detect_only=False:
                                         True if detect_only else click_final_purchase(runner))


def buy_independent(runner, item, slot):
    """独立槽位购买：头盔/护甲/胸挂/背包（强制军需处&交易行标签校验）"""
    name = item.get("name", "")
    log(f"▶ [{slot}] {name} (价:{item.get('price')})")
    if not click_slot_dynamic(runner, slot):
        return False
    # ⚠️ 最高优先级：找到装备且必须在「军需处&交易行」标签下
    pos = find_item_in_shop(runner, item)
    if not pos:
        log("  ✗ 未找到装备或未通过军需处&交易行标签校验")
        return False
    runner.click_pos(*pos)
    time.sleep(0.5)
    # 点购买/装备按钮（安全试验模式在这里截断）
    if not final_action(runner, item):
        log("  ✗ 找不到购买/装备/预安装按钮")
        return False
    time.sleep(0.8)
    return True


def buy_gun(runner, item, slot):
    """买枪本体：主武器/副武器/手枪（强制军需处&交易行标签校验）"""
    name = item.get("name", "")
    log(f"▶ [{slot}] {name} (价:{item.get('price')})")
    if not click_slot_dynamic(runner, slot):
        return False
    # 切武器分类（OCR 动态找分类标签）——注意顺序：射手/狙击等具体分类必须在「步枪」前面！
    cat_map = [
        ("射手", "精确射手步枪"), ("狙击", "狙击步枪"), ("冲锋枪", "冲锋枪"),
        ("霰弹枪", "霰弹枪"), ("轻机枪", "轻机枪"), ("手枪", "手枪"),
        ("特殊", "特殊武器"), ("步枪", "突击步枪"),
    ]
    for kw, label in cat_map:
        if kw in name:
            log(f"  [分类] 切「{label}」")
            if not click_weapon_category(runner, label):
                return False
            time.sleep(0.8)
            break
    # ⚠️ 最高优先级：找到枪且必须在「军需处&交易行」标签下
    pos = find_item_in_shop(runner, item)
    if not pos:
        log("  ✗ 未找到枪或未通过军需处&交易行标签校验")
        return False
    runner.click_pos(*pos)
    time.sleep(0.5)
    if not final_action(runner, item):
        log("  ✗ 找不到购买/装备/预安装按钮")
        return False
    time.sleep(0.8)
    return True


def buy_part(runner, item, part_type):
    """改装界面买配件：进改装 -> 按配件 type 精确点槽 -> 找配件 -> 购买"""
    name = item.get("name", "")
    log(f"▶ [配件] {name} ({part_type}, 价:{item.get('price')})")
    # 1) 进入改装界面（点主武器槽 -> 点「改装」）
    if not click_slot_dynamic(runner, "主武器"):
        return False
    time.sleep(0.8)
    img = runner.screencap_now()
    h, w = img.shape[:2]
    if not click_text_fast(runner, "改装", (int(w * 0.55), 0, int(w * 0.45), h),
                           exact_only=True):
        log("  [改装] 快速OCR未命中")
        pos = runner.find_text_pos("改装", timeout=7000)
        if not pos or pos[0] < w * 0.55:
            log("  ✗ 找不到「改装」按钮")
            return False
        runner.click_pos(*pos, precise=True)
    time.sleep(1.0)

    # 2) 配件 type -> 改装界面槽位（精确映射，不再乱匹配）
    #    改装界面槽位文字：护木/右导轨/左导轨/导轨架/前握把/瞄准/握把/枪托/后握把/枪口
    type_slot_kw = {
        "瞄具": ["瞄准", "瞄具", "镜"],
        "枪口": ["枪口"],
        "护木": ["护木"],
        "枪托": ["枪托"],
        "弹匣": ["弹匣", "弹鼓", "弹夹"],
        "前握把": ["前握把", "前把"],
        "握把": ["握把"],
        "后握把": ["后握把"],
    }
    slot_kw = None
    for key, kws in type_slot_kw.items():
        if key in part_type:
            slot_kw = kws
            break
    if slot_kw is None:
        slot_kw = [part_type.replace("枪1-", "").replace("枪2-", "")]

    # 找对应配件槽：识别文字下的方框并点方框中心，不点文字本身。
    slot_pos = find_accessory_slot(runner, slot_kw)
    if not slot_pos:
        log(f"  ✗ 找不到配件槽({slot_kw})，不进入错误页面搜索")
        return False
    runner.click_pos(*slot_pos, precise=True)
    time.sleep(1.0)
    # 3) 找配件并购买（⚠️ 配件列表使用「当前在售」标签，不是军需处&交易行）
    pos = find_item_in_shop(runner, item, tag_mode="sale")
    if not pos:
        log("  ✗ 配件未找到或未通过军需处&交易行标签校验")
        return False
    runner.click_pos(*pos)
    time.sleep(0.5)
    if not final_action(runner, item):
        log("  ✗ 找不到购买/装备/预安装按钮")
        return False
    time.sleep(0.8)
    return True


def acquire_run_lock():
    """Windows 互斥体：防止命令行与 GUI 双开同时操作游戏（避免重复购买事故）。"""
    import ctypes
    # 显式声明类型：64 位 Windows 下句柄是 64 位指针，默认 c_int 会截断。
    ctypes.windll.kernel32.CreateMutexW.restype = ctypes.c_void_p
    ctypes.windll.kernel32.CreateMutexW.argtypes = [ctypes.c_void_p,
                                                    ctypes.c_int,
                                                    ctypes.c_wchar_p]
    ctypes.windll.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = ctypes.windll.kernel32.CreateMutexW(None, False,
                                                 "Local\\KazhanbeiAutoEquipRunner")
    if not handle or ctypes.windll.kernel32.GetLastError() == 183:
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
        return None
    return handle


def release_run_lock(handle):
    if handle:
        import ctypes
        try:
            ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass


# ==================== 主流程 ====================
def run(lv, group_name=None, plan_index=None):
    """可编程入口：GUI/命令行共用。返回状态码：
       "ok"=正常完成（含部分购买失败） "locked"=双开被拒
       "aborted"=安全停止 "error"=未预期异常"""
    run_lock = acquire_run_lock()
    if run_lock is None:
        log("[安全停止] 已有卡战备运行实例(命令行或GUI)在操作游戏；拒绝双开")
        return "locked"
    try:
        _run_impl(lv, group_name, plan_index)
        return "ok"
    except GameWindowUnavailable as exc:
        log(f"[安全停止] {exc}")
        return "aborted"
    except Exception as exc:
        # 顶层兜底：记录日志并返回错误状态，让调用方以非零退出码结束，
        # 避免 GUI/CLI 把异常当“已完成”。
        import traceback
        log(f"[异常] 未预期的错误: {exc}")
        log(traceback.format_exc()[-1500:])
        return "error"
    finally:
        release_run_lock(run_lock)


def _run_impl(lv, group_name=None, plan_index=None):
    # 清理上次运行遗留的 Maa pipeline 临时文件
    try:
        for tmp in pathlib.Path(BASE_DIR).glob("_maa_task_*.json"):
            tmp.unlink(missing_ok=True)
    except Exception:
        pass
    # 日志文件超过 2MB 自动裁剪，防止无限增长
    try:
        if os.path.getsize(LOG_FILE) > 2_000_000:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write("[日志] 超过 2MB 已自动裁剪\n")
    except Exception:
        pass
    log("=" * 60)
    log("  卡战备自动配装 v4.3.3（MaaFramework Pipeline 版）")
    mode = "PREVIEW_MODE（全跳过）" if PREVIEW_MODE else ("DRY_RUN_API（真实导航，最终动作截断）" if DRY_RUN_API else "真实购买")
    log(f"  运行模式: {mode} | 购买顺序: 头甲→胸挂→背包→枪→配件")
    if not PREVIEW_MODE and not DRY_RUN_API:
        log("  [安全警告] 当前为真实购买模式；调试请显式设置 DRY_RUN_API=1")
    if PREVIEW_MODE and DRY_RUN_API:
        log("  [模式提示] PREVIEW_MODE 优先：所有点击和滚动仍会跳过")
    log("=" * 60)

    if lv is None or lv not in LV_MAP:
        lv = 0
    log(f"[档位] lv={lv} {LV_MAP[lv]}")

    # 2) 实时 API
    log("[API] 实时拉取方案...")
    data = fetch_kazhanbei(lv)
    if not data:
        log("获取失败"); return
    group = pick_group(data, group_name)
    plan = pick_plan(group, plan_index)
    log(f"[方案] {group.get('name')} | 总价:{plan.get('price')} 战备:{plan.get('jz')} 省:{plan.get('cz')}")

    # 3) 购买计划
    ordered = build_plan(plan)
    log(f"[计划] {len(ordered)} 项：")
    for it, stage in ordered:
        log(f"  - {it.get('name')} ({it.get('type')}) 价:{it.get('price')}")

    # 4) 窗口
    hwnd = ae.find_game_window()
    if not hwnd:
        log("未找到游戏窗口"); return
    ae.activate_game_window(hwnd)
    time.sleep(0.8)
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
    except Exception:
        hwnd = ae.find_game_window()
        if not hwnd:
            log("[安全停止] 游戏窗口在初始化期间失效，重新查找失败；未发送后续点击")
            return
        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
        except Exception as exc:
            log(f"[安全停止] 游戏窗口在初始化期间持续不可用: {exc}")
            return
    w, h = r - l, b - t
    ae.setup_scale(w, h, l, t)  # 关键：校准坐标 -> 当前窗口缩放
    log(f"[窗口] hwnd={hwnd} {w}x{h} @({l},{t}) 缩放 {ae.SCALE_X:.3f}/{ae.SCALE_Y:.3f}")

    runner = MaaRunner(hwnd)
    ok = fail = 0
    try:
        for i, (item, stage) in enumerate(ordered, 1):
            t = str(item.get("type", ""))
            ex = str(item.get("exchange", ""))
            log(f"\n[{i}/{len(ordered)}] {item.get('name')} ({stage})")
            if ex == "兑换":
                log("  [渠道] API exchange=兑换：详情面板优先识别并点击“兑换”按钮")
            try:
                # 仅在 OCR 明确看到“返回”时退一层；禁止盲点窗口左上角。
                runner.ensure_window()
                returned = click_return_if_visible(runner)
                if not returned:
                    log("  [安全停止] 当前不在主配装页且无法明确返回；不执行本件及后续装备")
                    fail += 1
                    break
                if not is_main_loadout_page(runner.screencap_now()):
                    time.sleep(0.8)
                    if not is_main_loadout_page(runner.screencap_now()):
                        log("  [安全停止] 返回后仍未确认主配装页面；不执行本件及后续装备")
                        fail += 1
                        break
                hwnd = runner.hwnd
                ae.activate_game_window(hwnd)
                time.sleep(0.5)
                if stage in ("头盔", "护甲", "胸挂", "背包"):
                    r = buy_independent(runner, item, stage)
                elif stage in ("主武器", "副武器", "手枪"):
                    r = buy_gun(runner, item, stage)
                else:
                    r = buy_part(runner, item, t)
            except GameWindowUnavailable as exc:
                log(f"  [安全停止] {exc}；不再截图、点击或继续后续装备")
                fail += 1
                break
            if r:
                ok += 1
            else:
                fail += 1
                if not recover_to_main_page(runner, item.get("name", "本件装备")):
                    log("  [安全停止] 本件失败后无法安全恢复主配装页面；停止后续装备")
                    break
            time.sleep(0.5)
    finally:
        runner.stop()
        # 清理本轮运行生成的 Maa pipeline 临时文件（含 MaaRunner._pipe 产生的）
        try:
            for tmp in pathlib.Path(BASE_DIR).glob("_maa_task_*.json"):
                tmp.unlink(missing_ok=True)
        except Exception as exc:
            log(f"  [清理] 临时文件清理失败: {exc}")

    log("\n" + "=" * 60)
    log(f"完成！成功 {ok}，失败 {fail}")
    log(f"日志: {LOG_FILE}")
    log("=" * 60)


if __name__ == "__main__":
    # 命令行入口: python maa_kazhanbei.py [lv] [方案组名] [方案索引]
    lv = None
    if len(sys.argv) > 1:
        try:
            lv = int(sys.argv[1])
        except ValueError:
            lv = None
    group_name = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        plan_index = int(sys.argv[3]) if len(sys.argv) > 3 else None
    except ValueError:
        plan_index = None
    result = run(lv, group_name, plan_index)
    # 双开被拒 / 未预期异常 → 非零退出码（GUI 据此显示异常，而不是“已完成”）
    sys.exit(1 if result in ("locked", "error") else 0)
