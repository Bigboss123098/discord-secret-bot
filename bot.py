import os
import time
import random
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
SWITCH_TIMEOUT = 10       # ถ้าห่างกันเกินกี่วิ ให้เริ่มนับใหม่

DEBOUNCE_SECONDS = 20      # หน่วงกี่วิก่อนเปลี่ยนชื่อห้อง
RATE_LIMIT_RETRY_BUFFER = 2   # ติดลิมิตแล้วรอเพิ่มกี่วิ
RECONCILE_INTERVAL_SECONDS = 90  # ทุกกี่วิ ให้ไล่เช็คสถานะทุกห้องแบบเงียบๆ

# ----------------------------------------------------
# ตั้งค่าระบบ "บอทสุ่มเข้าห้องเล่นเสียง" (Haunt System)
# ----------------------------------------------------
HAUNT_SOUND_ENABLED = True
# รวมเสียงทุกไฟล์เข้าด้วยกันสำหรับสุ่มเล่น
HAUNT_SOUND_PATHS = [
    "sounds/sound1.MP3", 
    "sounds/sound2.MP3", 
    "sounds/1.mp3", 
    "sounds/2.mp3", 
    "sounds/3.mp3", 
    "sounds/4.mp3", 
    "sounds/5.mp3"
]
HAUNT_CHECK_INTERVAL = 1 * 60   # เช็ครอบใหม่ทุกกี่วิ (5 นาที)
HAUNT_CHANCE_PER_CHECK = 1    # โอกาสที่จะเข้าไปเล่นจริงในแต่ละรอบเช็ค (0.5 = 50%)
HAUNT_COOLDOWN = 20 * 60        # หลังเข้าไปเล่นสำเร็จแล้ว พักกี่วิก่อนเริ่มเช็ครอบใหม่ (20 นาที)

SOUND_VOLUME = 0.4  # ระดับเสียง 0.0 - 1.0 (0.4 = 40%)

# ชื่อห้อง (แบบไม่รวมอีโมจิสถานะ)
CHANNEL_NAMES = {
    1344731417233330226: "┊ 𝔾𝔸𝕄𝔼 𝕄𝕆𝔻𝔼 𝕀",
    1189901220471636018: "┊ 𝔾𝔸𝕄𝔼 𝕄𝕆𝔻𝔼 𝕀𝕀",
    1196415294483202098: "┊ 𝔾𝔸𝕄𝔼 𝕄𝕆𝔻𝔼 𝕀𝕀𝕀",
}

user_switch_history = defaultdict(lambda: {"last_channel": None, "count": 0, "last_time": 0.0})
_pending_status_tasks = {}
_voice_lock = defaultdict(asyncio.Lock)


async def play_sound_in_channel(channel, sound_path, label="เสียง"):
    """ฟังก์ชันเล่นเสียงเข้าห้อง แล้วออกจากห้องเอง"""
    guild = channel.guild
    lock = _voice_lock[guild.id]

    if lock.locked():
        print(f"[VOICE] ข้าม {label} เพราะบอทกำลังเล่นเสียงอื่นอยู่ในกิลด์นี้", flush=True)
        return False

    async with lock:
        if not os.path.isfile(sound_path):
            print(f"[ERROR] ไม่พบไฟล์เสียง: {sound_path}", flush=True)
            return False

        voice_client = None
        try:
            # 1. หน่วงเวลาเล็กน้อยให้ Voice State ของ Discord นิ่ง
            await asyncio.sleep(1.0)

            # 2. ล้าง Voice Client เก่าถ้ามีค้าง
            if guild.voice_client:
                try:
                    await guild.voice_client.disconnect(force=True)
                except Exception:
                    pass
                await asyncio.sleep(0.5)

            # 3. เชื่อมต่อเข้าห้องเสียง
            print(f"[VOICE] กำลังเชื่อมต่อเข้าห้อง {channel.name} ({channel.id})...", flush=True)
            voice_client = await channel.connect(timeout=15, reconnect=True)
            await asyncio.sleep(0.5)

            if not voice_client.is_connected():
                print(f"[ERROR] การเชื่อมต่อ voice หลุดก่อนจะได้เล่นเสียง", flush=True)
                return False

            print(f"[VOICE] ต่อ voice สำเร็จ กำลังเริ่มเล่น {label}: {sound_path}", flush=True)

            # ใช้ FFmpeg Filter ปรับระดับเสียง เพื่อลดภาระ CPU ของเครื่อง Render
            ffmpeg_options = {
                'options': f'-vn -filter:a "volume={SOUND_VOLUME}"'
            }
            source = discord.FFmpegPCMAudio(sound_path, **ffmpeg_options)

            finished = asyncio.Event()

            def _on_done(error):
                if error:
                    print(f"[ERROR] เล่น {label} ไม่สำเร็จ: {error}", flush=True)
                bot.loop.call_soon_threadsafe(finished.set)

            voice_client.play(source, after=_on_done)

            # 4. รอจนกว่าเสียงจะเล่นจบ
            try:
                await asyncio.wait_for(finished.wait(), timeout=60)
                if voice_client.is_connected():
                    print(f"[VOICE] เล่น {label} จบแล้ว กำลังออกจากห้อง", flush=True)
                else:
                    print(f"[WARN] เสียงเล่นจบแล้ว แต่บอทหลุดจากห้องระหว่างเล่น", flush=True)
            except asyncio.TimeoutError:
                print(f"[WARN] {label} เล่นนานเกินคาด บังคับหยุด", flush=True)
                if voice_client.is_playing():
                    voice_client.stop()

            return True

        except discord.errors.ClientException as e:
            print(f"[ERROR] ต่อ voice ไม่สำเร็จ: {e}", flush=True)
            return False
        except asyncio.TimeoutError:
            print(f"[ERROR] ต่อ voice ห้อง {channel.id} timeout", flush=True)
            return False
        except Exception:
            print(f"\n❌ เกิด ERROR ใน play_sound_in_channel ({label}):", flush=True)
            print(traceback.format_exc(), flush=True)
            return False
        finally:
            # 5. ออกจากห้องเสมอไม่ว่าจะเกิดอะไรขึ้น
            if voice_client and voice_client.is_connected():
                try:
                    await voice_client.disconnect(force=True)
                except Exception:
                    pass


async def backroom_haunt_loop(guild):
    """ระบบสุ่มเข้าห้องลับมาเล่นเสียงแบบสุ่ม เมื่อมีคนอยู่ในห้อง"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(HAUNT_CHECK_INTERVAL)

        try:
            secret_channel = guild.get_channel(SECRET_CHANNEL_ID)
            if not secret_channel:
                continue

            human_members = [m for m in secret_channel.members if not m.bot]
            if not human_members:
                continue  # ห้องว่าง ข้ามรอบนี้ไป

            if random.random() > HAUNT_CHANCE_PER_CHECK:
                continue  # สุ่มไม่โดนรอบนี้

            sound_path = random.choice(HAUNT_SOUND_PATHS)
            success = await play_sound_in_channel(secret_channel, sound_path, label="เสียงสุ่ม")
            if success:
                print(f"[HAUNT] เข้าไปเล่นเสียงสำเร็จ พักอย่างน้อย {HAUNT_COOLDOWN / 60:.0f} นาที", flush=True)
                await asyncio.sleep(HAUNT_COOLDOWN)

        except Exception:
            print(f"\n❌ เกิด ERROR ใน backroom_haunt_loop:", flush=True)
            print(traceback.format_exc(), flush=True)


async def _apply_channel_status(channel):
    """อัปเดตชื่อห้องพร้อมสถานะ 🟢 / 🔴"""
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
        print(f"[STATUS] เปลี่ยนชื่อห้อง {channel.id} เป็น: {new_name}", flush=True)
    except discord.errors.Forbidden:
        print(f"[ERROR] บอทไม่มีสิทธิ์ Manage Channels สำหรับห้อง {channel.id}", flush=True)
    except discord.errors.HTTPException as e:
        if e.status == 429:
            retry_after = getattr(e, "retry_after", None)
            if not retry_after:
                try:
                    retry_after = float(e.response.headers.get("Retry-After", 5))
                except Exception:
                    retry_after = 5.0
            wait_time = retry_after + RATE_LIMIT_RETRY_BUFFER
            print(f"[WARN] ติด Rate Limit ห้อง {channel.id} จะลองใหม่ใน {wait_time:.1f} วิ", flush=True)
            await asyncio.sleep(wait_time)
            await _apply_channel_status(channel)
        else:
            print(f"[WARN] เปลี่ยนชื่อห้อง {channel.id} ไม่สำเร็จ: {e}", flush=True)
    except Exception:
        print(f"\n❌ เกิด ERROR ใน _apply_channel_status:", flush=True)
        print(traceback.format_exc(), flush=True)


async def _debounced_status_update(channel):
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
        await _apply_channel_status(channel)
    except asyncio.CancelledError:
        pass
    finally:
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
        for guild in bot.guilds:
            for channel_id in CHANNEL_NAMES.keys():
                pending = _pending_status_tasks.get(channel_id)
                if pending and not pending.done():
                    continue

                channel = guild.get_channel(channel_id)
                if channel:
                    await _apply_channel_status(channel)
                await asyncio.sleep(1)

        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)


def handle_switch_count(member_id, current_channel_id):
    current_time = time.time()
    user_data = user_switch_history[member_id]

    timed_out = (current_time - user_data["last_time"]) > SWITCH_TIMEOUT
    previous_channel_id = user_data["last_channel"] if not timed_out else None

    if current_channel_id in VALID_CHANNELS:
        if previous_channel_id in VALID_CHANNELS and current_channel_id != previous_channel_id:
            user_data["count"] += 1
        else:
            user_data["count"] = 1
        print(f"  └─> อยู่ในห้อง valid สะสม {user_data['count']}/{SWITCH_THRESHOLD} ครั้ง", flush=True)
    else:
        user_data["count"] = 0

    user_data["last_channel"] = current_channel_id
    user_data["last_time"] = current_time

    if user_data["count"] >= SWITCH_THRESHOLD:
        user_data["count"] = 0
        return True
    return False


_reconciler_started = False
_haunt_started = False


@bot.event
async def on_ready():
    global _reconciler_started, _haunt_started
    print(f'=== บอท {bot.user.name} ออนไลน์เรียบร้อยแล้ว! ===', flush=True)
    for guild in bot.guilds:
        for channel_id in CHANNEL_NAMES.keys():
            channel = guild.get_channel(channel_id)
            if channel:
                await _apply_channel_status(channel)

    if not _reconciler_started:
        _reconciler_started = True
        asyncio.create_task(channel_status_reconciler())

    if HAUNT_SOUND_ENABLED and not _haunt_started:
        _haunt_started = True
        for guild in bot.guilds:
            asyncio.create_task(backroom_haunt_loop(guild))


@bot.event
async def on_voice_state_update(member, before, after):
    try:
        # 1) ดึงสิทธิ์ห้องลับคืนเมื่อสมาชิกออกจากห้องลับ
        if before.channel and before.channel.id == SECRET_CHANNEL_ID and (
            after.channel is None or after.channel.id != SECRET_CHANNEL_ID
        ):
            secret_channel = member.guild.get_channel(SECRET_CHANNEL_ID)
            if secret_channel:
                try:
                    await secret_channel.set_permissions(member, overwrite=None)
                    print(f"[PERMISSION] ดึงสิทธิ์ห้องลับคืนจาก {member.name} แล้ว", flush=True)
                except discord.errors.Forbidden:
                    print(f"[ERROR] บอทไม่มีสิทธิ์จัดการ permission ห้องลับ", flush=True)

        # 2) ตรวจสอบการสลับห้องเพื่อวาร์ปเข้าห้องลับ
        if before.channel != after.channel and after.channel is not None:
            should_warp = handle_switch_count(member.id, after.channel.id)

            if should_warp:
                secret_channel = member.guild.get_channel(SECRET_CHANNEL_ID)
                if secret_channel:
                    try:
                        await secret_channel.set_permissions(member, connect=True, view_channel=True)
                        await member.move_to(secret_channel)
                        print(f"[SUCCESS] วาร์ป {member.name} ไปยังห้องลับสำเร็จ!", flush=True)
                    except discord.errors.Forbidden:
                        print(f"[ERROR] บอทไม่มีสิทธิ์ move_members หรือจัดการ permission ห้องลับ", flush=True)

        # 3) อัปเดตสถานะห้อง 🔴 / 🟢
        if before.channel != after.channel:
            if before.channel:
                schedule_channel_status_update(before.channel)
            if after.channel:
                schedule_channel_status_update(after.channel)

    except Exception:
        print(f"\n❌ เกิด ERROR ในการทำงาน:", flush=True)
        print(traceback.format_exc(), flush=True)


BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
bot.run(BOT_TOKEN)
