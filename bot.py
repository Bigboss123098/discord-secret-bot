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

DEBOUNCE_SECONDS = 20      # หน่วงกี่วิก่อนเปลี่ยนชื่อห้อง (รอสถานะนิ่งก่อนค่อยเช็คจริง)
RATE_LIMIT_RETRY_BUFFER = 2   # ติดลิมิตแล้วรอเพิ่มกี่วิจาก retry_after ที่ Discord บอก
RECONCILE_INTERVAL_SECONDS = 90  # ทุกกี่วิ ให้ไล่เช็คสถานะทุกห้องแบบเงียบๆ (ตาข่ายนิรภัย)

# ----------------------------------------------------
# ตั้งค่าเสียงต้อนรับ (ตอนคนถูกวาร์ปเข้า Backroom)
# ----------------------------------------------------
WELCOME_SOUND_ENABLED = True
WELCOME_SOUND_PATHS = ["sounds/sound1.MP3", "sounds/sound2.MP3"]  # แก้ path ให้ตรงกับไฟล์จริง
WELCOME_SOUND_CHANCE = 1.0       # โอกาสที่จะเล่นเสียงต้อนรับตอนมีคนวาร์ปเข้ามา
WELCOME_SOUND_COOLDOWN = 0   # cooldown วิ หลังเล่นแล้ว ก่อนจะเล่นให้คนถัดไปได้อีก

# ----------------------------------------------------
# ตั้งค่าระบบ "บอทหลอน" สุ่มเข้า Backroom เอง
# ----------------------------------------------------
# หมายเหตุ: Render แผน Free จะ sleep/restart service เป็นระยะ (พบว่า restart
# ทุก ~12 นาทีจริง) ทำให้ asyncio.sleep() แบบรอยาวๆ (เช่นรอ 10-50 นาทีทีเดียว)
# ไม่มีวันครบรอบ เพราะตัวแปรในหน่วยความจำหายไปพร้อม process ทุกครั้งที่ตาย
# แก้โดยเปลี่ยนวิธีคิด: "เช็คถี่ๆ ด้วยรอบสั้น" + "สุ่มโอกาสต่ำในแต่ละรอบ"
# แทนที่จะ "รอยาวแล้วเข้าทีเดียว" — ทำให้ทนต่อการ restart ได้ เพราะแต่ละรอบ
# เช็คสั้นพอที่จะครบก่อนบอทตายไปเสียก่อน
HAUNT_SOUND_ENABLED = True
HAUNT_SOUND_PATHS = ["sounds/1.mp3", "sounds/2.mp3", "sounds/3.mp3", "sounds/4.mp3", "sounds/5.mp3"]
HAUNT_CHECK_INTERVAL = 5 * 60   # เช็ครอบใหม่ทุกกี่วิ (แค่เช็ค ไม่ได้แปลว่าเข้า)
HAUNT_CHANCE_PER_CHECK = 0.07   # โอกาสที่จะเข้าไปเล่นจริงในแต่ละรอบเช็ค (7%)
HAUNT_COOLDOWN = 30 * 60        # หลังเข้าไปเล่นสำเร็จแล้ว ต้องรออย่างน้อยกี่วิถึงจะเริ่มเช็ครอบใหม่

# ระดับเสียงโดยรวม (ใช้ร่วมกันทั้งสองระบบ) — ปรับให้เบาลงได้ตามต้องการ
# หมายเหตุ: เสียง "ติ้ง" ตอนบอทเข้า/ออกห้องเสียงเป็นเสียงแจ้งเตือนของ Discord
# client แต่ละคนเอง ไม่ใช่เสียงที่บอทเล่น บอทไม่มีทางปิดเสียงนี้ได้เลย
# ถ้าอยากปิดต้องให้สมาชิกไปปิดเองที่ User Settings > Notifications ฝั่งเขา
SOUND_VOLUME = 0.4  # 0.0 - 1.0+ (1.0 = ระดับเสียงต้นฉบับ)

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

# กันบอทเล่นเสียงซ้อนกันหลายห้อง/หลายคนพร้อมกัน — ใช้ 1 lock ต่อกิลด์ ร่วมกันทั้ง
# ระบบเสียงต้อนรับและระบบบอทหลอน เพราะบอทมี voice connection ได้แค่ 1 อันต่อกิลด์
# (เล่นสองอย่างพร้อมกันในกิลด์เดียวไม่ได้อยู่แล้วในทางเทคนิค)
_voice_lock = defaultdict(asyncio.Lock)

# เวลาล่าสุดที่เล่นเสียงต้อนรับสำเร็จ ต่อกิลด์ (guild_id -> timestamp) สำหรับ cooldown
_welcome_sound_last_played = defaultdict(lambda: 0.0)


async def play_sound_in_channel(channel, sound_path, label="เสียง"):
    """ฟังก์ชันกลาง: ต่อ voice เข้าห้อง เล่นไฟล์เสียง แล้วออกจากห้องเองเสมอ
    ปลอดภัยจาก error ระหว่างเล่น ใช้ร่วมกันทั้งระบบต้อนรับและระบบหลอน
    คืนค่า True ถ้าเล่นสำเร็จ (ต่อ voice ได้และเล่นจบ), False ถ้าไม่สำเร็จ/ถูกข้าม"""
    guild = channel.guild
    lock = _voice_lock[guild.id]

    if lock.locked():
        print(f"[VOICE] ข้าม{label} เพราะบอทกำลังเล่นเสียงอื่นอยู่ในกิลด์นี้", flush=True)
        return False

    async with lock:
        if not os.path.isfile(sound_path):
            print(f"[ERROR] ไม่พบไฟล์เสียง: {sound_path}", flush=True)
            return False

        voice_client = None
        try:
            # 1. หน่วงเวลา 1.5 วินาที รอให้ Discord จัดการ Permission/State การย้ายห้องให้เสร็จก่อน
            await asyncio.sleep(1.5)

            # 2. ถ้ามี Voice Client ค้างอยู่ ให้ตัดสายเดิมออกอย่างปลอดภัยก่อน
            if guild.voice_client:
                try:
                    await guild.voice_client.disconnect(force=True)
                except Exception:
                    pass
                await asyncio.sleep(0.5)

            # 3. เชื่อมต่อเข้าห้องเสียงใหม่ (เปิด reconnect=True เพื่อให้พยายามต่อใหม่ถ้าโดนตัด)
            print(f"[VOICE] กำลังเชื่อมต่อเข้าห้อง {channel.name} ({channel.id})...", flush=True)
            voice_client = await channel.connect(timeout=15, reconnect=True)
            await asyncio.sleep(0.5)  # รอให้ Handshake นิ่งแป๊บหนึ่ง

            if not voice_client.is_connected():
                print(f"[ERROR] การเชื่อมต่อ voice หลุดก่อนจะได้เล่นเสียง", flush=True)
                return False

            print(f"[VOICE] ต่อ voice สำเร็จ กำลังเริ่มเล่น {label}: {sound_path}", flush=True)

            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(sound_path),
                volume=SOUND_VOLUME,
            )

            finished = asyncio.Event()

            def _on_done(error):
                if error:
                    print(f"[ERROR] เล่น{label}ไม่สำเร็จ: {error}", flush=True)
                bot.loop.call_soon_threadsafe(finished.set)

            voice_client.play(source, after=_on_done)

            # 4. วน Loop รอจนกว่าเสียงจะเล่นจบ และคอยเช็กว่าบอทไม่โดนเตะออกจากห้อง
            try:
                await asyncio.wait_for(finished.wait(), timeout=60)
                if voice_client.is_connected():
                    print(f"[VOICE] เล่น{label}จบแล้ว กำลังออกจากห้อง", flush=True)
                else:
                    print(f"[WARN] เสียงเล่นจบแล้ว แต่บอทหลุดจากห้องระหว่างเล่น", flush=True)
            except asyncio.TimeoutError:
                print(f"[WARN] {label}เล่นนานเกินคาด บังคับหยุด", flush=True)
                if voice_client.is_playing():
                    voice_client.stop()

            return True

        except discord.errors.ClientException as e:
            print(f"[ERROR] ต่อ voice ไม่สำเร็จ (อาจต่ออยู่แล้วหรือสิทธิ์ไม่พอ): {e}", flush=True)
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

async def play_backroom_welcome_sound(member, secret_channel):
    if not WELCOME_SOUND_ENABLED:
        print(f"[VOICE] ระบบเสียงต้อนรับปิดอยู่", flush=True)
        return

    guild_id = secret_channel.guild.id
    now = time.time()

    remaining_cooldown = WELCOME_SOUND_COOLDOWN - (now - _welcome_sound_last_played[guild_id])
    if remaining_cooldown > 0:
        print(f"[VOICE] ติด Cooldown อีก {remaining_cooldown:.0f} วินาที (ไม่เล่นเสียง)", flush=True)
        return

    roll = random.random()
    if roll > WELCOME_SOUND_CHANCE:
        print(f"[VOICE] สุ่มไม่โดน (roll={roll:.2f} > {WELCOME_SOUND_CHANCE:.2f})", flush=True)
        return

    print(f"[VOICE] สุ่มโดน! กำลังส่งบอทเข้าห้อง {secret_channel.name}...", flush=True)
    sound_path = random.choice(WELCOME_SOUND_PATHS)
    
    success = await play_sound_in_channel(secret_channel, sound_path, label="เสียงต้อนรับ")
    if success:
        _welcome_sound_last_played[guild_id] = time.time()
        print(f"[VOICE] เล่นเสียงให้ {member.name} สำเร็จ!", flush=True)
    else:
        print(f"[VOICE] เล่นเสียงไม่สำเร็จ (เช็ก Error ด้านบน)", flush=True)


async def backroom_haunt_loop(guild):
    """ระบบบอทหลอน: เช็คถี่ๆ ทุก HAUNT_CHECK_INTERVAL วิ แต่ละรอบสุ่มโอกาสต่ำๆ
    (HAUNT_CHANCE_PER_CHECK) ว่าจะเข้าไปเล่นจริงไหม เข้าได้ก็ต่อเมื่อมีคน (ไม่ใช่บอท)
    อยู่ในห้องนั้นจริงๆ ถ้าห้องว่างข้ามรอบไปเลยโดยไม่นับเป็นการสุ่มเสียโอกาส
    เข้าสำเร็จแล้วมี cooldown ก่อนจะเริ่มเช็ครอบใหม่ได้
    ออกแบบให้ทนต่อการที่ Render Free tier restart บ่อยๆ เพราะรอบเช็คสั้น
    (ไม่ต้องพึ่งการ asyncio.sleep() รอยาวๆ ที่จะหายไปพร้อม process ถ้าตายกลางทาง)"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(HAUNT_CHECK_INTERVAL)

        try:
            secret_channel = guild.get_channel(SECRET_CHANNEL_ID)
            if not secret_channel:
                continue

            human_members = [m for m in secret_channel.members if not m.bot]
            if not human_members:
                continue  # ห้องว่าง ข้ามรอบนี้เงียบๆ ไม่นับเป็นการสุ่มเสียโอกาส

            if random.random() > HAUNT_CHANCE_PER_CHECK:
                continue  # สุ่มไม่โดนรอบนี้ รอเช็ครอบถัดไป

            sound_path = random.choice(HAUNT_SOUND_PATHS)
            success = await play_sound_in_channel(secret_channel, sound_path, label="เสียงหลอน")
            if success:
                print(f"[HAUNT] เข้าไปหลอนสำเร็จ พักอย่างน้อย {HAUNT_COOLDOWN / 60:.0f} นาที")
                await asyncio.sleep(HAUNT_COOLDOWN)

        except Exception:
            print(f"\n❌ เกิด ERROR ใน backroom_haunt_loop:")
            print(traceback.format_exc())


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
        if e.status == 429:
            # ดึง retry_after ให้ชัวร์ที่สุดเท่าที่ทำได้ (บางกรณี discord.py ไม่เซ็ต
            # e.retry_after ให้ตรงๆ) ถ้าหาไม่ได้เลยก็ fallback เป็นค่า default
            retry_after = getattr(e, "retry_after", None)
            if not retry_after:
                try:
                    retry_after = float(e.response.headers.get("Retry-After", 5))
                except Exception:
                    retry_after = 5.0
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


async def channel_status_reconciler():
    """ตาข่ายนิรภัย: วนเช็คทุกห้องใน CHANNEL_NAMES เป็นระยะๆ ไม่พึ่ง event เลย
    กันกรณีห้องค้างอิโมจิผิด (เช่น ติด rate limit ตอนบอท restart พอดี,
    หรือ retry ครั้งก่อนพลาดไปด้วยเหตุผลอื่น) ให้กลับมาตรงกับความจริงเองได้เสมอ
    ไม่ยิงพร้อมกันทีเดียวทุกห้อง แต่เว้นจังหวะห่างกันเล็กน้อยเพื่อลดความเสี่ยง
    ชนกับ Rate Limit ที่อาจเกิดจาก event สดๆ ที่ debounce กำลังจัดการอยู่พอดี"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in bot.guilds:
            for channel_id in CHANNEL_NAMES.keys():
                # ถ้าห้องนี้มี debounce task ของ event สดกำลังทำงานอยู่ ข้ามไปก่อน
                # ปล่อยให้ event เป็นคนจัดการรอบนี้ ไม่ต้องแย่งกันยิง API
                pending = _pending_status_tasks.get(channel_id)
                if pending and not pending.done():
                    continue

                channel = guild.get_channel(channel_id)
                if channel:
                    await _apply_channel_status(channel)
                await asyncio.sleep(1)  # เว้นจังหวะระหว่างห้อง กันยิงรัวๆ ทีเดียวหลายห้อง

        await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)


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


_reconciler_started = False
_haunt_started = False


@bot.event
async def on_ready():
    global _reconciler_started, _haunt_started
    print(f'=== บอท {bot.user.name} ออนไลน์พร้อมระบบเปลี่ยนสีสถานะห้อง! ===')
    for guild in bot.guilds:
        for channel_id in CHANNEL_NAMES.keys():
            channel = guild.get_channel(channel_id)
            if channel:
                # ตอนเปิดบอทไม่ต้อง debounce เช็คสถานะจริงได้เลย
                await _apply_channel_status(channel)

    # on_ready อาจถูกเรียกซ้ำได้ถ้า Discord ให้ reconnect ใหม่ กันไม่ให้เปิด
    # reconciler loop ซ้อนกันหลายตัว
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
                        # เล่นเสียงต้อนรับแบบ background ไม่ block event handler
                        # (ถ้ารอ await ตรงๆ event ของสมาชิกคนอื่นจะต้องรอจนเสียงเล่นจบ)
                        asyncio.create_task(play_backroom_welcome_sound(member, secret_channel))
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
