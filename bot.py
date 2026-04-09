import asyncio
import json
import logging
import os
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord.ext import commands
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = Path(os.getenv("STATE_FILE_PATH", str(BASE_DIR / "state.json")))
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


@dataclass
class ProductStatus:
    in_stock: bool
    title: str
    price: Optional[str]
    summary: str


class RestockBot(commands.Bot):
    def __init__(self, targets: List[ProductTarget]) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.targets = targets
        self.channel_id = int(os.environ["DISCORD_CHANNEL_ID"])
        self.check_interval = env_int("CHECK_INTERVAL_SECONDS", 300)
        self.mention_role_id = os.getenv("DISCORD_MENTION_ROLE_ID")
        self.state = self.load_state()
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
        updates: List[str] = []

        for target in self.targets:
            status = await self.fetch_product_status(target)
            previous = self.state.get(target.url)
            self.state[target.url] = status.in_stock

            if previous is None:
                logging.info("Initial state for %s set to %s", target.name, status.in_stock)
                continue

            if not previous and status.in_stock:
                updates.append(self.build_restock_message(target, status))
            elif previous and not status.in_stock:
                logging.info("%s is out of stock again.", target.name)

        self.save_state()

        if updates:
            for message in updates:
                await self.send_discord_alert(message)
                await self.send_email_alert("Restock detected", message)

    async def fetch_product_status(self, target: ProductTarget) -> ProductStatus:
        if self.session is None:
            raise RuntimeError("HTTP session is not initialized")

        async with self.session.get(target.url) as response:
            response.raise_for_status()
            html = await response.text()

        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text(" ", strip=True).lower()

        title = self.extract_title(soup) or target.name
        price = self.extract_price(soup)
        in_stock = self.detect_in_stock(page_text)
        summary = "In stock" if in_stock else "Sold out"

        logging.info("%s checked: %s", target.name, summary)
        return ProductStatus(in_stock=in_stock, title=title, price=price, summary=summary)

    @staticmethod
    def extract_title(soup: BeautifulSoup) -> Optional[str]:
        heading = soup.find("h1")
        if heading:
            return heading.get_text(strip=True)
        return None

    @staticmethod
    def extract_price(soup: BeautifulSoup) -> Optional[str]:
        for selector in [
            '[data-product-price]',
            '.price-item--regular',
            '.price',
        ]:
            node = soup.select_one(selector)
            if node:
                text = node.get_text(" ", strip=True)
                if text:
                    return text
        return None

    @staticmethod
    def detect_in_stock(page_text: str) -> bool:
        sold_out_markers = [
            "sold out",
            "sorry sold out",
            "out of stock",
            "unavailable",
        ]
        positive_markers = [
            "add to cart",
            "buy it now",
            "pre-order",
        ]

        if any(marker in page_text for marker in sold_out_markers):
            return False

        if any(marker in page_text for marker in positive_markers):
            return True

        return False

    def build_restock_message(self, target: ProductTarget, status: ProductStatus) -> str:
        mention = f"<@&{self.mention_role_id}> " if self.mention_role_id else ""
        price_line = f"\nPrice: {status.price}" if status.price else ""
        return (
            f"{mention}Restock detected for {status.title}!\n"
            f"Status: {status.summary}{price_line}\n"
            f"Link: {target.url}"
        )

    async def send_startup_message(self) -> None:
        message = (
            "Restock bot is online and monitoring 3 products.\n"
            f"Check interval: {self.check_interval} seconds."
        )
        await self.send_discord_alert(message)

    async def send_discord_alert(self, message: str) -> None:
        channel = self.get_channel(self.channel_id)
        if channel is None:
            channel = await self.fetch_channel(self.channel_id)

        if not isinstance(channel, discord.abc.Messageable):
            raise TypeError("Configured Discord channel is not messageable")

        await channel.send(message)
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


def load_targets() -> List[ProductTarget]:
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
    return [ProductTarget(name=name, url=url) for name, url in urls]


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
    bot.run(token)


if __name__ == "__main__":
    main()
