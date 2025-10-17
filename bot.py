from io import BytesIO
from datetime import datetime, timedelta
from flask import Flask, request # НАСТРОЙКА WEBHOOK ДЛЯ RENDER
import threading
import logging
import telebot
import requests
import json
import time
import os

# === НАСТРОЙКИ ===
WEBHOOK_URL = "https://tg-bot-daily-planner.onrender.com"
TELEGRAM_BOT_TOKEN = "8396602686:AAFfOqaDehOGf7Y3iom_j6VNxEGEmyOxIgU"
TIMEZONE_OFFSET = 3  # UTC+3 (Москва)
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
ADMIN_USER_ID = ["1287372767"] #в настройки: добавлять и удалять админов. Возможности админов и администрирования

# Работа с гистом с гитхаба (переносим БД туда)
GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# WEB-HOOK
app = Flask(__name__)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Состояния
user_awaiting_json_file = set()
user_awaiting_task_text = {}
user_awaiting_datetime = {}

CANCEL_ACTION_NAMES = {
    "cancel_task": "/task",
    "cancel_jsonin": "/jsonin",
}

# Автоматически формируем множество допустимых callback_data-действий для отмены
CANCEL_ACTIONS = set(CANCEL_ACTION_NAMES.keys())

# Текст оповещения о системной ошибке для пользователей
USER_DB_ERROR_MESSAGE = "⚠️ Произошла ошибка при работе с базой данных. Обратитесь, пожалуйста, к администраторам бота!"

def now_msk():
    return datetime.utcnow() + timedelta(hours=TIMEZONE_OFFSET)

# === УНИВЕРСАЛЬНАЯ ФУНКЦИЯ ДЛЯ КНОПКИ ОТМЕНЫ ===
def make_cancel_button(callback_data: str = "cancel_task") -> telebot.types.InlineKeyboardMarkup:
    """Создаёт inline-клавиатуру с кнопкой 'Cancel'."""
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Cancel", callback_data=callback_data))
    return markup

# === РАБОТА С ФАЙЛАМИ ===
def load_data(user_name: str, user_id: int, cmd: str):
    """Загружает данные из приватного Gist. Возвращает dict или None при ошибке."""
    if not GIST_ID or not GITHUB_TOKEN:
        logger.error("GIST_ID или GITHUB_TOKEN не заданы в переменных окружения.")
        return None

    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            gist_data = resp.json()
            for filename, file_info in gist_data["files"].items():
                if filename == "data.json":
                    content = file_info["content"]
                    if not content.strip():
                        return {}
                    try:
                        return json.loads(content)
                    except json.JSONDecodeError as e:
                        notify_admins_about_db_error(user_name, user_id, cmd, f"JSON decode error in Gist: {e}")
                        return None
            # Файл data.json не найден
            notify_admins_about_db_error(user_name, user_id, cmd, "Файл data.json не найден в Gist")
            return None
        else:

            notify_admins_about_db_error(user_name, user_id, cmd, f"GitHub API error: {resp.status_code} {resp.text}")
            return None
    except requests.RequestException as e:
        notify_admins_about_db_error(user_name, user_id, cmd, f"Network error loading Gist: {e}")
        return None
    except Exception as e:
        notify_admins_about_db_error(user_name, user_id, cmd, f"Unexpected error in load_data: {e}")
        return None

def save_data(data):
    """Сохраняет данные в приватный Gist."""
    if not GIST_ID or not GITHUB_TOKEN:
        logger.error("❌GIST_ID или GITHUB_TOKEN не заданы в переменных окружения.")
        return
    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    payload = {
        "files": {
            "data.json": {
                "content": json.dumps(data, ensure_ascii=False, indent=2)
            }
        }
    }
    try:
        resp = requests.patch(url, json=payload, headers=headers, timeout=10)
        if resp.status_code != 200:
            logger.error(f"❌Не удалось сохранить данные в Gist: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"❌Ошибка при сохранении данных в Gist: {e}")
        
"""def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_data(data):
    temp_file = DATA_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_file, DATA_FILE)"""

# === КОМАНДЫ АДМИНА ===
def notify_admins_about_db_error(user_name: str, user_id: str, command: str, error_details: str):
    """Отправляет всем админам уведомление о проблеме с БД."""
    message_to_admins = (
        f"‼️ Пользователь {user_name} (ID={user_id}) пытается выполнить команду /{command}, "
        f"но произошла ошибка при работе с Базой Данных!\n"
        f"Подробнее об ошибке:\n{error_details}"
    )
    logger.error(error_details)
    for admin_id in ADMIN_USER_ID:
        try:
            if user_name != "" and user_id != 0 and command != "":
                bot.send_message(admin_id, message_to_admins)
                bot.send_message(user_id, "⚠ Ошибка при работе с Базой Данных! Пожалуйста, обратитесь к админам.")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")

@bot.message_handler(commands=["jsonout"])
def jsonout_handler(message):
    user_name = message.from_user.first_name or "Пользователь"
    if str(message.from_user.id) not in ADMIN_USER_ID:
        bot.send_message(message.chat.id, "❌ Эта команда доступна только администратору.")
        return

    try:
        data = load_data(user_name, message.from_user.id, "jsonout")
        if not data:
            bot.send_message(message.chat.id, "⚠️ База данных ещё не создана.")
            return
        elif is_data_empty(data):
            bot.send_message(message.chat.id, "⚠️ База данных существует, но пока пуста.")
            return

        json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        bot.send_document(
            message.chat.id,
            document=BytesIO(json_bytes),
            visible_file_name="data.json",
            caption="📁 Текущая база данных"
        )

    except json.JSONDecodeError as e:
        error_details = f"Ошибка в JSON (строка {e.lineno}, колонка {e.colno}): {e.msg}"
        logger.error(f"❌ Ошибка разбора JSON из Gist: {error_details}")
        bot.send_message(
            message.chat.id,
            f"⚠️ База данных повреждена: файл не является валидным JSON.\nПодробности:\n```\n{error_details}\n```",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"❌ Ошибка в /jsonout: {e}")
        bot.send_message(message.chat.id, f"❌ Не удалось получить базу данных: {e}")

# Проверка БД на пустоту по смыслу (json с содержимым, но без задач)
def is_data_empty(data: dict) -> bool:
    """Проверяет, содержит ли data хоть одну задачу у любого пользователя."""
    if not data:
        return True
    for user_data in data.values():
        if isinstance(user_data, dict) and user_data.get("tasks"):
            # Если у кого-то есть хотя бы одна задача — не пусто
            return False
    return True

@bot.message_handler(commands=["jsonin"])
def jsonin_handler(message):
    user_name = message.from_user.first_name or "Пользователь"
    if str(message.from_user.id) not in ADMIN_USER_ID:
        try:
            bot.send_message(message.chat.id, "❌ Эта команда доступна только администратору.")
        except Exception as e:
            logger.error(f"❌Не удалось отправить сообщение: {e}")
        return

    main_msg = "Прикрепите файл с расширением .json с содержимым Базы Данных планов всех пользователей для бота."

    # Загружаем текущую БД из Gist
    try:
        data = load_data(message.from_user.name, message.from_user.id, "jsonin")
        if not data:
            bot.send_message(
                message.chat.id,
                main_msg + "\n⚠️ База данных ещё не создана.",
                reply_markup=make_cancel_button("cancel_jsonin")
            )
        elif is_data_empty(data):
            bot.send_message(
                message.chat.id,
                main_msg + "\n⚠️ База данных существует, но пока пуста.",
                reply_markup=make_cancel_button("cancel_jsonin")
            )
        else:
            # Отправляем текущую БД как файл
            json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            bot.send_document(
                message.chat.id,
                document=BytesIO(json_bytes),
                visible_file_name="data.json",
                caption=main_msg + "\n📁 Текущая база данных:",
                reply_markup=make_cancel_button("cancel_jsonin")
            )
    except Exception as e:
        logger.error(f"❌Ошибка при чтении БД в /jsonin: {e}")
        bot.send_message(
            message.chat.id,
            main_msg + "\n❌ Не удалось прочитать текущую базу данных.",
            reply_markup=make_cancel_button("cancel_jsonin")
        )

    # Важно: всегда входим в режим ожидания файла
    user_awaiting_json_file.add(str(message.from_user.id))

@bot.message_handler(content_types=["document"], func=lambda msg: str(msg.from_user.id) in user_awaiting_json_file)
def handle_json_file(msg):
    user_id = str(msg.from_user.id)
    chat_id = msg.chat.id
    if not msg.document:
        bot.send_message(chat_id, "⚠️Пожалуйста, отправьте именно файл.", reply_markup=make_cancel_button("cancel_jsonin"))
        return
    file_info = bot.get_file(msg.document.file_id)
    file_name = msg.document.file_name or ""
    if not file_name.lower().endswith(".json"):
        bot.send_message(chat_id, "⚠️Файл должен иметь расширение .json.", reply_markup=make_cancel_button("cancel_jsonin"))
        return
    try:
        downloaded_file = bot.download_file(file_info.file_path)
        json_content = json.loads(downloaded_file.decode("utf-8"))
        # Проверка: не пустой ли файл по смыслу?
        if is_data_empty(json_content):
            bot.send_message(
                chat_id,
                "⚠️ Загруженный файл — валидный JSON, но не содержит ни одной задачи.\n"
                "Файл не был применён.",
                reply_markup=make_cancel_button("cancel_jsonin")
            )
            return
        save_data(json_content)
        user_awaiting_json_file.discard(user_id)
        bot.send_message(chat_id, "✅ Файл успешно загружен и применён!")
    except json.JSONDecodeError as e:
        error_details = f"❌Ошибка в JSON (строка {e.lineno}, колонка {e.colno}): {e.msg}"
        logger.error(f"❌JSON decode error from user {msg.from_user.id}: {error_details}")
        bot.send_message(
            chat_id,
            f"❌ Некорректный JSON-файл.\nПодробности:\n{error_details}",
            reply_markup=make_cancel_button("cancel_jsonin")
        )
    except UnicodeDecodeError as e:
        logger.error(f"Unicode decode error from user {msg.from_user.id}: {e}")
        bot.send_message(chat_id, "❌Ошибка: файл не в кодировке UTF-8.", reply_markup=make_cancel_button("cancel_jsonin"))
    except Exception as e:
        logger.error(f"Unexpected error in handle_json_file: {e}", exc_info=True)
        bot.send_message(chat_id, f"❌Ошибка при обработке файла: {e}", reply_markup=make_cancel_button("cancel_jsonin"))
        
@bot.callback_query_handler(func=lambda call: call.data in CANCEL_ACTIONS)
def universal_cancel_handler(call):
    user_id = str(call.from_user.id)
    action = call.data
    command_name = CANCEL_ACTION_NAMES[action]

    # Определяем, находится ли пользователь в нужном режиме
    in_mode = False
    if action == "cancel_task":
        in_mode = (user_id in user_awaiting_task_text) or (user_id in user_awaiting_datetime)
    elif action == "cancel_jsonin":
        in_mode = user_id in user_awaiting_json_file

    if in_mode:
        # Выходим из режима
        if action == "cancel_task":
            user_awaiting_task_text.pop(user_id, None)
            user_awaiting_datetime.pop(user_id, None)
        elif action == "cancel_jsonin":
            user_awaiting_json_file.discard(user_id)

        # Отправляем сообщение в чат (не редактируем старое!)
        bot.send_message(call.message.chat.id, f"❌ Режим ввода {command_name} отменён.")
        # Подтверждаем нажатие кнопки (убираем "часики")
        bot.answer_callback_query(call.id)
    else:
        # Пользователь уже не в режиме → показываем всплывающее уведомление
        bot.answer_callback_query(
            call.id,
            f"Режим ввода команды {command_name} уже был отменён!",
            show_alert=False  # можно True, если хочешь модальное окно
        )

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def send_long_message(bot, chat_id, text):
    if not text.strip():
        return
    max_len = 4000
    for i in range(0, len(text), max_len):
        bot.send_message(chat_id, text[i:i + max_len])

def generate_example_datetime():
    now = now_msk()
    tomorrow = now.date() + timedelta(days=1)
    example_dt = datetime.combine(tomorrow, datetime.min.time()).replace(
        hour=now.hour, minute=now.minute
    )
    return example_dt.strftime("%Y-%m-%d %H:%M")

# === ОСНОВНЫЕ КОМАНДЫ ПОЛЬЗОВАТЕЛЯ ===
@bot.message_handler(commands=["info"])
def info_handler(message):
    user_id = str(message.from_user.id)
    is_admin = (user_id in ADMIN_USER_ID)

    text = "ℹ️ <b>Информация о боте «Ежедневник»</b>\n"
    text += "<b>Для всех пользователей (кроме админов, для них - свои доп.-команды):</b>\n"
    text += "<i>Для получения информации о возможностях для админов обратитесь к действующим админам</i>\n\n"
    text += "• /start — <i>начать работу с ботом</i>\n"
    text += "• /info — <i>получить подробную справку</i>\n"
    text += "• /task — <i>добавить новую задачу</i>\n"
    text += "• <i>Задачи напоминаются автоматически:</i>\n"
    text += "  – <i>за день в 13:00 по МСК,</i>\n"
    text += "  – <i>или за 12 часов до начала.</i>\n\n"

    if is_admin:
        text += "<b>Для администраторов:</b>\n"
        text += "• /jsonout — <i>получить текущуюa БД в виде файла</i>\n"
        text += "• /jsonin — <i>загрузить новую БД из файла</i>\n"
        text += "<i>⚠️ Все операции с БД требуют корректного JSON-формата.</i>\n"

    bot.send_message(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(commands=["start"])
def start_handler(message):
    user_id = str(message.from_user.id)
    user_name = message.from_user.first_name or "Пользователь"

    for attempt in range(3):  # до 3 попыток при конфликте
        # 1. Читаем СВЕЖУЮ БД из Gist
        data = load_data(user_name, message.from_user.id, "start")

        # bot.send_message(message.chat.id, "🔍 Текущая БД:\n" + json.dumps(data, ensure_ascii=False, indent=2))

        # 2. Если пользователь уже есть — выходим
        if user_id in data:
            bot.send_message(message.chat.id, f"С возвращением, {user_name}! Готов работать.")
            return

        # 3. Добавляем пользователя
        data[user_id] = {
            "user_name": user_name,
            "chat_id": str(message.chat.id),
            "tasks": []
        }

        # 4. Сохраняем ВСЮ БД (включая новых пользователей)
        save_data(data)

        # 5. Проверяем, что всё сохранилось
        data_check = load_data(user_name, message.from_user.id, "start")
        if user_id in data_check:
            bot.send_message(
                message.chat.id,
                f"Привет, {user_name}! 👋\n"
                "Я — твой личный ежедневник в Telegram.\n"
                "Используй команды:\n"
                "/start - запустить бота\n"
                "/task — добавить задачу\n"
            )
            return

        # Если не сохранилось — повторяем цикл (возможно, кто-то перезаписал)
        logger.warning(f"Попытка {attempt + 1}: пользователь {user_id} не сохранился в БД")

    # Если все попытки провалились
    bot.send_message(message.chat.id, "⚠️ Не удалось инициализировать профиль. Попробуйте позже.")
    logger.error(f"❌ Не удалось инициализировать пользователя {user_id} после 3 попыток")

@bot.message_handler(commands=["task"])
def task_handler(message):
    user_id = str(message.from_user.id)
    user_name = message.from_user.first_name or "Пользователь"
    data = load_data(user_name, message.from_user.id, "task")
    text = message.text[6:].strip()
    if not text:
        bot.send_message(
            message.chat.id,
            "Введите текст задачи (просто напишите его, без команды):",
            reply_markup=make_cancel_button("cancel_task")
        )
        user_awaiting_task_text[user_id] = True
    else:
        user_awaiting_datetime[user_id] = text
        example = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        bot.send_message(
            message.chat.id,
            f"Укажи дату и время в формате: ГГГГ-ММ-ДД ЧЧ:ММ\n"
            f"Пример:\n{example}\n"
            f"Или нажми Cancel ниже.",
            reply_markup=make_cancel_button("cancel_task")
        )

@bot.message_handler(func=lambda msg: str(msg.from_user.id) in user_awaiting_task_text)
def task_text_input(msg):
    user_id = str(msg.from_user.id)
    user_name = msg.from_user.first_name or "Пользователь"
    data = load_data(user_name, user_id, "task")
    user_id = str(msg.from_user.id)
    text = msg.text.strip()
    if not text:
        bot.send_message(msg.chat.id, "Текст не может быть пустым. Попробуй снова.")
        return
    user_awaiting_datetime[user_id] = text
    del user_awaiting_task_text[user_id]
    example = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    bot.send_message(
        msg.chat.id,
        f"Укажи дату и время в формате: ГГГГ-ММ-ДД ЧЧ:ММ\n"
        f"Пример:\n{example}\n"
        f"Или нажми inline-кнопку Cancel ниже.",
        reply_markup=make_cancel_button("cancel_task")
    )

@bot.message_handler(func=lambda message: str(message.from_user.id) in user_awaiting_datetime)
def datetime_input_handler(message):
    user_id = str(message.from_user.id)
    user_name = message.from_user.first_name or "Пользователь"
    data = load_data(user_name, user_id, "task")
    chat_id = message.chat.id
    user_name = message.from_user.first_name or "Пользователь"
    datetime_str = message.text.strip()
    try:
        task_datetime = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M")
    except ValueError:
        example = generate_example_datetime()
        bot.send_message(
            chat_id,
            f"Неверный формат даты.\n"
            f"Используй: ГГГГ-ММ-ДД ЧЧ:ММ\n"
            f"Пример:\n{example}",
            reply_markup=make_cancel_button("cancel_task")
        )
        return
    text = user_awaiting_datetime[user_id]
    data = load_data(user_name, message.from_user.id, "task")
    if user_id not in data:
        bot.send_message(chat_id, "Сначала отправь /start")
        return
    new_task = {
        "text": text,
        "datetime": task_datetime.isoformat(),
        "status": "waiting",
        "reminded": False,
        "created_at": now_msk().isoformat()
    }
    data[user_id]["tasks"].append(new_task)
    save_data(data)
    del user_awaiting_datetime[user_id]
    bot.send_message(
        chat_id,
        f"✅ Задача сохранена!\n"
        f"{text}\n"
        f"📅 {task_datetime.strftime('%d.%m.%Y в %H:%M')}"
    )

# === НАПОМИНАНИЯ ===
def check_and_send_reminders(bot, user_id, chat_id, data):
    now = now_msk()
    tasks_to_remind = []
    for task in data[user_id]["tasks"]:
        if task.get("status") != "waiting" or task.get("reminded", True):
            continue
        try:
            task_time = datetime.fromisoformat(task["datetime"])
        except:
            continue
        if (task_time.date() == (now.date() + timedelta(days=1))) and now.hour == 13:
            tasks_to_remind.append(task)
        elif (task_time - now).total_seconds() <= 12 * 3600 and task.get("status") != "overdue":
            tasks_to_remind.append(task)
    if not tasks_to_remind:
        return
    lines = []
    for task in tasks_to_remind:
        dt_str = datetime.fromisoformat(task["datetime"]).strftime('%d.%m.%Y в %H:%M')
        lines.append(f"🔔 {task['text']}\n📅 {dt_str}")
        task["reminded"] = True
    save_data(data)
    send_long_message(bot, chat_id, "\n\n".join(lines).strip())

def reminder_daemon():
    while True:
        try:
            data = load_data("", 0, "")
            for user_id, user_data in data.items():
                check_and_send_reminders(bot, user_id, user_id, data)
        except Exception as e:
            print(f"Reminder error: {e}")
        time.sleep(600)  # 10 минут — раскомментировать при запуске на сервере

@app.route('/' + TELEGRAM_BOT_TOKEN, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    else:
        return 'Invalid content type', 403

@app.route('/')
def index():
    return 'Bot is running.'

if __name__ == '__main__':
    # Запускаем напоминания в фоновом потоке
    reminder_thread = threading.Thread(target=reminder_daemon, daemon=True)
    reminder_thread.start()

    # Устанавливаем webhook
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL + '/' + TELEGRAM_BOT_TOKEN)

    # Запускаем Flask-сервер
    port = int(os.environ.get('PORT', 5000))
    app.run(host="0.0.0.0", port=port)
