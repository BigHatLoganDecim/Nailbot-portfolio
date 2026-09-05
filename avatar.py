"""
Одноразовый скрипт для установки аватарки бота (анимированной mp4 или картинки).

Запуск:
    python avatar.py                  # ищет wizard_avatar.mp4 в текущей папке
    python avatar.py path/to/file.mp4 # явный путь

Токен ищет в таком порядке:
    1. переменная окружения BOT_TOKEN
    2. файл .env
    3. интерактивный ввод
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


def load_token() -> str:
    """Достать BOT_TOKEN откуда возможно."""
    # 1. Уже в окружении?
    token = os.getenv("BOT_TOKEN", "").strip()
    if token and token != "ВСТАВЬ_СЮДА_СВОЙ_ТОКЕН_БОТА":
        return token

    # 2. Пробуем .env
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("BOT_TOKEN="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v and ":" in v:
                    return v

    # 3. Интерактивный ввод
    print("BOT_TOKEN не найден ни в окружении, ни в .env.")
    token = input("Вставь токен (или Ctrl+C для отмены): ").strip()
    return token


def find_avatar_file(arg: str | None) -> Path:
    """Найти файл аватарки."""
    if arg:
        p = Path(arg)
        if p.is_file():
            return p
        raise FileNotFoundError(f"Файл не найден: {arg}")

    here = Path(__file__).resolve().parent
    candidates = [
        here / "wizard_avatar.mp4",
        here.parent / "wizard_avatar.mp4",
        here / "Avatar" / "wizard_avatar.mp4",
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(
        f"Не нашёл wizard_avatar.mp4 в {here} или соседних папках. "
        "Положи файл рядом со скриптом или передай путь аргументом."
    )


async def main() -> None:
    from aiogram import Bot
    from aiogram.types import FSInputFile, InputProfilePhotoAnimated, InputProfilePhotoStatic

    token = load_token()
    if not token or token == "ВСТАВЬ_СЮДА_СВОЙ_ТОКЕН_БОТА":
        print("❌ Токен не задан. Прерываю.")
        return

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    path = find_avatar_file(arg)

    is_animated = path.suffix.lower() in (".mp4", ".gif", ".webm")
    print(f"📤 Загружаю аватарку: {path.name} "
          f"({'анимированная' if is_animated else 'статичная'})")

    bot = Bot(token=token)
    try:
        if is_animated:
            await bot.set_my_profile_photo(
                photo=InputProfilePhotoAnimated(animation=FSInputFile(str(path)))
            )
        else:
            await bot.set_my_profile_photo(
                photo=InputProfilePhotoStatic(photo=FSInputFile(str(path)))
            )
        print("✅ Аватарка успешно установлена! Telegram обновит её в течение минуты.")
    except Exception as e:
        print(f"❌ Не удалось установить аватарку: {e}")
        print("\nЕсли видишь ошибку про размер — Telegram требует:")
        print("  • mp4: до 2 MB, до 10 секунд, без звука, квадратное")
        print("  • картинка: до 5 MB, квадратная (или близко)")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
