"""
errwatch — drop-in self-debugging error reporter for Discord bots and Python scripts.

Catches exceptions, formats full tracebacks, sends to a Discord webhook and/or
a collector endpoint. Dedupes, rate-limits, logs locally, never crashes on its
own failure.

Bot usage:
    from errwatch import ErrorWatcher
    watcher = ErrorWatcher(
        webhook_url=os.environ["ERRWATCH_WEBHOOK"],
        owner_id=int(os.environ.get("ERRWATCH_OWNER_ID", "0")),
        bot_name="starboard",
    )
    watcher.attach(bot)   # auto-hooks on_command_error + on_error

Script usage:
    watcher = ErrorWatcher(webhook_url="...", bot_name="spammer")
    try:
        await main()
    except Exception as e:
        await watcher.report(e, context={"script": "spam.py"})

Task decorator:
    @watcher.catch
    @tasks.loop(minutes=5)
    async def my_task():
        ...
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import aiohttp

try:
    import discord  # noqa: F401
    _DISCORD_OK = True
except ImportError:
    _DISCORD_OK = False


__version__ = "1.0.0"
__all__ = ["ErrorWatcher", "CriticalError"]


class CriticalError(Exception):
    """Subclass an exception with this to force owner-DM delivery alongside webhook."""


class ErrorWatcher:
    def __init__(
        self,
        webhook_url: str = "",
        collector_url: str = "",
        collector_secret: str = "",
        owner_id: int = 0,
        log_file: str = "errwatch.log",
        dedup_window: int = 300,
        rate_limit_per_hour: int = 10,
        bot_name: str = "bot",
    ):
        self.webhook_url       = (webhook_url or "").strip()
        self.collector_url     = (collector_url or "").strip().rstrip("/")
        self.collector_secret  = (collector_secret or "").strip()
        self.owner_id          = int(owner_id or 0)
        self.log_file          = log_file
        self.dedup_window      = dedup_window
        self.rate_limit        = rate_limit_per_hour
        self.bot_name          = bot_name

        self._bot: Optional[Any] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock             = asyncio.Lock()
        self._dedup: dict[str, dict] = {}
        self._recent_sends     = deque(maxlen=200)

    # ─── ATTACH TO BOT ─────────────────────────────────────────────

    def attach(self, bot):
        """Hook error handlers into a discord.py / discord.py-self bot."""
        if not _DISCORD_OK:
            raise RuntimeError(
                "discord.py or discord.py-self is required for attach(). "
                "Install it, or use watcher.report() manually."
            )

        from discord.ext import commands

        self._bot = bot
        try:
            self.bot_name = str(bot.user) if bot.user else self.bot_name
        except Exception:
            pass

        @bot.event
        async def on_command_error(ctx, error):
            await self._handle_command_error(ctx, error)

        @bot.event
        async def on_error(event_name, *args, **kwargs):
            exc = sys.exc_info()[1]
            if exc is None:
                return
            await self.report(exc, context={"kind": "event", "event": str(event_name)})

        print(f"[ERRWATCH] attached to {self.bot_name}")

    # ─── DECORATOR ─────────────────────────────────────────────────

    def catch(self, func: Callable):
        """Wrap an async function so exceptions are reported without crashing the caller."""
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                await self.report(e, context={
                    "kind":     "task",
                    "function": getattr(func, "__name__", "?"),
                    "module":   getattr(func, "__module__", "?"),
                })
                raise
        return wrapper

    # ─── DISCORD HANDLERS ──────────────────────────────────────────

    async def _handle_command_error(self, ctx, error):
        from discord.ext import commands

        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CheckFailure):
            return

        context = {
            "kind":    "command",
            "command": getattr(ctx.command, "qualified_name", str(ctx.command)),
        }
        try:
            if ctx.guild:
                context["guild"] = f"{ctx.guild.name} ({ctx.guild.id})"
            if ctx.author:
                context["user"] = f"{ctx.author} ({ctx.author.id})"
            if ctx.channel:
                context["channel"] = f"#{getattr(ctx.channel, 'name', ctx.channel.id)}"
            context["message"] = (ctx.message.content or "")[:200]
        except Exception:
            pass

        await self.report(error, context=context)

        try:
            await ctx.send(f"❌ `{type(error).__name__}` — logged for owner.")
        except Exception:
            pass

    # ─── PUBLIC REPORT ENTRYPOINT ──────────────────────────────────

    async def report(
        self,
        exc: BaseException,
        context: Optional[dict] = None,
        severity: str = "error",
    ):
        """Report an exception. Safe to call from anywhere."""
        try:
            context = context or {}
            tb_full = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )

            fp_src = f"{type(exc).__name__}|{str(exc)[:200]}|{tb_full[-300:]}"
            fp     = hashlib.sha256(fp_src.encode()).hexdigest()[:16]
            now    = time.time()

            async with self._lock:
                entry = self._dedup.get(fp)
                if entry and now - entry["first"] < self.dedup_window:
                    entry["count"] += 1
                    entry["last"] = now
                    is_duplicate  = True
                else:
                    self._dedup[fp] = {"count": 1, "first": now, "last": now}
                    is_duplicate    = False

                cutoff = now - self.dedup_window * 4
                self._dedup = {k: v for k, v in self._dedup.items() if v["first"] >= cutoff}

            self._write_log(exc, tb_full, context)

            if is_duplicate and entry["count"] not in (2, 5, 10, 25, 100):
                return

            recent = [t for t in self._recent_sends if now - t < 3600]
            self._recent_sends = deque(recent, maxlen=200)
            if len(recent) >= self.rate_limit:
                print(f"[ERRWATCH] rate limit — skipping delivery for {type(exc).__name__}")
                return
            self._recent_sends.append(now)

            count = entry["count"] if is_duplicate else 1

            if self.collector_url:
                await self._post_collector(exc, tb_full, context, severity, count, fp)
            if self.webhook_url:
                await self._post_webhook(exc, tb_full, context, fp, count, severity)

            if isinstance(exc, CriticalError):
                await self._dm_owner(exc, tb_full, context)

        except Exception as internal:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(f"\n[{datetime.now(timezone.utc).isoformat()}] "
                            f"ERRWATCH INTERNAL: {internal}\n")
                    f.write(traceback.format_exc())
            except Exception:
                pass

    # ─── DELIVERY ──────────────────────────────────────────────────

    async def _post_webhook(self, exc, tb_full, context, fp, count, severity):
        if not self.webhook_url:
            return
        color = {"error": 0xED4245, "warning": 0xFEE75C, "critical": 0xFF0000}.get(
            severity, 0xED4245
        )
        embed = {
            "title":       f"🐛 {type(exc).__name__}"[:256],
            "description": f"```python\n{tb_full[-1900:]}\n```"[:4000],
            "color":       color,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "footer":      {"text": f"{self.bot_name} · {fp}"},
            "fields":      [],
        }
        if count > 1:
            embed["fields"].append(
                {"name": "Occurrences", "value": f"×{count}", "inline": True}
            )
        label_map = {
            "kind":     "Kind",
            "command":  "Command",
            "guild":    "Guild",
            "user":     "User",
            "channel":  "Channel",
            "message":  "Message",
            "event":    "Event",
            "script":   "Script",
            "function": "Function",
            "module":   "Module",
        }
        for key, label in label_map.items():
            if key in context:
                embed["fields"].append(
                    {"name": label, "value": f"`{str(context[key])[:500]}`", "inline": True}
                )

        files: list[dict] = []
        if len(tb_full) > 1900:
            files.append({"name": f"{type(exc).__name__}.txt", "content": tb_full.encode()})

        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()

            form = aiohttp.FormData()
            form.add_field("payload_json", json.dumps({"embeds": [embed]}))
            for i, f in enumerate(files):
                form.add_field(
                    f"files[{i}]", f["content"],
                    filename=f["name"], content_type="text/plain",
                )

            async with self._session.post(
                self.webhook_url, data=form,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status == 429:
                    try:
                        data = await r.json()
                    except Exception:
                        data = {}
                    wait = float(data.get("retry_after", 2))
                    await asyncio.sleep(wait + 1)
                    await self._post_webhook(exc, tb_full, context, fp, count, severity)
                elif r.status not in (200, 204):
                    text = await r.text()
                    print(f"[ERRWATCH] webhook HTTP {r.status}: {text[:200]}")
        except Exception as e:
            print(f"[ERRWATCH] webhook send failed: {type(e).__name__}: {e}")

    async def _post_collector(self, exc, tb_full, context, severity, count, fp):
        if not self.collector_url:
            return
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()

            headers = {"Content-Type": "application/json"}
            if self.collector_secret:
                headers["X-Errwatch-Token"] = self.collector_secret

            payload = {
                "bot_name":      self.bot_name,
                "severity":      severity,
                "error_type":    type(exc).__name__,
                "error_message": str(exc)[:1000],
                "traceback":     tb_full[:8000],
                "context":       context,
                "fingerprint":   fp,
                "count":         count,
            }

            async with self._session.post(
                f"{self.collector_url}/report",
                headers=headers, json=payload,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status not in (200, 201):
                    text = await r.text()
                    print(f"[ERRWATCH] collector HTTP {r.status}: {text[:200]}")
        except Exception as e:
            print(f"[ERRWATCH] collector send failed: {type(e).__name__}: {e}")

    async def _dm_owner(self, exc, tb_full, context):
        if not _DISCORD_OK or not self.owner_id or not self._bot:
            return
        try:
            import discord

            owner = self._bot.get_user(self.owner_id)
            if owner is None:
                owner = await self._bot.fetch_user(self.owner_id)
            if owner is None:
                return

            embed = discord.Embed(
                title=f" CRITICAL: {type(exc).__name__}"[:256],
                description=f"```python\n{tb_full[-1800:]}\n```"[:4000],
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc),
            )
            for k, v in context.items():
                if len(embed.fields) >= 10:
                    break
                embed.add_field(name=str(k)[:256], value=str(v)[:1000], inline=True)
            await owner.send(embed=embed)
        except Exception as e:
            print(f"[ERRWATCH] DM owner failed: {type(e).__name__}: {e}")

    # ─── FILE LOG ──────────────────────────────────────────────────

    def _write_log(self, exc, tb_full, context):
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(f"\n{'=' * 70}\n")
                f.write(f"[{datetime.now(timezone.utc).isoformat()}] "
                        f"{type(exc).__name__}: {exc}\n")
                for k, v in context.items():
                    f.write(f"  {k}: {v}\n")
                f.write(tb_full)
        except Exception:
            pass

    # ─── CLEANUP ───────────────────────────────────────────────────

    async def close(self):
        """Close the internal HTTP session. Call on shutdown if you created a watcher."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
