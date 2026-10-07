import os
import uuid
import telebot
from dotenv import load_dotenv
import yt_dlp

# Load token from .env file
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("Missing BOT_TOKEN. Add it to your .env file!")

bot = telebot.TeleBot(BOT_TOKEN)

# 50 MB Telegram Bot API limit in bytes
MAX_FILE_SIZE = 50 * 1024 * 1024


@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(
        message,
        "👋 **Welcome to Universal Video Downloader!**\n\n"
        "Send me a link from:\n"
        "• YouTube (Shorts & Videos)\n"
        "• TikTok\n"
        "• Instagram (Reels)\n"
        "• X (Twitter)\n"
        "• Facebook, Reddit, etc.\n\n"
        "⚡ *Just paste the link below!*",
        parse_mode="Markdown"
    )


@bot.message_handler(func=lambda msg: msg.text and msg.text.startswith(('http://', 'https://')))
def handle_link(message):
    url = message.text.strip()
    chat_id = message.chat.id

    # 1. Send processing status message
    status_msg = bot.reply_to(message, "⏳ Fetching and downloading video... Please wait.")

    # Unique filename using random ID to prevent collisions
    file_id = str(uuid.uuid4())[:8]
    output_template = f"downloads/{file_id}_%(title).50s.%(ext)s"
    os.makedirs("downloads", exist_ok=True)

    downloaded_file = None

    ydl_opts = {
        # Try best video + audio under 50MB, or fallback to best available mp4
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_template,
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
    }

    try:
        # 2. Extract and download with yt-dlp
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'Video')
            downloaded_file = ydl.prepare_filename(info)

            # Ensure file extension ends with .mp4 after merge
            if not os.path.exists(downloaded_file):
                base, _ = os.path.splitext(downloaded_file)
                if os.path.exists(f"{base}.mp4"):
                    downloaded_file = f"{base}.mp4"

        # 3. Check Telegram's 50MB limit
        if os.path.exists(downloaded_file):
            file_size = os.path.getsize(downloaded_file)

            if file_size > MAX_FILE_SIZE:
                bot.edit_message_text(
                    "❌ This video is larger than 50 MB. Telegram bots cannot upload files over 50 MB.",
                    chat_id=chat_id,
                    message_id=status_msg.message_id
                )
                return

            # Update status
            bot.edit_message_text(
                "🚀 Uploading video to Telegram...",
                chat_id=chat_id,
                message_id=status_msg.message_id
            )

            # 4. Send video to user
            with open(downloaded_file, 'rb') as video:
                bot.send_video(
                    chat_id,
                    video=video,
                    caption=f"🎬 **{title}**",
                    parse_mode="Markdown"
                )

            # Delete the status message once done
            bot.delete_message(chat_id, status_msg.message_id)

    except yt_dlp.utils.DownloadError:
        bot.edit_message_text(
            "❌ Failed to download. The link might be invalid, private, or age-restricted.",
            chat_id=chat_id,
            message_id=status_msg.message_id
        )
    except Exception as e:
        bot.edit_message_text(
            f"❌ An error occurred: {str(e)[:100]}",
            chat_id=chat_id,
            message_id=status_msg.message_id
        )
    finally:
        # 5. Always clean up files to keep your disk clean
        if downloaded_file and os.path.exists(downloaded_file):
            try:
                os.remove(downloaded_file)
            except Exception:
                pass


@bot.message_handler(func=lambda msg: True)
def handle_other(message):
    bot.reply_to(message, "Please send a valid video URL starting with `http://` or `https://`.", parse_mode="Markdown")


if __name__ == "__main__":
    print("🤖 Bot is active and listening for links...")
    bot.infinity_polling()