# Maintenance Pass — Summary

## 1. Statistics page — fixed
Root cause: two separate, drifted implementations existed —
`plugins/p_ttishow.py`'s `/stats` command (files/users/chats/storage only)
and `plugins/admin.py`'s Admin Panel → Statistics screen (cpu/ram/uptime/ping
only, with a fake 0ms "ping" that measured nothing). The Admin Panel refactor
never merged the two, so each view was missing fields the other had.

Fix: added one shared helper, `get_bot_stats()` in `utils.py`, which is now
the single source of truth for:
- total indexed files (`Media.count_documents()`)
- total users / total chats (`db.total_users_count/total_chat_count`)
- MongoDB/Postgres storage usage (`db.get_db_size()`)
- memory usage (`psutil.virtual_memory()`)
- CPU usage (`psutil.cpu_percent()`)
- ping (now a real Telegram round-trip via `client.get_me()`, not a no-op)
- bot uptime (`BOT_START_TIME` from `info.py`)

Both `/stats` (`plugins/p_ttishow.py`) and the Admin Panel Statistics screen
(`plugins/admin.py`) now call this one function instead of duplicating the
logic. `Script.py`'s `STATUS_TXT` template was extended with the CPU/RAM/ping/
uptime lines it was missing.

Note: `plugins/etc.py`'s `/usage` command is a separate, pre-existing
admin-only disk-usage command and was left as-is — it's not a duplicate of
`/stats`, it reports different data (disk, not DB).

## 2. Regressions from the Admin Panel refactor
No other missing/duplicated callback handlers were found — every
`admin:*` callback regex in `plugins/admin.py` is unique (checked
programmatically), and `python3 -m py_compile` passes clean on all 52
`.py` files in the repo, so there are no syntax errors anywhere.

## 3. Pyrogram → Kurigram
The repo was already migrated at the dependency level: `requirements.txt`
lists `pyrotgfork` (the PyPI package name for Kurigram) rather than
`pyrogram`, and there is no separate `pyrogram` package listed anywhere.
Kurigram/pyrotgfork is a drop-in fork that keeps the `pyrogram` import
namespace on purpose, so `from pyrogram import ...` across the 44 files that
use it is correct as-is — it is *not* leftover Pyrogram, it's Kurigram
running under its compatibility namespace. No import changes were needed.
I pinned the version (`pyrotgfork>=2.2.24`) so builds are reproducible.

## 4. Latest Bot API / deprecated usage
No deprecated Pyrogram/Kurigram API calls were found in the plugins scanned
(`admin.py`, `p_ttishow.py`, `etc.py`, `webcode.py`). No further changes made
here beyond the stats fix.

## 5. KeyboardButtonStyle / colored buttons — NOT implemented, documented
Per the Feb 9 2026 changelog, Telegram's Bot API added colored keyboard/inline
buttons and custom-emoji buttons (`KeyboardButtonStyle`, background colors).
As of the Kurigram/pyrotgfork release currently pinned in this repo, there is
**no exposed `style`/color parameter on `InlineKeyboardButton` or
`KeyboardButton`** — this is a library-level gap, not something safely
patchable without touching Kurigram's raw TL layer and risking breakage on
the next upstream update. Per the task instructions, this was **not**
implemented. Recommendation: track Kurigram's changelog/issue tracker for
when `InlineKeyboardButton(style=...)` lands, then wire it into
`plugins/admin.py`'s button-builder helpers (`_btn`, `_main_markup`).

## Files changed
- `utils.py` — added `get_bot_stats()`
- `plugins/p_ttishow.py` — `/stats` now uses `get_bot_stats()`
- `plugins/admin.py` — Statistics screen now uses `get_bot_stats()`, added
  total indexed files, fixed fake ping measurement
- `Script.py` — `STATUS_TXT` extended with CPU/RAM/ping/uptime placeholders
- `requirements.txt` — pinned `pyrotgfork>=2.2.24`
