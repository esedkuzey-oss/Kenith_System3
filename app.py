import os
import secrets
import sqlite3
import threading
import requests
import discord

from datetime import datetime, timedelta
from flask import Flask, jsonify, request
from discord.ext import commands

app = Flask(__name__)

DB_NAME = "licenses.db"

DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")


# ==============================
# DATABASE
# ==============================

def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=15)
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kenith_licenses (
            key TEXT PRIMARY KEY,
            hwid TEXT DEFAULT '',
            discord_user TEXT UNIQUE,
            days INTEGER NOT NULL,
            expires_at TEXT DEFAULT '',
            status TEXT DEFAULT 'ACTIVE',
            remaining_seconds INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


init_db()


# ==============================
# DISCORD
# ==============================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    case_insensitive=True
)


@bot.event
async def on_ready():
    print(f"Bot aktif: {bot.user}")


# ==============================
# KEY GENERATOR
# ==============================

def generate_custom_key():
    return (
        "K"
        + "".join(secrets.choice("0123456789") for _ in range(3))
        + "-"
        + "".join(secrets.choice("0123456789") for _ in range(4))
        + "-"
        + "".join(secrets.choice("0123456789") for _ in range(4))
    )


# ==============================
# DISCORD COMMANDS
# ==============================

@bot.command(name="license")
@commands.has_permissions(administrator=True)
async def create_license(ctx, arg1: str, member: discord.Member = None):
    
    try:
        days = int(arg1)
    except ValueError:
        await ctx.send("Geçersiz komut! Örnek: `!license 30 @Kullanici` veya `!license 7 @Kullanici`")
        return

    if days not in (7, 30):
        await ctx.send("Hata: Sadece **7 günlük** veya **30 günlük** lisans oluşturulabilir!")
        return

    if not member:
        await ctx.send("Lütfen geçerli bir Discord kullanıcısı etiketleyin. Örnek: `!license 30 @Kullanici`")
        return

    user_id_str = str(member.id)

    conn = get_db()
    cursor = conn.cursor()

    try:
        # Kişinin üzerinde AKTİF veya FROZEN (geçerli) bir lisans var mı?
        cursor.execute("SELECT key FROM kenith_licenses WHERE discord_user = ? AND status IN ('ACTIVE', 'FROZEN')", (user_id_str,))
        existing_license = cursor.fetchone()

        if existing_license:
            conn.close()
            await ctx.send("Kişinin üzerinde license mevcut")
            return

        # Eski REVOKED kayıt varsa temizle, yeni key'e yer aç
        cursor.execute("DELETE FROM kenith_licenses WHERE discord_user = ?", (user_id_str,))

        # Yeni tek bir key üret ve kaydet
        new_key = generate_custom_key()
        total_seconds = days * 24 * 3600

        cursor.execute(
            """
            INSERT INTO kenith_licenses
            (key, hwid, discord_user, days, expires_at, status, remaining_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (new_key, "", user_id_str, days, "", "ACTIVE", total_seconds)
        )
        
        conn.commit()
        conn.close()

        await ctx.send(
            f"Yeni {days} günlük lisans {member.mention} için oluşturuldu:\n"
            f"`{new_key}`"
        )

    except sqlite3.Error as e:
        conn.rollback()
        conn.close()
        print(f"Database hatası: {e}")
        await ctx.send("Veritabanı hatası!")


@bot.command(name="reset")
@commands.has_permissions(administrator=True)
async def reset_command(ctx, sub_action: str = None, member: discord.Member = None):
    """Komut: !reset license @Kullanici"""
    if not sub_action or sub_action.lower() != "license" or not member:
        await ctx.send("Kullanım: `!reset license @Kullanici`")
        return

    user_id_str = str(member.id)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE kenith_licenses SET status = 'REVOKED' WHERE discord_user = ?", (user_id_str,))
    
    if cursor.rowcount == 0:
        conn.close()
        await ctx.send("Bu kullanıcıya ait lisans bulunamadı.")
        return

    conn.commit()
    conn.close()
    await ctx.send(f"{member.mention} üzerindeki key etkisiz hale getirildi.")


@bot.command(name="freeze")
@commands.has_permissions(administrator=True)
async def freeze_command(ctx, sub_action: str = None, member: discord.Member = None):
    """Komut: !freeze license @Kullanici"""
    if not sub_action or sub_action.lower() != "license" or not member:
        await ctx.send("Kullanım: `!freeze license @Kullanici`")
        return

    user_id_str = str(member.id)

    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT expires_at, remaining_seconds, status FROM kenith_licenses WHERE discord_user = ?", (user_id_str,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        await ctx.send("Bu kullanıcıya ait aktif bir lisans bulunamadı.")
        return

    expires_at, remaining_seconds, status = row

    if status == 'FROZEN':
        conn.close()
        await ctx.send("Bu kullanıcının lisansı zaten dondurulmuş.")
        return

    if expires_at:
        try:
            expire_dt = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
            current_time = datetime.utcnow()
            if expire_dt > current_time:
                remaining_seconds = int((expire_dt - current_time).total_seconds())
            else:
                remaining_seconds = 0
        except ValueError:
            pass

    cursor.execute(
        """
        UPDATE kenith_licenses 
        SET status = 'FROZEN', remaining_seconds = ? 
        WHERE discord_user = ?
        """,
        (remaining_seconds, user_id_str)
    )
    conn.commit()
    conn.close()
    await ctx.send(f"{member.mention} kullanıcısının lisansı donduruldu, süresi durduruldu ve exeye giriş engellendi.")


@bot.command(name="hwid")
@commands.has_permissions(administrator=True)
async def hwid_command(ctx, action: str = None, member: discord.Member = None):
    """Komut: !hwid reset @Kullanici"""
    if not action or action.lower() != "reset" or not member:
        await ctx.send("Kullanım: `!hwid reset @Kullanici`")
        return

    user_id_str = str(member.id)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE kenith_licenses SET hwid = '', expires_at = '', status = 'ACTIVE' WHERE discord_user = ?", (user_id_str,))
    
    if cursor.rowcount == 0:
        conn.close()
        await ctx.send("Bu kullanıcıya ait lisans bulunamadı.")
        return

    conn.commit()
    conn.close()
    await ctx.send(f"{member.mention} kullanıcısının HWID'si sıfırlandı.")


@create_license.error
async def license_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Bu komutu sadece yöneticiler kullanabilir.")
    else:
        print(f"Komut hatası: {error}")


# ==============================
# VALIDATION API (EXENİN BAĞLANDIĞI YER)
# ==============================

@app.route("/validate", methods=["POST"])
def validate():
    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"status": "ERROR", "message": "Geçersiz JSON!"}), 400

    key = data.get("license_key")
    hwid = data.get("hwid")

    if not isinstance(key, str) or not isinstance(hwid, str):
        return jsonify({"status": "ERROR", "message": "Eksik veri!"}), 400

    key = key.strip()
    hwid = hwid.strip()

    if not key or not hwid or len(hwid) > 256:
        return jsonify({"status": "ERROR", "message": "Eksik veri!"}), 400

    conn = get_db()

    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute(
            """
            SELECT hwid, expires_at, status, remaining_seconds
            FROM kenith_licenses
            WHERE key = ?
            """,
            (key,)
        )

        row = cursor.fetchone()

        if not row:
            conn.rollback()
            return jsonify({"status": "ERROR", "message": "Geçersiz anahtar!"}), 403

        saved_hwid, expires_at, status, remaining_seconds = row
        current_time = datetime.utcnow()

        if status == 'REVOKED':
            conn.rollback()
            return jsonify({"status": "ERROR", "message": "Bu lisans etkisiz hale getirilmiştir!"}), 403

        if status == 'FROZEN':
            conn.rollback()
            return jsonify({"status": "ERROR", "message": "Bu lisans dondurulmuştur!"}), 403

        # İLK AKTİVASYON VE HWID KİLİTLEME
        if not saved_hwid:
            expire_date = current_time + timedelta(seconds=remaining_seconds)
            formatted_expire = expire_date.strftime("%Y-%m-%d %H:%M:%S")

            cursor.execute(
                """
                UPDATE kenith_licenses
                SET hwid = ?, expires_at = ?
                WHERE key = ? AND hwid = ''
                """,
                (hwid, formatted_expire, key)
            )

            conn.commit()

            if WEBHOOK_URL:
                try:
                    requests.post(
                        WEBHOOK_URL,
                        json={
                            "content": (
                                "Yeni lisans aktivasyonu!\n"
                                f"Key: `{key}`\n"
                                f"HWID: `{hwid}`\n"
                                f"Bitiş: `{formatted_expire}`"
                            )
                        },
                        timeout=5
                    )
                except requests.RequestException as e:
                    print(f"Webhook hatası: {e}")

            return jsonify({
                "status": "SUCCESS",
                "message": "Giriş onaylandı! Lisans cihaza sabitlendi.",
                "remaining_time": f"{remaining_seconds // 86400} gün",
                "license_bound": True
            })

        if saved_hwid != hwid:
            conn.rollback()
            return jsonify({"status": "ERROR", "message": "Bu anahtar başka bir cihaza kilitli!"}), 403

        try:
            expire_dt = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            conn.rollback()
            return jsonify({"status": "ERROR", "message": "Geçersiz bitiş tarihi!"}), 500

        if current_time >= expire_dt:
            conn.rollback()
            return jsonify({"status": "ERROR", "message": "Lisans süresi dolmuş!"}), 403

        remaining = expire_dt - current_time
        rem_days = remaining.days
        rem_hours = remaining.seconds // 3600
        rem_minutes = (remaining.seconds % 3600) // 60

        conn.commit()

        return jsonify({
            "status": "SUCCESS",
            "message": "Giriş onaylandı!",
            "remaining_time": f"{rem_days} gün {rem_hours} saat {rem_minutes} dakika",
            "license_bound": True
        })

    except sqlite3.Error as e:
        conn.rollback()
        print(f"Database hatası: {e}")
        return jsonify({"status": "ERROR", "message": "Sunucu veritabanı hatası!"}), 500

    finally:
        conn.close()


# ==============================
# HOME
# ==============================

@app.route("/")
def home():
    return "Kenith Licensing API is running!"


# ==============================
# START BOT
# ==============================

def run_discord_bot():
    if not DISCORD_TOKEN:
        print("HATA: DISCORD_BOT_TOKEN bulunamadı!")
        return

    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print(f"Bot başlatma hatası: {e}")


# ==============================
# START SERVER
# ==============================

if __name__ == "__main__":
    bot_thread = threading.Thread(
        target=run_discord_bot,
        daemon=True
    )
    bot_thread.start()

    port = int(os.environ.get("PORT", 5000))
    print("Kenith Licensing API başlatılıyor...")

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )