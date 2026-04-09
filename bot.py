import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord.ext import commands
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = Path(os.getenv("STATE_FILE_PATH", str(BASE_DIR / "state.json")))
TARGETS_FILE = Path(os.getenv("TARGETS_FILE_PATH", str(BASE_DIR / "targets.json")))
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


@dataclass
class ProductTarget:
    name: str
    url: str
    mode: str = "stock"


@dataclass
class ProductStatus:
    in_stock: Optional[bool]
    title: str
    price: Optional[str]
    summary: str


class RestockBot(commands.Bot):
    def __init__(self, targets: List[ProductTarget]) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.targets = targets
        self.channel_id = int(os.environ["DISCORD_CHANNEL_ID"])
        self.check_interval = env_int("CHECK_INTERVAL_SECONDS", 300)
        self.mention_role_id = os.getenv("DISCORD_MENTION_ROLE_ID")
        self.state = self.load_state()
        self.latest_statuses: Dict[str, ProductStatus] = {}
        self.session: Optional[aiohttp.ClientSession] = None

    def load_state(self) -> Dict[str, bool]:
        if not STATE_FILE.exists():
            return {}

        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logging.warning("state.json is invalid. Starting with a fresh state cache.")
            return {}

    def save_state(self) -> None:
        STATE_FILE.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def save_targets(self) -> None:
        payload = [
            {"name": target.name, "url": target.url, "mode": target.mode}
            for target in self.targets
        ]
        TARGETS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=30)
        self.session = aiohttp.ClientSession(timeout=timeout, headers=HEADERS)
        self.loop.create_task(self.poll_forever())

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
        await super().close()

    async def on_ready(self) -> None:
        logging.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")
        await self.send_startup_message()

    async def poll_forever(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.check_all_products()
            except Exception:
                logging.exception("Unexpected error during product check")
            await asyncio.sleep(self.check_interval)

    async def check_all_products(self) -> None:
        updates: List[tuple[ProductTarget, str]] = []

        for target in self.targets:
            status = await self.fetch_product_status(target)
            self.latest_statuses[target.url] = status
            previous = self.state.get(target.url)
            if status.in_stock is not None:
                self.state[target.url] = status.in_stock

            if previous is None:
                logging.info("Initial state for %s set to %s", target.name, status.summary)
                continue

            if status.in_stock is None:
                logging.info("%s availability is unknown for this check.", target.name)
                continue

            if not previous and status.in_stock:
                updates.append((target, self.build_restock_message(target, status)))
            elif previous and not status.in_stock:
                logging.info("%s returned to inactive state.", target.name)

        self.save_state()

        if updates:
            for target, message in updates:
                embed = self.build_restock_embed(target, self.latest_statuses[target.url])
                await self.send_discord_alert(message, embed=embed)
                await self.send_email_alert("Restock detected", message)

    async def fetch_product_status(self, target: ProductTarget) -> ProductStatus:
        if self.session is None:
            raise RuntimeError("HTTP session is not initialized")

        async with self.session.get(target.url) as response:
            response.raise_for_status()
            html = await response.text()
            final_url = str(response.url)

        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text(" ", strip=True).lower()

        title = self.extract_title(soup) or target.name
        if target.mode == "queue":
            price = None
            in_stock = self.detect_queue_open(target.url, final_url, page_text)
            summary = self.queue_summary(in_stock)
        else:
            price = self.extract_price(soup)
            in_stock = self.detect_in_stock(target.url, soup, html, page_text)
            summary = self.stock_summary(in_stock)

        logging.info("%s checked: %s", target.name, summary)
        return ProductStatus(in_stock=in_stock, title=title, price=price, summary=summary)

    async def fetch_product_title(self, url: str) -> str:
        if self.session is None:
            raise RuntimeError("HTTP session is not initialized")

        async with self.session.get(url) as response:
            response.raise_for_status()
            html = await response.text()

        soup = BeautifulSoup(html, "html.parser")
        title = self.extract_title(soup)
        if title:
            return title

        title_tag = soup.find("title")
        if title_tag:
            text = title_tag.get_text(strip=True)
            if text:
                return text

        return self.fallback_name_from_url(url)

    def build_status_lines(self) -> List[str]:
        lines: List[str] = []
        for index, target in enumerate(self.targets, start=1):
            status = self.latest_statuses.get(target.url)
            if status is None:
                lines.append(f"{index}. {target.name}: no data yet")
                continue

            price = f" | Price: {status.price}" if status.price else ""
            lines.append(f"{index}. {status.title}: {status.summary}{price}")
        return lines

    def build_target_lines(self) -> List[str]:
        return [
            f"{index}. [{target.mode}] {target.name} | {target.url}"
            for index, target in enumerate(self.targets, start=1)
        ]

    @staticmethod
    def chunk_lines(lines: List[str], limit: int = 3500) -> List[str]:
        if not lines:
            return ["No data available."]

        chunks: List[str] = []
        current: List[str] = []
        current_len = 0

        for line in lines:
            line_len = len(line) + 1
            if current and current_len + line_len > limit:
                chunks.append("\n".join(current))
                current = [line]
                current_len = line_len
            else:
                current.append(line)
                current_len += line_len

        if current:
            chunks.append("\n".join(current))

        return chunks

    @staticmethod
    def extract_title(soup: BeautifulSoup) -> Optional[str]:
        heading = soup.find("h1")
        if heading:
            return heading.get_text(strip=True)
        return None

    @staticmethod
    def fallback_name_from_url(url: str) -> str:
        slug = url.rstrip("/").split("/")[-1]
        slug = re.sub(r"[-_]+", " ", slug).strip()
        return slug.title() if slug else "Untitled Product"

    @staticmethod
    def extract_price(soup: BeautifulSoup) -> Optional[str]:
        for selector in [
            'meta[property="product:price:amount"]',
            'meta[itemprop="price"]',
            '[data-product-price]',
            '.price-item--regular',
            '.price',
        ]:
            node = soup.select_one(selector)
            if node:
                text = node.get("content") or node.get_text(" ", strip=True)
                if text:
                    return text
        return None

    @staticmethod
    def stock_summary(in_stock: Optional[bool]) -> str:
        if in_stock is True:
            return "In stock"
        if in_stock is False:
            return "Sold out"
        return "Unknown"

    @staticmethod
    def queue_summary(is_open: Optional[bool]) -> str:
        if is_open is True:
            return "Queue open"
        if is_open is False:
            return "Queue closed"
        return "Queue status unknown"

    @staticmethod
    def collect_availability_values(payload: Any) -> List[str]:
        values: List[str] = []

        if isinstance(payload, dict):
            for key, value in payload.items():
                if key.lower() == "availability":
                    if isinstance(value, str):
                        values.append(value)
                else:
                    values.extend(RestockBot.collect_availability_values(value))
        elif isinstance(payload, list):
            for item in payload:
                values.extend(RestockBot.collect_availability_values(item))

        return values

    @staticmethod
    def normalize_availability_token(value: str) -> Optional[bool]:
        token = value.strip().lower()
        if not token:
            return None

        positive_tokens = [
            "instock",
            "in stock",
            "limitedavailability",
            "limited availability",
            "preorder",
            "pre-order",
        ]
        negative_tokens = [
            "outofstock",
            "out of stock",
            "soldout",
            "sold out",
            "discontinued",
            "unavailable",
        ]

        if any(marker in token for marker in positive_tokens):
            return True
        if any(marker in token for marker in negative_tokens):
            return False
        return None

    @staticmethod
    def extract_schema_availability(soup: BeautifulSoup, html: str) -> List[str]:
        values: List[str] = []

        for node in soup.select('[itemprop="availability"]'):
            value = node.get("href") or node.get("content") or node.get_text(" ", strip=True)
            if value:
                values.append(value)

        for script in soup.find_all("script", type="application/ld+json"):
            raw = script.string or script.get_text()
            if not raw:
                continue

            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue

            values.extend(RestockBot.collect_availability_values(parsed))

        for match in re.findall(r"schema\.org/([A-Za-z]+)", html, flags=re.IGNORECASE):
            values.append(match)

        return values

    @staticmethod
    def extract_json_hint_booleans(html: str, keys: List[str]) -> List[bool]:
        hints: List[bool] = []
        for key in keys:
            pattern = rf'"{re.escape(key)}"\s*:\s*(true|false)'
            for match in re.findall(pattern, html, flags=re.IGNORECASE):
                hints.append(match.lower() == "true")
        return hints

    @staticmethod
    def detect_target_stock(page_text: str, html: str) -> Optional[bool]:
        negative_markers = [
            "out of stock",
            "temporarily out of stock",
            "notify me when it's back",
            "notify me when its back",
            "sold out",
        ]
        positive_markers = [
            "in stock",
            "only a few left",
            "add to cart",
            "ship it",
            "same day delivery",
            "pickup",
        ]

        if any(marker in page_text for marker in negative_markers):
            return False
        if any(marker in page_text for marker in positive_markers):
            return True

        for hint in RestockBot.extract_json_hint_booleans(
            html,
            ["is_out_of_stock", "in_stock", "available_to_promise"],
        ):
            return hint

        return None

    @staticmethod
    def detect_bestbuy_stock(page_text: str, html: str) -> Optional[bool]:
        negative_markers = [
            "sold out",
            "coming soon",
            "unavailable nearby",
            "not available for pickup",
            "not available for shipping",
            "check stores",
        ]
        positive_markers = [
            "add to cart",
            "add to basket",
            "get it by",
            "shipping",
            "pickup",
        ]

        if any(marker in page_text for marker in negative_markers):
            return False
        if any(marker in page_text for marker in positive_markers):
            return True

        for hint in RestockBot.extract_json_hint_booleans(
            html,
            ["isSoldOut", "isComingSoon", "isPreOrder", "orderable", "inStock"],
        ):
            return hint

        return None

    @staticmethod
    def detect_pokemoncenter_stock(page_text: str, html: str) -> Optional[bool]:
        if "incapsula incident id" in page_text or "request unsuccessful" in page_text:
            return None

        negative_markers = [
            "sold out",
            "email me when available",
            "notify me when available",
            "out of stock",
        ]
        positive_markers = [
            "add to cart",
            "add to bag",
            "quantity",
        ]

        if any(marker in page_text for marker in negative_markers):
            return False
        if any(marker in page_text for marker in positive_markers):
            return True

        for hint in RestockBot.extract_json_hint_booleans(
            html,
            ["inStock", "isInStock", "available"],
        ):
            return hint

        return None

    @staticmethod
    def detect_ikea_stock(page_text: str, html: str) -> Optional[bool]:
        negative_markers = [
            "out of stock",
            "not available for delivery",
            "not available in store",
            "sold out",
        ]
        positive_markers = [
            "add to bag",
            "add to cart",
            "buy now",
            "in stock",
        ]

        if any(marker in page_text for marker in negative_markers):
            return False
        if any(marker in page_text for marker in positive_markers):
            return True
        if "checking availability" in page_text:
            return None

        for hint in RestockBot.extract_json_hint_booleans(
            html,
            ["inStock", "available", "sellable"],
        ):
            return hint

        return None

    @staticmethod
    def detect_in_stock(
        url: str,
        soup: BeautifulSoup,
        html: str,
        page_text: str,
    ) -> Optional[bool]:
        for value in RestockBot.extract_schema_availability(soup, html):
            parsed = RestockBot.normalize_availability_token(value)
            if parsed is not None:
                return parsed

        domain = urlparse(url).netloc.lower()
        store_detectors = [
            ("ikea.com", RestockBot.detect_ikea_stock),
            ("pokemoncenter.com", RestockBot.detect_pokemoncenter_stock),
            ("target.com", RestockBot.detect_target_stock),
            ("bestbuy.com", RestockBot.detect_bestbuy_stock),
        ]
        for site, detector in store_detectors:
            if site in domain:
                detected = detector(page_text, html)
                if detected is not None:
                    return detected

        sold_out_markers = [
            "sold out",
            "sorry sold out",
            "out of stock",
            "unavailable",
            "email me when available",
            "notify me when available",
            "notify me when it's back",
            "temporarily out of stock",
            "not available",
        ]
        positive_markers = [
            "add to cart",
            "buy it now",
            "pre-order",
            "add to bag",
            "available for pickup",
            "available for shipping",
        ]

        if any(marker in page_text for marker in sold_out_markers):
            return False

        if any(marker in page_text for marker in positive_markers):
            return True

        return None

    @staticmethod
    def detect_queue_open(
        requested_url: str,
        final_url: str,
        page_text: str,
    ) -> Optional[bool]:
        requested_domain = urlparse(requested_url).netloc.lower()
        final_domain = urlparse(final_url).netloc.lower()

        if "incapsula incident id" in page_text or "request unsuccessful" in page_text:
            return None

        queue_markers = [
            "virtual queue",
            "queue-it",
            "you are now in line",
            "you are in line",
            "estimated wait",
            "waiting room",
            "when it is your turn",
            "your turn will begin",
            "line is paused",
        ]

        if final_domain != requested_domain and "queue" in final_domain:
            return True

        if any(marker in final_url.lower() for marker in ["queue-it", "waitingroom", "queue"]):
            return True

        if any(marker in page_text for marker in queue_markers):
            return True

        if "pokemoncenter.com" in requested_domain:
            return False

        return None

    def build_restock_message(self, target: ProductTarget, status: ProductStatus) -> str:
        mention = f"<@&{self.mention_role_id}> " if self.mention_role_id else ""
        if target.mode == "queue":
            return (
                f"{mention}Queue alert for {status.title}!\n"
                f"Status: {status.summary}\n"
                f"Link: {target.url}"
            )

        price_line = f"\nPrice: {status.price}" if status.price else ""
        return (
            f"{mention}Restock detected for {status.title}!\n"
            f"Status: {status.summary}{price_line}\n"
            f"Link: {target.url}"
        )

    def build_restock_embed(self, target: ProductTarget, status: ProductStatus) -> discord.Embed:
        if target.mode == "queue":
            embed = discord.Embed(
                title="Queue Alert",
                description=f"**{status.title}** queue is now open.",
                color=discord.Color.orange(),
            )
            embed.add_field(name="Status", value=status.summary, inline=True)
            embed.add_field(name="Link", value=target.url, inline=False)
            embed.set_footer(text="Cortis Restock Monitor")
            return embed

        embed = discord.Embed(
            title="Restock Detected",
            description=f"**{status.title}** is available now.",
            color=discord.Color.green(),
        )
        embed.add_field(name="Status", value=status.summary, inline=True)
        embed.add_field(name="Price", value=status.price or "Unknown", inline=True)
        embed.add_field(name="Link", value=target.url, inline=False)
        embed.set_footer(text="Cortis Restock Monitor")
        return embed

    def build_status_embed(self, title: str, description: str, color: discord.Color) -> discord.Embed:
        embed = discord.Embed(title=title, description=description, color=color)
        embed.set_footer(text="Cortis Restock Monitor")
        return embed

    def build_links_embed(self) -> discord.Embed:
        description = "\n".join(self.build_target_lines()) or "No monitored links yet."
        embed = discord.Embed(
            title="Monitored Links",
            description=description,
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Cortis Restock Monitor")
        return embed

    def build_paginated_embeds(
        self,
        title: str,
        lines: List[str],
        color: discord.Color,
    ) -> List[discord.Embed]:
        chunks = self.chunk_lines(lines)
        total = len(chunks)
        embeds: List[discord.Embed] = []

        for index, chunk in enumerate(chunks, start=1):
            page_title = title if total == 1 else f"{title} ({index}/{total})"
            embed = discord.Embed(title=page_title, description=chunk, color=color)
            embed.set_footer(text="Cortis Restock Monitor")
            embeds.append(embed)

        return embeds

    async def send_startup_message(self) -> None:
        embed = discord.Embed(
            title="Restock Bot Tutorial | 使用教程",
            description=(
                "Use the commands below to check stock and manage monitored links.\n"
                "使用下面的命令查看库存和管理监控链接。\n\n"
                f"Currently monitoring **{len(self.targets)}** products.\n"
                f"当前监控商品数量：**{len(self.targets)}**\n"
                f"Check interval / 检查间隔：**{self.check_interval}** seconds"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="User Commands | 普通命令",
            value=(
                "`!status` Show latest stock status\n"
                "`!check` Run an immediate stock check\n"
                "`!links` Show all monitored links\n\n"
                "`!status` 查看当前库存状态\n"
                "`!check` 立即手动检查一次\n"
                "`!links` 查看当前监控链接"
            ),
            inline=False,
        )
        embed.add_field(
            name="Admin Commands | 管理员命令",
            value=(
                "`!addlink <url>` Add a new monitored link\n"
                "`!addqueue <url>` Add a Pokemon Center queue monitor\n"
                "`!removelink <number>` Remove a link by number\n\n"
                "`!addlink <url>` 添加新的监控链接\n"
                "`!addqueue <url>` 添加 Pokemon Center 排队监控\n"
                "`!removelink <编号>` 按编号删除链接"
            ),
            inline=False,
        )
        embed.add_field(
            name="Permissions | 权限说明",
            value=(
                "`!addlink` and `!removelink` require `Manage Server` permission.\n"
                "`!addlink` 和 `!removelink` 需要 `Manage Server` 权限。"
            ),
            inline=False,
        )
        embed.add_field(
            name="Restock Alert Example | 补货提醒示例",
            value=(
                "When an item restocks, the bot posts a green alert card with:\n"
                "- product name\n"
                "- stock status\n"
                "- price\n"
                "- product link\n\n"
                "商品补货时，机器人会发送绿色提醒卡片，包含：\n"
                "- 商品名称\n"
                "- 库存状态\n"
                "- 价格\n"
                "- 商品链接"
            ),
            inline=False,
        )
        embed.set_footer(text="Cortis Restock Monitor")
        await self.send_discord_alert("", embed=embed)

    async def manual_check(self) -> List[tuple[ProductTarget, str]]:
        updates: List[tuple[ProductTarget, str]] = []

        for target in self.targets:
            status = await self.fetch_product_status(target)
            self.latest_statuses[target.url] = status
            previous = self.state.get(target.url)
            if status.in_stock is not None:
                self.state[target.url] = status.in_stock

            if previous is not None and status.in_stock is True and not previous:
                updates.append((target, self.build_restock_message(target, status)))

        self.save_state()
        return updates

    def add_target(self, name: str, url: str, mode: str = "stock") -> bool:
        normalized_url = url.strip()
        if any(target.url == normalized_url and target.mode == mode for target in self.targets):
            return False

        self.targets.append(ProductTarget(name=name.strip(), url=normalized_url, mode=mode))
        self.save_targets()
        return True

    def remove_target(self, index: int) -> ProductTarget:
        target = self.targets.pop(index)
        self.latest_statuses.pop(target.url, None)
        self.state.pop(target.url, None)
        self.save_targets()
        self.save_state()
        return target

    async def send_discord_alert(
        self,
        message: str,
        *,
        embed: Optional[discord.Embed] = None,
    ) -> None:
        channel = self.get_channel(self.channel_id)
        if channel is None:
            channel = await self.fetch_channel(self.channel_id)

        if not isinstance(channel, discord.abc.Messageable):
            raise TypeError("Configured Discord channel is not messageable")

        await channel.send(content=message or None, embed=embed)
        logging.info("Discord alert sent.")

    async def send_email_alert(self, subject: str, body: str) -> None:
        if not env_bool("EMAIL_ENABLED"):
            return

        smtp_host = os.getenv("SMTP_HOST")
        smtp_port = env_int("SMTP_PORT", 587)
        smtp_username = os.getenv("SMTP_USERNAME")
        smtp_password = os.getenv("SMTP_PASSWORD")
        email_from = os.getenv("EMAIL_FROM")
        email_to = os.getenv("EMAIL_TO")

        required = [smtp_host, smtp_username, smtp_password, email_from, email_to]
        if not all(required):
            logging.warning("Email is enabled but SMTP settings are incomplete. Skipping email alert.")
            return

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = email_from
        message["To"] = email_to
        message.set_content(body)

        await asyncio.to_thread(
            self._send_email_sync,
            smtp_host,
            smtp_port,
            smtp_username,
            smtp_password,
            message,
        )
        logging.info("Email alert sent.")

    @staticmethod
    def _send_email_sync(
        smtp_host: str,
        smtp_port: int,
        smtp_username: str,
        smtp_password: str,
        message: EmailMessage,
    ) -> None:
        import smtplib

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_username, smtp_password)
            server.send_message(message)


def default_targets() -> List[ProductTarget]:
    urls = [
        (
            "GREENGREEN (BRIDGE ver.) (Signed)",
            "https://cortisofficial.us/products/greengreen-bridge-ver-signed",
        ),
        (
            "GREENGREEN (STREET ver.) (Signed)",
            "https://cortisofficial.us/products/greengreen-street-ver-signed",
        ),
        (
            "GREENGREEN (STUDIO ver.) (Signed)",
            "https://cortisofficial.us/products/greengreen-studio-ver-signed",
        ),
    ]
    return [ProductTarget(name=name, url=url, mode="stock") for name, url in urls]


def load_targets() -> List[ProductTarget]:
    if not TARGETS_FILE.exists():
        targets = default_targets()
        TARGETS_FILE.write_text(
            json.dumps(
                [{"name": target.name, "url": target.url, "mode": target.mode} for target in targets],
                indent=2,
            ),
            encoding="utf-8",
        )
        return targets

    try:
        raw_targets = json.loads(TARGETS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("targets.json is invalid. Rebuilding with default targets.")
        targets = default_targets()
        TARGETS_FILE.write_text(
            json.dumps(
                [{"name": target.name, "url": target.url, "mode": target.mode} for target in targets],
                indent=2,
            ),
            encoding="utf-8",
        )
        return targets

    targets: List[ProductTarget] = []
    for item in raw_targets:
        name = item.get("name")
        url = item.get("url")
        mode = item.get("mode", "stock")
        if name and url:
            targets.append(ProductTarget(name=name, url=url, mode=mode))

    if not targets:
        targets = default_targets()
        TARGETS_FILE.write_text(
            json.dumps(
                [{"name": target.name, "url": target.url, "mode": target.mode} for target in targets],
                indent=2,
            ),
            encoding="utf-8",
        )
    return targets


def main() -> None:
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is missing from your environment.")

    if not os.getenv("DISCORD_CHANNEL_ID"):
        raise RuntimeError("DISCORD_CHANNEL_ID is missing from your environment.")

    bot = RestockBot(load_targets())

    async def ensure_manage_permission(ctx: commands.Context) -> bool:
        if isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.manage_guild:
            return True

        await ctx.send("You need the `Manage Server` permission to manage monitored links.")
        return False

    @bot.command(name="status")
    async def status_command(ctx: commands.Context) -> None:
        if not bot.latest_statuses:
            embed = bot.build_status_embed(
                "No Product Data Yet",
                "Wait for the next scheduled check or run `!check`.",
                discord.Color.orange(),
            )
            await ctx.send(embed=embed)
            return

        embeds = bot.build_paginated_embeds(
            "Current Product Status",
            bot.build_status_lines(),
            discord.Color.blurple(),
        )
        for embed in embeds:
            await ctx.send(embed=embed)

    @bot.command(name="check")
    async def check_command(ctx: commands.Context) -> None:
        progress_embed = bot.build_status_embed(
            "Manual Check Started",
            "Running a manual stock check now...",
            discord.Color.orange(),
        )
        await ctx.send(embed=progress_embed)
        updates = await bot.manual_check()
        result_embeds = bot.build_paginated_embeds(
            "Manual Check Finished",
            bot.build_status_lines(),
            discord.Color.blurple(),
        )
        for embed in result_embeds:
            await ctx.send(embed=embed)

        for target, message in updates:
            embed = bot.build_restock_embed(target, bot.latest_statuses[target.url])
            await bot.send_discord_alert(message, embed=embed)
            await bot.send_email_alert("Restock detected", message)

    @bot.command(name="links")
    async def links_command(ctx: commands.Context) -> None:
        embeds = bot.build_paginated_embeds(
            "Monitored Links",
            bot.build_target_lines(),
            discord.Color.gold(),
        )
        for embed in embeds:
            await ctx.send(embed=embed)

    @bot.command(name="addlink")
    async def addlink_command(ctx: commands.Context, url: str) -> None:
        if not await ensure_manage_permission(ctx):
            return

        if not url.startswith("http://") and not url.startswith("https://"):
            await ctx.send("Please provide a valid URL starting with http:// or https://")
            return

        try:
            name = await bot.fetch_product_title(url)
        except Exception:
            logging.exception("Failed to fetch product title for %s", url)
            name = bot.fallback_name_from_url(url)

        if bot.add_target(name=name, url=url, mode="stock"):
            await ctx.send(f"Added link: {name}\n{url}")
            return

        await ctx.send("That link is already being monitored.")

    @bot.command(name="addqueue")
    async def addqueue_command(ctx: commands.Context, url: str) -> None:
        if not await ensure_manage_permission(ctx):
            return

        if not url.startswith("http://") and not url.startswith("https://"):
            await ctx.send("Please provide a valid URL starting with http:// or https://")
            return

        parsed_domain = urlparse(url).netloc.lower()
        if "pokemoncenter.com" not in parsed_domain:
            await ctx.send("`!addqueue` is currently intended for Pokemon Center queue monitoring.")
            return

        name = "Pokemon Center Queue"
        if bot.add_target(name=name, url=url, mode="queue"):
            await ctx.send(f"Added queue monitor: {name}\n{url}")
            return

        await ctx.send("That queue monitor is already being tracked.")

    @bot.command(name="removelink")
    async def removelink_command(ctx: commands.Context, index: int) -> None:
        if not await ensure_manage_permission(ctx):
            return

        if index < 1 or index > len(bot.targets):
            await ctx.send("Invalid link number. Use `!links` to see the current list.")
            return

        removed = bot.remove_target(index - 1)
        await ctx.send(f"Removed link: {removed.name}\n{removed.url}")

    bot.run(token)


if __name__ == "__main__":
    main()
