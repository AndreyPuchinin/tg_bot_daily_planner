from io import BytesIO
from datetime import datetime, timedelta
import threading
import telebot
import json
import os

# === НАСТРОЙКИ ===
TELEGRAM_BOT_TOKEN = "8396602686:AAFfOqaDehOGf7Y3iom_j6VNxEGEmyOxIgU"
DATA_FILE = "data.json"
TIMEZONE_OFFSET = 3  # UTC+3 (Москва)
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
ADMIN_USER_ID = "1287372767" #в настройки: добавлять и удалять админов. Возможности админов и администрирования

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

def now_msk():
    return datetime.utcnow() + timedelta(hours=TIMEZONE_OFFSET)

# === УНИВЕРСАЛЬНАЯ ФУНКЦИЯ ДЛЯ КНОПКИ ОТМЕНЫ ===
def make_cancel_button(callback_data: str = "cancel_task") -> telebot.types.InlineKeyboardMarkup:
    """Создаёт inline-клавиатуру с кнопкой 'Cancel'."""
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Cancel", callback_data=callback_data))
    return markup

# === РАБОТА С ФАЙЛАМИ ===
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_data(data):
    temp_file = DATA_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_file, DATA_FILE)

# === КОМАНДЫ АДМИНА ===
@bot.message_handler(commands=["jsonout"])
def jsonout_handler(message):
    if str(message.from_user.id) != ADMIN_USER_ID:
        bot.send_message(message.chat.id, "❌ Эта команда доступна только администратору.")
        return
    if not os.path.exists(DATA_FILE):
        bot.send_message(message.chat.id, "Файл данных не найден.")
        return
    try:
        with open(DATA_FILE, "rb") as f:
            bot.send_document(
                message.chat.id,
                document=BytesIO(f.read()),
                visible_file_name="data.json",
                caption="📁 Текущая база данных"
            )
    except Exception as e:
        bot.send_message(message.chat.id, f"Ошибка при отправке файла: {e}")

@bot.message_handler(commands=["jsonin"])
def jsonin_handler(message):
    if str(message.from_user.id) != ADMIN_USER_ID:
        try:
            bot.send_message(message.chat.id, "❌ Эта команда доступна только администратору.")
        except Exception as e:
            print(f"Не удалось отправить сообщение: {e}")
        return

    user_awaiting_json_file.add(str(message.from_user.id))
    try:
        bot.send_message(
            message.chat.id,
            "Прикрепите файл с расширением .json с содержимым Базы Данных планов всех пользователей для бота.",
            reply_markup=make_cancel_button("cancel_json")
        )
    except Exception as e:
        print(f"Ошибка при отправке сообщения в /jsonin: {e}")
        # Можно отправить fallback через повторную попытку или просто логировать

@bot.message_handler(content_types=["document"], func=lambda msg: str(msg.from_user.id) in user_awaiting_json_file)
def handle_json_file(msg):
    user_id = str(msg.from_user.id)
    chat_id = msg.chat.id
    if not msg.document:
        bot.send_message(chat_id, "Пожалуйста, отправьте именно файл.", reply_markup=make_cancel_button("cancel_json"))
        return
    file_info = bot.get_file(msg.document.file_id)
    file_name = msg.document.file_name or ""
    if not file_name.lower().endswith(".json"):
        bot.send_message(chat_id, "Файл должен иметь расширение .json.", reply_markup=make_cancel_button("cancel_json"))
        return
    try:
        downloaded_file = bot.download_file(file_info.file_path)
        json_content = json.loads(downloaded_file.decode("utf-8"))
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(json_content, f, ensure_ascii=False, indent=2)
        user_awaiting_json_file.discard(user_id)
        bot.send_message(chat_id, "✅ Файл успешно загружен и применён!")
    except json.JSONDecodeError:
        bot.send_message(chat_id, "Ошибка: файл не является валидным JSON.", reply_markup=make_cancel_button("cancel_json"))
    except UnicodeDecodeError:
        bot.send_message(chat_id, "Ошибка: файл не в кодировке UTF-8.", reply_markup=make_cancel_button("cancel_json"))
    except Exception as e:
        bot.send_message(chat_id, f"Ошибка при обработке файла: {e}", reply_markup=make_cancel_button("cancel_json"))

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
@bot.message_handler(commands=["start"])
def start_handler(message):
    user_id = str(message.from_user.id)
    user_name = message.from_user.first_name or "Пользователь"
    data = load_data()
    if user_id not in data:
        data[user_id] = {
            "user_name": user_name,
            "chat_id": str(message.chat.id),
            "tasks": []
        }
        save_data(data)
        bot.send_message(
            message.chat.id,
            f"Привет, {user_name}! 👋\n"
            "Я — твой личный ежедневник в Telegram.\n"
            "Используй команды:\n"
            "/start - запустить бота\n"
            "/task — добавить задачу\n"
        )
    else:
        bot.send_message(message.chat.id, f"С возвращением, {user_name}! Готов работать.")

@bot.message_handler(commands=["task"])
def task_handler(message):
    user_id = str(message.from_user.id)
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
    chat_id = message.chat.id
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
    data = load_data()
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
        elif (task_time - now).total_seconds() <= 12 * 3600:
            tasks_to_remind.append(task)
    if not tasks_to_remind:
        return
    lines = []
    for task in tasks_to_remind:
        dt_str = datetime.fromisoformat(task["datetime"]).strftime('%d.%m.%Y в %H:%M')
        lines.append(f"🔔 {task['text']}\n📅 {dt_str}")
        task["reminded"] = True
    save_data(data)
    send_long_message(bot, chat_id, "\n".join(lines))

def reminder_daemon():
    while True:
        try:
            data = load_data()
            for user_id, user_data in data.items():
                check_and_send_reminders(bot, user_id, user_id, data)
        except Exception as e:
            print(f"Reminder error: {e}")
        # time.sleep(600)  # 10 минут — раскомментировать при запуске на сервере

# === ЗАПУСК ===
if __name__ == "__main__":
    reminder_thread = threading.Thread(target=reminder_daemon, daemon=True)
    reminder_thread.start()
    bot.polling()
