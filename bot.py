import os
import time
import asyncio
import threading
import traceback
from collections import defaultdict
from flask import Flask
import discord
from discord.ext import commands

# --- Web Server สำหรับ Render ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is online 24/7!"

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
# กำหนด ID และชื่อฐานของห้องพูดคุย
# ----------------------------------------------------
TARGET_CHANNEL_1_ID = 1189901220471636018  # ID ห้อง A
TARGET_CHANNEL_2_ID = 1196415294483202098  # ID ห้อง B
SECRET_CHANNEL_ID = 1548313870614138940    # ID ห้องลับ
VALID_CHANNELS = {TARGET_CHANNEL_1_ID, TARGET_CHANNEL_2_ID}

SWITCH_THRESHOLD = 5      # ต้องสลับกี่ครั้งถึงจะวาร์ป
SWITCH_TIMEOUT = 20       # ถ้าห่างกันเกินกี่วิ ให้เริ่มนับใหม่

DEBOUNCE_SECONDS = 20      # หน่วงกี่วิก่อนเปลี่ยนชื่อห้อง (รอสถานะนิ่งก่อนค่อยเช็คจริง)
RATE_LIMIT_RETRY_BUFFER = 2  # ติดลิมิตแล้วรอเพิ่มกี่วิจาก retry_after ที่ Discord บอก

# ชื่อห้อง (แบบไม่รวมอีโมจิสถานะ)
CHANNEL_NAMES = {
    1344731417233330226: "┊ 𝔾𝔸𝕄𝔼 𝕄𝕆𝔻𝔼 𝕀",
    1189901220471636018: "┊ 𝔾𝔸𝕄𝔼 𝕄𝕆𝔻𝔼 𝕀𝕀",
    1196415294483202098: "┊ 𝔾𝔸𝕄𝔼 𝕄𝕆𝔻𝔼 𝕀𝕀𝕀",
    # เพิ่มห้องอื่นๆ ได้ตามต้องการ
}

user_switch_history = defaultdict(lambda: {"last_channel": None, "count": 0, "last_time": 0.0})

# เก็บ debounce task ที่กำลังรอต่อห้อง (channel_id -> asyncio.Task)
_pending_status_tasks = {}


async def _apply_channel_status(channel):
    """เช็คสถานะห้อง ณ ตอนนี้จริงๆ แล้วเปลี่ยนชื่อถ้าจำเป็น
    ถ้าติด Rate Limit จะตั้ง retry อัตโนมัติหลังจากลิมิตหมด
    (เช็คสถานะใหม่อีกครั้งตอน retry ไม่ใช่ apply ค่าเก่าที่อาจไม่จริงแล้ว)"""
    if not channel or channel.id not in CHANNEL_NAMES:
        return

    base_name = CHANNEL_NAMES[channel.id]
    human_members = [m for m in channel.members if not m.bot]
    has_members = len(human_members) > 0

    status_emoji = "🟢" if has_members else "🔴"
    new_name = f"{status_emoji} {base_name}"

    if channel.name == new_name:
        return

    try:
        await channel.edit(name=new_name)
        print(f"[STATUS] เปลี่ยนชื่อห้อง {channel.id} เป็น: {new_name}")
    except discord.errors.Forbidden:
        print(f"[ERROR] บอทไม่มีสิทธิ์ Manage Channels สำหรับห้อง {channel.id}")
    except discord.errors.HTTPException as e:
        retry_after = getattr(e, "retry_after", None)
        if e.status == 429 and retry_after:
            wait_time = retry_after + RATE_LIMIT_RETRY_BUFFER
            print(f"[WARN] ติด Rate Limit ห้อง {channel.id} จะลองใหม่ใน {wait_time:.1f} วิ")
            await asyncio.sleep(wait_time)
            await _apply_channel_status(channel)  # เช็คสถานะสดใหม่อีกรอบ ไม่ใช้ค่าเก่า
        else:
            print(f"[WARN] เปลี่ยนชื่อห้อง {channel.id} ไม่สำเร็จ: {e}")
    except Exception:
        print(f"\n❌ เกิด ERROR ใน _apply_channel_status:")
        print(traceback.format_exc())


async def _debounced_status_update(channel):
    """รอเงียบๆ สักพัก ถ้าไม่มี event ใหม่มาแทรกในช่วงนี้ ค่อยเช็ค+เปลี่ยนชื่อจริง
    กันปัญหาคนเข้าออกรัวๆ ทำให้ยิง API ถี่จนชนลิมิต"""
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        await _apply_channel_status(channel)
    except asyncio.CancelledError:
        pass  # ถูกยกเลิกเพราะมี event ใหม่มาแทนที่ ไม่ต้องทำอะไร
    finally:
        _pending_status_tasks.pop(channel.id, None)


def schedule_channel_status_update(channel):
    """เรียกทุกครั้งที่มีคนเข้า/ออกห้อง — จะ debounce ให้อัตโนมัติ
    ถ้าห้องนี้มี task รออยู่แล้ว จะยกเลิกของเก่าแล้วเริ่มรอใหม่ (เอาจังหวะล่าสุด)"""
    if not channel or channel.id not in CHANNEL_NAMES:
        return

    existing_task = _pending_status_tasks.get(channel.id)
    if existing_task and not existing_task.done():
        existing_task.cancel()

    _pending_status_tasks[channel.id] = asyncio.create_task(_debounced_status_update(channel))


def handle_switch_count(member_id, current_channel_id):
    """คำนวณและอัปเดตตัวนับการสลับห้อง คืนค่า True ถ้าถึงเงื่อนไขวาร์ป"""
    current_time = time.time()
    user_data = user_switch_history[member_id]

    timed_out = (current_time - user_data["last_time"]) > SWITCH_TIMEOUT
    previous_channel_id = user_data["last_channel"] if not timed_out else None

    if current_channel_id in VALID_CHANNELS:
        if previous_channel_id in VALID_CHANNELS and current_channel_id != previous_channel_id:
            user_data["count"] += 1
        else:
            # เข้าห้อง valid ครั้งแรก (หรือ timeout ไปแล้ว) นับเป็นแต้มที่ 1 เลย
            user_data["count"] = 1
        print(f"  └─> อยู่ในห้อง valid สะสม {user_data['count']}/{SWITCH_THRESHOLD} ครั้ง")
    else:
        user_data["count"] = 0

    user_data["last_channel"] = current_channel_id
    user_data["last_time"] = current_time

    if user_data["count"] >= SWITCH_THRESHOLD:
        user_data["count"] = 0
        return True
    return False


@bot.event
async def on_ready():
    print(f'=== บอท {bot.user.name} ออนไลน์พร้อมระบบเปลี่ยนสีสถานะห้อง! ===')
    for guild in bot.guilds:
        for channel_id in CHANNEL_NAMES.keys():
            channel = guild.get_channel(channel_id)
            if channel:
                # ตอนเปิดบอทไม่ต้อง debounce เช็คสถานะจริงได้เลย
                await _apply_channel_status(channel)


@bot.event
async def on_voice_state_update(member, before, after):
    try:
        # ----------------------------------------------------
        # 1) ดักจับการสลับห้องเข้าห้องลับ (ทำก่อน ให้ไทม์มิ่งแม่นยำ
        #    ไม่ถูกดีเลย์จากการเรียก channel.edit ด้านล่าง)
        # ----------------------------------------------------
        if before.channel and before.channel.id == SECRET_CHANNEL_ID and (
            after.channel is None or after.channel.id != SECRET_CHANNEL_ID
        ):
            secret_channel = member.guild.get_channel(SECRET_CHANNEL_ID)
            if secret_channel:
                try:
                    await secret_channel.set_permissions(member, overwrite=None)
                    print(f"[PERMISSION] ดึงสิทธิ์ห้องลับคืนจาก {member.name} แล้ว")
                except discord.errors.Forbidden:
                    print(f"[ERROR] บอทไม่มีสิทธิ์จัดการ permission ห้องลับ")

        if before.channel != after.channel and after.channel is not None:
            should_warp = handle_switch_count(member.id, after.channel.id)

            if should_warp:
                secret_channel = member.guild.get_channel(SECRET_CHANNEL_ID)
                if secret_channel:
                    try:
                        await secret_channel.set_permissions(member, connect=True, view_channel=True)
                        await member.move_to(secret_channel)
                        print(f"[SUCCESS] วาร์ป {member.name} ไปยังห้องลับสำเร็จ!")
                    except discord.errors.Forbidden:
                        print(f"[ERROR] บอทไม่มีสิทธิ์ move_members หรือจัดการ permission ห้องลับ")

        # ----------------------------------------------------
        # 2) อัปเดตสีสถานะ 🔴 / 🟢
        #    ใช้ debounce: รอสถานะนิ่งก่อนค่อยเช็คจริงและเปลี่ยนชื่อ
        #    กันคนเข้าออกรัวๆ ยิง API ถี่จนชน Discord Rate Limit
        #    (ถ้าชนแล้วก็ยัง auto-retry จนสถานะซิงค์กับความจริงอยู่ดี)
        # ----------------------------------------------------
        if before.channel != after.channel:
            if before.channel:
                schedule_channel_status_update(before.channel)
            if after.channel:
                schedule_channel_status_update(after.channel)

    except Exception:
        print(f"\n❌ เกิด ERROR ในการทำงาน:")
        print(traceback.format_exc())


BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
bot.run(BOT_TOKEN)
