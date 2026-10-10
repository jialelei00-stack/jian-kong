"""运行时共享的辅助函数：限速、客户端 IP、区域规范化等。
拆分到这里是为了避免 main.py <-> routers/auth.py 之间的循环 import。
"""
import re
import threading
import time
from collections import deque
from typing import Optional

from fastapi import Request


# ── 标准区域划分（与「区域划分表.md」一致，6 大区 / 47 细分区域） ──
REGIONS = {
    "华东区": ["安徽AB区", "安徽CD区", "江苏AB区", "上海市", "苏北CD区", "苏南CD区", "浙东区", "浙西区"],
    "华北区": ["北京市", "河北AB区", "河北CD区", "河南AB区", "山东AB区", "山西省", "天津市", "豫北CD区", "豫南CD区", "鲁东CD区", "鲁西CD区"],
    "华南区": ["海南省", "湖北AB区", "湖北CD区", "湖南AB区", "湖南CD区", "江西省", "闽北区", "闽南区", "粤东A区", "粤东B区", "粤西A区", "粤西B区"],
    "东北区": ["黑龙江省", "吉林省", "辽北区", "辽南区", "内蒙古"],
    "西一区": ["甘青宁藏", "陕西AB区", "陕西CD区", "四川AB区", "四川CD区", "新疆省", "重庆AB区", "重庆CD区"],
    "西二区": ["贵州省", "桂北区", "桂南区", "云南省"],
}

# 大区名按长度倒序，避免「西一区」先于「西二区」之类的短名把长名前缀吃掉
_BIG_REGIONS_BY_LEN = sorted(REGIONS, key=len, reverse=True)


def normalize_region(region: Optional[str]) -> str:
    """把区域串统一成「大区 / 细分区域」（斜杠两侧各一个空格）。

    历史数据里同一个区域存在三种写法：「西二区 / 云南省」「西二区/云南省」
    「西二区云南省」。榜单、驾驶舱都按 region 原串分组，于是同一个区域被拆成
    好几条并列显示；更严重的是大区名按「/」前段截取，没有斜杠时整串会被当成
    大区，凭空多出一个「西二区云南省」幻影大区。这里统一收口所有写入入口。

    认不出来的值原样返回：宁可保留，也不替用户编造一个区域。
    """
    r = (region or "").strip().replace("／", "/")
    if not r:
        return ""
    r = re.sub(r"\s*/\s*", " / ", r)
    if " / " in r:
        big, sub = r.split(" / ", 1)
        return f"{big.strip()} / {sub.strip()}"
    # 没有分隔符：按已知大区前缀 + 小区后缀还原（如「西二区云南省」→「西二区 / 云南省」）
    for big in _BIG_REGIONS_BY_LEN:
        if r.startswith(big) and len(r) > len(big):
            sub = r[len(big):].strip()
            if sub in REGIONS[big]:
                return f"{big} / {sub}"
    return r


# ── 客户端 IP（兼容反代） ──
def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return (request.client.host if request.client else "unknown")


# ── 通用滑窗限速 ──
_RATELIMIT_LOCK = threading.Lock()
# key -> deque[float]
_RATELIMIT_BUCKETS: dict = {}


def hit_rl(key: str, limit: int, window_s: int) -> bool:
    """滑窗：当前 key 在 window_s 内累计 ≤ limit 次则放行并计数，否则拒绝。"""
    now = time.time()
    cutoff = now - window_s
    with _RATELIMIT_LOCK:
        dq = _RATELIMIT_BUCKETS.get(key)
        if dq is None:
            dq = deque()
            _RATELIMIT_BUCKETS[key] = dq
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


# ── 登录失败计数（按 ip + username 维度） ──
_LOGIN_FAIL: dict = {}
_LOGIN_LOCK = threading.Lock()
LOGIN_LOCK_WINDOW_S = 300
LOGIN_LOCK_THRESHOLD = 5


def _login_attempts(ip: str, username: str) -> list:
    now = time.time()
    with _LOGIN_LOCK:
        dq = _LOGIN_FAIL.get((ip, username))
        if dq is None:
            dq = deque()
            _LOGIN_FAIL[(ip, username)] = dq
        # 清理窗口外的记录
        while dq and now - dq[0][0] > LOGIN_LOCK_WINDOW_S:
            dq.popleft()
        return list(dq)


def record_login(ip: str, username: str, success: bool):
    attempts = _login_attempts(ip, username)
    attempts.append((time.time(), not success))
    with _LOGIN_LOCK:
        dq = _LOGIN_FAIL.get((ip, username))
        dq.clear()
        dq.extend(attempts)


def login_locked(ip: str, username: str) -> bool:
    attempts = _login_attempts(ip, username)
    fails = sum(1 for _, f in attempts if f)
    return fails >= LOGIN_LOCK_THRESHOLD
