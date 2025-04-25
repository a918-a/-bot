import os
import json
import re
import random
import logging
from pathlib import Path
from datetime import datetime, timedelta
from threading import Lock
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    InlineQueryResultArticle,
    InputTextMessageContent
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    CallbackContext,
    InlineQueryHandler,
    JobQueue
)

# ===== 配置区 =====
TOKEN = "7101281684:AAGYq7QBoq-sZAQMys6bGjHSam4YAUktfmE"  # ⚠️替换为你的机器人Token
ADMIN_ID = 7606364039  # ⚠️替换为你的管理员ID
DATA_FILE = Path("user_data.json")
TRON_ADDRESS = "充值请联系客服"
RED_PACKET_MIN_AMOUNT = 100
RED_PACKET_MAX_COUNT = 1000
REBATE_RATE = 0.015  # 返水比例1.5%
# =================
RED_PACKET_STATES = {
    "SET_AMOUNT": 1,
    "SET_COUNT": 2,
    "CONFIRMING": 3
}
# ===== 日志配置 =====
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== 线程安全文件锁 =====
FILE_LOCK = Lock()

# ===== 数据管理 =====
def load_user_data():
    default_data = {
        "balance": {},
        "total_bet": {},
        "logs": [],
        "bets": {},
        "bet_history": {},
        "pending_rolls": {},
        "history": [],
        "in_progress": {},
        "red_packets": {},
        "user_red_packets": {},
        "global_round": {
            "last_date": datetime.now().strftime("%Y%m%d"),
            "daily_counter": 0
        },
        "transaction_id": 1  # 新增交易编号字段
    }
    with FILE_LOCK:
        if DATA_FILE.exists():
            try:
                with open(DATA_FILE, "r", encoding='utf-8') as f:
                    data = json.load(f)
                # 兼容旧数据：如果无transaction_id则初始化
                if "transaction_id" not in data:
                    data["transaction_id"] = 1
                return data
            except Exception as e:
                logger.error(f"加载数据失败: {str(e)}")
                return default_data
        return default_data

def save_user_data(data):
    with FILE_LOCK:
        try:
            with open(DATA_FILE, "w", encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存数据失败: {str(e)}")

def add_log(action, user_id=None, amount=None, target_user=None):
    data = load_user_data()
    log_entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "admin": user_id,
        "target": target_user,
        "amount": amount
    }
    data["logs"].append(log_entry)
    save_user_data(data)

# ===== 赔率配置 =====
ODDS = {
    '大': 2, '小': 2, '单': 2, '双': 2,
    '大单': 4.5, '小单': 4.5, '大双': 4.5, '小双': 4.5,
    '豹子': 32, '顺子': 8, '对子': 2.1,
    '豹1': 200, '豹2': 200, '豹3': 200,
    '豹4': 200, '豹5': 200, '豹6': 200,
    '定位胆4': 58, '定位胆5': 28, '定位胆6': 16,
    '定位胆7': 12, '定位胆8': 8, '定位胆9': 7,
    '定位胆10': 7, '定位胆11': 6, '定位胆12': 6,
    '定位胆13': 8, '定位胆14': 12, '定位胆15': 16,
    '定位胆16': 28, '定位胆17': 58
}

# ===== 游戏核心逻辑 =====
def parse_bet(message: str):
    bet_details = {}
    message = message.lower().replace(' ', '')  # 移除所有空格

    patterns = [
        # 优先级1: 通配豹子 (豹子100 或 bz100)
        (r'^(通配豹子|bz)(\d+)$', '豹子'),  
        # 优先级2: 具体豹子 (豹��1 100 或 bz1 100)
        (r'^(豹子|bz)(1|2|3|4|5|6)(\d+)$', lambda m: f'豹{m.group(2)}'),
        # 组合项 (大单100)
        (r'^(大单|dd)(\d+)', '大单'),
        (r'^(大双|ds)(\d+)', '大双'),
        (r'^(小单|xd)(\d+)', '小单'),
        (r'^(小双|xs)(\d+)', '小双'),
        # 基础项 (大100)
        (r'^(大|da)(\d+)', '大'),
        (r'^(小|x)(\d+)', '小'),
        (r'^(单|dan)(\d+)', '单'),
        (r'^(双|s)(\d+)', '双'),
        # 其他类型
        (r'^(顺子|sz)(\d+)', '顺子'),
        (r'^(对子|dz)(\d+)', '对子'),
        (r'^(定位胆|dwd)(4|5|6|7|8|9|10|11|12|13|14|15|16|17)(\d+)', lambda m: f'定位胆{m.group(2)}'),
        (r'^(4|5|6|7|8|9|10|11|12|13|14|15|16|17)y(\d+)', lambda m: f'定位胆{m.group(1)}'),
        (r'^(\d+)(大|小|单|双)', lambda m: f"{m.group(2)}"),
    ]

    for pattern, key in patterns:
        for match in re.finditer(pattern, message):
            try:
                if callable(key):
                    bet_type = key(match)
                    # 提取具体豹子/定位胆的金额
                    if '豹' in bet_type:
                        amount_str = match.group(3)  # 第3组是金额
                    else:
                        amount_str = match.group(2)  # 其他类型取第2组
                else:
                    bet_type = key
                    amount_str = match.group(2)  # 通配豹子取第2组

                amount = int(amount_str)
                if amount <= 0:
                    raise ValueError("金额必须大于0")

                bet_details[bet_type] = bet_details.get(bet_type, 0) + amount
                message = message.replace(match.group(0), '', 1)  # 移除已解析部分
            except ValueError as ve:
                logger.warning(f"金额错误: {str(ve)}")
                continue
            except Exception as e:
                logger.warning(f"解析失败: {str(e)}")
                continue

    return bet_details if bet_details else None

def calculate_result(dice_values):
    total = sum(dice_values)
    return {
        'values': dice_values,
        'total': total,
        'is_big': total > 10,
        'is_small': total <= 10,
        'is_odd': total % 2 != 0,
        'is_even': total % 2 == 0,
        'is_triple': len(set(dice_values)) == 1,
        'is_straight': sorted(dice_values) in [[1,2,3], [2,3,4], [3,4,5], [4,5,6]],
        'is_pair': len(set(dice_values)) == 2,
        'is_he': dice_values[0] == dice_values[2],
        'triple_num': dice_values[0] if len(set(dice_values)) == 1 else None
    }

def calculate_winnings(bet_details, result):
    winnings = 0
    winning_bets = []

    if result['is_triple']:
        triple_num = result['triple_num']
        for bet_type, amount in bet_details.items():
            if bet_type.startswith('豹'):
                if bet_type == '豹子':
                    winnings += amount * ODDS[bet_type]
                    winning_bets.append(bet_type)
                else:
                    try:
                        num = int(bet_type[1:])
                        if num == triple_num:
                            winnings += amount * ODDS[bet_type]
                            winning_bets.append(bet_type)
                    except ValueError:
                        continue
        return winnings, winning_bets

    if result['is_he']:
        for bet_type, amount in bet_details.items():
            if bet_type in ['大单', '大双', '小单', '小双']:
                continue
            if bet_type == '大' and result['is_big']:
                winnings += amount * ODDS[bet_type]
                winning_bets.append(bet_type)
            elif bet_type == '小' and result['is_small']:
                winnings += amount * ODDS[bet_type]
                winning_bets.append(bet_type)
            elif bet_type == '单' and result['is_odd']:
                winnings += amount * ODDS[bet_type]
                winning_bets.append(bet_type)
            elif bet_type == '双' and result['is_even']:
                winnings += amount * ODDS[bet_type]
                winning_bets.append(bet_type)
        return winnings, winning_bets

    for bet_type, amount in bet_details.items():
        win = False
        if bet_type == '大' and result['is_big']:
            win = True
        elif bet_type == '小' and result['is_small']:
            win = True
        elif bet_type == '单' and result['is_odd']:
            win = True
        elif bet_type == '双' and result['is_even']:
            win = True
        elif bet_type == '大单' and result['is_big'] and result['is_odd']:
            win = True
        elif bet_type == '小单' and result['is_small'] and result['is_odd']:
            win = True
        elif bet_type == '大双' and result['is_big'] and result['is_even']:
            win = True
        elif bet_type == '小双' and result['is_small'] and result['is_even']:
            win = True
        elif bet_type == '顺子' and result['is_straight']:
            win = True
        elif bet_type == '对子' and result['is_pair']:
            win = True
        elif bet_type.startswith('定位胆'):
            try:
                target = int(bet_type[3:])
                if result['total'] == target:
                    win = True
            except ValueError:
                continue

        if win:
            winnings += amount * ODDS.get(bet_type, 0)
            winning_bets.append(bet_type)

    return winnings, winning_bets

# ===== 管理员指令 =====

from datetime import datetime

# 1. 管理员重置所有玩家数据
async def admin_reset_all_data(update: Update, context: CallbackContext):
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("❌ 权限不足")
        return

    data = {
        "balance": {},
        "total_bet": {},
        "logs": [],
        "bets": {},
        "bet_history": {},
        "pending_rolls": {},
        "history": [],
        "in_progress": {},
        "red_packets": {},
        "user_red_packets": {},
        "global_round": {
            "last_date": datetime.now().strftime("%Y%m%d"),
            "daily_counter": 0
        },
        "transaction_id": 1
    }
    save_user_data(data)
    await update.message.reply_text("✅ 已清除所有玩家余额、下注流水、下注记录")

# 2. 管理员给某个用户加款
async def admin_add_balance(update: Update, context: CallbackContext):
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("❌ 权限不足")
        return

    if len(context.args) < 2:
        await update.message.reply_text("❌ 参数不足。用法：/add_balance 用户ID 金额")
        return

    try:
        target_user = int(context.args[0])
        amount = int(context.args[1])
        data = load_user_data()
        current = data['balance'].get(str(target_user), 0)

        # 获取并递增交易编号
        tx_id = data["transaction_id"]
        data["transaction_id"] += 1

        data['balance'][str(target_user)] = current + amount
        save_user_data(data)
        add_log("ADD_BALANCE", update.message.from_user.id, amount, target_user)

        # 发送客户通知
        try:
            await context.bot.send_message(
                chat_id=target_user,
                text=(
                    f"💸 +{amount} USDT\n"
                    f"来自管理员加款\n"
                    f"加款编号：TX{tx_id:06d}\n"
                    f"当前余额：{current + amount} USDT"
                )
            )
        except Exception as e:
            logger.error(f"客户通知发送失败: {str(e)}")

        await update.message.reply_text(
            f"✅ 充值成功\n用户ID：{target_user}\n用户加款：{amount} USDT\n"
            f"当前余额：{current + amount} USDT\n"
            f"操作者：{update.message.from_user.id}\n"
            f"加款编号：TX{tx_id:06d}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ 加款失败：{str(e)}")
        
        # 获取并递增交易编号
        tx_id = data["transaction_id"]
        data["transaction_id"] += 1
        
        data['balance'][str(target_user)] = current + amount
        save_user_data(data)
        add_log("ADD_BALANCE", update.message.from_user.id, amount, target_user)
        
        # 发送客户通知
        try:
            await context.bot.send_message(
                chat_id=target_user,
                text=(
                    f"💵 +{amount} USDT\n"
                    f"来自管理员加款\n"
                    f"加款编号: TX{tx_id:06d}\n"
                    f"当前余额: {current + amount} USDT"
                )
            )
        except Exception as e:
            logger.error(f"客户通知发送失败: {str(e)}")

        await update.message.reply_text(
            f"✅ 充值成功\n━━━━━━━━━━━━\n"
            f"用户ID: {target_user}\n"
            f"充值金额: +{amount} USDT\n"
            f"当前余额: {current + amount} USDT\n"
            f"操作员: {update.message.from_user.id}\n"
            f"交易编号: TX{tx_id:06d}"
        )
    except Exception as e:
        logger.error(f"管理员充值失败: {str(e)}")
        await update.message.reply_text("⚠️ 格式错误\n使用: /add 用户ID 金额")

async def admin_set(update: Update, context: CallbackContext):
    try:
        if update.message.from_user.id != ADMIN_ID:
            await update.message.reply_text("❌ 权限不足")
            return

        target_user = int(context.args[0])
        amount = int(context.args[1])
        data = load_user_data()
        old_balance = data['balance'].get(str(target_user), 0)
        data['balance'][str(target_user)] = amount
        save_user_data(data)
        add_log("SET_BALANCE", update.message.from_user.id, amount, target_user)
        await update.message.reply_text(
            f"✅ 余额设置成功\n━━━━━━━━━━━━\n"
            f"用户ID: {target_user}\n"
            f"原余额: {old_balance} USDT\n"
            f"新余额: {amount} USDT\n"
            f"操作员: {update.message.from_user.id}"
        )
    except Exception as e:
        logger.error(f"设置余额失败: {str(e)}")
        await update.message.reply_text("⚠️ 格式错误\n使用: /set 用户ID 金额")

async def admin_list(update: Update, context: CallbackContext):
    try:
        if update.message.from_user.id != ADMIN_ID:
            await update.message.reply_text("❌ 权限不足")
            return

        data = load_user_data()
        if not data['balance']:
            await update.message.reply_text("暂无用户数据")
            return

        msg = ["📊 用户余额\n━━━━━━━━━━━━"]
        for uid, bal in data['balance'].items():
            msg.append(f"ID: {uid} | 余额: {bal} USDT")
        await update.message.reply_text("\n".join(msg[:20]))
    except Exception as e:
        logger.error(f"查询用户列表失败: {str(e)}")

async def admin_logs(update: Update, context: CallbackContext):
    try:
        if update.message.from_user.id != ADMIN_ID:
            await update.message.reply_text("❌ 权限不足")
            return

        data = load_user_data()
        if not data['logs']:
            await update.message.reply_text("暂无日志")
            return

        msg = ["📜 操作日志(最近10条)\n━━━━━━━━━━━━"]
        for log in data['logs'][-10:]:
            msg.append(
                f"时间: {log['time']}\n"
                f"操作: {log['action']}\n"
                f"目标: {log['target']}\n"
                f"金额: {log['amount']} USDT\n"
                f"━━━━━━━━━━━━"
            )
        await update.message.reply_text("\n".join(msg))
    except Exception as e:
        logger.error(f"查询日志失败: {str(e)}")

async def admin_invite(update: Update, context: CallbackContext):
    try:
        if update.message.from_user.id != ADMIN_ID:
            await update.message.reply_text("❌ 权限不足")
            return

        chat_id = update.message.chat.id
        invite_link = await context.bot.create_chat_invite_link(
            chat_id,
            member_limit=1,
            creates_join_request=True
        )
        await update.message.reply_text(
            f"📩 邀请链接:\n{invite_link.invite_link}\n\n"
            "• 有效期：永久\n"
            "• 使用次数：无限制"
        )
    except Exception as e:
        logger.error(f"生成邀请链接失败: {str(e)}")
        await update.message.reply_text(f"⚠️ 生成链接失败: {str(e)}")

async def handle_admin_commands(update: Update, context: CallbackContext):
    try:
        if update.message.from_user.id != ADMIN_ID:
            return
        if update.message.chat.type not in ["group", "supergroup"]:
            return
        if not update.message.reply_to_message:
            return

        target_user = update.message.reply_to_message.from_user.id
        command = update.message.text.strip()

        if not re.fullmatch(r'^[+-]\d+$', command):
            return

        amount = int(command)
        data = load_user_data()
        current = data['balance'].get(str(target_user), 0)

        if amount > 0:
            action_type = "ADD_BALANCE"
            data['balance'][str(target_user)] = current + abs(amount)
        else:
            action_type = "SUB_BALANCE"
            if current < abs(amount):
                await update.message.reply_text(f"❌ 余额不足 | 用户ID: {target_user}")
                return
            data['balance'][str(target_user)] = current - abs(amount)

        save_user_data(data)
        add_log(action_type, ADMIN_ID, abs(amount), target_user)

        if amount > 0:
            msg = (
                f"✅ 充值成功\n━━━━━━━━━━━━\n"
                f"用户ID: {target_user}\n"
                f"充值金额: +{abs(amount)} USDT\n"
                f"当前余额: {data['balance'][str(target_user)]} USDT"
            )
        else:
            msg = (
                f"✅ 提现成功\n━━━━━━━━━━━━\n"
                f"用户ID: {target_user}\n"
                f"提现金额: -{abs(amount)} USDT\n"
                f"当前余额: {data['balance'][str(target_user)]} USDT"
            )
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"管理员快捷操作失败: {str(e)}")

# ===== 用户功能 =====
async def start(update: Update, context: CallbackContext) -> None:
    try:
        user_id = update.effective_user.id
        data = load_user_data()

        if str(user_id) not in data['balance']:
            data['balance'][str(user_id)] = 0
            data['total_bet'][str(user_id)] = 0
            save_user_data(data)

        keyboard = [
            [InlineKeyboardButton("💰 充值", callback_data='deposit'),
             InlineKeyboardButton("💸 提现", callback_data='withdraw')],
            [InlineKeyboardButton("💳 余额", callback_data='check_balance'),
             InlineKeyboardButton("🧧 发红包", callback_data='send_red_packet')],
            [InlineKeyboardButton("📦 我的红包", callback_data='my_packets'),
             InlineKeyboardButton("🔄 反水", callback_data='rebate')],
            [InlineKeyboardButton("📜 下注记录", callback_data='bet_history'),
             InlineKeyboardButton("📊 查看总流水", callback_data='total_stats'),
             InlineKeyboardButton("📖 帮助", callback_data='help')]
        ]

        text = (
            f"🎲 极速快三\n━━━━━━━━━━━━\n"
            f"ID: {user_id}\n"
            f"余额: {data['balance'][str(user_id)]} USDT\n"
            f"━━━━━━━━━━━━\n"
            f"✅ TRC20自动充值自动到账\n"
            f"✅ 采用TelegRam官方骰子公平公正公开"
        )

        # 判断是命令还是按钮
        if update.message:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"启动命令失败: {str(e)}")

async def private_text_handler(update: Update, context: CallbackContext):
    if 'red_packet' in context.user_data:
        await handle_red_packet_input(update, context)
    else:
        await place_bet(update, context)

async def place_bet(update: Update, context: CallbackContext) -> None:
    try:
        if update.message.chat.type != "private":
            return

        user_id = update.message.from_user.id
        data = load_user_data()

        if data['in_progress'].get(str(user_id), False):
            await update.message.reply_text("⏳ 请先完成当前对局")
            return

        bet_details = parse_bet(update.message.text)
        if not bet_details:
            await update.message.reply_text("⚠️ 下注格式错误\n示例：大单100 豹子50 定位胆4 10")
            return

        total_bet = sum(bet_details.values())
        balance = data['balance'].get(str(user_id), 0)

        if balance < total_bet:
            await update.message.reply_text(f"❌ 余额不足\n当前余额: {balance} USDT\n需: {total_bet} USDT")
            return

        # 记录下注历史
        current_date = datetime.now().strftime("%Y%m%d")
        if data['global_round']['last_date'] != current_date:
            data['global_round']['last_date'] = current_date
            data['global_round']['daily_counter'] = 0
        data['global_round']['daily_counter'] += 1
        round_id = f"{current_date}{data['global_round']['daily_counter']:03d}"

        user_id_str = str(user_id)
        history_entry = {
            "round_id": round_id,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "bet_details": bet_details,
            "total_amount": total_bet,
            "result": "待开奖"
        }

        if user_id_str not in data['bet_history']:
            data['bet_history'][user_id_str] = []
        data['bet_history'][user_id_str].append(history_entry)
        data['bet_history'][user_id_str] = data['bet_history'][user_id_str][-10:]

        # 更新数据
        data['total_bet'][user_id_str] = data['total_bet'].get(user_id_str, 0) + total_bet
        data['in_progress'][user_id_str] = True
        data['balance'][user_id_str] = balance - total_bet
        data['bets'][user_id_str] = bet_details
        save_user_data(data)

        bet_list = "\n".join([f"• {k}: {v} USDT" for k, v in bet_details.items()])

        await update.message.reply_text(
            f"🎯 下注成功\n━━━━━━━━━━━━\n"
            f"下注内容:\n{bet_list}\n"
            f"总下注: {total_bet} USDT\n"
            f"剩余: {data['balance'][user_id_str]} USDT\n"
            f"请选择开奖方式:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎰 机器帮你摇", callback_data='roll_machine')],
                [InlineKeyboardButton("👋 老子自己摇", callback_data='roll_user')]
            ]))
    except Exception as e:
        logger.error(f"下注失败: {str(e)}")
        await update.message.reply_text("⚠️ 下注处理出错，请稍后再试")

async def handle_dice_result(update: Update, context: CallbackContext):
    try:
        message = update.message
        if not message or not message.dice:
            return

        user_id = message.from_user.id
        user_id_str = str(user_id)
        data = load_user_data()

        # 检查是否本局已被判负作废
        if data.get('forfeit', {}).get(user_id_str, False):
            await message.reply_text("⚠️ 您本局已因转发骰子判负，请重新下注开启新一局。")
            return

        # 检查是否转发骰子
        if hasattr(message, 'forward_date') and message.forward_date:
            # 标记本局作废
            if 'forfeit' not in data:
                data['forfeit'] = {}
            data['forfeit'][user_id_str] = True

            # 清空所有游戏相关状态
            data['pending_rolls'].pop(user_id_str, None)
            data['in_progress'].pop(user_id_str, None)
            data['bets'].pop(user_id_str, None)
            save_user_data(data)

            # 取消超时任务
            job_name = f"roll_timeout_{user_id}"
            for job in context.job_queue.get_jobs_by_name(job_name):
                job.schedule_removal()

            await message.reply_text("⚠️ 检测到转发骰子，本局已作废，金额不予退还。请重新下注开启新一局。")
            return

        # 正常流程：检查是否在等待手摇骰子状态
        if user_id_str not in data['pending_rolls']:
            await message.reply_text("❌ 请先通过菜单选择「手摇骰子」")
            return

        # 记录骰子值
        dice_value = message.dice.value
        data['pending_rolls'][user_id_str].append(dice_value)

        # 检查是否已收集3个骰子
        if len(data['pending_rolls'][user_id_str]) >= 3:
            dice_values = data['pending_rolls'][user_id_str][:3]
            data['pending_rolls'].pop(user_id_str, None)
            save_user_data(data)

            # 取消超时任务
            job_name = f"roll_timeout_{user_id}"
            for job in context.job_queue.get_jobs_by_name(job_name):
                job.schedule_removal()

            # 获取下注数据
            bet_details = data['bets'].get(user_id_str, {})
            result = calculate_result(dice_values)

            # 结算并显示结果
            await show_result(user_id, result, bet_details, data, context, is_machine=False)

            # 清除进行中状态
            data['in_progress'][user_id_str] = False
            save_user_data(data)
        else:
            save_user_data(data)
            await message.reply_text(f"🎲 已记录骰子: {dice_value}，请继续发送骰子")
    except Exception as e:
        import traceback
        print("处理骰子结果异常:", e)
        traceback.print_exc()
        if update.message:
            await update.message.reply_text("‼️ 结算错误，请联系管理员")


async def start_new_game(update: Update, context: CallbackContext):
    # 当用户重新下注/开始新一局时，务必清除forfeit标志
    data = load_user_data()
    user_id_str = str(update.effective_user.id)
    if 'forfeit' in data:
        data['forfeit'].pop(user_id_str, None)
    # ...你的新一局初始化逻辑
    save_user_data(data)
# ===== 红包功能 =====
class RedPacketHandler:
    @staticmethod
    def generate_id():
        return datetime.now().strftime("%Y%m%d%H%M%S%f")

    @staticmethod
    def calculate_amounts(total_amount, count):
        amounts = []
        remaining = total_amount

        for i in range(count - 1):
            max_amount = remaining / (count - len(amounts)) * 2
            amount = round(random.uniform(0.01, max(0.01, max_amount)), 2)
            if amount > remaining - 0.01 * (count - len(amounts) - 1):
                amount = round(remaining - 0.01 * (count - len(amounts) - 1), 2)
            amounts.append(amount)
            remaining -= amount

        amounts.append(round(remaining, 2))
        random.shuffle(amounts)
        return amounts

async def handle_red_packet_creation(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    if query.data == 'send_red_packet':
        context.user_data['red_packet'] = {
            'state': RED_PACKET_STATES["SET_AMOUNT"],
            'id': None,
            'amount': 0.0,
            'count': 0
        }
        await query.edit_message_text(
            "🎁 创建红包\n━━━━━━━━━━━━\n"
            "请输入红包总金额（例如：100.00或100USDT）\n"
            f"⚠️ 最低金额{RED_PACKET_MIN_AMOUNT} USDT",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ 取消", callback_data='cancel_red_packet')]])
        )

    elif query.data == 'cancel_red_packet':
        context.user_data.pop('red_packet', None)
        await query.edit_message_text("❌ 已取消红包创建")

async def handle_red_packet_input(update: Update, context: CallbackContext):
    if 'red_packet' not in context.user_data:
        return

    user_id = str(update.message.from_user.id)
    data = load_user_data()
    state = context.user_data['red_packet']['state']

    try:
        if state == RED_PACKET_STATES["SET_AMOUNT"]:
            amount_str = update.message.text.strip()
            amount_str = amount_str.replace('USDT','').replace('usdt','').replace(' ', '')
            amount = float(amount_str)
            if amount < RED_PACKET_MIN_AMOUNT:
                raise ValueError(f"金额不能低于{RED_PACKET_MIN_AMOUNT} USDT")
            if data['balance'].get(user_id, 0) < amount:
                raise ValueError("余额不足")

            context.user_data['red_packet'].update({
                'amount': amount,
                'state': RED_PACKET_STATES["SET_COUNT"]
            })

            await update.message.reply_text(
                f"✅ 已设置金额: {amount} USDT\n"
                f"请输入红包个数（1-{RED_PACKET_MAX_COUNT})",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ 取消", callback_data='cancel_red_packet')]])
            )

        elif state == RED_PACKET_STATES["SET_COUNT"]:
            count_str = update.message.text.strip().replace('个', '').replace('份','').replace(' ', '')
            count = int(count_str)
            if not 1 <= count <= RED_PACKET_MAX_COUNT:
                raise ValueError("红包个数无效")

            packet_id = RedPacketHandler.generate_id()
            amounts = RedPacketHandler.calculate_amounts(
                context.user_data['red_packet']['amount'],
                count
            )

            context.user_data['red_packet'].update({
                'count': count,
                'id': packet_id,
                'state': RED_PACKET_STATES["CONFIRMING"],
                'amounts': amounts
            })

            confirm_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ 确认发送", callback_data='confirm_red_packet')],
                [InlineKeyboardButton("✏️ 修改金额", callback_data='modify_amount')],
                [InlineKeyboardButton("❌ 取消", callback_data='cancel_red_packet')]
            ])

            await update.message.reply_text(
                f"🎁 红包详情\n━━━━━━━━━━━━\n"
                f"总金额: {context.user_data['red_packet']['amount']} USDT\n"
                f"红包个数: {count}\n"
                f"━━━━━━━━━━━━\n"
                f"当前余额: {data['balance'][user_id]} USDT",
                reply_markup=confirm_keyboard)

    except ValueError as e:
        await update.message.reply_text(f"❌ 错误: {str(e)}")
        context.user_data.pop('red_packet', None)

async def confirm_red_packet(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    if query.data == 'confirm_red_packet':
        user_id = str(query.from_user.id)
        data = load_user_data()
        packet = context.user_data['red_packet']

        data['balance'][user_id] -= packet['amount']
        data['red_packets'][packet['id']] = {
            'creator': user_id,
            'total': packet['amount'],
            'count': packet['count'],
            'remaining': packet['count'],
            'amounts': packet['amounts'],
            'claimed': {},
            'create_time': datetime.now().isoformat(),
            'group_id': None,
            'expire_time': (datetime.now() + timedelta(hours=24)).isoformat()
        }

        data['user_red_packets'][user_id] = data['user_red_packets'].get(user_id, []) + [packet['id']]
        save_user_data(data)

        share_keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "📩 转发到群组",
                switch_inline_query=f"redpacket_{packet['id']}"
            )
        ]])

        await query.edit_message_text(
            f"✅ 红包创建成功！\n"
            f"红包ID: {packet['id']}\n"
            f"有效期至: {data['red_packets'][packet['id']]['expire_time'][:16]}",
            reply_markup=share_keyboard)

        context.user_data.pop('red_packet', None)

async def handle_group_red_packet(update: Update, context: CallbackContext):
    if not update.inline_query:
        return

    query = update.inline_query
    packet_id = query.query.split('_')[-1]
    data = load_user_data()

    if packet_id not in data['red_packets']:
        return

    packet = data['red_packets'][packet_id]
    results = [InlineQueryResultArticle(
        id=packet_id,
        title="点击发送红包到本群",
        input_message_content=InputTextMessageContent(
            f"🧧 红包来袭！\n"
            f"总金额: {packet['total']} USDT\n"
            f"个数: {packet['count']}\n"
            f"由用户 {query.from_user.mention_markdown()} 发送\n"
            f"━━━━━━━━━━━━\n"
            f"剩余: {packet['remaining']}/{packet['count']}",
            parse_mode='Markdown'
        ),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎁 领取红包", callback_data=f"claim_{packet_id}")]])
    )]

    await context.bot.answer_inline_query(query.id, results)

async def claim_red_packet(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    packet_id = query.data.split('_')[-1]
    data = load_user_data()

    if packet_id not in data['red_packets']:
        await query.edit_message_text("❌ 红包已过期")
        return

    packet = data['red_packets'][packet_id]

    if user_id in packet['claimed']:
        await query.answer("您已经领过这个红包啦！")
        return

    if datetime.now() > datetime.fromisoformat(packet['expire_time']):
        await query.edit_message_text("⏳ 红包已过期")
        data['red_packets'].pop(packet_id, None)
        save_user_data(data)
        return

    try:
        amount = packet['amounts'].pop()
    except IndexError:
        await query.edit_message_text("🧧 红包已领完")
        data['red_packets'].pop(packet_id, None)
        save_user_data(data)
        return

    packet['remaining'] -= 1
    packet['claimed'][user_id] = amount
    data['balance'][user_id] = data['balance'].get(user_id, 0) + amount
    if packet['remaining'] == 0:
        data['red_packets'].pop(packet_id, None)
    save_user_data(data)

    claim_items = list(packet['claimed'].items())[-5:]
    claim_info = "\n".join(
        [f"{(uid[:4]+'***') if len(uid)>4 else uid}: {amt} USDT" for uid, amt in claim_items]
    )

    await query.edit_message_text(
        f"🧧 红包详情\n━━━━━━━━━━━━\n"
        f"创建者: {(packet['creator'][:4]+'***') if len(packet['creator'])>4 else packet['creator']}\n"
        f"总金额: {packet['total']} USDT\n"
        f"已领取: {packet['count'] - packet['remaining']}/{packet['count']}\n"
        f"━━━━━━━━━━━━\n"
        f"领取记录:\n{claim_info}\n"
        f"━━━━━━━━━━━━\n"
        f"剩余: {packet['remaining']}个 | 有效期至: {packet['expire_time'][:16]}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎁 领取红包", callback_data=f"claim_{packet_id}")]]) if packet['remaining'] > 0 else None
    )

    await query.answer(f"领取成功！获得 {amount} USDT")

async def show_my_packets(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    data = load_user_data()

    packets_info = []
    for pid in data['user_red_packets'].get(user_id, []):
        if pid in data['red_packets']:
            p = data['red_packets'][pid]
            status = "进行中" if datetime.now() < datetime.fromisoformat(p['expire_time']) else "已结束"
            packets_info.append(
                f"📆 {p['create_time'][:16]} | {p['total']} USDT\n"
                f"状态: {status} | 剩余: {p['remaining']}/{p['count']}"
            )

    await query.edit_message_text(
        f"📦 我的红包\n━━━━━━━━━━━━\n" +
        ("\n━━━━━━━━━━━━\n".join(packets_info[:5]) if packets_info else "暂无红包记录") +
        "\n\n注：仅显示最近5个红包",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回主菜单", callback_data='back_to_main')]])
    )

async def check_expired_packets(context: CallbackContext):
    data = load_user_data()
    now = datetime.now()
    changed = False
    for packet_id in list(data['red_packets'].keys()):
        packet = data['red_packets'][packet_id]
        expire_time = datetime.fromisoformat(packet['expire_time'])
        if now > expire_time and packet['remaining'] > 0:
            remaining = sum(packet['amounts'])
            creator = packet['creator']
            data['balance'][creator] = data['balance'].get(creator, 0) + remaining
            data['red_packets'].pop(packet_id)
            changed = True

    if changed:
        save_user_data(data)

async def handle_total_stats(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    data = load_user_data()
    # 统计总流水
    total_bet = sum(data['total_bet'].values())
    # 统计当日流水
    today = datetime.now().strftime("%Y%m%d")
    today_bet = 0
    for user_history in data['bet_history'].values():
        for entry in user_history:
            if entry['round_id'].startswith(today):
                today_bet += entry['total_amount']

    # 统计所有人的总输赢
    total_result = 0
    for user_id, user_history in data['bet_history'].items():
        for entry in user_history:
            if isinstance(entry['result'], dict):
                win = entry['result'].get("winnings", 0)
                total_result += win - entry['total_amount']

    # 统计返水金额（假如logs里有action=REBATE的记录）
    total_rebate = 0
    for log in data['logs']:
        if log.get('action') == "REBATE":
            total_rebate += log.get('amount', 0)

    msg = (
        f"📊 平台数据统计\n"
        f"━━━━━━━━━━━━\n"
        f"总流水: {total_bet} USDT\n"
        f"当日流水: {today_bet} USDT\n"
        f"总输赢: {total_result} USDT\n"
        f"总返水: {total_rebate} USDT\n"
        f"━━━━━━━━━━━━"
    )
    await query.edit_message_text(
        msg,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 返回主菜单", callback_data='back_to_main')]])
    )

# ===== 辅助功能 =====
async def button_handler(update: Update, context: CallbackContext) -> None:
    try:
        query = update.callback_query
        user_id = query.from_user.id
        data = load_user_data()

        await query.answer()

        if query.data == 'deposit':
            await query.edit_message_text(
                f"💰 充值地址\n━━━━━━━━━━━━\n"
                f"TRC20地址: `{TRON_ADDRESS}`\n"
                f"━━━━━━━━━━━━\n"
                f"• 最小充值: 1 USDT\n"
                f"• 自动到账，无需联系客服\n"
                f"• 充值后余额自动更新",
                parse_mode='Markdown'
            )

        elif query.data == 'withdraw':
                await query.edit_message_text(
                "⚠️ 提现功能维护中\n"
                "请联系客服处理\n"
                "Telegram: @YwK3kf"
    )

        elif query.data == 'total_stats':
                await handle_total_stats(update, context)

        elif query.data == 'rebate':
            user_id_str = str(user_id)
            total_bet = data['total_bet'].get(user_id_str, 0)
            rebate_amount = round(total_bet * REBATE_RATE, 2)

            if rebate_amount <= 0:
                await query.answer("暂无返水可领取")
                return

            data['balance'][user_id_str] = data['balance'].get(user_id_str, 0) + rebate_amount
            data['total_bet'][user_id_str] = 0
            save_user_data(data)

            await query.edit_message_text(
                f"✅ 返水领取成功\n"
                f"━━━━━━━━━━━━\n"
                f"累计下注: {total_bet} USDT\n"
                f"返水比例: {REBATE_RATE*100}%\n"
                f"获得返水: +{rebate_amount} USDT\n"
                f"当前余额: {data['balance'][user_id_str]} USDT"
            )

        elif query.data == 'bet_history':
            user_id_str = str(user_id)
            data = load_user_data()
            history = data['bet_history'].get(user_id_str, [])

            if not history:
                await query.edit_message_text("📭 暂无下注记录")
                return

            history_msg = ["📜 下注记录（最近10期）\n━━━━━━━━━━━━"]
            for entry in reversed(history[-10:]):
                bet_details = "\n".join([f"{k}: {v} USDT" for k, v in entry['bet_details'].items()])
                result_info = (
                    f"🎲 骰子: {entry['result']['dice_values']}\n"
                    f"💰 盈利: {entry['result']['winnings']} USDT"
                    if isinstance(entry['result'], dict)
                    else "🕒 状态：等待开奖"
                )
                history_msg.append(
                    f"🆔 期号: {entry['round_id']}\n"
                    f"⏰ 时间: {entry['time']}\n"
                    f"📥 下注内容:\n{bet_details}\n"
                    f"📊 总额: {entry['total_amount']} USDT\n"
                    f"{result_info}\n"
                    f"━━━━━━━━━━━━"
                )

            await query.edit_message_text("\n".join(history_msg))
            return

        elif query.data == 'check_balance':
            balance = data['balance'].get(str(user_id), 0)
            await query.edit_message_text(f"💰 当前余额: {balance} USDT")

        elif query.data == 'help':
            await show_help(update, context)

        elif query.data == 'back_to_main':
            await start(update, context)

        elif query.data in ['roll_machine', 'roll_user']:
            try:
                if query.data == 'roll_machine':
                    dice_messages, dice_values = await send_dice(query.message, context)
                    result = calculate_result(dice_values)
                    bet_details = data['bets'].get(str(user_id), {})
                    await show_result(user_id, result, bet_details, data, context)
                    data['in_progress'][str(user_id)] = False
                    save_user_data(data)
                else:
                    user_id_str = str(user_id)
                    job_name = f"roll_timeout_{user_id}"
                    current_jobs = context.job_queue.get_jobs_by_name(job_name)
                    for job in current_jobs:
                        job.schedule_removal()

                    data['pending_rolls'][user_id_str] = []
                    save_user_data(data)
                    context.job_queue.run_once(
                        roll_timeout,
                        30,
                        data=user_id,
                        name=job_name
                    )
                    await query.edit_message_text(
        "`🎲` 请连续发送3个骰子\n\n"
        "⚠️ 重要规则：\n"
        "1. 必须直接发送骰子，转发无效\n"
        "2. 超时或无效操作不退还金额\n"
        "3. 骰子需在30秒内发送完成\n\n"
        "点击下方骰子复制：\n`🎲`",
        parse_mode='Markdown'
    )

            except Exception as e:
                await query.edit_message_text(f"❌ 错误: {str(e)}")
    except Exception as e:
        logger.error(f"按钮处理失败: {str(e)}")

async def send_dice(message: Message, context: CallbackContext, num_dice=3):
    try:
        dice_messages = []
        dice_values = []
        for _ in range(num_dice):
            msg = await message.reply_dice(emoji="🎲")
            dice_messages.append(msg)
            dice_values.append(msg.dice.value)
        return dice_messages, dice_values
    except Exception as e:
        logger.error(f"发送骰子失败: {str(e)}")
        raise

def format_history(history):
    trends = []
    for idx, entry in enumerate(history[-10:], 1):
        total = entry['total']
        size = '大' if total > 10 else '小'
        parity = '单' if total % 2 else '双'
        trends.append(f"第{idx}期: {entry['values']} {total} {size}{parity}")
    return "\n".join(trends) if trends else "暂无历史"

async def show_result(user_id, result, bet_details, data, context, is_machine=True):
    try:
        winnings, winning_bets = calculate_winnings(bet_details, result)

        if winnings > 0:
            data['balance'][str(user_id)] += winnings

        history_entry = {
            'values': result['values'],
            'total': result['total'],
            'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        data['history'].append(history_entry)
        data['history'] = data['history'][-10:]

        user_id_str = str(user_id)
        if user_id_str in data['bet_history'] and data['bet_history'][user_id_str]:
            latest_entry = data['bet_history'][user_id_str][-1]
            latest_entry["result"] = {
                "dice_values": result['values'],
                "winnings": winnings
            }
            save_user_data(data)

        bet_list = "\n".join([f"• {k}: {v} USDT" for k, v in bet_details.items()])
        trend_info = format_history(data['history'])

        msg = (
            f"🎲 {'机摇' if is_machine else '手摇'}结果\n━━━━━━━━━━━━\n"
            f"骰子: {result['values']}\n"
            f"总和: {result['total']}\n"
            f"━━━━━━━━━━━━\n"
            f"下注内容:\n{bet_list}\n"
        )

        if winnings > 0:
            msg += (
                f"🎉 中奖!\n"
                f"中奖项: {', '.join(winning_bets)}\n"
                f"奖金: +{winnings} USDT\n"
            )
        else:
            msg += "😞 未中奖\n"

        msg += (
            f"余额: {data['balance'][str(user_id)]} USDT\n"
            f"━━━━━━━━━━━━\n"
            f"近期走势:\n{trend_info}"
        )

        data['bets'].pop(str(user_id), None)
        save_user_data(data)
        await context.bot.send_message(chat_id=user_id, text=msg)
    except Exception as e:
        logger.error(f"显示结果失败: {str(e)}")

async def show_help(update: Update, context: CallbackContext):
    help_text = """
🎮 下注指南：

大小单双: da10 x10 dan10 s10 或 大10 小10 单10 双10

组合: dd10 ds10 xd10 xs10 或 大单10 大双10 小单10 小双10

特殊: 通配豹子10 dz10 sz10 或 豹子10 顺子10 对子10

特码: 定位胆位置+数字，例如: 例如: 定位胆4 10, dwd4 10, 4y 10

高倍: bz1 10 bz1 10 或 豹子1 10 豹子2 10 豹子3 10

➤ 基础项赔付：
   - 大/小/单/双 → 1:2

➤ 组合项赔率：
   - 大单/大双/小单/小双 → 1:4.5
   - 和局（首尾骰子相同）时组合项杀 例如下注大单10 开奖636

📊其余项赔率表：
通配豹子: 32 顺子: 8 对子: 2.1  指定豹子: 200

定位胆4: 58, 定位胆5: 28, 定位胆6: 16, 定位胆7: 12, 定位胆8: 8

定位胆9: 7, 定位胆10: 7, 定位胆11: 6, 定位胆12: 6, 定位胆13: 8

定位胆14: 12, 定位胆15: 16, 定位胆16: 28, 定位胆17: 58

➤ 特殊规则：
   - 豹子通杀（赔付定位豹子和通配豹子）

🔄 返水规则：
   - 下注金额的1.5%可手动领取
   - 点击【🔄 反水】按钮领取

📜 历史记录：
   💡 常用命令：
/start - 显示主菜单
/help - 显示帮助信息

- 点击【📜 下注记录】查看最近10期
    """
    await context.bot.send_message(chat_id=update.effective_chat.id, text=help_text)

async def error_handler(update: Update, context: CallbackContext):
    logger.error(msg="异常发生", exc_info=context.error)
    if update.effective_message:
        await update.effective_message.reply_text("⚠️ 系统繁忙，请稍后再试")

async def roll_timeout(context: CallbackContext):
    user_id = context.job.data
    data = load_user_data()
    user_id_str = str(user_id)

    if user_id_str in data['pending_rolls']:
        # 关键修改：不清除下注记录，不退还金额
        del data['pending_rolls'][user_id_str]
        data['in_progress'][user_id_str] = False
        save_user_data(data)
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "⏰ 手摇骰子超时\n"
                    "━━━━━━━━━━━━\n"
                    "未完成手摇骰子，本局金额不予退还"
                )
            )
        except Exception as e:
            logger.error(f"超时处理失败: {str(e)}")
# ===== 主程序 =====
def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        private_text_handler
    ))

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_red_packet_creation, pattern='^(send_red_packet|cancel)$'))
    application.add_handler(CallbackQueryHandler(confirm_red_packet, pattern='^confirm_red_packet'))
    application.add_handler(CallbackQueryHandler(show_my_packets, pattern='^my_packets'))
    application.add_handler(InlineQueryHandler(handle_group_red_packet, pattern=r'^redpacket_'))
    application.add_handler(CallbackQueryHandler(claim_red_packet, pattern=r'^claim_\d+$'))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & filters.REPLY, handle_admin_commands))
    application.add_handler(MessageHandler(filters.Dice.ALL, handle_dice_result))
    application.add_handler(CommandHandler("add", admin_add_balance))  # 这里是加余额函数
    application.add_handler(CommandHandler("set", admin_set))
    application.add_handler(CommandHandler("list", admin_list))
    application.add_handler(CommandHandler("logs", admin_logs))
    application.add_handler(CommandHandler("invite", admin_invite))
    application.add_handler(CommandHandler("help", show_help))
    application.add_handler(CommandHandler("reset_all_data", admin_reset_all_data))
    application.add_handler(CallbackQueryHandler(handle_total_stats, pattern='^total_stats$'))
    application.add_error_handler(error_handler)

    application.job_queue.run_repeating(
        check_expired_packets,
        interval=3600,
        first=10
    )

    if not DATA_FILE.exists():
        save_user_data({
            "balance": {},
            "total_bet": {},
            "logs": [],
            "bets": {},
            "bet_history": {},
            "pending_rolls": {},
            "history": [],
            "in_progress": {},
            "red_packets": {},
            "user_red_packets": {},
            "global_round": {
                "last_date": datetime.now().strftime("%Y%m%d"),
                "daily_counter": 0
            },
            "transaction_id": 1  # 初始化交易编号
        })

    application.run_polling()

if __name__ == "__main__":
    main()
