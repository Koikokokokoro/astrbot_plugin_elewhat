"""
饿了么 H5 页面自动化模块
GPS 模拟定位 + 像素坐标点击 + .shopList 卡片提取
"""

import asyncio
import json
import os
import random
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    from playwright.async_api import TimeoutError as PlaywrightTimeout
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    from playwright_stealth import Stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

# ── 常量 ──────────────────────────────────────────────────────
BASE_URL = "https://h5.ele.me/"
VIEWPORT = {"width": 390, "height": 844}
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

DEFAULT_COORDS = {
    "phone_input":    (250, 90),
    "send_code_btn":  (300, 135),
    "code_input":     (170, 135),
    "agree_checkbox": (53, 257),
    "login_submit":   (210, 205),
    "address_bar":    (213, 22),
    "refresh_addr":   (77, 50),
}

ANTI_BOT_SCRIPT = r"""
delete Object.getPrototypeOf(navigator).webdriver;
delete navigator.webdriver;
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined, configurable: true, enumerable: false
});
if (!window.chrome) window.chrome = {};
if (!chrome.runtime) {
    chrome.runtime = {
        id: 'fake-' + Math.random().toString(36).substr(2, 9),
        onConnect: { addListener: function(){} },
        onMessage: { addListener: function(){} },
        getManifest: function() { return {}; },
        getURL: function(p) { return 'chrome-extension://fake/' + p; },
        connect: function() { return {
            onDisconnect: { addListener: function(){} },
            onMessage: { addListener: function(){} },
            postMessage: function(){}, disconnect: function(){}
        };},
        sendMessage: function() {}, lastError: undefined
    };
}
if (!chrome.webstore) chrome.webstore = { install: function(){} };
if (!chrome.loadTimes) chrome.loadTimes = function() { return {
    requestTime: Date.now()/1000, startLoadTime: Date.now()/1000-0.1,
    commitLoadTime: Date.now()/1000-0.05, finishDocumentLoadTime: Date.now()/1000-0.02,
    finishLoadTime: Date.now()/1000, firstPaintTime: Date.now()/1000-0.01,
    navigationType: 'Other', wasFetchedViaSpdy: true, wasNpnNegotiated: true,
    connectionInfo: 'h2', npnNegotiatedProtocol: 'h2'
};};
if (!chrome.csi) chrome.csi = function() { return {
    startE: Date.now()-100, onloadT: Date.now(), pageT: 100, tran: 15
};};
if (!chrome.app) chrome.app = {
    isInstalled: false,
    InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' },
    RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' }
};
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        let p = [{ name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 }];
        p.item = function(i) { return this[i]; };
        p.namedItem = function() { return null; };
        p.refresh = function() {};
        return p;
    }, configurable: true, enumerable: true
});
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8, configurable: true });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8, configurable: true });
Object.defineProperty(navigator, 'productSub', { get: () => '20030107', configurable: true });
Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.', configurable: true });
Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 5, configurable: true });
"""


class EleAutomation:
    def __init__(self, data_dir: Path, headless: bool = False, amap_key: str = "",
                 logger=None, debug: bool = False):
        self.data_dir = data_dir
        self.cookie_file = data_dir / "cookies.json"
        self._headless = headless
        self._amap_key = amap_key
        self._debug = debug
        self._log = logger or (lambda msg: None)
        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._coords = self._load_coords(data_dir)
        # 可调参数，默认值
        self.scroll_times = (5, 12)
        self.scroll_dist = (300, 700)
        self.scroll_interval = (0.5, 1.5)

    # ── 配置 / 坐标 ────────────────────────────────────────

    def _load_coords(self, data_dir: Path) -> dict:
        cf = data_dir / "coords.json"
        if cf.exists():
            try:
                return {k: (float(v[0]), float(v[1])) for k, v in json.loads(cf.read_text("utf-8")).items()}
            except Exception:
                pass
        cf.write_text(json.dumps(DEFAULT_COORDS, ensure_ascii=False, indent=2), "utf-8")
        return dict(DEFAULT_COORDS)

    # ── 公共属性 ───────────────────────────────────────────

    @property
    def is_logged_in(self) -> bool:
        return self.cookie_file.exists() and self.cookie_file.stat().st_size > 100

    @property
    def browser_alive(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    # ── 像素操作 ───────────────────────────────────────────

    async def _tap(self, name: str):
        if name not in self._coords:
            return False
        x, y = int(self._coords[name][0]), int(self._coords[name][1])
        await self._page.mouse.click(x, y)
        return True

    async def _type_at(self, name: str, text: str):
        if name not in self._coords:
            return False
        x, y = int(self._coords[name][0]), int(self._coords[name][1])
        await self._page.mouse.click(x, y, click_count=3)
        await self._rdelay(0.1, 0.3)
        await self._page.keyboard.press("Backspace")
        await self._page.keyboard.type(text, delay=random.randint(50, 150))
        return True

    async def _rdelay(self, lo: float = 0.8, hi: float = 2.5):
        await asyncio.sleep(random.uniform(lo, hi))

    async def _wait_idle(self, timeout: int = 15):
        try:
            await self._page.wait_for_load_state("networkidle", timeout=timeout * 1000)
        except PlaywrightTimeout:
            pass

    # ── 浏览器生命周期 ─────────────────────────────────────

    async def start(self):
        await self._ensure_browser()

    async def _ensure_browser(self):
        async with self._lock:
            if self._browser and self._browser.is_connected():
                return
            if not HAS_PLAYWRIGHT:
                raise RuntimeError("playwright 未安装")

            self._pw = await async_playwright().start()
            args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
            if self._headless:
                args.extend(["--no-sandbox", "--disable-setuid-sandbox",
                             "--disable-dev-shm-usage", "--disable-gpu"])
            self._browser = await self._pw.chromium.launch(headless=self._headless, args=args)

            storage = None
            if self.is_logged_in:
                try:
                    storage = json.loads(self.cookie_file.read_text("utf-8"))
                except Exception:
                    pass

            self._context = await self._browser.new_context(
                storage_state=storage, viewport=VIEWPORT,
                user_agent=MOBILE_UA, locale="zh-CN",
            )
            if HAS_STEALTH:
                stealth = Stealth(chrome_runtime=True, navigator_webdriver=True,
                                  chrome_app=True, chrome_csi=True, chrome_load_times=True)
                await stealth.apply_stealth_async(self._context)
            else:
                await self._context.add_init_script(ANTI_BOT_SCRIPT)
            self._page = await self._context.new_page()

    async def close(self):
        for obj in [self._context, self._browser]:
            if obj:
                try: await obj.close()
                except: pass
        if self._pw:
            try: await self._pw.stop()
            except: pass

    async def _save_cookies(self):
        if self._context:
            storage = await self._context.storage_state()
            self.cookie_file.write_text(json.dumps(storage, ensure_ascii=False, indent=2), "utf-8")

    async def _check_logged_in(self) -> bool:
        """判断是否已登录：有 [data-aspm-c=shopList] 且有卡片"""
        try:
            n = await self._page.evaluate(
                "() => document.querySelectorAll('[data-aspm-c=\"shopList\"] .card-takeaway-big').length"
            )
            return n >= 2
        except Exception:
            return False

    # ── 地理编码 ───────────────────────────────────────────

    async def _geocode(self, address: str) -> Optional[tuple[float, float]]:
        m = re.match(r"^\s*(-?\d+\.?\d*)\s*[,，\s]\s*(-?\d+\.?\d*)\s*$", address)
        if m:
            lat, lng = float(m.group(1)), float(m.group(2))
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                return lat, lng

        amap_key = self._amap_key or os.environ.get("AMAP_KEY", "")
        if amap_key:
            try:
                lat, lng = await self._geocode_amap(address, amap_key)
                if lat is not None:
                    return lat, lng
            except Exception:
                pass

        try:
            lat, lng = await self._geocode_nominatim(address)
            if lat is not None:
                return lat, lng
        except Exception:
            pass
        return None

    async def _geocode_amap(self, address: str, key: str) -> tuple:
        params = {"key": key, "address": address, "output": "JSON"}
        url = "https://restapi.amap.com/v3/geocode/geo?" + urllib.parse.urlencode(params)
        def _fetch():
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        data = await asyncio.to_thread(_fetch)
        if data.get("status") == "1" and data.get("geocodes"):
            loc = data["geocodes"][0]["location"]
            lng_str, lat_str = loc.split(",")
            return float(lat_str), float(lng_str)
        return None, None

    async def _geocode_nominatim(self, address: str) -> tuple:
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({"q": address, "format": "json", "limit": 3})
        def _fetch():
            req = urllib.request.Request(url, headers={"User-Agent": "ElewhatBot/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        data = await asyncio.to_thread(_fetch)
        if data:
            best = max(data, key=lambda d: float(d.get("importance", 0)))
            return float(best["lat"]), float(best["lon"])
        return None, None

    # ── GPS 刷新 ───────────────────────────────────────────

    async def _trigger_gps_refresh(self):
        await self._tap("address_bar")
        await self._rdelay(2, 3)
        await self._tap("refresh_addr")
        await self._rdelay(1, 2)
        await self._page.goto(BASE_URL, wait_until="domcontentloaded")
        await self._wait_idle()
        await self._rdelay(1, 2)

    # ── 登录 ───────────────────────────────────────────────

    async def send_sms(self, phone: str) -> dict:
        await self._ensure_browser()
        # 预设默认 GPS，防止页面首次加载弹权限框
        await self._context.grant_permissions(["geolocation"])
        await self._context.set_geolocation({"latitude": 31.2304, "longitude": 121.4737})
        await self._page.goto(BASE_URL, wait_until="domcontentloaded")
        await self._wait_idle()
        await self._rdelay(2, 3)

        if await self._check_logged_in():
            await self._save_cookies()
            return {"success": True, "already_logged_in": True}

        await self._type_at("phone_input", phone)
        await self._rdelay(0.5, 1)
        await self._tap("agree_checkbox")
        await self._rdelay(0.3, 0.6)
        await self._tap("send_code_btn")
        return {"success": True, "phone": phone}

    async def verify_code(self, code: str) -> dict:
        await self._ensure_browser()

        if await self._check_logged_in():
            await self._save_cookies()
            return {"success": True}

        await self._type_at("code_input", code)
        await self._rdelay(0.5, 1)
        await self._tap("login_submit")
        await self._rdelay(2, 4)

        for _ in range(20):
            await asyncio.sleep(1)
            if await self._check_logged_in():
                await self._save_cookies()
                self._log("[饿了啥] 登录成功")
                return {"success": True}
            try:
                body = await self._page.inner_text("body")
                for err in ["验证码错误", "验证码过期", "验证码无效"]:
                    if err in body:
                        return {"success": False, "error": err}
            except Exception:
                pass

        if await self._check_logged_in():
            await self._save_cookies()
            return {"success": True}
        return {"success": False, "error": "登录超时"}

    # ── 商家获取 ───────────────────────────────────────────

    async def get_one_restaurant(self, address: str) -> dict:
        """主流程：地理编码 → 设 GPS → 刷新 → 滚动 → 提取 → 随机选一"""
        geo = await self._geocode(address)
        if not geo:
            return {"success": False, "error": f"地理编码失败: {address}"}

        lat, lng = geo
        await self._ensure_browser()

        # ★ 设目标 GPS 后 goto
        await self._context.grant_permissions(["geolocation"])
        await self._context.set_geolocation({"latitude": lat, "longitude": lng})
        await self._page.goto(BASE_URL, wait_until="domcontentloaded")
        await self._wait_idle()
        await self._rdelay(2, 3)

        # 触发 GPS 刷新
        await self._trigger_gps_refresh()

        # 滚动加载
        await self._dump_page("GPS 刷新后开始提取")
        restaurants = await self._scroll_and_extract()
        if not restaurants:
            await self._dump_page("滚动提取后无结果")
            return {"success": False, "error": "未获取到商家"}

        deduped = []
        seen = set()
        for r in restaurants:
            if r["name"] not in seen:
                seen.add(r["name"])
                deduped.append(r)

        random.shuffle(deduped)
        shop = deduped[0]
        return {"success": True, "total": len(deduped), "shop": shop}

    async def _scroll_and_extract(self) -> list[dict]:
        n = random.randint(*self.scroll_times)
        d_lo, d_hi = self.scroll_dist
        i_lo, i_hi = self.scroll_interval

        for _ in range(n):
            dist = random.randint(d_lo, d_hi)
            await self._page.mouse.wheel(0, dist)
            await asyncio.sleep(random.uniform(i_lo, i_hi))

        if random.random() < 0.5:
            await self._page.mouse.wheel(0, random.randint(-int(d_hi * 0.6), -int(d_lo * 0.6)))
            await asyncio.sleep(random.uniform(i_lo, i_hi))
            await self._page.mouse.wheel(0, random.randint(d_lo, d_hi))
            await asyncio.sleep(random.uniform(i_lo, i_hi))

        return await self._extract_cards()

    async def _dump_page(self, reason: str = ""):
        """失败时 dump 页面结构到日志，用于排查"""
        if not self._debug:
            return
        self._log(f"[饿了啥 DEBUG] === 页面 dump: {reason} ===")
        try:
            self._log(f"[饿了啥 DEBUG] URL: {self._page.url}")
            body = await self._page.inner_text("body")
            self._log(f"[饿了啥 DEBUG] body 前500字: {body[:500]}")
        except Exception as e:
            self._log(f"[饿了啥 DEBUG] body 读取失败: {e}")

        try:
            info = await self._page.evaluate("""
                () => {
                    const r = {};
                    r.cards = document.querySelectorAll('.card-takeaway-big').length;
                    r.firstName = document.querySelector('.card-takeaway__title')?.innerText || '(none)';
                    return r;
                }
            """)
            self._log(f"[饿了啥 DEBUG] card-takeaway-big: {info.get('cards', 0)} 个, 第一个: {info.get('firstName', '')}")
        except Exception as e:
            self._log(f"[饿了啥 DEBUG] JS dump 失败: {e}")

        try:
            path = self.data_dir / f"debug_{int(time.time())}.png"
            await self._page.screenshot(path=str(path))
            self._log(f"[饿了啥 DEBUG] 截图: {path}")
        except Exception as e:
            self._log(f"[饿了啥 DEBUG] 截图失败: {e}")

    async def _extract_cards(self) -> list[dict]:
        """从 .card-takeaway-big 卡片中按 class 提取各字段"""
        for attempt in range(8):
            await asyncio.sleep(1)
            cards = await self._page.evaluate("""
                () => {
                    const cards = document.querySelectorAll('.card-takeaway-big');
                    if (cards.length < 2) return [];
                    return Array.from(cards).map(el => {
                        const getText = (sel) => (el.querySelector(sel)?.innerText || '').trim();
                        // 图片：.card-takeaway__pic 的 src 或 background-image
                        const picEl = el.querySelector('.card-takeaway__pic');
                        let img = '';
                        if (picEl) {
                            img = picEl.getAttribute('src') || '';
                            if (!img) {
                                const bg = picEl.style.backgroundImage || '';
                                const m = bg.match(/url\\(["']?([^"')]+)["']?\\)/i);
                                if (m) img = m[1];
                            }
                        }
                        return {
                            name: getText('.card-takeaway__title'),
                            rating: getText('.card-takeaway__store-point'),
                            monthly_sales: getText('.card-takeaway__store-sell'),
                            delivery_time: getText('.card-takeaway__store-distance'),
                            delivery_fee: getText('.card-takeaway__delivery-fee'),
                            min_price: getText('.card-takeaway__delivery-start'),
                            image: img
                        };
                    }).filter(c => c.name && c.name.length > 1);
                }
            """)
            if cards:
                return cards
        await self._dump_page("未找到 .card-takeaway-big")
        return []
