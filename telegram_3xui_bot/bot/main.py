import asyncio
from typing import List
import re
import random
import string
import os
import uuid as _uuid
import urllib.parse as _up
from datetime import datetime, timezone, timedelta
import logging
from telegram.constants import ParseMode
import httpx
from telegram.error import TimedOut
from telegram.request import HTTPXRequest

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
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
    set_setting,
    get_setting,
    get_all_settings,
    set_inbound_port as db_set_inbound_port,
    get_inbound_port as db_get_inbound_port,
    unset_inbound_port as db_unset_inbound_port,
    count_test_configs_by_telegram_user,
    get_latest_config_by_identifier,
    get_user_config_stats,
    get_all_configs_non_test,
)


# Conversation states for create flow (numeric id step removed)
WAIT_INBOUND_SELECT, WAIT_VOLUME_GB, WAIT_DAYS, WAIT_USERNAME = range(4)
DEFAULT_EXPIRY_DAYS = int(os.getenv('DEFAULT_EXPIRY_DAYS', '30'))

# Conversation state for listing configs
WAIT_LIST_NUMERIC_ID = 100

# Conversation state for viewing stats
WAIT_STATS_USERNAME = 200


logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("/start by user_id=%s text=%r", getattr(update.effective_user, 'id', None), getattr(update.message, 'text', ''))
    if update.message:
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton('ساخت کانفیگ')],
             [KeyboardButton('استعلام سرویس')],
             [KeyboardButton('کانفیگ تست')]],
            resize_keyboard=True
        )
        await update.message.reply_text('لطفاً یک گزینه را انتخاب کنید:', reply_markup=kb)


async def cmd_inbounds(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    client: ThreeXUIClient = context.application.bot_data['3x']
    try:
        inbounds = await client.list_inbounds()
    except ThreeXUIError:
        logger.exception("list_inbounds failed user_id=%s", getattr(update.effective_user, 'id', None))
        if update.message:
            await update.message.reply_text('خطا در دریافت ورودی‌ها')
        return
    if not inbounds:
        if update.message:
            await update.message.reply_text('هیچ ورودی‌ای یافت نشد.')
        return
    lines: List[str] = []
    for item in inbounds[:20]:
        inbound_id = item.get('id') or item.get('inboundId')
        title = item.get('remark') or str(inbound_id)
        lines.append(f"id={inbound_id}  {title}")
    if update.message:
        await update.message.reply_text('\n'.join(lines))


async def create_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Use Telegram user id as numeric id automatically
    context.user_data.clear()
    numeric_id = update.effective_user.id
    logger.info("create_entry start user_id=%s via_text=%r", numeric_id, getattr(update.message, 'text', ''))

    # If started via button 'کانفیگ تست'
    try:
        if update.message and (update.message.text or '').strip() == 'کانفیگ تست':
            context.user_data['is_test'] = 1
            context.user_data['total_gb'] = 1
            context.user_data['expiry_days'] = DEFAULT_EXPIRY_DAYS
    except Exception:
        pass

    app = context.application.bot_data['appcfg']
    await register_user(numeric_id, update.effective_user.id, app.bot.per_user_limit)
    used = await count_user_configs(numeric_id)
    urec = await get_user(numeric_id)
    allowed = int(urec.get('max_configs') if urec else app.bot.per_user_limit)
    if used >= allowed:
        logger.info("limit_reached user_id=%s used=%s allowed=%s", numeric_id, used, allowed)
        if update.message:
            await update.message.reply_text('شما به محدودیت ساخت رسیدید .با ادمین در ارتباط باشید\n@Driven_Under')
        return ConversationHandler.END

    client: ThreeXUIClient = context.application.bot_data['3x']
    try:
        inbounds = await client.list_inbounds()
    except ThreeXUIError:
        logger.exception("list_inbounds failed in create_entry user_id=%s", numeric_id)
        if update.message:
            await update.message.reply_text('خطا در دریافت ورودی‌ها')
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
        logger.warning("no_inbounds user_id=%s", numeric_id)
        if update.message:
            await update.message.reply_text('هیچ ورودی‌ای در دسترس نیست.')
        return ConversationHandler.END
    if update.message:
        await update.message.reply_text('یک ورودی را انتخاب کنید:', reply_markup=InlineKeyboardMarkup(buttons))
    return WAIT_INBOUND_SELECT


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
    logger.info("inbound_selected user_id=%s inbound_id=%s is_test=%s", numeric_id_str, inbound_id_str, context.user_data.get('is_test'))
    # If test flow, skip volume and go to username directly
    if int(context.user_data.get('is_test', 0)) == 1:
        await query.edit_message_text('نام کانفیگ را وارد کنید (حروف، عدد، -):')
        logger.info("next_state=WAIT_USERNAME (test) user_id=%s", numeric_id_str)
        return WAIT_USERNAME
    await query.edit_message_text('حجم را بر حسب GB وارد کنید (مثلاً 10)')
    logger.info("next_state=WAIT_VOLUME_GB user_id=%s", numeric_id_str)
    return WAIT_VOLUME_GB


async def on_volume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.message.text if update.message else '').strip()
    gb = _parse_float_from_text(raw)
    if gb is None or gb <= 0:
        logger.info("invalid_volume user_id=%s raw=%r", getattr(update.effective_user, 'id', None), raw)
        if update.message:
            await update.message.reply_text('عدد نامعتبر. مقدار مثبت وارد کنید.')
        return WAIT_VOLUME_GB
    context.user_data['total_gb'] = gb
    # Set default expiry days and go ask username directly
    context.user_data['expiry_days'] = DEFAULT_EXPIRY_DAYS
    if update.message:
        await update.message.reply_text('نام کانفیگ را وارد کنید (حروف، عدد، -):')
    logger.info("next_state=WAIT_USERNAME user_id=%s volume_gb=%.2f", getattr(update.effective_user, 'id', None), gb)
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
        logger.info("invalid_username user_id=%s raw=%r", getattr(update.effective_user, 'id', None), raw_username)
        if update.message:
            await update.message.reply_text('Username is too short. Send another (letters, digits, -).')
        return WAIT_USERNAME
    # validate allowed chars: letters, digits, -
    if not re.fullmatch(r'[A-Za-z0-9\-]+', username):
        logger.info("username_bad_chars user_id=%s cleaned=%r", getattr(update.effective_user, 'id', None), username)
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
            logger.info("username_duplicate user_id=%s username=%s", getattr(update.effective_user, 'id', None), username)
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
            logger.info("add_client_duplicate user_id=%s username=%s", getattr(update.effective_user, 'id', None), username)
            if update.message:
                await update.message.reply_text('این نام کاربری قبلاً وجود دارد. نام دیگری بفرستید.')
            return WAIT_USERNAME
        logger.exception("add_client_failed user_id=%s inbound_id=%s", getattr(update.effective_user, 'id', None), context.user_data.get('inbound_id'))
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
        int(context.user_data.get('is_test', 0)),
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
    try:
        override_port = await db_get_inbound_port(int(inbound_id))
        if override_port:
            v_port = str(override_port)
    except Exception:
        pass
    v_host = os.getenv('VLESS_HOST')
    v_port = os.getenv('VLESS_PORT')
    try:
        override_port = await db_get_inbound_port(int(inbound_id))
        if override_port:
            v_port = str(override_port)
    except Exception:
        pass
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

    # Build Persian summary message
    try:
        # expiry date: use API if present otherwise now + days
        exp_ms = 0
        if isinstance(resp, dict):
            exp_ms = int(resp.get('expiryTime') or 0)
        if exp_ms > 0:
            dt_exp = datetime.fromtimestamp(exp_ms / 1000.0, tz=timezone.utc).astimezone()
        else:
            dt_exp = datetime.now(tz=timezone.utc).astimezone() + timedelta(days=int(expiry_days))
        exp_str = dt_exp.strftime('%Y-%m-%d')
    except Exception:
        exp_str = ''

    # remaining equals total at creation time
    try:
        total_gb_f = float(total_gb)
        remain_disp = f"{int(total_gb_f)} GB" if abs(total_gb_f - int(total_gb_f)) < 1e-9 else f"{total_gb_f:.2f} GB"
    except Exception:
        remain_disp = f"{total_gb} GB"

    # server label (do not show host fallback)
    flag = (await get_setting('CONFIG_SERVER_FLAG')) or os.getenv('CONFIG_SERVER_FLAG', '')
    sname = (await get_setting('CONFIG_SERVER_NAME')) or os.getenv('CONFIG_SERVER_NAME', '')
    server_line = f"\n📡 {flag} سرور کانفیگ : {sname}".rstrip() if (flag or sname) else ''

    summary = (
        f"با موفقیت ایجاد شد 💥\n"
        f"📅 تاریخ انقضا :{exp_str}\n"
        f"✏️ نام کانفیگ : {username} \n"
        f"🔋 حجم باقی مانده : {remain_disp} \n"
        f"🚶‍♂️ محدودیت کاربر : ندارد"
        f"{server_line}"
    )

    if update.message:
        try:
            await update.message.reply_text(summary)
        except TimedOut:
            try:
                await asyncio.sleep(1)
                await update.message.reply_text(summary)
            except Exception:
                pass
        if cfg_lines:
            try:
                await update.message.reply_text(cfg_lines[0])
            except TimedOut:
                try:
                    await asyncio.sleep(1)
                    await update.message.reply_text(cfg_lines[0])
                except Exception:
                    pass

        # Instruction message with download links
        instruction_html = (
            "توجه : تحت هیچ عنوان اسم کانفیگ رو تغییر ندهید چون در صورت تغییر، گارانتی کانفیگ توسط شما باطل می‌شود🚨\n\n"
            "📱 برای استفاده در اندروید از برنامه های :\n"
            "- <a href='https://play.google.com/store/apps/details?id=com.v2ray.ang'>V2RayNG</a>\n"
            "- <a href='https://play.google.com/store/apps/details?id=io.nekohasekai.sfa'>V2Box</a>\n"
            "- <a href='https://play.google.com/store/apps/details?id=com.napsternetlabs.napsternetv'>NpV Tunnel</a>\n\n"
            "📱 و در گوشی های آیفون از برنامه های :\n\n"
            "- <a href='https://apps.apple.com/app/v2box-shadowsocks-v2ray/id6446814690'>V2Box</a>\n"
            "- <a href='https://apps.apple.com/app/napsternetv/id1629465476'>NpV Tunnel</a>\n\n"
            "و برای استفاده در ویندوز از <a href='https://www.google.com/search?q=v2rayn+download'>V2rayN</a> 💻 استفاده کنید."
        )
        try:
            await update.message.reply_text(instruction_html, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except TimedOut:
            try:
                await asyncio.sleep(1)
                await update.message.reply_text(instruction_html, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception:
                pass
    return ConversationHandler.END


async def myconfigs_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Show this user's configs without asking numeric id
    rows = await get_configs_by_numeric_id(update.effective_user.id)
    if not rows:
        await update.message.reply_text('هیچ سرویسی برای شما ثبت نشده است.')
        return ConversationHandler.END
    lines: List[str] = []
    for r in rows[:10]:
        lines.append(f"ورودی {r['inbound_id']} | نام {r['client_identifier']} | تاریخ {r['created_at']}")
    await update.message.reply_text('\n'.join(lines))
    return ConversationHandler.END


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


async def on_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or '').strip()
    if text == 'استعلام سرویس':
        await mystats_entry(update, context)
        return
    # 'ساخت کانفیگ' و 'کانفیگ تست' هر دو به create_entry می‌روند؛ تست در create_entry تشخیص داده می‌شود
    if text in ('ساخت کانفیگ', 'کانفیگ تست'):
        # allow only once per Telegram user for test
        if text == 'کانفیگ تست':
            used_tests = await count_test_configs_by_telegram_user(update.effective_user.id)
            if used_tests >= 1:
                await update.message.reply_text('شما قبلاً کانفیگ تست دریافت کرده‌اید.')
                return
        await create_entry(update, context)
        return


async def mystats_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text('نام کاربری/ایمیل کانفیگ را ارسال کنید تا وضعیت ترافیک نمایش داده شود.')
    return WAIT_STATS_USERNAME


async def on_stats_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = (update.message.text or '').strip() if update.message else ''
    if not username:
        if update.message:
            await update.message.reply_text('Please send a valid username/email.')
        return WAIT_STATS_USERNAME
    client: ThreeXUIClient = context.application.bot_data['3x']
    # Attempt to fetch stats; if expiryTime missing afterwards, we will fallback to DB record
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

        # expiryTime may be missing in traffics; fallback to options
        exp_ms = int(data.get('expiryTime') or data.get('expireTime') or 0)
        if exp_ms <= 0:
            try:
                opts = await client.get_client_options(email=username)
                src = opts.get('obj') if isinstance(opts, dict) and 'obj' in opts else opts
                exp_ms = int((src or {}).get('expiryTime') or (src or {}).get('expireTime') or 0)
            except Exception:
                exp_ms = 0
        exp_str = 'نامشخص'
        days_left = 'نامشخص'
        status = 'فعال' if bool(data.get('enable', True)) else 'غیرفعال'
        if exp_ms <= 0:
            # Fallback 2: derive from our DB record created_at + expiry_days
            try:
                rec = await get_latest_config_by_identifier(username)
                if rec:
                    from datetime import datetime as _dt
                    created_iso = rec.get('created_at')
                    exp_days = int(rec.get('expiry_days') or 0)
                    if created_iso and exp_days > 0:
                        try:
                            created_dt = _dt.fromisoformat(created_iso)
                        except Exception:
                            created_dt = _dt.strptime(created_iso.split('.')[0], '%Y-%m-%d %H:%M:%S')
                        exp_dt = created_dt + timedelta(days=exp_days)
                        exp_ms = int(exp_dt.timestamp() * 1000)
            except Exception:
                pass

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


def _is_admin(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    app = context.application.bot_data['appcfg']
    admin_ids = set(app.bot.admin_numeric_ids or [])
    return user_id in admin_ids


async def admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        if update.message:
            await update.message.reply_text('Unauthorized.')
        return
    kv = await get_all_settings()
    lines = [f"{k}={v}" for k, v in kv.items()]
    if not lines:
        lines = ['<empty>']
    await update.message.reply_text('\n'.join(lines))


async def set_default_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        if update.message:
            await update.message.reply_text('Unauthorized.')
        return
    parts = (update.message.text or '').strip().split()
    if len(parts) != 2:
        await update.message.reply_text('Usage: /set_default_expiry <days>')
        return
    try:
        days = int(parts[1])
        if days <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text('Invalid days.')
        return
    await set_setting('default_expiry_days', str(days))
    await update.message.reply_text(f'Default expiry set to {days} days.')


async def set_vless(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        if update.message:
            await update.message.reply_text('Unauthorized.')
        return
    # format: /set_vless host=... port=... type=... path=... sni=... header=http security=none suffix=-...
    text = (update.message.text or '')
    items = text.split()[1:]
    if not items:
        await update.message.reply_text('Usage: /set_vless host=.. port=.. [type=.. path=.. sni=.. header=.. security=.. suffix=..]')
        return
    allowed = {'host':'VLESS_HOST','port':'VLESS_PORT','type':'VLESS_TYPE','path':'VLESS_PATH','sni':'VLESS_SNI','header':'VLESS_HEADER_TYPE','security':'VLESS_SECURITY','suffix':'CONFIG_REMARK_SUFFIX'}
    changes = []
    for it in items:
        if '=' not in it:
            continue
        k, v = it.split('=', 1)
        if k in allowed:
            os.environ[allowed[k]] = v
            changes.append(f"{allowed[k]}={v}")
    if not changes:
        await update.message.reply_text('No valid keys. Allowed: ' + ','.join(allowed.keys()))
        return
    await update.message.reply_text('Updated: ' + ', '.join(changes))


async def set_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        if update.message:
            await update.message.reply_text('Unauthorized.')
        return
    # Usage: /set_server <host> [port] [flag] [name...]
    parts = (update.message.text or '').strip().split()
    if len(parts) < 2:
        await update.message.reply_text('Usage: /set_server <host> [port] [flag] [name...]')
        return
    host = parts[1]
    port = None
    flag = ''
    name = ''
    if len(parts) >= 3 and parts[2].isdigit():
        port = parts[2]
    if len(parts) >= 4:
        flag = parts[3]
    if len(parts) >= 5:
        name = ' '.join(parts[4:])
    os.environ['VLESS_HOST'] = host
    await set_setting('VLESS_HOST', host)
    changed = [f'HOST={host}']
    if port:
        os.environ['VLESS_PORT'] = port
        await set_setting('VLESS_PORT', port)
        changed.append(f'PORT={port}')
    if flag:
        os.environ['CONFIG_SERVER_FLAG'] = flag
        await set_setting('CONFIG_SERVER_FLAG', flag)
        changed.append(f'FLAG={flag}')
    if name:
        os.environ['CONFIG_SERVER_NAME'] = name
        await set_setting('CONFIG_SERVER_NAME', name)
        changed.append(f'NAME={name}')
    await update.message.reply_text('Server config updated: ' + ', '.join(changed))


async def set_inbound_port(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        if update.message:
            await update.message.reply_text('Unauthorized.')
        return
    parts = (update.message.text or '').strip().split()
    if len(parts) != 3:
        await update.message.reply_text('Usage: /set_inbound_port <inbound_id> <port>')
        return
    try:
        inbound_id = int(parts[1]); port = int(parts[2])
        if port <= 0 or port > 65535:
            raise ValueError
    except Exception:
        await update.message.reply_text('Provide valid inbound id and port (1-65535).')
        return
    await db_set_inbound_port(inbound_id, port)
    await update.message.reply_text(f'Inbound {inbound_id} port overridden to {port}.')


async def unset_inbound_port(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        if update.message:
            await update.message.reply_text('Unauthorized.')
        return
    parts = (update.message.text or '').strip().split()
    if len(parts) != 2:
        await update.message.reply_text('Usage: /unset_inbound_port <inbound_id>')
        return
    try:
        inbound_id = int(parts[1])
    except Exception:
        await update.message.reply_text('Provide a valid inbound id.')
        return
    await db_unset_inbound_port(inbound_id)
    await update.message.reply_text(f'Inbound {inbound_id} port override removed.')
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


async def sets_server_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
	if not _is_admin(context, update.effective_user.id):
		if update.message:
			await update.message.reply_text('Unauthorized.')
		return
	# Usage: /sets <name...> <flag>
	# Practical simple rule: آخرین بخش را به عنوان پرچم می‌گیریم، بقیه را نام سرور
	parts = (update.message.text or '').strip().split()
	if len(parts) < 2:
		await update.message.reply_text('Usage: /sets <name...> <flag>')
		return
	# remove command
	parts = parts[1:]
	flag = parts[-1]
	name = ' '.join(parts[:-1])
	os.environ['CONFIG_SERVER_FLAG'] = flag
	os.environ['CONFIG_SERVER_NAME'] = name
	await set_setting('CONFIG_SERVER_FLAG', flag)
	await set_setting('CONFIG_SERVER_NAME', name)
	await update.message.reply_text(f'Updated server label: name="{name}", flag="{flag}"')


async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
	if not _is_admin(context, update.effective_user.id):
		if update.message:
			await update.message.reply_text('Unauthorized.')
		return
	text = (
		"<b>راهنمای دستورات مدیریت ربات</b>\n\n"
		"<b>۱) محدودیت ساخت کانفیگ</b>\n"
		"<code>/setlimit &lt;numeric_id&gt; &lt;limit&gt;</code>\n"
		"مثال: <code>/setlimit 6839887159 10</code>\n\n"
		"<b>۲) تنظیمات پیش‌فرض مدت اعتبار</b>\n"
		"<code>/set_default_expiry &lt;days&gt;</code> — پیش‌فرض روزهای اعتبار ساخت\n"
		"مثال: <code>/set_default_expiry 30</code>\n\n"
		"<b>۳) تنظیم پارامترهای ساخت vless</b>\n"
		"<code>/set_vless host=... port=... [type=.. path=.. sni=.. header=.. security=.. suffix=..]</code>\n"
		"مثال: <code>/set_vless host=shop2.mhzshop.xyz port=12836 type=tcp path=/ header=http security=none</code>\n\n"
		"<b>۴) تعیین پورت اختصاصی ورودی</b>\n"
		"<code>/set_inbound_port &lt;inbound_id&gt; &lt;port&gt;</code> — فقط روی همان ورودی اعمال می‌شود\n"
		"<code>/unset_inbound_port &lt;inbound_id&gt;</code> — حذف تنظیم پورت ورودی\n"
		"مثال: <code>/set_inbound_port 18 12836</code>\n\n"
		"<b>۵) برچسب نمایش سرور</b>\n"
		"<code>/sets &lt;name...&gt; &lt;flag&gt;</code> — فقط نام/پرچم را در پیام خلاصه نشان می‌دهد\n"
		"مثال: <code>/sets آلمان 🇩🇪</code>\n\n"
		"<b>۶) تنظیم سرور کامل</b>\n"
		"<code>/set_server &lt;host&gt; [port] [flag] [name...]</code>\n"
		"مثال: <code>/set_server shop2.mhzshop.xyz 12836 🇩🇪 آلمان</code>\n\n"
		"<b>۷) خروجی کاربران</b>\n"
		"<code>/export_users</code> — فایل CSV ستونی: هر ستون یک numeric_id؛ ردیف‌ها: نام کانفیگ (حجم GB)\n\n"
		"<b>۸) مشاهده تنظیمات</b>\n"
		"<code>/admin_settings</code> — نمایش کلیدهای تنظیمات\n"
		"لاگ دیباگ: اجرای ربات با <code>BOT_LOG_LEVEL=DEBUG</code>\n"
	)
	try:
		await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
	except Exception:
		await update.message.reply_text('Help send failed.')


async def export_user_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(context, update.effective_user.id):
        if update.message:
            await update.message.reply_text('Unauthorized.')
        return
    # New layout: each numeric_id becomes a column; under it list of config names, alongside sizes
    configs = await get_all_configs_non_test()
    if not configs:
        await update.message.reply_text('داده‌ای برای خروجی وجود ندارد.')
        return
    # group by numeric_id
    from collections import defaultdict
    grouped = defaultdict(list)
    for r in configs:
        total_gb = (int(r.get('total_bytes') or 0)) / (1024*1024*1024)
        grouped[r['numeric_id']].append((r['client_identifier'], total_gb))
    # build CSV pivot-like: header = numeric ids; rows = max len list; cells = "name (size GB)"
    import io, csv
    buf = io.StringIO()
    writer = csv.writer(buf)
    headers = list(grouped.keys())
    headers.sort()
    writer.writerow(headers)
    max_len = max(len(v) for v in grouped.values())
    for i in range(max_len):
        row = []
        for nid in headers:
            items = grouped[nid]
            if i < len(items):
                name, size_gb = items[i]
                cell = f"{name} ({size_gb:.2f} GB)"
            else:
                cell = ''
            row.append(cell)
        writer.writerow(row)
    data = buf.getvalue().encode('utf-8-sig')
    buf.close()
    try:
        await update.message.reply_document(document=data, filename='user_configs_pivot.csv', caption='گزارش ستونی کاربران (بدون کانفیگ‌های تست)')
    except Exception:
        await update.message.reply_text('ارسال فایل گزارش ناموفق بود.')


def run() -> None:
    appcfg = load_app_config()
    # Logging config
    log_level = os.getenv('BOT_LOG_LEVEL', 'INFO').upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s'
    )
    # Ensure an event loop exists (fixes Python 3.10 get_event_loop error)
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    # Ensure database exists
    loop.run_until_complete(init_db())

    # Build Telegram application
    # Build application with default request backend
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
        entry_points=[
            CommandHandler('create', create_entry),
            MessageHandler(filters.Regex('^ساخت کانفیگ$'), create_entry),
            MessageHandler(filters.Regex('^کانفیگ تست$'), create_entry),
        ],
        states={
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

    # Conversation for viewing stats (supports both /mystats and menu button)
    conv_stats = ConversationHandler(
        entry_points=[
            CommandHandler('mystats', mystats_entry),
            MessageHandler(filters.Regex('^استعلام سرویس$'), mystats_entry),
        ],
        states={
            WAIT_STATS_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_stats_username)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('inbounds', cmd_inbounds))
    application.add_handler(CommandHandler('setlimit', setlimit))
    application.add_handler(CommandHandler('admin_settings', admin_settings))
    application.add_handler(CommandHandler('set_default_expiry', set_default_expiry))
    application.add_handler(CommandHandler('set_vless', set_vless))
    application.add_handler(CommandHandler('set_inbound_port', set_inbound_port))
    application.add_handler(CommandHandler('unset_inbound_port', unset_inbound_port))
    application.add_handler(CommandHandler('set_server', set_server))
    application.add_handler(CommandHandler('sets', sets_server_label))
    application.add_handler(CommandHandler('help', admin_help))
    application.add_handler(CommandHandler('export_users', export_user_stats))
    # No direct handler for inquiry; handled by conv_stats entry_points
    application.add_handler(conv_create)
    application.add_handler(conv_list)
    application.add_handler(conv_stats)

    # Global error handler to avoid noisy logs on transient network issues
    async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        # Swallow common network timeouts silently
        if isinstance(err, TimedOut):
            return
        if isinstance(err, httpx.TimeoutException) or isinstance(err, httpx.ConnectTimeout):
            return
        # Fallback: print minimal log
        try:
            print(f"Unhandled error: {err}")
        except Exception:
            pass

    application.add_error_handler(_on_error)

    # Blocking call - handles its own event loop internally
    application.run_polling()

    
