# 卡战备自动配装

三角洲行动的卡战备自动配装工具。基于 MaaFramework 自动化框架,调用配装方案 API 实时拉取方案,自动在游戏内完成购买。

## 功能

- 自动拉取配装方案(实时,不存本地)
- 游戏内自动购买:军需处搜名 → 标签校验 → 价格识别 → 购买验证,失败自动重试
- 磨损档位识别:全新 / 几乎全新 / 破损
- 防重复购买:购买按钮不可见即视为成功,不会二次点击
- 防双开:命令行和 GUI 互斥
- 胸挂/背包可选"使用仓库已有",只买缺的

## 环境

- Windows 10/11
- Python 3.11+
- 三角洲行动游戏(窗口模式,2560×1440 或 1938×1127)

## 安装

```bash
pip install -r requirements.txt
```

配置 API Token(需自行获取):

```bash
set KZB_TOKEN=你的token
```

也可以在 GUI 开始页填入保存。

## 使用

GUI 模式:

```bash
python kbe_app.py
```

流程:开始页填 Token → 选战备价值 → 选方案 → 控制台运行。

命令行模式:

```bash
python maa_kazhanbei.py 0
```

运行模式:预览(只拉方案)、试跑(导航到购买前停)、购买(真实购买)。建议先试跑一次。

## 参数(环境变量)

| 变量 | 默认 | 说明 |
|------|------|------|
| `KZB_TOKEN` | 空 | API Token |
| `SHOP_SCROLL_DELTA` | -360 | 商店滚动步长 |
| `SHOP_SCROLL_DELAY` | 0.08 | 滚动间隔秒 |
| `WEAR_CHECKBOX_THRESHOLD` | 0.55 | 磨损检测阈值 |
| `PURCHASE_RETRY_LIMIT` | 3 | 购买重试次数 |
| `USE_WAREHOUSE_RIG_BAG` | 0 | 胸挂背包用仓库已有 |

## 项目结构

```
├── kbe_app.py              # GUI
├── maa_kazhanbei.py        # 主流程
├── auto_equip.py           # 识别/购买
├── api_client.py           # API 客户端
├── detect_wear_checkbox.py # 磨损检测
├── maa_win_pkg/            # MaaFramework 运行时
├── model/                  # OCR 模型
└── 截图/                   # 识别效果截图
```

## 免责声明

仅供学习交流,使用后果自负。请勿用于商业用途。
