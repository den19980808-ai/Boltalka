import re
import logging
import random
import json
import os
import base64
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters
from typing import Dict, List, Any

EMOJI_RE_SIMPLE = re.compile(r"[\U0001F300-\U0001FAFF\U00002600-\U000026FF]", flags=re.UNICODE)

def _sanitize_boltun_reply(text: str, user_message: str, max_sentences: int = 2, max_chars: int = 450) -> str:
    """Обрезает длинные рассуждения, убирает лишние вопросы и эмодзи, возвращает компактный ответ."""
    if not text:
        return ""
    t = text.strip()
    # нормализуем пробелы/переносы
    t = re.sub(r"\s+\n", "\n", t)
    t = re.sub(r"\n{2,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t).strip()

    # Разбиваем на предложения по . ? !
    parts = re.split(r"(?<=[\.\!\?])\s+", t)
    # Выясним, задавал ли пользователь вопрос — тогда можно оставить 1 вопрос
    user_asked = bool(re.search(r"\?", (user_message or "")))

    # Если пользователь не спрашивал — удалим предложения с вопросительным знаком
    if not user_asked:
        parts = [p for p in parts if "?" not in p]

    # Оставим первые N предложений
    parts = parts[:max_sentences] if parts else parts

    # Соберём обратно
    out = " ".join(p.strip() for p in parts).strip()

    # Ограничим количество эмодзи — оставим максимум 1
    emojis = EMOJI_RE_SIMPLE.findall(out)
    if len(emojis) > 1:
        out = EMOJI_RE_SIMPLE.sub("", out)
        # добавить один эмодзи, если был
        if emojis:
            out = out + " " + emojis[0]

    # Сохранить длину
    if len(out) > max_chars:
        out = out[:max_chars].rstrip()
        if " " in out:
            out = out[:out.rfind(" ")] + "…"

    # Последняя гарантия: не слишком навязчивый вопрос в конце
    if not user_asked and out.endswith("?"):
        out = out.rstrip(" ?!.") + "."
    return out

def get_boltun_reply(user_name, message, history_context=""):
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    # Проверяем есть ли в контексте инструкция на развернутый ответ (новости)
    is_news_context = "[КОНТЕКСТ НОВОСТИ - ОТВЕЧАЙ РАЗВЕРНУТО!]" in history_context
    
    if is_news_context:
        # Для вопросов о новостях - развернутые ответы
        system_prompt = (
            "Ты — Болтун, тёплый и живой собеседник. "
            "Пользователь спрашивает о новости. ОТВЕЧАЙ РАЗВЕРНУТО! "
            "Требования к ответу: "
            "- Объем: 300-500+ символов (пиши как настоящий человек, а не робот) "
            "- Структура: начни с прямого ответа, затем добавь контекст и детали "
            "- Тон: естественный, разговорный, не сухой "
            "- Не пиши коротко, развей мысль "
            "- Если полезно, добавь фоновую информацию или аналогии "
            "- Используй эмодзи экономно (максимум 1-2) "
            "- Будь информативным и дружелюбным "
            f"{history_context}"
        )
        max_tokens = 800  # Больше токенов для развернутых ответов
    else:
        # Для обычных сообщений - краткие ответы
        system_prompt = (
            "Ты — Болтун, тёплый и живой собеседник. Отвечай кратко и по существу. "
            "Не задавай встречных вопросов, если пользователь явно не спрашивает — максимум 0–1 вопрос в ответе. "
            "Ограничь ответ 1–2 предложениями, избегай длинных рассуждений. "
            "Используй эмодзи очень экономно (0–1). "
            "Если пользователь сообщает факт (погода, блюдо, план) — ответь коротко и поддерживающе без дополнительных вопросов. "
            "Если нужно уточнить — сформулируй только ОДИН ясный вопрос в конце. "
            "Будь дружелюбным, но ненавязчивым."
            f"{history_context}"
        )
        max_tokens = 300

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": f"{user_name}: {message}"
            }
        ],
        max_tokens=max_tokens,
        temperature=0.7,
    )

    raw = ""
    try:
        raw = (response.choices[0].message.content or "").strip()
    except Exception:
        raw = ""

    # Постобработка: для новостей - не обрезаем так сильно
    if is_news_context:
        safe = _sanitize_boltun_reply(raw, message, max_sentences=5, max_chars=1000)
    else:
        safe = _sanitize_boltun_reply(raw, message, max_sentences=2, max_chars=450)
    
    return safe

class ChatHandler:    
    def __init__(self, intel_chat_function, history_manager):
        self.intel_chat = intel_chat_function
        self.history_manager = history_manager
        self.conversations = {}
        self.user_stats = {}
        self.memory_file = "user_memory.json"  # Добавьте это
        self.memory = self._load_memory()
        self.conversation_context = {}
        
    # === 🧠 Блок работы с памятью ===
    def _load_memory(self):
        """Загружает память пользователей из файла"""
        if os.path.exists(self.memory_file):
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    memory_data = json.load(f)
                user_count = len(memory_data)
                total_entries = sum(len(entries) for entries in memory_data.values())
                logging.info(f"🧠 Загружена память: {user_count} пользователей, {total_entries} записей")
                return memory_data
            except Exception as e:
                logging.error(f"❌ Ошибка загрузки памяти: {e}")
        else:
            logging.info("🧠 Файл памяти не найден, создаём новую память")
        return {}

    def _save_memory(self):
        """Сохраняет память пользователей в файл"""
        try:
            user_count = len(self.memory)
            total_entries = sum(len(entries) for entries in self.memory.values())
            
            with open(self.memory_file, "w", encoding="utf-8") as f:
                json.dump(self.memory, f, ensure_ascii=False, indent=2)
            
            logging.info(f"💾 Память сохранена: {user_count} пользователей, {total_entries} записей")
                
        except Exception as e:
            logging.error(f"❌ Ошибка сохранения памяти: {e}")

    def remember(self, user_id: int, key: str, value: str):
        """Добавляет или обновляет запись о пользователе"""
        user_id_str = str(user_id)
        
        if user_id_str not in self.memory:
            self.memory[user_id_str] = {}
            logging.info(f"🧠 Создан новый пользователь: {user_id_str}")
        
        self.memory[user_id_str][key] = value
        self._save_memory()
        logging.info(f"✅ Запись для {user_id_str}: {key} = '{value}'")

    def recall(self, user_id: int, key: str):
        """Извлекает запись о пользователе"""
        user_id_str = str(user_id)
        value = self.memory.get(user_id_str, {}).get(key)
        
        if value:
            logging.info(f"🔍 Найдена запись для {user_id_str}: {key} = '{value}'")
        else:
            logging.info(f"❓ Запись не найдена для {user_id_str}: {key}")
            
        return value

    def get_user_memory(self, user_id: int):
        """Возвращает всю память о пользователе"""
        user_id_str = str(user_id)
        memory = self.memory.get(user_id_str, {})
        logging.info(f"📊 Полная память пользователя {user_id_str}: {len(memory)} записей")
        return memory

    def remember_conversation_topic(self, user_id: int, message: str):
        """Запоминает тему разговора на основе сообщения"""
        try:
            topics = {
                'работа': ['работа', 'проект', 'задача', 'начальник', 'коллега'],
                'семья': ['семья', 'родители', 'дети', 'муж', 'жена', 'ребенок'],
                'отдых': ['отпуск', 'отдых', 'каникулы', 'путешествие', 'поездка'],
                'хобби': ['хобби', 'увлечение', 'рисование', 'музыка', 'спорт'],
                'здоровье': ['здоровье', 'болею', 'врач', 'боль', 'лекарство']
            }
            
            message_lower = message.lower()
            current_topic = None
            
            for topic, keywords in topics.items():
                if any(keyword in message_lower for keyword in keywords):
                    current_topic = topic
                    break
            
            if current_topic:
                self.remember(user_id, "last_topic", current_topic)
                logging.info(f"🏷️  Запомнена тема разговора для {user_id}: {current_topic}")
                
        except Exception as e:
            logging.error(f"❌ Ошибка анализа темы разговора: {e}")

    # === 🗣️ Блок контекстного диалога ===
    async def update_conversation_context(self, chat_id: str, last_bot_question: str = None):
        """Обновляет контекст диалога"""
        if chat_id not in self.conversation_context:
            self.conversation_context[chat_id] = {
                'last_bot_question': None,
                'last_interaction_time': None,
                'is_awaiting_reply': False
            }
        
        if last_bot_question:
            self.conversation_context[chat_id]['last_bot_question'] = last_bot_question
            self.conversation_context[chat_id]['is_awaiting_reply'] = True
        
        self.conversation_context[chat_id]['last_interaction_time'] = datetime.now()

    def should_continue_conversation(self, chat_id: str, user_message: str) -> bool:
        """Проверяет, является ли сообщение продолжением диалога"""
        if chat_id not in self.conversation_context:
            return False
        
        context = self.conversation_context[chat_id]
        
        # Проверяем таймаут (10 минут)
        if context['last_interaction_time']:
            time_diff = datetime.now() - context['last_interaction_time']
            if time_diff.total_seconds() > 600:
                return False
        
        # Если бот ожидает ответа на свой вопрос
        if context['is_awaiting_reply']:
            return True
        
        # Проверяем, является ли сообщение ответом на вопрос бота
        if context['last_bot_question'] and self._is_likely_response(user_message, context['last_bot_question']):
            return True
        
        return False
    
    def _is_likely_response(self, user_message: str, bot_question: str) -> bool:
        """Проверяет, похоже ли сообщение на ответ на вопрос бота"""
        user_msg_lower = user_message.lower()
        bot_question_lower = bot_question.lower()
        
        response_indicators = [
            'нормально', 'хорошо', 'отлично', 'плохо', 'устал', 'устала',
            'работаю', 'отдыхаю', 'сижу', 'стою', 'иду', 'ем', 'сплю',
            r'\d+', 'да', 'нет', 'возможно', 'наверное'
        ]
        
        for indicator in response_indicators:
            if re.search(indicator, user_msg_lower):
                return True
        
        question_keywords = self._extract_keywords(bot_question_lower)
        answer_keywords = self._extract_keywords(user_msg_lower)
        
        common_keywords = set(question_keywords) & set(answer_keywords)
        return len(common_keywords) >= 1
    
    def _extract_keywords(self, text: str) -> list:
        """Извлекает ключевые слова из текста"""
        stop_words = {'как', 'что', 'где', 'когда', 'почему', 'зачем', 'ты', 'вы', 'мне', 'тебе', 'вам'}
        words = re.findall(r'\b[а-яё]{3,}\b', text)
        return [word for word in words if word not in stop_words]
    
    def end_conversation(self, chat_id: str):
        """Завершает текущий диалог"""
        if chat_id in self.conversation_context:
            self.conversation_context[chat_id]['is_awaiting_reply'] = False

    # === 🔄 Основная логика ответов ===
    def _get_triggers(self):
        """Список триггеров для реакции"""
        return [
            r'\b[Бб]олтун\w*',
            r'\b[Пп]оболта\w+',
            r'\b[Пп]оговори\w+', 
            r'\b[Ээ]й\s*,\s*бот',
            r'\b[Пп]ривет\s*,\s*бот',
            r'\b[Бб]от\s*,\s*[Пп]ривет',
            r'\b[Пп]риветствую',
            r'\b[Пп]оздороваться',
            r'\b[Вв]ася\w*',
            r'\b[Вв]асилий\w*',
            r'\b[Бб]олт\w*',
        ]
    
    def should_respond(self, update: Update) -> bool:
        """Улучшенная логика ответа с учетом контекста"""
        if not update.message or not update.message.text:
            return False
            
        if update.message.from_user and update.message.from_user.is_bot:
            return False
            
        user_id = update.message.from_user.id
        message_text = update.message.text
        chat_id = str(update.effective_chat.id)

        # Проверяем продолжение диалога по контексту
        if self.should_continue_conversation(chat_id, message_text):
            logging.info("🔄 Продолжение диалога по контексту")
            return True

        # Проверяем триггеры
        triggers = '|'.join(self._get_triggers())
        if not re.search(triggers, message_text, re.IGNORECASE):
            return False

        # Обновляем статистику пользователя
        self.remember(user_id, "last_activity", datetime.now().isoformat())
        self.remember(user_id, "total_messages", 
                     int(self.recall(user_id, "total_messages") or 0) + 1)
        self.remember_conversation_topic(user_id, message_text)
        
        return True

    async def generate_contextual_response(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Генерация контекстного ответа с использованием истории чата"""
        user = update.message.from_user
        message_text = update.message.text
        user_name = user.first_name or "друг"
        user_id = user.id
        chat_id = str(update.effective_chat.id)

        # Добавляем различение пользователей с одинаковыми именами
        if user_name.lower() in ['надежда', 'надя']:
            # Создаем уникальный идентификатор для различения
            stored_identifier = self.recall(user_id, "name_identifier")
            if not stored_identifier:
                # Сохраняем короткий идентификатор для этой Надежды
                if user_id == 5307161226:  # ID первой Надежды
                    identifier = "Надежда (бабуля)"
                elif user_id == 5614316592:  # ID второй Надежды
                    identifier = "Надежда (бабушка)"
                else:
                    identifier = f"Надежда ({str(user_id)[-4:]})"  # Последние 4 цифры ID
                self.remember(user_id, "name_identifier", identifier)
                user_name = identifier
            else:
                user_name = stored_identifier

        logging.info(f"🧠 Начало обработки сообщения от {user_name} (ID: {user_id})")

        # Получаем память о пользователе
        user_memory = self.get_user_memory(user_id)
        known_name = self.recall(user_id, "name") or user_name
        mood = self.recall(user_id, "mood")
        last_topic = self.recall(user_id, "last_topic")

        # Если имя ещё не сохранено — запоминаем
        if not self.recall(user_id, "name"):
            self.remember(user_id, "name", user_name)

        # Получаем контекст из истории чата
        chat_history_context = self.history_manager.get_conversation_context(last_n=15)
        
        # Анализируем настроение
        if any(word in message_text.lower() for word in ["груст", "устал", "плохо", "уныл", "тоск"]):
            self.remember(user_id, "mood", "грустный")
            mood = "грустный"
        elif any(word in message_text.lower() for word in ["супер", "отлично", "весело", "классно", "рад", "счастлив"]):
            self.remember(user_id, "mood", "радостный")
            mood = "радостный"

        # Собираем контекст из памяти
        memory_context_parts = []
        if known_name:
            memory_context_parts.append(f"Собеседника зовут {known_name}.")
        if mood:
            memory_context_parts.append(f"Сейчас он в {mood} настроении.")
        if last_topic:
            memory_context_parts.append(f"Ранее обсуждали тему: {last_topic}.")
            
        memory_context = " ".join(memory_context_parts)

        # Формируем полный контекст для ИИ
        full_context = f"""
ИСТОРИЯ ЧАТА (последние сообщения):
{chat_history_context}

ИНФОРМАЦИЯ О СОБЕСЕДНИКЕ:
{memory_context}

ТЕКУЩЕЕ СООБЩЕНИЕ ОТ {user_name.upper()}: {message_text}
"""

        logging.info(f"🧠 Полный контекст для генерации ответа: {len(full_context)} символов")

        try:
            response = get_boltun_reply(user_name, message_text, full_context)
            if response and len(response.strip()) > 5:
                # Сохраняем ответ бота в историю
                self.history_manager.add_message(
                    from_user="Болтун",
                    from_id="bot",
                    text=response
                )
                
                # Запоминаем последний ответ бота
                self.remember(user_id, "last_bot_response", response)
                
                # Обновляем контекст диалога
                await self.update_conversation_context(chat_id, response)
                
                logging.info(f"✅ Сгенерирован ответ с использованием истории")
                return response.strip()
        except Exception as e:
            logging.error(f"❌ Ошибка генерации контекстного ответа: {e}")
            
        return self._get_fallback_response(user_name)
    
    def _get_fallback_response(self, user_name: str) -> str:
        """Запасные ответы"""
        hour = datetime.now().hour
        if 5 <= hour < 12:
            time_of_day = "доброе утро"
        elif 12 <= hour < 18:
            time_of_day = "добрый день" 
        elif 18 <= hour < 23:
            time_of_day = "добрый вечер"
        else:
            time_of_day = "доброй ночи"
            
        responses = [
            f"{time_of_day.capitalize()}, {user_name}! Рад тебя видеть! Как твои дела? 🌟",
            f"Привет, {user_name}! {time_of_day}! Что интересного расскажешь? 😊",
            f"О, {user_name}! {time_of_day}! Готов к общению! Как настроение?",
        ]
        
        return random.choice(responses)

    # === 📷 Обработка фото ===
    async def handle_photo_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик сообщений с изображениями"""
        try:
            if not update.message or not update.message.photo:
                return
                
            if not self._check_photo_flood_protection(update):
                return
                
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, 
                action="typing"
            )
            
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
            photo_bytes = await file.download_as_bytearray()
            
            analysis = await self.analyze_image(photo_bytes, update.message.from_user.first_name)
            
            if analysis:
                # Сохраняем в историю
                self.history_manager.add_message(
                    from_user=update.message.from_user.first_name,
                    from_id=str(update.message.from_user.id),
                    text="[Отправил фото]"
                )
                self.history_manager.add_message(
                    from_user="Болтун",
                    from_id="bot",
                    text=analysis
                )
                
                await update.message.reply_text(
                    analysis,
                    reply_to_message_id=update.message.message_id
                )
                logging.info(f"📷 Прокомментировал фото от {update.message.from_user.first_name}")
                
        except Exception as e:
            logging.error(f"Ошибка обработки фото: {e}")
            try:
                await update.message.reply_text(
                    "Интересное изображение! К сожалению, не могу его проанализировать прямо сейчас 🖼️",
                    reply_to_message_id=update.message.message_id
                )
            except:
                pass

    def _check_photo_flood_protection(self, update: Update) -> bool:
        """Защита от флуда изображениями"""
        user_id = update.message.from_user.id
        now = datetime.now()
        
        if user_id in self.user_stats:
            last_photo_time = self.user_stats[user_id].get('last_photo_interaction')
            if last_photo_time and (now - last_photo_time) < timedelta(seconds=30):
                return False
                
        if user_id not in self.user_stats:
            self.user_stats[user_id] = {}
        self.user_stats[user_id]['last_photo_interaction'] = now
        return True

    async def analyze_image(self, image_bytes: bytes, user_name: str) -> str:
        """Анализирует изображение и генерирует комментарий"""
        try:
            base64_image = base64.b64encode(image_bytes).decode('utf-8')
            analysis = await self._analyze_with_gpt4_vision(base64_image, user_name)
            return analysis
            
        except Exception as e:
            logging.error(f"Ошибка анализа изображения: {e}")
            return self._get_fallback_photo_response(user_name)

    async def _analyze_with_gpt4_vision(self, base64_image: str, user_name: str) -> str:
        """Анализ изображения через GPT-4 Vision"""
        from openai import OpenAI
        import os
        
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты дружелюбный собеседник, который смотрит фотографию. "
                        "Опиши своё впечатление естественным разговорным языком, как будто общаешься с другом."

                        "Следуй этим принципам:"
                        "1. Начни с эмоциональной реакции (""Ого, какая красота!"", ""Вау!"", ""О, классное фото!"")"
                        "2. Кратко опиши что видишь"
                        "3. Добавь личное впечатление или вопрос, например:"
                            "- Для пейзажей/архитектуры: спроси где это снято или поделись впечатлением"
                            "- Для фото людей: сделай искренний комплимент или отметь что-то особенное"
                            "- Для еды: прокомментируй как аппетитно выглядит"
                            "- Для животных: отметь их милые черты"

                        "Говори просто и по-дружески, используй 1-2 эмодзи. Ответ должен быть 2-3 предложения."
                    )
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text", 
                            "text": f"Привет! Пользователь {user_name} отправил это фото. Опиши что ты видишь и прокомментируй:"
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=300
        )
        
        return response.choices[0].message.content.strip()

    def _get_fallback_photo_response(self, user_name: str) -> str:
        """Запасные ответы для фото"""
        responses = [
            f"Ого, {user_name}! Интересное фото! 🖼️ Что это за момент?",
            f"Классное изображение, {user_name}! Расскажи о нём? 📸",
            f"Прикольно, {user_name}! Нравится мне эта картинка! 😊",
        ]
        return random.choice(responses)

    # === 📊 Команды ===
    async def show_memory_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда для просмотра памяти о пользователе"""
        user_id = update.message.from_user.id
        user_memory = self.get_user_memory(user_id)
        
        if user_memory:
            memory_text = "🧠 Что я о тебе помню:\n\n"
            for key, value in user_memory.items():
                memory_text += f"• {key}: {value}\n"
        else:
            memory_text = "🤔 Я ещё ничего не знаю о тебе. Давай пообщаемся!"
            
        await update.message.reply_text(memory_text)

    async def show_history_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда для просмотра истории чата"""
        recent_messages = self.history_manager.get_recent_messages(10)
        
        if recent_messages:
            history_text = "📖 Последние сообщения в чате:\n\n"
            for msg in recent_messages:
                user = msg.get("from", "Неизвестный")
                text = msg.get("text", "")
                if isinstance(text, list):
                    text = "".join([part if isinstance(part, str) else part.get("text", "") for part in text])
                history_text += f"• {user}: {text}\n"
            
            await update.message.reply_text(history_text)
        else:
            await update.message.reply_text("📖 История чата пока пуста. Давайте пообщаемся!")

    async def export_history_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда для экспорта истории"""
        user_id = update.message.from_user.id
        ADMINS = [323357522]  # Ваш Telegram ID
        
        if user_id not in ADMINS:
            await update.message.reply_text("❌ Эта команда только для администраторов")
            return
        
        try:
            export_filename = f"chat_history_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            
            with open(export_filename, "w", encoding="utf-8") as f:
                json.dump(self.history_manager.history, f, ensure_ascii=False, indent=2)
            
            with open(export_filename, "rb") as f:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=f,
                    filename=export_filename,
                    caption="📚 Экспорт истории чата"
                )
            
            os.remove(export_filename)
            
        except Exception as e:
            logging.error(f"❌ Ошибка экспорта истории: {e}")
            await update.message.reply_text("❌ Ошибка при экспорте истории")

# Глобальные экземпляры
_chat_handler = None

def init_chat_handler(intel_chat_function, history_manager):
    """Инициализация обработчика чата"""
    global _chat_handler
    _chat_handler = ChatHandler(intel_chat_function, history_manager)
    logging.info("✅ Чат-обработчик инициализирован с историей")
    return _chat_handler

def get_chat_handler():
    """Получение обработчика чата"""
    if _chat_handler is None:
        logging.error("❌ Чат-обработчик не инициализирован!")
    return _chat_handler
