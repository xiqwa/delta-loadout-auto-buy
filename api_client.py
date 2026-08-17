# -*- coding: utf-8 -*-
"""轻量共享 HTTP 客户端：配装 API 与装备图片下载。"""
import time

import requests


LOADOUT_URL = "https://orzice.com/workApi/v1/sjz_api/jzv4_zb"
DEFAULT_TIMEOUT = (5, 15)

_session = requests.Session()


def _backoff(attempt, base=0.35, cap=2.5):
    time.sleep(min(cap, base * (2 ** attempt)))


def _notify(logger, message):
    if logger:
        logger(message)


def get_json(url, params=None, retries=3, timeout=DEFAULT_TIMEOUT, logger=None):
    """请求 JSON 接口，返回 (data, error)。HTTP/JSON/接口 code 均重试。"""
    last_error = None
    for attempt in range(retries):
        try:
            resp = _session.get(url, params=params, timeout=timeout)
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
                _notify(logger, f"[API] 第{attempt + 1}/{retries}次 {last_error}")
                _backoff(attempt)
                continue
            data = resp.json()
            if not isinstance(data, dict):
                last_error = "接口返回不是 JSON 对象"
                _notify(logger, f"[API] 第{attempt + 1}/{retries}次 {last_error}")
                _backoff(attempt)
                continue
            if data.get("code") != 0:
                last_error = f"接口错误: {data.get('message')}"
                _notify(logger, f"[API] 第{attempt + 1}/{retries}次 {last_error}")
                _backoff(attempt)
                continue
            return data, None
        except requests.RequestException as exc:
            last_error = f"请求失败: {exc}"
            _notify(logger, f"[API] 第{attempt + 1}/{retries}次 {last_error}")
            _backoff(attempt)
        except ValueError as exc:
            last_error = f"JSON 解析失败: {exc}"
            _notify(logger, f"[API] 第{attempt + 1}/{retries}次 {last_error}")
            _backoff(attempt)
    return None, last_error


def fetch_loadout(lv, token, retries=3, logger=None):
    """拉取指定档位的原始方案组列表，返回 (groups, error)。"""
    data, err = get_json(
        LOADOUT_URL,
        {"lv": lv, "token": token},
        retries=retries,
        logger=logger,
    )
    if err:
        return None, err
    groups = data.get("data", [])
    if not isinstance(groups, list):
        return None, "接口 data 字段不是数组"
    return groups, None


def download_bytes(url, timeout=12, retries=2, logger=None):
    """下载二进制资源（装备缩略图），返回 (bytes, error)。"""
    last_error = None
    for attempt in range(retries):
        try:
            resp = _session.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.content, None
        except Exception as exc:
            last_error = f"{exc}"
            _notify(logger, f"[图片] 第{attempt + 1}/{retries}次 {last_error}")
            _backoff(attempt, base=0.2, cap=1.0)
    return None, last_error
