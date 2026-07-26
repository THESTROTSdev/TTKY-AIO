import os
import asyncio
import aiohttp
import random
import re
import time
import threading
import logging
import sqlite3
import json
import bcrypt
import jwt
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from dataclasses import dataclass
from collections import deque
import psutil
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Depends, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import uvicorn

# ---------- PURE PYTHON X-GORGON GENERATOR ----------
def generate_x_gorgon(params: dict, data: dict) -> str:
    sorted_params = sorted(params.items())
    query_parts = [f"{k}={v}" for k, v in sorted_params]
    query_string = '&'.join(query_parts)
    sorted_data = sorted(data.items())
    body_parts = [f"{k}={v}" for k, v in sorted_data]
    body_string = '&'.join(body_parts)
    path = "/aweme/v1/aweme/stats"
    khronos = str(int(time.time()))
    sign_str = path + '?' + query_string + body_string + khronos
    key = "0123456789ABCDEF"
    hmac_digest = hmac.new(key.encode(), sign_str.encode(), hashlib.sha1).digest()
    md5_timestamp = hashlib.md5(khronos.encode()).hexdigest()
    combined = hmac_digest.hex() + md5_timestamp
    x_gorgon = (combined + "0"*64)[:64].upper()
    return x_gorgon

# ---------- CONFIG ----------
SECRET_KEY = "change-this-in-production-use-env-var"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 1440

# ---------- SUPPRESS NOISY LOGS ----------
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logger = logging.getLogger("ttky")
logger.setLevel(logging.INFO)

# ---------- DATABASE ----------
DB_PATH = "users.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ---------- AUTH ----------
def get_user_by_username(username: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, username, password_hash FROM users WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "username": row[1], "password_hash": row[2]}
    return None

def create_user(username: str, password: str):
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (username, hashed))
        conn.commit()
        user_id = c.lastrowid
        conn.close()
        return {"id": user_id, "username": username}
    except sqlite3.IntegrityError:
        conn.close()
        return None

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode('utf-8'), hashed.encode('utf-8'))

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None

security = HTTPBearer()

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user = get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user

# ---------- TIKTOK CONFIG ----------
HOST = "api19.tiktokv.com"
DEADLINE = 3
REFRESHRATE = 0.20
MAX_THREADS = 100
GLOBAL_MAX_CONCURRENT = 200
BUILDS = [247, 312, 322, 357, 358, 415, 422, 444, 466]
MAX_AMOUNT = 250
ORDER_PREFIX = "TTKY"

# ---------- PROXY MANAGER ----------
PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks4&timeout=10000&country=all&ssl=all&anonymity=all",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5&timeout=10000&country=all&ssl=all&anonymity=all",
]
PROXY_REFRESH_INTERVAL = 300

class ProxyManager:
    def __init__(self):
        self.proxies: List[str] = []
        self.lock = threading.Lock()
        self.last_update = 0

    async def fetch_proxies(self) -> List[str]:
        new_proxies = set()
        async with aiohttp.ClientSession() as session:
            for url in PROXY_SOURCES:
                try:
                    async with session.get(url, timeout=10) as resp:
                        if resp.status == 200:
                            text = await resp.text()
                            for line in text.splitlines():
                                line = line.strip()
                                if line and ':' in line:
                                    parts = line.split(':')
                                    if len(parts) == 2 and parts[0].replace('.', '').isdigit() and parts[1].isdigit():
                                        new_proxies.add(line)
                except Exception as e:
                    logger.warning(f"Failed to fetch proxies from {url}: {e}")
        return list(new_proxies)

    async def refresh(self):
        logger.info("Refreshing proxy pool...")
        new_list = await self.fetch_proxies()
        with self.lock:
            if new_list:
                self.proxies = new_list
                self.last_update = time.time()
                logger.info(f"Loaded {len(self.proxies)} proxies")
            else:
                logger.warning("No new proxies, keeping old ones")

    def get_random_proxy(self) -> Optional[str]:
        with self.lock:
            if not self.proxies:
                return None
            return random.choice(self.proxies)

    def get_proxy_url(self, proxy: str) -> str:
        if '@' in proxy:
            return f"http://{proxy}"
        else:
            return f"http://{proxy}"

PROXY_MANAGER = ProxyManager()

# ---------- DEVICE MODELS ----------
@dataclass
class DeviceModel:
    model: str
    brand: str
    res: str
    dpi: str

ANDROID_MODELS = [
    DeviceModel("SM-F926B", "samsung", "1080x2400", "480"),
    DeviceModel("SM-G998B", "samsung", "1440x3200", "560"),
    DeviceModel("SM-G991B", "samsung", "1080x2400", "420"),
    DeviceModel("SM-A536B", "samsung", "1080x2400", "400"),
    DeviceModel("SM-A546B", "samsung", "1080x2400", "400"),
    DeviceModel("SM-S918B", "samsung", "1440x3120", "550"),
    DeviceModel("SM-G781B", "samsung", "1080x2400", "420"),
    DeviceModel("Pixel 7", "google", "1080x2400", "440"),
    DeviceModel("Pixel 6", "google", "1080x2400", "440"),
    DeviceModel("Pixel 8", "google", "1080x2400", "480"),
    DeviceModel("Pixel 8 Pro", "google", "1440x3120", "550"),
    DeviceModel("Pixel 7a", "google", "1080x2400", "440"),
    DeviceModel("Redmi Note 12", "xiaomi", "1080x2400", "440"),
    DeviceModel("Redmi Note 11", "xiaomi", "1080x2400", "400"),
    DeviceModel("Redmi Note 13", "xiaomi", "1080x2400", "440"),
    DeviceModel("POCO F5", "xiaomi", "1080x2400", "440"),
    DeviceModel("POCO F6", "xiaomi", "1080x2400", "440"),
    DeviceModel("POCO X5", "xiaomi", "1080x2400", "440"),
    DeviceModel("OnePlus 11", "oneplus", "1440x3216", "525"),
    DeviceModel("OnePlus 10", "oneplus", "1440x3216", "525"),
    DeviceModel("OnePlus 12", "oneplus", "1440x3216", "525"),
    DeviceModel("OnePlus 9", "oneplus", "1440x3216", "525"),
    DeviceModel("OPPO Find X5", "oppo", "1080x2400", "450"),
    DeviceModel("OPPO Find X6", "oppo", "1080x2400", "450"),
    DeviceModel("OPPO Reno 8", "oppo", "1080x2400", "450"),
    DeviceModel("vivo X80", "vivo", "1080x2400", "440"),
    DeviceModel("vivo X90", "vivo", "1080x2400", "440"),
    DeviceModel("vivo Y78", "vivo", "1080x2400", "400"),
    DeviceModel("Honor 70", "honor", "1080x2400", "430"),
    DeviceModel("Honor 90", "honor", "1080x2400", "430"),
    DeviceModel("Honor Magic5", "honor", "1080x2400", "430"),
    DeviceModel("Nothing Phone (2)", "nothing", "1080x2400", "440"),
    DeviceModel("Nothing Phone (1)", "nothing", "1080x2400", "440"),
    DeviceModel("Realme GT 2", "realme", "1080x2400", "440"),
    DeviceModel("Realme 11", "realme", "1080x2400", "400"),
    DeviceModel("Motorola Edge 40", "motorola", "1080x2400", "440"),
    DeviceModel("Motorola Razr 40", "motorola", "1080x2400", "440"),
    DeviceModel("Sony Xperia 1 V", "sony", "1080x2400", "440"),
    DeviceModel("Sony Xperia 5 V", "sony", "1080x2400", "440"),
    DeviceModel("Nokia X30", "nokia", "1080x2400", "400"),
    DeviceModel("Nokia G60", "nokia", "1080x2400", "400"),
]

IOS_MODELS = [
    DeviceModel("iPhone14,5", "apple", "1170x2532", "460"),
    DeviceModel("iPhone14,2", "apple", "1170x2532", "460"),
    DeviceModel("iPhone14,3", "apple", "1170x2532", "460"),
    DeviceModel("iPhone14,4", "apple", "1170x2532", "460"),
    DeviceModel("iPhone14,6", "apple", "1170x2532", "460"),
    DeviceModel("iPhone14,7", "apple", "1170x2532", "460"),
    DeviceModel("iPhone14,8", "apple", "1284x2778", "458"),
    DeviceModel("iPad13,4", "apple", "2048x2732", "320"),
    DeviceModel("iPad13,1", "apple", "1640x2360", "264"),
    DeviceModel("iPad13,2", "apple", "1640x2360", "264"),
    DeviceModel("iPad13,10", "apple", "2388x1668", "264"),
    DeviceModel("iPad13,11", "apple", "2388x1668", "264"),
]

LAPTOP_MODELS = [
    DeviceModel("MacBookPro18,1", "apple", "2560x1600", "220"),
    DeviceModel("MacBookPro18,2", "apple", "3024x1964", "254"),
    DeviceModel("MacBookPro18,3", "apple", "3456x2234", "254"),
    DeviceModel("MacBookPro18,4", "apple", "3024x1964", "254"),
    DeviceModel("MacBookAir10,1", "apple", "2560x1600", "227"),
    DeviceModel("XPS-15-9520", "dell", "1920x1200", "180"),
    DeviceModel("XPS-13-9310", "dell", "1920x1200", "166"),
    DeviceModel("ThinkPad-X1-Carbon-Gen10", "lenovo", "1920x1200", "170"),
    DeviceModel("ThinkPad-X1-Carbon-Gen9", "lenovo", "1920x1200", "170"),
    DeviceModel("Surface-Laptop-5", "microsoft", "2256x1504", "201"),
    DeviceModel("Surface-Pro-9", "microsoft", "2688x1920", "267"),
    DeviceModel("HP-Spectre-x360", "hp", "1920x1280", "166"),
    DeviceModel("ASUS-ZenBook", "asus", "1920x1200", "166"),
]

TIMEZONES = [
    "Europe/Paris", "Europe/London", "Europe/Berlin", "Europe/Rome",
    "Europe/Madrid", "Europe/Amsterdam", "Europe/Stockholm", "Europe/Warsaw",
    "Europe/Prague", "Europe/Vienna", "Europe/Zurich", "Europe/Brussels",
    "Asia/Kolkata", "Asia/Tokyo", "Asia/Shanghai", "Asia/Hong_Kong",
    "Asia/Singapore", "Asia/Seoul", "Asia/Bangkok", "Asia/Jakarta",
    "Asia/Manila", "Asia/Kuala_Lumpur", "Asia/Taipei", "Asia/Dubai",
    "America/New_York", "America/Los_Angeles", "America/Chicago", "America/Denver",
    "America/Phoenix", "America/Toronto", "America/Mexico_City", "America/Sao_Paulo",
    "America/Buenos_Aires", "America/Lima", "Africa/Cairo", "Africa/Lagos",
    "Africa/Johannesburg", "Africa/Nairobi", "Africa/Casablanca",
    "Australia/Sydney", "Australia/Melbourne", "Australia/Perth",
    "Australia/Brisbane", "Australia/Adelaide", "Pacific/Auckland",
    "Pacific/Fiji", "Pacific/Guam", "Pacific/Honolulu",
]

REGIONS = [
    "US", "CA", "GB", "DE", "FR", "IT", "ES", "NL", "SE", "NO", "DK", "FI",
    "PL", "CZ", "AT", "CH", "BE", "IN", "CN", "JP", "KR", "SG", "TH", "ID",
    "MY", "PH", "VN", "TW", "HK", "AE", "SA", "IL", "BR", "AR", "MX", "CO",
    "PE", "CL", "VE", "EC", "BO", "UY", "PY", "ZA", "EG", "NG", "KE", "MA",
    "GH", "TN", "DZ", "AO", "ET", "TZ", "UG", "AU", "NZ", "FJ", "PG", "GU",
    "HI", "SB", "VU", "NC", "PF", "RU", "TR", "UA", "KZ", "UZ", "KG", "TJ",
    "TM", "GE", "AM", "AZ",
]

def rand_digits(n: int) -> str:
    return ''.join(random.choice('0123456789') for _ in range(n))

def rand_hex(n: int) -> str:
    return ''.join(random.choice('0123456789abcdef') for _ in range(n))

def generate_uuid() -> str:
    u = []
    for i in range(36):
        if i in (8, 13, 18, 23):
            u.append('-')
        else:
            u.append(random.choice('0123456789abcdef'))
    return ''.join(u)

def generate_device() -> Dict:
    dt = random.randint(0, 2)
    if dt == 0:
        m = random.choice(ANDROID_MODELS)
        model, brand, res, dpi = m.model, m.brand, m.res, m.dpi
        osv = random.choice(["10", "11", "12", "13", "14"])
        ua = f"com.ss.android.ugc.trill/300804 (Linux; U; Android {osv}; en_US; {model}; Build/RP1A.200720.012; Cronet/58.0.2991.0)"
        ch = "googleplay"
    elif dt == 1:
        m = random.choice(IOS_MODELS)
        model, brand, res, dpi = m.model, m.brand, m.res, m.dpi
        osv = random.choice(["14.8", "15.6", "16.2", "17.0"])
        ua = f"Trill/300804 (iOS {osv})"
        ch = "appstore"
    else:
        m = random.choice(LAPTOP_MODELS)
        model, brand, res, dpi = m.model, m.brand, m.res, m.dpi
        osv = random.choice(["10", "11", "12"])
        ua = f"Mozilla/5.0 ({brand}; OS {osv})"
        ch = "desktop"
    return {
        "device_id": rand_digits(19),
        "iid": rand_digits(19),
        "device_type": model,
        "device_brand": brand,
        "cdid": generate_uuid() + rand_digits(6),
        "openudid": rand_hex(20),
        "os_version": osv,
        "version_code": "300804",
        "version_name": "3.0.1.0",
        "update_version_code": "300804",
        "manifest_version_code": "3008040",
        "resolution": res,
        "dpi": dpi,
        "aid": "1180",
        "channel": ch,
        "app_name": "trill",
        "region": random.choice(REGIONS),
        "sys_region": random.choice(REGIONS),
        "carrier_region": random.choice(REGIONS),
        "timezone_name": random.choice(TIMEZONES),
        "timezone_offset": random.randint(0, 10000),
        "user_agent": ua,
        "device_token": rand_hex(32),
        "report_token": rand_hex(32),
    }

class DevicePool:
    def __init__(self, size=50000):
        self.pool = [generate_device() for _ in range(size)]
        self.index = 0
        self.lock = threading.Lock()
    def get(self) -> Dict:
        with self.lock:
            d = self.pool[self.index % len(self.pool)]
            self.index += 1
            return d

DEV_POOL = DevicePool()

class TikTokStats:
    def __init__(self):
        self.sent = 0
        self.fail = 0
        self.timeout = 0
        self.error = 0
        self.lock = threading.Lock()
    def add_sent(self):
        with self.lock: self.sent += 1
    def add_fail(self):
        with self.lock: self.fail += 1
    def add_timeout(self):
        with self.lock: self.timeout += 1
    def add_error(self):
        with self.lock: self.error += 1

GLOBAL_SEM = asyncio.Semaphore(GLOBAL_MAX_CONCURRENT)

# ---------- SEND VIEW / SHARE ----------
async def send_view(session: aiohttp.ClientSession, vid: str, stats: TikTokStats, proxy: Optional[str] = None):
    build = random.choice(BUILDS)
    d = DEV_POOL.get()
    osv = str(random.randint(5, 12))
    params = {
        "app_language": "fr",
        "iid": d["iid"],
        "device_id": d["device_id"],
        "channel": d["channel"],
        "device_type": d["device_type"],
        "ac": "wifi",
        "os_version": osv,
        "version_code": str(build),
        "app_name": d["app_name"],
        "device_brand": d["device_brand"],
        "ssmix": "a",
        "device_platform": "android",
        "aid": "1180",
        "as": "a1iosdfgh",
        "cp": "androide1",
    }
    data_payload = {
        "manifest_version_code": str(build),
        "update_version_code": str(build) + "0",
        "play_delta": "1",
        "item_id": vid,
        "version_code": str(build),
        "aweme_type": "0",
    }
    x_gorgon = generate_x_gorgon(params, data_payload)
    khronos = str(int(time.time()))
    headers = {
        "Host": HOST,
        "Connection": "keep-alive",
        "Accept-Encoding": "gzip",
        "X-SS-REQ-TICKET": str(int(time.time() * 1000)),
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": d["user_agent"],
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "application/json",
        "X-Forwarded-For": f"{random.randint(1,255)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(0,255)}",
        "X-Khronos": khronos,
        "X-Gorgon": x_gorgon,
    }
    url = f"https://{HOST}/aweme/v1/aweme/stats"
    proxy_url = PROXY_MANAGER.get_proxy_url(proxy) if proxy else None
    try:
        async with GLOBAL_SEM:
            async with session.post(url, params=params, data=data_payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=DEADLINE),
                                    proxy=proxy_url) as resp:
                text = await resp.text()
                if resp.status == 200:
                    try:
                        json_resp = json.loads(text)
                        if json_resp.get("status_code") == 0:
                            stats.add_sent()
                        else:
                            stats.add_fail()
                    except:
                        stats.add_sent()
                else:
                    stats.add_fail()
    except asyncio.TimeoutError:
        stats.add_timeout()
    except Exception:
        stats.add_error()

async def send_share(session: aiohttp.ClientSession, vid: str, stats: TikTokStats, proxy: Optional[str] = None):
    build = random.choice(BUILDS)
    d = DEV_POOL.get()
    osv = str(random.randint(5, 12))
    params = {
        "app_language": "fr",
        "iid": d["iid"],
        "device_id": d["device_id"],
        "channel": d["channel"],
        "device_type": d["device_type"],
        "ac": "wifi",
        "os_version": osv,
        "version_code": str(build),
        "app_name": d["app_name"],
        "device_brand": d["device_brand"],
        "ssmix": "a",
        "device_platform": "android",
        "aid": "1180",
        "as": "a1iosdfgh",
        "cp": "androide1",
    }
    data_payload = {
        "manifest_version_code": str(build),
        "update_version_code": str(build) + "0",
        "share_delta": "1",
        "item_id": vid,
        "version_code": str(build),
        "aweme_type": "0",
    }
    x_gorgon = generate_x_gorgon(params, data_payload)
    khronos = str(int(time.time()))
    headers = {
        "Host": HOST,
        "Connection": "keep-alive",
        "Accept-Encoding": "gzip",
        "X-SS-REQ-TICKET": str(int(time.time() * 1000)),
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": d["user_agent"],
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "application/json",
        "X-Forwarded-For": f"{random.randint(1,255)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(0,255)}",
        "X-Khronos": khronos,
        "X-Gorgon": x_gorgon,
    }
    url = f"https://{HOST}/aweme/v1/aweme/stats"
    proxy_url = PROXY_MANAGER.get_proxy_url(proxy) if proxy else None
    try:
        async with GLOBAL_SEM:
            async with session.post(url, params=params, data=data_payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=DEADLINE),
                                    proxy=proxy_url) as resp:
                text = await resp.text()
                if resp.status == 200:
                    try:
                        json_resp = json.loads(text)
                        if json_resp.get("status_code") == 0:
                            stats.add_sent()
                        else:
                            stats.add_fail()
                    except:
                        stats.add_sent()
                else:
                    stats.add_fail()
    except asyncio.TimeoutError:
        stats.add_timeout()
    except Exception:
        stats.add_error()

async def run_tiktok_views(vid: str, target: int, threads: int = 50, callback=None, stop_flag=None) -> int:
    stats = TikTokStats()
    sem = asyncio.Semaphore(threads)
    connector = aiohttp.TCPConnector(limit=threads, limit_per_host=threads)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = []
        start = time.time()
        running = True

        async def worker():
            nonlocal running
            while running and stats.sent < target:
                if stop_flag and stop_flag():
                    running = False
                    break
                async with sem:
                    proxy = PROXY_MANAGER.get_random_proxy()
                    await send_view(session, vid, stats, proxy)

        for _ in range(threads):
            tasks.append(asyncio.create_task(worker()))

        while running and stats.sent < target:
            await asyncio.sleep(REFRESHRATE)
            elapsed = time.time() - start
            rate = stats.sent / elapsed if elapsed > 0 else 0
            if callback:
                callback(stats.sent, target, rate)
            if stop_flag and stop_flag():
                running = False
                break
            if stats.sent >= target:
                running = False
                break

        running = False
        await asyncio.gather(*tasks, return_exceptions=True)

    return stats.sent

async def run_tiktok_shares(vid: str, target: int, threads: int = 50, callback=None, stop_flag=None) -> int:
    stats = TikTokStats()
    sem = asyncio.Semaphore(threads)
    connector = aiohttp.TCPConnector(limit=threads, limit_per_host=threads)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = []
        start = time.time()
        running = True

        async def worker():
            nonlocal running
            while running and stats.sent < target:
                if stop_flag and stop_flag():
                    running = False
                    break
                async with sem:
                    proxy = PROXY_MANAGER.get_random_proxy()
                    await send_share(session, vid, stats, proxy)

        for _ in range(threads):
            tasks.append(asyncio.create_task(worker()))

        while running and stats.sent < target:
            await asyncio.sleep(REFRESHRATE)
            elapsed = time.time() - start
            rate = stats.sent / elapsed if elapsed > 0 else 0
            if callback:
                callback(stats.sent, target, rate)
            if stop_flag and stop_flag():
                running = False
                break
            if stats.sent >= target:
                running = False
                break

        running = False
        await asyncio.gather(*tasks, return_exceptions=True)

    return stats.sent

class SystemMonitor:
    @staticmethod
    def is_safe() -> bool:
        try:
            cpu = psutil.cpu_percent(interval=0.2)
            mem = psutil.virtual_memory().percent
            return cpu < 85 and mem < 90
        except:
            return True

class RateLimiter:
    def __init__(self, max_requests: int, window: int = 1):
        self.max_requests = max_requests
        self.window = window
        self.requests: Dict[str, deque] = {}
        self.lock = threading.Lock()

    def is_allowed(self, client_ip: str) -> bool:
        with self.lock:
            now = time.time()
            if client_ip not in self.requests:
                self.requests[client_ip] = deque()
            while self.requests[client_ip] and self.requests[client_ip][0] < now - self.window:
                self.requests[client_ip].popleft()
            if len(self.requests[client_ip]) < self.max_requests:
                self.requests[client_ip].append(now)
                return True
            return False

rate_limiter = RateLimiter(20)

# ---------- JOB SYSTEM ----------
class ViewJob:
    def __init__(self, vid: str, target: int, client_ip: str, job_type: str = "views", user_id: int = None):
        self.vid = vid
        self.target = target
        self.client_ip = client_ip
        self.job_type = job_type
        self.user_id = user_id
        self.sent = 0
        self.status = "queued"
        self.error = None
        self.start_time = None
        self.elapsed = 0
        self.speed = 0
        self.cancelled = False
        self.created_at = time.time()

job_queue = asyncio.Queue()
job_store: Dict[str, ViewJob] = {}
job_id_counter = 0
job_lock = asyncio.Lock()
worker_running = False

def generate_order_id() -> str:
    global job_id_counter
    job_id_counter += 1
    return f"{ORDER_PREFIX}-{job_id_counter:05d}"

async def job_worker():
    global worker_running
    worker_running = True
    while worker_running:
        try:
            try:
                order_id, job = await asyncio.wait_for(job_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if job is None:
                break

            job.status = "running"
            job.start_time = time.time()
            logger.info(f"Order {order_id} ({job.job_type}) started for video {job.vid}, target {job.target}")

            if not SystemMonitor.is_safe():
                job.status = "error"
                job.error = "System overloaded"
                job_store[order_id] = job
                continue

            if not rate_limiter.is_allowed(job.client_ip):
                job.status = "error"
                job.error = "Rate limit exceeded"
                job_store[order_id] = job
                continue

            threads = min(50, job.target)
            def update_callback(sent, target, rate):
                job.sent = sent
                job.speed = int(rate)
                job_store[order_id] = job

            def stop_flag():
                return job.cancelled

            if job.job_type == "views":
                sent = await run_tiktok_views(job.vid, job.target, threads, update_callback, stop_flag)
            elif job.job_type == "shares":
                sent = await run_tiktok_shares(job.vid, job.target, threads, update_callback, stop_flag)
            else:
                job.status = "error"
                job.error = "Unknown job type"
                job_store[order_id] = job
                continue

            job.sent = sent
            job.elapsed = time.time() - job.start_time
            job.speed = int(sent / job.elapsed) if job.elapsed > 0 else 0
            if job.cancelled:
                job.status = "cancelled"
            else:
                job.status = "completed" if sent >= job.target else "partial"
            job_store[order_id] = job
            logger.info(f"Order {order_id} finished: sent {sent}/{job.target} in {job.elapsed:.1f}s")

        except Exception as e:
            if 'order_id' in locals() and 'job' in locals():
                job.status = "error"
                job.error = str(e)
                job_store[order_id] = job
                logger.error(f"Order {order_id} error: {e}")
            else:
                logger.error(f"Worker error: {e}")

# ---------- FASTAPI APP ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    async def proxy_updater():
        while True:
            await PROXY_MANAGER.refresh()
            await asyncio.sleep(PROXY_REFRESH_INTERVAL)
    asyncio.create_task(proxy_updater())
    asyncio.create_task(job_worker())
    logger.info("Background worker and proxy updater started.")
    yield
    global worker_running
    worker_running = False
    await asyncio.sleep(0.5)
    logger.info("Background worker stopped.")

app = FastAPI(lifespan=lifespan)

# ---------- AUTH ENDPOINTS ----------
@app.post("/register")
async def register(request: Request):
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required")
    if len(username) < 3 or len(password) < 6:
        raise HTTPException(status_code=400, detail="Username min 3 chars, password min 6")
    user = create_user(username, password)
    if not user:
        raise HTTPException(status_code=400, detail="Username already exists")
    return {"status": "ok", "message": "User created"}

@app.post("/login")
async def login(request: Request):
    data = await request.json()
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    user = get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(data={"sub": user["username"]})
    return {"access_token": token, "token_type": "bearer", "username": user["username"]}

# ---------- PROTECTED ENDPOINTS ----------
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_TEMPLATE

@app.get("/orders_summary", dependencies=[Depends(get_current_user)])
async def orders_summary(user=Depends(get_current_user)):
    total = len(job_store)
    running = sum(1 for j in job_store.values() if j.status == "running")
    completed = sum(1 for j in job_store.values() if j.status == "completed")
    cancelled = sum(1 for j in job_store.values() if j.status == "cancelled")
    return {"total": total, "running": running, "completed": completed, "cancelled": cancelled}

@app.get("/active_orders", dependencies=[Depends(get_current_user)])
async def active_orders(user=Depends(get_current_user)):
    orders = []
    for oid, job in job_store.items():
        orders.append({
            "order_id": oid,
            "vid": job.vid,
            "job_type": job.job_type,
            "target": job.target,
            "sent": job.sent,
            "status": job.status,
            "speed": job.speed,
            "elapsed": job.elapsed,
            "created_at": job.created_at
        })
    orders.sort(key=lambda x: x['created_at'], reverse=True)
    return orders[:50]

@app.get("/proxy_count", dependencies=[Depends(get_current_user)])
async def proxy_count(user=Depends(get_current_user)):
    with PROXY_MANAGER.lock:
        return {"count": len(PROXY_MANAGER.proxies)}

@app.post("/send_views", dependencies=[Depends(get_current_user)])
async def send_views(request: Request, data: dict, user=Depends(get_current_user)):
    client_ip = request.client.host
    video = data.get('video', '').strip()
    amount = data.get('amount', 250)
    if not video:
        raise HTTPException(status_code=400, detail="Video ID/URL required")
    vid_match = re.search(r'(\d{10,30})', video)
    if vid_match:
        vid = vid_match.group(1)
    elif video.isdigit() and len(video) >= 10:
        vid = video
    else:
        raise HTTPException(status_code=400, detail="Invalid video ID or URL")
    if amount < 1:
        amount = 1
    elif amount > MAX_AMOUNT:
        amount = MAX_AMOUNT

    if not rate_limiter.is_allowed(client_ip):
        return JSONResponse({"status": "error", "message": "Rate limit exceeded. Please wait."}, status_code=429)

    order_id = generate_order_id()
    job = ViewJob(vid, amount, client_ip, job_type="views", user_id=user["id"])
    job_store[order_id] = job
    await job_queue.put((order_id, job))
    return {"order_id": order_id, "status": "queued", "message": "Order queued"}

@app.post("/send_shares", dependencies=[Depends(get_current_user)])
async def send_shares(request: Request, data: dict, user=Depends(get_current_user)):
    client_ip = request.client.host
    video = data.get('video', '').strip()
    amount = data.get('amount', 250)
    if not video:
        raise HTTPException(status_code=400, detail="Video ID/URL required")
    vid_match = re.search(r'(\d{10,30})', video)
    if vid_match:
        vid = vid_match.group(1)
    elif video.isdigit() and len(video) >= 10:
        vid = video
    else:
        raise HTTPException(status_code=400, detail="Invalid video ID or URL")
    if amount < 1:
        amount = 1
    elif amount > MAX_AMOUNT:
        amount = MAX_AMOUNT

    if not rate_limiter.is_allowed(client_ip):
        return JSONResponse({"status": "error", "message": "Rate limit exceeded. Please wait."}, status_code=429)

    order_id = generate_order_id()
    job = ViewJob(vid, amount, client_ip, job_type="shares", user_id=user["id"])
    job_store[order_id] = job
    await job_queue.put((order_id, job))
    return {"order_id": order_id, "status": "queued", "message": "Order queued"}

@app.get("/status/{order_id}", dependencies=[Depends(get_current_user)])
async def get_status(order_id: str, user=Depends(get_current_user)):
    job = job_store.get(order_id)
    if not job:
        raise HTTPException(status_code=404, detail="Order not found")
    return {
        "order_id": order_id,
        "vid": job.vid,
        "target": job.target,
        "sent": job.sent,
        "status": job.status,
        "error": job.error,
        "elapsed": job.elapsed,
        "speed": job.speed
    }

@app.post("/stop/{order_id}", dependencies=[Depends(get_current_user)])
async def stop_job(order_id: str, user=Depends(get_current_user)):
    job = job_store.get(order_id)
    if not job:
        raise HTTPException(status_code=404, detail="Order not found")
    if job.status not in ("queued", "running"):
        return JSONResponse({"status": "error", "message": "Order is not active"})
    job.cancelled = True
    return JSONResponse({"status": "ok", "message": "Stop signal sent"})

# ---------- FULL HTML TEMPLATE (exact copy from your message) ----------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TTKY AIO</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: #0c1017;
            color: #dce3ef;
            font-family: 'Inter', 'Segoe UI', sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            transition: all 0.3s;
        }
        .auth-container {
            background: rgba(16, 22, 34, 0.9);
            backdrop-filter: blur(12px);
            border: 1px solid #1e2a3a;
            border-radius: 20px;
            padding: 40px 32px;
            width: 380px;
            max-width: 90%;
            box-shadow: 0 20px 60px rgba(0,0,0,0.6);
        }
        .auth-container h1 {
            text-align: center;
            font-size: 28px;
            background: linear-gradient(135deg, #7b8cff, #a78bfa);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 8px;
        }
        .auth-container .sub {
            text-align: center;
            color: #5f7290;
            font-size: 13px;
            margin-bottom: 24px;
        }
        .auth-container .form-group {
            margin-bottom: 16px;
        }
        .auth-container label {
            display: block;
            color: #b0c0d4;
            font-size: 13px;
            font-weight: 500;
            margin-bottom: 5px;
        }
        .auth-container input {
            width: 100%;
            padding: 12px 16px;
            background: rgba(0,0,0,0.3);
            border: 1px solid #1e2a3a;
            border-radius: 10px;
            color: #e8ecf4;
            font-size: 15px;
            outline: none;
            transition: border 0.2s;
        }
        .auth-container input:focus {
            border-color: #7b8cff;
        }
        .auth-container .btn {
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, #2e3b52, #1f2a3a);
            border: none;
            border-radius: 10px;
            color: #fff;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            margin-top: 8px;
        }
        .auth-container .btn:hover {
            background: linear-gradient(135deg, #3d4d6a, #2e3b52);
            transform: translateY(-2px);
        }
        .auth-container .toggle-link {
            text-align: center;
            margin-top: 16px;
            color: #7a8aa3;
            font-size: 14px;
        }
        .auth-container .toggle-link a {
            color: #7b8cff;
            cursor: pointer;
            text-decoration: none;
        }
        .auth-container .toggle-link a:hover { color: #a78bfa; }
        .auth-container .error-msg {
            color: #ff3b30;
            font-size: 14px;
            margin-top: 8px;
            display: none;
        }
        /* Main app styles */
        .main-app { display: none; }
        .main-app.active { display: block; width: 100%; }
        .sidebar {
            width: 240px;
            background: rgba(13, 18, 28, 0.92);
            backdrop-filter: blur(16px);
            padding: 28px 16px;
            border-right: 1px solid #1e2a3a;
            height: 100vh;
            position: sticky;
            top: 0;
            flex-shrink: 0;
            display: flex;
            flex-direction: column;
            box-shadow: 4px 0 30px rgba(0,0,0,0.6);
        }
        .logo {
            text-align: center;
            margin-bottom: 32px;
        }
        .logo h1 {
            font-size: 28px;
            font-weight: 700;
            background: linear-gradient(135deg, #7b8cff, #a78bfa);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            letter-spacing: 1px;
        }
        .logo .sub {
            color: #5f7290;
            font-size: 11px;
            letter-spacing: 3px;
            text-transform: uppercase;
            margin-top: 2px;
        }
        .nav {
            flex: 1;
        }
        .nav a {
            display: flex;
            align-items: center;
            padding: 12px 16px;
            color: #8a9bb5;
            text-decoration: none;
            border-radius: 10px;
            margin-bottom: 2px;
            font-size: 14px;
            transition: all 0.2s;
            cursor: pointer;
            gap: 12px;
        }
        .nav a i {
            width: 20px;
            text-align: center;
            font-size: 16px;
        }
        .nav a:hover {
            background: rgba(123, 140, 255, 0.08);
            color: #dce3ef;
        }
        .nav a.active {
            background: rgba(123, 140, 255, 0.15);
            color: #7b8cff;
            box-shadow: inset 3px 0 0 #7b8cff;
        }
        .nav .badge {
            margin-left: auto;
            background: #4cd964;
            color: #000;
            font-size: 10px;
            padding: 2px 10px;
            border-radius: 20px;
            font-weight: 700;
        }
        .sidebar-footer {
            border-top: 1px solid #1e2a3a;
            padding-top: 16px;
            font-size: 13px;
            color: #5f7290;
            text-align: center;
        }
        .sidebar-footer .discord-btn {
            display: inline-block;
            background: #5865F2;
            color: #fff;
            padding: 8px 18px;
            border-radius: 30px;
            font-weight: 600;
            font-size: 14px;
            transition: all 0.2s;
            margin-top: 8px;
        }
        .sidebar-footer .discord-btn:hover {
            background: #4752c4;
            transform: scale(1.02);
        }
        .sidebar-footer .proxy-info {
            font-size: 12px;
            margin-top: 12px;
            color: #4c5f7a;
        }
        .sidebar-footer .proxy-info i { margin-right: 6px; }
        .main {
            flex: 1;
            padding: 32px 48px;
            overflow-y: auto;
            background: radial-gradient(ellipse at 70% 20%, #141e2a, #070b12);
            min-height: 100vh;
        }
        .page { display: none; animation: fadeIn 0.35s ease; }
        .page.active { display: block; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
        .page-header {
            margin-bottom: 28px;
        }
        .page-header h2 {
            font-size: 30px;
            font-weight: 600;
            background: linear-gradient(135deg, #e8ecf4, #a0b0cc);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .page-header h2 i { -webkit-text-fill-color: #7b8cff; }
        .page-header p {
            color: #7a8aa3;
            font-size: 15px;
            margin-top: 4px;
        }
        .glass {
            background: rgba(16, 22, 34, 0.7);
            backdrop-filter: blur(12px);
            border: 1px solid #1e2a3a;
            border-radius: 18px;
            padding: 28px;
            max-width: 700px;
            box-shadow: 0 12px 40px rgba(0,0,0,0.5);
            transition: all 0.2s;
        }
        .glass:hover { border-color: #2a3a52; }
        .form-group {
            margin-bottom: 20px;
        }
        .form-group label {
            display: block;
            color: #b0c0d4;
            font-size: 13px;
            font-weight: 500;
            margin-bottom: 6px;
        }
        .form-group label i { margin-right: 8px; color: #7b8cff; }
        .form-group input {
            width: 100%;
            padding: 14px 18px;
            background: rgba(0,0,0,0.3);
            border: 1px solid #1e2a3a;
            border-radius: 12px;
            color: #e8ecf4;
            font-size: 15px;
            outline: none;
            transition: border 0.2s, box-shadow 0.2s;
        }
        .form-group input:focus {
            border-color: #7b8cff;
            box-shadow: 0 0 0 3px rgba(123, 140, 255, 0.1);
        }
        .btn {
            padding: 14px 32px;
            background: linear-gradient(135deg, #2e3b52, #1f2a3a);
            border: none;
            border-radius: 12px;
            color: #fff;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            display: inline-flex;
            align-items: center;
            gap: 10px;
        }
        .btn i { font-size: 18px; }
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(123, 140, 255, 0.15);
            background: linear-gradient(135deg, #3d4d6a, #2e3b52);
        }
        .btn:disabled {
            opacity: 0.5;
            transform: none;
            cursor: not-allowed;
            box-shadow: none;
        }
        .btn-danger {
            background: linear-gradient(135deg, #6a1a2a, #3d0f1a);
        }
        .btn-danger:hover {
            background: linear-gradient(135deg, #8a2a3a, #5a1f2a);
            box-shadow: 0 8px 25px rgba(255, 59, 48, 0.2);
        }
        .result-card {
            margin-top: 32px;
            max-width: 700px;
            display: none;
        }
        .result-card.show { display: block; }
        .result-card h3 {
            color: #7b8cff;
            font-size: 20px;
            margin-bottom: 16px;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .stat-row {
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid rgba(26, 36, 53, 0.4);
        }
        .stat-row:last-child { border-bottom: none; }
        .stat-label { color: #7a8aa3; }
        .stat-value { font-weight: 500; }
        .status-badge {
            display: inline-block;
            padding: 3px 14px;
            border-radius: 30px;
            font-size: 12px;
            font-weight: 600;
        }
        .status-queued { background: #ff9500; color: #000; }
        .status-running { background: #7b8cff; color: #fff; }
        .status-completed { background: #4cd964; color: #000; }
        .status-partial { background: #ffcc00; color: #000; }
        .status-error { background: #ff3b30; color: #fff; }
        .status-cancelled { background: #8d9db5; color: #000; }
        .progress-track {
            width: 100%;
            height: 8px;
            background: #1a2435;
            border-radius: 8px;
            margin-top: 16px;
            overflow: hidden;
        }
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #7b8cff, #a78bfa);
            width: 0%;
            border-radius: 8px;
            transition: width 0.4s ease;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 18px;
            max-width: 700px;
            margin-bottom: 28px;
        }
        .stat-box {
            background: rgba(16, 22, 34, 0.7);
            backdrop-filter: blur(8px);
            border: 1px solid #1e2a3a;
            border-radius: 14px;
            padding: 18px 20px;
            text-align: center;
            transition: all 0.2s;
        }
        .stat-box:hover { border-color: #2a3a52; }
        .stat-box .number {
            font-size: 32px;
            font-weight: 700;
            color: #7b8cff;
            line-height: 1.2;
        }
        .stat-box .label {
            font-size: 12px;
            color: #7a8aa3;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-top: 4px;
        }
        .stat-box i { color: #7b8cff; margin-right: 6px; }
        .orders-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }
        .orders-table th {
            text-align: left;
            padding: 12px 14px;
            background: rgba(16, 22, 34, 0.5);
            color: #7a8aa3;
            font-weight: 500;
            border-bottom: 1px solid #1e2a3a;
        }
        .orders-table td {
            padding: 12px 14px;
            border-bottom: 1px solid #1a2435;
        }
        .orders-table tr:hover td {
            background: rgba(123, 140, 255, 0.03);
        }
        .status-cell .status-badge { font-size: 11px; padding: 2px 10px; }
        @media (max-width: 768px) {
            .sidebar { width: 72px; padding: 20px 8px; }
            .logo h1 { font-size: 20px; }
            .logo .sub { display: none; }
            .nav a { padding: 12px; justify-content: center; }
            .nav a span.label { display: none; }
            .nav .badge { display: none; }
            .sidebar-footer .discord-btn span { display: none; }
            .sidebar-footer .discord-btn i { font-size: 20px; }
            .main { padding: 16px; }
            .stats-grid { grid-template-columns: 1fr 1fr; }
        }
    </style>
</head>
<body>
    <!-- Auth Container -->
    <div id="authContainer" class="auth-container">
        <h1>TTKY</h1>
        <div class="sub">Sign in to continue</div>
        <div id="loginForm">
            <div class="form-group">
                <label><i class="fas fa-user"></i> Username</label>
                <input type="text" id="loginUsername" placeholder="Enter username">
            </div>
            <div class="form-group">
                <label><i class="fas fa-lock"></i> Password</label>
                <input type="password" id="loginPassword" placeholder="Enter password">
            </div>
            <button class="btn" id="loginBtn"><i class="fas fa-sign-in-alt"></i> Login</button>
            <div class="toggle-link">Don't have an account? <a id="showRegister">Register</a></div>
            <div class="error-msg" id="loginError"></div>
        </div>
        <div id="registerForm" style="display:none;">
            <div class="form-group">
                <label><i class="fas fa-user"></i> Username</label>
                <input type="text" id="regUsername" placeholder="Choose a username">
            </div>
            <div class="form-group">
                <label><i class="fas fa-lock"></i> Password</label>
                <input type="password" id="regPassword" placeholder="Min 6 chars">
            </div>
            <button class="btn" id="registerBtn"><i class="fas fa-user-plus"></i> Register</button>
            <div class="toggle-link">Already have an account? <a id="showLogin">Login</a></div>
            <div class="error-msg" id="regError"></div>
        </div>
    </div>

    <!-- Main App -->
    <div id="mainApp" class="main-app">
        <div style="display:flex; width:100%; min-height:100vh;">
            <div class="sidebar">
                <div class="logo">
                    <h1>TTKY</h1>
                    <div class="sub">AIO v3</div>
                </div>
                <div class="nav">
                    <a class="active" data-page="dashboard"><i class="fas fa-tachometer-alt"></i><span class="label">Dashboard</span></a>
                    <a data-page="views"><i class="fas fa-eye"></i><span class="label">Views</span></a>
                    <a data-page="shares"><i class="fas fa-share-alt"></i><span class="label">Shares</span><span class="badge">Live</span></a>
                    <a data-page="track"><i class="fas fa-search"></i><span class="label">Track</span></a>
                    <a data-page="orders"><i class="fas fa-list-ul"></i><span class="label">Orders</span><span class="badge" id="ordersBadge">0</span></a>
                </div>
                <div class="sidebar-footer">
                    <a href="https://discord.gg/U5erFsQSX4" target="_blank" class="discord-btn">
                        <i class="fab fa-discord"></i> <span>Join Discord</span>
                    </a>
                    <div class="proxy-info"><i class="fas fa-network-wired"></i> Proxies: <span id="proxyCount">0</span></div>
                </div>
            </div>
            <div class="main">
                <!-- Dashboard -->
                <div id="page-dashboard" class="page active">
                    <div class="page-header">
                        <h2><i class="fas fa-tachometer-alt"></i> Dashboard</h2>
                        <p>System status and order overview</p>
                    </div>
                    <div class="stats-grid" id="dashboardStats">
                        <div class="stat-box"><div class="number" id="statTotal">0</div><div class="label"><i class="fas fa-shopping-bag"></i> Total Orders</div></div>
                        <div class="stat-box"><div class="number" id="statRunning">0</div><div class="label"><i class="fas fa-spinner"></i> Running</div></div>
                        <div class="stat-box"><div class="number" id="statCompleted">0</div><div class="label"><i class="fas fa-check-circle"></i> Completed</div></div>
                        <div class="stat-box"><div class="number" id="statCancelled">0</div><div class="label"><i class="fas fa-times-circle"></i> Cancelled</div></div>
                    </div>
                    <div class="glass">
                        <p style="color:#7a8aa3;"><i class="fas fa-info-circle" style="color:#7b8cff;"></i> System is ready. Proxies are auto-refreshed every 5 minutes.</p>
                    </div>
                </div>

                <!-- Views -->
                <div id="page-views" class="page">
                    <div class="page-header">
                        <h2><i class="fas fa-eye"></i> Views Bot</h2>
                        <p>Send real TikTok views – up to 250 per order</p>
                    </div>
                    <form id="viewForm" class="glass">
                        <div class="form-group">
                            <label><i class="fas fa-video"></i> Video ID or URL</label>
                            <input type="text" id="video" placeholder="e.g. 1234567890123456789 or https://..." required>
                        </div>
                        <div class="form-group">
                            <label><i class="fas fa-sort-amount-up"></i> Amount (max 250)</label>
                            <input type="number" id="amount" value="250" min="1" max="250" required>
                        </div>
                        <button type="submit" class="btn" id="submitBtn"><i class="fas fa-play"></i> Start Views</button>
                    </form>
                    <div class="result-card glass" id="result">
                        <h3><i class="fas fa-clipboard-list"></i> Order Details</h3>
                        <div id="resultContent"></div>
                        <div class="progress-track"><div class="progress-fill" id="progressFill"></div></div>
                        <div style="margin-top:16px; display:none;" id="stopBtnContainer">
                            <button class="btn btn-danger" id="stopBtn"><i class="fas fa-stop"></i> Stop Order</button>
                        </div>
                    </div>
                </div>

                <!-- Shares -->
                <div id="page-shares" class="page">
                    <div class="page-header">
                        <h2><i class="fas fa-share-alt"></i> Shares Bot</h2>
                        <p>Send real TikTok shares – up to 250 per order</p>
                    </div>
                    <form id="shareForm" class="glass">
                        <div class="form-group">
                            <label><i class="fas fa-video"></i> Video ID or URL</label>
                            <input type="text" id="shareVideo" placeholder="e.g. 1234567890123456789 or https://..." required>
                        </div>
                        <div class="form-group">
                            <label><i class="fas fa-sort-amount-up"></i> Amount (max 250)</label>
                            <input type="number" id="shareAmount" value="250" min="1" max="250" required>
                        </div>
                        <button type="submit" class="btn" id="shareSubmitBtn"><i class="fas fa-play"></i> Start Shares</button>
                    </form>
                    <div class="result-card glass" id="shareResult">
                        <h3><i class="fas fa-clipboard-list"></i> Order Details</h3>
                        <div id="shareResultContent"></div>
                        <div class="progress-track"><div class="progress-fill" id="shareProgressFill"></div></div>
                        <div style="margin-top:16px; display:none;" id="shareStopBtnContainer">
                            <button class="btn btn-danger" id="shareStopBtn"><i class="fas fa-stop"></i> Stop Order</button>
                        </div>
                    </div>
                </div>

                <!-- Track -->
                <div id="page-track" class="page">
                    <div class="page-header">
                        <h2><i class="fas fa-search"></i> Track Order</h2>
                        <p>Enter your Order ID (e.g., TTKY-00001) to check status</p>
                    </div>
                    <form id="trackForm" class="glass">
                        <div class="form-group">
                            <label><i class="fas fa-tag"></i> Order ID</label>
                            <input type="text" id="trackOrderId" placeholder="TTKY-00001" required>
                        </div>
                        <button type="submit" class="btn" id="trackBtn"><i class="fas fa-search"></i> Track</button>
                    </form>
                    <div class="result-card glass" id="trackResult">
                        <h3><i class="fas fa-info-circle"></i> Order Status</h3>
                        <div id="trackContent"></div>
                        <div class="progress-track"><div class="progress-fill" id="trackProgress"></div></div>
                    </div>
                </div>

                <!-- Orders -->
                <div id="page-orders" class="page">
                    <div class="page-header">
                        <h2><i class="fas fa-list-ul"></i> Active Orders</h2>
                        <p>Real-time list of all orders</p>
                    </div>
                    <div class="glass" style="max-width:100%; overflow-x:auto;">
                        <table class="orders-table" id="ordersTable">
                            <thead><tr><th>Order ID</th><th>Video</th><th>Type</th><th>Progress</th><th>Speed</th><th>Status</th></tr></thead>
                            <tbody id="ordersBody"></tbody>
                        </table>
                        <div style="margin-top:16px; color:#5f7290; font-size:14px;" id="ordersEmpty"><i class="fas fa-inbox"></i> No orders yet</div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        // Navigation
        document.querySelectorAll('.nav a[data-page]').forEach(link => {
            link.addEventListener('click', function(e) {
                const page = this.dataset.page;
                if (!page) return;
                document.querySelectorAll('.nav a').forEach(a => a.classList.remove('active'));
                this.classList.add('active');
                document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
                document.getElementById('page-' + page).classList.add('active');
                if (page === 'dashboard') updateDashboard();
                if (page === 'orders') loadOrders();
            });
        });

        // Update dashboard stats
        async function updateDashboard() {
            try {
                const resp = await fetch('/orders_summary');
                const data = await resp.json();
                document.getElementById('statTotal').textContent = data.total;
                document.getElementById('statRunning').textContent = data.running;
                document.getElementById('statCompleted').textContent = data.completed;
                document.getElementById('statCancelled').textContent = data.cancelled;
                document.getElementById('ordersBadge').textContent = data.running;
            } catch(e) {}
            try {
                const p = await fetch('/proxy_count');
                const d = await p.json();
                document.getElementById('proxyCount').textContent = d.count;
            } catch(e) {}
        }
        setInterval(updateDashboard, 4000);
        updateDashboard();

        // Load active orders
        async function loadOrders() {
            try {
                const resp = await fetch('/active_orders');
                const orders = await resp.json();
                const tbody = document.getElementById('ordersBody');
                const empty = document.getElementById('ordersEmpty');
                tbody.innerHTML = '';
                if (orders.length === 0) {
                    empty.style.display = 'block';
                    return;
                }
                empty.style.display = 'none';
                orders.forEach(o => {
                    const tr = document.createElement('tr');
                    let statusClass = 'status-' + o.status;
                    let pct = o.target > 0 ? Math.round((o.sent / o.target)*100) : 0;
                    tr.innerHTML = `
                        <td><strong>${o.order_id}</strong></td>
                        <td>${o.vid.slice(0,12)}...</td>
                        <td>${o.job_type}</td>
                        <td>${o.sent}/${o.target} (${pct}%)</td>
                        <td>${o.speed || 0}/s</td>
                        <td class="status-cell"><span class="status-badge ${statusClass}">${o.status}</span></td>
                    `;
                    tbody.appendChild(tr);
                });
            } catch(e) {}
        }

        // Views Form
        const viewForm = document.getElementById('viewForm');
        const viewSubmit = document.getElementById('submitBtn');
        const viewResult = document.getElementById('result');
        const viewResultContent = document.getElementById('resultContent');
        const viewProgress = document.getElementById('progressFill');
        const viewStopContainer = document.getElementById('stopBtnContainer');
        const viewStopBtn = document.getElementById('stopBtn');
        let viewOrderId = null;
        let viewPollInterval = null;

        viewForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const video = document.getElementById('video').value.trim();
            const amount = parseInt(document.getElementById('amount').value) || 250;
            if (!video) { alert('Please enter a video ID or URL'); return; }
            if (amount < 1 || amount > 250) { alert('Amount must be between 1 and 250'); return; }

            viewSubmit.disabled = true;
            viewSubmit.innerHTML = '<i class="fas fa-spinner fa-pulse"></i> Submitting...';
            viewResult.classList.remove('show');
            viewProgress.style.width = '0%';
            viewStopContainer.style.display = 'none';

            try {
                const response = await fetch('/send_views', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ video, amount })
                });
                const data = await response.json();
                if (data.status === 'error') {
                    alert('Error: ' + data.message);
                    viewSubmit.disabled = false;
                    viewSubmit.innerHTML = '<i class="fas fa-play"></i> Start Views';
                    return;
                }
                viewOrderId = data.order_id;
                viewStopContainer.style.display = 'block';
                viewPollInterval = setInterval(async () => {
                    const statusRes = await fetch(`/status/${viewOrderId}`);
                    const statusData = await statusRes.json();
                    displayResult(statusData, viewResultContent, viewProgress, viewOrderId);
                    if (['completed','partial','error','cancelled'].includes(statusData.status)) {
                        clearInterval(viewPollInterval);
                        viewSubmit.disabled = false;
                        viewSubmit.innerHTML = '<i class="fas fa-play"></i> Start Views';
                        viewStopContainer.style.display = 'none';
                    }
                }, 800);
            } catch(err) {
                alert('Network error: ' + err.message);
                viewSubmit.disabled = false;
                viewSubmit.innerHTML = '<i class="fas fa-play"></i> Start Views';
            }
        });

        viewStopBtn.addEventListener('click', async () => {
            if (!viewOrderId) return;
            try {
                const response = await fetch(`/stop/${viewOrderId}`, { method: 'POST' });
                const data = await response.json();
                if (data.status === 'ok') {
                    viewStopBtn.disabled = true;
                    viewStopBtn.innerHTML = '<i class="fas fa-spinner fa-pulse"></i> Stopping...';
                } else {
                    alert('Failed to stop: ' + data.message);
                }
            } catch(err) { alert('Network error'); }
        });

        // Shares Form
        const shareForm = document.getElementById('shareForm');
        const shareSubmit = document.getElementById('shareSubmitBtn');
        const shareResult = document.getElementById('shareResult');
        const shareResultContent = document.getElementById('shareResultContent');
        const shareProgress = document.getElementById('shareProgressFill');
        const shareStopContainer = document.getElementById('shareStopBtnContainer');
        const shareStopBtn = document.getElementById('shareStopBtn');
        let shareOrderId = null;
        let sharePollInterval = null;

        shareForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const video = document.getElementById('shareVideo').value.trim();
            const amount = parseInt(document.getElementById('shareAmount').value) || 250;
            if (!video) { alert('Please enter a video ID or URL'); return; }
            if (amount < 1 || amount > 250) { alert('Amount must be between 1 and 250'); return; }

            shareSubmit.disabled = true;
            shareSubmit.innerHTML = '<i class="fas fa-spinner fa-pulse"></i> Submitting...';
            shareResult.classList.remove('show');
            shareProgress.style.width = '0%';
            shareStopContainer.style.display = 'none';

            try {
                const response = await fetch('/send_shares', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ video, amount })
                });
                const data = await response.json();
                if (data.status === 'error') {
                    alert('Error: ' + data.message);
                    shareSubmit.disabled = false;
                    shareSubmit.innerHTML = '<i class="fas fa-play"></i> Start Shares';
                    return;
                }
                shareOrderId = data.order_id;
                shareStopContainer.style.display = 'block';
                sharePollInterval = setInterval(async () => {
                    const statusRes = await fetch(`/status/${shareOrderId}`);
                    const statusData = await statusRes.json();
                    displayResult(statusData, shareResultContent, shareProgress, shareOrderId);
                    if (['completed','partial','error','cancelled'].includes(statusData.status)) {
                        clearInterval(sharePollInterval);
                        shareSubmit.disabled = false;
                        shareSubmit.innerHTML = '<i class="fas fa-play"></i> Start Shares';
                        shareStopContainer.style.display = 'none';
                    }
                }, 800);
            } catch(err) {
                alert('Network error: ' + err.message);
                shareSubmit.disabled = false;
                shareSubmit.innerHTML = '<i class="fas fa-play"></i> Start Shares';
            }
        });

        shareStopBtn.addEventListener('click', async () => {
            if (!shareOrderId) return;
            try {
                const response = await fetch(`/stop/${shareOrderId}`, { method: 'POST' });
                const data = await response.json();
                if (data.status === 'ok') {
                    shareStopBtn.disabled = true;
                    shareStopBtn.innerHTML = '<i class="fas fa-spinner fa-pulse"></i> Stopping...';
                } else {
                    alert('Failed to stop: ' + data.message);
                }
            } catch(err) { alert('Network error'); }
        });

        // Track Form
        const trackForm = document.getElementById('trackForm');
        const trackBtn = document.getElementById('trackBtn');
        const trackResult = document.getElementById('trackResult');
        const trackContent = document.getElementById('trackContent');
        const trackProgress = document.getElementById('trackProgress');

        trackForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const orderId = document.getElementById('trackOrderId').value.trim();
            if (!orderId) { alert('Please enter an Order ID'); return; }
            trackBtn.disabled = true;
            trackBtn.innerHTML = '<i class="fas fa-spinner fa-pulse"></i> Checking...';
            trackResult.classList.remove('show');
            try {
                const response = await fetch(`/status/${orderId}`);
                if (!response.ok) {
                    if (response.status === 404) alert('Order not found');
                    else alert('Error: ' + response.status);
                    trackBtn.disabled = false;
                    trackBtn.innerHTML = '<i class="fas fa-search"></i> Track';
                    return;
                }
                const data = await response.json();
                displayResult(data, trackContent, trackProgress, orderId);
                trackResult.classList.add('show');
            } catch(err) { alert('Network error: ' + err.message); }
            trackBtn.disabled = false;
            trackBtn.innerHTML = '<i class="fas fa-search"></i> Track';
        });

        // Shared display function
        function displayResult(data, contentEl, progressEl, orderId) {
            const resultDiv = contentEl.closest('.result-card');
            if (resultDiv) resultDiv.classList.add('show');
            let statusClass = 'status-' + data.status;
            let statusText = data.status.charAt(0).toUpperCase() + data.status.slice(1);
            let pct = data.target > 0 ? Math.round((data.sent / data.target) * 100) : 0;
            if (progressEl) progressEl.style.width = pct + '%';
            const displayId = orderId || data.order_id || 'N/A';
            let html = `
                <div class="stat-row"><span class="stat-label"><i class="fas fa-tag"></i> Order ID</span><span class="stat-value"><strong>${displayId}</strong></span></div>
                <div class="stat-row"><span class="stat-label"><i class="fas fa-flag"></i> Status</span><span class="stat-value"><span class="status-badge ${statusClass}">${statusText}</span></span></div>
                <div class="stat-row"><span class="stat-label"><i class="fas fa-video"></i> Video ID</span><span class="stat-value">${data.vid || 'N/A'}</span></div>
                <div class="stat-row"><span class="stat-label"><i class="fas fa-chart-line"></i> Sent</span><span class="stat-value">${data.sent || 0} / ${data.target || 0}</span></div>
                <div class="stat-row"><span class="stat-label"><i class="fas fa-tachometer-alt"></i> Speed</span><span class="stat-value">${data.speed || 0}/s</span></div>
                <div class="stat-row"><span class="stat-label"><i class="fas fa-clock"></i> Elapsed</span><span class="stat-value">${data.elapsed ? data.elapsed.toFixed(1) : '0'}s</span></div>
            `;
            if (data.error) {
                html += `<div class="stat-row"><span class="stat-label"><i class="fas fa-exclamation-triangle"></i> Error</span><span class="stat-value" style="color:#ff3b30;">${data.error}</span></div>`;
            }
            contentEl.innerHTML = html;
        }

        // Periodic refresh for orders page
        setInterval(() => {
            if (document.getElementById('page-orders').classList.contains('active')) {
                loadOrders();
            }
        }, 5000);

        // Auth logic (login/register)
        const loginBtn = document.getElementById('loginBtn');
        const registerBtn = document.getElementById('registerBtn');
        const showRegister = document.getElementById('showRegister');
        const showLogin = document.getElementById('showLogin');
        const loginError = document.getElementById('loginError');
        const regError = document.getElementById('regError');

        showRegister.addEventListener('click', () => {
            document.getElementById('loginForm').style.display = 'none';
            document.getElementById('registerForm').style.display = 'block';
        });
        showLogin.addEventListener('click', () => {
            document.getElementById('registerForm').style.display = 'none';
            document.getElementById('loginForm').style.display = 'block';
        });

        function setError(el, msg) {
            el.textContent = msg;
            el.style.display = 'block';
        }
        function clearError(el) {
            el.style.display = 'none';
        }

        loginBtn.addEventListener('click', async () => {
            const username = document.getElementById('loginUsername').value.trim();
            const password = document.getElementById('loginPassword').value.trim();
            clearError(loginError);
            if (!username || !password) {
                setError(loginError, 'Please fill all fields');
                return;
            }
            try {
                const resp = await fetch('/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password })
                });
                const data = await resp.json();
                if (resp.ok) {
                    localStorage.setItem('token', data.access_token);
                    localStorage.setItem('username', data.username);
                    document.getElementById('authContainer').style.display = 'none';
                    document.getElementById('mainApp').classList.add('active');
                    // Reload to fully initialize dashboard
                    window.location.reload();
                } else {
                    setError(loginError, data.detail || 'Login failed');
                }
            } catch(e) {
                setError(loginError, 'Network error');
            }
        });

        registerBtn.addEventListener('click', async () => {
            const username = document.getElementById('regUsername').value.trim();
            const password = document.getElementById('regPassword').value.trim();
            clearError(regError);
            if (!username || !password) {
                setError(regError, 'Please fill all fields');
                return;
            }
            if (password.length < 6) {
                setError(regError, 'Password must be at least 6 characters');
                return;
            }
            try {
                const resp = await fetch('/register', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password })
                });
                const data = await resp.json();
                if (resp.ok) {
                    alert('Registration successful! Please login.');
                    document.getElementById('registerForm').style.display = 'none';
                    document.getElementById('loginForm').style.display = 'block';
                } else {
                    setError(regError, data.detail || 'Registration failed');
                }
            } catch(e) {
                setError(regError, 'Network error');
            }
        });

        // Auto-login if token exists
        (function() {
            const token = localStorage.getItem('token');
            if (token) {
                document.getElementById('authContainer').style.display = 'none';
                document.getElementById('mainApp').classList.add('active');
                // Load dashboard data
                updateDashboard();
                loadOrders();
            }
        })();
    </script>
</body>
</html>
"""

# ---------- RUN APP ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
