import asyncio
from typing import List
import re

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from core.config_loader import load_app_config
from clients.three_x_ui import ThreeXUIClient, ThreeXUIError
from storage.db import (
    init_db,
    register_user,
    count_user_configs,
    add_config_record,
    get_configs_by_numeric_id,
    set_user_limit,
)


# Conversation states for create flow
WAIT_NUMERIC_ID, WAIT_INBOUND_SELECT, WAIT_VOLUME_GB, WAIT_DAYS, WAIT_USERNAME = range(5)

# Conversation state for listing configs
WAIT_LIST_NUMERIC_ID = 100

# Conversation state for viewing stats
WAIT_STATS_USERNAME = 200


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            'Welcome. Use /create to create a config, /myconfigs to list, /mystats to view usage.'
        )


async def cmd_inbounds(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    client: ThreeXUIClient = context.application.bot_data['3x']
    try:
        inbounds = await client.list_inbounds()
    except ThreeXUIError as e:
        if update.message:
            await update.message.reply_text(f'Failed to list inbounds: {e}')
        return
    if not inbounds:
        if update.message:
            await update.message.reply_text('No inbounds found.')
        return
    lines: List[str] = []
    for item in inbounds[:20]:
        inbound_id = item.get('id') or item.get('inboundId')
        title = item.get('remark') or str(inbound_id)
        lines.append(f"id={inbound_id}  {title}")
    if update.message:
        await update.message.reply_text('\n'.join(lines))


async def create_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text('Send your numeric ID (provided by admin).')
    context.user_data.clear()
    return WAIT_NUMERIC_ID


async def on_numeric_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    numeric_text = (update.message.text if update.message else '').strip()
    try:
        numeric_id = int(numeric_text)
    except Exception:
        if update.message:
            await update.message.reply_text('Invalid numeric ID. Send /create to retry.')
        return ConversationHandler.END

    app = context.application.bot_data['appcfg']
    await register_user(numeric_id, update.effective_user.id, app.bot.per_user_limit)
    used = await count_user_configs(numeric_id)
    if used >= app.bot.per_user_limit:
        if update.message:
            await update.message.reply_text(f'Limit reached ({used}/{app.bot.per_user_limit}). Contact admin.')
        return ConversationHandler.END

    client: ThreeXUIClient = context.application.bot_data['3x']
    try:
        inbounds = await client.list_inbounds()
    except ThreeXUIError as e:
        if update.message:
            await update.message.reply_text(f'Failed to list inbounds: {e}')
        return ConversationHandler.END

    buttons: List[List[InlineKeyboardButton]] = []
    for item in inbounds[:20]:
        inbound_id = item.get('id') or item.get('inboundId')
        title = item.get('remark') or f'Inbound {inbound_id}'
        if inbound_id is not None:
            buttons.append([
                InlineKeyboardButton(
                    f'{title} (id={inbound_id})', callback_data=f'inb:{inbound_id}:{numeric_id}'
                )
            ])
    if not buttons:
        if update.message:
            await update.message.reply_text('No inbounds available.')
        return ConversationHandler.END
    if update.message:
        await update.message.reply_text('Choose inbound:', reply_markup=InlineKeyboardMarkup(buttons))
    return WAIT_INBOUND_SELECT


async def on_inbound_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, inbound_id_str, numeric_id_str = query.data.split(':', 2)
    context.user_data['inbound_id'] = int(inbound_id_str)
    context.user_data['numeric_id'] = int(numeric_id_str)
    await query.edit_message_text('Enter volume limit in GB (e.g., 10)')
    return WAIT_VOLUME_GB


async def on_volume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text if update.message else '').strip()
    gb = _parse_float_from_text(raw)
    if gb is None or gb <= 0:
        if update.message:
            await update.message.reply_text('Invalid number. Send GB as a positive number.')
        return WAIT_VOLUME_GB
    context.user_data['total_gb'] = gb
    if update.message:
        await update.message.reply_text('Enter expiration days (e.g., 30)')
    return WAIT_DAYS


async def on_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text if update.message else '').strip()
    days = _parse_int_from_text(raw)
    if days is None or days <= 0:
        if update.message:
            await update.message.reply_text('Invalid days. Enter a positive integer.')
        return WAIT_DAYS
    context.user_data['expiry_days'] = days
    if update.message:
        await update.message.reply_text('Enter a username identifier (email-like or name)')
    return WAIT_USERNAME


async def on_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = (update.message.text if update.message else '').strip()
    inbound_id = context.user_data.get('inbound_id')
    numeric_id = context.user_data.get('numeric_id')
    total_gb = context.user_data.get('total_gb')
    expiry_days = context.user_data.get('expiry_days')
    if not all(v is not None for v in (inbound_id, numeric_id, total_gb, expiry_days)):
        if update.message:
            await update.message.reply_text('Flow lost state. Please /create again.')
        return ConversationHandler.END

    app = context.application.bot_data['appcfg']
    if await count_user_configs(numeric_id) >= app.bot.per_user_limit:
        if update.message:
            await update.message.reply_text('Limit reached. Contact admin.')
        return ConversationHandler.END

    client: ThreeXUIClient = context.application.bot_data['3x']
    try:
        resp = await client.add_client(
            inbound_id=inbound_id,
            username=username,
            total_gb=float(total_gb),
            expiry_days=int(expiry_days),
        )
    except ThreeXUIError as e:
        if update.message:
            await update.message.reply_text(f'Failed to create client: {e}')
        return ConversationHandler.END

    sub_url = ''
    if isinstance(resp, dict):
        sub_url = resp.get('subscription') or resp.get('url') or ''
    if not sub_url:
        base = getattr(app, 'subscription_base_url', '')
        if base:
            sub_url = f"{base.rstrip('/')}/{username}"

    client_id = (
        str(resp.get('id') or resp.get('clientId') or '') if isinstance(resp, dict) else ''
    )
    await add_config_record(
        int(numeric_id),
        update.effective_user.id,
        int(inbound_id),
        username,
        client_id,
        int(float(total_gb) * 1024 * 1024 * 1024),
        int(expiry_days),
        str(resp),
    )

    text = 'Client created.'
    if sub_url:
        text += f'\nLink: {sub_url}'
    if update.message:
        await update.message.reply_text(text)
    return ConversationHandler.END


async def myconfigs_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text('Send your numeric ID to view configs.')
    return WAIT_LIST_NUMERIC_ID


async def on_list_numeric(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or '').strip() if update.message else ''
    try:
        numeric_id = int(text)
    except Exception:
        if update.message:
            await update.message.reply_text('Invalid ID. Send a number.')
        return WAIT_LIST_NUMERIC_ID
    rows = await get_configs_by_numeric_id(numeric_id)
    if not rows:
        if update.message:
            await update.message.reply_text('No configs found.')
        return ConversationHandler.END
    lines: List[str] = []
    for r in rows[:10]:
        lines.append(
            f"Inbound {r['inbound_id']} | user {r['client_identifier']} | created {r['created_at']}"
        )
    if update.message:
        await update.message.reply_text('\n'.join(lines))
    return ConversationHandler.END


async def mystats_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text('Send the username/email of your client to view traffic stats.')
    return WAIT_STATS_USERNAME


async def on_stats_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = (update.message.text or '').strip() if update.message else ''
    if not username:
        if update.message:
            await update.message.reply_text('Please send a valid username/email.')
        return WAIT_STATS_USERNAME
    client: ThreeXUIClient = context.application.bot_data['3x']
    try:
        data = await client.get_client_traffics(email=username)
    except ThreeXUIError as e:
        if update.message:
            await update.message.reply_text(f'Failed to fetch stats: {e}')
        return ConversationHandler.END

    text_lines: List[str] = []
    if isinstance(data, dict):
        for key in ['up', 'down', 'total', 'remain', 'expiryTime', 'enable']:
            if key in data:
                text_lines.append(f'{key}: {data[key]}')
    if not text_lines:
        text_lines.append(str(data))
    if update.message:
        await update.message.reply_text('\n'.join(text_lines))
    return ConversationHandler.END


async def setlimit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application.bot_data['appcfg']
    admin_ids = set(app.bot.admin_numeric_ids or [])
    if update.effective_user.id not in admin_ids:
        if update.message:
            await update.message.reply_text('Unauthorized.')
        return
    parts = (update.message.text or '').strip().split()
    if len(parts) != 3:
        if update.message:
            await update.message.reply_text('Usage: /setlimit <numeric_id> <limit>')
        return
    try:
        numeric_id = int(parts[1])
        limit = int(parts[2])
        if limit <= 0:
            raise ValueError
    except Exception:
        if update.message:
            await update.message.reply_text('Provide valid integers for id and limit (>0).')
        return
    await set_user_limit(numeric_id, limit)
    if update.message:
        await update.message.reply_text(f'Set limit {limit} for numeric ID {numeric_id}.')


def _fa2en_digits(text: str) -> str:
    mapping = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')
    return text.translate(mapping)


def _parse_int_from_text(text: str) -> int | None:
    s = _fa2en_digits(text)
    match = re.search(r'(\d+)', s)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _parse_float_from_text(text: str) -> float | None:
    s = _fa2en_digits(text)
    match = re.search(r'(\d+(?:[\.,]\d+)?)', s)
    if not match:
        return None
    num = match.group(1).replace(',', '.')
    try:
        return float(num)
    except Exception:
        return None


def run() -> None:
    appcfg = load_app_config()

    # Ensure an event loop exists (fixes Python 3.10 get_event_loop error)
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    # Ensure database exists
    loop.run_until_complete(init_db())

    # Build Telegram application
    application = Application.builder().token(appcfg.bot.token).build()

    # Prepare API client in bot_data (created in the running loop via post init)
    async def _post_init(_: Application) -> None:
        application.bot_data['appcfg'] = appcfg
        application.bot_data['3x'] = ThreeXUIClient(
            appcfg.panel.base_url,
            appcfg.admin.username,
            appcfg.admin.password,
            insecure=appcfg.panel.insecure,
        )

    application.post_init = _post_init  # type: ignore[attr-defined]

    # Create conversation for creating configs
    conv_create = ConversationHandler(
        entry_points=[CommandHandler('create', create_entry)],
        states={
            WAIT_NUMERIC_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_numeric_id)],
            WAIT_INBOUND_SELECT: [CallbackQueryHandler(on_inbound_selected, pattern='^inb:')],
            WAIT_VOLUME_GB: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_volume)],
            WAIT_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_days)],
            WAIT_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_username)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    # Conversation for listing configs
    conv_list = ConversationHandler(
        entry_points=[CommandHandler('myconfigs', myconfigs_entry)],
        states={
            WAIT_LIST_NUMERIC_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_list_numeric)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    # Conversation for viewing stats
    conv_stats = ConversationHandler(
        entry_points=[CommandHandler('mystats', mystats_entry)],
        states={
            WAIT_STATS_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_stats_username)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('inbounds', cmd_inbounds))
    application.add_handler(CommandHandler('setlimit', setlimit))
    application.add_handler(conv_create)
    application.add_handler(conv_list)
    application.add_handler(conv_stats)

    # Blocking call - handles its own event loop internally
    application.run_polling()

import asyncio
from typing import List
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, ConversationHandler, MessageHandler, filters
from core.config_loader import load_app_config
from clients.three_x_ui import ThreeXUIClient, ThreeXUIError
from storage.db import init_db, register_user, count_user_configs, add_config_record, get_configs_by_numeric_id

CHOOSING_INBOUND, ASK_VOLUME, ASK_DAYS, ASK_USERNAME = range(4)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text('Welcome. Use /create to create a config, /myconfigs to view.')

async def create_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text('Please send your numeric ID (provided by admin).')
    context.user_data.clear()
    return CHOOSING_INBOUND

async def ask_inbound(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    numeric_text = (update.message.text if update.message else '').strip()
    try:
        numeric_id = int(numeric_text)
    except Exception:
        if update.message:
            await update.message.reply_text('Invalid numeric ID. Send /create to retry.')
        return ConversationHandler.END

    app = context.application.bot_data['appcfg']
    await register_user(numeric_id, update.effective_user.id, app.bot.per_user_limit)
    used = await count_user_configs(numeric_id)
    if used >= app.bot.per_user_limit:
        if update.message:
            await update.message.reply_text(f'Limit reached ({used}/{app.bot.per_user_limit}). Contact admin.')
        return ConversationHandler.END

    client: ThreeXUIClient = context.application.bot_data['3x']
    try:
        inbounds = await client.list_inbounds()
    except ThreeXUIError as e:
        if update.message:
            await update.message.reply_text(f'Failed to list inbounds: {e}')
        return ConversationHandler.END

    buttons: List[List[InlineKeyboardButton]] = []
    for item in inbounds[:20]:
        inbound_id = item.get('id') or item.get('inboundId')
        title = item.get('remark') or f'Inbound {inbound_id}'
        if inbound_id is not None:
            buttons.append([InlineKeyboardButton(f'{title} (id={inbound_id})', callback_data=f'inb:{inbound_id}:{numeric_id}')])
    if not buttons:
        if update.message:
            await update.message.reply_text('No inbounds available.')
        return ConversationHandler.END
    if update.message:
        await update.message.reply_text('Choose inbound:', reply_markup=InlineKeyboardMarkup(buttons))
    return ASK_VOLUME

async def on_inbound_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, inbound_id_str, numeric_id_str = query.data.split(':', 2)
    context.user_data['inbound_id'] = int(inbound_id_str)
    context.user_data['numeric_id'] = int(numeric_id_str)
    await query.edit_message_text('Enter volume limit in GB (e.g., 10)')
    return ASK_DAYS

async def on_volume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        gb = float((update.message.text if update.message else '').strip())
        if gb <= 0:
            raise ValueError
    except Exception:
        if update.message:
            await update.message.reply_text('Invalid number. Send GB as a positive number.')
        return ASK_DAYS
    context.user_data['total_gb'] = gb
    if update.message:
        await update.message.reply_text('Enter expiration days (e.g., 30)')
    return ASK_USERNAME

async def on_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        days = int((update.message.text if update.message else '').strip())
        if days <= 0:
            raise ValueError
    except Exception:
        if update.message:
            await update.message.reply_text('Invalid days. Enter a positive integer.')
        return ASK_USERNAME
    context.user_data['expiry_days'] = days
    if update.message:
        await update.message.reply_text('Enter a username identifier (email-like or name)')
    return ASK_USERNAME

async def on_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = (update.message.text if update.message else '').strip()
    inbound_id = context.user_data.get('inbound_id')
    numeric_id = context.user_data.get('numeric_id')
    total_gb = context.user_data.get('total_gb')
    expiry_days = context.user_data.get('expiry_days')
    if not all(v is not None for v in (inbound_id, numeric_id, total_gb, expiry_days)):
        if update.message:
            await update.message.reply_text('Flow lost state. Please /create again.')
        return ConversationHandler.END

    app = context.application.bot_data['appcfg']
    if await count_user_configs(numeric_id) >= app.bot.per_user_limit:
        if update.message:
            await update.message.reply_text('Limit reached. Contact admin.')
        return ConversationHandler.END

    client: ThreeXUIClient = context.application.bot_data['3x']
    try:
        resp = await client.add_client(inbound_id=inbound_id, username=username, total_gb=total_gb, expiry_days=expiry_days)
    except ThreeXUIError as e:
        if update.message:
            await update.message.reply_text(f'Failed to create client: {e}')
        return ConversationHandler.END

    sub_url = ''
    if isinstance(resp, dict):
        sub_url = resp.get('subscription') or resp.get('url') or ''
    if not sub_url:
        base = getattr(app, 'subscription_base_url', '')
        if base:
            sub_url = f"{base.rstrip('/')}/{username}"

    client_id = str(resp.get('id') or resp.get('clientId') or '') if isinstance(resp, dict) else ''
    await add_config_record(numeric_id, update.effective_user.id, inbound_id, username, client_id, int(float(total_gb)*1024*1024*1024), int(expiry_days), str(resp))

    text = 'Client created.'
    if sub_url:
        text += f'\nLink: {sub_url}'
    if update.message:
        await update.message.reply_text(text)
    return ConversationHandler.END

async def my_configs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text('Send your numeric ID to view configs.')
    context.user_data['awaiting_numeric_for_list'] = True

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or '').strip() if update.message else ''
    if context.user_data.get('awaiting_numeric_for_list'):
        try:
            numeric_id = int(text)
        except Exception:
            if update.message:
                await update.message.reply_text('Invalid ID. Send a number.')
            return
        context.user_data['awaiting_numeric_for_list'] = False
        rows = await get_configs_by_numeric_id(numeric_id)
        if not rows:
            if update.message:
                await update.message.reply_text('No configs found.')
            return
        lines = []
        for r in rows[:10]:
            lines.append(f"Inbound {r['inbound_id']} | user {r['client_identifier']} | created {r['created_at']}")
        if update.message:
            await update.message.reply_text('\n'.join(lines))
        return

# Note: A previous async run() variant existed; remove duplicates to avoid confusion.
