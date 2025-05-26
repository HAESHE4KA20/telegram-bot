import logging
import sqlite3
import asyncio
import random
from collections import deque
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# --- Конфигурация Логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Конфигурация Токена Бота (ЗАМЕНИ НА СВОЙ ТОКЕН!) ---
TOKEN = "7777081792:AAGBUhVUfuCRn3OFZAPzrH0CCadnbnyT9Mk" 

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ДЛЯ РАЗНЫХ ФАЗ ПОИСКА/СОЗДАНИЯ МАТЧА ---
GLOBAL_MATCH_FLOW = {} # Словарь для хранения информации о каждом матче на разных этапах {chat_id: {match_id: ..., 'players': [], 'current_phase': 'search'|'map_vote'|'captain_pick'|'finished', 'message_id': ..., 'map_votes': {...}, 'captains': [...], 'teams': {...}, 'remaining_players_for_pick': [...], 'current_picker_index': ..., 'search_timeout_task': None}}
MATCH_ID_COUNTER = 0 # Уникальный ID для каждого матча, который проходит через все фазы

# Конфигурация карт
MAPS = ["Sandstone", "Zone 7", "Rust", "Sakura", "Breeze", "Dune", "Province"]

# --- Конфигурация Базы Данных ---
DB_NAME = 'facesit.db'


# --- Функции для работы с Базой Данных ---

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            game_id TEXT,
            rating INTEGER DEFAULT 1000,
            is_admin INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            is_muted INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("Database initialized.")

def is_registered(user_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM users WHERE telegram_id = ?", (user_id,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def register_user(telegram_id: int, username: str, game_id: str) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO users (telegram_id, username, game_id) VALUES (?, ?, ?)",
                   (telegram_id, username, game_id))
    conn.commit()
    conn.close()
    logger.info(f"User {username} ({telegram_id}) registered with game_id: {game_id}.")

def get_user_data(user_id: int) -> dict:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT username, game_id, rating FROM users WHERE telegram_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result:
        return {"username": result[0], "game_id": result[1], "rating": result[2]}
    return None

def get_user_rating(user_id: int) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT rating FROM users WHERE telegram_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0

def is_admin(user_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT is_admin FROM users WHERE telegram_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result and result[0] == 1

def is_banned(user_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT is_banned FROM users WHERE telegram_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result and result[0] == 1 # 1 означает True

def is_muted(user_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT is_muted FROM users WHERE telegram_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result and result[0] == 1 # 1 означает True

def update_user_status(user_id: int, status_type: str, value: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(f"UPDATE users SET {status_type} = ? WHERE telegram_id = ?", (value, user_id))
    conn.commit()
    conn.close()

def change_user_rating_db(user_id: int, new_rating: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET rating = ? WHERE telegram_id = ?", (new_rating, user_id))
    conn.commit()
    conn.close()

def delete_user_from_db(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE telegram_id = ?", (user_id,))
    conn.commit()
    conn.close()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ РАБОТЫ МАТЧА ---

### ИЗМЕНЕНИЕ: Изменена функция для редактирования вместо удаления/отправки
async def edit_or_send_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, text: str, reply_markup: InlineKeyboardMarkup = None, parse_mode: str = None) -> int:
    """Редактирует существующее сообщение или отправляет новое, если old_message_id равен None."""
    try:
        if message_id:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
            logger.info(f"Edited message {message_id} in chat {chat_id}.")
            return message_id
        else:
            new_message = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
            logger.info(f"Sent new message {new_message.message_id} in chat {chat_id}.")
            return new_message.message_id
    except Exception as e:
        logger.warning(f"Could not edit message {message_id} in chat {chat_id}, sending new one: {e}")
        new_message = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        logger.info(f"Sent new message {new_message.message_id} in chat {chat_id} after edit failure.")
        return new_message.message_id


async def cancel_search_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, match_id: int):
    """
    Отменяет поиск матча, если за 15 минут не набралось достаточно игроков.
    Вызывается по таймауту.
    """
    match_info = GLOBAL_MATCH_FLOW.get(chat_id)
    if not match_info or match_info['match_id'] != match_id or match_info['current_phase'] != 'search':
        return # Матч уже неактивен или перешел в другую фазу

    players_in_match = match_info['players']
    
    # ### ИЗМЕНЕНИЕ: Отменяем поиск, если игроков меньше 2 (минимально для 1х1)
    if len(players_in_match) < 2: 
        logger.info(f"Match {match_id} in chat {chat_id} cancelled due to 15-minute timeout. Players: {len(players_in_match)}")
        text = "Время поиска истекло. Не удалось набрать достаточно игроков для матча."
        old_message_id = match_info['message_id']
        
        ### ИЗМЕНЕНИЕ: Используем edit_or_send_message
        await edit_or_send_message(context, chat_id, old_message_id, text, reply_markup=None)
        
        # Оповещаем каждого игрока, что матч отменен
        for player in players_in_match:
            try:
                # ### ИЗМЕНЕНИЕ: Уточняем название чата для ЛС
                chat_title = await context.bot.get_chat(chat_id).title if chat_id != player['id'] else "этом чате"
                await context.bot.send_message(chat_id=player['id'], text=f"Матч в группе '{chat_title}' (ID: {match_id}) был отменен, так как не набралось достаточно игроков за 15 минут.")
            except Exception as e:
                logger.warning(f"Could not send cancellation message to user {player['id']}: {e}")

        del GLOBAL_MATCH_FLOW[chat_id] # Удаляем матч из активных

# --- Фаза 1: Поиск игроков ---

def generate_find_match_markup_phase1(match_id: int):
    keyboard = [
        [InlineKeyboardButton("Зайти в матч", callback_data=f"match_{match_id}_join")],
        [InlineKeyboardButton("Выйти из матча", callback_data=f"match_{match_id}_leave")],
        [InlineKeyboardButton("Завершить поиск", callback_data=f"match_{match_id}_endsearch")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def update_search_message_phase1(context: ContextTypes.DEFAULT_TYPE, chat_id: int, match_id: int):
    match_info = GLOBAL_MATCH_FLOW.get(chat_id)
    if not match_info or match_info['match_id'] != match_id or match_info['current_phase'] != 'search':
        logger.warning(f"Attempted to update non-existent or inactive search message for chat {chat_id}, match {match_id}")
        return

    players_in_match = match_info['players']
    usernames = [f"@{p['username']}" for p in players_in_match]

    text = (
        "Начинаю поиск игроков для матча!\n"
        f"Поиск игроков...\n\n"
        f"Игроки в матче ({len(players_in_match)}/10):\n"
    )
    if usernames:
        text += "\n".join(usernames)
    else:
        text += "Ожидание игроков..."

    reply_markup = generate_find_match_markup_phase1(match_id)
    
    old_message_id = match_info['message_id']
    ### ИЗМЕНЕНИЕ: Используем edit_or_send_message
    new_message_id = await edit_or_send_message(context, chat_id, old_message_id, text, reply_markup, parse_mode='Markdown')
    GLOBAL_MATCH_FLOW[chat_id]['message_id'] = new_message_id
    logger.info(f"Updated search message for chat {chat_id}, match {match_id}. Players: {len(players_in_match)}")

    # Проверяем, если игроков 10, переходим к следующей фазе автоматически
    if len(players_in_match) >= 10:
        # Отменяем таймаут поиска, если он есть
        if 'search_timeout_task' in match_info and match_info['search_timeout_task']:
            match_info['search_timeout_task'].cancel()
            logger.info(f"Cancelled search timeout task for match {match_id} in chat {chat_id}.")
        await start_map_vote_phase2(context, chat_id, match_id)

# --- Фаза 2: Выбор карты ---

def generate_map_vote_markup_phase2(match_id: int, map_votes: dict):
    keyboard_buttons = []
    # Отображаем текущие голоса рядом с картой
    for map_name in MAPS:
        votes_count = len(map_votes.get(map_name, []))
        keyboard_buttons.append([InlineKeyboardButton(f"{map_name} ({votes_count} голосов)", callback_data=f"match_{match_id}_votemap_{map_name}")])
    return InlineKeyboardMarkup(keyboard_buttons)


async def update_map_vote_message_phase2(context: ContextTypes.DEFAULT_TYPE, chat_id: int, match_id: int):
    match_info = GLOBAL_MATCH_FLOW.get(chat_id)
    if not match_info or match_info['match_id'] != match_id or match_info['current_phase'] != 'map_vote':
        logger.warning(f"Attempted to update non-existent or inactive map vote message for chat {chat_id}, match {match_id}")
        return

    num_players = len(match_info['players'])
    vote_threshold = match_info['vote_threshold']
    map_votes = match_info['map_votes']

    text = (
        f"Поиск завершен! Игроков: {num_players}\n\n"
        f"**Выбор карты:** (Требуется {vote_threshold} голосов)\n\n"
    )
    for map_name in MAPS:
        votes_count = len(map_votes.get(map_name, []))
        text += f"- {map_name}: {votes_count} голосов\n"

    reply_markup = generate_map_vote_markup_phase2(match_id, map_votes)

    old_message_id = match_info['message_id']
    ### ИЗМЕНЕНИЕ: Используем edit_or_send_message
    new_message_id = await edit_or_send_message(context, chat_id, old_message_id, text, reply_markup, parse_mode='Markdown')
    GLOBAL_MATCH_FLOW[chat_id]['message_id'] = new_message_id
    logger.info(f"Updated map vote message for chat {chat_id}, match {match_id}.")

    # Проверка на выбранную карту
    for map_name, votes in map_votes.items():
        if len(votes) >= vote_threshold:
            match_info['selected_map'] = map_name
            await start_captain_pick_phase3(context, chat_id, match_id)
            return

async def start_map_vote_phase2(context: ContextTypes.DEFAULT_TYPE, chat_id: int, match_id: int, is_manual_end: bool = False):
    match_info = GLOBAL_MATCH_FLOW.get(chat_id)
    if not match_info or match_info['match_id'] != match_id:
        logger.error(f"Attempted to start map vote for non-existent match {match_id} in chat {chat_id}")
        return

    match_info['current_phase'] = 'map_vote'
    match_info['map_votes'] = {map_name: [] for map_name in MAPS} # Инициализируем голоса за карты

    num_players = len(match_info['players'])
    # Определяем порог голосов: (количество игроков / 2) + 1 (если округляем вверх)
    # Или просто 1, если хотим, чтобы любой голос завершал голосование после "Завершить поиск"
    match_info['vote_threshold'] = 1 # ### ИЗМЕНЕНИЕ: Устанавливаем порог 1 голос, так как голосование по карте не главное.
                                    # Если нужно, чтобы голосование было ОБЯЗАТЕЛЬНЫМ до пиков,
                                    # можно оставить (num_players // 2) + 1, но тогда без таймаута
                                    # карта не выберется, если никто не проголосует достаточно.
                                    # Мы решили не добавлять таймаут на голосование за карту.
    
    logger.info(f"Starting map vote for match {match_id} in chat {chat_id}. Players: {num_players}. Threshold: {match_info['vote_threshold']}")

    # Формируем сообщение и кнопки для голосования за карту
    text = (
        f"Поиск завершен! Игроков: {num_players}\n\n"
        f"**Выбор карты:** (Требуется {match_info['vote_threshold']} голосов)\n\n"
    )
    for map_name in MAPS:
        text += f"- {map_name}: 0 голосов\n"

    reply_markup = generate_map_vote_markup_phase2(match_id, match_info['map_votes'])

    old_message_id = match_info['message_id']
    ### ИЗМЕНЕНИЕ: Используем edit_or_send_message
    new_message_id = await edit_or_send_message(context, chat_id, old_message_id, text, reply_markup, parse_mode='Markdown')
    GLOBAL_MATCH_FLOW[chat_id]['message_id'] = new_message_id


# --- Фаза 3: Пики игроков ---

def generate_pick_markup_phase3(match_id: int, players_to_pick: list):
    keyboard_buttons = []
    # Каждая кнопка - это игрок, которого можно пикнуть
    for player in players_to_pick:
        keyboard_buttons.append([InlineKeyboardButton(f"@{player['username']}", callback_data=f"match_{match_id}_pick_{player['id']}")])
    
    return InlineKeyboardMarkup(keyboard_buttons)


async def update_captain_pick_message_phase3(context: ContextTypes.DEFAULT_TYPE, chat_id: int, match_id: int):
    match_info = GLOBAL_MATCH_FLOW.get(chat_id)
    if not match_info or match_info['match_id'] != match_id or match_info['current_phase'] != 'captain_pick':
        logger.warning(f"Attempted to update non-existent or inactive pick message for chat {chat_id}, match {match_id}")
        return

    captains = match_info['captains']
    team1 = match_info['teams']['team1']
    team2 = match_info['teams']['team2']
    remaining_players = match_info['remaining_players_for_pick']
    current_picker_index = match_info['current_picker_index']

    # ### ИЗМЕНЕНИЕ: Проверяем, что капитаны существуют, прежде чем обращаться к ним
    current_captain_username = captains[current_picker_index]['username'] if captains and len(captains) > current_picker_index else "Неизвестный"

    # Формируем сообщение о пиках
    text = (
        f"Игроки собраны, а карта выбрана! (Карта: {match_info['selected_map']})\n"
        f"**Пики игроков:**\n\n"
    )
    
    # Отображаем капитанов и уже выбранных игроков
    team1_usernames = [f"@{p['username']}" for p in team1]
    team2_usernames = [f"@{p['username']}" for p in team2]

    # Сначала капитаны
    # ### ИЗМЕНЕНИЕ: Добавлена проверка на наличие капитанов, чтобы избежать ошибок индекса
    if captains and len(captains) >= 2:
        text += f"**@{captains[0]['username']}**\t\t\t\t**@{captains[1]['username']}**\n"
    elif captains and len(captains) == 1:
        text += f"**@{captains[0]['username']}**\t\t\t\t**Ожидание второго капитана**\n"
    else:
        text += "**Капитаны не определены.**\n"
    
    # Затем пики по одному
    max_len = max(len(team1_usernames), len(team2_usernames))
    for i in range(max_len):
        player1 = team1_usernames[i] if i < len(team1_usernames) else ""
        player2 = team2_usernames[i] if i < len(team2_usernames) else ""
        # Добавим форматирование, чтобы выровнять колонки (Telegram Markdown не поддерживает табы)
        text += f"{player1:<20}\t\t\t\t{player2}\n" # Adjust width based on expected max username length

    text += f"\nСейчас пикает: @{current_captain_username}"

    reply_markup = generate_pick_markup_phase3(match_id, remaining_players)

    old_message_id = match_info['message_id']
    ### ИЗМЕНЕНИЕ: Используем edit_or_send_message
    new_message_id = await edit_or_send_message(context, chat_id, old_message_id, text, reply_markup, parse_mode='Markdown')
    GLOBAL_MATCH_FLOW[chat_id]['message_id'] = new_message_id

    # Если все игроки выбраны, переходим к финальной фазе
    if not remaining_players:
        await finish_match_phase4(context, chat_id, match_id)


async def start_captain_pick_phase3(context: ContextTypes.DEFAULT_TYPE, chat_id: int, match_id: int):
    match_info = GLOBAL_MATCH_FLOW.get(chat_id)
    if not match_info or match_info['match_id'] != match_id:
        logger.error(f"Attempted to start captain pick for non-existent match {match_id} in chat {chat_id}")
        return

    match_info['current_phase'] = 'captain_pick'
    
    all_players = list(match_info['players']) # Все игроки
    random.shuffle(all_players) # Перемешиваем для случайного выбора капитанов

    # Выбираем двух капитанов с самым высоким рейтингом
    # Если рейтинги одинаковы, то случайный выбор
    sorted_players = sorted(all_players, key=lambda x: x['rating'], reverse=True)
    
    # ### ИЗМЕНЕНИЕ: Изменен выбор капитанов, чтобы он работал при любом четном числе игроков
    if len(sorted_players) >= 2:
        captains = sorted_players[:2]
    elif len(sorted_players) == 0:
        logger.error(f"No players to select captains from in match {match_id}.")
        # Возможно, стоит отменить матч или сообщить об ошибке
        await context.bot.send_message(chat_id=chat_id, text="Ошибка: Недостаточно игроков для определения капитанов. Матч отменен.")
        del GLOBAL_MATCH_FLOW[chat_id]
        return
    else: # 1 игрок
        captains = sorted_players # Один игрок будет капитаном одной команды, вторая команда будет пустой, или нужно обработать это отдельно
        logger.warning(f"Only one player in match {match_id}. Proceeding with one captain.")

    match_info['captains'] = captains
    match_info['teams']['team1'] = [captains[0]] # Капитаны сразу в своих командах
    match_info['teams']['team2'] = [captains[1]] if len(captains) > 1 else [] # Если только один капитан, вторая команда пуста
    
    # Оставшиеся игроки для пика (исключая капитанов)
    match_info['remaining_players_for_pick'] = [p for p in all_players if p not in captains]
    match_info['current_picker_index'] = 0 # Начинает пикать первый капитан

    logger.info(f"Starting captain pick for match {match_id} in chat {chat_id}. Captains: @{captains[0]['username']} and @{captains[1]['username'] if len(captains) > 1 else 'N/A'}")

    await update_captain_pick_message_phase3(context, chat_id, match_id)


# --- Фаза 4: Финальное сообщение ---

async def finish_match_phase4(context: ContextTypes.DEFAULT_TYPE, chat_id: int, match_id: int):
    match_info = GLOBAL_MATCH_FLOW.get(chat_id)
    if not match_info or match_info['match_id'] != match_id:
        logger.error(f"Attempted to finish non-existent match {match_id} in chat {chat_id}")
        return

    match_info['current_phase'] = 'finished'

    # ### ИЗМЕНЕНИЕ: Проверка на наличие капитанов перед обращением
    captain1_username = match_info['captains'][0]['username'] if match_info['captains'] else "Неизвестный"
    
    text = (
        "Команды собраны!\n"
        f"Создает лобби @{captain1_username}\n\n" # Юзернейм первого капитана
        "Счастливой игры!\n\n"
        f"**Команда 1:**\n" + "\n".join([f"@{p['username']}" for p in match_info['teams']['team1']]) + "\n\n"
        f"**Команда 2:**\n" + "\n".join([f"@{p['username']}" for p in match_info['teams']['team2']])
    )
    
    old_message_id = match_info['message_id']
    ### ИЗМЕНЕНИЕ: Используем edit_or_send_message
    await edit_or_send_message(context, chat_id, old_message_id, text, reply_markup=None, parse_mode='Markdown')
    
    logger.info(f"Match {match_id} in chat {chat_id} finished.")

    # --- Отправка сообщений всем игрокам в личные сообщения ---
    all_players_in_match = match_info['teams']['team1'] + match_info['teams']['team2']
    team1_usernames_pm = [f"@{p['username']}" for p in match_info['teams']['team1']]
    team2_usernames_pm = [f"@{p['username']}" for p in match_info['teams']['team2']]

    for player in all_players_in_match:
        # ### ИЗМЕНЕНИЕ: Получаем название чата для ЛС, чтобы оно было корректным
        try:
            chat_title_for_pm = update.message.chat.title if update.message else 'неизвестная группа'
        except AttributeError:
            # Если update.message недоступен (например, если матч начался по таймауту)
            # можно попробовать получить название чата другим способом или использовать общее название
            chat_title_for_pm = "вашей группе"

        pm_text = f"Матч в группе '{chat_title_for_pm}' (ID: {match_id}) завершен!\n\n"
        
        if player in match_info['teams']['team1']:
            pm_text += "**Ваша команда (Команда 1):**\n" + "\n".join(team1_usernames_pm)
            pm_text += "\n\n**Команда противника (Команда 2):**\n" + "\n".join(team2_usernames_pm)
        else:
            pm_text += "**Ваша команда (Команда 2):**\n" + "\n".join(team2_usernames_pm)
            pm_text += "\n\n**Команда противника (Команда 1):**\n" + "\n".join(team2_usernames_pm) # Исправлено: должно быть team1_usernames_pm
            
        try:
            await context.bot.send_message(chat_id=player['id'], text=pm_text, parse_mode='Markdown')
            logger.info(f"Sent match finish PM to user {player['username']} ({player['id']}).")
        except Exception as e:
            logger.warning(f"Could not send match finish PM to user {player['id']}: {e}")

    del GLOBAL_MATCH_FLOW[chat_id] # Удаляем матч из активных после завершения


# --- ОСНОВНЫЕ ОБРАБОТЧИКИ КОМАНД ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    telegram_username = update.effective_user.username
    if telegram_username is None:
        await update.message.reply_text("У вашего аккаунта Telegram нет имени пользователя. Пожалуйста, установите его в настройках Telegram, чтобы использовать бота.")
        return

    if not is_registered(user_id):
        register_keyboard = [
            [InlineKeyboardButton("Зарегистрироваться", callback_data="register_now")]
        ]
        reply_markup = InlineKeyboardMarkup(register_keyboard)
        await update.message.reply_text("Добро пожаловать! Вы не зарегистрированы. Пожалуйста, нажмите кнопку ниже, чтобы начать регистрацию.", reply_markup=reply_markup)
    else:
        await update.message.reply_text("Вы уже зарегистрированы. Используйте /profile для просмотра своего профиля или /find_match для поиска игры.")

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    telegram_username = update.effective_user.username

    if telegram_username is None:
        await update.message.reply_text("У вашего аккаунта Telegram нет имени пользователя. Пожалуйста, установите его в настройках Telegram, чтобы использовать бота.")
        return

    if is_registered(user_id):
        await update.message.reply_text("Вы уже зарегистрированы.")
        return

    await update.message.reply_text("Пожалуйста, введите ваш игровой ник (Faceit, Steam ID или что-то подобное).")
    context.user_data['awaiting_game_id'] = True

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_registered(user_id):
        await update.message.reply_text("Вы не зарегистрированы. Используйте /register.")
        return

    user_data = get_user_data(user_id)
    if user_data:
        status_text = "Админ" if is_admin(user_id) else "Пользователь"
        if is_banned(user_id):
            status_text += ", Забанен"
        if is_muted(user_id):
            status_text += ", Замьючен"

        await update.message.reply_text(
            f"Ваш профиль:\n"
            f"Telegram: @{user_data['username']}\n"
            f"Game ID: {user_data['game_id']}\n"
            f"Рейтинг: {user_data['rating']}\n"
            f"Статус: {status_text}"
        )
    else:
        await update.message.reply_text("Не удалось получить данные вашего профиля.")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT username, rating FROM users ORDER BY rating DESC LIMIT 10")
    top_users = cursor.fetchall()
    conn.close()

    if not top_users:
        await update.message.reply_text("Таблица лидеров пуста.")
        return

    response = "🏆 **Таблица лидеров (Топ 10):** 🏆\n\n"
    for i, user in enumerate(top_users):
        response += f"{i+1}. @{user[0]} - Рейтинг: {user[1]}\n"

    await update.message.reply_text(response, parse_mode='Markdown')

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("У вас нет прав администратора.")
        return

    keyboard = [
        [InlineKeyboardButton("Назначить админа", callback_data="admin_setadmin")],
        [InlineKeyboardButton("Снять админа", callback_data="admin_removeadmin")],
        [InlineKeyboardButton("Забанить пользователя", callback_data="admin_banuser")],
        [InlineKeyboardButton("Разбанить пользователя", callback_data="admin_unbanuser")],
        [InlineKeyboardButton("Замьютить пользователя", callback_data="admin_muteuser")],
        [InlineKeyboardButton("Разгмьютить пользователя", callback_data="admin_unmuteuser")],
        [InlineKeyboardButton("Изменить рейтинг", callback_data="admin_changerating")],
        [InlineKeyboardButton("Удалить пользователя", callback_data="admin_deleteuser")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Добро пожаловать в админ-панель:", reply_markup=reply_markup)

async def set_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("У вас нет прав администратора.")
        return
    await update.message.reply_text("Введите Telegram ID пользователя, которого хотите назначить администратором.")
    context.user_data['awaiting_admin_id'] = True

async def remove_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("У вас нет прав администратора.")
        return
    await update.message.reply_text("Введите Telegram ID пользователя, которого хотите снять с администратора.")
    context.user_data['awaiting_remove_admin_id'] = True

async def ban_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("У вас нет прав администратора.")
        return
    await update.message.reply_text("Введите Telegram ID пользователя, которого хотите забанить.")
    context.user_data['awaiting_ban_id'] = True

async def unban_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("У вас нет прав администратора.")
        return
    await update.message.reply_text("Введите Telegram ID пользователя, которого хотите разбанить.")
    context.user_data['awaiting_unban_id'] = True

async def mute_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("У вас нет прав администратора.")
        return
    await update.message.reply_text("Введите Telegram ID пользователя, которого хотите замьютить.")
    context.user_data['awaiting_mute_id'] = True

async def unmute_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("У вас нет прав администратора.")
        return
    await update.message.reply_text("Введите Telegram ID пользователя, которого хотите размьютить.")
    context.user_data['awaiting_unmute_id'] = True

async def change_rating_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("У вас нет прав администратора.")
        return
    await update.message.reply_text("Введите Telegram ID пользователя и новый рейтинг, разделенные пробелом (например, 123456789 1500).")
    context.user_data['awaiting_rating_change'] = True

async def delete_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("У вас нет прав администратора.")
        return
    await update.message.reply_text("Введите Telegram ID пользователя, которого хотите удалить из базы данных.")
    context.user_data['awaiting_delete_user_id'] = True

async def cancel_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if not is_admin(user_id):
        await update.message.reply_text("У вас нет прав администратора для отмены матча.")
        return

    match_info = GLOBAL_MATCH_FLOW.get(chat_id)
    if not match_info:
        await update.message.reply_text("В этом чате нет активного матча для отмены.")
        return
    
    match_id = match_info['match_id']
    players_in_match = match_info['players'] # Сохраняем список игроков до удаления информации о матче

    # Отменяем таймаут поиска, если он активен
    if 'search_timeout_task' in match_info and match_info['search_timeout_task']:
        match_info['search_timeout_task'].cancel()
        logger.info(f"Admin cancelled search timeout task for match {match_id} in chat {chat_id}.")

    # Удаляем сообщение матча в группе (если оно было отправлено)
    try:
        if match_info.get('message_id'):
            await context.bot.delete_message(chat_id=chat_id, message_id=match_info['message_id'])
    except Exception as e:
        logger.warning(f"Could not delete message during admin cancellation: {e}")

    del GLOBAL_MATCH_FLOW[chat_id] # Удаляем матч из активных

    # Оповещаем админа
    await update.message.reply_text(f"Матч (ID: {match_id}) в этом чате отменен администратором.")
    logger.info(f"Match {match_id} in chat {chat_id} was cancelled by admin {user_id}.")

    # Оповещаем каждого игрока, участвовавшего в матче (если они были)
    for player in players_in_match:
        try:
            # ### ИЗМЕНЕНИЕ: Уточняем название чата для ЛС
            chat_title = update.effective_chat.title if update.effective_chat else 'неизвестная группа'
            await context.bot.send_message(chat_id=player['id'], text=f"Матч в группе '{chat_title}' (ID: {match_id}) был отменен администратором.")
        except Exception as e:
            logger.warning(f"Could not send cancellation message to user {player['id']}: {e}")


async def find_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    telegram_username = update.effective_user.username
    chat_id = update.effective_chat.id

    if not is_registered(user_id):
        await update.message.reply_text("Вы не зарегистрированы. Используйте /register.")
        return

    if is_banned(user_id):
        await update.message.reply_text("Вы забанены и не можете искать матчи.")
        return

    # Проверяем, не находится ли пользователь уже в каком-либо активном матче/поиске
    for active_chat_id, match_info in GLOBAL_MATCH_FLOW.items():
        for player in match_info['players']:
            if player['id'] == user_id:
                # ### ИЗМЕНЕНИЕ: Исправлено сообщение о текущем матче
                if active_chat_id == chat_id:
                    await update.message.reply_text("Вы уже участвуете в активном поиске матча в этом чате. Нажмите 'Зайти в матч' под сообщением.")
                else:
                    try:
                        other_chat = await context.bot.get_chat(active_chat_id)
                        chat_name = other_chat.title if other_chat.title else 'другом чате'
                    except Exception:
                        chat_name = 'другом чате'
                    await update.message.reply_text(f"Вы уже участвуете в активном поиске матча (ID: {match_info['match_id']}) в {chat_name}.")
                return
    
    # Если в этом чате уже идет поиск, сообщаем об этом
    if chat_id in GLOBAL_MATCH_FLOW and GLOBAL_MATCH_FLOW[chat_id]['current_phase'] == 'search':
        await update.message.reply_text("В этом чате уже идет поиск матча. Нажмите 'Зайти в матч' под сообщением.")
        await update_search_message_phase1(context, chat_id, GLOBAL_MATCH_FLOW[chat_id]['match_id'])
        return

    global MATCH_ID_COUNTER
    MATCH_ID_COUNTER += 1
    current_match_id = MATCH_ID_COUNTER

    user_rating = get_user_rating(user_id)
    
    # Инициализируем новый матч-флоу
    GLOBAL_MATCH_FLOW[chat_id] = {
        'match_id': current_match_id,
        'players': [{'id': user_id, 'username': telegram_username, 'rating': user_rating}], # Список игроков, каждый игрок - словарь
        'current_phase': 'search', # Текущая фаза: поиск
        'message_id': None, # ID сообщения, которое будем удалять/обновлять
        'map_votes': {}, # Для фазы голосования
        'captains': [], # Для фазы пиков
        'teams': {'team1': [], 'team2': []}, # Сформированные команды
        'remaining_players_for_pick': [], # Игроки, которых еще нужно пикнуть
        'current_picker_index': 0, # Индекс текущего капитана, который пикает
        'search_timeout_task': None # Задача для отмены по таймауту
    }

    # Отправляем первое сообщение и получаем его message_id
    text = (
        "Начинаю поиск игроков для матча!\n"
        f"Поиск игроков...\n\n"
        f"Игроки в матче (1/10):\n"
        f"@{telegram_username}"
    )
    reply_markup = generate_find_match_markup_phase1(current_match_id)
    sent_message = await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    GLOBAL_MATCH_FLOW[chat_id]['message_id'] = sent_message.message_id
    
    logger.info(f"New match flow {current_match_id} initiated in chat {chat_id} by {telegram_username}.")

    # Запускаем таймаут на 15 минут
    GLOBAL_MATCH_FLOW[chat_id]['search_timeout_task'] = asyncio.create_task(
        asyncio.sleep(15 * 60)
    )
    # Добавляем callback в задачу, чтобы она выполнилась после завершения сна
    GLOBAL_MATCH_FLOW[chat_id]['search_timeout_task'].add_done_callback(
        lambda t: asyncio.ensure_future(cancel_search_timeout(context, chat_id, current_match_id))
    )
    logger.info(f"Search timeout task started for match {current_match_id} in chat {chat_id}.")


async def handle_match_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer() # Обязательно отвечаем на callback_query, чтобы убрать "часики"

    user_id = query.from_user.id
    telegram_username = query.from_user.username
    chat_id = query.message.chat_id
    
    # Извлекаем match_id и действие из callback_data: "match_ID_ACTION" или "match_ID_ACTION_DATA"
    parts = query.data.split('_')
    
    # Если это админ-колбэк, обрабатываем его отдельно
    if parts[0] == "admin":
        if not is_admin(user_id):
            await query.edit_message_text("У вас нет прав администратора.")
            return

        admin_action = parts[1] # Например, 'set' из 'admin_set_admin'
        
        if admin_action == 'set_admin':
            await query.message.reply_text("Введите Telegram ID пользователя, которого хотите назначить администратором.")
            context.user_data['awaiting_admin_id'] = True
        elif admin_action == 'remove_admin':
            await query.message.reply_text("Введите Telegram ID пользователя, которого хотите снять с администратора.")
            context.user_data['awaiting_remove_admin_id'] = True
        elif admin_action == 'ban_user':
            await query.message.reply_text("Введите Telegram ID пользователя, которого хотите забанить.")
            context.user_data['awaiting_ban_id'] = True
        elif admin_action == 'unban_user':
            await query.message.reply_text("Введите Telegram ID пользователя, которого хотите разбанить.")
            context.user_data['awaiting_unban_id'] = True
        elif admin_action == 'mute_user':
            await query.message.reply_text("Введите Telegram ID пользователя, которого хотите замьютить.")
            context.user_data['awaiting_mute_id'] = True
        elif admin_action == 'unmute_user':
            await query.message.reply_text("Введите Telegram ID пользователя, которого хотите размьютить.")
            context.user_data['awaiting_unmute_id'] = True
        elif admin_action == 'change_rating':
            await query.message.reply_text("Введите Telegram ID пользователя и новый рейтинг, разделенные пробелом (например, 123456789 1500).")
            context.user_data['awaiting_rating_change'] = True
        elif admin_action == 'delete_user':
            await query.message.reply_text("Введите Telegram ID пользователя, которого хотите удалить из базы данных.")
            context.user_data['awaiting_delete_user_id'] = True
        return # Важно выйти после обработки админ-колбэка
    
    # Обработка колбэков регистрации
    if query.data == "register_now":
        if is_registered(user_id):
            # ### ИЗМЕНЕНИЕ: Используем edit_or_send_message для существующего сообщения
            await edit_or_send_message(context, chat_id, query.message.message_id, "Вы уже зарегистрированы.")
        else:
            await query.message.reply_text("Пожалуйста, введите ваш игровой ник (Faceit, Steam ID или что-то подобное).")
            context.user_data['awaiting_game_id'] = True
        return

    # Продолжаем обработку колбэков матча
    if len(parts) < 3 or parts[0] != "match":
        logger.error(f"Invalid callback_data format for match: {query.data}")
        return
    
    match_id = int(parts[1])
    action = parts[2]
    action_data = parts[3] if len(parts) > 3 else None # Например, название карты или ID игрока для пика

    match_info = GLOBAL_MATCH_FLOW.get(chat_id)

    # Проверка, что это актуальный матч и пользователь участвует в нем
    if not match_info or match_info['match_id'] != match_id:
        # Если сообщение уже изменено или удалено, просто игнорируем или отвечаем
        if query.message.text and "Этот матч уже" not in query.message.text:
             ### ИЗМЕНЕНИЕ: Используем edit_or_send_message
             await edit_or_send_message(context, chat_id, query.message.message_id, "Этот матч уже неактивен или завершен.")
        return

    current_phase = match_info['current_phase']
    
    # --- Обработка Фазы 1: Поиск игроков (кнопки join, leave, end_search) ---
    if current_phase == 'search':
        players_in_match = match_info['players']
        player_ids_in_match = [p['id'] for p in players_in_match]

        if action == 'join':
            if user_id in player_ids_in_match:
                #await update_search_message_phase1(context, chat_id, match_id) # Может быть лишним, если текст не изменился
                return # Уже в списке
            
            if not is_registered(user_id):
                await query.answer("Вы не зарегистрированы. Используйте /register.") # Ответ в виде всплывающего уведомления
                return
            
            if is_banned(user_id):
                await query.answer("Вы забанены и не можете присоединиться.")
                return

            user_rating = get_user_rating(user_id)
            players_in_match.append({'id': user_id, 'username': telegram_username, 'rating': user_rating})
            logger.info(f"User {telegram_username} ({user_id}) joined match {match_id} via button. Players: {len(players_in_match)}")
            await update_search_message_phase1(context, chat_id, match_id)

        elif action == 'leave':
            if user_id not in player_ids_in_match:
                #await query.edit_message_text(f"Вы не были в этом поиске матча.") # Может быть лишним
                await query.answer(f"Вы не были в этом поиске матча.")
                return

            match_info['players'] = [p for p in players_in_match if p['id'] != user_id]
            logger.info(f"User {telegram_username} ({user_id}) left match {match_id}.")
            
            if not match_info['players']: # Если никого не осталось, закрываем комнату
                # Отменяем таймаут поиска, если он есть
                if 'search_timeout_task' in match_info and match_info['search_timeout_task']:
                    match_info['search_timeout_task'].cancel()
                    logger.info(f"Cancelled search timeout task for match {match_id} in chat {chat_id} due to last player leaving.")
                try:
                    # ### ИЗМЕНЕНИЕ: Удаляем сообщение при закрытии комнаты
                    if query.message.message_id:
                        await context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
                except Exception as e:
                    logger.warning(f"Could not delete message after room closed: {e}")
                await query.message.reply_text("Вы покинули поиск матча. Комната закрыта, так как никого не осталось.")
                logger.info(f"Match {match_id} in chat {chat_id} closed as no players left.")
            else:
                await update_search_message_phase1(context, chat_id, match_id)

        elif action == 'endsearch':
            # Проверка, является ли пользователь админом или участником поиска
            if user_id not in player_ids_in_match and not is_admin(user_id):
                await query.answer("Только участник поиска или администратор может завершить поиск.")
                return
            
            num_players = len(players_in_match)
            # ### ИЗМЕНЕНИЕ: Проверка на четное количество игроков от 2 до 10 включительно
            if num_players >= 2 and num_players <= 10 and num_players % 2 == 0: 
                # Отменяем таймаут поиска
                if 'search_timeout_task' in match_info and match_info['search_timeout_task']:
                    match_info['search_timeout_task'].cancel()
                    logger.info(f"Cancelled search timeout task for match {match_id} in chat {chat_id} by manual end.")
                await start_map_vote_phase2(context, chat_id, match_id, is_manual_end=True)
            else:
                await query.answer(f"Невозможно завершить поиск. Количество игроков ({num_players}) должно быть четным и от 2 до 10.")
                # await update_search_message_phase1(context, chat_id, match_id) # Не нужно обновлять, если не завершено

    # --- Обработка Фазы 2: Выбор карты (кнопки vote_map) ---
    elif current_phase == 'map_vote':
        if action == 'votemap':
            map_name = action_data # Название карты
            
            # Проверяем, что голосующий является игроком в этом матче
            players_in_match_ids = [p['id'] for p in match_info['players']]
            if user_id not in players_in_match_ids:
                await query.answer("Вы не участвуете в этом матче и не можете голосовать.")
                return

            # Удаляем голос пользователя со всех других карт, если он уже голосовал
            for m, voters in match_info['map_votes'].items():
                if user_id in voters:
                    voters.remove(user_id)
            
            # Добавляем голос за выбранную карту
            if map_name not in match_info['map_votes']:
                match_info['map_votes'][map_name] = []
            match_info['map_votes'][map_name].append(user_id)
            logger.info(f"User {telegram_username} ({user_id}) voted for map {map_name} in match {match_id}.")
            
            await update_map_vote_message_phase2(context, chat_id, match_id)

    # --- Обработка Фазы 3: Пики игроков (кнопки pick) ---
    elif current_phase == 'captain_pick':
        if action == 'pick':
            player_to_pick_id = int(action_data) # ID игрока, которого пикают
            
            current_captain_info = match_info['captains'][match_info['current_picker_index']]
            
            # Проверяем, что пикает текущий капитан
            if user_id != current_captain_info['id']:
                await query.answer("Сейчас не ваша очередь пикать.")
                return

            # Проверяем, что выбранный игрок еще доступен для пика
            player_found = False
            for i, p in enumerate(match_info['remaining_players_for_pick']):
                if p['id'] == player_to_pick_id:
                    picked_player = match_info['remaining_players_for_pick'].pop(i) # Удаляем из списка оставшихся
                    player_found = True
                    break

            if not player_found:
                await query.answer("Этот игрок уже был выбран или недоступен.")
                #await update_captain_pick_message_phase3(context, chat_id, match_id) # Обновляем на случай, если сообщение поменялось
                return

            # Добавляем игрока в команду текущего капитана
            current_team_key = 'team1' if match_info['current_picker_index'] == 0 else 'team2'
            match_info['teams'][current_team_key].append(picked_player)
            logger.info(f"Captain @{current_captain_info['username']} picked @{picked_player['username']} for match {match_id}.")

            # Переключаем капитана
            match_info['current_picker_index'] = (match_info['current_picker_index'] + 1) % 2
            
            await update_captain_pick_message_phase3(context, chat_id, match_id)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text
    telegram_username = update.effective_user.username

    # Обработка ввода игрового ID после команды /register
    if context.user_data.get('awaiting_game_id'):
        game_id = text
        register_user(user_id, telegram_username, game_id)
        await update.message.reply_text(f"Вы успешно зарегистрированы! Ваш Game ID: {game_id}.")
        context.user_data['awaiting_game_id'] = False
        logger.info(f"User {telegram_username} ({user_id}) completed registration with game_id: {game_id}")
        return
    
    # Обработка ввода для админ-команд
    if is_admin(user_id):
        try:
            if context.user_data.get('awaiting_admin_id'):
                target_user_id = int(text)
                update_user_status(target_user_id, 'is_admin', 1)
                await update.message.reply_text(f"Пользователь {target_user_id} назначен администратором.")
                context.user_data['awaiting_admin_id'] = False
                logger.info(f"Admin {telegram_username} ({user_id}) set user {target_user_id} as admin.")
                return

            if context.user_data.get('awaiting_remove_admin_id'):
                target_user_id = int(text)
                update_user_status(target_user_id, 'is_admin', 0)
                await update.message.reply_text(f"Пользователь {target_user_id} снят с администратора.")
                context.user_data['awaiting_remove_admin_id'] = False
                logger.info(f"Admin {telegram_username} ({user_id}) removed admin status from user {target_user_id}.")
                return

            if context.user_data.get('awaiting_ban_id'):
                target_user_id = int(text)
                update_user_status(target_user_id, 'is_banned', 1)
                await update.message.reply_text(f"Пользователь {target_user_id} забанен.")
                context.user_data['awaiting_ban_id'] = False
                logger.info(f"Admin {telegram_username} ({user_id}) banned user {target_user_id}.")
                return

            if context.user_data.get('awaiting_unban_id'):
                target_user_id = int(text)
                update_user_status(target_user_id, 'is_banned', 0)
                await update.message.reply_text(f"Пользователь {target_user_id} разбанен.")
                context.user_data['awaiting_unban_id'] = False
                logger.info(f"Admin {telegram_username} ({user_id}) unbanned user {target_user_id}.")
                return
            
            if context.user_data.get('awaiting_mute_id'):
                target_user_id = int(text)
                update_user_status(target_user_id, 'is_muted', 1)
                await update.message.reply_text(f"Пользователь {target_user_id} замьючен.")
                context.user_data['awaiting_mute_id'] = False
                logger.info(f"Admin {telegram_username} ({user_id}) muted user {target_user_id}.")
                return

            if context.user_data.get('awaiting_unmute_id'):
                target_user_id = int(text)
                update_user_status(target_user_id, 'is_muted', 0)
                await update.message.reply_text(f"Пользователь {target_user_id} размьючен.")
                context.user_data['awaiting_unmute_id'] = False
                logger.info(f"Admin {telegram_username} ({user_id}) unmuted user {target_user_id}.")
                return

            if context.user_data.get('awaiting_rating_change'):
                parts = text.split()
                if len(parts) == 2:
                    target_user_id = int(parts[0])
                    new_rating = int(parts[1])
                    change_user_rating_db(target_user_id, new_rating)
                    await update.message.reply_text(f"Рейтинг пользователя {target_user_id} изменен на {new_rating}.")
                    context.user_data['awaiting_rating_change'] = False
                    logger.info(f"Admin {telegram_username} ({user_id}) changed rating for user {target_user_id} to {new_rating}.")
                else:
                    await update.message.reply_text("Неверный формат. Используйте 'Telegram ID Новый Рейтинг'.")
                return

            if context.user_data.get('awaiting_delete_user_id'):
                target_user_id = int(text)
                delete_user_from_db(target_user_id)
                await update.message.reply_text(f"Пользователь {target_user_id} удален из базы данных.")
                context.user_data['awaiting_delete_user_id'] = False
                logger.info(f"Admin {telegram_username} ({user_id}) deleted user {target_user_id}.")
                return

        except ValueError:
            await update.message.reply_text("Неверный формат ID или рейтинга. Пожалуйста, введите число.")
            # Сброс флагов, если ввели неверные данные
            for key in ['awaiting_admin_id', 'awaiting_remove_admin_id', 'awaiting_ban_id', 'awaiting_unban_id', 
                         'awaiting_mute_id', 'awaiting_unmute_id', 'awaiting_rating_change', 'awaiting_delete_user_id']:
                if context.user_data.get(key):
                    context.user_data[key] = False
            return
        except Exception as e:
            await update.message.reply_text(f"Произошла ошибка при выполнении админ-действия: {e}")
            logger.error(f"Error in admin action for user {user_id}: {e}")
            return

    # Если сообщение не является командой и не обрабатывается логикой ожидания ввода
    # Можно добавить сюда любую другую логику для обработки обычных текстовых сообщений
    # await update.message.reply_text("Извините, я не понимаю эту команду или ваше сообщение.")


# --- ГЛАВНАЯ ФУНКЦИЯ ЗАПУСКА БОТА ---
import sqlite3

DB_NAME = 'facesit.db' # Убедись, что это имя твоего файла базы данных

def make_admin(telegram_id_to_make_admin: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Проверяем, существует ли пользователь с таким ID
    cursor.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (telegram_id_to_make_admin,))
    user_exists = cursor.fetchone()

    if user_exists:
        cursor.execute("UPDATE users SET is_admin = 1 WHERE telegram_id = ?", (telegram_id_to_make_admin,))
        conn.commit()
        print(f"Пользователь с Telegram ID {telegram_id_to_make_admin} успешно назначен администратором.")
    else:
        print(f"Ошибка: Пользователь с Telegram ID {telegram_id_to_make_admin} не найден в базе данных. Пожалуйста, убедитесь, что он зарегистрирован.")
        print("Сначала пользователь должен отправить /start и /register боту.")

    conn.close()

# В самом начале файла, где у тебя TOKEN, DB_NAME, your_telegram_id
# Добавь эти переменные:
PORT = 8000 # Порт, на котором будет слушать твой бот на Render. Может быть 443 или 80. Render обычно по умолчанию предоставляет 10000.
# ВАЖНО: Render может требовать использовать порт из переменной окружения.
# Давай попробуем 8000 сначала, если не получится, я скажу, как изменить.
# Если у тебя есть переменная окружения PORT в Render, то используй ее:
# import os
# PORT = int(os.environ.get('PORT', '8000')) # Замени 8000 на то, что Render дает. 10000 часто используется.

# ... остальной код бота ...

if __name__ == "__main__":
    init_db()

    # --- Важный момент: получение токена бота ---
    # Убедись, что TOKEN переменная определена и содержит токен бота
    # Если токен хранится в файле .env, убедись, что этот файл загружен на Render
    # Или просто захардкодь его здесь для теста, если это не секрет
    # TOKEN = "ТВОЙ_ТОКЕН_БОТА" # Например, так

    application = Application.builder().token(TOKEN).build()

    # Обработчики команд и сообщений (оставь их как есть)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("register", register))
    application.add_handler(CommandHandler("profile", profile))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CommandHandler("find_match", find_match))

    # Админские команды
    application.add_handler(CommandHandler("admin_panel", admin_panel))
    application.add_handler(CommandHandler("set_admin", set_admin_cmd))
    application.add_handler(CommandHandler("ban_user", ban_user_cmd))
    application.add_handler(CommandHandler("unban_user", unban_user_cmd))
    application.add_handler(CommandHandler("mute_user", mute_user_cmd))
    application.add_handler(CommandHandler("unmute_user", unmute_user_cmd))
    application.add_handler(CommandHandler("remove_admin", remove_admin_cmd))
    application.add_handler(CommandHandler("change_rating", change_rating_cmd))
    application.add_handler(CommandHandler("delete_user", delete_user_cmd))

    # Обработчик сообщений, которые не являются командами
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # Если handle_message закомментировано, раскомментируй его, чтобы бот отвечал на текст

    # --- Новый блок для Webhook ---
    # Получаем URL твоего сервиса Render (он будет у Render после создания сервиса)
    # Например, если ты назвал сервис "my-amazing-bot", то URL будет https://my-amazing-bot.onrender.com/
    # Сюда нужно будет вставить этот URL. Пока поставь заглушку.
    WEBHOOK_URL = "https://ТВОЁ_ИМЯ_СЕРВИСА_НА_RENDER.onrender.com/" # Эту строку нужно будет ОБЯЗАТЕЛЬНО ОБНОВИТЬ ПОЗЖЕ!

    # Запускаем бота в режиме Webhook
    # listen: Порт, на котором будет слушать твой бот внутри контейнера Render.
    # url_path: Путь, по которому Telegram будет отправлять обновления (может быть произвольным, например, '/webhook')
    # webhook_url: Полный URL, который Telegram будет использовать.
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT, # Используй PORT, который ты определил выше
        url_path="/", # Можно оставить '/', Telegram будет отправлять на корень URL
        webhook_url=WEBHOOK_URL # Полный URL твоего сервиса на Render
    )
    
    # ... здесь должны быть добавлены твои хэндлеры (application.add_handler)...

    # Запускаем бота
    application.run_polling() # или application.run_webhook() - зависит от того, как ты запускаешь

    # --- ОБРАБОТЧИКИ КОМАНД ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("register", register))
    application.add_handler(CommandHandler("profile", profile))
    application.add_handler(CommandHandler("leaderboard", leaderboard))
    application.add_handler(CommandHandler("find_match", find_match))

    # Админские команды (как отдельные команды)
    application.add_handler(CommandHandler("admin_panel", admin_panel))
    application.add_handler(CommandHandler("set_admin", set_admin_cmd))
    application.add_handler(CommandHandler("remove_admin", remove_admin_cmd))
    application.add_handler(CommandHandler("ban_user", ban_user_cmd))
    application.add_handler(CommandHandler("unban_user", unban_user_cmd))
    application.add_handler(CommandHandler("mute_user", mute_user_cmd))
    application.add_handler(CommandHandler("unmute_user", unmute_user_cmd))
    application.add_handler(CommandHandler("change_rating", change_rating_cmd))
    application.add_handler(CommandHandler("delete_user", delete_user_cmd))
    application.add_handler(CommandHandler("cancel_match", cancel_match)) # НОВАЯ КОМАНДА ДЛЯ АДМИНОВ

    # --- УНИВЕРСАЛЬНЫЙ ОБРАБОТЧИК ДЛЯ ВСЕХ КНОПОК ---
    # Он будет ловить как кнопки матча ("match_"), так и кнопки админки ("admin_")
    application.add_handler(CallbackQueryHandler(handle_match_callbacks)) # Добавлен register_now

    # --- ОБРАБОТЧИК СООБЩЕНИЙ (для ввода Game ID и админ-действий) ---
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)
