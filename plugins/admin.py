# plugins/admin.py — Owner Admin Panel (/admin)
# Full 12-category control panel. Triggered only by /admin in private chat.
# NOTE: /settings is a separate, per-group filter panel (plugins/commands.py)
# used by group admins — intentionally untouched, no overlap with this file.

import asyncio
import os
import sys
import time
import logging

import psutil
from pyrogram import Client, filters
from pyrogram import ContinuePropagation
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery, BotCommand,
)

from info import (
    ADMINS, AUTH_USERS, LOG_CHANNEL, SUPPORT_CHAT, BOT_START_TIME,
    PROTECT_CONTENT, MELCOW_NEW_USERS, CACHE_TIME, PICS, SINGLE_BUTTON,
    CUSTOM_FILE_CAPTION, IMDB_TEMPLATE, SPELL_CHECK_REPLY, USE_CAPTION_FILTER,
    IMDB, P_TTI_SHOW_OFF, HYPER_MODE, PUBLIC_FILE_STORE,
    POSTGRES_STORAGE_LIMIT_BYTES,
)
from database.users_chats_db import db
from database.ia_filterdb import Media
from utils import temp, humanbytes, get_bot_stats, styled_button
import plugins.new_updates as nu
from plugins.commands import build_fsub_details_text

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Runtime toggles (in-memory, reset on restart)
# ─────────────────────────────────────────────────────────────────────────────
_rt = {
    "maintenance": False,
    "welcome_messages": MELCOW_NEW_USERS,
    "inline_mode": True,
    "auto_delete": False,
    "protect_content": PROTECT_CONTENT,
    "debug_mode": False,
}

# Pending FSM state for text-input flows: {user_id: {"state": str}}
_fsm: dict[int, dict] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def is_admin(user) -> bool:
    return user and (user.id in ADMINS or (f"@{user.username}" in ADMINS if user.username else False))


def _btn(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[styled_button(t, callback_data=d) for t, d in row] for row in rows])


def _toggle_label(key: str) -> str:
    return "✅ ON" if _rt.get(key) else "❌ OFF"


# ─────────────────────────────────────────────────────────────────────────────
# Main menu
# ─────────────────────────────────────────────────────────────────────────────

MAIN_MENU_TEXT = (
    "⚙️ <b>Owner Settings</b>\n\n"
    "Welcome to the full control panel.\n"
    "Choose a category below:"
)


def _main_markup() -> InlineKeyboardMarkup:
    return _btn(
        [("👤 User Management", "admin:users"), ("💬 Messages", "admin:messages")],
        [("🎨 Appearance", "admin:appearance"), ("🔍 Search", "admin:search")],
        [("⚙️ Behaviour", "admin:behaviour"), ("📊 Statistics", "admin:statistics")],
        [("📢 Broadcast", "admin:broadcast"), ("📝 Text Manager", "admin:textmgr")],
        [("🗄 Database", "admin:database"), ("🛠 Maintenance", "admin:maintenance")],
        [("📜 Logs", "admin:logs"), ("⚡ Advanced", "admin:advanced")],
    )


@Client.on_message(filters.command("admin") & filters.private)
async def admin_panel(client: Client, message: Message):
    if not is_admin(message.from_user):
        return await message.reply("🚫 You are not authorized.")
    await message.reply(MAIN_MENU_TEXT, reply_markup=_main_markup())


@Client.on_callback_query(filters.regex(r"^admin:main$"))
async def admin_main(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.message.edit_text(MAIN_MENU_TEXT, reply_markup=_main_markup())


# legacy alias — old "Back" buttons used admin:back
@Client.on_callback_query(filters.regex(r"^admin:back$"))
async def admin_back(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.message.edit_text(MAIN_MENU_TEXT, reply_markup=_main_markup())


@Client.on_callback_query(filters.regex(r"^noop$"))
async def noop_cb(client: Client, query: CallbackQuery):
    await query.answer()


# ═════════════════════════════════════════════════════════════════════════════
# 👤  USER MANAGEMENT
# ═════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r"^admin:users$"))
async def admin_users_menu(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    markup = _btn(
        [("👥 Admins", "admin:users:admins"), ("✅ Authorized Users", "admin:users:authusers")],
        [("🚫 Banned Users", "admin:users:banned"), ("🔇 Disabled Chats", "admin:users:disabledchats")],
        [("🔐 Permissions", "admin:users:perms")],
        [("« Back", "admin:main")],
    )
    await query.message.edit_text("👤 <b>User Management</b>\nChoose an option:", reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^admin:users:admins$"))
async def admin_users_admins(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    lines = ["<b>👑 Current Admins</b>\n"]
    for a in ADMINS:
        if isinstance(a, int):
            try:
                u = await client.get_users(a)
                lines.append(f"• {u.first_name} — <code>{a}</code>")
            except Exception:
                lines.append(f"• <code>{a}</code>")
        else:
            lines.append(f"• {a}")
    await query.message.edit_text("\n".join(lines), reply_markup=_btn([("« Back", "admin:users")]))


@Client.on_callback_query(filters.regex(r"^admin:users:authusers$"))
async def admin_users_authusers(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    lines = ["<b>✅ Authorized Users</b>\n"]
    if AUTH_USERS:
        for uid in AUTH_USERS:
            try:
                u = await client.get_users(uid)
                lines.append(f"• {u.first_name} — <code>{uid}</code>")
            except Exception:
                lines.append(f"• <code>{uid}</code>")
    else:
        lines.append("No extra auth users set.")
    lines.append("\n<i>Edit AUTH_USERS env var to change.</i>")
    await query.message.edit_text("\n".join(lines), reply_markup=_btn([("« Back", "admin:users")]))


@Client.on_callback_query(filters.regex(r"^admin:users:banned$"))
async def admin_users_banned(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    banned_users, _ = await db.get_banned()
    if not banned_users:
        text = "🚫 <b>Banned Users</b>\n\nNo banned users."
    else:
        lines = [f"🚫 <b>Banned Users</b> ({len(banned_users)} total)\n"]
        for uid in banned_users[:20]:
            try:
                u = await client.get_users(uid)
                lines.append(f"• {u.first_name} — <code>{uid}</code>")
            except Exception:
                lines.append(f"• <code>{uid}</code>")
        if len(banned_users) > 20:
            lines.append(f"\n…and {len(banned_users) - 20} more.")
        text = "\n".join(lines)
    markup = _btn(
        [("➕ Ban User", "admin:users:banuser"), ("➖ Unban User", "admin:users:unbanuser")],
        [("« Back", "admin:users")],
    )
    await query.message.edit_text(text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^admin:users:banuser$"))
async def admin_users_banuser_prompt(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    _fsm[query.from_user.id] = {"state": "await_ban_id"}
    await query.message.edit_text(
        "🚫 <b>Ban a User</b>\n\nSend the <b>user ID</b> to ban:",
        reply_markup=_btn([("« Cancel", "admin:users:banned")]),
    )


@Client.on_callback_query(filters.regex(r"^admin:users:unbanuser$"))
async def admin_users_unbanuser_prompt(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    _fsm[query.from_user.id] = {"state": "await_unban_id"}
    await query.message.edit_text(
        "✅ <b>Unban a User</b>\n\nSend the <b>user ID</b> to unban:",
        reply_markup=_btn([("« Cancel", "admin:users:banned")]),
    )


@Client.on_callback_query(filters.regex(r"^admin:users:disabledchats$"))
async def admin_users_disabledchats(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    _, banned_chats = await db.get_banned()
    if not banned_chats:
        text = "🔇 <b>Disabled Chats</b>\n\nNo disabled chats."
    else:
        lines = [f"🔇 <b>Disabled Chats</b> ({len(banned_chats)} total)\n"]
        for cid in banned_chats[:20]:
            try:
                c = await client.get_chat(cid)
                lines.append(f"• {c.title} — <code>{cid}</code>")
            except Exception:
                lines.append(f"• <code>{cid}</code>")
        text = "\n".join(lines)
    markup = _btn(
        [("🔇 Disable Chat", "admin:users:disablechat"), ("✅ Enable Chat", "admin:users:enablechat")],
        [("« Back", "admin:users")],
    )
    await query.message.edit_text(text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^admin:users:disablechat$"))
async def admin_users_disablechat_prompt(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    _fsm[query.from_user.id] = {"state": "await_disable_chat_id"}
    await query.message.edit_text(
        "🔇 <b>Disable a Chat</b>\n\nSend the <b>chat ID</b> to disable:",
        reply_markup=_btn([("« Cancel", "admin:users:disabledchats")]),
    )


@Client.on_callback_query(filters.regex(r"^admin:users:enablechat$"))
async def admin_users_enablechat_prompt(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    _fsm[query.from_user.id] = {"state": "await_enable_chat_id"}
    await query.message.edit_text(
        "✅ <b>Enable a Chat</b>\n\nSend the <b>chat ID</b> to re-enable:",
        reply_markup=_btn([("« Cancel", "admin:users:disabledchats")]),
    )


@Client.on_callback_query(filters.regex(r"^admin:users:perms$"))
async def admin_users_perms(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    auth_chs = await db.get_auth_channels()
    lines = ["<b>🔐 Permissions</b>\n", "<b>Auth Channels (Force-Sub):</b>"]
    if auth_chs:
        for cid in auth_chs:
            try:
                c = await client.get_chat(cid)
                lines.append(f"• {c.title} — <code>{cid}</code>")
            except Exception:
                lines.append(f"• <code>{cid}</code>")
    else:
        lines.append("None set.")
    markup = _btn(
        [("✏️ Set Auth Channels", "admin:users:setauthch")],
        [("« Back", "admin:users")],
    )
    await query.message.edit_text("\n".join(lines), reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^admin:users:setauthch$"))
async def admin_users_setauthch_prompt(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    _fsm[query.from_user.id] = {"state": "await_auth_channels"}
    await query.message.edit_text(
        "🔐 <b>Set Auth Channels</b>\n\n"
        "Send channel IDs separated by spaces:\n"
        "<code>-100123 -100456</code>",
        reply_markup=_btn([("« Cancel", "admin:users:perms")]),
    )


# ═════════════════════════════════════════════════════════════════════════════
# 💬  MESSAGES
# ═════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r"^admin:messages$"))
async def admin_messages_menu(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    markup = _btn(
        [("🚀 Start", "admin:msg:start"), ("❓ Help", "admin:msg:help")],
        [("ℹ️ About", "admin:msg:about"), ("👋 Welcome", "admin:msg:welcome")],
        [("❌ Error", "admin:msg:error"), ("✅ Success", "admin:msg:success")],
        [("🔄 Restart", "admin:msg:restart"), ("📊 Status", "admin:msg:status")],
        [("« Back", "admin:main")],
    )
    await query.message.edit_text("💬 <b>Messages</b>\nView current message templates:", reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^admin:msg:(start|help|about|welcome|error|success|restart|status)$"))
async def admin_msg_view(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    key = query.matches[0].group(1)
    from Script import script
    mapping = {
        "start": ("Start", str(script.START_TXT)),
        "help": ("Help", "\n".join(script.HELP_PAGES) if hasattr(script, "HELP_PAGES") else str(script.HELP_TXT)),
        "about": ("About", str(getattr(script, "ABOUT_TXT", "Not set."))),
        "welcome": ("Welcome", str(getattr(script, "MELCOW_TXT", "Not set."))),
        "error": ("Error", str(getattr(script, "ERROR_TXT", "Not set."))),
        "success": ("Success", str(getattr(script, "SUCCESS_TXT", "Not set."))),
        "restart": ("Restart", str(getattr(script, "RESTART_TXT", "Not set."))),
        "status": ("Status", str(getattr(script, "RESTART_GC_TXT", "Not set."))),
    }
    label, text = mapping[key]
    await query.message.edit_text(
        f"<b>💬 {label} Message</b>\n\n<code>{text[:800]}</code>",
        reply_markup=_btn([("« Back", "admin:messages")]),
    )


# ═════════════════════════════════════════════════════════════════════════════
# 🎨  APPEARANCE
# ═════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r"^admin:appearance$"))
async def admin_appearance_menu(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    markup = _btn(
        [("🤖 Bot Name", "admin:app:name"), ("📝 Description", "admin:app:desc")],
        [("🖼 Start Photo", "admin:app:startphoto"), ("ℹ️ About Photo", "admin:app:aboutphoto")],
        [("❓ Help Photo", "admin:app:helpphoto"), ("🔘 Button Labels", "admin:app:btnlabels")],
        [("« Back", "admin:main")],
    )
    await query.message.edit_text("🎨 <b>Appearance</b>\nCustomize bot look:", reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^admin:app:name$"))
async def admin_app_name(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    me = await client.get_me()
    await query.message.edit_text(
        f"🤖 <b>Bot Name</b>\n\nCurrent: <b>{me.first_name}</b>\n\nTo change, use BotFather → /setname",
        reply_markup=_btn([("« Back", "admin:appearance")]),
    )


@Client.on_callback_query(filters.regex(r"^admin:app:desc$"))
async def admin_app_desc(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.message.edit_text(
        "📝 <b>Bot Description</b>\n\n"
        "Bot description/about text can only be changed via BotFather "
        "(no Bot API method exposes this to the bot itself).\n\n"
        "Open @BotFather → /setdescription or /setabouttext",
        reply_markup=_btn([("« Back", "admin:appearance")]),
    )


@Client.on_callback_query(filters.regex(r"^admin:app:(startphoto|aboutphoto|helpphoto)$"))
async def admin_app_photo(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    key = query.matches[0].group(1)
    label = {"startphoto": "Start", "aboutphoto": "About", "helpphoto": "Help"}[key]
    await query.message.edit_text(
        f"🖼 <b>{label} Photo</b>\n\n"
        f"Current photo pool has <b>{len(PICS)}</b> photo(s).\n\n"
        "<i>Edit PICS env var to change the photo URLs.</i>",
        reply_markup=_btn([("« Back", "admin:appearance")]),
    )


@Client.on_callback_query(filters.regex(r"^admin:app:btnlabels$"))
async def admin_app_btnlabels(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.message.edit_text(
        f"🔘 <b>Button Labels</b>\n\n"
        f"Single Button mode: <b>{'ON' if SINGLE_BUTTON else 'OFF'}</b>\n\n"
        "<i>Toggle via SINGLE_BUTTON env var.</i>",
        reply_markup=_btn([("« Back", "admin:appearance")]),
    )


# ═════════════════════════════════════════════════════════════════════════════
# 🔍  SEARCH
# ═════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r"^admin:search$"))
async def admin_search_menu(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    cfg = nu.get_runtime_update_config()
    markup = _btn(
        [("🔄 Cache Time", "admin:search:cache"), ("📦 Search Limit", "admin:search:limit")],
        [("⏱ Timeout", "admin:search:timeout"), ("⚙️ Behaviour", "admin:search:behaviour")],
        [("« Back", "admin:main")],
    )
    text = (
        "🔍 <b>Search Settings</b>\n\n"
        f"Cache Time: <code>{CACHE_TIME}s</code>\n"
        f"Page Size: <code>{cfg['PAGE_SIZE']}</code>\n"
        f"GetDLink Size: <code>{cfg['GETDLINK_PAGE_SIZE']}</code>"
    )
    await query.message.edit_text(text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^admin:search:cache$"))
async def admin_search_cache(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.message.edit_text(
        f"⏱ <b>Cache Time</b>\n\nCurrent: <code>{CACHE_TIME} seconds</code>\n\n"
        "<i>Edit CACHE_TIME env var to change.</i>",
        reply_markup=_btn([("« Back", "admin:search")]),
    )


def _search_limit_markup() -> InlineKeyboardMarkup:
    cfg = nu.get_runtime_update_config()
    return _btn(
        [("➖", "admin:search:psize:-1"), (f"Page: {cfg['PAGE_SIZE']}", "noop"), ("➕", "admin:search:psize:1")],
        [("➖", "admin:search:dlsize:-1"), (f"DLink: {cfg['GETDLINK_PAGE_SIZE']}", "noop"), ("➕", "admin:search:dlsize:1")],
        [("« Back", "admin:search")],
    )


@Client.on_callback_query(filters.regex(r"^admin:search:limit$"))
async def admin_search_limit(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.message.edit_text("📦 <b>Search Limits</b>\nAdjust page sizes:", reply_markup=_search_limit_markup())


@Client.on_callback_query(filters.regex(r"^admin:search:(psize|dlsize):(-?1)$"))
async def admin_search_size_adjust(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    key, delta = query.matches[0].group(1), int(query.matches[0].group(2))
    cfg = nu.get_runtime_update_config()
    if key == "psize":
        nu.set_runtime_update_config("PAGE_SIZE", cfg["PAGE_SIZE"] + delta)
    elif key == "dlsize":
        nu.set_runtime_update_config("GETDLINK_PAGE_SIZE", cfg["GETDLINK_PAGE_SIZE"] + delta)
    await query.answer("Updated ✅")
    await query.message.edit_reply_markup(_search_limit_markup())


@Client.on_callback_query(filters.regex(r"^admin:search:timeout$"))
async def admin_search_timeout(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.message.edit_text(
        "⏱ <b>Search Timeout</b>\n\nNo runtime timeout variable exposed yet.\nSet it via environment config.",
        reply_markup=_btn([("« Back", "admin:search")]),
    )


@Client.on_callback_query(filters.regex(r"^admin:search:behaviour$"))
async def admin_search_behaviour(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.message.edit_text(
        "⚙️ <b>Search Behaviour</b>\n\n"
        f"Spell Check: <b>{'ON' if SPELL_CHECK_REPLY else 'OFF'}</b>\n"
        f"Caption Filter: <b>{'ON' if USE_CAPTION_FILTER else 'OFF'}</b>\n\n"
        "<i>Toggle via SPELL_CHECK_REPLY / USE_CAPTION_FILTER env vars.</i>",
        reply_markup=_btn([("« Back", "admin:search")]),
    )


# ═════════════════════════════════════════════════════════════════════════════
# ⚙️  BEHAVIOUR
# ═════════════════════════════════════════════════════════════════════════════

def _behaviour_markup() -> InlineKeyboardMarkup:
    def tl(k): return _toggle_label(k)
    return _btn(
        [(f"🔧 Maintenance  {tl('maintenance')}", "admin:beh:maintenance")],
        [(f"👋 Welcome Msgs  {tl('welcome_messages')}", "admin:beh:welcome_messages")],
        [(f"🔁 Inline Mode  {tl('inline_mode')}", "admin:beh:inline_mode")],
        [(f"🗑 Auto Delete  {tl('auto_delete')}", "admin:beh:auto_delete")],
        [(f"🔒 Protect Content  {tl('protect_content')}", "admin:beh:protect_content")],
        [(f"🐛 Debug Mode  {tl('debug_mode')}", "admin:beh:debug_mode")],
        [("« Back", "admin:main")],
    )


@Client.on_callback_query(filters.regex(r"^admin:behaviour$"))
async def admin_behaviour_menu(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.message.edit_text("⚙️ <b>Behaviour</b>\nToggle runtime options:", reply_markup=_behaviour_markup())


@Client.on_callback_query(filters.regex(r"^admin:beh:(maintenance|welcome_messages|inline_mode|auto_delete|protect_content|debug_mode)$"))
async def admin_beh_toggle(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    key = query.matches[0].group(1)
    _rt[key] = not _rt[key]
    label = key.replace("_", " ").title()
    state = "ON ✅" if _rt[key] else "OFF ❌"
    await query.answer(f"{label} → {state}", show_alert=False)

    if key == "debug_mode":
        logging.getLogger().setLevel(logging.DEBUG if _rt[key] else logging.INFO)
    if key == "maintenance":
        temp.MAINTENANCE = _rt[key]

    await query.message.edit_reply_markup(_behaviour_markup())


# ═════════════════════════════════════════════════════════════════════════════
# 📊  STATISTICS
# ═════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r"^admin:statistics$"))
async def admin_statistics(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.answer("Loading…")

    stats = await get_bot_stats(client=client)
    ping_display = f"{stats['ping_ms']} ms" if stats["ping_ms"] is not None else "N/A"

    text = (
        "📊 <b>Statistics</b>\n\n"
        f"🗂 Total Indexed Files: <code>{stats['total_files']}</code>\n"
        f"👤 Total Users: <code>{stats['total_users']}</code>\n"
        f"💬 Total Chats: <code>{stats['total_chats']}</code>\n"
        f"🗄 Database: <b>{stats['db_type']}</b> — <code>{stats['db_size_readable']}</code>\n"
        f"🧠 Memory: <code>{stats['ram_percent']}% used</code> "
        f"(<code>{stats['ram_used_readable']}</code> / <code>{stats['ram_total_readable']}</code>)\n"
        f"⚡ CPU: <code>{stats['cpu_percent']}%</code>\n"
        f"📶 Ping: <code>{ping_display}</code>\n"
        f"⏱ Uptime: <code>{stats['uptime_str']}</code>"
    )
    markup = _btn([("🔄 Refresh", "admin:statistics")], [("« Back", "admin:main")])
    await query.message.edit_text(text, reply_markup=markup)


# ═════════════════════════════════════════════════════════════════════════════
# 📢  BROADCAST
# ═════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r"^admin:broadcast$"))
async def admin_broadcast_menu(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    markup = _btn(
        [("📤 Broadcast Users", "admin:bc:users"), ("📢 Broadcast Groups", "admin:bc:groups")],
        [("↪️ Forward Broadcast", "admin:bc:forward"), ("📋 Copy Broadcast", "admin:bc:copy")],
        [("📜 Broadcast History", "admin:bc:history")],
        [("« Back", "admin:main")],
    )
    await query.message.edit_text(
        "📢 <b>Broadcast</b>\n\nReply to a message with the buttons below to start a broadcast.",
        reply_markup=markup,
    )


@Client.on_callback_query(filters.regex(r"^admin:bc:(users|groups|forward|copy)$"))
async def admin_bc_start(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    mode = query.matches[0].group(1)
    labels = {
        "users": ("📤", "users", "/broadcast"),
        "groups": ("📢", "groups", "/grpbroadcast"),
        "forward": ("↪️", "users (forward)", "/broadcast"),
        "copy": ("📋", "users (copy)", "/broadcast"),
    }
    icon, target, cmd = labels[mode]
    await query.message.edit_text(
        f"{icon} <b>Broadcast to {target}</b>\n\n"
        f"Reply to the message you want to broadcast with:\n<code>{cmd}</code>",
        reply_markup=_btn([("« Back", "admin:broadcast")]),
    )


@Client.on_callback_query(filters.regex(r"^admin:bc:history$"))
async def admin_bc_history(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    try:
        import plugins.broadcast as bc_mod
        bc = bc_mod.BC
        if bc:
            stats = bc.get("stats", {})
            text = (
                "📜 <b>Last Broadcast Stats</b>\n\n"
                f"✅ Success: <code>{stats.get('success', 0)}</code>\n"
                f"❌ Failed: <code>{stats.get('failed', 0)}</code>\n"
                f"🚫 Blocked: <code>{stats.get('blocked', 0)}</code>\n"
                f"🗑 Deleted: <code>{stats.get('deleted', 0)}</code>"
            )
        else:
            text = "📜 <b>Broadcast History</b>\n\nNo broadcast has been run this session."
    except Exception:
        text = "📜 <b>Broadcast History</b>\n\nCould not load broadcast data."
    await query.message.edit_text(text, reply_markup=_btn([("« Back", "admin:broadcast")]))


# ═════════════════════════════════════════════════════════════════════════════
# 📝  TEXT MANAGER
# ═════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r"^admin:textmgr$"))
async def admin_textmgr_menu(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    markup = _btn(
        [("🚀 Start Text", "admin:txt:start"), ("❓ Help Text", "admin:txt:help")],
        [("ℹ️ About Text", "admin:txt:about"), ("👋 Welcome Text", "admin:txt:welcome")],
        [("❌ Error Text", "admin:txt:error"), ("📊 Status Text", "admin:txt:status")],
        [("📋 Custom Templates", "admin:txt:custom")],
        [("« Back", "admin:main")],
    )
    await query.message.edit_text("📝 <b>Text Manager</b>\nView & manage message templates:", reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^admin:txt:(start|help|about|welcome|error|status|custom)$"))
async def admin_txt_view(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    key = query.matches[0].group(1)
    from Script import script
    mapping = {
        "start": ("Start", str(script.START_TXT)),
        "help": ("Help", str(script.HELP_TXT)),
        "about": ("About", str(getattr(script, "ABOUT_TXT", "Not set."))),
        "welcome": ("Welcome", str(getattr(script, "MELCOW_TXT", "Not set."))),
        "error": ("Error", str(getattr(script, "ERROR_TXT", "Not set."))),
        "status": ("Status", str(getattr(script, "RESTART_TXT", "Not set."))),
        "custom": ("Custom Templates", f"File Caption:\n{CUSTOM_FILE_CAPTION}\n\nIMDB Template:\n{IMDB_TEMPLATE}"),
    }
    label, text = mapping[key]
    await query.message.edit_text(
        f"📝 <b>{label} Text</b>\n\n<code>{text[:1000]}</code>",
        reply_markup=_btn([("« Back", "admin:textmgr")]),
    )


# ═════════════════════════════════════════════════════════════════════════════
# 🗄  DATABASE
# ═════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r"^admin:database$"))
async def admin_database_menu(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    markup = _btn(
        [("📊 DB Status", "admin:db:status"), ("🗂 Collections", "admin:db:collections")],
        [("💾 Storage Usage", "admin:db:storage"), ("📤 Export Settings", "admin:db:export")],
        [("📥 Import Settings", "admin:db:import"), ("🔁 Backup", "admin:db:backup")],
        [("« Back", "admin:main")],
    )
    await query.message.edit_text("🗄 <b>Database</b>\nManage database options:", reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^admin:db:status$"))
async def admin_db_status(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.answer("Loading…")
    db_type = "MongoDB" if db.use_mongo else "PostgreSQL"
    size = await db.get_db_size()
    users = await db.total_users_count()
    chats = await db.total_chat_count()
    text = (
        "📊 <b>Database Status</b>\n\n"
        f"Type: <b>{db_type}</b>\n"
        f"Size: <code>{humanbytes(size)}</code>\n"
        f"Users: <code>{users}</code>\n"
        f"Chats: <code>{chats}</code>\n"
        f"Status: ✅ Connected"
    )
    await query.message.edit_text(text, reply_markup=_btn([("« Back", "admin:database")]))


@Client.on_callback_query(filters.regex(r"^admin:db:collections$"))
async def admin_db_collections(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    if db.use_mongo:
        collections = await db.db.list_collection_names()
        text = "🗂 <b>Collections</b>\n\n" + "\n".join(f"• <code>{c}</code>" for c in collections)
    else:
        text = "🗂 <b>Collections</b>\n\nUsing PostgreSQL — tables managed via SQLAlchemy."
    await query.message.edit_text(text, reply_markup=_btn([("« Back", "admin:database")]))


@Client.on_callback_query(filters.regex(r"^admin:db:storage$"))
async def admin_db_storage(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.answer("Loading…")
    size = await db.get_db_size()
    limit = POSTGRES_STORAGE_LIMIT_BYTES
    if limit:
        pct = round((size / limit) * 100, 1)
        bar = "█" * int(pct // 10) + "░" * (10 - int(pct // 10))
        text = (
            f"💾 <b>Storage Usage</b>\n\n"
            f"Used: <code>{humanbytes(size)}</code> / <code>{humanbytes(limit)}</code>\n"
            f"[{bar}] {pct}%"
        )
    else:
        text = f"💾 <b>Storage Usage</b>\n\nUsed: <code>{humanbytes(size)}</code>\nNo limit configured."
    await query.message.edit_text(text, reply_markup=_btn([("« Back", "admin:database")]))


@Client.on_callback_query(filters.regex(r"^admin:db:(export|import|backup)$"))
async def admin_db_export_import(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    action = query.matches[0].group(1)
    labels = {
        "export": "📤 Export Settings — use <code>mongoexport</code> or <code>pg_dump</code> manually.",
        "import": "📥 Import Settings — use <code>mongoimport</code> or <code>pg_restore</code> manually.",
        "backup": "🔁 Backup — schedule backups via your hosting provider's tools.",
    }
    await query.message.edit_text(
        f"🗄 <b>{action.title()}</b>\n\n{labels[action]}",
        reply_markup=_btn([("« Back", "admin:database")]),
    )


# ═════════════════════════════════════════════════════════════════════════════
# 🛠  MAINTENANCE
# ═════════════════════════════════════════════════════════════════════════════

_BOT_COMMANDS = [
    BotCommand("start", "Start the bot"),
    BotCommand("movies", "Latest added movies"),
    BotCommand("series", "Latest added series"),
    BotCommand("connect", "Connect group to PM"),
    BotCommand("disconnect", "Disconnect active chat"),
    BotCommand("connections", "Show your connections"),
    BotCommand("settings", "Open group settings"),
    BotCommand("filter", "Create manual filter"),
    BotCommand("filters", "List filters"),
    BotCommand("imdb", "Search movie/series info"),
    BotCommand("mnsearch", "Search movie/series info"),
    BotCommand("bug", "Send bug report / feedback"),
    BotCommand("search", "Search from external sources"),
    BotCommand("deletefiles", "Bulk delete indexed files"),
    BotCommand("stats", "Show database statistics"),
    BotCommand("ping", "Check bot ping"),
]


@Client.on_callback_query(filters.regex(r"^admin:maintenance$"))
async def admin_maintenance_menu(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    markup = _btn(
        [("🔄 Restart Bot", "admin:maint:restart"), ("🔃 Reload Config", "admin:maint:reload")],
        [("♻️ Refresh Runtime", "admin:maint:refresh"), ("🗑 Clear Cache", "admin:maint:clearcache")],
        [("🔁 Sync Commands", "admin:maint:synccmds"), ("⚠️ Reset Runtime", "admin:maint:resetrt")],
        [("« Back", "admin:main")],
    )
    await query.message.edit_text("🛠 <b>Maintenance</b>\nSystem tools:", reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^admin:maint:restart$"))
async def admin_maint_restart(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.message.edit_text("🔄 <b>Restarting bot…</b>")
    try:
        await client.send_message(LOG_CHANNEL, "🔄 Bot restarted via Admin Panel.")
    except Exception:
        pass
    await asyncio.sleep(1)
    os.execl(sys.executable, sys.executable, *sys.argv)


@Client.on_callback_query(filters.regex(r"^admin:maint:reload$"))
async def admin_maint_reload(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.message.edit_text(
        "🔃 <b>Reload Config</b>\n\n"
        "Config is loaded from environment variables at startup.\n"
        "Restart the bot to reload config changes.",
        reply_markup=_btn([("« Back", "admin:maintenance")]),
    )


@Client.on_callback_query(filters.regex(r"^admin:maint:refresh$"))
async def admin_maint_refresh(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    b_users, b_chats = await db.get_banned()
    temp.BANNED_USERS = b_users
    temp.BANNED_CHATS = b_chats
    await query.answer("Runtime refreshed ✅", show_alert=True)
    await query.message.edit_text(
        "♻️ <b>Refresh Runtime</b>\n\nBanned users/chats reloaded from DB.",
        reply_markup=_btn([("« Back", "admin:maintenance")]),
    )


@Client.on_callback_query(filters.regex(r"^admin:maint:clearcache$"))
async def admin_maint_clearcache(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.answer("Cache cleared ✅", show_alert=True)
    await query.message.edit_text(
        "🗑 <b>Clear Cache</b>\n\nIn-memory cache cleared.",
        reply_markup=_btn([("« Back", "admin:maintenance")]),
    )


@Client.on_callback_query(filters.regex(r"^admin:maint:synccmds$"))
async def admin_maint_synccmds(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.answer("Syncing…")
    try:
        await client.set_bot_commands(_BOT_COMMANDS)
        text = "🔁 <b>Sync Commands</b>\n\n✅ Bot commands synced with Telegram."
    except Exception as e:
        text = f"🔁 <b>Sync Commands</b>\n\n❌ Failed: {e}"
    await query.message.edit_text(text, reply_markup=_btn([("« Back", "admin:maintenance")]))


@Client.on_callback_query(filters.regex(r"^admin:maint:resetrt$"))
async def admin_maint_resetrt(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    markup = _btn(
        [("⚠️ Confirm Reset", "admin:maint:resetrt:confirm")],
        [("« Cancel", "admin:maintenance")],
    )
    await query.message.edit_text(
        "⚠️ <b>Reset Runtime</b>\n\nThis resets all in-memory toggles to defaults.\nAre you sure?",
        reply_markup=markup,
    )


@Client.on_callback_query(filters.regex(r"^admin:maint:resetrt:confirm$"))
async def admin_maint_resetrt_confirm(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    _rt["maintenance"] = False
    _rt["welcome_messages"] = MELCOW_NEW_USERS
    _rt["inline_mode"] = True
    _rt["auto_delete"] = False
    _rt["protect_content"] = PROTECT_CONTENT
    _rt["debug_mode"] = False
    _fsm.clear()
    await query.answer("Runtime reset ✅", show_alert=True)
    await query.message.edit_text(
        "⚠️ <b>Reset Runtime</b>\n\n✅ All runtime settings reset to defaults.",
        reply_markup=_btn([("« Back", "admin:maintenance")]),
    )


# ═════════════════════════════════════════════════════════════════════════════
# 📜  LOGS
# ═════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r"^admin:logs$"))
async def admin_logs_menu(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    markup = _btn(
        [("❌ Error Logs", "admin:logs:error"), ("🔄 Restart Logs", "admin:logs:restart")],
        [("📋 Activity Logs", "admin:logs:activity"), ("📢 Log Channel", "admin:logs:channel")],
        [("« Back", "admin:main")],
    )
    await query.message.edit_text("📜 <b>Logs</b>\nAccess bot logs:", reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^admin:logs:(error|restart|activity)$"))
async def admin_logs_file(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    log_type = query.matches[0].group(1)
    log_file = "logs/bot.log" if os.path.exists("logs/bot.log") else "TGBot.log"
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", errors="replace") as f:
                content = f.read()
            if log_type == "error":
                lines = [l for l in content.splitlines() if "ERROR" in l or "CRITICAL" in l]
                snippet = "\n".join(lines[-30:]) or "No errors found."
            elif log_type == "restart":
                lines = [l for l in content.splitlines() if "restart" in l.lower() or "start" in l.lower()]
                snippet = "\n".join(lines[-20:]) or "No restart entries."
            else:
                snippet = "\n".join(content.splitlines()[-40:])
            await query.message.reply_document(log_file, caption=f"📜 {log_type.title()} Logs")
            await query.message.edit_text(
                f"📜 <b>{log_type.title()} Logs</b>\n\n<code>{snippet[-2000:]}</code>",
                reply_markup=_btn([("« Back", "admin:logs")]),
            )
        except Exception as e:
            await query.message.edit_text(f"❌ Could not read logs: {e}", reply_markup=_btn([("« Back", "admin:logs")]))
    else:
        await query.message.edit_text("📜 No log file found.", reply_markup=_btn([("« Back", "admin:logs")]))


@Client.on_callback_query(filters.regex(r"^admin:logs:channel$"))
async def admin_logs_channel(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    try:
        ch = await client.get_chat(LOG_CHANNEL)
        ch_name = ch.title or str(LOG_CHANNEL)
    except Exception:
        ch_name = str(LOG_CHANNEL)
    await query.message.edit_text(
        f"📢 <b>Log Channel</b>\n\nCurrent: <b>{ch_name}</b> (<code>{LOG_CHANNEL}</code>)\n\n"
        "<i>Change via LOG_CHANNEL env var.</i>",
        reply_markup=_btn([("« Back", "admin:logs")]),
    )


# ═════════════════════════════════════════════════════════════════════════════
# ⚡  ADVANCED
# ═════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r"^admin:advanced$"))
async def admin_advanced_menu(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    markup = _btn(
        [("🧪 Developer Mode", "admin:adv:devmode"), ("🔧 Runtime Variables", "admin:adv:rtvars")],
        [("🚩 Feature Flags", "admin:adv:flags"), ("📤 Export Runtime", "admin:adv:exportrt")],
        [("📥 Import Runtime", "admin:adv:importrt"), ("⚠️ Reset All Settings", "admin:adv:resetall")],
        [("« Back", "admin:main")],
    )
    await query.message.edit_text("⚡ <b>Advanced</b>\nDeveloper & power options:", reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^admin:adv:devmode$"))
async def admin_adv_devmode(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    _rt["debug_mode"] = not _rt["debug_mode"]
    logging.getLogger().setLevel(logging.DEBUG if _rt["debug_mode"] else logging.INFO)
    state = "ON ✅" if _rt["debug_mode"] else "OFF ❌"
    await query.answer(f"Developer Mode → {state}", show_alert=True)
    await query.message.edit_text(
        f"🧪 <b>Developer Mode</b>\n\nCurrent: <b>{state}</b>\n\n"
        "Debug logging is now " + ("enabled." if _rt["debug_mode"] else "disabled."),
        reply_markup=_btn([("Toggle", "admin:adv:devmode"), ("« Back", "admin:advanced")]),
    )


@Client.on_callback_query(filters.regex(r"^admin:adv:rtvars$"))
async def admin_adv_rtvars(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    cfg = nu.get_runtime_update_config()
    lines = ["🔧 <b>Runtime Variables</b>\n"]
    for k, v in {**_rt, **cfg}.items():
        lines.append(f"• <code>{k}</code>: <b>{v}</b>")
    markup = _btn([("✏️ Edit Update Config", "admin:adv:editcfg")], [("« Back", "admin:advanced")])
    await query.message.edit_text("\n".join(lines), reply_markup=markup)


def _editcfg_markup() -> InlineKeyboardMarkup:
    cfg = nu.get_runtime_update_config()
    return _btn(
        [("➖", "admin:adv:cfg:PAGE_SIZE:-1"), (f"PAGE_SIZE: {cfg['PAGE_SIZE']}", "noop"), ("➕", "admin:adv:cfg:PAGE_SIZE:1")],
        [("➖", "admin:adv:cfg:GROUP_SIZE:-1"), (f"GROUP_SIZE: {cfg['GROUP_SIZE']}", "noop"), ("➕", "admin:adv:cfg:GROUP_SIZE:1")],
        [("➖", "admin:adv:cfg:GETDLINK_PAGE_SIZE:-1"), (f"DLink: {cfg['GETDLINK_PAGE_SIZE']}", "noop"), ("➕", "admin:adv:cfg:GETDLINK_PAGE_SIZE:1")],
        [("Mode: individual", "admin:adv:setmode:individual"), ("grouped", "admin:adv:setmode:grouped"), ("manual", "admin:adv:setmode:manual")],
        [("« Back", "admin:adv:rtvars")],
    )


@Client.on_callback_query(filters.regex(r"^admin:adv:editcfg$"))
async def admin_adv_editcfg(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    cfg = nu.get_runtime_update_config()
    await query.message.edit_text(
        f"🔧 <b>Edit Update Config</b>\n\nCurrent mode: <code>{cfg['CHANNEL_SEND_MODE']}</code>",
        reply_markup=_editcfg_markup(),
    )


@Client.on_callback_query(filters.regex(r"^admin:adv:cfg:(PAGE_SIZE|GROUP_SIZE|GETDLINK_PAGE_SIZE):(-?1)$"))
async def admin_adv_cfg_adjust(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    key, delta = query.matches[0].group(1), int(query.matches[0].group(2))
    cfg = nu.get_runtime_update_config()
    nu.set_runtime_update_config(key, cfg[key] + delta)
    await query.answer(f"{key} updated ✅")
    await query.message.edit_reply_markup(_editcfg_markup())


@Client.on_callback_query(filters.regex(r"^admin:adv:setmode:(individual|grouped|manual)$"))
async def admin_adv_setmode(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    mode = query.matches[0].group(1)
    nu.set_runtime_update_config("CHANNEL_SEND_MODE", mode)
    await query.answer(f"Mode → {mode} ✅", show_alert=False)
    await query.message.edit_reply_markup(_editcfg_markup())


@Client.on_callback_query(filters.regex(r"^admin:adv:flags$"))
async def admin_adv_flags(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    flags = {
        "IMDB": IMDB,
        "P_TTI_SHOW_OFF": P_TTI_SHOW_OFF,
        "HYPER_MODE": HYPER_MODE,
        "MELCOW_NEW_USERS": MELCOW_NEW_USERS,
        "PUBLIC_FILE_STORE": PUBLIC_FILE_STORE,
    }
    lines = ["🚩 <b>Feature Flags</b>\n"]
    for k, v in flags.items():
        icon = "✅" if v else "❌"
        lines.append(f"{icon} <code>{k}</code>")
    lines.append("\n<i>Toggle via env vars — restart to apply.</i>")
    await query.message.edit_text("\n".join(lines), reply_markup=_btn([("« Back", "admin:advanced")]))


@Client.on_callback_query(filters.regex(r"^admin:adv:exportrt$"))
async def admin_adv_exportrt(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    cfg = nu.get_runtime_update_config()
    export = {**_rt, **cfg}
    import json
    text = json.dumps(export, indent=2)
    await query.message.reply_document(
        document=bytes(text, "utf-8"),
        file_name="runtime_export.json",
        caption="📤 Runtime config export",
    )
    await query.answer("Exported ✅")


@Client.on_callback_query(filters.regex(r"^admin:adv:importrt$"))
async def admin_adv_importrt(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    _fsm[query.from_user.id] = {"state": "await_import_json"}
    await query.message.edit_text(
        "📥 <b>Import Runtime</b>\n\nSend a JSON document (exported earlier) to restore runtime settings.",
        reply_markup=_btn([("« Cancel", "admin:advanced")]),
    )


@Client.on_callback_query(filters.regex(r"^admin:adv:resetall$"))
async def admin_adv_resetall_confirm(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    markup = _btn(
        [("⚠️ Yes, reset everything", "admin:adv:resetall:do")],
        [("« Cancel", "admin:advanced")],
    )
    await query.message.edit_text(
        "⚠️ <b>Reset All Settings</b>\n\n"
        "This resets ALL runtime toggles & update config to defaults.\nCannot be undone.",
        reply_markup=markup,
    )


@Client.on_callback_query(filters.regex(r"^admin:adv:resetall:do$"))
async def admin_adv_resetall_do(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    _rt["maintenance"] = False
    _rt["welcome_messages"] = MELCOW_NEW_USERS
    _rt["inline_mode"] = True
    _rt["auto_delete"] = False
    _rt["protect_content"] = PROTECT_CONTENT
    _rt["debug_mode"] = False
    _fsm.clear()
    nu.set_runtime_update_config("PAGE_SIZE", 5)
    nu.set_runtime_update_config("SEND_DELAY", 0.5)
    nu.set_runtime_update_config("GETDLINK_PAGE_SIZE", 10)
    nu.set_runtime_update_config("GROUP_SIZE", 10)
    nu.set_runtime_update_config("CHANNEL_SEND_MODE", "individual")
    await query.answer("All settings reset ✅", show_alert=True)
    await query.message.edit_text(
        "⚠️ <b>Reset All Settings</b>\n\n✅ All runtime settings have been reset to defaults.",
        reply_markup=_btn([("« Back", "admin:advanced")]),
    )


# ═════════════════════════════════════════════════════════════════════════════
# Legacy FSUB / Movie Updates sections — kept and nested under new menu
# ═════════════════════════════════════════════════════════════════════════════

def _updates_text():
    cfg = nu.get_runtime_update_config()
    return (
        "<b>Movie Updates Config</b>\n\n"
        f"PAGE_SIZE: <code>{cfg['PAGE_SIZE']}</code>\n"
        f"SEND_DELAY: <code>{cfg['SEND_DELAY']}</code>\n"
        f"GETDLINK_PAGE_SIZE: <code>{cfg['GETDLINK_PAGE_SIZE']}</code>\n"
        f"GROUP_SIZE: <code>{cfg['GROUP_SIZE']}</code>\n"
        f"CHANNEL_SEND_MODE: <code>{cfg['CHANNEL_SEND_MODE']}</code>\n"
        f"GROUP_SEARCH_TEXT: <code>{cfg['GROUP_SEARCH_TEXT']}</code>"
    )


def _updates_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Movie Channels", callback_data="admin:upd:channels"), InlineKeyboardButton("Set New Chat", callback_data="admin:upd:setchat")],
        [InlineKeyboardButton("Mode", callback_data="admin:upd:mode"), InlineKeyboardButton("Group Size", callback_data="admin:upd:gsize")],
        [InlineKeyboardButton("Page Size", callback_data="admin:upd:psize"), InlineKeyboardButton("Send Delay", callback_data="admin:upd:sdelay")],
        [InlineKeyboardButton("GetDLink Size", callback_data="admin:upd:dlsize"), InlineKeyboardButton("Refresh", callback_data="admin:updates")],
        [styled_button("« Back", callback_data="admin:main")],
    ])


@Client.on_callback_query(filters.regex(r"^admin:fsub$"))
async def admin_fsub_menu(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("Set New Chats", callback_data="admin:fsub:set")],
        [InlineKeyboardButton("Show Current FSUB", callback_data="admin:fsub:show")],
        [styled_button("« Back", callback_data="admin:main")],
    ])
    await query.message.edit_text("FSUB options:", reply_markup=buttons)


@Client.on_callback_query(filters.regex(r"^admin:fsub:set$"))
async def admin_fsub_set(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.answer()
    await query.message.reply("Use:\n<code>/fsub -100123 -100456 ...</code>")


@Client.on_callback_query(filters.regex(r"^admin:fsub:show$"))
async def admin_fsub_show(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    text = await build_fsub_details_text(client)
    await query.message.edit_text(text, disable_web_page_preview=True)


@Client.on_callback_query(filters.regex(r"^admin:updates$"))
async def admin_updates(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.message.edit_text(_updates_text(), reply_markup=_updates_markup())


@Client.on_callback_query(filters.regex(r"^admin:upd:channels$"))
async def admin_upd_channels(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    ids = await db.get_update_chat_ids()
    if not ids:
        return await query.answer("No update chats set", show_alert=True)
    lines = ["<b>Update Chats</b>"]
    for cid in ids:
        try:
            c = await client.get_chat(int(cid))
            name = c.title or c.first_name or "Unknown"
            lines.append(f"\n• {name} - <code>{cid}</code>")
        except Exception:
            lines.append(f"\n• <code>{cid}</code>")
    await query.message.edit_text("\n".join(lines), reply_markup=_updates_markup())


@Client.on_callback_query(filters.regex(r"^admin:upd:setchat$"))
async def admin_upd_setchat(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    await query.message.reply("Use: <code>/setupchat -100123 -100456</code>")
    await query.answer()


@Client.on_callback_query(filters.regex(r"^admin:upd:mode$"))
async def admin_upd_mode(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("individual", callback_data="admin:setmode:individual"),
         InlineKeyboardButton("grouped", callback_data="admin:setmode:grouped"),
         InlineKeyboardButton("manual", callback_data="admin:setmode:manual")],
        [styled_button("« Back", callback_data="admin:updates")],
    ])
    await query.message.edit_text("Choose CHANNEL_SEND_MODE:", reply_markup=kb)


@Client.on_callback_query(filters.regex(r"^admin:setmode:(individual|grouped|manual)$"))
async def admin_setmode(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    mode = query.matches[0].group(1)
    nu.set_runtime_update_config("CHANNEL_SEND_MODE", mode)
    await query.answer(f"Mode set to {mode}")
    await query.message.edit_text(_updates_text(), reply_markup=_updates_markup())


@Client.on_callback_query(filters.regex(r"^admin:upd:(gsize|psize|dlsize|sdelay)$"))
async def admin_upd_numeric(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    key = query.matches[0].group(1)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("-", callback_data=f"admin:num:{key}:-1"), InlineKeyboardButton("+", callback_data=f"admin:num:{key}:1")],
        [styled_button("« Back", callback_data="admin:updates")],
    ])
    await query.message.edit_text(f"Adjust {key}", reply_markup=kb)


@Client.on_callback_query(filters.regex(r"^admin:num:(gsize|psize|dlsize|sdelay):(-?1)$"))
async def admin_num_apply(client: Client, query: CallbackQuery):
    if not is_admin(query.from_user):
        return await query.answer("Not allowed", show_alert=True)
    key, delta = query.matches[0].group(1), int(query.matches[0].group(2))
    cfg = nu.get_runtime_update_config()
    if key == "gsize":
        nu.set_runtime_update_config("GROUP_SIZE", cfg["GROUP_SIZE"] + delta)
    elif key == "psize":
        nu.set_runtime_update_config("PAGE_SIZE", cfg["PAGE_SIZE"] + delta)
    elif key == "dlsize":
        nu.set_runtime_update_config("GETDLINK_PAGE_SIZE", cfg["GETDLINK_PAGE_SIZE"] + delta)
    elif key == "sdelay":
        nu.set_runtime_update_config("SEND_DELAY", round(cfg["SEND_DELAY"] + (0.1 * delta), 2))
    await query.answer("Updated")
    await query.message.edit_text(_updates_text(), reply_markup=_updates_markup())


# ═════════════════════════════════════════════════════════════════════════════
# FSM MESSAGE HANDLER — catches text messages when awaiting input
# ═════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.private & filters.text & ~filters.command(["start", "settings", "admin"]))
async def admin_fsm_handler(client: Client, message: Message):
    uid = message.from_user.id if message.from_user else None
    if not uid or uid not in _fsm:
        # Not an admin-panel input in progress — this is a regular PM message
        # (e.g. a movie search query). Let it fall through to other handlers
        # such as pm_filter.give_filter instead of swallowing it here.
        raise ContinuePropagation
    if not is_admin(message.from_user):
        _fsm.pop(uid, None)
        raise ContinuePropagation

    state_data = _fsm.pop(uid)
    state = state_data["state"]

    if state == "await_ban_id":
        try:
            target = int(message.text.strip())
            if not await db.is_user_exist(target):
                try:
                    u = await client.get_users(target)
                    name = u.first_name or str(target)
                except Exception:
                    name = str(target)
                await db.add_user(target, name)
            await db.ban_user(target, "Banned via Admin Panel")
            if target not in temp.BANNED_USERS:
                temp.BANNED_USERS.append(target)
            await message.reply(f"🚫 User <code>{target}</code> banned.")
        except ValueError:
            await message.reply("❌ Invalid user ID.")
        except Exception as e:
            await message.reply(f"❌ Error: {e}")

    elif state == "await_unban_id":
        try:
            target = int(message.text.strip())
            await db.remove_ban(target)
            if target in temp.BANNED_USERS:
                temp.BANNED_USERS.remove(target)
            await message.reply(f"✅ User <code>{target}</code> unbanned.")
        except ValueError:
            await message.reply("❌ Invalid user ID.")
        except Exception as e:
            await message.reply(f"❌ Error: {e}")

    elif state == "await_disable_chat_id":
        try:
            cid = int(message.text.strip())
            existing = await db.get_chat(cid)
            if existing is False:
                try:
                    chat_title = (await client.get_chat(cid)).title or str(cid)
                except Exception:
                    chat_title = str(cid)
                await db.add_chat(cid, chat_title)
            await db.disable_chat(cid, "Disabled via Admin Panel")
            if cid not in temp.BANNED_CHATS:
                temp.BANNED_CHATS.append(cid)
            await message.reply(f"🔇 Chat <code>{cid}</code> disabled.")
        except ValueError:
            await message.reply("❌ Invalid chat ID.")
        except Exception as e:
            await message.reply(f"❌ Error: {e}")

    elif state == "await_enable_chat_id":
        try:
            cid = int(message.text.strip())
            await db.re_enable_chat(cid)
            if cid in temp.BANNED_CHATS:
                temp.BANNED_CHATS.remove(cid)
            await message.reply(f"✅ Chat <code>{cid}</code> re-enabled.")
        except ValueError:
            await message.reply("❌ Invalid chat ID.")
        except Exception as e:
            await message.reply(f"❌ Error: {e}")

    elif state == "await_auth_channels":
        try:
            ids = [int(x) for x in message.text.strip().split()]
            await db.set_auth_channels(ids)
            await message.reply(f"✅ Auth channels updated: {ids}")
        except ValueError:
            await message.reply("❌ Invalid IDs. Send space-separated integers.")
        except Exception as e:
            await message.reply(f"❌ Error: {e}")

    elif state == "await_import_json":
        import json
        try:
            data = json.loads(message.text.strip())
            for k, v in data.items():
                if k in _rt:
                    _rt[k] = v
                else:
                    try:
                        nu.set_runtime_update_config(k, v)
                    except Exception:
                        pass
            await message.reply("✅ Runtime settings imported successfully.")
        except json.JSONDecodeError:
            await message.reply("❌ Invalid JSON. Send the exported JSON as plain text.")
        except Exception as e:
            await message.reply(f"❌ Error: {e}")
