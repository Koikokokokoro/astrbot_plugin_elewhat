"""
饿了么随机外卖推荐 —— 像素级坐标点击方案
固定视口 390×844，所有操作走 page.mouse.click(x, y)
GPS 模拟切换地址，高德 API 地理编码

用法:
  python standalone_ele.py                # 正常使用
  python standalone_ele.py --headless     # Debian 无头模式
  python standalone_ele.py --check-bot    # 反爬检测
  python standalone_ele.py --debug        # 每步截图

配置:
  config.json        编辑 amap_key 填入高德 API key（留空则用 Nominatim 兜底）
                    免费申请: https://console.amap.com/ → 应用管理 → 创建应用
                    服务平台选 "Web服务"，每天 5000 次免费额度

坐标配置: 编辑 coords.json（首次运行自动生成默认值）

依赖:
  pip install playwright playwright-stealth
  playwright install chromium
  playwright install-deps        # Debian/Ubuntu 额外
"""

import argparse
import asyncio
import json
import random
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    from playwright.async_api import TimeoutError as PlaywrightTimeout
    HAS_PW = True
except ImportError:
    HAS_PW = False

try:
    from playwright_stealth import Stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

# ── 路径 ──────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
COOKIE_FILE = DATA_DIR / "cookies.json"
COORDS_FILE = BASE_DIR / "coords.json"
CONFIG_FILE = BASE_DIR / "config.json"
SCREENSHOT_DIR = BASE_DIR / "screenshots"

BASE_URL = "https://h5.ele.me/"
VIEWPORT = {"width": 390, "height": 844}

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

# ── 默认坐标 (390×844 视口) ──────────────────────────────────
# 如果坐标不对，编辑 coords.json 调整，或删掉让它重新生成默认值
DEFAULT_COORDS = {
    # === 登录页 ===
    "phone_input":    (250, 90),    # 手机号输入框中心
    "send_code_btn":  (300, 135),   # 发送验证码按钮
    "code_input":     (170, 135),   # 验证码输入框
    "agree_checkbox": (53, 257),    # 同意协议勾选框
    "login_submit":   (210, 205),   # 登录按钮
    # === 首页 ===
    "address_bar":    (195, 55),    # 顶部地址栏
    # === 地址搜索页 ===
    "addr_search":    (195, 120),   # 地址搜索框
    "addr_first":     (195, 260),   # 第一个搜索结果
    # === 滚动 ===
    "scroll_area":    (195, 600),   # 商家列表滚动区域
}

# ── 反检测 fallback ───────────────────────────────────────────
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
        let p = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '', length: 1 },
            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '', length: 2 }
        ];
        p.item = function(i) { return this[i]; };
        p.namedItem = function() { return null; };
        p.refresh = function() {};
        return p;
    },
    configurable: true, enumerable: true
});
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8, configurable: true });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8, configurable: true });
Object.defineProperty(navigator, 'productSub', { get: () => '20030107', configurable: true });
Object.defineProperty(navigator, 'vendor', { get: () => 'Google Inc.', configurable: true });
Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 5, configurable: true });
"""


# ── 日志 ──────────────────────────────────────────────────────

def ts(): return datetime.now().strftime("%H:%M:%S")

def log(msg: str, level: str = "INFO"):
    p = {"INFO": "  ", "OK": " ✓", "ERR": " ✗", "WARN": " ⚠", "STEP": "▶ "}
    print(f"[{ts()}] {p.get(level, '  ')} {msg}")


# ── 核心类 ────────────────────────────────────────────────────

class EleSpider:
    def __init__(self, headless: bool = False, debug: bool = False):
        self._headless = headless
        self._debug = debug
        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._config = self._load_config()
        self._coords = self._load_coords()

    @property
    def logged_in(self) -> bool:
        return COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 100

    # ── 配置管理 ──────────────────────────────────────────

    def _load_config(self) -> dict:
        if CONFIG_FILE.exists():
            try:
                return json.loads(CONFIG_FILE.read_text("utf-8"))
            except Exception:
                pass
        # 生成默认配置
        cfg = {"amap_key": "", "note": "高德地图 API key，留空则用 Nominatim 兜底。免费申请: https://console.amap.com/"}
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
        return cfg

    # ── 坐标管理 ──────────────────────────────────────────

    def _load_coords(self) -> dict:
        if COORDS_FILE.exists():
            try:
                data = json.loads(COORDS_FILE.read_text("utf-8"))
                log(f"已加载 coords.json ({len(data)} 个点)", "OK")
                return {k: (float(v[0]), float(v[1])) for k, v in data.items()}
            except Exception:
                pass
        # 生成默认配置
        COORDS_FILE.write_text(
            json.dumps(DEFAULT_COORDS, ensure_ascii=False, indent=2), "utf-8"
        )
        log(f"已生成默认 coords.json，坐标不对请编辑后重试", "WARN")
        return dict(DEFAULT_COORDS)

    # ── 像素操作 ──────────────────────────────────────────

    async def _tap(self, name: str, dx: int = 0, dy: int = 0):
        """点击命名坐标"""
        if name not in self._coords:
            log(f"坐标 '{name}' 未在 coords.json 中定义", "ERR")
            return False
        x, y = self._coords[name]
        x, y = int(x + dx), int(y + dy)
        if self._debug:
            log(f"tap '{name}' @ ({x}, {y})", "DBG")
        await self._page.mouse.click(x, y)
        return True

    async def _type_at(self, name: str, text: str):
        """点击命名坐标后逐字输入"""
        if name not in self._coords:
            log(f"坐标 '{name}' 未定义", "ERR")
            return False
        x, y = self._coords[name]
        x, y = int(x), int(y)
        if self._debug:
            log(f"type '{name}' @ ({x}, {y})", "DBG")
        # 三击选中已有内容
        await self._page.mouse.click(x, y, click_count=3)
        await self._rdelay(0.1, 0.3)
        await self._page.keyboard.press("Backspace")
        await self._rdelay(0.1, 0.2)
        await self._page.keyboard.type(text, delay=random.randint(50, 150))
        return True

    # ── 工具 ──────────────────────────────────────────────

    async def _rdelay(self, lo: float = 0.8, hi: float = 2.5):
        await asyncio.sleep(random.uniform(lo, hi))

    async def _shot(self, name: str):
        if not self._debug:
            return
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / f"{datetime.now().strftime('%H%M%S')}_{name}.png"
        try: await self._page.screenshot(path=path)
        except Exception: pass

    async def _wait_idle(self, timeout: int = 15):
        try:
            await self._page.wait_for_load_state("networkidle", timeout=timeout * 1000)
        except PlaywrightTimeout:
            pass

    # ── 浏览器生命周期 ─────────────────────────────────────

    async def start(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not HAS_PW:
            log("pip install playwright && playwright install chromium", "ERR")
            sys.exit(1)
        self._pw = await async_playwright().start()

        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ]
        if self._headless:
            args.extend([
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage", "--disable-gpu",
            ])

        self._browser = await self._pw.chromium.launch(headless=self._headless, args=args)

        storage = None
        if self.logged_in:
            try: storage = json.loads(COOKIE_FILE.read_text("utf-8"))
            except: pass

        self._context = await self._browser.new_context(
            storage_state=storage,
            viewport=VIEWPORT,
            user_agent=MOBILE_UA,
            locale="zh-CN",
        )

        if HAS_STEALTH:
            stealth = Stealth(
                chrome_runtime=True, navigator_webdriver=True,
                chrome_app=True, chrome_csi=True, chrome_load_times=True,
            )
            await stealth.apply_stealth_async(self._context)
            log("playwright-stealth 已注入", "OK")
        else:
            await self._context.add_init_script(ANTI_BOT_SCRIPT)

        self._page = await self._context.new_page()
        log(f"浏览器就绪 ({VIEWPORT['width']}x{VIEWPORT['height']})", "OK")

    async def stop(self):
        for obj in [self._context, self._browser]:
            if obj:
                try: await obj.close()
                except: pass
        if self._pw:
            try: await self._pw.stop()
            except: pass

    # ── cookie ────────────────────────────────────────────

    async def _save_cookies(self):
        if self._context:
            storage = await self._context.storage_state()
            COOKIE_FILE.write_text(
                json.dumps(storage, ensure_ascii=False, indent=2), "utf-8"
            )
            log("登录态已保存", "OK")

    async def _check_logged_in(self) -> bool:
        """通过页面内容判断是否已登录"""
        try:
            body = await self._page.inner_text("body")
            if "首页" in body and ("评分" in body or "配送" in body or "月售" in body):
                return True
            for kw in ["我的订单", "收货地址", "会员中心", "退出登录"]:
                if kw in body:
                    return True
        except:
            pass
        return False

    # ── bot 检测 ───────────────────────────────────────────

    async def check_bot(self):
        await self._page.goto("https://bot.sannysoft.com/", wait_until="networkidle")
        await self._rdelay(2, 4)
        await self._shot("bot_check")
        log("请检查浏览器窗口，绿色 ✓ 越多越好", "STEP")
        input("按回车继续...")

    # ── 登录 ───────────────────────────────────────────────

    async def login(self, address: str = "") -> Optional[tuple[float, float]]:
        """登录饿了么。返回目标 GPS (lat, lng)，None 表示失败"""
        target_geo = None
        if address:
            target_geo = await self._geocode(address)
            if not target_geo:
                return None

        # ★ 先设目标 GPS，再开网页 — 饿了么首次加载就读定位
        lat = target_geo[0] if target_geo else 31.2304
        lng = target_geo[1] if target_geo else 121.4737
        await self._context.grant_permissions(["geolocation"])
        await self._context.set_geolocation({"latitude": lat, "longitude": lng})
        log(f"GPS 预设: ({lat:.6f}, {lng:.6f})", "OK")

        await self._page.goto(BASE_URL, wait_until="domcontentloaded")
        await self._wait_idle()
        await self._rdelay(2, 3)
        await self._shot("01_initial")

        # 已有登录态
        if await self._check_logged_in():
            log("已有登录态", "OK")
            await self._save_cookies()
            return target_geo

        log("页面应已自动跳转到登录页", "STEP")

        phone = ""
        while not re.match(r"^1\d{10}$", phone):
            phone = input("请输入手机号: ").strip()

        log("填写手机号...", "STEP")
        await self._type_at("phone_input", phone)
        await self._shot("02_phone")
        await self._rdelay(0.5, 1)

        log("勾选同意协议...", "STEP")
        await self._tap("agree_checkbox")
        await self._rdelay(0.3, 0.6)

        log("点击发送验证码...", "STEP")
        await self._tap("send_code_btn")
        await self._shot("03_code_sent")
        await self._rdelay(1, 2)

        code = input("请输入短信验证码: ").strip()
        await self._type_at("code_input", code)
        await self._shot("04_code")
        await self._rdelay(0.5, 1)

        await self._tap("login_submit")
        await self._shot("05_login_submitted")
        await self._rdelay(2, 4)

        log("等待登录结果...", "STEP")
        for _ in range(20):
            await asyncio.sleep(1)
            if await self._check_logged_in():
                await self._save_cookies()
                log("登录成功!", "OK")
                return target_geo
            try:
                body = await self._page.inner_text("body")
                for err in ["验证码错误", "验证码过期", "验证码无效"]:
                    if err in body:
                        log(f"登录失败: {err}", "ERR")
                        return None
            except:
                pass

        if await self._check_logged_in():
            await self._save_cookies()
            return target_geo
        log("登录超时", "ERR")
        return None

    # ── 地址切换 (GPS 模拟) ───────────────────────────────

    async def _geocode(self, address: str, city: str = "") -> Optional[tuple[float, float]]:
        """地址 → 经纬度。高德 API 优先，Nominatim 兜底"""
        # 经纬度直输
        m = re.match(r"^\s*(-?\d+\.?\d*)\s*[,，\s]\s*(-?\d+\.?\d*)\s*$", address)
        if m:
            lat, lng = float(m.group(1)), float(m.group(2))
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                log(f"经纬度: ({lat}, {lng})", "OK")
                return lat, lng

        # ── 高德地图 API（优先） ──
        amap_key = self._config.get("amap_key", "") or os.environ.get("AMAP_KEY", "")
        if amap_key:
            try:
                lat, lng = await self._geocode_amap(address, city, amap_key)
                if lat is not None:
                    return lat, lng
            except Exception as e:
                log(f"高德 API 异常: {e}", "WARN")

        # ── Nominatim（兜底） ──
        log("使用 Nominatim 地理编码（注册高德 API key 可获得更准的中文结果）", "STEP")
        try:
            lat, lng = await self._geocode_nominatim(address)
            if lat is not None:
                return lat, lng
        except Exception as e:
            log(f"Nominatim 失败: {e}", "WARN")

        log("地理编码失败。可直接输入: 31.2304,121.4737", "ERR")
        log("或编辑 config.json 填入高德 amap_key", "ERR")
        return None

    async def _geocode_amap(self, address: str, city: str, key: str) -> tuple[Optional[float], Optional[float]]:
        """高德地图地理编码 API"""
        params = {"key": key, "address": address, "output": "JSON"}
        if city:
            params["city"] = city

        url = "https://restapi.amap.com/v3/geocode/geo?" + urllib.parse.urlencode(params)
        log(f"高德地理编码: {address}", "STEP")

        def _fetch():
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())

        data = await asyncio.to_thread(_fetch)
        if data.get("status") != "1" or not data.get("geocodes"):
            log(f"  高德返回空: {data.get('info', '')}", "WARN")
            return None, None

        # 高德格式: "lng,lat"（经度在前！）
        loc = data["geocodes"][0]["location"]
        lng_str, lat_str = loc.split(",")
        lat, lng = float(lat_str), float(lng_str)

        formatted = data["geocodes"][0].get("formatted_address", "")
        level = data["geocodes"][0].get("level", "")
        log(f"  → {formatted} ({level})", "OK")
        log(f"  → ({lat}, {lng})", "OK")
        return lat, lng

    async def _geocode_nominatim(self, address: str) -> tuple[Optional[float], Optional[float]]:
        """Nominatim 地理编码（免费兜底）"""
        queries = [address]
        if len(address) < 6:
            queries.append(f"{address} 中国")

        for q in queries:
            url = (
                "https://nominatim.openstreetmap.org/search?"
                + urllib.parse.urlencode({"q": q, "format": "json", "limit": 3})
            )

            def _fetch():
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "ElewhatBot/1.0"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read())

            data = await asyncio.to_thread(_fetch)
            if data:
                best = max(data, key=lambda d: float(d.get("importance", 0)))
                lat = float(best["lat"])
                lng = float(best["lon"])
                log(f"  → {best.get('display_name', '')[:80]}", "OK")
                return lat, lng
        return None, None

    # ── 滚动 + 提取 ───────────────────────────────────────

    async def scroll_and_extract(self) -> list[dict]:
        n = random.randint(5, 12)
        log(f"随机滚动 {n} 次...", "STEP")

        for i in range(n):
            dist = random.randint(300, 700)
            await self._page.mouse.wheel(0, dist)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            if self._debug:
                log(f"  滚动 {i + 1}/{n}  {dist}px", "DBG")

        # 偶尔往回滚一点再滚下去，增加加载随机性
        if random.random() < 0.5:
            await self._page.mouse.wheel(0, random.randint(-400, -200))
            await asyncio.sleep(random.uniform(0.5, 1.0))
            await self._page.mouse.wheel(0, random.randint(300, 600))
            await asyncio.sleep(random.uniform(0.5, 1.0))

        log("提取商家卡片...", "STEP")
        return await self._extract_all_cards()

    async def _extract_all_cards(self) -> list[dict]:
        """等待页面渲染完成后查找商家容器并提取"""
        # 等几秒确保 SPA 渲染完成
        for attempt in range(8):
            await asyncio.sleep(1)

            # 策略1: 精确 .shopList
            count = await self._page.evaluate(
                "() => document.querySelector('.shopList')?.children.length || 0"
            )
            if count > 2:
                log(f"找到 .shopList ({count} 个子元素)", "OK")
                return await self._extract_from_selector(".shopList")

            # 策略2: 找 children 最多的可见容器
            best = await self._page.evaluate("""
                () => {
                    let best = null, bestN = 0;
                    document.querySelectorAll('div[class], ul[class], section[class]').forEach(el => {
                        const n = el.children.length;
                        if (n > bestN && n > 5 && el.offsetHeight > 200) {
                            bestN = n;
                            best = el.className?.toString().substring(0, 60);
                        }
                    });
                    return best ? {cls: best, n: bestN} : null;
                }
            """)
            if best:
                # 找到了一个大容器，用它下面 children 最多的子层
                log(f"最大容器: .{best['cls'].replace(' ', '.')} ({best['n']} children)", "DBG")
                # 找这个容器的直接子元素中 children 最多的那个作为卡片列表
                cards_data = await self._page.evaluate("""
                    () => {
                        let maxKids = 0, target = null;
                        document.querySelectorAll('div[class] > *').forEach(el => {
                            if (el.children.length > maxKids && el.children.length > 3) {
                                maxKids = el.children.length;
                                target = el;
                            }
                        });
                        if (target) {
                            return Array.from(target.children)
                                .map(c => c.innerText.trim())
                                .filter(t => t.length > 10);
                        }
                        return [];
                    }
                """)
                if cards_data:
                    log(f"从最大子容器提取到 {len(cards_data)} 张卡片", "OK")
                    return self._parse_cards(cards_data)

        # 全部策略失败 → dump 页面文本排查
        log("自动查找失败，dump 页面信息...", "WARN")
        body_text = await self._page.inner_text("body")
        # 找 body 中最大的容器
        containers = await self._page.evaluate("""
            () => {
                const info = [];
                document.querySelectorAll('*').forEach(el => {
                    if (el.children.length > 5 && el.offsetHeight > 300) {
                        info.push({
                            tag: el.tagName,
                            cls: (el.className || '').toString().substring(0, 50),
                            kids: el.children.length,
                            h: el.offsetHeight
                        });
                    }
                });
                info.sort((a, b) => b.kids - a.kids);
                return info.slice(0, 10);
            }
        """)
        log(f"页面大型容器 top10: {json.dumps(containers, ensure_ascii=False)}", "DBG")
        log(f"body 前 300 字: {body_text[:300]}", "DBG")
        return []

    async def _extract_from_selector(self, sel: str) -> list[dict]:
        cards_data = await self._page.evaluate(f"""
            () => {{
                const c = document.querySelector('{sel}');
                return Array.from(c.children)
                    .map(el => el.innerText.trim())
                    .filter(t => t.length > 10);
            }}
        """)
        return self._parse_cards(cards_data)

    def _parse_cards(self, cards_data: list[str]) -> list[dict]:
        restaurants = []
        seen = set()
        for text in cards_data:
            info = self._parse_shop_card(text)
            if info and info["name"] not in seen:
                seen.add(info["name"])
                restaurants.append(info)
        log(f"解析出 {len(restaurants)} 家商家", "OK")
        return restaurants

    def _parse_shop_card(self, text: str) -> Optional[dict]:
        """从餐厅卡片文本中解析结构化字段"""
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if len(lines) < 2:
            return None

        info = {"name": "", "rating": "", "monthly_sales": "",
                "delivery_fee": "", "delivery_time": "", "min_price": ""}

        unknown = []
        for line in lines:
            if re.match(r"^\d\.\d分?$", line):
                info["rating"] = line
            elif re.search(r"(月售|已售|销量)\d", line):
                info["monthly_sales"] = line
            elif re.search(r"(免配送|配送费|配送.*¥|^¥\d$)", line):
                info["delivery_fee"] = line
            elif re.search(r"\d+分钟|\d+min", line):
                info["delivery_time"] = line
            elif re.match(r"^\d+\.\d+km$", line):
                continue
            elif re.search(r"(起送|起$|¥\d+起)", line):
                info["min_price"] = line
            elif re.match(r"^¥\d+$", line) and not info["delivery_fee"]:
                info["delivery_fee"] = line
            else:
                unknown.append(line)

        for u in unknown:
            if re.match(r"^\d", u) and len(u) < 8:
                continue
            if u in ("新品", "优惠", "折扣", "新店", "品牌", "免配送费"):
                continue
            info["name"] = u
            break

        return info if info["name"] else None

    # ── 主流程 ─────────────────────────────────────────────

    async def _trigger_gps_refresh(self):
        """点进地址页 → 点自动定位地址 → 返回"""
        log("触发 GPS 刷新...", "STEP")
        await self._tap("address_bar")
        await self._rdelay(2, 3)
        # 点击自动定位的地址
        await self._tap("refresh_addr")
        await self._rdelay(1, 2)
        await self._page.go_back()
        await self._wait_idle()
        await self._rdelay(1, 2)
        log("GPS 已刷新", "OK")

    async def run(self) -> Optional[dict]:
        """GPS 刷新 → 滚动 → 提取 → 随机选一家"""
        await self._trigger_gps_refresh()

        restaurants = await self.scroll_and_extract()
        if not restaurants:
            log("未获取到商家", "ERR")
            return None
        deduped = []
        seen = set()
        for r in restaurants:
            if r["name"] not in seen:
                seen.add(r["name"])
                deduped.append(r)
        log(f"共 {len(deduped)} 家", "OK")

        # 打乱后选第一个（等价于随机选一）
        random.shuffle(deduped)
        return deduped[0]


# ── CLI ────────────────────────────────────────────────────────

def banner(spider: EleSpider):
    print()
    print("=" * 50)
    print("      饿了啥")
    print("=" * 50)
    print(f"  登录态: {'已登录' if spider.logged_in else '未登录'}")
    print(f"  视口:   {VIEWPORT['width']}x{VIEWPORT['height']}")
    if ARGS.debug:
        print(f"  debug:  screenshots/")
    print()
    print("  1. 来一单（登录 + 随机推荐）")
    print("  2. 退出")
    print()


def _print_result(shop: dict):
    if not shop:
        return
    meta = [v for k, v in shop.items() if v and k != "name"]
    print()
    print("─" * 30)
    print(f"  {shop['name']}")
    if meta:
        print(f"  {' | '.join(meta)}")
    print("─" * 30)
    print(json.dumps(shop, ensure_ascii=False, indent=2))


async def main():
    spider = EleSpider(headless=ARGS.headless, debug=ARGS.debug)
    try:
        await spider.start()
    except Exception as e:
        log(f"浏览器启动失败: {e}", "ERR")
        return

    try:
        if ARGS.check_bot:
            await spider.check_bot()
            return

        while True:
            banner(spider)
            choice = input("请选择 [1-2]: ").strip()

            if choice == "1":
                addr = input("请输入收货地址: ").strip()
                if not addr:
                    continue

                geo = await spider.login(address=addr)
                if not geo:
                    continue

                try:
                    result = await asyncio.wait_for(spider.run(), timeout=120)
                except asyncio.TimeoutError:
                    log("超时", "ERR")
                    continue
                _print_result(result)

            elif choice == "2":
                break
            else:
                log("无效", "WARN")
    finally:
        await spider.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="饿了么随机外卖推荐")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--check-bot", action="store_true")
    ARGS = parser.parse_args()
    asyncio.run(main())
