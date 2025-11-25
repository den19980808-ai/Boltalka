#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тестирование blockquote и разделителя в пожелании"""

import os
import sys
from datetime import date, datetime
from dotenv import load_dotenv

load_dotenv(dotenv_path="token.env", override=True)

# Добавляем текущую директорию в path
sys.path.insert(0, os.path.dirname(__file__))

from main import build_morning_digest

print("=" * 60)
print("ТЕСТ: Блокквот и разделитель в пожелании")
print("=" * 60)

try:
    # Генерируем дайджест на сегодня
    text, weather = build_morning_digest()
    
    print("\n📋 ПОЛНЫЙ ДАЙДЖЕСТ:\n")
    print(text)
    
    print("\n" + "=" * 60)
    print("✅ ПРОВЕРКА ЭЛЕМЕНТОВ:")
    print("=" * 60)
    
    # Проверяем наличие разделителя
    if "─" * 40 in text:
        print("✅ Разделитель (линия) найден")
    else:
        print("❌ Разделитель не найден")
    
    # Проверяем наличие blockquote
    if "<blockquote>" in text and "</blockquote>" in text:
        print("✅ Blockquote тег найден")
        # Находим содержимое blockquote
        start = text.find("<blockquote>")
        end = text.find("</blockquote>")
        if start != -1 and end != -1:
            blockquote_content = text[start+12:end]
            print(f"📝 Содержимое blockquote:\n   {blockquote_content[:100]}...")
    else:
        print("❌ Blockquote тег не найден")
    
    # Проверяем порядок
    separator_pos = text.find("─" * 10)  # ищем хотя бы часть разделителя
    blockquote_pos = text.find("<blockquote>")
    
    if separator_pos != -1 and blockquote_pos != -1:
        if separator_pos < blockquote_pos:
            print("✅ Правильный порядок: разделитель → blockquote")
        else:
            print("❌ Неправильный порядок")
    
    print("\n" + "=" * 60)
    print("✅ ТЕСТ ЗАВЕРШЕН УСПЕШНО")
    print("=" * 60)
    
except Exception as e:
    print(f"\n❌ ОШИБКА: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
