"""
errwatch — drop-in self-debugging error reporter for Discord bots and Python scripts.

v1.3.0 — adds:
  · Universal auto-breadcrumbs (prefix commands, slash commands, events)
  · attach_breadcrumbs_only() — non-invasive for bots with custom error handlers
  · attach_tree() — slash command tracking
  · track_task() decorator — wraps tasks.loop with breadcrumbs
  · track() context manager — breadcrumbs for arbitrary async blocks
  · track_events() — add tracking to specific Discord events at runtime
  · Log rotation (log_max_bytes + log_backup_count)
  · Text-symbol based emojis (fully customizable)
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
import random
import sys
import time
import traceback as tb_module
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import aiohttp

try:
    import discord  # noqa: F401
    _DISCORD_OK = True
except ImportError:
    _DISCORD_OK = False


__version__ = "1.3.0"
__all__ = ["ErrorWatcher", "CriticalError"]


_SEV_COLOR = {
    "info":     0x5865F2,
    "warning":  0xFEE75C,
    "error":    0xED4245,
    "critical": 0xFF0000,
}

# Your requested character set
_EMOJI_DEFAULTS: dict[str, str] = {
    "error":       "✕",     # Heavy multiplication X
    "warning":     "▲",     # Up-pointing triangle
    "critical":    "☠",     # Skull and crossbones
    "info":        "ⓘ",     # Circled I
    "fix":         "⚒",     # Hammer and pick
    "bread":       "↳",     # Downwards arrow with tip rightwards
    "muted":       "⊘",     # Circled division slash
    "new":         "✦",     # Black four pointed star
    "env":         "⌂",     # House
    "config":      "☰",     # Trigram for heaven
    "stats":       "▦",     # Square with orthogonal crosshatch fill
    "commands":    "⌘",     # Place of interest sign
    "reply_error": "✘",     # Heavy ballot X
}

_RECOVERY_SUGGESTIONS: dict[str, str] = {
    "AttributeError": "Method or attribute missing — check spelling, or the object type changed. Often a version mismatch or a missing method in a class.",
    "TypeError": "Wrong argument type or count. Check the function signature vs what's being passed.",
    "KeyError": "Dict key not found. Use `.get(key, default)` or check `if key in dict` first.",
    "IndexError": "List index out of range. Check `len(list)` before indexing.",
    "ValueError": "Value is valid type but wrong content. Check input validation.",
    "NameError": "Name used before definition. Classic typo or variable scoped incorrectly.",
    "ImportError": "Module or name not available. Check requirements.txt and pip install.",
    "ModuleNotFoundError": "Package not installed in this environment. Add to requirements.txt.",
    "RuntimeError": "Generic runtime error — read the message; often 'event loop is closed' or 'coroutine never awaited'.",
    "discord.errors.Forbidden": "Bot's role too low OR missing a permission. Check Server Settings → Roles and channel permissions.",
    "discord.errors.NotFound": "Message/channel/user deleted between fetch and use. Wrap in try/except or check `.deleted`.",
    "discord.errors.HTTPException": "Discord API rejected the request. Check status — 400 often means malformed embed or form body.",
    "asyncio.TimeoutError": "Operation exceeded timeout. Consider increasing it or retrying with backoff.",
    "aiohttp.ClientConnectorError": "Network can't reach host. DNS or target server down.",
    "sqlite3.OperationalError": "SQLite error — often 'database is locked' or schema mismatch.",
    "aiosqlite.Error": "SQLite error via aiosqlite. Check SQL syntax and connection state.",
}

_DEFAULT_EVENTS = [
    "on_guild_join",
    "on_guild_remove",
    "on_member_join",
    "on_member_remove",
]


def _fmt_uptime(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"


def _filter_traceback(exc: BaseException, project_dirs: Optional[list] = None) -> list[str]:
    tb = exc.__traceback__
    frames = tb_module.extract_tb(tb)
    if not frames:
        return [f"{type(exc).__name__}: {exc}"]

    def is_project(filename: str) -> bool:
        if project_dirs:
            return any(filename.startswith(d) for d in project_dirs)
        return (
            "site-packages" not in filename
            and "dist-packages" not in filename
            and "/lib/python" not in filename
            and "/lib64/python" not in filename
        )

    out = ["Traceback (most recent call last):"]
    hidden = 0
    for frame in frames:
        if is_project(frame.filename):
            if hidden > 0:
                out.append(f"  [... {hidden} library frame(s) hidden ...]")
                hidden = 0
            out.append(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}')
            if frame.line:
                out.append(f"    {frame.line}")
        else:
            hidden += 1
    if hidden > 0:
        out.append(f"  [... {hidden} library frame(s) hidden ...]")
    out.append(f"{type(exc).__name__}: {exc}")

    chained = exc.__cause__ or exc.__context__
    if chained and chained is not exc:
        out.append("")
        out.append("--- caused by ---")
        out.extend(_filter_traceback(chained, project_dirs))
    return out


class CriticalError(Exception):
    """Subclass to force owner-DM in addition to webhook."""


class _Breadcrumbs:
    def __init__(self, max_size: int = 20):
        self._buf: deque = deque(maxlen=max_size)

    def add(self, event: str, data: dict):
        self._buf.append({"t": time.time(), "event": event, "data": data})

    def snapshot(self, limit: int = 10) -> list[dict]:
        return list(self._buf)[-limit:]

    def clear(self):
        self._buf.clear()


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
        project_dirs: Optional[list] = None,
        config_provider: Optional[Callable[[], dict]] = None,
        test_mode: bool = False,
        sample_rate: float = 1.0,
        breadcrumb_max: int = 20,
        breadcrumb_show: int = 10,
        show_suggestions: bool = True,
        show_env: bool = True,
        show_stats: bool = True,
        auto_breadcrumbs: bool = True,
        silent_commands: Optional[set] = None,
        breadcrumb_include_args: bool = True,
        breadcrumb_include_attachments: bool = True,
        auto_events: bool = True,
        extra_events: Optional[set] = None,
        log_max_bytes: int = 0,
        log_backup_count: int = 3,
        use_emojis: bool = True,
        emojis: Optional[dict] = None,
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

        self.project_dirs       = list(project_dirs) if project_dirs else None
        self.config_provider    = config_provider
        self.test_mode          = bool(test_mode)
        self.sample_rate        = max(0.0, min(1.0, float(sample_rate)))
        self.show_suggestions   = show_suggestions
        self.show_env           = show_env
        self.show_stats         = show_stats
        self._breadcrumbs       = _Breadcrumbs(breadcrumb_max)
        self._breadcrumb_show   = breadcrumb_show
        self._config_cache: dict = {}
        self._config_cache_t: float = 0.0
        self._config_ttl: float = 60.0
        self._seen_fingerprints: set = set()
        self._max_fingerprints: int = 5000
        self._command_stats: dict[str, dict] = {}

        self.auto_breadcrumbs   = bool(auto_breadcrumbs)
        self.silent_commands    = set(silent_commands or [])
        self.breadcrumb_include_args = breadcrumb_include_args
        self.breadcrumb_include_attachments = breadcrumb_include_attachments
        self.auto_events        = bool(auto_events)
        self.extra_events       = set(extra_events or [])
        self.log_max_bytes      = int(log_max_bytes)
        self.log_backup_count   = int(log_backup_count)
        self.use_emojis         = bool(use_emojis)
        self.emojis             = dict(emojis or {})
        
        self._command_timings: dict[int, float] = {}
        self._hooked_events: set = set()
        self._hooked_bots: set = set()
        self._hooked_trees: set = set()

        self._start_time        = time.time()
        self._bot: Optional[Any] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock              = asyncio.Lock()
        self._dedup: dict[str, dict] = {}
        self._hourly: dict[str, dict] = {}
        self._recent_sends      = deque(maxlen=200)

    def breadcrumb(self, event: str, **data):
        self._breadcrumbs.add(event, data)

    def clear_breadcrumbs(self):
        self._breadcrumbs.clear()

    def attach(self, bot):
        if not _DISCORD_OK:
            raise RuntimeError("discord.py or discord.py-self is required for attach()")
        if id(bot) in self._hooked_bots:
            return
        self._hooked_bots.add(id(bot))
        self._bot = bot
        try:
            self.bot_name = str(bot.user) if bot.user else self.bot_name
        except Exception:
            pass

        @bot.event
        async def on_error(event_name, *args, **kwargs):
            exc = sys.exc_info()[1]
            if exc is None:
                return
            if self.auto_breadcrumbs:
                self.breadcrumb(f"event_error:{event_name}", error=type(exc).__name__)
            await self.report(exc, context={"kind": "event", "event": str(event_name)})

        if hasattr(bot, "on_command_error") and hasattr(bot, "on_command"):
            @bot.event
            async def on_command_error(ctx, error):
                await self._handle_command_error(ctx, error)

        self.attach_breadcrumbs_only(bot)
        print(f"[ERRWATCH] attached to {self.bot_name}")

    def attach_breadcrumbs_only(self, bot):
        if not _DISCORD_OK:
            return
        if hasattr(bot, "on_command") and hasattr(bot, "add_listener"):
            if self.auto_breadcrumbs:
                try:
                    bot.add_listener(self._on_command, "on_command")
                    bot.add_listener(self._on_command_completion, "on_command_completion")
                except Exception:
                    pass
        tree = getattr(bot, "tree", None)
        if tree is not None:
            self.attach_tree(tree)
        self._attach_events(bot)

    def attach_tree(self, tree):
        if id(tree) in self._hooked_trees:
            return
        self._hooked_trees.add(id(tree))
        original_error = tree.on_error
        watcher = self

        async def wrapped_error(interaction, error):
            try:
                if watcher.auto_breadcrumbs:
                    cmd = getattr(interaction.command, "qualified_name", "?") if interaction.command else "?"
                    watcher.breadcrumb("slash_command_error", cmd=cmd, error=type(error).__name__)
                ctx = {
                    "kind":    "slash",
                    "command": getattr(interaction.command, "qualified_name", "?") if interaction.command else "?",
                    "guild":   f"{interaction.guild.name} ({interaction.guild.id})" if interaction.guild else "DM",
                    "user":    f"{interaction.user} ({interaction.user.id})" if interaction.user else "?",
                }
                await watcher.report(error, context=ctx)
            except Exception:
                pass
            try:
                await original_error(interaction, error)
            except Exception:
                pass
        tree.on_error = wrapped_error

        async def on_interaction(interaction):
            try:
                if interaction.type.name != "application_command":
                    return
                if not self.auto_breadcrumbs or not interaction.command:
                    return
                cmd = interaction.command.qualified_name
                if cmd in self.silent_commands:
                    return
                data = {"cmd": cmd}
                if self.breadcrumb_include_args:
                    ns = getattr(interaction, "namespace", None)
                    if ns is not None:
                        for k in dir(ns):
                            if k.startswith("_"): continue
                            try:
                                v = getattr(ns, k)
                                if v is None or callable(v): continue
                                data[f"arg.{k}"] = self._serialize_arg_value(v)
                            except Exception: pass
                if interaction.guild:
                    data["guild_id"] = interaction.guild.id
                if interaction.user:
                    data["user_id"] = interaction.user.id
                self.breadcrumb("slash_command_start", **data)
            except Exception:
                pass
        try:
            tree.client.add_listener(on_interaction, "on_interaction")
        except Exception:
            pass

    def track_events(self, bot, *event_names):
        if not _DISCORD_OK or not hasattr(bot, "add_listener"):
            return
        for name in event_names:
            self._hook_single_event(bot, name)

    def _attach_events(self, bot):
        if not hasattr(bot, "add_listener"):
            return
        wanted = set()
        if self.auto_events:
            wanted.update(_DEFAULT_EVENTS)
        wanted.update(self.extra_events)
        for name in wanted:
            self._hook_single_event(bot, name)

    def _hook_single_event(self, bot, event_name: str):
        if not event_name.startswith("on_"):
            event_name = f"on_{event_name}"
        key = (id(bot), event_name)
        if key in self._hooked_events:
            return
        self._hooked_events.add(key)
        hook = self._make_event_hook(event_name)
        try:
            bot.add_listener(hook, event_name)
        except Exception:
            pass

    def _make_event_hook(self, event_name: str):
        watcher = self
        async def hook(*args, **kwargs):
            try:
                if not watcher.auto_breadcrumbs: return
                data = {}
                if args:
                    first = args[0]
                    for attr in ("guild", "author", "channel"):
                        try:
                            val = getattr(first, attr, None)
                            if val is not None and hasattr(val, "id"):
                                data[f"{attr}_id"] = val.id
                        except Exception: pass
                    for attr in ("id", "name", "display_name"):
                        try:
                            val = getattr(first, attr, None)
                            if isinstance(val, (int, str)):
                                data[attr] = str(val)[:50]
                        except Exception: pass
                watcher.breadcrumb(f"event:{event_name}", **data)
            except Exception:
                pass
        return hook

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

    def track_task(self, name: str):
        def decorator(func):
            if hasattr(func, "coro") and hasattr(func, "start"):
                original_coro = func.coro
                @functools.wraps(original_coro)
                async def new_coro(*args, **kwargs):
                    self.breadcrumb(f"task_start:{name}")
                    try:
                        return await original_coro(*args, **kwargs)
                    except Exception as e:
                        self.breadcrumb(f"task_error:{name}", error=type(e).__name__)
                        raise
                    finally:
                        self.breadcrumb(f"task_done:{name}")
                func.coro = new_coro
                return func

            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                self.breadcrumb(f"task_start:{name}")
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    self.breadcrumb(f"task_error:{name}", error=type(e).__name__)
                    raise
                finally:
                    self.breadcrumb(f"task_done:{name}")
            return wrapper
        return decorator

    @contextlib.asynccontextmanager
    async def track(self, name: str, **context):
        self.breadcrumb(f"track_start:{name}", **context)
        start = time.time()
        try:
            yield
            runtime_ms = int((time.time() - start) * 1000)
            self.breadcrumb(f"track_done:{name}", runtime_ms=runtime_ms)
        except Exception as e:
            runtime_ms = int((time.time() - start) * 1000)
            self.breadcrumb(f"track_error:{name}", error=type(e).__name__, runtime_ms=runtime_ms)
            raise

    @contextlib.asynccontextmanager
    async def suppress(self, *exc_types, report: bool = True, severity: str = "error"):
        try:
            yield
        except exc_types as e:
            if report:
                await self.report(e, context={
                    "kind":  "suppressed",
                    "types": [t.__name__ for t in exc_types],
                }, severity=severity)

    def install_excepthook(self):
        original = sys.excepthook
        def hook(exc_type, exc_value, exc_tb):
            try:
                self.report_sync(
                    exc_value,
                    context={"kind": "uncaught", "sys_excepthook": True},
                    severity="critical" if issubclass(exc_type, SystemExit) else "error",
                )
            except Exception: pass
            try:
                original(exc_type, exc_value, exc_tb)
            except Exception: pass
        sys.excepthook = hook

    def report_sync(self, exc, context: Optional[dict] = None, severity: str = "error"):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.report(exc, context=context, severity=severity))
        except RuntimeError:
            try:
                asyncio.run(self.report(exc, context=context, severity=severity))
            except Exception as e:
                print(f"[ERRWATCH] report_sync failed: {e}")

    def _serialize_arg_value(self, arg) -> str:
        try:
            if arg is None: return "None"
            if isinstance(arg, bool): return str(arg)
            if isinstance(arg, (int, float)): return str(arg)
            if isinstance(arg, str): return arg[:100] + ("…" if len(arg) > 100 else "")
            if isinstance(arg, (list, tuple)):
                if len(arg) > 3: return f"[{len(arg)} items]"
                return "[" + ",".join(self._serialize_arg_value(a) for a in arg) + "]"
            if isinstance(arg, dict): return f"{{{len(arg)} keys}}"
            if hasattr(arg, "filename"): return f"Attachment({arg.filename})"
            if hasattr(arg, "id") and hasattr(arg, "name"): return f"{arg.__class__.__name__}({arg.name}, {arg.id})"
            if hasattr(arg, "id"): return f"{arg.__class__.__name__}({arg.id})"
            return f"<{type(arg).__name__}>"
        except Exception:
            return "<?>"

    def _extract_command_args(self, ctx) -> dict:
        try:
            result = {}
            skip = [ctx]
            if ctx.cog is not None:
                skip.append(ctx.cog)
            params = list(ctx.command.clean_params.values()) if ctx.command else []
            pos_idx = 0
            for arg in ctx.args:
                if any(arg is s for s in skip):
                    continue
                name = params[pos_idx].name if pos_idx < len(params) else f"arg{pos_idx}"
                result[name] = self._serialize_arg_value(arg)
                pos_idx += 1
            for k, v in (ctx.kwargs or {}).items():
                result[k] = self._serialize_arg_value(v)
            return result
        except Exception:
            return {}

    async def _on_command(self, ctx):
        try:
            if not ctx.command or ctx.command.qualified_name in self.silent_commands:
                return
            now = time.time()
            self._gc_command_timings(now)
            try:
                self._command_timings[ctx.message.id] = now
            except Exception:
                pass
            data = {"cmd": ctx.command.qualified_name}
            if self.breadcrumb_include_args:
                for k, v in self._extract_command_args(ctx).items():
                    data[f"arg.{k}"] = v
            if ctx.guild: data["guild_id"] = ctx.guild.id
            if ctx.author: data["user_id"] = ctx.author.id
            if self.breadcrumb_include_attachments and ctx.message.attachments:
                atts = [f"{a.filename}({a.size}B)" for a in ctx.message.attachments[:3]]
                data["attachments"] = ",".join(atts)
            self.breadcrumb("command_start", **data)
        except Exception:
            pass

    async def _on_command_completion(self, ctx):
        try:
            if not ctx.command or ctx.command.qualified_name in self.silent_commands:
                return
            start = self._command_timings.pop(ctx.message.id, None)
            data = {"cmd": ctx.command.qualified_name}
            if start is not None:
                data["runtime_ms"] = int((time.time() - start) * 1000)
            self.breadcrumb("command_done", **data)
        except Exception:
            pass

    def _gc_command_timings(self, now: float):
        cutoff = now - 3600
        self._command_timings = {k: v for k, v in self._command_timings.items() if v >= cutoff}

    async def _handle_command_error(self, ctx, error):
        from discord.ext import commands
        if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
            return

        context = {
            "kind":    "command",
            "command": getattr(ctx.command, "qualified_name", str(ctx.command)),
        }
        try:
            if ctx.guild: context["guild"] = f"{ctx.guild.name} ({ctx.guild.id})"
            if ctx.author: context["user"] = f"{ctx.author} ({ctx.author.id})"
            if ctx.channel: context["channel"] = f"#{getattr(ctx.channel, 'name', ctx.channel.id)}"
            context["message"] = (ctx.message.content or "")[:200]
        except Exception:
            pass

        if self.auto_breadcrumbs and ctx.command:
            try:
                start = self._command_timings.pop(ctx.message.id, None)
                data = {"cmd": ctx.command.qualified_name, "error": type(error).__name__}
                if start is not None:
                    data["runtime_ms"] = int((time.time() - start) * 1000)
                self.breadcrumb("command_error", **data)
            except Exception:
                pass

        await self.report(error, context=context)

        try:
            await ctx.send(f"{self._em('reply_error')}`{type(error).__name__}` — logged for owner.")
        except Exception:
            pass

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
                            mem = f"{int(line.split()[1]) // 1024}MB"
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
        return {
            "py":      f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "os":      platform.system(),
            "host":    host,
            "mem":     mem,
            "discord": dver,
            "uptime":  _fmt_uptime(int(time.time() - self._start_time)),
        }

    def _env_summary(self) -> str:
        e = self._env_snapshot()
        return f"Py {e['py']} · {e['os']} · discord {e['discord']} · up {e['uptime']} · {e['mem']}"

    def _em(self, key: str) -> str:
        if not self.use_emojis: return ""
        if key in self.emojis:
            emoji = self.emojis[key]
        else:
            emoji = _EMOJI_DEFAULTS.get(key, "")
        if not emoji: return ""
        return f"{emoji} "

    def _get_config_snapshot(self) -> dict:
        if self.config_provider is None:
            return {}
        now = time.time()
        if now - self._config_cache_t < self._config_ttl:
            return self._config_cache
        try:
            val = self.config_provider() if callable(self.config_provider) else self.config_provider
            if not isinstance(val, dict):
                val = dict(val) if val else {}
            out = {str(k): str(v)[:200] for k, v in val.items()}
            self._config_cache = out
            self._config_cache_t = now
            return out
        except Exception:
            return {}

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

    def _should_sample(self) -> bool:
        if self.sample_rate >= 1.0: return True
        if self.sample_rate <= 0.0: return False
        return random.random() < self.sample_rate

    def _is_new_fingerprint(self, fp: str) -> bool:
        if fp in self._seen_fingerprints: return False
        if len(self._seen_fingerprints) >= self._max_fingerprints: return False
        self._seen_fingerprints.add(fp)
        return True

    def _suggest_fix(self, exc) -> str:
        tname = type(exc).__name__
        full = f"{type(exc).__module__}.{tname}"
        return _RECOVERY_SUGGESTIONS.get(full) or _RECOVERY_SUGGESTIONS.get(tname, "")

    def _hourly_update(self, fp: str) -> tuple[int, bool, bool]:
        now = time.time()
        entry = self._hourly.get(fp)
        if entry is None or now >= entry["reset_at"]:
            prev_mute = entry.get("muted_until", 0) if entry else 0
            entry = {"count": 1, "reset_at": now + 3600, "muted_until": prev_mute}
            self._hourly[fp] = entry
        else:
            entry["count"] += 1
        already_muted = entry.get("muted_until", 0) > now
        just_became = False
        if entry["count"] >= self.mute_after_hourly and not already_muted:
            entry["muted_until"] = now + self.mute_duration
            just_became = True
            already_muted = True
        self._gc_hourly(now)
        return entry["count"], already_muted, just_became

    def _gc_hourly(self, now: float):
        cutoff = now - 7200
        self._hourly = {k: v for k, v in self._hourly.items() if v["reset_at"] > cutoff or v.get("muted_until", 0) > now}

    def _track_command(self, command: str):
        if not command: return
        now = time.time()
        s = self._command_stats.setdefault(command, {"total": 0, "hourly": deque(maxlen=100)})
        s["total"] += 1
        s["hourly"].append(now)
        cutoff = now - 3600
        while s["hourly"] and s["hourly"][0] < cutoff:
            s["hourly"].popleft()

    def _command_stats_for(self, command: str) -> dict:
        s = self._command_stats.get(command)
        if not s: return {}
        return {"total": s["total"], "hourly": len(s["hourly"])}

    async def report(self, exc: BaseException, context: Optional[dict] = None, severity: str = "error"):
        try:
            context = context or {}
            if self._should_ignore(exc): return
            if not self._should_sample(): return

            tb_full = "".join(tb_module.format_exception(type(exc), exc, exc.__traceback__))
            fp_src = f"{type(exc).__name__}|{str(exc)[:200]}|{tb_full[-300:]}"
            fp     = hashlib.sha256(fp_src.encode()).hexdigest()[:16]
            now    = time.time()
            is_new = self._is_new_fingerprint(fp)

            async with self._lock:
                entry = self._dedup.get(fp)
                if entry and now - entry["first"] < self.dedup_window:
                    entry["count"] += 1
                    entry["last"] = now
                    is_duplicate = True
                else:
                    self._dedup[fp] = {"count": 1, "first": now, "last": now, "commands": set()}
                    entry = self._dedup[fp]
                    is_duplicate = False
                cmd = context.get("command")
                if cmd: self._dedup[fp]["commands"].add(cmd)
                cutoff = now - self.dedup_window * 4
                self._dedup = {k: v for k, v in self._dedup.items() if v["first"] >= cutoff}

            cmd = context.get("command")
            if cmd: self._track_command(cmd)

            hourly_count, muted, just_muted = self._hourly_update(fp)
            self._write_log(exc, tb_full, context, fp, hourly_count)

            if muted and not just_muted: return
            if is_duplicate and entry["count"] not in (2, 5, 10, 25, 100):
                if not just_muted: return

            recent = [t for t in self._recent_sends if now - t < 3600]
            self._recent_sends = deque(recent, maxlen=200)
            if len(recent) >= self.rate_limit and not just_muted:
                return
            self._recent_sends.append(now)

            dedup_count = entry["count"] if is_duplicate else 1
            extra_note = f"AUTO-MUTED {self.mute_duration // 3600}h after {hourly_count} hits/hr" if just_muted else ""

            if self.test_mode:
                print(f"[ERRWATCH] test_mode — would deliver: {type(exc).__name__}: {exc}")
                return

            if self.collector_url:
                await self._post_collector(exc, tb_full, context, severity, dedup_count, fp, hourly_count, is_new)
            if self.webhook_url:
                await self._post_webhook(exc, tb_full, context, fp, dedup_count, severity, hourly_count, extra_note, is_new, entry.get("commands", set()))
            if isinstance(exc, CriticalError):
                await self._dm_owner(exc, tb_full, context)

        except Exception as internal:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(f"\n[{datetime.now(timezone.utc).isoformat()}] ERRWATCH INTERNAL: {internal}\n")
                    f.write(tb_module.format_exc())
            except Exception:
                pass

    async def _post_webhook(self, exc, tb_full, context, fp, count, severity, hourly_count=0, extra_note="", is_new=False, commands=None):
        if not self.webhook_url: return
        sev   = severity if severity in _SEV_COLOR else "error"
        color = _SEV_COLOR[sev]

        filtered = _filter_traceback(exc, self.project_dirs)
        filtered_text = "\n".join(filtered)

        title = f"{self._em(sev)}{type(exc).__name__}"
        if is_new: title = f"{self._em('new')}{title}"
        if extra_note: title = f"{title}  {self._em('warning')}{extra_note}"

        if len(filtered_text) <= 1700:
            desc = f"```python\n{filtered_text}\n```"
        else:
            desc = f"```python\n{filtered_text[:1500]}\n[... see attached file ...]\n```"

        embed = {
            "title":       title[:256],
            "description": desc[:4000],
            "color":       color,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "footer":      {"text": f"{self.bot_name} · {fp}"},
            "fields":      [],
        }

        if self.show_env:
            embed["fields"].append({"name": f"{self._em('env')}Environment", "value": f"`{self._env_summary()}`"[:1024], "inline": False})

        cfg = self._get_config_snapshot()
        if cfg:
            cfg_text = "  ·  ".join(f"`{k}`={v}" for k, v in list(cfg.items())[:8])
            embed["fields"].append({"name": f"{self._em('config')}Config", "value": cfg_text[:1024], "inline": False})

        if count > 1:
            embed["fields"].append({"name": "Window", "value": f"×{count} in last {self.dedup_window // 60}m", "inline": True})
        if hourly_count > 1:
            embed["fields"].append({"name": "Hourly", "value": f"×{hourly_count}/hr", "inline": True})

        if commands and len(commands) > 1:
            embed["fields"].append({"name": "Commands hit", "value": "`" + "` · `".join(sorted(commands))[:500] + "`", "inline": False})

        cmd = context.get("command")
        if self.show_stats and cmd:
            stats = self._command_stats_for(cmd)
            if stats:
                embed["fields"].append({"name": f"{self._em('stats')}Command stats", "value": f"total `{stats['total']}` · last hour `{stats['hourly']}`", "inline": True})

        if self.show_suggestions:
            suggestion = self._suggest_fix(exc)
            if suggestion:
                embed["fields"].append({"name": f"{self._em('fix')}Suggested fix", "value": suggestion[:1024], "inline": False})

        crumbs = self._breadcrumbs.snapshot(self._breadcrumb_show)
        if crumbs:
            crumb_lines = []
            for c in crumbs:
                t = datetime.fromtimestamp(c["t"], timezone.utc).strftime("%H:%M:%S")
                data_str = "  ".join(f"{k}={v}" for k, v in c["data"].items())[:160]
                crumb_lines.append(f"`{t}` · {c['event']}" + (f" · {data_str}" if data_str else ""))
            embed["fields"].append({"name": f"{self._em('bread')}Breadcrumbs", "value": "\n".join(crumb_lines)[:1024], "inline": False})

        label_map = {
            "kind": "Kind", "command": "Command", "guild": "Guild",
            "user": "User", "channel": "Channel", "message": "Message",
            "event": "Event", "script": "Script", "function": "Function", "module": "Module",
        }
        for key, label in label_map.items():
            if key in context:
                if len(embed["fields"]) >= 25: break
                embed["fields"].append({"name": label, "value": f"`{str(context[key])[:500]}`", "inline": True})

        files = []
        if len(filtered_text) > 1500:
            files.append({"name": f"{type(exc).__name__}-filtered.txt", "content": filtered_text.encode()})
        if len(tb_full) > 1500:
            files.append({"name": f"{type(exc).__name__}-raw.txt", "content": tb_full.encode()})

        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            form = aiohttp.FormData()
            form.add_field("payload_json", json.dumps({"embeds": [embed]}))
            for i, f in enumerate(files):
                form.add_field(f"files[{i}]", f["content"], filename=f["name"], content_type="text/plain")

            async with self._session.post(self.webhook_url, data=form, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 429:
                    try: data = await r.json()
                    except Exception: data = {}
                    wait = float(data.get("retry_after", 2))
                    await asyncio.sleep(wait + 1)
                    await self._post_webhook(exc, tb_full, context, fp, count, severity, hourly_count, extra_note, is_new, commands)
                elif r.status not in (200, 204):
                    text = await r.text()
                    print(f"[ERRWATCH] webhook HTTP {r.status}: {text[:200]}")
        except Exception as e:
            print(f"[ERRWATCH] webhook send failed: {type(e).__name__}: {e}")

    async def _post_collector(self, exc, tb_full, context, severity, count, fp, hourly_count, is_new):
        if not self.collector_url: return
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            headers = {"Content-Type": "application/json"}
            if self.collector_secret:
                headers["X-Errwatch-Token"] = self.collector_secret
            payload = {
                "bot_name": self.bot_name, "severity": severity, "error_type": type(exc).__name__,
                "error_message": str(exc)[:1000], "traceback": tb_full[:8000],
                "context": {**context, "_env": self._env_snapshot(), "_config": self._get_config_snapshot()},
                "fingerprint": fp, "count": count, "hourly_count": hourly_count, "is_new": is_new,
                "breadcrumbs": self._breadcrumbs.snapshot(self._breadcrumb_show),
            }
            async with self._session.post(f"{self.collector_url}/report", headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status not in (200, 201):
                    text = await r.text()
                    print(f"[ERRWATCH] collector HTTP {r.status}: {text[:200]}")
        except Exception as e:
            print(f"[ERRWATCH] collector send failed: {type(e).__name__}: {e}")

    async def _dm_owner(self, exc, tb_full, context):
        if not _DISCORD_OK or not self.owner_id or not self._bot: return
        try:
            import discord
            owner = self._bot.get_user(self.owner_id)
            if owner is None:
                owner = await self._bot.fetch_user(self.owner_id)
            if owner is None: return
            filtered_text = "\n".join(_filter_traceback(exc, self.project_dirs))
            embed = discord.Embed(
                title=f"{self._em('critical')}CRITICAL: {type(exc).__name__}"[:256],
                description=f"```python\n{filtered_text[-1800:]}\n```"[:4000],
                color=0xFF0000,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Environment", value=f"`{self._env_summary()}`"[:1024], inline=False)
            for k, v in list(context.items())[:8]:
                embed.add_field(name=str(k)[:256], value=str(v)[:1000], inline=True)
            await owner.send(embed=embed)
        except Exception as e:
            print(f"[ERRWATCH] DM owner failed: {type(e).__name__}: {e}")

    def _write_log(self, exc, tb_full, context, fp, hourly_count):
        if not self.log_file: return
        try:
            if self.log_max_bytes > 0 and os.path.exists(self.log_file):
                if os.path.getsize(self.log_file) > self.log_max_bytes:
                    self._rotate_log()
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(f"\n{'=' * 70}\n")
                f.write(f"[{datetime.now(timezone.utc).isoformat()}] {type(exc).__name__}: {exc}\n")
                f.write(f"  fp: {fp}  hourly: {hourly_count}  env: {self._env_summary()}\n")
                for k, v in context.items():
                    f.write(f"  {k}: {v}\n")
                f.write(tb_full)
        except Exception:
            pass

    def _rotate_log(self):
        try:
            for i in range(self.log_backup_count - 1, 0, -1):
                src = f"{self.log_file}.{i}"
                dst = f"{self.log_file}.{i + 1}"
                if os.path.exists(src): os.replace(src, dst)
            os.replace(self.log_file, f"{self.log_file}.1")
        except Exception:
            pass

    async def shutdown(self, timeout: float = 2.0):
        try:
            await asyncio.sleep(min(timeout, 1.0))
        except Exception:
            pass
        await self.close()

    async def close(self):
        if self._session and not self._session.closed:
            try: await self._session.close()
            except Exception: pass
            self._session = None
