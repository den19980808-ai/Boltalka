import json
import logging
import os
import tempfile
from datetime import datetime
from typing import List, Dict, Any

class ChatHistoryManager:
    def __init__(self, history_file: str = "chat_history.json"):
        self.history_file = history_file
        # файл кеша для append-only записей (каждая строка — JSON сообщения)
        self.cache_file = os.path.splitext(history_file)[0] + "_cache.jsonl"
        self.history = self._load_history()
        self._unsaved_count = 0  # счётчик для периодического снапшота
    
    def _atomic_write(self, path: str, data: Dict[str, Any]):
        """Атомарная запись JSON через временный файл + replace"""
        dirn = os.path.dirname(path) or "."
        os.makedirs(dirn, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dirn, prefix="._tmp_history_", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    def _load_history(self) -> Dict[str, Any]:
        """Загружает историю чата из JSON файла; при отсутствии или повреждении — создаёт базовую структуру.
           Также подтягивает сообщения из кеша (если есть) и объединяет их."""
        try:
            if os.path.exists(self.history_file):
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    logging.error("❌ Некорректный формат history file, восстанавлию базовую структуру")
                    data = {"name": "Семейная болталка", "type": "private_supergroup", "id": 1949890870, "messages": []}
            else:
                data = {"name": "Семейная болталка", "type": "private_supergroup", "id": 1949890870, "messages": []}

            if "messages" not in data or not isinstance(data["messages"], list):
                data["messages"] = []

            # Если есть кеш-файл — прочитаем и добавим сообщения, не дублируя (по id)
            if os.path.exists(self.cache_file):
                seen_ids = {m.get("id") for m in data.get("messages", []) if isinstance(m, dict) and "id" in m}
                try:
                    with open(self.cache_file, 'r', encoding='utf-8') as cf:
                        for line in cf:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                msg = json.loads(line)
                            except Exception:
                                continue
                            mid = msg.get("id")
                            if mid and mid not in seen_ids:
                                data["messages"].append(msg)
                                seen_ids.add(mid)
                    # кеш можно не удалять — он служит журналом append-only
                except Exception as e:
                    logging.warning(f"Не удалось прочитать кеш {self.cache_file}: {e}")
            return data
        except Exception as e:
            logging.error(f"❌ Ошибка загрузки истории: {e}")
            return {"name": "Семейная болталка", "type": "private_supergroup", "id": 1949890870, "messages": []}
    
    def _save_history(self, data: Dict[str, Any]):
        """Сохраняет основной history_file атомарно"""
        try:
            self._atomic_write(self.history_file, data)
        except Exception as e:
            logging.error(f"❌ Ошибка сохранения истории: {e}")
    
    def add_message(self, from_user: str, from_id: str, text: str, message_type: str = "message"):
        """Добавляет новое сообщение: дописывает в cache (append) и обновляет in-memory.
           Основной файл обновляется периодически, чтобы избежать полного перезаписи при каждом сообщении."""
        try:
            now = datetime.now()
            new_message = {
                "id": str(int(now.timestamp() * 1000)),
                "type": message_type,
                "date": now.isoformat(),
                "date_unixtime": int(now.timestamp()),
                "from": from_user,
                "from_id": from_id,
                "text": text,
                "text_entities": [{"type": "plain", "text": text}]
            }

            # Гарантируем список сообщений в памяти
            self.history.setdefault("messages", [])
            self.history["messages"].append(new_message)

            # Дописываем в кеш-файл (append, построчно JSON)
            try:
                dirn = os.path.dirname(self.cache_file)
                if dirn:
                    os.makedirs(dirn, exist_ok=True)
                with open(self.cache_file, 'a', encoding='utf-8') as cf:
                    cf.write(json.dumps(new_message, ensure_ascii=False) + "\n")
            except Exception as e:
                logging.error(f"❌ Ошибка записи в кеш-файл: {e}")

            # Периодически обновляем основной файл (например, каждые 50 сообщений)
            self._unsaved_count += 1
            if self._unsaved_count >= 50:
                try:
                    self._save_history(self.history)
                    self._unsaved_count = 0
                except Exception:
                    pass

            logging.info(f"💾 Сообщение добавлено в историю: {from_user}: {text[:50]}...")
            
        except Exception as e:
            logging.error(f"❌ Ошибка добавления сообщения в историю: {e}")
    
    def get_recent_messages(self, count: int = 50) -> List[Dict[str, Any]]:
        """Возвращает последние N сообщений из истории (по умолчанию 50)"""
        msgs = self.history.get("messages", [])
        return msgs[-count:] if msgs else []

    def get_conversation_context(self, user_id: str = None, last_n: int = 50, max_chars: int = 3000) -> str:
        """Возвращает контекст диалога для промпта.
           - last_n: сколько последних сообщений взять (по умолчанию 50)
           - max_chars: максимальное число символов итогового контекста (обрезаем старые сообщения)"""
        recent_messages = self.get_recent_messages(last_n)
        
        if not recent_messages:
            return "История чата пуста."
        
        context_lines = []
        for msg in recent_messages:
            user = msg.get("from", "Неизвестный")
            text = msg.get("text", "")
            if isinstance(text, list):
                # Обрабатываем сложные текстовые структуры
                text_parts = []
                for part in text:
                    if isinstance(part, str):
                        text_parts.append(part)
                    elif isinstance(part, dict):
                        text_parts.append(part.get("text", ""))
                text = "".join(text_parts)
            
            context_lines.append(f"{user}: {text}")

        # Обрезаем старые строки, если суммарная длина превышает max_chars
        joined = "\n".join(context_lines)
        if len(joined) <= max_chars:
            return joined
        # оставим последние куски: убираем самые старые строки до укладки в max_chars
        while len(joined) > max_chars and context_lines:
            context_lines.pop(0)
            joined = "\n".join(context_lines)
        return joined
    
    def get_user_messages(self, user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Возвращает сообщения конкретного пользователя"""
        user_messages = [msg for msg in self.history["messages"] if msg.get("from_id") == user_id]
        return user_messages[-limit:]
    
    def search_messages(self, keyword: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Ищет сообщения по ключевому слову"""
        results = []
        for msg in reversed(self.history["messages"]):
            text = msg.get("text", "")
            if isinstance(text, list):
                text = "".join([part if isinstance(part, str) else part.get("text", "") for part in text])
            
            if keyword.lower() in text.lower():
                results.append(msg)
                if len(results) >= limit:
                    break
        
        return results