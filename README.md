# errwatch

> Drop-in self-debugging error reporter for Discord bots and Python scripts.

Catches exceptions, formats full tracebacks, sends them to a Discord webhook or a collector endpoint. Dedupes repeats, rate-limits floods, logs locally, never crashes on its own failure.

## Install

### In a Discord bot project

```bash
pip install "git+https://github.com/shadow99-web/errwatch.git@v1.0.0"
```

Or in `requirements.txt`:

```
git+https://github.com/shadow99-web/errwatch.git@v1.0.0
```

### With Discord support (attach() and DM delivery)

```bash
pip install "errwatch[discord] @ git+https://github.com/shadow99-web/errwatch.git@v1.0.0"
```

Or if you already have `discord.py` / `discord.py-self` installed:

```bash
pip install "git+https://github.com/shadow99-web/errwatch.git@v1.0.0"
```

`discord` is auto-detected at runtime — `attach()` requires it, but `report()` works in any script.

## Setup

### 1. Create a Discord webhook

- In your private dev server, create a channel like `#bot-errors`
- Right-click channel → **Edit Channel** → **Integrations** → **Webhooks** → **New Webhook**
- Copy the URL

### 2. Set environment variables

```env
ERRWATCH_WEBHOOK=https://discord.com/api/webhooks/xxx/yyy
ERRWATCH_OWNER_ID=123456789012345678
```

## Usage

### Discord bot (discord.py or discord.py-self)

```python
import os
from errwatch import ErrorWatcher

watcher = ErrorWatcher(
    webhook_url=os.environ["ERRWATCH_WEBHOOK"],
    owner_id=int(os.environ.get("ERRWATCH_OWNER_ID", "0")),
    bot_name="starboard",
)

# After bot = commands.Bot(...)
watcher.attach(bot)
```

This auto-hooks `on_command_error` and `on_error`. Every uncaught exception in a command or event sends a formatted report to `#bot-errors`.

### Task / function decorator

```python
from discord.ext import tasks

@watcher.catch
@tasks.loop(minutes=5)
async def cycle_status():
    await bot.change_presence(...)
```

### Plain script

```python
import asyncio
from errwatch import ErrorWatcher

watcher = ErrorWatcher(
    webhook_url=os.environ["ERRWATCH_WEBHOOK"],
    bot_name="spammer",
)

async def main():
    ...

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        asyncio.run(watcher.report(e, context={"script": "spam.py"}))
        raise
```

### Manual reporting from a try/except

```python
try:
    await risky_operation()
except Exception as e:
    await watcher.report(e, context={"user_id": ctx.author.id})
    raise
```

### Critical errors (auto-DM owner)

```python
from errwatch import CriticalError

raise CriticalError("bot token invalid — needs restart")
```

`CriticalError` sends to the webhook **and** DMs the `owner_id`.

## What you get in Discord

```
🐛 ValueError
[full traceback]

Kind:    command
Command: errtest
Guild:   My Server (123...)
User:    you (456...)
Channel: #general
Message: !errtest
```

## Features

| Feature | Details |
|---|---|
| **Auto-catch** | `.attach(bot)` hooks `on_command_error` + `on_error` |
| **Manual catch** | `await watcher.report(exc, context={...})` |
| **Decorator** | `@watcher.catch` wraps async functions / `tasks.loop` |
| **Dedup** | Same fingerprint within 5 min collapses to a count |
| **Rate limit** | 10 webhook sends/hour max, then aggregates |
| **Local log** | Every error to `errwatch.log` regardless of webhook |
| **Collector mode** | Optional: POST to `/report` for queryable storage |
| **Self-safe** | Every internal op wrapped — never crashes the bot |
| **CriticalError** | Subclass forces owner DM alongside webhook |

## Configuration

```python
ErrorWatcher(
    webhook_url="...",         # Discord webhook URL
    collector_url=None,        # Optional: POST endpoint (see below)
    collector_secret=None,     # Optional: shared secret for collector
    owner_id=0,                # Discord user ID for CriticalError DMs
    log_file="errwatch.log",   # Local log path
    dedup_window=300,          # Seconds to collapse duplicate errors
    rate_limit_per_hour=10,    # Max webhook sends/hour
    bot_name="bot",            # Label in Discord embeds
)
```

### Optional — collector mode

If you run a collector service (see `errdash.py` in a separate repo), errwatch can POST errors there for queryable storage **in addition** to the webhook:

```python
watcher = ErrorWatcher(
    webhook_url=os.environ["ERRWATCH_WEBHOOK"],              # instant notification
    collector_url="https://your-collector.onrender.com",     # queryable storage
    collector_secret=os.environ["ERRWATCH_SECRET"],          # shared secret
    bot_name="starboard",
)
```

If collector is down → webhook still fires. If webhook isn't set → collector still stores. **Either works alone.**

## Environment variables

| Var | Required | Purpose |
|---|---|---|
| `ERRWATCH_WEBHOOK` | Recommended | Discord webhook URL |
| `ERRWATCH_OWNER_ID` | Optional | User ID for CriticalError DMs |
| `ERRWATCH_SECRET` | Optional | Shared secret for collector mode |

## Security

This package contains **no secrets**. All sensitive data comes from environment variables. The `.env` file is gitignored — only `.env.example` is committed.

If you find a security issue, please open an issue (or DM the maintainer for sensitive matters).

## License

MIT — see [LICENSE](LICENSE).

## What's new in v1.1.0

- **Environment snapshot** — every report includes Python version, OS, discord.py version, uptime, memory
- **Smart dedup with auto-mute** — errors firing >100x/hr auto-mute for 24h (configurable)
- **Exception filters** — `ignore=["discord.errors.NotFound"]` skips expected errors entirely
- **Pattern filters** — `ignore_patterns=["*unknown message*"]` for message-matching
- **Non-async excepthook** — `watcher.install_excepthook()` catches sync crashes
- **Sync reporting** — `watcher.report_sync(exc)` from non-async code
- **Shutdown flush** — `await watcher.shutdown()` waits for in-flight webhook posts
- **Expanded severity** — `"info"` / `"warning"` / `"error"` / `"critical"` with distinct emoji + colors
- **Suppress context manager** — `async with watcher.suppress(discord.NotFound):`

### New usage examples

```python
from errwatch import ErrorWatcher

watcher = ErrorWatcher(
    webhook_url="...",
    bot_name="starboard",
    ignore=[
        "discord.errors.Forbidden",
        "discord.errors.NotFound",
        "asyncio.CancelledError",
    ],
    ignore_patterns=["*unknown message*"],
    mute_after_hourly=100,
    mute_duration_seconds=86400,
)
watcher.attach(bot)
watcher.install_excepthook()
```

Suppress expected failures in-place:

```python
async with watcher.suppress(discord.NotFound, report=True):
    await msg.edit(embed=embed)
```

Report from sync code:

```python
try:
    config = load_config("config.json")
except FileNotFoundError as e:
    watcher.report_sync(e, context={"script": "startup"})
    raise
```

Flush on shutdown:

```python
async def _async_main():
    try:
        await bot.start(TOKEN)
    finally:
        await watcher.shutdown()
```

## What's new in v1.2.0

All features from v1.1.0 plus:

- **Smart traceback filtering** — `site-packages/` frames collapsed to `[... N library frames hidden ...]`, your code highlighted
- **Breadcrumbs** — `watcher.breadcrumb("event", key=val)` records events leading up to the error
- **Config snapshot** — `config_provider=lambda: {...}` includes current config in every report
- **Regression detection** — `[NEW]` tag on first occurrence of each fingerprint
- **Command coalescing** — reports show all commands that hit the same error
- **Per-command stats** — total + hourly count per command
- **Recovery suggestions** — common error types map to suggested fixes
- **Sampling** — `sample_rate=0.1` reports 1 in 10 (noisy bot protection)
- **Test mode** — `test_mode=True` logs only, no webhook (for local dev)

### Full config example

```python
from errwatch import ErrorWatcher

watcher = ErrorWatcher(
    webhook_url=os.environ["ERRWATCH_WEBHOOK"],
    owner_id=int(os.environ.get("ERRWATCH_OWNER_ID", "0")),
    bot_name="imgban",
    project_dirs=["/opt/render/project/src"],       # hide libs, highlight your code
    config_provider=lambda: {                        # included in every report
        "prefix":  PREFIX,
        "db_path": DB_PATH,
        "devs":    len(DEV_IDS),
        "hf":      HF_TOKEN is not None,
    },
    ignore=[
        "discord.errors.Forbidden",
        "discord.errors.NotFound",
        "asyncio.CancelledError",
    ],
    ignore_patterns=["*unknown message*", "*50035*"],
    mute_after_hourly=100,
    breadcrumb_max=30,
    show_suggestions=True,
    show_env=True,
    show_stats=True,
)
watcher.attach(bot)
watcher.install_excepthook()
```

### Breadcrumbs in action

```python
@bot.command(name="devfq")
async def devfq(ctx, action: str, user: discord.User):
    watcher.breadcrumb("command_start", command="devfq", action=action, user=user.id)
    try:
        watcher.breadcrumb("db_call", method="add_dev_friend", user=user.id)
        await bot.db.add_dev_friend(user.id, ctx.author.id)
        watcher.breadcrumb("success", command="devfq", user=user.id)
    except Exception:
        # error report will show all breadcrumbs leading up to this
        raise
```

### Test mode for local dev

```python
watcher = ErrorWatcher(
    webhook_url="",              # don't spam Discord
    test_mode=True,              # log only
    bot_name="imgban",
)
```

Every "reported" error appears in `errwatch.log` and prints to console — but doesn't hit Discord. Flip `test_mode=False` for production.

### Sampling for noisy bots

```python
watcher = ErrorWatcher(
    webhook_url="...",
    sample_rate=0.1,             # report 1 in 10 errors
)
```

Useful when a single bug floods your channel. Set `sample_rate=1.0` (default) to disable.
