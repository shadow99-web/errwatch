"""
errwatch — drop-in self-debugging error reporter for Discord bots and Python scripts.

v1.1.0 — adds environment snapshot, smart dedup with auto-mute, filters,
non-async excepthook, sync reporting, shutdown flush, expanded severity.

Bot usage:
    from errwatch import ErrorWatcher
    watcher = ErrorWatcher(
        webhook_url=os.environ["ERRWATCH_WEBHOOK"],
        owner_id=int(os.environ.get("ERRWATCH_OWNER_ID", "0")),
        bot_name="starboard",
    )
    watcher.attach(bot)
    watcher.install_excepthook()

Script usage:
    watcher = ErrorWatcher(webhook_url="...", bot_name="spammer")
    watcher.install_excepthook()
    try:
        await main()
    finally:
        await watcher.shutdown()

Filtering expected errors:
    watcher = ErrorWatcher(
        webhook_url="...",
        ignore=["discord.errors.NotFound", "asyncio.CancelledError"],
        ignore_patterns=["*unknown message*", "*50035*"],
    )

Suppressing in-place:
    async with watcher.suppress(discord.NotFound, report=True):
        await msg.edit(embed=new_embed)
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import functools
import hashlib
import json
import os
import platform
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


__version__ = "1.1.0"
__all__ = ["ErrorWatcher", "CriticalError"]


_SEV_COLOR = {
    "info":     0x5865F2,
    "warning":  0xFEE75C,
    "error":    0xED4245,
    "critical": 0xFF0000,
}
_SEV_EMOJI = {
    "info":     "ℹ️",
    "warning":  "⚠️",
    "error":    "🐛",
    "critical": "🚨",
}


class CriticalError(Exception):
    """Subclass an exception with this to force owner-DM delivery alongside webhook."""


def _fmt_uptime(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"


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
        ignore: Optional[list] = None,
        ignore_patterns: Optional[list] = None,
        mute_after_hourly: int = 100,
        mute_duration_seconds: int = 86400,
    ):
        self.webhook_url        = (webhook_url or "").strip()
        self.collector_url      = (collector_url or "").strip().rstrip("/")
        self.collector_secret   = (collector_secret or "").strip()
        self.owner_id           = int(owner_id or 0)
        self.log_file           = log_file
        self.dedup_window       = dedup_window
        self.rate_limit         = rate_limit_per_hour
        self.bot_name           = bot_name
        self.ignore             = list(ignore or [])
        self.ignore_patterns    = list(ignore_patterns or [])
        self.mute_after_hourly  = mute_after_hourly
        self.mute_duration      = mute_duration_seconds

        self._start_time        = time.time()
        self._bot: Optional[Any] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock              = asyncio.Lock()
        self._dedup: dict[str, dict] = {}
        self._hourly: dict[str, dict] = {}
        self._recent_sends      = deque(maxlen=200)

    # ─── ATTACH ────────────────────────────────────────────────────

    def attach(self, bot):
        if not _DISCORD_OK:
            raise RuntimeError(
                "discord.py or discord.py-self is required for attach()."
            )
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

    # ─── EXCEPTHOOK + SYNC REPORTING ───────────────────────────────

    def install_excepthook(self):
        """Catch uncaught sync exceptions that asyncio.run() never sees."""
        original = sys.excepthook

        def hook(exc_type, exc_value, exc_tb):
            try:
                self.report_sync(
                    exc_value,
                    context={"kind": "uncaught", "sys_excepthook": True},
                    severity="critical" if issubclass(exc_type, SystemExit) else "error",
                )
            except Exception:
                pass
            try:
                original(exc_type, exc_value, exc_tb)
            except Exception:
                pass

        sys.excepthook = hook

    def report_sync(self, exc, context: Optional[dict] = None, severity: str = "error"):
        """Report an exception from sync code (no await needed)."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.report(exc, context=context, severity=severity))
        except RuntimeError:
            try:
                asyncio.run(self.report(exc, context=context, severity=severity))
            except Exception as e:
                print(f"[ERRWATCH] report_sync failed: {e}")

    # ─── DECORATOR ─────────────────────────────────────────────────

    def catch(self, func: Callable):
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

    # ─── SUPPRESS CONTEXT MANAGER ──────────────────────────────────

    @contextlib.asynccontextmanager
    async def suppress(self, *exc_types, report: bool = True, severity: str = "error"):
        """Suppress specific exception types while optionally reporting them.

        Usage:
            async with watcher.suppress(discord.NotFound, report=True):
                await msg.edit(embed=embed)
        """
        try:
            yield
        except exc_types as e:
            if report:
                await self.report(e, context={
                    "kind":     "suppressed",
                    "types":    [t.__name__ for t in exc_types],
                }, severity=severity)

    # ─── DISCORD ERROR HANDLER ─────────────────────────────────────

    async def _handle_command_error(self, ctx, error):
        from discord.ext import commands
        if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
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

    # ─── ENVIRONMENT SNAPSHOT ──────────────────────────────────────

    def _env_snapshot(self) -> dict:
        mem = "?"
        try:
            import psutil
            mem = f"{psutil.Process().memory_info().rss // 1024 // 1024}MB"
        except ImportError:
            try:
                with open("/proc/self/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            kb = int(line.split()[1])
                            mem = f"{kb // 1024}MB"
                            break
            except Exception:
                pass

        host = "?"
        try:
            host = os.uname().nodename
        except AttributeError:
            host = platform.node() or "?"

        dver = "-"
        if _DISCORD_OK:
            try:
                import discord as _d
                dver = getattr(_d, "__version__", "?")
            except Exception:
                pass

        py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        up = _fmt_uptime(int(time.time() - self._start_time))

        return {
            "py":      py,
            "os":      platform.system(),
            "host":    host,
            "mem":     mem,
            "discord": dver,
            "uptime":  up,
        }

    def _env_summary(self) -> str:
        e = self._env_snapshot()
        return (
            f"Py {e['py']} · {e['os']} · discord {e['discord']} · "
            f"up {e['uptime']} · {e['mem']}"
        )

    # ─── FILTERS ───────────────────────────────────────────────────

    def _should_ignore(self, exc) -> bool:
        tname = f"{type(exc).__module__}.{type(exc).__name__}"
        short = type(exc).__name__
        if tname in self.ignore or short in self.ignore:
            return True
        if self.ignore_patterns:
            msg = str(exc)
            for pat in self.ignore_patterns:
                if fnmatch.fnmatch(msg, pat) or fnmatch.fnmatch(tname, pat):
                    return True
        return False

    # ─── HOURLY COUNT / MUTE ───────────────────────────────────────

    def _hourly_update(self, fp: str) -> tuple[int, bool, bool]:
        """Return (hourly_count, is_muted, just_became_muted)."""
        now = time.time()
        entry = self._hourly.get(fp)
        if entry is None or now >= entry["reset_at"]:
            prev_mute = entry.get("muted_until", 0) if entry else 0
            entry = {
                "count":       1,
                "reset_at":    now + 3600,
                "muted_until": prev_mute,
            }
            self._hourly[fp] = entry
        else:
            entry["count"] += 1

        already_muted   = entry.get("muted_until", 0) > now
        just_became     = False

        if (
            entry["count"] >= self.mute_after_hourly
            and not already_muted
        ):
            entry["muted_until"] = now + self.mute_duration
            just_became = True
            already_muted = True

        self._gc_hourly(now)
        return entry["count"], already_muted, just_became

    def _gc_hourly(self, now: float):
        cutoff = now - 7200
        self._hourly = {
            k: v for k, v in self._hourly.items()
            if v["reset_at"] > cutoff or v.get("muted_until", 0) > now
        }

    # ─── REPORT ────────────────────────────────────────────────────

    async def report(
        self,
        exc: BaseException,
        context: Optional[dict] = None,
        severity: str = "error",
    ):
        try:
            context = context or {}

            if self._should_ignore(exc):
                return

            tb_full = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            fp_src = f"{type(exc).__name__}|{str(exc)[:200]}|{tb_full[-300:]}"
            fp     = hashlib.sha256(fp_src.encode()).hexdigest()[:16]
            now    = time.time()

            # Dedup window tracking
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

            # Hourly count / mute
            hourly_count, muted, just_muted = self._hourly_update(fp)

            # ALWAYS write to local log
            self._write_log(exc, tb_full, context, fp, hourly_count)

            # If already muted (and not the moment it just became muted), silence
            if muted and not just_muted:
                return

            # Dedup: milestones only
            if is_duplicate and entry["count"] not in (2, 5, 10, 25, 100):
                if not just_muted:
                    return

            # Global rate limit
            recent = [t for t in self._recent_sends if now - t < 3600]
            self._recent_sends = deque(recent, maxlen=200)
            if len(recent) >= self.rate_limit and not just_muted:
                print(f"[ERRWATCH] rate limit — skipping delivery for {type(exc).__name__}")
                return
            self._recent_sends.append(now)

            dedup_count = entry["count"] if is_duplicate else 1
            extra_note  = (
                f"AUTO-MUTED {self.mute_duration // 3600}h after {hourly_count} hits/hr"
                if just_muted else ""
            )

            if self.collector_url:
                await self._post_collector(
                    exc, tb_full, context, severity, dedup_count, fp, hourly_count
                )
            if self.webhook_url:
                await self._post_webhook(
                    exc, tb_full, context, fp, dedup_count, severity,
                    hourly_count=hourly_count, extra_note=extra_note,
                )

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

    # ─── WEBHOOK ───────────────────────────────────────────────────

    async def _post_webhook(
        self, exc, tb_full, context, fp, count, severity,
        hourly_count: int = 0, extra_note: str = "",
    ):
        if not self.webhook_url:
            return

        sev     = severity if severity in _SEV_COLOR else "error"
        emoji   = _SEV_EMOJI[sev]
        color   = _SEV_COLOR[sev]

        desc_parts = [f"```python\n{tb_full[-1800:]}\n```"]
        if extra_note:
            desc_parts.append(f"**{extra_note}**")
        desc = "\n".join(desc_parts)[:4000]

        embed = {
            "title":       f"{emoji} {type(exc).__name__}"[:256],
            "description": desc,
            "color":       color,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "footer":      {"text": f"{self.bot_name} · {fp}"},
            "fields":      [],
        }

        # Environment field always shown
        embed["fields"].append({
            "name":   "Environment",
            "value":  f"`{self._env_summary()}`"[:1024],
            "inline": False,
        })

        # Counts
        if count > 1:
            embed["fields"].append({
                "name":   "Window",
                "value":  f"×{count} in last {self.dedup_window // 60}m",
                "inline": True,
            })
        if hourly_count > 1:
            embed["fields"].append({
                "name":   "Hourly",
                "value":  f"×{hourly_count}/hr",
                "inline": True,
            })

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
                if len(embed["fields"]) >= 20:
                    break
                embed["fields"].append({
                    "name":   label,
                    "value":  f"`{str(context[key])[:500]}`",
                    "inline": True,
                })

        files: list[dict] = []
        if len(tb_full) > 1800:
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
                    await self._post_webhook(
                        exc, tb_full, context, fp, count, severity,
                        hourly_count=hourly_count, extra_note=extra_note,
                    )
                elif r.status not in (200, 204):
                    text = await r.text()
                    print(f"[ERRWATCH] webhook HTTP {r.status}: {text[:200]}")
        except Exception as e:
            print(f"[ERRWATCH] webhook send failed: {type(e).__name__}: {e}")

    # ─── COLLECTOR ─────────────────────────────────────────────────

    async def _post_collector(self, exc, tb_full, context, severity, count, fp, hourly_count):
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
                "context":       {**context, "_env": self._env_snapshot()},
                "fingerprint":   fp,
                "count":         count,
                "hourly_count":  hourly_count,
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

    # ─── OWNER DM ──────────────────────────────────────────────────

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
                title=f"🚨 CRITICAL: {type(exc).__name__}"[:256],
                description=f"```python\n{tb_full[-1800:]}\n```"[:4000],
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Environment", value=f"`{self._env_summary()}`"[:1024], inline=False)
            for k, v in context.items():
                if len(embed.fields) >= 10:
                    break
                embed.add_field(name=str(k)[:256], value=str(v)[:1000], inline=True)
            await owner.send(embed=embed)
        except Exception as e:
            print(f"[ERRWATCH] DM owner failed: {type(e).__name__}: {e}")

    # ─── FILE LOG ──────────────────────────────────────────────────

    def _write_log(self, exc, tb_full, context, fp, hourly_count):
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(f"\n{'=' * 70}\n")
                f.write(f"[{datetime.now(timezone.utc).isoformat()}] "
                        f"{type(exc).__name__}: {exc}\n")
                f.write(f"  fp: {fp}  hourly: {hourly_count}  env: {self._env_summary()}\n")
                for k, v in context.items():
                    f.write(f"  {k}: {v}\n")
                f.write(tb_full)
        except Exception:
            pass

    # ─── SHUTDOWN / CLOSE ──────────────────────────────────────────

    async def shutdown(self, timeout: float = 2.0):
        """Flush pending webhook sends, then close. Best-effort."""
        try:
            await asyncio.sleep(min(timeout, 1.0))
        except Exception:
            pass
        await self.close()

    async def close(self):
        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
