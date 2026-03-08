#!/usr/bin/env python3
"""
Enhanced Middle East + oil alert Telegram bot

What it does
- Polls Telegram for commands using getUpdates
- Polls NewsAPI for fresh Middle East / war / oil headlines
- Sends priority alerts for oil, shipping, Hormuz, Iran, Israel, Hezbollah, Gaza, Lebanon, Houthis
- Tracks crude oil futures (WTI + Brent) using Yahoo Finance chart endpoints
- Sends oil-move alerts when price changes exceed thresholds
- Deduplicates articles it has already sent
- Lets you subscribe/unsubscribe from a private chat

Required environment variables
- TELEGRAM_BOT_TOKEN=your_bot_token
- NEWSAPI_KEY=your_newsapi_key

Optional environment variables
- POLL_SECONDS=30
- NEWS_LOOKBACK_MINUTES=20
- NEWS_SOURCES=reuters,associated-press,bbc-news,al-jazeera-english,bloomberg,cbs-news,cnn,fox-news,financial-times,the-wall-street-journal,the-washington-post,abc-news,axios,newsweek
- OIL_ALERT_PCT=1.0
- OIL_ALERT_ABS=1.0
- PRIORITY_ONLY=0

How to use
1) Create a bot with @BotFather and copy the token.
2) Get a NewsAPI key from newsapi.org.
3) Export the environment variables above.
4) Install dependencies: pip3 install requests
5) Run: python3 war_bot.py
6) Open your bot in Telegram and send: /start
7) Optional commands: /stop, /status, /test, /sources, /oil, /now, /priority, /all

Notes
- This uses Telegram's HTTP Bot API directly.
- News is fetched from NewsAPI's /v2/everything endpoint.
- Oil prices are fetched from Yahoo Finance chart endpoints for WTI (CL=F) and Brent (BZ=F).
- You can change thresholds and speed using environment variables near the top.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import requests

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "war_news_state.json"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "").strip()
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
NEWS_LOOKBACK_MINUTES = int(os.getenv("NEWS_LOOKBACK_MINUTES", "20"))
NEWS_SOURCES = [
    s.strip()
    for s in os.getenv(
        "NEWS_SOURCES",
        "reuters,associated-press,bbc-news,al-jazeera-english,bloomberg,cbs-news,cnn,fox-news,financial-times,the-wall-street-journal,the-washington-post,abc-news,axios,newsweek",
    ).split(",")
    if s.strip()
]
OIL_ALERT_PCT = float(os.getenv("OIL_ALERT_PCT", "1.0"))
OIL_ALERT_ABS = float(os.getenv("OIL_ALERT_ABS", "1.0"))
PRIORITY_ONLY = os.getenv("PRIORITY_ONLY", "0").strip() == "1"

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
NEWSAPI_EVERYTHING_URL = "https://newsapi.org/v2/everything"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

REGIONAL_QUERY = (
    '(("Middle East" OR Israel OR Iran OR Gaza OR Hezbollah OR Lebanon OR '
    'Yemen OR Houthi OR Houthis OR Syria OR Iraq OR Qatar OR Bahrain OR UAE '
    'OR Oman OR Saudi OR "Strait of Hormuz" OR Hormuz OR Red\ Sea) AND '
    '(war OR strike OR missile OR drone OR attack OR ceasefire OR bombing '
    'OR invasion OR retaliation OR naval OR military OR conflict))'
)

OIL_QUERY = (
    '((oil OR crude OR brent OR wti OR energy OR tanker OR shipping OR '
    '"Strait of Hormuz" OR Hormuz OR OPEC OR refinery OR diesel OR gasoline) AND '
    '(Iran OR Israel OR Gaza OR Lebanon OR Hezbollah OR Houthi OR Houthis '
    'OR Yemen OR Red\ Sea OR Gulf OR attack OR strike OR disruption OR sanctions '
    'OR supply OR exports OR production OR tanker))'
)

PRIORITY_TERMS = {
    "hormuz",
    "strait of hormuz",
    "oil",
    "crude",
    "brent",
    "wti",
    "tanker",
    "shipping",
    "red sea",
    "pipeline",
    "refinery",
    "exports",
    "opec",
    "missile",
    "drone",
    "attack",
    "strike",
    "retaliation",
    "iran",
    "israel",
    "hezbollah",
    "houthi",
    "houthis",
}

HELP_TEXT = (
    "Commands:\n"
    "/start - subscribe this chat to alerts\n"
    "/stop - unsubscribe this chat\n"
    "/status - show current bot status\n"
    "/test - send a test message\n"
    "/sources - show tracked publishers\n"
    "/oil - show latest WTI and Brent snapshot\n"
    "/now - fetch latest headlines immediately\n"
    "/priority - only priority alerts\n"
    "/all - all matching alerts"
)


@dataclass
class BotState:
    subscribed_chat_ids: set[int] = field(default_factory=set)
    priority_only_chat_ids: set[int] = field(default_factory=set)
    sent_urls: set[str] = field(default_factory=set)
    sent_titles: set[str] = field(default_factory=set)
    last_update_id: int | None = None
    last_news_check_iso: str | None = None
    last_oil_snapshot: dict[str, Any] = field(default_factory=dict)
    last_oil_alert_key: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "subscribed_chat_ids": sorted(self.subscribed_chat_ids),
            "priority_only_chat_ids": sorted(self.priority_only_chat_ids),
            "sent_urls": sorted(self.sent_urls),
            "sent_titles": sorted(self.sent_titles),
            "last_update_id": self.last_update_id,
            "last_news_check_iso": self.last_news_check_iso,
            "last_oil_snapshot": self.last_oil_snapshot,
            "last_oil_alert_key": self.last_oil_alert_key,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "BotState":
        return cls(
            subscribed_chat_ids=set(data.get("subscribed_chat_ids", [])),
            priority_only_chat_ids=set(data.get("priority_only_chat_ids", [])),
            sent_urls=set(data.get("sent_urls", [])),
            sent_titles=set(data.get("sent_titles", [])),
            last_update_id=data.get("last_update_id"),
            last_news_check_iso=data.get("last_news_check_iso"),
            last_oil_snapshot=data.get("last_oil_snapshot", {}),
            last_oil_alert_key=data.get("last_oil_alert_key"),
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_state() -> BotState:
    if not STATE_FILE.exists():
        return BotState()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return BotState.from_json(data)
    except Exception as exc:
        print(f"[WARN] Failed to load state: {exc}")
        return BotState()


def save_state(state: BotState) -> None:
    STATE_FILE.write_text(
        json.dumps(state.to_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def require_env() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not NEWSAPI_KEY:
        missing.append("NEWSAPI_KEY")
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"Missing required environment variables: {joined}")


def telegram_request(method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{TELEGRAM_API}/{method}"
    try:
        response = requests.post(url, json=payload or {}, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Telegram request failed for {method}: {exc}") from exc

    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error for {method}: {data}")
    return data


def send_message(chat_id: int, text: str, disable_preview: bool = False) -> None:
    telegram_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_preview,
        },
    )


def get_updates(offset: int | None = None, timeout_seconds: int = 25) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"timeout": timeout_seconds}
    if offset is not None:
        payload["offset"] = offset
    data = telegram_request("getUpdates", payload)
    return data.get("result", [])


def normalise_title(title: str) -> str:
    return " ".join(title.lower().split())


def is_priority_article(article: dict[str, Any]) -> bool:
    text = " ".join(
        [
            article.get("title") or "",
            article.get("description") or "",
            article.get("content") or "",
        ]
    ).lower()
    return any(term in text for term in PRIORITY_TERMS)


def fetch_news(query: str, since_utc: datetime, page_size: int = 50) -> list[dict[str, Any]]:
    headers = {"X-Api-Key": NEWSAPI_KEY}
    params = {
        "q": query,
        "searchIn": "title,description",
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "from": since_utc.isoformat().replace("+00:00", "Z"),
        "sources": ",".join(NEWS_SOURCES),
    }

    try:
        response = requests.get(NEWSAPI_EVERYTHING_URL, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"NewsAPI request failed: {exc}") from exc

    if data.get("status") != "ok":
        raise RuntimeError(f"NewsAPI error: {data}")

    return data.get("articles", [])


def fetch_oil_symbol(symbol: str, interval: str = "5m", range_: str = "1d") -> dict[str, Any]:
    url = YAHOO_CHART_URL.format(symbol=quote_plus(symbol))
    params = {"interval": interval, "range": range_}
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
    }

    response = requests.get(url, params=params, headers=headers, timeout=20)
    response.raise_for_status()
    data = response.json()

    result = data.get("chart", {}).get("result", [{}])[0]
    meta = result.get("meta", {})
    quote = result.get("indicators", {}).get("quote", [{}])[0]
    closes = [x for x in (quote.get("close") or []) if isinstance(x, (int, float))]

    price = meta.get("regularMarketPrice")
    prev_close = meta.get("previousClose")
    if price is None and closes:
        price = closes[-1]
    if prev_close is None and len(closes) >= 2:
        prev_close = closes[0]

    change = None
    pct = None
    if isinstance(price, (int, float)) and isinstance(prev_close, (int, float)) and prev_close != 0:
        change = price - prev_close
        pct = (change / prev_close) * 100

    return {
        "symbol": symbol,
        "price": price,
        "previous_close": prev_close,
        "change": change,
        "pct": pct,
        "currency": meta.get("currency") or "USD",
        "exchange": meta.get("exchangeName") or "",
    }


def fetch_oil_snapshot() -> dict[str, Any]:
    wti = fetch_oil_symbol("CL=F")
    brent = fetch_oil_symbol("BZ=F")
    return {"WTI": wti, "Brent": brent, "timestamp": utc_now().isoformat()}


def format_price_block(label: str, data: dict[str, Any]) -> str:
    price = data.get("price")
    change = data.get("change")
    pct = data.get("pct")
    if not isinstance(price, (int, float)):
        return f"{label}: unavailable"

    out = f"{label}: ${price:.2f}"
    if isinstance(change, (int, float)) and isinstance(pct, (int, float)):
        direction = "+" if change >= 0 else ""
        out += f" ({direction}{change:.2f}, {direction}{pct:.2f}%)"
    return out


def format_oil_snapshot(snapshot: dict[str, Any]) -> str:
    return (
        "🛢 Oil snapshot\n\n"
        + format_price_block("WTI", snapshot.get("WTI", {}))
        + "\n"
        + format_price_block("Brent", snapshot.get("Brent", {}))
        + f"\n\nUpdated: {snapshot.get('timestamp', '')}"
    )


def format_article(article: dict[str, Any]) -> str:
    source = article.get("source", {}).get("name") or "Unknown source"
    title = article.get("title") or "Untitled"
    description = article.get("description") or ""
    url = article.get("url") or ""
    published_at = article.get("publishedAt") or ""

    summary = description.strip()
    if len(summary) > 220:
        summary = summary[:217].rstrip() + "..."

    alert_type = "🛢 Oil / shipping alert" if is_priority_article(article) else "🚨 Middle East update"

    return (
        f"{alert_type}\n\n"
        f"{title}\n"
        f"Source: {source}\n"
        f"Published: {published_at}\n\n"
        f"{summary}\n\n"
        f"{url}"
    ).strip()


def article_is_new(article: dict[str, Any], state: BotState) -> bool:
    url = (article.get("url") or "").strip()
    title = normalise_title(article.get("title") or "")
    if not url and not title:
        return False
    if url and url in state.sent_urls:
        return False
    if title and title in state.sent_titles:
        return False
    return True


def remember_article(article: dict[str, Any], state: BotState) -> None:
    url = (article.get("url") or "").strip()
    title = normalise_title(article.get("title") or "")
    if url:
        state.sent_urls.add(url)
    if title:
        state.sent_titles.add(title)

    if len(state.sent_urls) > 4000:
        state.sent_urls = set(list(state.sent_urls)[-3000:])
    if len(state.sent_titles) > 4000:
        state.sent_titles = set(list(state.sent_titles)[-3000:])


def send_to_subscribers(state: BotState, article: dict[str, Any]) -> None:
    message = format_article(article)
    is_priority = is_priority_article(article)

    for chat_id in sorted(state.subscribed_chat_ids):
        if PRIORITY_ONLY and not is_priority:
            continue
        if chat_id in state.priority_only_chat_ids and not is_priority:
            continue
        try:
            send_message(chat_id, message)
        except Exception as exc:
            print(f"[WARN] Failed to send article to {chat_id}: {exc}")


def maybe_send_oil_alerts(state: BotState) -> None:
    if not state.subscribed_chat_ids:
        return

    try:
        snapshot = fetch_oil_snapshot()
    except Exception as exc:
        print(f"[WARN] Oil snapshot failed: {exc}")
        return

    state.last_oil_snapshot = snapshot

    lines: list[str] = []
    fired = False
    for label in ("WTI", "Brent"):
        data = snapshot.get(label, {})
        price = data.get("price")
        change = data.get("change")
        pct = data.get("pct")
        if not isinstance(price, (int, float)):
            continue
        if isinstance(change, (int, float)) and isinstance(pct, (int, float)):
            if abs(change) >= OIL_ALERT_ABS or abs(pct) >= OIL_ALERT_PCT:
                fired = True
                lines.append(format_price_block(label, data))

    if not fired:
        save_state(state)
        return

    alert_key = json.dumps(lines, ensure_ascii=False)
    if alert_key == state.last_oil_alert_key:
        save_state(state)
        return

    message = (
        "🛢 Oil move alert\n\n"
        + "\n".join(lines)
        + "\n\nThresholds hit. Watch crude, tanker headlines, Hormuz risk, refinery disruption, and broader Middle East escalation."
    )

    for chat_id in sorted(state.subscribed_chat_ids):
        try:
            send_message(chat_id, message)
        except Exception as exc:
            print(f"[WARN] Failed to send oil alert to {chat_id}: {exc}")

    state.last_oil_alert_key = alert_key
    save_state(state)


def build_since_time(state: BotState) -> datetime:
    if state.last_news_check_iso:
        try:
            last_check = datetime.fromisoformat(state.last_news_check_iso)
            if last_check.tzinfo is None:
                last_check = last_check.replace(tzinfo=timezone.utc)
        except ValueError:
            last_check = utc_now() - timedelta(minutes=NEWS_LOOKBACK_MINUTES)
    else:
        last_check = utc_now() - timedelta(minutes=NEWS_LOOKBACK_MINUTES)
    return last_check - timedelta(minutes=2)


def poll_and_send_news(state: BotState, force_now: bool = False, one_chat_id: int | None = None) -> None:
    if not state.subscribed_chat_ids and not one_chat_id:
        state.last_news_check_iso = utc_now().isoformat()
        save_state(state)
        return

    since_utc = utc_now() - timedelta(minutes=NEWS_LOOKBACK_MINUTES) if force_now else build_since_time(state)

    regional_articles = fetch_news(REGIONAL_QUERY, since_utc, page_size=40)
    oil_articles = fetch_news(OIL_QUERY, since_utc, page_size=40)

    merged: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for article in oil_articles + regional_articles:
        key = (article.get("url") or "") + "|" + normalise_title(article.get("title") or "")
        if key in seen_keys:
            continue
        seen_keys.add(key)
        merged.append(article)

    fresh_articles = [a for a in merged if article_is_new(a, state)]
    fresh_articles.sort(key=lambda a: a.get("publishedAt") or "")

    if one_chat_id is not None:
        chosen = fresh_articles[-5:]
        if not chosen:
            send_message(one_chat_id, "No new matching headlines found right now.")
        else:
            for article in chosen:
                send_message(one_chat_id, format_article(article))
                remember_article(article, state)
        state.last_news_check_iso = utc_now().isoformat()
        save_state(state)
        return

    for article in fresh_articles:
        send_to_subscribers(state, article)
        remember_article(article, state)

    state.last_news_check_iso = utc_now().isoformat()
    save_state(state)


def handle_command(chat_id: int, text: str, state: BotState) -> None:
    command = text.split()[0].lower().strip()

    if command == "/start":
        state.subscribed_chat_ids.add(chat_id)
        save_state(state)
        send_message(
            chat_id,
            "Subscribed. You will now receive Middle East, war, shipping, and oil alerts from the tracked publishers.\n\n" + HELP_TEXT,
        )
        return

    if command == "/stop":
        state.subscribed_chat_ids.discard(chat_id)
        state.priority_only_chat_ids.discard(chat_id)
        save_state(state)
        send_message(chat_id, "Unsubscribed. You will not receive further alerts.")
        return

    if command == "/priority":
        state.subscribed_chat_ids.add(chat_id)
        state.priority_only_chat_ids.add(chat_id)
        save_state(state)
        send_message(chat_id, "Priority mode enabled. You will receive oil / shipping / high-impact escalation alerts only.")
        return

    if command == "/all":
        state.subscribed_chat_ids.add(chat_id)
        state.priority_only_chat_ids.discard(chat_id)
        save_state(state)
        send_message(chat_id, "All-alert mode enabled. You will receive all matching Middle East alerts.")
        return

    if command == "/status":
        status = (
            f"Subscribed chats: {len(state.subscribed_chat_ids)}\n"
            f"Priority-only chats: {len(state.priority_only_chat_ids)}\n"
            f"Tracked publishers: {', '.join(NEWS_SOURCES)}\n"
            f"Polling every: {POLL_SECONDS} seconds\n"
            f"Lookback window: {NEWS_LOOKBACK_MINUTES} minutes\n"
            f"Oil move threshold: ${OIL_ALERT_ABS:.2f} or {OIL_ALERT_PCT:.2f}%\n"
            f"Last news check: {state.last_news_check_iso or 'never'}"
        )
        send_message(chat_id, status)
        return

    if command == "/test":
        send_message(chat_id, "Test successful. Bot is running.")
        return

    if command == "/sources":
        send_message(chat_id, "Tracked publishers:\n- " + "\n- ".join(NEWS_SOURCES))
        return

    if command == "/oil":
        try:
            snapshot = fetch_oil_snapshot()
            state.last_oil_snapshot = snapshot
            save_state(state)
            send_message(chat_id, format_oil_snapshot(snapshot))
        except Exception as exc:
            send_message(chat_id, f"Oil snapshot failed: {exc}")
        return

    if command == "/now":
        try:
            poll_and_send_news(state, force_now=True, one_chat_id=chat_id)
        except Exception as exc:
            send_message(chat_id, f"Immediate fetch failed: {exc}")
        return

    send_message(chat_id, HELP_TEXT)


def process_updates(state: BotState) -> None:
    offset = state.last_update_id + 1 if state.last_update_id is not None else None
    updates = get_updates(offset=offset, timeout_seconds=25)

    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            state.last_update_id = update_id

        message = update.get("message") or {}
        text = message.get("text")
        chat = message.get("chat") or {}
        chat_id = chat.get("id")

        if isinstance(chat_id, int) and isinstance(text, str) and text.startswith("/"):
            handle_command(chat_id, text, state)

    save_state(state)


def main() -> None:
    require_env()
    state = load_state()
    print("[INFO] Bot started")
    print(f"[INFO] Tracking sources: {', '.join(NEWS_SOURCES)}")
    print(f"[INFO] Poll interval: {POLL_SECONDS}s")

    while True:
        try:
            process_updates(state)
            poll_and_send_news(state)
            maybe_send_oil_alerts(state)
        except KeyboardInterrupt:
            print("\n[INFO] Bot stopped by user")
            save_state(state)
            sys.exit(0)
        except Exception as exc:
            print(f"[ERROR] {exc}")
            time.sleep(15)
        else:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

