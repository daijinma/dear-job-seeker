"""Yupao request signing. Secrets come from env at call time:
  sign      = sha256(canon(body + timestamp + nonce) + DJS_YUPAO_SIGN_SALT)
  x-yp-sign = md5(DJS_YUPAO_NONCE_KEY + token + x-yp-nonce + VER)
"""
from __future__ import annotations

import hashlib
import json
import random
import string
import time

from .base import env

APPID = "wx31a1c86a67bc6c54"
VER = "9.37.1"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Mac")
REFERER = f"https://servicewechat.com/{APPID}/452/page-frame.html"


def _sort(v):
    if isinstance(v, dict):
        return {k: _sort(v[k]) for k in sorted(v)}
    return [_sort(x) for x in v] if isinstance(v, list) else v


def _js(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


def canon(body: dict) -> str:
    """Key-sorted "k=v&"; objects/arrays/null use compact JSON, primitives JS-style."""
    return "".join(
        f"{k}={json.dumps(v, ensure_ascii=False, separators=(',', ':')) if v is None or isinstance(v, (dict, list)) else _js(v)}&"
        for k, v in _sort(body).items())


def sign(body: dict, ts: str, nonce: int) -> str:
    return hashlib.sha256((canon({**body, "timestamp": ts, "nonce": nonce}) + env("DJS_YUPAO_SIGN_SALT")).encode()).hexdigest()


def yp_sign(token: str, ypnonce: str) -> str:
    return hashlib.md5((env("DJS_YUPAO_NONCE_KEY") + token + ypnonce + VER).encode()).hexdigest()


def headers(body: dict) -> dict:
    ms = int(time.time() * 1000)
    ts, nonce = str(ms), random.randint(0, 999999)
    ypn = "".join(random.choices(string.digits + string.ascii_lowercase, k=10)) + f".{ms}"
    return {
        "business": "YPZP", "hybrid": "NO", "system": "MAC", "wechat_token": APPID, "nonce": str(nonce),
        "time": ts, "timestamp": ts, "channel": "mac", "systemVersion": "OS", "Content-Type": "application/json",
        "os": "MAC", "token": "", "requestType": "form", "platform": "wx_mini", "brand": "apple",
        "runtime": "WX_MINI", "sign": sign(body, ts, nonce), "signversion": "1", "osVersion": "4.1.13.11",
        "uid": "", "mid": "", "versionmini": "1", "trackSeed": "", "packageversion": VER, "version": VER,
        "appVersion": VER, "runtimeversion": VER, "userrole": "2", "unionid": "", "openid": "", "gps": "",
        "packagename": APPID, "system_type": "wechat", "x-yp-nonce": ypn, "x-yp-sign": yp_sign("", ypn),
        "occversion": "2", "token_time": "", "reqsource": "YPZP", "uuid": "", "refid": "", "model": "Mac17,2",
        "User-Agent": UA, "Referer": REFERER, "Accept-Encoding": "gzip",
    }
