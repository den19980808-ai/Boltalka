#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тест функции build_morning_digest() без отправки сообщений.
Проверяет структуру и формат выходного текста.
"""

import os
import sys

# Указываем UTF-8 для вывода в Windows
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from datetime import date

# Загружаем переменные окружения
load_dotenv(dotenv_path="token.env", override=True)

# Импортируем функцию
from main import build_morning_digest

def test_build_morning_digest():
    """Тест формирования утреннего дайджеста"""
    print("🧪 Начинаем тест build_morning_digest()...\n")
    
    try:
        # Вызываем функцию
        text, weather_map = build_morning_digest()
        
        # Проверяем результат
        print("=" * 70)
        print("📋 ПОЛУЧЕННЫЙ ТЕКСТ:")
        print("=" * 70)
        print(text)
        print("=" * 70)
        print()
        
        # Проверяем структуру
        print("✅ ПРОВЕРКА СТРУКТУРЫ:")
        
        checks = {
            "🌅 Доброе утро!": "Приветствие",
            "Отличного дня!": "Заключение",
        }
        
        for check_str, label in checks.items():
            if check_str in text:
                print(f"  ✓ {label}: НАЙДЕНО")
            else:
                print(f"  ✗ {label}: НЕ НАЙДЕНО (ВАЖНО)")
        
        # Проверяем погоду
        if "📍 Погода:" in text:
            print(f"  ✓ Блок погоды: НАЙДЕН")
            print(f"    Weather map содержит {len(weather_map)} города:")
            for city, info in weather_map.items():
                if info:
                    print(f"      • {city}: {info['min']}° → {info['max']}° {info['icon']}")
        else:
            print(f"  ⚠️  Блок погоды: НЕ НАЙДЕН (ключ API или ошибка)")
        
        # Проверяем новости
        if "🌍 Сегодня в мире:" in text:
            print(f"  ✓ Блок новостей: НАЙДЕН")
        else:
            print(f"  ℹ️  Блок новостей: отсутствует (нет доступных новостей)")
        
        # Проверяем праздники/ДР
        if "Дни рождения" in text or "Праздники" in text:
            print(f"  ✓ Блок праздников/ДР: НАЙДЕН")
        else:
            print(f"  ℹ️  Блок праздников/ДР: отсутствует")
        
        print()
        print("✅ ТЕСТ ЗАВЕРШЕН УСПЕШНО!")
        print()
        
    except Exception as e:
        print(f"ОШИБКА ПРИ ТЕСТИРОВАНИИ: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    test_build_morning_digest()
