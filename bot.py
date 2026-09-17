import os
import sys
import time
import asyncio
import threading
import traceback
import logging
from flask import Flask
import discord
from discord.ext import commands

# ----------------------------------------------------
# 1. ตั้งค่าระบบ Logging 
# ----------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# --- Web Server สำหรับ Render ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is online and running core systems!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_web, daemon=True).start()
# --------------------------------

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ----------------------------------------------------
# 2. ตั้งค่า ID และชื่อฐานของห้อง
# ----------------------------------------------------
TARGET_CHANNEL_1_ID = 1189901220471636018  
TARGET_CHANNEL_2_ID = 1196415294483202098  
SECRET_CHANNEL_ID = 1548313870614138940    
VALID_CHANNELS = {TARGET_CHANNEL_1_ID, TARGET_CHANNEL_2_ID}

# กฎการวาร์ป
SWITCH_THRESHOLD = 5      
SWITCH_TIMEOUT = 10       

# กฎอัปเดตสีสถานะห้อง
DEBOUNCE_SECONDS = 15             
RATE_LIMIT_RETRY_BUFFER = 2       
RECONCILE_INTERVAL_SECONDS = 90   

CHANNEL_NAMES = {
    1344731417233330226: "┊ 𝔾𝔸𝕄𝔼 𝕄𝕆𝔻𝔼 𝕀",
    1189901220471636018: "┊ 𝔾𝔸𝕄𝔼 𝕄𝕆𝔻𝔼 𝕀𝕀",
    1196415294483202098: "┊ 𝔾𝔸𝕄𝔼 𝕄𝕆𝔻𝔼 𝕀𝕀𝕀",
}

user_switch_history = {}
_pending_status_tasks = {}

# ----------------------------------------------------
# 3. ฟังก์ชันผู้ช่วย (Helper Functions)
# ----------------------------------------------------
async def _safe_remove_permission(channel, member):
    """ฟังก์ชันช่วยถอนสิทธิ์ผู้ใช้ออกจากห้องลับแบบปลอดภัย"""
    try:
        await channel.set_permissions(member, overwrite=None)
        logger.info(f"[PERM CLEANUP] ถอนสิทธิ์ห้องลับของ {member.name} สำเร็จ")
    except Exception as e:
        logger.error(f"[PERM CLEANUP ERROR] ไม่สามารถถอนสิทธิ์ {member.name}: {e}")

async def reconcile_secret_channel_permissions(guild):
    """สแกนและล้าง Permission ค้างในห้องลับตอนบอทเริ่มทำงาน (Startup Reconciliation)"""
    secret_channel = guild.get_channel(SECRET_CHANNEL_ID)
    if not secret_channel:
        return

    current_members = set(secret_channel.members)
    
    for target, overwrite in list(secret_channel.overwrites.items()):
        if isinstance(target, discord.Member) and not target.bot:
            # ถ้าผู้ใช้ไม่อยู่ในห้องลับ แต่มี Overwrite ค้างอยู่ -> ลบทิ้ง
            if target not in current_members:
                logger.warning(f"[RECONCILE STARTUP] พบสิทธิ์ค้างของ {target.name} (อาจเกิดจาก บอท Crash/Restart) กำลังล้างสิทธิ์...")
                await _safe_remove_permission(secret_channel, target)

# ----------------------------------------------------
# 4. ฟังก์ชันระบบจัดการสีห้อง
# ----------------------------------------------------
async def _apply_channel_status(channel):
    """เปลี่ยนชื่อห้องพร้อมสถานะ 🟢 / 🔴"""
    if not channel or channel.id not in CHANNEL_NAMES:
        return

    base_name = CHANNEL_NAMES[channel.id]
    has_members = any(not m.bot for m in channel.members)
    status_emoji = "🟢" if has_members else "🔴"
    new_name = f"{status_emoji} {base_name}"

    if channel.name == new_name:
        return

    for attempt in range(3):
        try:
            await channel.edit(name=new_name)
            logger.info(f"[STATUS] เปลี่ยนชื่อห้องสำเร็จ {channel.id} -> {new_name}")
            return
        except discord.errors.Forbidden:
            logger.error(f"[PERMISSION DENIED] ขาดสิทธิ์ 'Manage Channels' ห้อง {channel.id}")
            return
        except discord.errors.HTTPException as e:
            if e.status == 429:
                retry_after = getattr(e, "retry_after", None)
                if not retry_after:
                    retry_after = float(e.response.headers.get("Retry-After", 5))
                wait_time = retry_after + RATE_LIMIT_RETRY_BUFFER
                logger.warning(f"[RATE LIMIT] ติดลิมิตครั้งที่ {attempt+1}/3, รอ {wait_time:.1f} วิ...")
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"[HTTP ERROR] เปลี่ยนชื่อห้องไม่ได้: {e}")
                return
        except Exception:
            logger.error(f"[SYSTEM ERROR] เกิดข้อผิดพลาด:\n{traceback.format_exc()}")
            return


async def _debounced_status_update(channel):
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        await _apply_channel_status(channel)
    except asyncio.CancelledError:
        pass 
    finally:
        current_task = asyncio.current_task()
        if _pending_status_tasks.get(channel.id) is current_task:
            _pending_status_tasks.pop(channel.id, None)


def schedule_channel_status_update(channel):
    if not channel or channel.id not in CHANNEL_NAMES:
        return

    existing_task = _pending_status_tasks.get(channel.id)
    if existing_task and not existing_task.done():
        existing_task.cancel()

    _pending_status_tasks[channel.id] = asyncio.create_task(_debounced_status_update(channel))


async def channel_status_reconciler():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            for guild in bot.guilds:
                for channel_id in CHANNEL_NAMES.keys():
                    pending = _pending_status_tasks.get(channel_id)
                    if pending and not pending.done():
                        continue

                    channel = guild.get_channel(channel_id)
                    if channel:
                        await _apply_channel_status(channel)
                    await asyncio.sleep(1)
        except Exception:
            logger.error(f"[RECONCILER ERROR] ระบบซิงค์สีทำงานผิดพลาด:\n{traceback.format_exc()}")
            
        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)


# ----------------------------------------------------
# 5. ฟังก์ชันคำนวณการวาร์ปเข้าห้องลับ
# ----------------------------------------------------
def handle_switch_count(guild_id, member_id, current_channel_id):
    key = (guild_id, member_id)
    current_time = time.monotonic()

    if key not in user_switch_history:
        user_switch_history[key] = {"last_channel": None, "count": 0, "last_time": 0.0}
    
    user_data = user_switch_history[key]

    if (current_time - user_data["last_time"]) > SWITCH_TIMEOUT:
        user_data["count"] = 0
        user_data["last_channel"] = None

    previous_channel_id = user_data["last_channel"]

    if current_channel_id in VALID_CHANNELS:
        if previous_channel_id in VALID_CHANNELS and current_channel_id != previous_channel_id:
            user_data["count"] += 1
        elif user_data["count"] == 0:
            user_data["count"] = 1
            
        logger.info(f"[TRACKING] User: {member_id} | สลับห้องเป้าหมายสะสม: {user_data['count']}/{SWITCH_THRESHOLD}")
    else:
        user_data["count"] = 0 

    user_data["last_channel"] = current_channel_id
    user_data["last_time"] = current_time

    if user_data["count"] >= SWITCH_THRESHOLD:
        user_switch_history.pop(key, None)
        return True
    return False


# ----------------------------------------------------
# 6. Core Events ของ Discord
# ----------------------------------------------------
_reconciler_started = False

@bot.event
async def on_ready():
    global _reconciler_started
    logger.info(f'=== [STARTUP] บอท {bot.user.name} ออนไลน์ (Core Systems Only) ===')
    
    for guild in bot.guilds:
        # A) ซิงค์สถานะสีห้อง
        for channel_id in CHANNEL_NAMES.keys():
            channel = guild.get_channel(channel_id)
            if channel:
                await _apply_channel_status(channel)

        # B) ล้างสิทธิ์ค้างห้องลับช่วง Startup
        await reconcile_secret_channel_permissions(guild)

    if not _reconciler_started:
        _reconciler_started = True
        asyncio.create_task(channel_status_reconciler())


@bot.event
async def on_voice_state_update(member, before, after):
    try:
        # A) คืนสิทธิ์เมื่อสมาชิกออกจากห้องลับ
        if before.channel and before.channel.id == SECRET_CHANNEL_ID and (after.channel is None or after.channel.id != SECRET_CHANNEL_ID):
            secret_channel = member.guild.get_channel(SECRET_CHANNEL_ID)
            if secret_channel:
                await _safe_remove_permission(secret_channel, member)

        # B) เคลียร์ประวัติทันทีเมื่อผู้ใช้ออกจาก Voice Channel
        if after.channel is None:
            user_switch_history.pop((member.guild.id, member.id), None)

        # C) คำนวณเพื่อวาร์ปเข้าห้องลับ
        if before.channel != after.channel and after.channel is not None:
            should_warp = handle_switch_count(member.guild.id, member.id, after.channel.id)

            if should_warp:
                secret_channel = member.guild.get_channel(SECRET_CHANNEL_ID)
                if secret_channel:
                    try:
                        await secret_channel.set_permissions(member, connect=True, view_channel=True)
                        await member.move_to(secret_channel)
                        logger.info(f"[WARP-IN] !!! ดึงตัว {member.name} เข้าห้องลับสำเร็จ !!!")
                    # 1. Catch Forbidden ก่อนเสมอ (Subclass of HTTPException)
                    except discord.errors.Forbidden:
                        logger.error(f"[PERMISSION DENIED] ไม่มีสิทธิ์วาร์ป หรือจัดการห้องลับสำหรับ {member.name}")
                        await _safe_remove_permission(secret_channel, member)
                    # 2. Catch HTTPException
                    except discord.errors.HTTPException as e:
                        logger.error(f"[MOVE FAILED] ย้ายตัวไม่สำเร็จ อาจเพราะออกห้องไปก่อน: {e}")
                        await _safe_remove_permission(secret_channel, member)
                    # 3. Catch-all เป็นตาข่ายรองรับ Exception ชนิดอื่นๆ ทั้งหมด
                    except Exception as e:
                        logger.error(f"[UNEXPECTED WARP ERROR] เกิดข้อผิดพลาดไม่คาดคิดในการวาร์ป:\n{traceback.format_exc()}")
                        await _safe_remove_permission(secret_channel, member)

        # D) ตั้งเวลาอัปเดตสี 🔴 / 🟢
        if before.channel != after.channel:
            if before.channel:
                schedule_channel_status_update(before.channel)
            if after.channel:
                schedule_channel_status_update(after.channel)

    except Exception:
        logger.error(f"[EVENT ERROR] เกิดปัญหาใน on_voice_state_update:\n{traceback.format_exc()}")


# ----------------------------------------------------
# 7. ตรวจสอบ Token ก่อนเริ่มทำงาน
# ----------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    logger.critical("[FATAL] ไม่พบ BOT_TOKEN ใน Environment Variables หรือยังไม่ได้ตั้งค่า")
    sys.exit(1)

bot.run(BOT_TOKEN)
