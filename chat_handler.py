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
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    f"Ты — Болтун, тёплый и живой собеседник. "
                    f"Общайся с пользователями как с друзьями, без излишней вежливости и формальности. "
                    f"Пиши естественно, иногда коротко, иногда чуть с юмором. "
                    f"Не задавай вопрос после каждого ответа, если это неуместно. "
                    f"Если человек говорит что-то грустное — поддержи, но не как психолог, а как близкий приятель. "
                    f"Используй иногда смайлики, но не после каждого предложения."
                )
            },
            {"role": "user", "content": f"{user_name}: {message}"}
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
        """Загружает память пользователей из файла."""
        if os.path.exists(self.memory_file):
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logging.error(f"Ошибка загрузки памяти: {e}")
        return {}

    def _save_memory(self):
        """Сохраняет память пользователей в файл."""
        try:
            with open(self.memory_file, "w", encoding="utf-8") as f:
                json.dump(self.memory, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"Ошибка сохранения памяти: {e}")

    def remember(self, user_id: int, key: str, value: str):
        """Добавляет или обновляет запись о пользователе."""
        if str(user_id) not in self.memory:
            self.memory[str(user_id)] = {}
        self.memory[str(user_id)][key] = value
        self._save_memory()

    def recall(self, user_id: int, key: str):
        """Извлекает запись о пользователе."""
        return self.memory.get(str(user_id), {}).get(key)

    # === конец блока памяти ===

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
        
        # Проверяем триггеры
        triggers = '|'.join(self._get_triggers())
        if not re.search(triggers, message_text, re.IGNORECASE):
            return False
            
        # Проверяем анти-флуд
        now = datetime.now()
        if user_id in self.user_stats:
            last_time = self.user_stats[user_id].get('last_interaction')
            if last_time and (now - last_time) < timedelta(seconds=20):
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
        known_name = self.recall(user_id, "name")
        mood = self.recall(user_id, "mood")

        # Если имя ещё не сохранено — запоминаем
        if not known_name:
            self.remember(user_id, "name", user_name)

        # Анализируем настроение по тексту
        if any(word in message_text.lower() for word in ["груст", "устал", "плохо"]):
            self.remember(user_id, "mood", "грустный")
        elif any(word in message_text.lower() for word in ["супер", "отлично", "весело", "классно"]):
            self.remember(user_id, "mood", "радостный")
        
        # Определяем контекст разговора
        conversation_context = self.conversations.get(user_id, [])
        conversation_context.append(f"Пользователь: {message_text}")
        
        # Ограничиваем историю
        if len(conversation_context) > 6:
            conversation_context = conversation_context[-6:]
            
        context_text = "\n".join(conversation_context[-3:])  # Берем последние 3 реплики

        # Включаем это в контекст
        memory_context = ""
        if known_name:
            memory_context += f"Ты уже знаешь, что собеседника зовут {known_name}. "
        if mood:
            memory_context += f"Ранее он был в {mood} настроении. "

        
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
                return response.strip()
        except Exception as e:
            logging.error(f"Ошибка генерации контекстного ответа: {e}")
            
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
                
                logging.info(f"Ответил {update.message.from_user.first_name}: {response[:50]}...")
                
        except Exception as e:
            logging.error(f"Ошибка обработки сообщения: {e}")

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
