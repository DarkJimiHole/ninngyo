""" 监听指定群组/频道里指定用户的发言，并自动转发到另一个对话。 """

import contextlib
import datetime
from asyncio import sleep
from random import uniform
from typing import Dict, List, Optional, Union

import pytz
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
HELP_TEXT = """监听某群里某人的发言并自动转发。

<b>在来源群里回复那个人的任意一条消息</b>，发送：
<code>,wf add 目标 [copy]</code>

或者完整写法：
<code>,wf add 来源 用户 目标 [copy]</code>
  来源/目标：对话 id、@用户名，或 here（当前对话）
  用户：用户 id 或 @用户名，多个用英文逗号分隔
  copy：不显示"转发自"（默认 forward 显示原发送者）

<code>,wf list</code> 列出规则
<code>,wf del 编号</code> 删除规则
<code>,wf on/off 编号</code> 启用/暂停规则"""

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
    await message.edit(HELP_TEXT)


wf: "CommandHandler"


@wf.sub_command(command="add")
async def wf_add(client: Client, message: Message):
    params = message.parameter[1:]
    mode = "forward"
    if params and params[-1].lower() in MODES:
        mode = params.pop().lower()
    reply = message.reply_to_message
    try:
        if reply and len(params) == 1:
            src = message.chat.id
            uid = sender_id(reply)
            if uid is None:
                return await message.edit("无法识别被回复消息的发送者。")
            users = [uid]
            target = await resolve_chat(client, message, params[0])
        elif len(params) == 3:
            src = await resolve_chat(client, message, params[0])
            users = await resolve_users(client, params[1])
            target = await resolve_chat(client, message, params[2])
        else:
            return await message.edit(HELP_TEXT)
    except Exception as e:
        return await message.edit(f"参数解析失败：{e}")
    if not users:
        return await message.edit("没有识别到任何用户。")
    if src == target:
        return await message.edit("来源和目标不能是同一个对话。")

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
    await message.edit(text)
    await log(f"watch_forward {text}")


@wf.sub_command(command="list")
async def wf_list(message: Message):
    rules = load_rules()
    if not rules:
        return await message.edit("当前没有转发规则。")
    await message.edit(
        "\n".join(format_rule(i, r) for i, r in enumerate(rules, start=1))
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
        return await message.edit("编号无效，用 <code>,wf list</code> 查看。")
    rules = list(load_rules())
    rule = rules.pop(index)
    save_rules(rules)
    await message.edit(f"已删除：\n{format_rule(index + 1, rule)}")


async def set_enabled(message: Message, enabled: bool):
    index = get_index(message)
    if index is None:
        return await message.edit("编号无效，用 <code>,wf list</code> 查看。")
    rules = list(load_rules())
    rules[index]["enabled"] = enabled
    save_rules(rules)
    await message.edit(format_rule(index + 1, rules[index]))


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
