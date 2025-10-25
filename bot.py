from io import BytesIO
from datetime import datetime, timedelta
from collections import defaultdict
from flask import Flask, request # НАСТРОЙКА WEBHOOK ДЛЯ RENDER
import threading
import logging
import telebot
import requests
import json
import html
import time
import os

# === НАСТРОЙКИ ===

# Bз переменных окружения
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_USER_ID_RAW = os.getenv("ADMIN_USER_ID") #в настройки: добавлять и удалять админов. Возможности админов и администрирования

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("❌ Переменная окружения TELEGRAM_BOT_TOKEN не задана!")
if not WEBHOOK_URL:
    raise RuntimeError("❌ Переменная окружения WEBHOOK_URL не задана!")

# Преобразуем ADMIN_USER_ID в список (поддерживаем несколько админов через запятую)
if ADMIN_USER_ID_RAW:
    # Убираем пробелы и разбиваем по запятым
    ADMIN_USER_ID = [uid.strip() for uid in ADMIN_USER_ID_RAW.split(",") if uid.strip()]
else:
    ADMIN_USER_ID = []
    logger.warning("⚠️ Переменная окружения ADMIN_USER_ID не задана. Админ-команды будут недоступны.")

TIMEZONE_OFFSET = 3  # UTC+3 (Москва)
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Админ-бот (только для отправки сообщений админам)
admin_bot_token = os.getenv("ADMIN_BOT_TOKEN")
if not admin_bot_token:
    logger.warning("⚠️ ADMIN_BOT_TOKEN не задан. Админ-уведомления будут отключены.")
    admin_bot = None
else:
    admin_bot = telebot.TeleBot(admin_bot_token)

# Бот-напоминалка (только для отправки напоминаний)
reminder_bot_token = os.getenv("REMINDER_BOT_TOKEN")
if not reminder_bot_token:
    logger.warning("⚠️ REMINDER_BOT_TOKEN не задан. Напоминания будут через основного бота.")
    reminder_bot = None
else:
    reminder_bot = telebot.TeleBot(reminder_bot_token)

# ПРОВЕРИТЬ, ЧТО АДМИНОВ МОЖЕТ БЫТЬ МНОГО!!!
# ПРОВЕРИТЬ, ЧТО АДМИНЫ ТЕ ЖЕ!!!
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# Работа с гистом с гитхаба (переносим БД туда)
GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# WEB-HOOK
app = Flask(__name__)

# Защита от спама: {user_id: [timestamp1, timestamp2, ...]}
user_request_timestamps = defaultdict(list)
REQUEST_LIMIT = 10  # максимум 10 команд
REQUEST_WINDOW = 60 # за 60 секунд

# Logging
# Настраиваем ТОЛЬКО свой логгер
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Добавляем обработчик, если его ещё нет (например, в консоль)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(levelname)s:%(name)s: %(message)s'))
    logger.addHandler(handler)

# Подавляем шум от urllib3 и requests
# Чтобы вывод логов в Рендере был ПОНЯТНЫМ
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("telebot").setLevel(logging.WARNING)

# Состояния режимов ввода команд
user_awaiting_json_file = set()
user_awaiting_task_text = {}
user_awaiting_datetime = {}
user_awaiting_feedback = set()
user_awaiting_daytasks_date = set()
user_awaiting_weekbydate_input = set()
user_awaiting_settings_input = {}  # {user_id: "urgent_threshold" или "daily_hour"}
user_in_settings_menu = set()  # кто сейчас в меню /settings

CANCEL_ACTION_NAMES = {
    "cancel_task": "/task",
    "cancel_jsonin": "/jsonin",
    "cancel_feedback": "/feedback",
    "cancel_daytasks": "/daytasks",
    "cancel_weekbydate": "/weekbydate",
    "settings_cancel": "/settings",
    "cancel_settings_urgent_threshold": "/settings",
    "cancel_settings_daily_hour": "/settings"
}

# Автоматически формируем множество допустимых callback_data-действий для отмены
CANCEL_ACTIONS = set(CANCEL_ACTION_NAMES.keys())

# Текст оповещения о системной ошибке для пользователей
USER_DB_ERROR_MESSAGE = "⚠️ Произошла ошибка при работе с базой данных. Обратитесь, пожалуйста, к администраторам бота!"

def now_msk():
    return datetime.utcnow() + timedelta(hours=TIMEZONE_OFFSET)

# === УНИВЕРСАЛЬНАЯ ФУНКЦИЯ ДЛЯ КНОПКИ ОТМЕНЫ ===
def make_cancel_button(callback_data: str) -> telebot.types.InlineKeyboardMarkup:
    """Создаёт inline-клавиатуру с кнопкой 'Cancel'."""
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Cancel", callback_data=callback_data))
    return markup

# === РАБОТА С ФАЙЛАМИ ===
def load_data(user_name: str, user_id: int, cmd: str):
    """Загружает данные из приватного Gist. Возвращает dict или None при ошибке."""
    if not GIST_ID or not GITHUB_TOKEN:
        logger.critical("GIST_ID или GITHUB_TOKEN не заданы в переменных окружения.")
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
            logger.critical(f"❌Не удалось сохранить данные в Gist: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.critical(f"❌Ошибка при сохранении данных в Gist: {e}")
        
# === КОМАНДЫ АДМИНА ===
def notify_admins_about_db_error(user_name: str, user_id: str, command: str, error_details: str):
    message_to_admins = (
        f"‼️ Пользователь <b>{user_name} (ID={user_id})</b> пытается выполнить команду /{command}, "
        f"но произошла ошибка при работе с Базой Данных!"
        f"Подробнее об ошибке: {error_details}"
    )
    logger.error(error_details)

    if not ADMIN_USER_ID:
        logger.warning("Нет админов для уведомления (ADMIN_USER_ID пуст)")
        return

    for admin_id_str in ADMIN_USER_ID:
        try:
            admin_id = int(admin_id_str.strip())
            bot.send_message(admin_id, message_to_admins, parse_mode="HTML")
        except ValueError:
            logger.error(f"Некорректный ID админа: '{admin_id_str}'")
        except telebot.apihelper.ApiTelegramException as e:
            if "chat not found" in str(e):
                logger.critical(f"❌ Админ {admin_id_str} не начал диалог с ботом! Напишите /start.")
            else:
                logger.critical(f"Ошибка Telegram API для админа {admin_id_str}: {e}")
        except Exception as e:
            logger.critical(f"Неизвестная ошибка при отправке админу {admin_id_str}: {e}")

@bot.message_handler(commands=["jsonout"])
def jsonout_handler(message):
    if is_rate_limited(str(message.from_user.id)):
        bot.send_message(
            message.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    if message.chat.type != "private":
        stop_command_in_group(message.chat.id, message.from_user.first_name or "Пользователь")
        return
    
    user_name = message.from_user.first_name or "Пользователь"

    try:
        data = load_data(user_name, message.from_user.id, "jsonout")
        text = ""
        if not data:
            text += "⚠️ Корректная база данных ещё не создана.\n"
        elif is_data_empty(data):
            text += "⚠️ База данных существует, но пока пуста.\n"

        json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

        if str(message.from_user.id) not in ADMIN_USER_ID:
            bot.send_message(message.chat.id, "❌ Эта команда доступна только администратору.")
            return

        text += "📁 Текущая база данных"
        bot.send_document(
            message.chat.id,
            document=BytesIO(json_bytes),
            visible_file_name="data.json",
            caption=text
        )

    except json.JSONDecodeError as e:
        error_details = f"Ошибка в JSON (строка {e.lineno}, колонка {e.colno}): {e.msg}"
        logger.critical(f"❌ Ошибка разбора JSON из Gist: {error_details}")
        bot.send_message(
            message.chat.id,
            f"⚠️ База данных повреждена: файл не является валидным JSON.\nПодробности:\n```\n{error_details}\n```",
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.critical(f"❌ Ошибка в /jsonout: {e}")
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
    if is_rate_limited(str(message.from_user.id)):
        bot.send_message(
            message.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    if message.chat.type != "private":
        stop_command_in_group(message.chat.id, message.from_user.first_name or "Пользователь")
        return
    user_name = message.from_user.first_name or "Пользователь"
    main_msg = "Прикрепите файл с расширением .json с содержимым Базы Данных планов всех пользователей для бота.\n"

    # Загружаем текущую БД из Gist
    try:
        data = load_data(user_name, message.chat.id, "jsonin")
        if not data:
            main_msg += "⚠️ Корректная база данных ещё не создана.\n"
        elif is_data_empty(data):
            main_msg += "⚠️ База данных существует, но пока пуста.\n"
        # Отправляем текущую БД как файл, даже если она пуста
        # (ведь там могут быть айдишники юзеров...)
        json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

        if str(message.from_user.id) not in ADMIN_USER_ID:
            try:
                bot.send_message(message.chat.id, "❌ Эта команда доступна только администратору.")
            except Exception as e:
                logger.critical(f"❌Не удалось отправить сообщение: {e}")
            return
        
        main_msg += "📁 Текущая база данных:"
        bot.send_document(
            message.chat.id,
            document=BytesIO(json_bytes),
            visible_file_name="data.json",
            caption=main_msg, 
            reply_markup=make_cancel_button("cancel_jsonin")
        )
    except Exception as e:
        logger.critical(f"❌Ошибка при чтении БД в /jsonin: {e}")
        bot.send_message(
            message.chat.id,
            main_msg + f"\n❌ Не удалось прочитать текущую базу данных: {e}",
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
        logger.critical(f"❌JSON decode error from user {msg.from_user.id}: {error_details}")
        bot.send_message(
            chat_id,
            f"❌ Некорректный JSON-файл.\nПодробности:\n{error_details}",
            reply_markup=make_cancel_button("cancel_jsonin")
        )
    except UnicodeDecodeError as e:
        logger.critical(f"Unicode decode error from user {msg.from_user.id}: {e}")
        bot.send_message(chat_id, "❌Ошибка: файл не в кодировке UTF-8.", reply_markup=make_cancel_button("cancel_jsonin"))
    except Exception as e:
        logger.critical(f"Unexpected error in handle_json_file: {e}", exc_info=True)
        bot.send_message(chat_id, f"❌Ошибка при обработке файла: {e}", reply_markup=make_cancel_button("cancel_jsonin"))

# ФУНКЦИЯ ОБРАБОТКИ КНОПОК
@bot.callback_query_handler(func=lambda call: True)  # ЛОВИМ ВСЁ
def universal_callback_handler(call):
    user_id = str(call.from_user.id)
    action = call.data

    # --- Обработка отмен ---
    if action in CANCEL_ACTIONS:
        command_name = CANCEL_ACTION_NAMES[action]
        in_mode = False
        if action == "cancel_task":
            in_mode = (user_id in user_awaiting_task_text) or (user_id in user_awaiting_datetime)
        elif action == "cancel_jsonin":
            in_mode = user_id in user_awaiting_json_file
        elif action == "cancel_feedback":
            in_mode = user_id in user_awaiting_feedback
        elif action == "cancel_daytasks":
            in_mode = user_id in user_awaiting_daytasks_date
        elif action == "cancel_weekbydate":
            in_mode = user_id in user_awaiting_weekbydate_input
        elif action == "settings_cancel":
            in_mode = user_id in user_in_settings_menu
        elif action in ("cancel_settings_urgent_threshold", "cancel_settings_daily_hour"):
            in_mode = user_id in user_awaiting_settings_input

        if in_mode:
            # Выход из режима
            if action == "cancel_task":
                user_awaiting_task_text.pop(user_id, None)
                user_awaiting_datetime.pop(user_id, None)
            elif action == "cancel_jsonin":
                user_awaiting_json_file.discard(user_id)
            elif action == "cancel_feedback":
                user_awaiting_feedback.discard(user_id)
            elif action == "cancel_daytasks":
                user_awaiting_daytasks_date.discard(user_id)
            elif action == "cancel_weekbydate":
                user_awaiting_weekbydate_input.discard(user_id)
            elif action == "settings_cancel":
                user_in_settings_menu.discard(user_id)
            elif action in ("cancel_settings_urgent_threshold", "cancel_settings_daily_hour"):
                user_awaiting_settings_input.pop(user_id, None)

            bot.send_message(call.message.chat.id, f"❌ Режим ввода {command_name} отменён.")
            bot.answer_callback_query(call.id)
        else:
            bot.answer_callback_query(call.id, f"Режим {command_name} уже отменён!", show_alert=False)
        return

    # --- Обработка настроек ---
    if action.startswith("settings_"):
        if action == "settings_cancel":
            # Это уже обработано выше, но на случай дубля
            bot.answer_callback_query(call.id)
            return

        # Проверка: пользователь в меню настроек?
        if user_id not in user_in_settings_menu:
            bot.answer_callback_query(call.id, "Меню /settings закрыто.", show_alert=False)
            return

        # Загрузка данных
        data = load_data(call.from_user.first_name, call.message.from_user.id, "settings")
        if data is None or user_id not in data:
            bot.send_message(call.message.chat.id, "Сначала отправьте /start")
            bot.answer_callback_query(call.id)
            return

        # Определяем параметр
        if action == "settings_urgent_threshold":
            param_name = "urgent_threshold"
            current_val = data.get("settings", {}).get("urgent_threshold_hours", 12)
            prompt = f"Введите новый порог срочности (в часах).\nТекущее значение: {current_val}\nДопустимо: от 1 до 168."
        elif action == "settings_daily_hour":
            param_name = "daily_hour"
            current_val = data.get("settings", {}).get("daily_reminder_hour", 6)
            prompt = f"Введите час ежедневного напоминания (по МСК).\nТекущее значение: {current_val}\nДопустимо: от 0 до 23."
        else:
            bot.answer_callback_query(call.id, "⚠️ Неизвестная настройка!", show_alert=True)
            return

        # Переход в режим ввода значения
        user_awaiting_settings_input[user_id] = param_name
        user_in_settings_menu.discard(user_id)  # вышли из меню
        bot.send_message(
            call.message.chat.id,
            prompt,
            reply_markup=make_cancel_button(f"cancel_settings_{param_name}")
        )
        bot.answer_callback_query(call.id)
        return

    # --- Если action неизвестен ---
    bot.answer_callback_query(call.id, "⚠️ Неизвестное действие", show_alert=True)

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===

# против ддос-атаки
def is_rate_limited(user_id: str) -> bool:
    now = time.time()
    # Удаляем устаревшие запросы (старше REQUEST_WINDOW секунд)
    user_request_timestamps[user_id] = [
        ts for ts in user_request_timestamps[user_id] if now - ts < REQUEST_WINDOW
    ]
    # Проверяем, превышен ли лимит
    if len(user_request_timestamps[user_id]) >= REQUEST_LIMIT:
        return True
    # Добавляем текущий запрос
    user_request_timestamps[user_id].append(now)
    return False

# против ддос-атаки
def cleanup_old_requests():
    while True:
        time.sleep(3600)  # раз в час
        now = time.time()
        to_delete = []
        for user_id, timestamps in user_request_timestamps.items():
            # Оставляем только активные записи
            user_request_timestamps[user_id] = [ts for ts in timestamps if now - ts < REQUEST_WINDOW]
            if not user_request_timestamps[user_id]:
                to_delete.append(user_id)
        for user_id in to_delete:
            del user_request_timestamps[user_id]

def get_sorted_tasks_on_date(data: dict, user_id: str, target_date: datetime.date, logger = None) -> list:
    """Возвращает отсортированный по времени список СТРОК с задачами на указанную дату."""
    raw_tasks = []
    for task in data.get(user_id, {}).get("tasks", []):
        if (task.get("status") == "completed") and (task.get("status") != True):
            # logger.error(f"status = {task.get('status')}")
            continue
        try:
            task_dt = datetime.fromisoformat(task["datetime"])
            # logger.error(f"date1 = {task_dt.date()}, date2 = {target_date}")
            if task_dt.date() == target_date:
                raw_tasks.append(task)
        except (ValueError, KeyError) as e:
            # logger.error(f"Error!: {e}")
            continue
    # Сортируем по времени
    raw_tasks.sort(key=lambda t: datetime.fromisoformat(t["datetime"]))
    # Преобразуем в строки
    return [f"• {task['text']} <b>({datetime.fromisoformat(task['datetime']).strftime('%H:%M')})</b>" for task in raw_tasks]

@bot.message_handler(func=lambda msg: str(msg.from_user.id) in user_awaiting_settings_input)
def settings_value_input(msg):
    user_id = str(msg.from_user.id)
    chat_id = msg.chat.id
    param = user_awaiting_settings_input[user_id]
    
    try:
        value = int(msg.text.strip())
    except ValueError:
        bot.send_message(
            chat_id,
            "❌ Введите целое число.",
            reply_markup=make_cancel_button(f"cancel_settings_{param}")
        )
        return

    # Загружаем данные
    data = load_data(msg.from_user.first_name, chat_id, "settings")
    if data is None or user_id not in data:
        bot.send_message(chat_id, "Сначала отправьте /start")
        user_awaiting_settings_input.pop(user_id, None)
        return

    # Валидация и сохранение
    valid = False
    if param == "urgent_threshold":
        if 1 <= value <= 168:
            data[user_id]["settings"]["urgent_threshold_hours"] = value
            valid = True
            success_msg = f"✅ Порог срочности установлен: {value} часов."
        else:
            bot.send_message(
                chat_id,
                "❌ Значение должно быть от 1 до 168.",
                reply_markup=make_cancel_button(f"cancel_settings_{param}")
            )
    elif param == "daily_hour":
        if 0 <= value <= 23:
            data[user_id]["settings"]["daily_reminder_hour"] = value
            valid = True
            success_msg = f"✅ Ежедневное напоминание будет приходить в {value}:00 по МСК."
        else:
            bot.send_message(
                chat_id,
                "❌ Час должен быть от 0 до 23.",
                reply_markup=make_cancel_button(f"cancel_settings_{param}")
            )

    if valid:
        save_data(data)
        bot.send_message(chat_id, success_msg)

        # ВАЖНО: не удаляем из user_in_settings_menu!
        # Но удаляем из режима ввода значения
        user_awaiting_settings_input.pop(user_id, None)

        # Повторно показываем меню настроек
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton("⏳ Порог срочности", callback_data="settings_urgent_threshold")
        )
        markup.add(
            telebot.types.InlineKeyboardButton("🕒 Ежедневное напоминание", callback_data="settings_daily_hour")
        )
        markup.add(
            telebot.types.InlineKeyboardButton("❌ Отмена / OK", callback_data="settings_cancel")
        )
        bot.send_message(
            chat_id,
            "⚙️ Выберите параметр для настройки:",
            reply_markup=markup
        )
        # Убедимся, что пользователь всё ещё в меню
        user_in_settings_menu.add(user_id)

def send_long_message(bot_instance, chat_id, text, parse_mode=None):
    if not text.strip():
        return
    max_len = 4000
    for i in range(0, len(text), max_len):
        bot_instance.send_message(chat_id, text[i:i + max_len], parse_mode=parse_mode)

def generate_example_datetime():
    now = now_msk()
    tomorrow = now.date() + timedelta(days=1)
    example_dt = datetime.combine(tomorrow, datetime.min.time()).replace(
        hour=now.hour, minute=now.minute
    )
    return example_dt.strftime("%Y-%m-%d %H:%M")

def get_tasks_on_date(data: dict, user_id: str, target_date: datetime.date) -> list:
    """Возвращает список строк с задачами на указанную дату."""
    tasks_on_date = []
    if user_id not in data:
        return tasks_on_date
    for task in data[user_id]["tasks"]:
        if task.get("status") == "completed":
            continue
        try:
            task_dt = datetime.fromisoformat(task["datetime"])
            if task_dt.date() == target_date:
                formatted_time = task_dt.strftime("%H:%M")
                tasks_on_date.append(f"• {task['text']} ({formatted_time})")
        except (ValueError, KeyError):
            continue
    return tasks_on_date

def stop_command_in_group(chat_id, user_name):
    bot.send_message(chat_id, f"⚠️ Извините, <b>{user_name}</b>, бот не работает в группах!", parse_mode="HTML")

def generate_today_date():
    now = now_msk()
    today = now.date()
    example_dt = datetime.combine(today, datetime.min.time()).replace(
        hour=now.hour, minute=now.minute
    )
    return example_dt.strftime("%Y-%m-%d %H:%M")

# === ОСНОВНЫЕ КОМАНДЫ ПОЛЬЗОВАТЕЛЯ ===
@bot.message_handler(commands=["start"])
def start_handler(message):
    if is_rate_limited(str(message.from_user.id)):
        bot.send_message(
            message.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    user_id = str(message.from_user.id)
    user_name = message.from_user.first_name or "Пользователь"
    if message.chat.type != "private":
        stop_command_in_group(message.chat.id, message.from_user.first_name or "Пользователь")
        return

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
            "tasks": [],
            "settings": {
                "urgent_threshold_hours": 12,
                "daily_reminder_hour": 6
            }
        }

        # 4. Сохраняем ВСЮ БД (включая новых пользователей)
        save_data(data)

        # 5. Проверяем, что всё сохранилось
        data_check = load_data(user_name, message.from_user.id, "start")
        if user_id in data_check:
            info_handler(message)
            notify_admins_about_new_user(user_name, user_id, str(message.chat.id))
            return

        # Если не сохранилось — повторяем цикл (возможно, кто-то перезаписал)
        logger.warning(f"Попытка {attempt + 1}: пользователь {user_id} не сохранился в БД")

    # Если все попытки провалились
    bot.send_message(message.chat.id, "⚠️ Не удалось инициализировать профиль. Попробуйте позже.")
    logger.critical(f"❌ Не удалось инициализировать пользователя {user_id} после 3 попыток")

@bot.message_handler(commands=["info"])
def info_handler(message):
    if is_rate_limited(str(message.from_user.id)):
        bot.send_message(
            message.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    user_id = str(message.from_user.id)
    if message.chat.type != "private":
        command_in_group(user_name)
        return
    is_admin = (user_id in ADMIN_USER_ID)

    text = "ℹ️ <b>Информация о боте «Ежедневник»</b>\n\n"
    text += "<b>Команды для всех пользователей (кроме админов, для них - свои доп.-команды):</b>\n"
    text += "<i>Для получения информации о возможностях для админов обратитесь к действующим админам</i>\n\n"
    text += "• /start — <i>начать работу с ботом</i>\n"
    text += "• /info — <i>получить подробную справку</i>\n"
    text += "• /feedback — <i>отправить сообщение админам</i>\n"
    text += "• /task — <i>добавить новую задачу</i>\n"
    text += "• <i>Задачи напоминаются автоматически:</i>\n"
    text += "  – <i>за день в 13:00 по МСК,</i>\n"
    text += "  – <i>или за 12 часов до начала.</i>\n"
    text += "  – <i>позже можно будет настраивать.</i>\n"
    text += "• /settings — <i>настроить напоминания (/start = сброс!)</i>\n"
    text += "• /overdue — <i>показать все просроченные задачи</i>\n"
    text += "• /daytasks — <i>Посмотреть все задачи на указанную дату</i>\n"
    text += "• /today — <i>показать задачи на сегодня</i>\n"
    text += "• /tomorrow — <i>показать задачи на завтра</i>\n"
    text += "• /week — <i>показать задачи на текущую неделю</i>\n"
    text += "• /weekbydate — <i>показать задачи на неделю по дате</i>\n\n"
    text += "<i><b>P.s.</b>: при обновлении бота админом команды могут притормаживать (в пределах ~1-2 минут).</i>\n"
    text += "<i>Если бот не реагирует, попробуйте повторный запрос или другую команду.</i>\n"
    text += "<i>• Также иногда могут быть проблемы с Базой Данных при обновлениях.</i>\n"
    text += "<i>• В этом случае вы можете связаться с админами (/feedback) или просто подождать. При любых действиях, вызывающих ошибку, информация передается админам автоматически.</i>\n\n"

    if is_admin:
        text += "<b>Команды для администраторов:</b>\n"
        text += "• /jsonout — <i>получить текущуюa БД в виде файла</i>\n"
        text += "• /jsonin — <i>загрузить новую БД из файла</i>\n"
        text += "<i>⚠️ Все операции с БД требуют корректного JSON-формата.</i>\n"

    bot.send_message(message.chat.id, text, parse_mode="HTML")

@bot.message_handler(commands=["feedback"])
def feedback_handler(message):
    if is_rate_limited(str(message.from_user.id)):
        bot.send_message(
            message.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    user_id = str(message.from_user.id)
    if message.chat.type != "private":
        stop_command_in_group(message.chat.id, message.from_user.first_name or "Пользователь")
        return
    bot.send_message(
        message.chat.id,
        "• Напишите ваше сообщение админам. Это может быть жалоба, пожелание или благодарность.\n"
        "• Если вы хотите, чтобы с вами связались — укажите это в вашем сообщении.\n"
        "• Если вы хотите, чтобы с вами связались вне Telegram или вы хотите указать часы для связи — укажите это дополнительно.",
        reply_markup=make_cancel_button("cancel_feedback")
    )
    user_awaiting_feedback.add(user_id)

@bot.message_handler(func=lambda msg: str(msg.from_user.id) in user_awaiting_feedback)
def handle_feedback_message(msg):
    user_id = str(msg.from_user.id)
    user_name = msg.from_user.first_name or "Пользователь"
    feedback_text = msg.text.strip()

    if not feedback_text:
        bot.send_message(
            msg.chat.id,
            "Сообщение не может быть пустым. Пожалуйста, напишите что-нибудь.",
            reply_markup=make_cancel_button("cancel_feedback")
            )
        return

    # Формируем сообщение для админов
    admin_message = (
        f"📩 Пользователь <b>{user_name} (ID={user_id})</b> отправил фидбек:\n\n"
        f"{feedback_text}"
    )

    # Рассылаем всем админам
    success_count = 0
    for admin_id_str in ADMIN_USER_ID:
        try:
            admin_id = int(admin_id_str)  # всегда работаем с int
            bot.send_message(admin_id, message_to_admins, parse_mode="HTML")
        except ValueError:
            logger.error(f"Некорректный ADMIN_USER_ID: {admin_id_str}")
        except Exception as e:
            logger.critical(f"Не удалось отправить уведомление админу {admin_id_str}: {e}")

    # Подтверждаем пользователю
    if success_count > 0:
        bot.send_message(msg.chat.id, "Спасибо. Ваше сообщение отправлено админам бота.")
    else:
        bot.send_message(msg.chat.id, "⚠️ Не удалось отправить сообщение. Попробуйте позже.")

    # Выходим из режима ожидания
    user_awaiting_feedback.discard(user_id)

@bot.message_handler(commands=["overdue"])
def overdue_handler(message):
    if is_rate_limited(str(message.from_user.id)):
        bot.send_message(
            message.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    if message.chat.type != "private":
        stop_command_in_group(message.chat.id, message.from_user.first_name or "Пользователь")
        return

    user_id = str(message.from_user.id)
    user_name = message.from_user.first_name or "Пользователь"
    try:
        data = load_data(user_name, message.from_user.id, "overdue")
    except Exception as e:
        logger.critical(f"Ошибка загрузки БД в /overdue: {e}")
        bot.send_message(message.chat.id, "⚠️ Не удалось загрузить задачи. Попробуйте позже.")
        return

    if user_id not in data:
        bot.send_message(message.chat.id, "Сначала отправьте /start")
        return

    now = now_msk()
    overdue_tasks = []

    for task in data[user_id]["tasks"]:
        if task.get("status") != "waiting":
            continue
        try:
            task_time = datetime.fromisoformat(task["datetime"])
            if task_time < now:
                overdue_tasks.append(task)
        except (ValueError, KeyError):
            continue

    # Сортируем по времени (от старых к новым)
    overdue_tasks.sort(key=lambda t: datetime.fromisoformat(t["datetime"]))

    if not overdue_tasks:
        bot.send_message(message.chat.id, "✅ Нет просроченных задач.")
    else:
        lines = []
        for task in overdue_tasks:
            dt_str = datetime.fromisoformat(task["datetime"]).strftime('%d.%m.%Y в %H:%M')
            lines.append(f"• {task['text']} ({dt_str})")
        full_message = "⚠️ Просроченные задачи:\n\n" + "\n".join(lines)
        send_long_message(bot, message.chat.id, full_message)

@bot.message_handler(commands=["settings"])
def settings_handler(message):
    if is_rate_limited(str(message.from_user.id)):
        bot.send_message(
            message.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    user_id = str(message.from_user.id)
    if message.chat.type != "private":
        stop_command_in_group(message.chat.id, message.from_user.first_name or "Пользователь")
        return

    # Загружаем данные, чтобы убедиться, что пользователь существует
    data = load_data(message.from_user.first_name, message.from_user.id, "settings")
    if data is None or user_id not in data:
        bot.send_message(message.chat.id, "Сначала отправьте /start")
        return

    # Добавляем пользователя в режим
    user_in_settings_menu.add(user_id)

    # Создаём inline-клавиатуру
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("⏳ Порог срочности", callback_data="settings_urgent_threshold")
    )
    markup.add(
        telebot.types.InlineKeyboardButton("🕒 Ежедневное напоминание", callback_data="settings_daily_hour")
    )
    markup.add(
        telebot.types.InlineKeyboardButton("❌ Отмена / OK", callback_data="settings_cancel")
    )

    bot.send_message(
        message.chat.id,
        "⚙️ Выберите параметр для настройки:",
        reply_markup=markup
    )

@bot.message_handler(commands=["daytasks"])
def daytasks_handler(message):
    if is_rate_limited(str(message.from_user.id)):
        bot.send_message(
            message.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    user_id = str(message.from_user.id)
    if message.chat.type != "private":
        stop_command_in_group(message.chat.id, message.from_user.first_name or "Пользователь")
        return
    example = now_msk().strftime("%Y-%m-%d")  # Только дата, без времени
    bot.send_message(
        message.chat.id,
        f"Введите дату в формате: ГГГГ-ММ-ДД\n"
        f"Пример: {example}",
        reply_markup=make_cancel_button("cancel_daytasks")
    )
    user_awaiting_daytasks_date.add(user_id)

@bot.message_handler(func=lambda msg: str(msg.from_user.id) in user_awaiting_daytasks_date)
def handle_daytasks_date_input(msg):
    if is_rate_limited(str(msg.from_user.id)):
        bot.send_message(
            msg.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    user_id = str(msg.from_user.id)
    user_name = str(msg.from_user.first_name)
    chat_id = msg.chat.id
    date_str = msg.text.strip()

    # Удаляем из режима ожидания сразу
    user_awaiting_daytasks_date.discard(user_id)

    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        text = "❌ Неверный формат даты.\n"
        text += "Используй: ГГГГ-ММ-ДД\n"
        text += generate_today_date()
        bot.send_message(
            chat_id,
            text,
            reply_markup=make_cancel_button("cancel_daytasks")
        )
        user_awaiting_daytasks_date.add(user_id)  # вернуть в режим
        return

    # Загружаем данные
    try:
        data = load_data(user_name, msg.from_user.id, "daytasks")
    except Exception as e:
        logger.critical(f"Ошибка загрузки БД в /daytasks: {e}")
        bot.send_message(chat_id, "⚠️ Не удалось загрузить задачи. Попробуйте позже.")
        return

    if user_id not in data:
        bot.send_message(chat_id, "Сначала отправьте /start")
        return

    # Ищем задачи на эту дату
    tasks_on_date = get_sorted_tasks_on_date(data, str(msg.from_user.id), target_date)

    if not tasks_on_date:
        bot.send_message(chat_id, f"📅 На {date_str} нет запланированных задач.")
    else:
        header = f"📋 Задачи на <b>{date_str}</b>:\n\n"
        full_message = header + "\n".join(tasks_on_date)
        send_long_message(bot, chat_id, full_message, parse_mode="HTML")

@bot.message_handler(commands=["today"])
def today_handler(message):
    if is_rate_limited(str(message.from_user.id)):
        bot.send_message(
            message.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    if message.chat.type != "private":
        stop_command_in_group(message.chat.id, message.from_user.first_name or "Пользователь")
        return

    user_id = str(message.from_user.id)
    try:
        data = load_data(message.from_user.first_name, message.from_user.id, "today")
    except Exception as e:
        logger.critical(f"Ошибка загрузки БД в /today: {e}")
        bot.send_message(message.chat.id, "⚠️ Не удалось загрузить задачи. Попробуйте позже.")
        return

    if user_id not in data:
        bot.send_message(message.chat.id, "Сначала отправьте /start")
        return

    today = now_msk().date()
    # logger.error(f"today = {today}")
    
    tasks = get_sorted_tasks_on_date(data, str(message.from_user.id), today, logger)

    # logger.error(f"tasks = {tasks}")

    if not tasks:
        bot.send_message(message.chat.id, f"📅 На сегодня ({today.strftime('%d.%m.%Y')}) нет запланированных задач.")
    else:
        header = f"📋 Задачи на сегодня <b>({today.strftime('%d.%m.%Y')})</b>:\n\n"
        full_message = header + "\n".join(tasks)
        send_long_message(bot, message.chat.id, full_message, parse_mode="HTML")

@bot.message_handler(commands=["tomorrow"])
def tomorrow_handler(message):
    if is_rate_limited(str(message.from_user.id)):
        bot.send_message(
            message.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    if message.chat.type != "private":
        stop_command_in_group(message.chat.id, message.from_user.first_name or "Пользователь")
        return

    # logger.debug("1")

    user_id = str(message.from_user.id)
    user_name = message.from_user.first_name or "Пользователь"

    try:
        # logger.debug("2")
        data = load_data(user_name, message.from_user.id, "tomorrow")
    except Exception as e:
        logger.critical(f"Ошибка загрузки БД в /tomorrow: {e}")
        bot.send_message(message.chat.id, "⚠️ Не удалось загрузить задачи. Попробуйте позже.")
        return

    if user_id not in data:
        # logger.debug("3")
        bot.send_message(message.chat.id, "Сначала отправьте /start")
        return

    # logger.debug("4")
    tomorrow = (now_msk().date() + timedelta(days=1))
    tasks = get_sorted_tasks_on_date(data, str(message.from_user.id), tomorrow)

    # logger.debug("5")

    if not tasks:
        # logger.debug("6")
        bot.send_message(
            message.chat.id,
            f"📅 На завтра ({tomorrow.strftime('%d.%m.%Y')}) нет запланированных задач."
        )
    else:
        # logger.debug("7")
        header = f"📋 Задачи на завтра ({tomorrow.strftime('%d.%m.%Y')}):\n"
        full_message = header + "\n" + "\n".join(tasks)
        send_long_message(bot, message.chat.id, full_message)

@bot.message_handler(commands=["week"])
def week_handler(message):
    if is_rate_limited(str(message.from_user.id)):
        bot.send_message(
            message.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    if message.chat.type != "private":
        stop_command_in_group(message.chat.id, message.from_user.first_name or "Пользователь")
        return

    user_id = str(message.from_user.id)
    try:
        data = load_data(message.from_user.first_name, message.from_user.id, "week")
    except Exception as e:
        logger.critical(f"Ошибка загрузки БД в /week: {e}")
        bot.send_message(message.chat.id, "⚠️ Не удалось загрузить задачи. Попробуйте позже.")
        return

    if user_id not in data:
        bot.send_message(message.chat.id, "Сначала отправьте /start")
        return

    now = now_msk()
    today = now.date()
    # В Python: понедельник = 0, воскресенье = 6
    days_until_sunday = 6 - today.weekday()  # сколько дней до воскресенья (включая сегодня)
    week_days = [today + timedelta(days=i) for i in range(days_until_sunday + 1)]

    # Словарь: день недели → русская аббревиатура
    weekdays_ru = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]

    lines = []
    for day in week_days:
        weekday_abbr = weekdays_ru[day.weekday()] 
        date_str = day.strftime("%d.%m.%Y")
        # Собираем задачи как объекты
        raw_tasks = []
        for task in data[user_id].get("tasks", []):
            if task.get("status") == "completed":
                continue
            try:
                task_dt = datetime.fromisoformat(task["datetime"])
                if task_dt.date() == day:
                    raw_tasks.append(task)
            except (ValueError, KeyError):
                continue
        # Сортируем
        raw_tasks.sort(key=lambda t: datetime.fromisoformat(t["datetime"]))
        # Форматируем с экранированием
        tasks = []
        for task in raw_tasks:
            safe_text = html.escape(task["text"])
            time_str = datetime.fromisoformat(task["datetime"]).strftime("%H:%M")
            tasks.append(f"• {safe_text} ({time_str})")
        
        lines.append(f"<b>{weekday_abbr} {date_str}</b>")
        if tasks:
            lines.append("\n".join(tasks))
        else:
            lines.append("Нет задач")
        lines.append("")

    full_message = "\n".join(lines).strip()
    if not full_message:
        full_message = "На ближайшую неделю задач нет."
        send_long_message(bot, message.chat.id, full_message)
        return

    send_long_message(bot, message.chat.id, full_message, parse_mode="HTML")

@bot.message_handler(commands=["weekbydate"])
def weekbydate_handler(message):
    if is_rate_limited(str(message.from_user.id)):
        bot.send_message(
            message.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    if message.chat.type != "private":
        stop_command_in_group(message.chat.id, message.from_user.first_name or "Пользователь")
        return
    user_id = str(message.from_user.id)
    # Пример даты — сегодня + 7 дней
    example_date = (now_msk().date() + timedelta(days=7)).strftime("%Y-%m-%d")
    bot.send_message(
        message.chat.id,
        f"Введите дату в формате: ГГГГ-ММ-ДД\n"
        f"Пример: {example_date}",
        reply_markup=make_cancel_button("cancel_weekbydate")
    )
    user_awaiting_weekbydate_input.add(user_id)

@bot.message_handler(func=lambda msg: str(msg.from_user.id) in user_awaiting_weekbydate_input)
def handle_weekbydate_input(msg):
    if is_rate_limited(str(msg.from_user.id)):
        bot.send_message(
            msg.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    user_id = str(msg.from_user.id)
    chat_id = msg.chat.id
    user_name = msg.from_user.first_name or "Пользователь"
    date_str = msg.text.strip()
    user_awaiting_weekbydate_input.discard(user_id)

    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        example_date = (now_msk().date() + timedelta(days=7)).strftime("%Y-%m-%d")
        bot.send_message(
            chat_id,
            "❌ Неверный формат даты.\nИспользуй: ГГГГ-ММ-ДД\nПример: " + example_date,
            reply_markup=make_cancel_button("cancel_weekbydate")
        )
        user_awaiting_weekbydate_input.add(user_id)
        return

    try:
        data = load_data(user_name, msg.from_user.id, "weekbydate")
        if data is None or user_id not in data:
            bot.send_message(chat_id, "Сначала отправьте /start")
            return
    except Exception as e:
        logger.critical(f"Ошибка загрузки БД в /weekbydate: {e}")
        bot.send_message(chat_id, "⚠️ Не удалось загрузить задачи. Попробуйте позже.")
        return

    monday = target_date - timedelta(days=target_date.weekday())
    week_days = [monday + timedelta(days=i) for i in range(7)]
    weekdays_ru = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
    lines = []
    has_any_task = False

    for day in week_days:
        # Собираем и сортируем задачи на день
        raw_tasks = []
        for task in data[user_id].get("tasks", []):
            if task.get("status") == "completed":
                continue
            try:
                task_dt = datetime.fromisoformat(task["datetime"])
                if task_dt.date() == day:
                    raw_tasks.append(task)
            except (ValueError, KeyError):
                continue
        raw_tasks.sort(key=lambda t: datetime.fromisoformat(t["datetime"]))
        tasks = []
        for task in raw_tasks:
            safe_text = html.escape(task["text"])
            tasks.append(f"• {safe_text} ({datetime.fromisoformat(task['datetime']).strftime('%H:%M')})")
            has_any_task = True

        weekday_abbr = weekdays_ru[day.weekday()]
        date_str_fmt = day.strftime("%d.%m.%Y")
        lines.append(f"<b>{weekday_abbr} {date_str_fmt}</b>")
        lines.append("\n".join(tasks) if tasks else "• Нет задач")
        lines.append("")

    if not has_any_task:
        bot.send_message(chat_id, "На эту неделю задач нет.")
    else:
        full_message = "\n".join(lines).strip()
        send_long_message(bot, chat_id, full_message, parse_mode="HTML")

@bot.message_handler(commands=["task"])
def task_handler(message):
    if is_rate_limited(str(message.from_user.id)):
        bot.send_message(
            message.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    user_id = str(message.from_user.id)
    chat_id = message.chat.id
    if message.chat.type != "private":
        stop_command_in_group(message.chat.id, message.from_user.first_name or "Пользователь")
        return
    user_name = message.from_user.first_name or "Пользователь"
    data = load_data(user_name, message.from_user.id, "task")
    if data == None:
        return
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
    if is_rate_limited(str(msg.from_user.id)):
        bot.send_message(
            msg.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    user_id = str(msg.from_user.id)
    chat_id = msg.chat.id
    user_name = msg.from_user.first_name or "Пользователь"
    data = load_data(user_name, msg.from_user.id, "task")
    if data == None:
        return
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
    if is_rate_limited(str(message.from_user.id)):
        bot.send_message(
            message.chat.id,
            "⚠️ Слишком много запросов. Подождите немного перед следующей командой."
        )
        return
    user_id = str(message.from_user.id)
    chat_id = message.chat.id
    user_name = message.from_user.first_name or "Пользователь"
    datetime_str = message.text.strip()

    # Парсим дату
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

    # ВАЛИДАЦИЯ: дата не должна быть в прошлом или сейчас
    now = now_msk()
    if task_datetime <= now:
        current_time_str = now.strftime("%Y-%m-%d %H:%M")
        bot.send_message(
            chat_id,
            f"❌ Время задачи не может быть раньше текущего.\n"
            f"Сейчас: <i>{current_time_str}</i> (МСК)\n"
            f"Укажи время позже этого.",
            reply_markup=make_cancel_button("cancel_task"),
            parse_mode="HTML"
        )
        return

    # Загружаем данные
    data = load_data(user_name, chat_id, "task")
    if data is None or user_id not in data:
        bot.send_message(chat_id, "Сначала отправь /start")
        return

    text = user_awaiting_datetime[user_id]
    new_task = {
        "text": text,
        "datetime": task_datetime.isoformat(),
        "status": "waiting",
        "reminded": False,
        "created_at": now.isoformat()
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
    user_settings = data[user_id].get("settings", {})
    urgent_threshold = user_settings.get("urgent_threshold_hours", 12)
    daily_hour = user_settings.get("daily_reminder_hour", 6)
    now = now_msk()
    tasks_to_remind = []
    for task in data[user_id]["tasks"]:
        # task.get по-умолчанию == True. Поэтому надо перепроверять и добавлять False-ответ в условие!!!
        if (task.get("status") != "waiting" and task.get("status") != True) or task.get("reminded", False):
            # logger.debug(f"1; Task: {task}")
            continue
        try:
            # logger.debug("2")
            task_time = datetime.fromisoformat(task["datetime"])
        except Exception as e:
            # logger.debug(f"3; Reminder inner error: {e}")
            continue
        # logger.debug(f"4; Task: {task}")
        if (task_time.date() == (now.date() + timedelta(days=1))) and (now.hour == daily_hour):
            # logger.debug(f"5; Task time: {task_time.date()}")
            tasks_to_remind.append(task)
        elif (task_time - now).total_seconds() <= urgent_threshold * 3600  and task.get("status") != "overdue":
            # logger.debug(f"6; Task: {task}")
            tasks_to_remind.append(task)
    tasks_to_remind.sort(key=lambda t: datetime.fromisoformat(t["datetime"]))
    if not tasks_to_remind:
        return
    lines = []
    for task in tasks_to_remind:
        dt_str = datetime.fromisoformat(task["datetime"]).strftime('%d.%m.%Y в %H:%M')
        lines.append(f"🔔 {task['text']}\n📅 {dt_str}")
        task["reminded"] = True

    # Соединяем задачи через ДВЕ пустые строки (как у вас было)
    tasks_block = "\n\n".join(lines)

    # А теперь добавляем заголовок с ОДНОЙ пустой строкой после него
    full_message = "‼Напоминаю!\n\n" + tasks_block

    save_data(data)
    target_bot = reminder_bot if reminder_bot else bot
    send_long_message(target_bot, chat_id, full_message)

def reminder_daemon():
    while True:
        try:
            data = load_data("", 0, "")
            for user_id, user_data in data.items():
                # logger.error("I'm in reminder_daemon()!")
                check_and_send_reminders(bot, user_id, user_id, data)
        except Exception as e:
            lines.append(f"Reminder error: {e}")
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

    # Запускаем таймер на очистку списка запросов от юзеров против ддос-атак
    cleanup_thread = threading.Thread(target=cleanup_old_requests, daemon=True)
    cleanup_thread.start()

    # Устанавливаем webhook
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL + '/' + TELEGRAM_BOT_TOKEN)

    # Запускаем Flask-сервер
    port = int(os.environ.get('PORT', 5000))
    app.run(host="0.0.0.0", port=port)
