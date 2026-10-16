""" 监听指定群组/频道里指定用户的发言，并自动转发到另一个对话。 """

import contextlib
import datetime
import html
from asyncio import sleep
from random import uniform
from typing import Dict, List, Optional, Union

import pytz
from pyrogram.enums import ChatType
from pyrogram.errors import FloodWait, RPCError

from pagermaid.config import Config
from pagermaid.dependence import sqlite
from pagermaid.enums import Client, Message
from pagermaid.enums.command import CommandHandler
from pagermaid.listener import listener
from pagermaid.services import bot, scheduler
from pagermaid.utils import logs
from pagermaid.utils.bot_utils import log

DB_KEY = "watch_forward.rules"
MODES = ("forward", "copy")
HELP_TEXT = """监听某群里某人的发言并自动转发。以下命令都可以在收藏夹里执行，群里不会留下痕迹。

<b>查 id</b>
<code>,wf chats [关键词]</code> 列出已加入的群/频道及 id
<code>,wf who 群 关键词</code> 按名字/用户名在群里查找用户 id
回复转发来的消息：<code>,wf who 来源群</code> 查原发送者 id（对方隐藏转发来源时也能查）

<b>添加规则</b>
<code>,wf add 来源 用户 目标 [copy]</code>
  来源/目标：对话 id、@用户名，或 here（当前对话）
  用户：用户 id 或 @用户名，多个用英文逗号分隔
  copy：不显示"转发自"（默认 forward 显示原发送者）
把那个人的消息转发到收藏夹，回复它：<code>,wf add 来源 目标 [copy]</code>
在来源群里回复那个人的消息：<code>,wf add 目标 [copy]</code>（命令会立即删除，结果发到收藏夹）

<b>管理</b>
<code>,wf list</code> 列出规则
<code>,wf del 编号</code> 删除规则
<code>,wf on/off 编号</code> 启用/暂停规则"""

LIST_LIMIT = 50
GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.FORUM, ChatType.CHANNEL)

# 缓存，避免每条消息都读一次数据库
_rules: Optional[List[Dict]] = None


def load_rules() -> List[Dict]:
    global _rules
    if _rules is None:
        _rules = list(sqlite.get(DB_KEY, []))
    return _rules


def save_rules(rules: List[Dict]):
    global _rules
    _rules = rules
    # SqliteDict 只有重新赋值才会持久化
    sqlite[DB_KEY] = rules


def cast_id(val: str) -> Union[int, str]:
    try:
        return int(val)
    except ValueError:
        return val


async def resolve_chat(client: Client, message: Message, raw: str) -> int:
    if raw.lower() == "here":
        return message.chat.id
    raw = cast_id(raw)
    if isinstance(raw, int):
        # 数字 id 直接用，即便本地还没有该对话的缓存
        with contextlib.suppress(Exception):
            return (await client.get_chat(raw)).id
        return raw
    return (await client.get_chat(raw)).id


async def resolve_users(client: Client, raw: str) -> List[int]:
    ids = []
    for item in filter(None, raw.split(",")):
        item = cast_id(item.strip())
        if isinstance(item, int):
            ids.append(item)
            continue
        # @用户名 可能是用户，也可能是"以频道身份发言"的频道
        with contextlib.suppress(Exception):
            ids.append((await client.get_users(item)).id)
            continue
        ids.append((await client.get_chat(item)).id)
    return ids


def sender_id(message: Message) -> Optional[int]:
    if message.from_user:
        return message.from_user.id
    if message.sender_chat:
        return message.sender_chat.id
    return None


def origin_id(message: Message) -> Optional[int]:
    """转发消息的原发送者 id；对方隐藏了转发来源时返回 None。"""
    origin = message.forward_origin
    if origin is None:
        return sender_id(message)
    for attr in ("sender_user", "sender_chat", "chat"):
        peer = getattr(origin, attr, None)
        if peer:
            return peer.id
    return None


async def find_original_sender(client: Client, chat_id: int, fwd: Message) -> Optional[int]:
    """对方隐藏了转发来源时，按原消息的发送时间和内容去来源群的记录里找到原消息，取发送者 id。"""
    date = getattr(fwd.forward_origin, "date", None)
    if not date:
        return None
    content = fwd.text or fwd.caption
    candidates = []
    # offset_date 返回早于该时间的消息，所以往后放 2 秒
    async for msg in client.get_chat_history(
        chat_id, offset_date=date + datetime.timedelta(seconds=2), limit=100
    ):
        if not msg.date or msg.date < date - datetime.timedelta(seconds=1):
            break
        if abs((msg.date - date).total_seconds()) > 1:
            continue
        peer = msg.from_user or msg.sender_chat
        if not peer:
            continue
        if content and (msg.text or msg.caption) == content:
            return peer.id
        candidates.append(peer.id)
    # 内容对不上（比如原消息被编辑过）时，只有同一时刻仅一个人发言才采用
    return candidates[0] if len(set(candidates)) == 1 else None


def display_name(peer) -> str:
    name = getattr(peer, "title", None) or " ".join(
        filter(None, [getattr(peer, "first_name", None), getattr(peer, "last_name", None)])
    )
    username = f" @{peer.username}" if getattr(peer, "username", None) else ""
    return f"{html.escape(name or '')}{username}"


async def respond(message: Message, text: str):
    """在群/频道里执行时立即删除命令消息，结果发到收藏夹，避免被群友看到。"""
    if message.chat.type in GROUP_TYPES:
        with contextlib.suppress(Exception):
            await message.delete()
        await bot.send_message("me", text)
    else:
        await message.edit(text)


def format_rule(index: int, rule: Dict) -> str:
    users = ", ".join(f"<code>{i}</code>" for i in rule["users"])
    status = "" if rule.get("enabled", True) else " ⏸已暂停"
    return (
        f"<b>{index}.</b> <code>{rule['src']}</code> 中 {users}\n"
        f"    → <code>{rule['target']}</code> ({rule['mode']}){status}"
    )


@listener(
    command="wf",
    description="监听指定用户发言并自动转发",
    parameters=HELP_TEXT,
)
async def wf(message: Message):
    await respond(message, HELP_TEXT)


wf: "CommandHandler"


@wf.sub_command(command="chats")
async def wf_chats(client: Client, message: Message):
    keyword = " ".join(message.parameter[1:]).lower()
    lines = []
    async for dialog in client.get_dialogs():
        chat = dialog.chat
        if not chat or chat.type not in GROUP_TYPES:
            continue
        if keyword and keyword not in f"{chat.title or ''} {chat.username or ''}".lower():
            continue
        kind = "频道" if chat.type == ChatType.CHANNEL else "群"
        lines.append(f"<code>{chat.id}</code> {kind} {display_name(chat)}")
        if len(lines) >= LIST_LIMIT:
            lines.append(f"……只显示前 {LIST_LIMIT} 个，请加关键词缩小范围")
            break
    await respond(message, "\n".join(lines) or "没有找到匹配的群或频道。")


@wf.sub_command(command="who")
async def wf_who(client: Client, message: Message):
    reply = message.reply_to_message
    if len(message.parameter) < 2 or (len(message.parameter) < 3 and not reply):
        return await respond(
            message,
            "用法：<code>,wf who 群 关键词</code>，或回复一条转发来的消息：<code>,wf who 来源群</code>",
        )
    try:
        chat_id = await resolve_chat(client, message, message.parameter[1])
    except Exception as e:
        return await respond(message, f"无法识别群：{e}")
    if reply and len(message.parameter) == 2:
        uid = origin_id(reply) or await find_original_sender(client, chat_id, reply)
        if uid is None:
            return await respond(message, "没能在这个群的记录里找到这条消息的原消息。")
        return await respond(message, f"原发送者 id：<code>{uid}</code>")
    keyword = " ".join(message.parameter[2:])
    lowered = keyword.lower()
    found = {}
    # 先搜成员列表（普通群会忽略 query 返回全部成员，所以本地再筛一遍）；
    # 成员列表被隐藏或是频道时，退回到扫描最近的聊天记录
    with contextlib.suppress(Exception):
        async for member in client.get_chat_members(chat_id, query=keyword, limit=200):
            if member.user and lowered in display_name(member.user).lower():
                found[member.user.id] = member.user
                if len(found) >= 20:
                    break
    if not found:
        with contextlib.suppress(Exception):
            async for msg in client.get_chat_history(chat_id, limit=1000):
                peer = msg.from_user or msg.sender_chat
                if not peer or peer.id in found:
                    continue
                if lowered in display_name(peer).lower():
                    found[peer.id] = peer
                    if len(found) >= 20:
                        break
    if not found:
        return await respond(message, "没有找到匹配的用户。")
    await respond(
        message,
        "\n".join(f"<code>{pid}</code> {display_name(peer)}" for pid, peer in found.items()),
    )


@wf.sub_command(command="add")
async def wf_add(client: Client, message: Message):
    params = message.parameter[1:]
    mode = "forward"
    if params and params[-1].lower() in MODES:
        mode = params.pop().lower()
    reply = message.reply_to_message
    try:
        if len(params) == 3:
            src = await resolve_chat(client, message, params[0])
            users = await resolve_users(client, params[1])
            target = await resolve_chat(client, message, params[2])
        elif reply and len(params) == 2:
            # 收藏夹里回复一条转发来的消息：用户取原发送者
            src = await resolve_chat(client, message, params[0])
            uid = origin_id(reply) or await find_original_sender(client, src, reply)
            if uid is None:
                return await respond(
                    message,
                    "对方隐藏了转发来源，且没能在来源群的记录里找到原消息，"
                    "请用 <code>,wf who 群 名字</code> 查找。",
                )
            users = [uid]
            target = await resolve_chat(client, message, params[1])
        elif reply and len(params) == 1:
            if message.chat.type not in GROUP_TYPES:
                return await respond(
                    message, "不在群里时请用：回复转发的消息 <code>,wf add 来源 目标</code>"
                )
            uid = sender_id(reply)
            if uid is None:
                return await respond(message, "无法识别被回复消息的发送者。")
            src = message.chat.id
            users = [uid]
            target = await resolve_chat(client, message, params[0])
        else:
            return await respond(message, HELP_TEXT)
    except Exception as e:
        return await respond(message, f"参数解析失败：{html.escape(str(e))}")
    if not users:
        return await respond(message, "没有识别到任何用户。")
    if src == target:
        return await respond(message, "来源和目标不能是同一个对话。")

    rules = list(load_rules())
    for rule in rules:
        if rule["src"] == src and rule["target"] == target and rule["mode"] == mode:
            rule["users"] = sorted(set(rule["users"]) | set(users))
            break
    else:
        rules.append(
            {"src": src, "users": users, "target": target, "mode": mode, "enabled": True}
        )
    save_rules(rules)
    text = f"已添加：<code>{src}</code> 中 {users} 的发言 → <code>{target}</code> ({mode})"
    await respond(message, text)
    await log(f"watch_forward {text}")


@wf.sub_command(command="list")
async def wf_list(message: Message):
    rules = load_rules()
    if not rules:
        return await respond(message, "当前没有转发规则。")
    await respond(
        message, "\n".join(format_rule(i, r) for i, r in enumerate(rules, start=1))
    )


def get_index(message: Message) -> Optional[int]:
    if len(message.parameter) != 2:
        return None
    try:
        index = int(message.parameter[1]) - 1
    except ValueError:
        return None
    return index if 0 <= index < len(load_rules()) else None


@wf.sub_command(command="del")
async def wf_del(message: Message):
    index = get_index(message)
    if index is None:
        return await respond(message, "编号无效，用 <code>,wf list</code> 查看。")
    rules = list(load_rules())
    rule = rules.pop(index)
    save_rules(rules)
    await respond(message, f"已删除：\n{format_rule(index + 1, rule)}")


async def set_enabled(message: Message, enabled: bool):
    index = get_index(message)
    if index is None:
        return await respond(message, "编号无效，用 <code>,wf list</code> 查看。")
    rules = list(load_rules())
    rules[index]["enabled"] = enabled
    save_rules(rules)
    await respond(message, format_rule(index + 1, rules[index]))


@wf.sub_command(command="on")
async def wf_on(message: Message):
    await set_enabled(message, True)


@wf.sub_command(command="off")
async def wf_off(message: Message):
    await set_enabled(message, False)


async def do_forward(target: int, message: Message, ids: List[int], mode: str):
    try:
        await bot.forward_messages(
            target,
            message.chat.id,
            ids,
            hide_sender_name=mode == "copy",
        )
    except FloodWait as e:
        await sleep(e.value + uniform(0.5, 1.0))
        await do_forward(target, message, ids, mode)
    except RPCError as e:
        if "FORWARDS_RESTRICTED" not in str(e):
            logs.warning(f"watch_forward 转发失败 {message.chat.id} -> {target}: {e}")
            return
        # 来源对话禁止转发：只发送文字内容和链接
        text = message.text or message.caption or "[媒体消息]"
        with contextlib.suppress(Exception):
            await bot.send_message(target, f"{text}\n\n{message.link}")


async def forward_media_group(target: int, message: Message, mode: str):
    try:
        msgs = await bot.get_media_group(message.chat.id, message.id)
        ids = [m.id for m in msgs]
    except Exception:
        ids = [message.id]
    await do_forward(target, message, ids, mode)


@listener(
    is_plugin=True,
    incoming=True,
    outgoing=False,
    ignore_edited=True,
    ignore_forwarded=False,
)
async def watch_forward_handler(message: Message):
    rules = load_rules()
    if not rules:
        return
    uid = sender_id(message)
    if uid is None:
        return
    for rule in rules:
        if (
            rule["src"] != message.chat.id
            or uid not in rule["users"]
            or not rule.get("enabled", True)
        ):
            continue
        if message.media_group_id:
            # 相册会拆成多条消息到达，延迟几秒合并成一次转发
            key = f"watch_forward.{rule['target']}.{message.media_group_id}"
            scheduler.add_job(
                forward_media_group,
                trigger="date",
                args=(rule["target"], message, rule["mode"]),
                id=key,
                name=key,
                replace_existing=True,
                run_date=datetime.datetime.now(pytz.timezone(Config.TIME_ZONE))
                + datetime.timedelta(seconds=3),
            )
        else:
            await do_forward(rule["target"], message, [message.id], rule["mode"])
