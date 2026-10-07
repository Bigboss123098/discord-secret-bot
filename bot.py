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

# ====================================================
# 1. Logging
# ====================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger("voicebot")
# ปิด log ของ Flask/werkzeug ที่ขึ้นทุกครั้งที่ uptime monitor ยิง HEAD / (รกมาก)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# ====================================================
# 2. Web server สำหรับ Render (keep-alive)
# ====================================================
app = Flask("keepalive")


@app.route("/")
def home():
    return "Bot is online and running core systems!"


@app.route("/health")
def health():
    return "ok"


def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


# ====================================================
# 3. ค่าตั้งต้น (แก้ตรงนี้ที่เดียว)
# ====================================================
TARGET_CHANNEL_1_ID = 1189901220471636018
TARGET_CHANNEL_2_ID = 1196415294483202098
SECRET_CHANNEL_ID = 1548313870614138940
VALID_CHANNELS = {TARGET_CHANNEL_1_ID, TARGET_CHANNEL_2_ID}

# กฎการวาร์ป
SWITCH_THRESHOLD = 5      # สลับห้องกี่ครั้งถึงโดนวาร์ป
SWITCH_TIMEOUT = 10       # ต้องสลับต่อเนื่องภายในกี่วินาที

# กฎการเปลี่ยนชื่อห้อง (Discord จำกัด ~2 ครั้ง / 10 นาที / ห้อง)
DEBOUNCE_SECONDS = 30             # รอให้คนเข้าออกนิ่งก่อนค่อยเปลี่ยนชื่อ
MIN_RENAME_INTERVAL = 330         # เว้นอย่างน้อยกี่วินาทีระหว่างการ rename ห้องเดียวกัน
EDIT_TIMEOUT = 20                 # ถ้า edit ค้างเกินนี้ (ติด rate limit) ให้ยกเลิก ไม่รอเป็นชั่วโมง
MAX_FAIL_COOLDOWN = 3600          # เพดาน backoff เมื่อ error ที่ไม่ใช่ 429
RATE_LIMIT_BUFFER = 5             # บวกเวลาเผื่อจาก Retry-After
RECONCILE_INTERVAL_SECONDS = 300  # ตรวจชื่อห้องทุกกี่วินาที
PERM_RECONCILE_INTERVAL_SECONDS = 120  # ตรวจสิทธิ์ค้างห้องลับ

# สมาชิกที่ถูกตั้งสิทธิ์ในห้องลับไว้ "ถาวร" โดยแอดมิน (บอทจะไม่ล้างสิทธิ์ให้)
PROTECTED_MEMBER_IDS = set()  # เช่น {123456789012345678}

# Login backoff
MAX_LOGIN_RETRIES = 5
BASE_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 1800
FINAL_FAIL_SLEEP_SECONDS = 600   # ก่อน exit ให้รอสักพัก กัน Render restart รัวๆ
HEALTHY_RUN_SECONDS = 600        # รันได้นานกว่านี้ ถือว่าปกติ รีเซ็ตตัวนับ retry

CHANNEL_NAMES = {
    1344731417233330226: "┊ 𝔾𝔸𝕄𝔼 𝕄𝕆𝔻𝔼 𝕀",
    1189901220471636018: "┊ 𝔾𝔸𝕄𝔼 𝕄𝕆𝔻𝔼 𝕀𝕀",
    1196415294483202098: "┊ 𝔾𝔸𝕄𝔼 𝕄𝕆𝔻𝔼 𝕀𝕀𝕀",
}


# ====================================================
# 4. ตัวบอท (state ทั้งหมดอยู่ในคลาส ไม่ใช่ global
#    เพื่อให้สร้างใหม่ตอน retry login ได้โดยไม่มี state ค้าง)
# ====================================================
class VoiceBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.guilds = True
        intents.members = True  # ต้องเปิด SERVER MEMBERS INTENT ใน Developer Portal ด้วย
        super().__init__(command_prefix="!", intents=intents)

        self.switch_history = {}     # (guild_id, member_id) -> dict
        self.pending_status = {}     # channel_id -> debounce task
        self.rename_locks = {}       # channel_id -> asyncio.Lock
        self.next_allowed = {}       # channel_id -> เวลา monotonic ที่ rename ได้อีกครั้ง
        self.fail_counts = {}        # channel_id -> จำนวนครั้งที่พังติดกัน
        self.warping = set()         # (guild_id, member_id) ที่กำลังวาร์ปอยู่ กัน reconciler มาล้างสิทธิ์ซ้อน
        self._bg_tasks = []
        self._last_skip_log = {}     # throttle log

    # ---------- lifecycle ----------
    async def setup_hook(self):
        # setup_hook ถูกเรียกครั้งเดียวต่อการ start ไม่ซ้ำตอน reconnect
        self._bg_tasks.append(asyncio.create_task(self._status_reconciler()))
        self._bg_tasks.append(asyncio.create_task(self._permission_reconciler()))

    async def close(self):
        for t in self._bg_tasks:
            t.cancel()
        for t in list(self.pending_status.values()):
            t.cancel()
        await super().close()

    async def on_ready(self):
        # on_ready อาจถูกเรียกซ้ำตอน reconnect จึงทำแค่ log
        # งานซิงค์ทั้งหมดให้ reconciler (ที่รอ wait_until_ready) จัดการเอง
        logger.info(f"=== [STARTUP] บอท {self.user} ออนไลน์ | {len(self.guilds)} guild ===")

    async def on_error(self, event_method, *args, **kwargs):
        logger.error(f"[EVENT ERROR] {event_method}:\n{traceback.format_exc()}")

    # ---------- ห้องลับ: จัดการสิทธิ์ ----------
    async def _safe_remove_permission(self, channel, member):
        try:
            await channel.set_permissions(member, overwrite=None)
            logger.info(f"[PERM CLEANUP] ถอนสิทธิ์ห้องลับของ {member} สำเร็จ")
        except discord.NotFound:
            pass  # overwrite/ห้อง/สมาชิกหายไปแล้ว ถือว่าเรียบร้อย
        except Exception as e:
            logger.error(f"[PERM CLEANUP ERROR] ไม่สามารถถอนสิทธิ์ {member}: {e}")

    async def reconcile_secret_channel_permissions(self, guild):
        secret_channel = guild.get_channel(SECRET_CHANNEL_ID)
        if not secret_channel:
            return

        current_members = set(secret_channel.members)
        for target, _ in list(secret_channel.overwrites.items()):
            if not isinstance(target, discord.Member) or target.bot:
                continue
            if target.id in PROTECTED_MEMBER_IDS:
                continue
            if (guild.id, target.id) in self.warping:
                continue
            if target not in current_members:
                logger.warning(f"[RECONCILE] พบสิทธิ์ค้างของ {target} กำลังล้าง...")
                await self._safe_remove_permission(secret_channel, target)

    async def _permission_reconciler(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                for guild in self.guilds:
                    await self.reconcile_secret_channel_permissions(guild)
                self._prune_switch_history()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(f"[PERM RECONCILER ERROR]\n{traceback.format_exc()}")
            await asyncio.sleep(PERM_RECONCILE_INTERVAL_SECONDS)

    # ---------- เปลี่ยนชื่อห้องตามสถานะ ----------
    def _log_skip_throttled(self, channel_id, msg, every=600):
        now = time.monotonic()
        if now - self._last_skip_log.get(channel_id, 0) >= every:
            self._last_skip_log[channel_id] = now
            logger.info(msg)

    def _set_cooldown(self, channel_id, seconds):
        self.next_allowed[channel_id] = time.monotonic() + seconds

    def _fail_cooldown(self, channel_id):
        n = self.fail_counts.get(channel_id, 0) + 1
        self.fail_counts[channel_id] = n
        return min(MIN_RENAME_INTERVAL * (2 ** (n - 1)), MAX_FAIL_COOLDOWN)

    @staticmethod
    def _retry_after_from(exc, default=60.0):
        value = getattr(exc, "retry_after", None)
        if value is None:
            try:
                value = exc.response.headers.get("Retry-After")
            except Exception:
                value = None
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def apply_channel_status(self, channel):
        """เปลี่ยนชื่อห้องเป็น 🟢/🔴 ตามสถานะจริง โดยไม่มีวันยิงรัวหรือค้างรอเป็นชั่วโมง"""
        if not channel or channel.id not in CHANNEL_NAMES:
            return

        lock = self.rename_locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            base_name = CHANNEL_NAMES[channel.id]
            has_members = any(not m.bot for m in channel.members)
            new_name = f"{'🟢' if has_members else '🔴'} {base_name}"

            if channel.name == new_name:
                self.fail_counts.pop(channel.id, None)
                return

            now = time.monotonic()
            allowed_at = self.next_allowed.get(channel.id, 0)
            if now < allowed_at:
                self._log_skip_throttled(
                    channel.id,
                    f"[STATUS] ห้อง {channel.id} ยังอยู่ในช่วงพัก อีก {allowed_at - now:.0f} วิ (ข้ามรอบนี้)",
                )
                return

            # ตั้งช่วงพักล่วงหน้าเลย: ต่อให้เทียบชื่อไม่ตรงตลอด ก็จะไม่ยิงถี่กว่า MIN_RENAME_INTERVAL
            self._set_cooldown(channel.id, MIN_RENAME_INTERVAL)

            try:
                await asyncio.wait_for(
                    channel.edit(name=new_name, reason="Update voice channel status"),
                    timeout=EDIT_TIMEOUT,
                )
                self.fail_counts.pop(channel.id, None)
                logger.info(f"[STATUS] เปลี่ยนชื่อห้องสำเร็จ {channel.id} -> {new_name}")

            except asyncio.TimeoutError:
                # discord.py กำลัง sleep รอ rate limit ยาวๆ อยู่ข้างใน -> ตัดทิ้ง ไม่รอ
                cd = self._fail_cooldown(channel.id)
                self._set_cooldown(channel.id, cd)
                logger.warning(
                    f"[RATE LIMIT?] edit ห้อง {channel.id} ค้างเกิน {EDIT_TIMEOUT} วิ "
                    f"ยกเลิกและพัก {cd:.0f} วิ"
                )
            except discord.Forbidden:
                self._set_cooldown(channel.id, MAX_FAIL_COOLDOWN)
                logger.error(f"[PERMISSION DENIED] ขาดสิทธิ์ 'Manage Channels' ห้อง {channel.id} พัก 1 ชม.")
            except discord.NotFound:
                self._set_cooldown(channel.id, MAX_FAIL_COOLDOWN)
                logger.error(f"[NOT FOUND] ไม่พบห้อง {channel.id} (อาจถูกลบ) พัก 1 ชม.")
            except discord.HTTPException as e:
                if e.status == 429:
                    wait = self._retry_after_from(e) + RATE_LIMIT_BUFFER
                    self._set_cooldown(channel.id, wait)
                    logger.warning(
                        f"[RATE LIMIT] ห้อง {channel.id} โดนบล็อก {wait:.0f} วิ -> ข้ามจนกว่าจะพ้น (ไม่ retry)"
                    )
                else:
                    cd = self._fail_cooldown(channel.id)
                    self._set_cooldown(channel.id, cd)
                    logger.error(f"[HTTP ERROR] เปลี่ยนชื่อห้องไม่ได้ ({e.status}): {e} พัก {cd:.0f} วิ")
            except Exception:
                cd = self._fail_cooldown(channel.id)
                self._set_cooldown(channel.id, cd)
                logger.error(f"[SYSTEM ERROR] พัก {cd:.0f} วิ:\n{traceback.format_exc()}")

    async def _debounced_status_update(self, channel):
        me = asyncio.current_task()
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        # ออกจากสถานะ "รอ" แล้ว: ถอดตัวเองออกจาก pending ก่อนยิงจริง
        # เพื่อไม่ให้การ schedule รอบใหม่ไป cancel ตอนที่กำลัง edit อยู่
        if self.pending_status.get(channel.id) is me:
            self.pending_status.pop(channel.id, None)
        try:
            await self.apply_channel_status(channel)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(f"[DEBOUNCE ERROR]\n{traceback.format_exc()}")

    def schedule_channel_status_update(self, channel):
        if not channel or channel.id not in CHANNEL_NAMES:
            return
        existing = self.pending_status.get(channel.id)
        if existing and not existing.done():
            existing.cancel()
        self.pending_status[channel.id] = asyncio.create_task(self._debounced_status_update(channel))

    async def _status_reconciler(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                for guild in self.guilds:
                    for channel_id in CHANNEL_NAMES:
                        pending = self.pending_status.get(channel_id)
                        if pending and not pending.done():
                            continue
                        channel = guild.get_channel(channel_id)
                        if channel:
                            await self.apply_channel_status(channel)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(f"[RECONCILER ERROR]\n{traceback.format_exc()}")
            await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)

    # ---------- นับการสลับห้องเพื่อวาร์ป ----------
    def handle_switch_count(self, guild_id, member_id, current_channel_id):
        key = (guild_id, member_id)
        now = time.monotonic()

        data = self.switch_history.setdefault(key, {"last_channel": None, "count": 0, "last_time": 0.0})

        if (now - data["last_time"]) > SWITCH_TIMEOUT:
            data["count"] = 0
            data["last_channel"] = None

        previous = data["last_channel"]

        if current_channel_id in VALID_CHANNELS:
            if previous in VALID_CHANNELS and current_channel_id != previous:
                data["count"] += 1
            elif data["count"] == 0:
                data["count"] = 1
            logger.info(f"[TRACKING] User: {member_id} | สลับห้องเป้าหมายสะสม: {data['count']}/{SWITCH_THRESHOLD}")
        else:
            data["count"] = 0

        data["last_channel"] = current_channel_id
        data["last_time"] = now

        if data["count"] >= SWITCH_THRESHOLD:
            self.switch_history.pop(key, None)
            return True
        return False

    def _prune_switch_history(self):
        """ลบประวัติเก่าที่ไม่มีใครใช้แล้ว กัน memory โตไปเรื่อยๆ"""
        now = time.monotonic()
        for key in [k for k, v in self.switch_history.items() if now - v["last_time"] > SWITCH_TIMEOUT * 6]:
            self.switch_history.pop(key, None)

    # ---------- วาร์ปเข้าห้องลับ ----------
    async def _warp_to_secret(self, member):
        secret_channel = member.guild.get_channel(SECRET_CHANNEL_ID)
        if not secret_channel:
            logger.error(f"[WARP] ไม่พบห้องลับ {SECRET_CHANNEL_ID}")
            return

        if not member.voice or not member.voice.channel:
            logger.info(f"[WARP] {member} ออกจาก voice ไปแล้ว ยกเลิกการวาร์ป")
            return

        key = (member.guild.id, member.id)
        self.warping.add(key)
        success = False
        try:
            await secret_channel.set_permissions(member, connect=True, view_channel=True)
            await member.move_to(secret_channel)
            success = True
            logger.info(f"[WARP-IN] ดึงตัว {member} เข้าห้องลับสำเร็จ")
        except discord.Forbidden:
            logger.error(f"[PERMISSION DENIED] ไม่มีสิทธิ์วาร์ป/จัดการห้องลับสำหรับ {member}")
        except discord.HTTPException as e:
            logger.error(f"[MOVE FAILED] ย้ายตัวไม่สำเร็จ (อาจออกห้องไปก่อน): {e}")
        except Exception:
            logger.error(f"[UNEXPECTED WARP ERROR]\n{traceback.format_exc()}")
        finally:
            self.warping.discard(key)
            if not success:
                await self._safe_remove_permission(secret_channel, member)

    # ---------- Event หลัก ----------
    async def on_voice_state_update(self, member, before, after):
        try:
            if member.bot:
                return

            # A) ออกจากห้องลับ -> ถอนสิทธิ์
            if (
                before.channel
                and before.channel.id == SECRET_CHANNEL_ID
                and (after.channel is None or after.channel.id != SECRET_CHANNEL_ID)
                and member.id not in PROTECTED_MEMBER_IDS
            ):
                secret_channel = member.guild.get_channel(SECRET_CHANNEL_ID)
                if secret_channel:
                    await self._safe_remove_permission(secret_channel, member)

            # B) ออกจาก voice -> เคลียร์ประวัติ
            if after.channel is None:
                self.switch_history.pop((member.guild.id, member.id), None)

            # C) ย้ายห้อง -> นับเพื่อวาร์ป
            if before.channel != after.channel and after.channel is not None:
                if self.handle_switch_count(member.guild.id, member.id, after.channel.id):
                    await self._warp_to_secret(member)

            # D) ตั้งเวลาอัปเดตสีห้อง
            if before.channel != after.channel:
                if before.channel:
                    self.schedule_channel_status_update(before.channel)
                if after.channel:
                    self.schedule_channel_status_update(after.channel)

        except Exception:
            logger.error(f"[EVENT ERROR] on_voice_state_update:\n{traceback.format_exc()}")


# ====================================================
# 5. เริ่มบอทพร้อม backoff กัน crash-loop / login rate limit
# ====================================================
async def run_once(token):
    bot = VoiceBot()
    async with bot:
        await bot.start(token)  # reconnect อัตโนมัติเมื่อหลุดระหว่างทาง


async def main(token):
    attempt = 0
    while attempt < MAX_LOGIN_RETRIES:
        started = time.monotonic()
        try:
            await run_once(token)
            logger.info("[SHUTDOWN] บอทปิดตัวปกติ")
            return

        except discord.LoginFailure:
            logger.critical("[FATAL] BOT_TOKEN ไม่ถูกต้อง ตรวจ Environment Variables แล้วค่อย deploy ใหม่")
            await asyncio.sleep(FINAL_FAIL_SLEEP_SECONDS)
            sys.exit(1)

        except discord.PrivilegedIntentsRequired:
            logger.critical(
                "[FATAL] ยังไม่ได้เปิด Privileged Intents (SERVER MEMBERS INTENT) "
                "ใน Discord Developer Portal -> Bot"
            )
            await asyncio.sleep(FINAL_FAIL_SLEEP_SECONDS)
            sys.exit(1)

        except asyncio.CancelledError:
            raise

        except Exception as e:
            # ถ้ารันมาได้นานพอ ถือว่าเป็นปัญหาใหม่ ไม่ใช่ loop เดิม -> รีเซ็ตตัวนับ
            if time.monotonic() - started > HEALTHY_RUN_SECONDS:
                attempt = 0
            attempt += 1

            is_429 = isinstance(e, discord.HTTPException) and e.status == 429
            wait = min(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)
            if is_429:
                logger.critical(
                    f"[LOGIN RATE LIMITED] ครั้งที่ {attempt}/{MAX_LOGIN_RETRIES} "
                    f"โดน Discord บล็อกชั่วคราว รอ {wait} วิ..."
                )
            else:
                logger.critical(
                    f"[BOT CRASHED] ครั้งที่ {attempt}/{MAX_LOGIN_RETRIES} "
                    f"({type(e).__name__}: {e}) รอ {wait} วิ แล้วลองใหม่\n{traceback.format_exc()}"
                )
            if attempt < MAX_LOGIN_RETRIES:
                await asyncio.sleep(wait)

    logger.critical(
        f"[LOGIN FAILED] ลองครบ {MAX_LOGIN_RETRIES} ครั้งแล้วไม่สำเร็จ "
        f"รอ {FINAL_FAIL_SLEEP_SECONDS} วิ ก่อนปิดโปรเซส (กัน Render restart รัวๆ)"
    )
    await asyncio.sleep(FINAL_FAIL_SLEEP_SECONDS)
    sys.exit(1)


if __name__ == "__main__":
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.critical("[FATAL] ไม่พบ BOT_TOKEN ใน Environment Variables หรือยังไม่ได้ตั้งค่า")
        sys.exit(1)

    threading.Thread(target=run_web, daemon=True).start()

    try:
        asyncio.run(main(BOT_TOKEN))
    except KeyboardInterrupt:
        logger.info("[SHUTDOWN] ถูกหยุดด้วยผู้ใช้")
