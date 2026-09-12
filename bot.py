import discord
from discord.ext import commands
from collections import defaultdict
import time
import traceback

# --- สร้าง Web Server เพื่อหลอก Render ---
app = Flask('')
@app.route('/')
def home():
    return "Bot is alive!"

def run_web():
    app.run(host='0.0.0.0', port=8080)

threading.Thread(target=run_web).start()
# ----------------------------------------

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ID ห้องของคุณ
TARGET_CHANNEL_1_ID = 1298656451962732620  # ID ห้อง A
TARGET_CHANNEL_2_ID = 1310982353622925393  # ID ห้อง B
SECRET_CHANNEL_ID = 1548318934606942299    # ID ห้องลับ

user_switch_history = defaultdict(lambda: {"last_channel": None, "count": 0, "last_time": 0})

@bot.event
async def on_ready():
    print(f'=== บอท {bot.user.name} ออนไลน์และพร้อมใช้งานแล้ว! ===')

@bot.event
async def on_voice_state_update(member, before, after):
    try:
        # 1. เช็กกรณีออกจากห้องลับ ยึดสิทธิ์คืน
        if before.channel and before.channel.id == SECRET_CHANNEL_ID and (after.channel is None or after.channel.id != SECRET_CHANNEL_ID):
            secret_channel = member.guild.get_channel(SECRET_CHANNEL_ID)
            if secret_channel:
                await secret_channel.set_permissions(member, overwrite=None)
                print(f"[PERMISSION] ดึงสิทธิ์ห้องลับคืนจาก {member.name} แล้ว")

        # 2. เช็กการย้ายห้อง
        if before.channel != after.channel and after.channel is not None:
            print(f"[LOG] {member.name} ย้ายไปห้อง: {after.channel.name} (ID: {after.channel.id})")
            
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
                    print(f"  └─> เริ่มนับ 1 ใหม่ที่ห้องนี้")
                else:
                    user_data["count"] = 0

            user_data["last_channel"] = current_channel_id
            user_data["last_time"] = current_time

            # สลับครบ 5 ครั้ง
            if user_data["count"] >= 5:
                print(f"[ACTION] ครบ 5 ครั้ง! กำลังพา {member.name} ส่งห้องลับ...")
                secret_channel = member.guild.get_channel(SECRET_CHANNEL_ID)
                
                if not secret_channel:
                    print(f"[ERROR] หาห้องลับไม่เจอ! ตรวจสอบ SECRET_CHANNEL_ID อีกครั้ง")
                    return

                await secret_channel.set_permissions(member, connect=True, view_channel=True)
                await member.move_to(secret_channel)
                print(f"[SUCCESS] วาร์ป {member.name} ไปยังห้องลับสำเร็จ!")
                user_data["count"] = 0

    except Exception as e:
        print(f"\n❌ เกิด ERROR ในการทำงาน:")
        print(traceback.format_exc())

# เปลี่ยน Token ใหม่หลังจาก Reset ใน Developer Portal ที่นี่
bot.run(os.environ.get("BOT_TOKEN"))
