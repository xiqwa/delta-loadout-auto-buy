# -*- coding: utf-8 -*-
"""只识别购买入口左侧小方框，不执行任何鼠标或键盘操作。"""
import os
import sys

import cv2
import numpy as np

import auto_equip as ae


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(BASE_DIR, "checkbox_detection.png")
# 轮廓评分不是模板相似度；0.55 仅用于标注颜色/日志提示。
THRESHOLD = float(os.environ.get("WEAR_CHECKBOX_THRESHOLD", "0.55"))
REFERENCE_SIZE = (1938.0, 1127.0)
EXPECTED_CENTER = (1499.0, 973.0)
SEARCH_REGION = (0.60, 0.75, 0.90, 0.95)
MAX_POSITION_ERROR = (80.0, 90.0)


def _looks_like_purchase_scene(image):
    """轮廓回退前确认右下区域具备游戏购买界面的文字或暗色结构。"""
    height, width = image.shape[:2]
    roi = (int(width * 0.55), int(height * 0.62),
           int(width * 0.43), int(height * 0.36))
    texts = ae.ocr_region(image, roi)
    joined = "".join(ae.normalize_gear_name(text) for text, conf, _ in texts
                     if conf >= 0.2)
    keywords = ("交易行", "军需处", "购买", "兑换", "价格来源", "装备")
    if any(keyword in joined for keyword in keywords):
        return True, joined
    x, y, w, h = roi
    crop = image[y:y + h, x:x + w]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    # 游戏详情区以低亮度深色面板为主；明亮网页不允许进入轮廓点击回退。
    dark_ratio = float(np.mean(hsv[:, :, 2] < 95))
    return dark_ratio >= 0.55, joined


def _find_market_button_checkbox(image):
    """用交易行价格按钮的青绿色边框定位其左侧灰色方框，不读取价格数字。"""
    height, width = image.shape[:2]
    sx, sy = width / REFERENCE_SIZE[0], height / REFERENCE_SIZE[1]
    x0, y0 = int(width * 0.60), int(height * 0.68)
    x1, y1 = int(width * 0.98), int(height * 0.96)
    roi = image[y0:y1, x0:x1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    # 三角洲交易行按钮边框/价格使用青绿色；只取细边框的颜色，不解析文字。
    # HSV 范围可用环境变量覆盖（游戏主题/画质变化时微调，不必改代码）。
    hsv_low = tuple(int(v) for v in
                    os.environ.get("MARKET_TEAL_LOW", "68,75,65").split(","))
    hsv_high = tuple(int(v) for v in
                     os.environ.get("MARKET_TEAL_HIGH", "105,255,255").split(","))
    teal = cv2.inRange(hsv, np.array(hsv_low, dtype=np.uint8),
                      np.array(hsv_high, dtype=np.uint8))
    teal = cv2.morphologyEx(teal, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (13, 3)))
    contours, _ = cv2.findContours(teal, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    anchors = []
    for contour in contours:
        rx, ry, rw, rh = cv2.boundingRect(contour)
        ref_w, ref_h = rw / sx, rh / sy
        # 价格按钮是右下横向长框；排除单独的价格文字和小图标。
        if not (180 <= ref_w <= 520 and 35 <= ref_h <= 100 and ref_w / ref_h >= 2.8):
            continue
        ax1, ay1, ax2, ay2 = x0 + rx, y0 + ry, x0 + rx + rw, y0 + ry + rh
        anchors.append((cv2.contourArea(contour), (ax1, ay1, ax2, ay2)))
    if not anchors:
        return None

    _, anchor = max(anchors, key=lambda item: (item[1][1], item[0]))
    ax1, ay1, ax2, ay2 = anchor
    # 用户实图：灰框紧贴价格框左边，约 55x70px，中心略低于价格框中心。
    box_w = int(round(58 * sx))
    box_h = int(round(70 * sy))
    bx2 = ax1
    bx1 = max(0, bx2 - box_w)
    by1 = max(0, ay1 + int(round(8 * sy)))
    by2 = min(height, by1 + box_h)
    center = ((bx1 + bx2) // 2, (by1 + by2) // 2)
    return {
        "score": 0.95,
        "box": (bx1, by1, bx2, by2),
        "inner_box": (bx1, by1, bx2, by2),
        "center": center,
        "method": "market-button-border",
        "anchor_box": anchor,
        "search_roi": (x0, y0, x1, y1),
    }


def load_image(path=None):
    if path:
        image = cv2.imread(os.path.abspath(path), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError(f"无法读取截图: {path}")
        return image, f"截图 {os.path.abspath(path)}"

    hwnd = ae.find_game_window()
    if not hwnd:
        raise RuntimeError("未找到游戏窗口")
    import win32gui
    if not win32gui.IsWindow(hwnd):
        raise RuntimeError(f"游戏窗口句柄无效: {hwnd}")
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 300 or height <= 200:
        raise RuntimeError(f"游戏窗口尺寸无效: {width}x{height}")
    return ae.capture_window(hwnd, width, height), f"游戏窗口 hwnd={hwnd} {width}x{height}"


def detect_checkbox(image, api_price=None):
    """在归一化右下区域直接检测近似正方形轮廓；api_price 仅为接口兼容。"""
    del api_price
    if image is None or image.ndim != 3:
        raise RuntimeError("截图为空或格式无效")
    anchored = _find_market_button_checkbox(image)
    if anchored is not None:
        return anchored, [anchored], []
    scene_ok, scene_text = _looks_like_purchase_scene(image)
    if not scene_ok:
        raise RuntimeError(
            f"当前画面不像游戏购买详情页，拒绝轮廓回退（区域OCR={scene_text[:80]!r}）")
    height, width = image.shape[:2]
    sx, sy = width / REFERENCE_SIZE[0], height / REFERENCE_SIZE[1]
    x0, y0 = int(width * SEARCH_REGION[0]), int(height * SEARCH_REGION[1])
    x1, y1 = int(width * SEARCH_REGION[2]), int(height * SEARCH_REGION[3])
    search = image[y0:y1, x0:x1]
    gray = cv2.cvtColor(search, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    hsv = cv2.cvtColor(search, cv2.COLOR_BGR2HSV)

    masks = [
        cv2.Canny(blurred, 40, 120),
        cv2.Canny(blurred, 80, 180),
        cv2.adaptiveThreshold(blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                              cv2.THRESH_BINARY, 15, -3),
        cv2.inRange(hsv, np.array((0, 0, 135), dtype=np.uint8),
                    np.array((179, 150, 255), dtype=np.uint8)),
    ]
    expected_x, expected_y = EXPECTED_CENTER[0] * sx, EXPECTED_CENTER[1] * sy
    raw_candidates = []
    for mask_index, mask in enumerate(masks):
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            rx, ry, rw, rh = cv2.boundingRect(contour)
            ref_w, ref_h = rw / sx, rh / sy
            if not (15 <= ref_w <= 45 and 15 <= ref_h <= 45):
                continue
            ratio = ref_w / max(ref_h, 1e-6)
            if not 0.7 <= ratio <= 1.4:
                continue

            bx1, by1, bx2, by2 = x0 + rx, y0 + ry, x0 + rx + rw, y0 + ry + rh
            cx, cy = bx1 + rw / 2.0, by1 + rh / 2.0
            if (abs(cx - expected_x) > MAX_POSITION_ERROR[0] * sx or
                    abs(cy - expected_y) > MAX_POSITION_ERROR[1] * sy):
                continue
            square_score = max(0.0, 1.0 - abs(1.0 - ratio) / 0.4)
            distance = ((cx - expected_x) / (width * 0.10)) ** 2
            distance += ((cy - expected_y) / (height * 0.09)) ** 2
            position_score = float(np.exp(-0.5 * distance))
            size_score = max(0.0, 1.0 - abs((ref_w + ref_h) / 2.0 - 30.0) / 20.0)

            local = gray[ry:ry + rh, rx:rx + rw]
            edge_local = masks[0][ry:ry + rh, rx:rx + rw]
            border = np.zeros((rh, rw), dtype=np.uint8)
            thickness = max(1, min(rw, rh) // 6)
            cv2.rectangle(border, (0, 0), (rw - 1, rh - 1), 255, thickness)
            inner = cv2.bitwise_not(border)
            border_mean = cv2.mean(local, mask=border)[0]
            inner_mean = cv2.mean(local, mask=inner)[0] if cv2.countNonZero(inner) else border_mean
            contrast_score = min(1.0, abs(border_mean - inner_mean) / 48.0)
            edge_score = min(1.0, cv2.countNonZero(cv2.bitwise_and(edge_local, border)) /
                             max(1.0, cv2.countNonZero(border) * 0.35))
            rectangularity = min(1.0, cv2.contourArea(contour) / max(1.0, rw * rh))
            score = (0.28 * square_score + 0.34 * position_score + 0.12 * size_score +
                     0.10 * contrast_score + 0.10 * edge_score + 0.06 * rectangularity)
            # 轮廓通常是方框内的白色图标；标注/点击区域应覆盖外层灰色方框。
            # 实际灰色选择框约 55-65px；用参考分辨率 32px 半边长覆盖完整外框。
            outer_half = max(32 * sx, 32 * sy)
            outer_box = (max(0, int(round(cx - outer_half))),
                         max(0, int(round(cy - outer_half))),
                         min(width, int(round(cx + outer_half))),
                         min(height, int(round(cy + outer_half))))
            raw_candidates.append({
                "score": float(score), "box": outer_box,
                "inner_box": (bx1, by1, bx2, by2),
                "center": (int(round(cx)), int(round(cy))), "method": "contour",
                "mask": mask_index, "square_score": square_score,
                "position_score": position_score, "contrast_score": contrast_score,
            })

    candidates = []
    for candidate in sorted(raw_candidates, key=lambda item: item["score"], reverse=True):
        cx, cy = candidate["center"]
        if any(abs(cx - old["center"][0]) <= 4 * sx and
               abs(cy - old["center"][1]) <= 4 * sy for old in candidates):
            continue
        candidates.append(candidate)
    if not candidates:
        raise RuntimeError("右下购买入口区域未检测到符合尺寸和宽高比的小方框轮廓")
    best = max(candidates, key=lambda candidate: candidate["score"])
    best["search_roi"] = (x0, y0, x1, y1)
    return best, candidates, []


def save_annotated(image, best):
    output = image.copy()
    x1, y1, x2, y2 = best["box"]
    color = (0, 255, 0) if best["score"] >= THRESHOLD else (0, 0, 255)
    cv2.rectangle(output, (x1, y1), (x2, y2), color, 3)
    cv2.drawMarker(output, best["center"], color, cv2.MARKER_CROSS, 18, 2)
    rx1, ry1, rx2, ry2 = best["search_roi"]
    cv2.rectangle(output, (rx1, ry1), (rx2, ry2), (255, 0, 255), 2)
    label = f"contour score={best['score']:.3f}"
    cv2.putText(output, label, (x1, max(25, y1 - 10)), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, color, 2, cv2.LINE_AA)
    if not cv2.imwrite(OUTPUT_PATH, output):
        raise RuntimeError(f"无法写入标注图: {OUTPUT_PATH}")


def main():
    try:
        args = list(sys.argv[1:])
        if "--price" in args:
            index = args.index("--price")
            del args[index:index + 2]
        image, source = load_image(args[0] if args else None)
        best, candidates, _ = detect_checkbox(image)
        save_annotated(image, best)
    except Exception as exc:
        print(f"[错误] {exc}")
        return 1

    passed = best["score"] >= THRESHOLD
    print(f"[来源] {source}")
    print(f"[方框搜索区] {best['search_roi']}")
    print(f"[候选] 共 {len(candidates)} 个方形轮廓")
    print(f"[最佳] method={best['method']} score={best['score']:.3f} threshold={THRESHOLD:.2f}")
    print(f"[坐标] box={best['box']} center={best['center']}")
    print(f"[结果] {'识别成功' if passed else '分数不足，不可信'}")
    print(f"[标注图] {OUTPUT_PATH}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
