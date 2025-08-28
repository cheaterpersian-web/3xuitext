import asyncio
from typing import List
import re
import random
import string
import os
import uuid as _uuid
import urllib.parse as _up
from datetime import datetime, timezone

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
    get_user,
)


# Conversation states for create flow
WAIT_NUMERIC_ID, WAIT_INBOUND_SELECT, WAIT_VOLUME_GB, WAIT_DAYS, WAIT_USERNAME = range(5)
DEFAULT_EXPIRY_DAYS = int(os.getenv('DEFAULT_EXPIRY_DAYS', '30'))

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
    urec = await get_user(numeric_id)
    allowed = int(urec.get('max_configs') if urec else app.bot.per_user_limit)
    if used >= allowed:
        if update.message:
            await update.message.reply_text(f'Limit reached ({used}/{allowed}). Contact admin.')
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
    # Set default expiry days and go ask username directly
    context.user_data['expiry_days'] = DEFAULT_EXPIRY_DAYS
    if update.message:
        await update.message.reply_text('Username (letters, digits, -):')
    return WAIT_USERNAME


async def on_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Legacy handler kept for safety; we now default to 30 days and skip asking days.
    context.user_data['expiry_days'] = DEFAULT_EXPIRY_DAYS
    if update.message:
        await update.message.reply_text('Username (letters, digits, -):')
    return WAIT_USERNAME


async def on_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw_username = (update.message.text if update.message else '').strip()
    username = _clean_username(raw_username)
    if not username or len(username) < 3:
        if update.message:
            await update.message.reply_text('Username is too short. Send another (letters, digits, -).')
        return WAIT_USERNAME
    # validate allowed chars: letters, digits, -
    if not re.fullmatch(r'[A-Za-z0-9\-]+', username):
        if update.message:
            await update.message.reply_text('Only letters, digits, and - are allowed. Send another.')
        return WAIT_USERNAME
    inbound_id = context.user_data.get('inbound_id')
    numeric_id = context.user_data.get('numeric_id')
    total_gb = context.user_data.get('total_gb')
    expiry_days = context.user_data.get('expiry_days')
    if not all(v is not None for v in (inbound_id, numeric_id, total_gb, expiry_days)):
        if update.message:
            await update.message.reply_text('Flow lost state. Please /create again.')
        return ConversationHandler.END

    app = context.application.bot_data['appcfg']
    client: ThreeXUIClient = context.application.bot_data['3x']
    # duplicate check
    try:
        exists = await client.get_client_traffics(email=username)
        if isinstance(exists, dict) and (exists.get('up') is not None or exists.get('obj') is not None or exists.get('total') is not None):
            if update.message:
                await update.message.reply_text('این نام کاربری قبلاً استفاده شده است. نام دیگری بفرستید.')
            return WAIT_USERNAME
    except Exception:
        pass

    # create identifiers
    uid = str(_uuid.uuid4())
    sub_id = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=16))

    try:
        resp = await client.add_client(
            inbound_id=int(inbound_id),
            username=username,
            total_gb=float(total_gb),
            expiry_days=int(expiry_days),
            client_uuid=uid,
            sub_id=sub_id,
        )
    except ThreeXUIError as e:
        msg = str(e)
        if 'Duplicate' in msg or 'duplicate' in msg:
            if update.message:
                await update.message.reply_text('این نام کاربری قبلاً وجود دارد. نام دیگری بفرستید.')
            return WAIT_USERNAME
        if update.message:
            await update.message.reply_text(f'Failed to create client: {e}')
        return ConversationHandler.END

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
    
    # configs extraction
    cfg_lines: List[str] = []
    if isinstance(resp, dict):
        for k in ('vmess', 'vless', 'trojan', 'shadowsocks', 'ss', 'sing-box', 'clash', 'hysteria'):
            val = resp.get(k)
            if isinstance(val, str) and val.strip():
                cfg_lines.append(val.strip())
        cfgs = resp.get('configs')
        if isinstance(cfgs, list):
            for item in cfgs:
                if isinstance(item, str) and item.strip():
                    cfg_lines.append(item.strip())

    if not cfg_lines:
        try:
            inbound = await client.get_inbound(inbound_id=int(inbound_id))
            if isinstance(inbound, dict):
                inb = inbound.get('obj') if 'obj' in inbound else inbound
                vless_line = _build_vless_from_inbound(app, inb, uid, username)
                if vless_line:
                    cfg_lines.append(vless_line)
        except Exception:
            pass

    # env-based vless
    v_host = os.getenv('VLESS_HOST')
    v_port = os.getenv('VLESS_PORT')
    if v_host and v_port:
        v_type = os.getenv('VLESS_TYPE', 'tcp')
        v_path = os.getenv('VLESS_PATH', '/')
        v_sni = os.getenv('VLESS_SNI', '')
        v_hdr = os.getenv('VLESS_HEADER_TYPE', 'http')
        v_sec = os.getenv('VLESS_SECURITY', 'none')
        suffix = os.getenv('CONFIG_REMARK_SUFFIX', '')
        remark = _up.quote(username + suffix)
        q = {
            'type': v_type,
            'path': v_path,
            'host': v_sni,
            'headerType': v_hdr,
            'security': v_sec,
        }
        query = '&'.join(f"{k}={_up.quote(str(v))}" for k, v in q.items() if str(v))
        cfg_line = f"vless://{uid}@{v_host}:{v_port}?{query}#{remark}"
        cfg_lines.insert(0, cfg_line)

    text = f'Client created.\nUsername: {username}'
    if cfg_lines:
        text += '\n' + '\n'.join(cfg_lines[:5])
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

    # Pretty format the traffic info
    try:
        up_b = int(data.get('up', 0) or 0)
        down_b = int(data.get('down', 0) or 0)
        total_b = int(data.get('total', 0) or 0)
        used_b = up_b + down_b
        remain_b = max(0, total_b - used_b)

        def gb(x: int) -> float:
            return x / (1024 * 1024 * 1024)

        up_g, down_g, total_g, used_g, remain_g = gb(up_b), gb(down_b), gb(total_b), gb(used_b), gb(remain_b)
        pct = (used_b / total_b * 100.0) if total_b > 0 else 0.0

        exp_ms = int(data.get('expiryTime') or 0)
        exp_str = 'نامشخص'
        days_left = 'نامشخص'
        status = 'فعال' if bool(data.get('enable', True)) else 'غیرفعال'
        if exp_ms > 0:
            dt = datetime.fromtimestamp(exp_ms / 1000.0, tz=timezone.utc).astimezone()
            exp_str = dt.strftime('%Y-%m-%d %H:%M')
            now = datetime.now(tz=timezone.utc).astimezone()
            delta = dt - now
            days_left = f"{max(0, delta.days)} روز"
            if delta.total_seconds() < 0:
                status = 'منقضی'

        text = (
            f"نام کاربری: {username}\n"
            f"حجم کل: {total_g:.2f} GB\n"
            f"مصرف شده: {used_g:.2f} GB ({pct:.1f}%)\n"
            f"باقی‌مانده: {remain_g:.2f} GB\n"
            f"آپلود: {up_g:.2f} GB | دانلود: {down_g:.2f} GB\n"
            f"انقضا: {exp_str} ({days_left})\n"
            f"وضعیت: {status}"
        )
    except Exception:
        text = str(data)

    if update.message:
        await update.message.reply_text(text)
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


def _clean_username(text: str) -> str:
    # keep letters, digits, - _ . @
    s = _fa2en_digits(text)
    s = re.sub(r'[^A-Za-z0-9._@-]+', '', s)
    return s[:64]


def _generate_username(prefix: str, seed: int | None = None) -> str:
    base = _uuid.uuid4().hex[:8]
    rnd3 = random.randint(100, 999)
    return f"{prefix}{base}-{rnd3}"


def _build_vless_from_inbound(appcfg, inbound: dict, uid: str, remark: str) -> str | None:
    try:
        port = inbound.get('port') or inbound.get('listen_port')
        if not port:
            return None
        # Host: prefer env VLESS_HOST, else derive from PANEL_BASE_URL
        v_host = os.getenv('VLESS_HOST')
        if not v_host:
            try:
                parsed = _up.urlparse(appcfg.panel.base_url)
                v_host = parsed.hostname or ''
            except Exception:
                v_host = ''
        if not v_host:
            return None

        stream = inbound.get('streamSettings') or {}
        network = (stream.get('network') or 'tcp').lower()
        security = 'none'
        tls = stream.get('security') or ''
        if isinstance(tls, str) and tls.lower() in ('tls', 'reality'):
            security = tls.lower()

        # Extract path/host/headerType from ws/http/tcp settings
        path = ''
        host_hdr = ''
        header_type = ''
        ws = stream.get('wsSettings') or {}
        if ws:
            path = ws.get('path') or ''
            headers = ws.get('headers') or {}
            host_hdr = headers.get('Host') or headers.get('host') or ''
        tcp = stream.get('tcpSettings') or {}
        if tcp:
            hdr = (tcp.get('header') or {})
            header_type = (hdr.get('type') or '')
            if header_type.lower() == 'http':
                req = hdr.get('request') or {}
                hosts = req.get('headers', {}).get('Host') or []
                if isinstance(hosts, list) and hosts:
                    host_hdr = hosts[0]

        # Build vless URL
        q = {'type': network}
        if path:
            q['path'] = path
        if host_hdr:
            q['host'] = host_hdr
        if header_type:
            q['headerType'] = header_type
        if security and security != 'none':
            q['security'] = security
        query = '&'.join(f"{k}={_up.quote(str(v))}" for k, v in q.items() if str(v))
        return f"vless://{uid}@{v_host}:{port}?{query}#{_up.quote(remark)}"
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

    
