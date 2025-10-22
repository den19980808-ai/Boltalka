import re
import logging
import random
import json
import os
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters

def get_boltun_reply(user_name, message):
    from openai import OpenAI
    import os

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты — Болтун, тёплый и живой собеседник. "
                    "Общайся с пользователями как с друзьями, без излишней вежливости и формальности. "
                    "Пиши естественно, иногда коротко, иногда с лёгким юмором. "
                    "Не задавай вопросы после каждого ответа, только если это уместно. "
                    "Если человек говорит что-то грустное — поддержи, но по-дружески, просто и тепло. "
                    "Иногда используй эмодзи, но не после каждого предложения. "
                    "Говори живо, как будто вы сидите на кухне и болтаете."
                )
            },
            {
                "role": "user",
                "content": f"{user_name}: {message}"
            }
        ]
    )

    return response.choices[0].message.content.strip()
    
class ChatHandler:
    def __init__(self, intel_chat_function, memory_file="memory_cache.json"):
        self.intel_chat = intel_chat_function
        self.conversations = {}  # Текущие диалоги
        self.user_stats = {}     # Статистика по пользователям
        self.memory_file = memory_file
        self.memory = self._load_memory()  # 🧠 Загружаем память из файла
        
    # === 🧠 Блок работы с памятью ===
    def _load_memory(self):
        """Загружает память пользователей из файла с детальным логированием"""
        if os.path.exists(self.memory_file):
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    memory_data = json.load(f)
                user_count = len(memory_data)
                total_entries = sum(len(entries) for entries in memory_data.values())
                logging.info(f"🧠 Загружена память: {user_count} пользователей, {total_entries} записей")
                
                # Логируем содержимое памяти для отладки
                for user_id, entries in memory_data.items():
                    logging.info(f"🧠 Пользователь {user_id}: {len(entries)} записей")
                    for key, value in entries.items():
                        logging.info(f"   📝 {key}: {value}")
                
                return memory_data
            except Exception as e:
                logging.error(f"❌ Ошибка загрузки памяти: {e}")
        else:
            logging.info("🧠 Файл памяти не найден, создаём новую память")
        return {}

    def _save_memory(self):
        """Сохраняет память пользователей в файл с логированием"""
        try:
            user_count = len(self.memory)
            total_entries = sum(len(entries) for entries in self.memory.values())
            
            with open(self.memory_file, "w", encoding="utf-8") as f:
                json.dump(self.memory, f, ensure_ascii=False, indent=2)
            
            logging.info(f"💾 Память сохранена: {user_count} пользователей, {total_entries} записей")
            
            # Логируем последнее состояние памяти
            for user_id, entries in self.memory.items():
                logging.info(f"💾 Пользователь {user_id}: {len(entries)} записей")
                
        except Exception as e:
            logging.error(f"❌ Ошибка сохранения памяти: {e}")

    def remember(self, user_id: int, key: str, value: str):
        """Добавляет или обновляет запись о пользователе с логированием"""
        user_id_str = str(user_id)
        old_value = None
        
        if user_id_str not in self.memory:
            self.memory[user_id_str] = {}
            logging.info(f"🧠 Создан новый пользователь: {user_id_str}")
        else:
            old_value = self.memory[user_id_str].get(key)
        
        self.memory[user_id_str][key] = value
        self._save_memory()
        
        if old_value:
            logging.info(f"🔄 Обновлена запись для {user_id_str}: {key} = '{old_value}' -> '{value}'")
        else:
            logging.info(f"✅ Новая запись для {user_id_str}: {key} = '{value}'")

    def recall(self, user_id: int, key: str):
        """Извлекает запись о пользователе с логированием"""
        user_id_str = str(user_id)
        value = self.memory.get(user_id_str, {}).get(key)
        
        if value:
            logging.info(f"🔍 Найдена запись для {user_id_str}: {key} = '{value}'")
        else:
            logging.info(f"❓ Запись не найдена для {user_id_str}: {key}")
            
        return value

    def get_user_memory(self, user_id: int):
        """Возвращает всю память о пользователе для отладки"""
        user_id_str = str(user_id)
        memory = self.memory.get(user_id_str, {})
        logging.info(f"📊 Полная память пользователя {user_id_str}: {len(memory)} записей")
        for key, value in memory.items():
            logging.info(f"   📖 {key}: {value}")
        return memory

    def remember_conversation_topic(self, user_id: int, message: str):
        """Запоминает тему разговора на основе сообщения"""
        try:
            # Простой анализ темы (можно улучшить с помощью ИИ)
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

    # === конец блока памяти ===

    async def handle_photo_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик сообщений с изображениями"""
        try:
            if not update.message or not update.message.photo:
                return
                
            # Проверяем анти-флуд
            if not self._check_photo_flood_protection(update):
                return
                
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, 
                action="typing"
            )
            
            # Получаем фото (берем самое большое качество)
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
            
            # Скачиваем изображение
            photo_bytes = await file.download_as_bytearray()
            
            # Анализируем изображение
            analysis = await self.analyze_image(photo_bytes, update.message.from_user.first_name)
            
            if analysis:
                await update.message.reply_text(
                    analysis,
                    reply_to_message_id=update.message.message_id
                )
                logging.info(f"Прокомментировал фото от {update.message.from_user.first_name}")
                
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
                
        # Обновляем статистику
        if user_id not in self.user_stats:
            self.user_stats[user_id] = {}
        self.user_stats[user_id]['last_photo_interaction'] = now
        self.user_stats[user_id]['photo_count'] = self.user_stats[user_id].get('photo_count', 0) + 1
        
        return True

    async def analyze_image(self, image_bytes: bytes, user_name: str) -> str:
        """Анализирует изображение и генерирует комментарий"""
        try:
            # Кодируем изображение в base64
            base64_image = base64.b64encode(image_bytes).decode('utf-8')
            
            # Используем GPT-4 Vision для анализа
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
            model="gpt-4o",  # Модель с поддержкой зрения
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты — Болтун, тёплый и живой собеседник в семейном чате. "
                        "Ты получаешь изображения от пользователей и комментируешь их в своём стиле: "
                        "дружелюбно, с юмором, тепло. "
                        "Опиши что видишь на изображении и добавь свой комментарий. "
                        "Будь естественным, как будто смотришь фото с друзьями. "
                        "Используй 1-2 эмодзи. Ответ должен быть коротким (1-3 предложения)."
                    )
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text", 
                            "text": f"Привет! Пользователь {user_name} отправил это фото. Опиши что ты видишь и прокомментируй в своём стиле:"
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
            f"Интересно, {user_name}! Что на этом фото? 🎨",
        ]
        return random.choice(responses)

    def _get_triggers(self):
        """Расширенный список триггеров"""
        return [
            r'\b[Бб]олтун\w*',
            r'\b[Пп]оболта\w+',
            r'\b[Пп]оговори\w+', 
            r'\b[Ээ]й\s*,\s*бот',
            r'\b[Пп]ривет\s*,\s*бот',
            r'\b[Бб]от\s*,\s*[Пп]ривет',
            r'\b[Пп]риветствую',
            r'\b[Пп]оздороваться'
        ]
    
    def should_respond(self, update: Update) -> bool:
        """Улучшенная логика ответа"""
        if not update.message or not update.message.text:
            return False
            
        if update.message.from_user and update.message.from_user.is_bot:
            return False
            
        user_id = update.message.from_user.id
        message_text = update.message.text.lower()

         # Запоминаем активность пользователя
        self.remember(user_id, "last_activity", datetime.now().isoformat())
        self.remember(user_id, "total_messages", 
                     int(self.recall(user_id, "total_messages") or 0) + 1)
        
        # Запоминаем тему разговора
        self.remember_conversation_topic(user_id, message_text)
        
        # Проверяем триггеры
        triggers = '|'.join(self._get_triggers())
        if not re.search(triggers, message_text, re.IGNORECASE):
            return False
            
        # Проверяем анти-флуд
        now = datetime.now()
        if user_id in self.user_stats:
            last_time = self.user_stats[user_id].get('last_interaction')
            if last_time and (now - last_time) < timedelta(seconds=10):
                return False
                
        # Обновляем статистику
        self.user_stats[user_id] = {
            'last_interaction': now,
            'message_count': self.user_stats.get(user_id, {}).get('message_count', 0) + 1
        }
        
        return True
    
    async def generate_contextual_response(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Генерация контекстного ответа"""
        user = update.message.from_user
        message_text = update.message.text
        user_name = user.first_name or "друг"
        user_id = user.id

        # Логируем начало обработки с памятью
        logging.info(f"🧠 Начало обработки сообщения от {user_name} (ID: {user_id})")

        # Получаем всю память о пользователе для отладки
        self.get_user_memory(user_id)
        
        known_name = self.recall(user_id, "name")
        mood = self.recall(user_id, "mood")
        last_topic = self.recall(user_id, "last_topic")
        total_messages = self.recall(user_id, "total_messages")

        # Если имя ещё не сохранено — запоминаем
        if not known_name:
            self.remember(user_id, "name", user_name)

        # Анализируем настроение по тексту
        if any(word in message_text.lower() for word in ["груст", "устал", "плохо", "уныл", "тоск"]):
            self.remember(user_id, "mood", "грустный")
            mood = "грустный"
        elif any(word in message_text.lower() for word in ["супер", "отлично", "весело", "классно", "рад", "счастлив"]):
            self.remember(user_id, "mood", "радостный")
            mood = "радостный"
        elif any(word in message_text.lower() for word in ["злой", "сердит", "разозлил", "бесит"]):
            self.remember(user_id, "mood", "сердитый")
            mood = "сердитый"
        
        # Определяем контекст разговора
        conversation_context = self.conversations.get(user_id, [])
        conversation_context.append(f"Пользователь: {message_text}")
        
        # Ограничиваем историю
        if len(conversation_context) > 6:
            conversation_context = conversation_context[-6:]
            
        context_text = "\n".join(conversation_context[-3:])  # Берем последние 3 реплики

         # Собираем контекст из памяти
        memory_context_parts = []
        if known_name:
            memory_context_parts.append(f"Собеседника зовут {known_name}.")
        if mood:
            memory_context_parts.append(f"Сейчас он в {mood} настроении.")
        if last_topic:
            memory_context_parts.append(f"Ранее обсуждали тему: {last_topic}.")
        if total_messages:
            memory_context_parts.append(f"Всего сообщений от него: {total_messages}.")
            
        memory_context = " ".join(memory_context_parts)

        # Логируем собранный контекст
        logging.info(f"🧠 Собранный контекст: {memory_context}")

        
        prompt = (
            f"Ты — дружелюбный собеседник в семейном чате. Тебе пишет {user_name}.\n\n"
            f"{memory_context}\n"
            f"КОНТЕКСТ РАЗГОВОРА:\n{context_text}\n\n"
            "ТВОЯ РОЛЬ:\n"
            "- Поддерживающий член семьи\n" 
            "- С чувством юмора, но без сарказма\n"
            "- Интересуешься жизнью собеседника\n"
            "- Краткие ответы (1-3 предложения)\n"
            "- Естественный разговорный стиль\n\n"
            "ОСОБЫЕ УКАЗАНИЯ:\n"
            "- Используй 0-2 уместных эмодзи\n"
            "- Задавай встречные вопросы\n"
            "- Помни предыдущие темы\n"
            "- Не будь навязчивым\n"
            "- Поддерживай позитивную атмосферу\n\n"
            "Сгенерируй только ответ:"
        )
        
        try:
            response = get_boltun_reply(user_name, message_text)
            if response and len(response.strip()) > 5:
                # Сохраняем контекст
                conversation_context.append(f"Бот: {response}")
                self.conversations[user_id] = conversation_context
                
                # Запоминаем последний ответ бота
                self.remember(user_id, "last_bot_response", response)
                
                logging.info(f"✅ Сгенерирован ответ с использованием памяти")
                return response.strip()
        except Exception as e:
            logging.error(f"❌ Ошибка генерации контекстного ответа: {e}")
            
        return self._get_fallback_response(user_name)
    
    def _get_fallback_response(self, user_name: str) -> str:
        """Запасные ответы с учетом времени суток"""
        hour = datetime.now().hour
        time_of_day = ""
        
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
            f"{user_name}, привет! {time_of_day}! Чем порадуешь?",
        ]
        
        return random.choice(responses)
    
    async def handle_chat_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик сообщений с улучшенной логикой"""
        if not self.should_respond(update):
            return
            
        try:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, 
                action="typing"
            )
            
            response = await self.generate_contextual_response(update, context)
            
            if response:
                await update.message.reply_text(
                    response,
                    reply_to_message_id=update.message.message_id
                )
                
                logging.info(f"✅ Ответил {update.message.from_user.first_name} с использованием памяти")
                
        except Exception as e:
            logging.error(f"Ошибка обработки сообщения: {e}")


# === Команда для просмотра памяти ===
    async def show_memory_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда для просмотра того, что бот запомнил о пользователе"""
        user_id = update.message.from_user.id
        user_memory = self.get_user_memory(user_id)
        
        if user_memory:
            memory_text = "🧠 Что я о тебе помню:\n\n"
            for key, value in user_memory.items():
                memory_text += f"• {key}: {value}\n"
        else:
            memory_text = "🤔 Я ещё ничего не знаю о тебе. Давай пообщаемся!"
            
        await update.message.reply_text(memory_text)

# Глобальные экземпляры
_chat_handler = None

def init_chat_handler(intel_chat_function):
    """Инициализация обработчика чата"""
    global _chat_handler
    _chat_handler = ChatHandler(intel_chat_function)
    logging.info("✅ Чат-обработчик инициализирован")
    return _chat_handler

def get_chat_handler():
    """Получение обработчика чата"""
    if _chat_handler is None:
        logging.error("❌ Чат-обработчик не инициализирован!")
    return _chat_handler
