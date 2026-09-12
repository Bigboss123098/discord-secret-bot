import os
import time
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

# ชื่อห้อง (แบบไม่รวมอีโมจิสถานะ)
CHANNEL_NAMES = {
    1344731417233330226: "┊ 𝔾𝔸𝕄𝔼 𝕄𝕆𝔻𝔼 𝕀",
    1189901220471636018: "┊ 𝔾𝔸𝕄𝔼 𝕄𝕆𝔻𝔼 𝕀𝕀",
    1196415294483202098: "┊ 𝔾𝔸𝕄𝔼 𝕄𝕆𝔻𝔼 𝕀𝕀𝕀",
    # เพิ่มห้องอื่นๆ ได้ตามต้องการ
}

user_switch_history = defaultdict(lambda: {"last_channel": None, "count": 0, "last_time": 0})

# ฟังก์ชันช่วยอัปเดตสีอีโมจิหน้าชื่อห้อง
async def update_channel_status(channel):
    if not channel or channel.id not in CHANNEL_NAMES:
        return
    
    base_name = CHANNEL_NAMES[channel.id]
    # เช็กว่าในห้องมีคนอยู่ไหม (ไม่นับบอท)
    human_members = [m for m in channel.members if not m.bot]
    has_members = len(human_members) > 0
    
    status_emoji = "🟢" if has_members else "🔴"
    new_name = f"{status_emoji} {base_name}"
    
    # เปลี่ยนชื่อเฉพาะตอนที่ชื่อปัจจุบันไม่ตรงกับสถานะใหม่ (กันโดน Rate Limit)
    if channel.name != new_name:
        try:
            await channel.edit(name=new_name)
            print(f"[STATUS] เปลี่ยนชื่อห้อง {channel.id} เป็น: {new_name}")
        except discord.errors.HTTPException as e:
            print(f"[WARN] ติด Rate Limit การเปลี่ยนชื่อห้อง: {e}")

@bot.event
async def on_ready():
    print(f'=== บอท {bot.user.name} ออนไลน์พร้อมระบบเปลี่ยนสีสถานะห้อง! ===')
    # ตรวจสอบสถานะห้องทั้งหมดตอนเปิดบอทขึ้นมาใหม่
    for guild in bot.guilds:
        for channel_id in CHANNEL_NAMES.keys():
            channel = guild.get_channel(channel_id)
            if channel:
                await update_channel_status(channel)

@bot.event
async def on_voice_state_update(member, before, after):
    try:
        # ----------------------------------------------------
        # ฟีเจอร์ใหม่: อัปเดตสีสถานะ 🔴 / 🟢 เมื่อมีคนเข้า/ออกจากห้อง
        # ----------------------------------------------------
        if before.channel != after.channel:
            if before.channel:
                await update_channel_status(before.channel)
            if after.channel:
                await update_channel_status(after.channel)

        # ----------------------------------------------------
        # ฟีเจอร์เดิม: ดักจับการสลับห้องเข้าห้องลับ
        # ----------------------------------------------------
        if before.channel and before.channel.id == SECRET_CHANNEL_ID and (after.channel is None or after.channel.id != SECRET_CHANNEL_ID):
            secret_channel = member.guild.get_channel(SECRET_CHANNEL_ID)
            if secret_channel:
                await secret_channel.set_permissions(member, overwrite=None)
                print(f"[PERMISSION] ดึงสิทธิ์ห้องลับคืนจาก {member.name} แล้ว")

        if before.channel != after.channel and after.channel is not None:
            current_time = time.time()
            user_data = user_switch_history[member.id]
            
            if current_time - user_data["last_time"] > 10:
                user_data["count"] = 0
                
            current_channel_id = after.channel.id
            previous_channel_id = user_data["last_channel"]
            valid_channels = {TARGET_CHANNEL_1_ID, TARGET_CHANNEL_2_ID}
            
            if current_channel_id in valid_channels and previous_channel_id in valid_channels and current_channel_id != previous_channel_id:
                user_data["count"] += 1
                print(f"  └─> สลับห้องสำเร็จ! สะสมครบ {user_data['count']}/5 ครั้ง")
            else:
                if current_channel_id in valid_channels:
                    user_data["count"] = 1
                else:
                    user_data["count"] = 0

            user_data["last_channel"] = current_channel_id
            user_data["last_time"] = current_time

            if user_data["count"] >= 5:
                secret_channel = member.guild.get_channel(SECRET_CHANNEL_ID)
                if secret_channel:
                    await secret_channel.set_permissions(member, connect=True, view_channel=True)
                    await member.move_to(secret_channel)
                    print(f"[SUCCESS] วาร์ป {member.name} ไปยังห้องลับสำเร็จ!")
                    user_data["count"] = 0

    except Exception as e:
        print(f"\n❌ เกิด ERROR ในการทำงาน:")
        print(traceback.format_exc())

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
bot.run(BOT_TOKEN)
