import asyncio
import json
import os
import random
import re
import time
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain, Image

from .ele_automation import EleAutomation


@register("elewhat", "Koikokokokoro", "饿了么随机推荐外卖", "2.1.0",
          "输入 /吃什么 <地址> 随机推荐一家外卖。配置文件在 _conf_schema.json")
class ElewhatPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config
        self.auto: EleAutomation | None = None
        self._last_use = 0.0
        self._pending_login: dict[str, str] = {}

    async def initialize(self):
        plugin_dir = Path(os.path.dirname(os.path.abspath(__file__)))
        data_dir = plugin_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        headless = self.cfg.get("headless", False)
        amap_key = self.cfg.get("amap_key", "")
        self.auto = EleAutomation(data_dir, headless=headless, amap_key=amap_key,
                                  logger=lambda msg: logger.info(msg),
                                  debug=self.cfg.get("debug", False))

        # 滚动参数
        self.auto.scroll_times = (self.cfg.get("scroll_times_min", 5),
                                  self.cfg.get("scroll_times_max", 12))
        self.auto.scroll_dist = (self.cfg.get("scroll_distance_min", 300),
                                 self.cfg.get("scroll_distance_max", 700))
        self.auto.scroll_interval = (self.cfg.get("scroll_interval_min", 0.5),
                                     self.cfg.get("scroll_interval_max", 1.5))

        logger.info(f"[饿了啥] 初始化完成 headless={headless} cooldown={self.cfg.get('cooldown', 600)}s")

    # ── 浏览器生命周期 ──────────────────────────────────────

    async def _ensure_browser(self):
        """复用或新建浏览器，超时则关旧开新"""
        now = time.time()
        if self.auto.browser_alive and (now - self._last_use) < self.cfg.get("cooldown", 600):
            return True
        if self.auto.browser_alive:
            await self.auto.close()
        await self.auto.start()
        self._last_use = now
        return True

    async def _refresh_browser(self):
        """冷窗期内直接复用，只需滚回顶部"""
        self._last_use = time.time()

    # ── /login ──────────────────────────────────────────────

    @filter.command("login")
    async def cmd_login(self, event: AstrMessageEvent):
        """管理员登录 —— /login <手机号> 或 /login <验证码>"""
        # 权限检查：只有管理员
        if not event.is_admin():
            yield event.plain_result("仅管理员可用")
            return

        arg = _parse_arg(event, "login")
        uid = event.get_sender_id()

        if not arg:
            yield event.plain_result("/login <手机号> 或 /login <验证码>")
            return

        # 判断是手机号还是验证码
        if re.match(r"^1\d{10}$", arg):
            # ── 阶段 1：发送验证码 ──
            await self._ensure_browser()
            try:
                result = await asyncio.wait_for(self.auto.send_sms(arg), timeout=30)
            except asyncio.TimeoutError:
                yield event.plain_result("操作超时")
                return
            except Exception as e:
                logger.error(f"[饿了啥] send_sms: {e}")
                yield event.plain_result(f"错误: {e}")
                return

            if result.get("already_logged_in"):
                yield event.plain_result("已有登录态，可直接使用 /吃什么")
                return
            if result.get("success"):
                self._pending_login[uid] = arg
                yield event.plain_result(f"验证码已发送至 {arg}，请用 /login <验证码> 完成登录")
            else:
                yield event.plain_result(f"发送失败: {result.get('error')}")

        elif re.match(r"^\d{4,6}$", arg):
            # ── 阶段 2：验证码登录 ──
            if uid not in self._pending_login:
                yield event.plain_result("请先使用 /login <手机号> 发送验证码")
                return
            try:
                result = await asyncio.wait_for(self.auto.verify_code(arg), timeout=45)
            except asyncio.TimeoutError:
                yield event.plain_result("验证超时")
                return
            except Exception as e:
                logger.error(f"[饿了啥] verify_code: {e}")
                yield event.plain_result(f"错误: {e}")
                return

            if result.get("success"):
                self._pending_login.pop(uid, None)
                yield event.plain_result("登录成功! 可使用 /吃什么 <地址>")
            else:
                yield event.plain_result(f"登录失败: {result.get('error')}")

        else:
            yield event.plain_result("格式错误: /login 13800138000 或 /login 123456")

    # ── /吃什么 ─────────────────────────────────────────────

    @filter.command("吃什么")
    async def cmd_eat(self, event: AstrMessageEvent):
        """随机推荐外卖 —— /吃什么 <地址>"""
        address = _parse_arg(event, "吃什么")
        if not address:
            yield event.plain_result("请提供地址，如: /吃什么 东南大学九龙湖校区")
            return

        if not self.auto.is_logged_in:
            yield event.plain_result("尚未登录，请管理员使用 /login <手机号> 登录")
            return

        yield event.plain_result(f"正在「{address}」附近搜索...")

        try:
            await self._ensure_browser()
            result = await asyncio.wait_for(
                self.auto.get_one_restaurant(address), timeout=120
            )
        except asyncio.TimeoutError:
            yield event.plain_result("操作超时")
            return
        except Exception as e:
            logger.error(f"[饿了啥] 查询异常: {e}")
            yield event.plain_result(f"错误: {e}")
            return

        self._refresh_browser()

        if not result.get("success"):
            yield event.plain_result(f"获取失败: {result.get('error')}")
            return

        shop = result["shop"]
        total = result.get("total", "?")

        # 构建消息链：图片 + 文字
        msg_chain = []
        img_url = shop.get("image", "")
        if img_url:
            try:
                msg_chain.append(Image.fromURL(img_url))
            except Exception:
                pass

        meta = [v for v in [shop.get("rating"), shop.get("monthly_sales"),
                             shop.get("delivery_time"), shop.get("delivery_fee"),
                             shop.get("min_price")] if v]
        text = (
            f" {shop['name']}\n"
            f"{' | '.join(meta) if meta else ''}\n"
            f"── {address} (共{total}家)"
        )
        msg_chain.append(Plain(text))

        yield event.chain_result(msg_chain)

    # ── 生命周期 ───────────────────────────────────────────

    async def terminate(self):
        if self.auto:
            await self.auto.close()
            logger.info("[饿了啥] 已关闭")


def _parse_arg(event: AstrMessageEvent, cmd: str) -> str:
    """从消息链中提取命令参数，兼容 AstrBot v4 的组件化消息"""
    prefix = f"/{cmd}"
    for seg in event.get_messages():
        if isinstance(seg, Plain):
            txt = (getattr(seg, "text", None) or str(seg)).strip()
            if not txt:
                continue
            # /cmd arg  格式
            if txt.startswith(prefix):
                return txt[len(prefix):].strip()
            # 某些平台已剥离命令前缀，直接就是参数
            if txt and not txt.startswith("/"):
                return txt
    # fallback: message_str
    msg = event.message_str.strip()
    if msg.startswith(prefix):
        return msg[len(prefix):].strip()
    if msg and not msg.startswith("/"):
        return msg
    return ""
