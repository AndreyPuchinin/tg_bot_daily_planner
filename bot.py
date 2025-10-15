from io import BytesIO
from telebot.apihelper import ApiTelegramException
from datetime import datetime, timedelta
import threading
import telebot
import json
# import time
import os

# === НАСТРОЙКИ ===
TELEGRAM_BOT_TOKEN = "8396602686:AAFfOqaDehOGf7Y3iom_j6VNxEGEmyOxIgU"
DATA_FILE = "data.json"
TIMEZONE_OFFSET = 3  # UTC+3 (Москва)

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

ADMIN_USER_ID = "1287372767"

user_states = {}  # user_id -> dict

TIMEZONE_OFFSET = 3

# Глобальные буферы
user_states = {}  # user_id -> {"mode": "task_text", "command": "/task", "original_message_id": 123}
user_awaiting_json_file = set()

def now_msk():
    return datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_OFFSET)

# Загрузка данных
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

# Сохранение данных
def save_data(data):
    temp_file = DATA_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp_file, DATA_FILE)  # атомарная замена
    
# Inline-кнопка отмены для /jsonin & /jsonout
def make_cancel_inline():
    return telebot.types.InlineKeyboardMarkup().add(
        telebot.types.InlineKeyboardButton("Cancel", callback_data="cancel_json")
    )

def cancel_operation(call, mode_name: str, command_name: str):
    user_id = str(call.from_user.id)
    chat_id = call.message.chat.id
    message_id = call.message.message_id

    current = user_states.get(user_id)
    if current and current["mode"] == mode_name:
        del user_states[user_id]
        try:
            bot.edit_message_text(
                f"❌ Отмена ввода команды {command_name}.",
                chat_id, message_id
            )
        except Exception:
            pass  # Игнорируем ошибки редактирования (сообщение удалено и т.д.)
    else:
        # Уже вышел из режима → показываем уведомление, но ловим ошибку
        try:
            bot.answer_callback_query(
                call.id,
                f"Режим ввода команды {command_name} уже отменён.",
                show_alert=False
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "query is too old" in str(e):
                # Игнорируем устаревшие запросы — это нормально
                pass
            else:
                raise  # Другие ошибки — оставляем

@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel:"))
def universal_cancel(call):
    _, mode, command = call.data.split(":", 2)
    cancel_operation(call, mode, command)
    
# Команда /jsonout — выгрузка файла
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

# Команда /jsonin — загрузка файла
@bot.message_handler(commands=["jsonin"])
def jsonin_handler(message):
    if str(message.from_user.id) != ADMIN_USER_ID:
        bot.send_message(message.chat.id, "❌ Эта команда доступна только администратору.")
        return

    user_awaiting_json_file.add(str(message.from_user.id))
    bot.send_message(
        message.chat.id,
        "Прикрепите файл с расширением .json с содержимым Базы Данных планов всех пользователей для бота.",
        reply_markup=make_cancel_inline()
    )

# Обработка документа (файла)
@bot.message_handler(content_types=["document"], func=lambda msg: str(msg.from_user.id) in user_awaiting_json_file)
def handle_json_file(msg):
    user_id = str(msg.from_user.id)
    chat_id = msg.chat.id

    # Проверка: есть ли файл
    if not msg.document:
        bot.send_message(chat_id, "Пожалуйста, отправьте именно файл.", reply_markup=make_cancel_inline())
        return

    file_info = bot.get_file(msg.document.file_id)
    file_name = msg.document.file_name or ""

    # Проверка расширения
    if not file_name.lower().endswith(".json"):
        bot.send_message(chat_id, "Файл должен иметь расширение .json.", reply_markup=make_cancel_inline())
        return

    try:
        # Скачиваем файл
        downloaded_file = bot.download_file(file_info.file_path)
        # Проверяем, что это валидный JSON
        json_content = json.loads(downloaded_file.decode("utf-8"))
        # Если всё ок — сохраняем
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(json_content, f, ensure_ascii=False, indent=2)

        user_awaiting_json_file.discard(user_id)
        bot.send_message(chat_id, "✅ Файл успешно загружен и применён!")

    except json.JSONDecodeError:
        bot.send_message(chat_id, "Ошибка: файл не является валидным JSON.", reply_markup=make_cancel_inline())
    except UnicodeDecodeError:
        bot.send_message(chat_id, "Ошибка: файл не в кодировке UTF-8.", reply_markup=make_cancel_inline())
    except Exception as e:
        bot.send_message(chat_id, f"Ошибка при обработке файла: {e}", reply_markup=make_cancel_inline())

def send_long_message(bot, chat_id, text):
    """Отправляет текст, разбивая на части по 4000 символов."""
    if not text.strip():
        return
    max_len = 4000
    for i in range(0, len(text), max_len):
        bot.send_message(chat_id, text[i:i + max_len])

# Генерация примера: завтра + текущее время (МСК)
def generate_example_datetime():
    now = now_msk()
    tomorrow = now.date() + timedelta(days=1)
    example_dt = datetime.combine(tomorrow, datetime.min.time()).replace(
        hour=now.hour, minute=now.minute
    )
    return example_dt.strftime("%Y-%m-%d %H:%M")

# Команда /start
@bot.message_handler(commands=["start"])
def start_handler(message):
    user_id = str(message.from_user.id)
    user_name = message.from_user.first_name or "Пользователь"
    data = load_data()

    if user_id not in data:
        data[user_id] = {
                            "user_name": user_name,
                            "chat_id": str(message.chat.id),  # ← добавили
                            "tasks": []
                        }
        save_data(data)
        bot.send_message(
            message.chat.id,
            f"Привет, {user_name}! 👋\n"
            "Я — твой личный ежедневник в Telegram.\n"
            "Используй команды:\n"
            "/task — добавить задачу\n"
            "/today — посмотреть задачи на сегодня\n"
            "/done — отметить выполнение"
        )
    else:
        bot.send_message(message.chat.id, f"С возвращением, {user_name}! Готов работать.")

# Команда /task
@bot.message_handler(commands=["task"])
def task_handler(message):
    user_id = str(message.from_user.id)
    text = message.text[6:].strip()

    if not text:
        bot.send_message(
            message.chat.id,
            "Введите текст задачи (просто напишите его, без команды):",
            reply_markup=make_cancel_inline("task_text", "/task")
        )
        user_states[user_id] = {"mode": "task_text", "command": "/task"}
    else:
        # Если текст уже в команде — сразу переходим к дате
        user_states[user_id] = text
        example = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        bot.send_message(
            message.chat.id,
            f"Укажи дату и время в формате: ГГГГ-ММ-ДД ЧЧ:ММ\n"
            f"Пример:\n{example}\n\n"
            f"Или нажми Cancel ниже.",
            reply_markup=make_cancel_inline("task_text", "/task")
        )
        
# Обработка текста задачи
@bot.message_handler(func=lambda msg: str(msg.from_user.id) in user_states)
def task_text_input(msg):
    user_id = str(msg.from_user.id)
    text = msg.text.strip()

    if not text:
        bot.send_message(msg.chat.id, "Текст не может быть пустым. Попробуй снова.")
        return

    # Сохраняем текст и переходим к ожиданию даты
    user_states[user_id] = text
    del user_states[user_id]  # выходим из режима ввода текста

    example = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    bot.send_message(
        msg.chat.id,
        f"Укажи дату и время в формате: ГГГГ-ММ-ДД ЧЧ:ММ\n"
        f"Пример:\n{example}\n\n"
        f"Или нажми inline-кнопку Cancel ниже.",
        reply_markup=make_cancel_inline("task", "/task")
    )

# Обработка ввода даты (только если ожидаем)
@bot.message_handler(func=lambda message: str(message.from_user.id) in user_states)
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
            f"Пример:\n"
            f"{example}",
            reply_markup=make_cancel_inline("task", "/task")
        )
        return

    text = user_states[user_id]
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

    del user_states[user_id]
    bot.send_message(
        chat_id,
        f"✅ Задача сохранена!\n"
        f"{text}\n"
        f"📅 {task_datetime.strftime('%d.%m.%Y в %H:%M')}"
    )
    
def check_and_send_reminders(bot, user_id, chat_id, data):
    # print("started")
    """Проверяет задачи и отправляет напоминания, если нужно."""
    now = now_msk()
    tasks_to_remind = []

    for task in data[user_id]["tasks"]:
        
        if task.get("status") != "waiting" or task.get("reminded", True):
            # print("skipped")
            continue

        try:
            task_time = datetime.fromisoformat(task["datetime"])
            # print((task_time - now))
        except:
            continue

        # Условие 1: задача завтра → напоминание в 13:00
        if (task_time.date() == (now.date() + timedelta(days=1))) and now.hour == 13:
            tasks_to_remind.append(task)

        # Условие 2: осталось ≤12 часов → напомнить сразу
        elif (task_time - now).total_seconds() <= 12 * 3600:
            tasks_to_remind.append(task)

    if not tasks_to_remind:
        return

    # Формируем текст
    lines = []
    for task in tasks_to_remind:
        dt_str = datetime.fromisoformat(task["datetime"]).strftime('%d.%m.%Y в %H:%M')
        lines.append(f"🔔 {task['text']}\n📅 {dt_str}")
        task["reminded"] = True  # помечаем как напомненную

    save_data(data)  # сохраняем изменения
    send_long_message(bot, chat_id, "\n\n".join(lines))
    
# Проверка задач на напоминание (каждые 10 минут)
# Работает с ограничениями. Перейти на какой-то VPS или Render
def reminder_daemon():
    while True:
        try:
            data = load_data()
            for user_id, user_data in data.items():
                # if "chat_id" in user_data:  # ← нужно сохранять chat_id!
                check_and_send_reminders(bot, user_id, user_id, data)
        except Exception as e:
            print(f"Reminder error: {e}")
        # time.sleep(600)  # 10 минут

"""@bot.message_handler(commands=["clear"])
def clear_keyboard(message):
    bot.send_message(
        message.chat.id,
        "Клавиатура сброшена. Теперь при нажатии на 🎛️ будет меню команд.",
        reply_markup=telebot.types.ReplyKeyboardRemove()
    )"""

# Запуск
if __name__ == "__main__":
    # Запускаем напоминания в фоновом потоке
    reminder_thread = threading.Thread(target=reminder_daemon, daemon=True)
    reminder_thread.start()

    # Запускаем бота в основном потоке
    bot.polling()
