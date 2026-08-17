# 卡战备自动配装 (Delta Force Loadout Auto-Buy)

> 三角洲行动 · 卡战备自动配装购买工具 | 鼠鼠卡战备 V4 API 方案驱动

基于 **MaaFramework** 的三角洲行动卡战备自动配装工具：
- 调用鼠鼠卡战备 V4 API 实时拉取配装方案（不保存本地）
- OCR 识别 + 轮廓检测驱动游戏内自动购买
- 购买顺序：头盔 → 护甲 → 胸挂 → 背包 → 枪械 → 配件

## 功能特性

- 🎯 **磨损档位识别**：全新 / 几乎全新 / 破损 三档（长词优先匹配，不误选）
- 🛒 **自动购买链**：军需处搜名 → 标签校验 → 价格 OCR → 购买 → 回主页验证（失败自动重试）
- 🖥 **GUI 三步向导**：选择战备价值 → 选择配装方案 → 控制台运行
- ⚙️ **边栏设置**：运行模式（预览/试跑/购买）、胸挂背包使用仓库已有
- 🔒 **防重复购买**：购买按钮不可见即视为已成功，绝不二次点击
- 🔒 **防双开**：命令行与 GUI 互斥锁

## 环境要求

- Windows 10/11
- Python 3.11+
- 三角洲行动游戏（窗口模式，参考分辨率 2560×1440 或 1938×1127）

## 安装

```bash
# 1. 安装 Python 依赖
pip install -r requirements.txt

# 2. 配置 API Token（鼠鼠卡战备 V4）
# 方式 A：设置环境变量
set KZB_TOKEN=你的token
# 方式 B：编辑 maa_kazhanbei.py / auto_equip.py 中的 TOKEN 变量
```

## 使用

### GUI 模式（推荐）

```bash
python kbe_app.py
```

1. 选择战备价值（如 5W 基础 / 11W 机密 / 60W 绝密）
2. 选择配装方案（卡片形式，含装备图片）
3. 控制台选择运行模式后开始：

| 模式 | 说明 |
|------|------|
| 预览 | 拉取方案并展示，不操作游戏 |
| 试跑 | 真实导航到购买前停止（推荐先验证） |
| 购买 | 真实执行购买流程 |

### 命令行模式

```bash
# 档位 0（5W），组名过滤，方案序号 1
python maa_kazhanbei.py 0
```

## 关键参数（环境变量）

| 变量 | 默认 | 说明 |
|------|------|------|
| `KZB_TOKEN` | 空 | 鼠鼠卡战备 API Token |
| `SHOP_SCROLL_DELTA` | -360 | 商店滚动步长（漏行设 -240） |
| `SHOP_SCROLL_DELAY` | 0.08 | 滚动间隔秒 |
| `WEAR_CHECKBOX_THRESHOLD` | 0.55 | 磨损小方框检测阈值 |
| `PURCHASE_RETRY_LIMIT` | 3 | 购买验证重试次数 |
| `PREVIEW_MODE` | - | 预览模式（GUI 使用） |
| `DRY_RUN_API` | - | 试跑模式（真实导航购买前停止） |
| `USE_WAREHOUSE_RIG_BAG` | 0 | 胸挂/背包使用仓库已有（跳过购买） |

## 项目结构

```
卡战备自动配装/
├── kbe_app.py              # GUI（customtkinter）
├── maa_kazhanbei.py        # 主流程（MaaFramework 自动化）
├── auto_equip.py           # 识别/购买链（OCR + 轮廓检测）
├── api_client.py           # 鼠鼠卡战备 V4 API 客户端
├── detect_wear_checkbox.py # 磨损小方框检测
├── maa_win_pkg/            # MaaFramework 运行时
├── model/                  # OCR 模型
├── config*.json            # 布局/校准配置
└── 截图/                   # 识别效果截图
```

## 免责声明

- 本工具仅供学习交流使用
- 使用本工具产生的任何后果（封号风险等）由使用者自行承担
- 请勿用于商业用途

## 致谢

- [MaaFramework](https://github.com/MaaXYZ/MaaFramework) - 自动化框架
- 鼠鼠卡战备 V4 API（orzice.com）
