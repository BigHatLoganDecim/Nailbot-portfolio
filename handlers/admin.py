"""
Админские хендлеры: рассылка, бэкап, очистка напоминаний, Sentry-test.

cmd_reload остаётся в main.py — он мутирует global kb, и тащить эту
зависимость через ctx с обратным колбэком сложнее, чем оставить 16 строк
на месте.

Внешний API:
  - backup_state_to_admin(force=...) — cron 21:00 (если есть записи сегодня) и /backup
  - cmd_broadcast_start(message) — из menu
  - cmd_clear_reminders(message) — из menu
"""

from __future__ import annotations

import gc
import gzip
import json
import logging
import os
import tempfile
import threading
import time
from typing import Any

from telebot import types

log = logging.getLogger("nailbot.handlers.admin")


def register(ctx) -> dict:
    """
    Регистрирует админские хендлеры. Возвращает публичные функции.

    ctx должен содержать: bot, state_store, config, scheduler,
    now_moscow, get_state, save_state, STEP_BROADCAST_TEXT, STEP_IDLE,
    db (опционально, для repair команд).
    """
    bot = ctx.bot
    state_store = ctx.state_store
    config = ctx.config
    scheduler = ctx.scheduler
    db = getattr(ctx, "db", None)  # Long-term database for repairs

    @bot.message_handler(commands=["clear_reminders", "сбросить_напоминания"])
    def cmd_clear_reminders(message: types.Message) -> None:
        """Админская команда: отменить ВСЕ запланированные напоминания (на случай тестов)."""
        if message.chat.id not in config.admin_ids:
            return
        removed = 0
        if scheduler:
            for job in scheduler.get_jobs():
                try:
                    job.remove()
                    removed += 1
                except Exception as e:
                    log.warning("clear_reminders: не смог удалить job %s: %s", job.id, e)
        bot.send_message(
            message.chat.id,
            f"🧹 Сброшено: {removed} напоминаний в очереди. "
            "События в Google Calendar нужно удалить вручную.",
        )

    @bot.message_handler(commands=["clear_my_bookings"])
    def cmd_clear_my_bookings(message: types.Message) -> None:
        """Админская команда: очистить ТВОЮ историю «Мои записи» (для тестов)."""
        if message.chat.id not in config.admin_ids:
            return
        state = ctx.get_state(message.chat.id)
        n = len(state.get("bookings", []))
        state["bookings"] = []
        ctx.save_state(message.chat.id, state)
        bot.send_message(message.chat.id, f"🧹 Удалено записей из истории: {n}")

    @bot.message_handler(commands=["broadcast", "рассылка"])
    def cmd_broadcast_start(message: types.Message) -> None:
        """Админская команда массовой рассылки. Шаг 1: попросить текст."""
        if message.chat.id not in config.admin_ids:
            return
        state = ctx.get_state(message.chat.id)
        state["step"] = ctx.STEP_BROADCAST_TEXT
        ctx.save_state(message.chat.id, state)
        bot.send_message(
            message.chat.id,
            "📣 <b>Рассылка</b>\n\n"
            "Напиши текст одним сообщением. Поддерживается HTML:\n"
            "<code>&lt;b&gt;жирный&lt;/b&gt;</code>, <code>&lt;i&gt;курсив&lt;/i&gt;</code>, "
            "<code>&lt;a href=\"...\"&gt;ссылка&lt;/a&gt;</code>.\n\n"
            "Для отмены — /cancel",
            parse_mode="HTML",
        )

    @bot.callback_query_handler(func=lambda c: c.data.startswith("br:"))
    def on_broadcast_callback(call: types.CallbackQuery) -> None:
        if call.from_user.id not in config.admin_ids:
            bot.answer_callback_query(call.id, "Только для админа")
            return
        action = call.data.split(":", 1)[1]
        state = ctx.get_state(call.from_user.id)
        pending = state.get("pending_broadcast") or {}

        if action == "cancel":
            state["pending_broadcast"] = None
            state["step"] = ctx.STEP_IDLE
            ctx.save_state(call.from_user.id, state)
            bot.answer_callback_query(call.id, "Отменено")
            try:
                bot.edit_message_text("Рассылка отменена.",
                                        chat_id=call.message.chat.id,
                                        message_id=call.message.message_id)
            except Exception:
                pass
            return

        if action == "send":
            bc_text = pending.get("text")
            if not bc_text:
                bot.answer_callback_query(call.id, "Текста не нашёл")
                return
            # Защита от двойного клика — Redis-lock на 15 минут (рассылка может идти долго)
            if state_store.redis:
                try:
                    acquired = state_store.redis.set(
                        "nailbot:lock:broadcast", "1", nx=True, ex=15 * 60)
                    if not acquired:
                        bot.answer_callback_query(call.id,
                            "Уже запущена рассылка — подожди отчёт", show_alert=True)
                        return
                except Exception:
                    pass
            state["pending_broadcast"] = None
            state["step"] = ctx.STEP_IDLE
            ctx.save_state(call.from_user.id, state)
            bot.answer_callback_query(call.id, "Запускаю...")
            try:
                bot.edit_message_text(
                    "📣 Рассылка запущена. Жди итоговый отчёт.",
                    chat_id=call.message.chat.id, message_id=call.message.message_id,
                )
            except Exception:
                pass

            admin_id = call.from_user.id

            def _send_all() -> None:
                user_ids = state_store.all_users()
                sent = 0
                failed = 0
                for uid in user_ids:
                    if uid == admin_id:
                        continue  # себе не шлём
                    try:
                        bot.send_message(uid, bc_text, parse_mode="HTML")
                        sent += 1
                    except Exception as e:
                        failed += 1
                        log.debug("broadcast → %s failed: %s", uid, e)
                    time.sleep(0.05)  # ~20 msg/sec, безопасно для Telegram API
                try:
                    bot.send_message(
                        admin_id,
                        f"📣 <b>Рассылка завершена</b>\n\n"
                        f"✅ Доставлено: <b>{sent}</b>\n"
                        f"❌ Не доставлено: <b>{failed}</b>",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    log.warning("broadcast report: %s", e)
                # Снимаем lock в любом случае — рассылка завершилась
                if state_store.redis:
                    try:
                        state_store.redis.delete("nailbot:lock:broadcast")
                    except Exception:
                        pass

            threading.Thread(target=_send_all, daemon=True).start()
            return

    def _has_bookings_today() -> bool:
        """Есть ли записи на сегодняшний день (МСК) — SQLite и/или Redis state."""
        day = ctx.now_moscow().date().isoformat()
        if db is not None:
            try:
                n = db.bookings.count_bookings_on_day(day)
                if n > 0:
                    return True
            except Exception as e:
                log.warning("backup: count_bookings_on_day: %s", e)
        try:
            for uid in state_store.all_users():
                st = state_store.get(uid) or {}
                for b in st.get("bookings") or []:
                    iso = (b.get("datetime_iso") or "")[:10]
                    if iso != day:
                        continue
                    status = (b.get("status") or "confirmed").lower()
                    if status in ("confirmed", "completed", ""):
                        return True
        except Exception as e:
            log.warning("backup: scan state for today's bookings: %s", e)
        return False

    def _backup_state_to_admin(force: bool = False) -> None:
        """
        Собирает state'ы клиентов из Redis в gzip-JSON и шлёт админам в Telegram.

        force=False (cron): только если на сегодня (МСК) есть записи.
        force=True (/backup, меню): всегда.

        Память (Render free 512 MB): стримим в temp-файл по одному клиенту,
        без гигантского dict+json в RAM; после отправки файл удаляем.
        """
        if not state_store.redis:
            log.info("backup: Redis отключён — нечего бэкапить")
            return
        if not config.admin_ids:
            log.info("backup: ADMIN_IDS не задан — бэкап некому отправлять")
            return

        if not force and not _has_bookings_today():
            log.info("backup: нет записей на сегодня (МСК) — пропуск отправки в TG")
            return

        tmp_path: str | None = None
        try:
            now = ctx.now_moscow()
            brand_slug = ctx.kb.brand.get("short_name", "bot").lower().replace(" ", "_")
            fname = f"{brand_slug}_backup_{now:%Y-%m-%d_%H%M}.json.gz"

            fd, tmp_path = tempfile.mkstemp(prefix="nailbot_backup_", suffix=".json.gz")
            os.close(fd)

            clients_count = 0
            with gzip.open(tmp_path, "wt", encoding="utf-8") as gz:
                gz.write('{"created_at":')
                gz.write(json.dumps(now.isoformat(timespec="seconds")))
                gz.write(',"clients":{')
                first = True
                for uid in state_store.all_users():
                    try:
                        state = state_store.get(uid) or {}
                        if not state:
                            continue
                        if not first:
                            gz.write(",")
                        first = False
                        gz.write(json.dumps(str(uid)))
                        gz.write(":")
                        gz.write(json.dumps(state, ensure_ascii=False, default=str))
                        clients_count += 1
                        # не держим state дольше одной итерации
                        del state
                    except Exception as e:
                        log.debug("backup: пропустил uid=%s: %s", uid, e)

                gz.write('},"redis_extras":')
                extras: dict[str, Any] = {}
                try:
                    for key in state_store.redis.scan_iter("nailbot:psy:*", count=100):
                        if isinstance(key, bytes):
                            key = key.decode()
                        try:
                            val = state_store.redis.get(key)
                            if isinstance(val, bytes):
                                val = val.decode()
                            extras[key] = val
                        except Exception:
                            pass
                except Exception:
                    pass
                gz.write(json.dumps(extras, ensure_ascii=False, default=str))
                gz.write(',"clients_count":')
                gz.write(str(clients_count))
                gz.write("}")
                del extras

            size_kb = os.path.getsize(tmp_path) / 1024
            caption = (
                f"💾 Бэкап {ctx.kb.brand.get('name', 'Salon')}\n"
                f"📅 {now:%d.%m.%Y %H:%M} МСК\n"
                f"👥 Клиентов: {clients_count}\n"
                f"📦 Размер сжатого: {size_kb:.1f} KB"
            )
            for admin_id in config.admin_ids:
                try:
                    with open(tmp_path, "rb") as f:
                        bot.send_document(
                            admin_id,
                            document=(fname, f),
                            caption=caption,
                        )
                except Exception as e:
                    log.warning("backup: не отправил админу %s: %s", admin_id, e)

            log.info("backup: отправлено админам, clients=%d, size_kb=%.1f",
                     clients_count, size_kb)
        except Exception as e:
            log.exception("backup: критическая ошибка: %s", e)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            gc.collect()

        # SQLite — отдельный файл, тоже temp → TG → delete (важно для CRM)
        _backup_sqlite_to_admin()

    def _backup_sqlite_to_admin() -> None:
        """Консистентная копия SQLite (backup API) → gzip → Telegram → удалить temp."""
        if not config.admin_ids:
            return
        tmp_db: str | None = None
        tmp_gz: str | None = None
        try:
            from database.connection import get_db_path
            import sqlite3

            db_path = get_db_path()
            if not os.path.isfile(db_path):
                log.info("backup sqlite: файл %s не найден — пропуск", db_path)
                return

            fd, tmp_db = tempfile.mkstemp(prefix="nailbot_sqlite_", suffix=".db")
            os.close(fd)
            src = sqlite3.connect(db_path, timeout=30)
            try:
                dst = sqlite3.connect(tmp_db)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()

            now = ctx.now_moscow()
            brand_slug = ctx.kb.brand.get("short_name", "bot").lower().replace(" ", "_")
            fname = f"{brand_slug}_sqlite_{now:%Y-%m-%d_%H%M}.db.gz"
            fd2, tmp_gz = tempfile.mkstemp(prefix="nailbot_sqlite_", suffix=".db.gz")
            os.close(fd2)
            with open(tmp_db, "rb") as raw_f, gzip.open(tmp_gz, "wb") as gz:
                while True:
                    chunk = raw_f.read(1024 * 256)
                    if not chunk:
                        break
                    gz.write(chunk)

            size_kb = os.path.getsize(tmp_gz) / 1024
            caption = (
                f"🗄 SQLite CRM\n"
                f"📅 {now:%d.%m.%Y %H:%M} МСК\n"
                f"📦 {size_kb:.1f} KB (gzip)\n"
                f"<i>Храните вне Render. После redeploy free-диска файл может обнулиться.</i>"
            )
            for admin_id in config.admin_ids:
                try:
                    with open(tmp_gz, "rb") as f:
                        bot.send_document(
                            admin_id,
                            document=(fname, f),
                            caption=caption,
                            parse_mode="HTML",
                        )
                except Exception as e:
                    log.warning("backup sqlite: не отправил %s: %s", admin_id, e)
            log.info("backup sqlite: отправлено, size_kb=%.1f path=%s", size_kb, db_path)
        except Exception as e:
            log.exception("backup sqlite: %s", e)
        finally:
            for p in (tmp_db, tmp_gz):
                if p:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
            gc.collect()

    @bot.message_handler(commands=["llm_test", "llm_check", "мозги"])
    def cmd_llm_test(message: types.Message) -> None:
        """Пингует LLM и показывает: провайдер, base_url, какая модель реально
        ответила, latency. Видно, сработал ли auto-fallback (если модель в
        last_usage отличается от запрошенной).

        Использование:
          /llm_test                — oracle + parser
          /llm_test all            — oracle, parser, summary
          /llm_test oracle|parser|summary  — только один
        """
        if message.chat.id not in config.admin_ids:
            return

        llm = getattr(ctx, "llm", None)
        if llm is None:
            bot.send_message(message.chat.id, "LLM-клиент не инициализирован.")
            return

        args = (message.text or "").split()[1:]
        arg = (args[0].lower() if args else "").strip()
        if arg in ("all", "все", "a"):
            features = ["oracle", "parser", "summary"]
        elif arg in ("oracle", "parser", "summary"):
            features = [arg]
        else:
            features = ["oracle", "parser"]

        key = llm.api_key or ""
        key_masked = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else ("задан" if key else "❌ нет")
        provider = getattr(config, "llm_provider", "?")
        header = [
            "🧠 <b>LLM check</b>",
            f"Provider: <code>{provider}</code>",
            f"Base URL: <code>{llm.base_url}</code>",
            f"Key: {key_masked}",
            f"Fallback: <code>{llm.fallback_model}</code>",
        ]
        if not llm.enabled:
            header.append("\n⚠️ Клиент disabled (нет ключа) — пинг не запускаю.")
            bot.send_message(message.chat.id, "\n".join(header), parse_mode="HTML")
            return

        # Тестовые запросы — короткие, дёшево по токенам.
        # Уникальная фраза в ответе → видно, что модель реально что-то сгенерила.
        probes = {
            "oracle":  (llm.model_oracle,
                        lambda: llm.psy_chat([
                            {"role": "system", "content": "Отвечай одной короткой фразой."},
                            {"role": "user", "content": "Скажи 'pong' и одно слово про маникюр."},
                        ])),
            "parser":  (llm.model_parser,
                        lambda: llm._chat(
                            llm.model_parser,
                            [{"role": "user", "content": "Ответь одним словом: pong"}],
                            temperature=0.0, max_tokens=10)),
            "summary": (llm.model_summary,
                        lambda: llm._chat(
                            llm.model_summary,
                            [{"role": "user", "content": "Ответь одним словом: pong"}],
                            temperature=0.0, max_tokens=10)),
        }

        import time as _time
        lines = list(header) + [""]
        for feat in features:
            model_requested, fn = probes[feat]
            lines.append(f"🎯 <b>{feat}</b> (<code>{model_requested}</code>)")
            t0 = _time.monotonic()
            try:
                resp = fn()
                dt = _time.monotonic() - t0
            except Exception as e:
                dt = _time.monotonic() - t0
                lines.append(f"  ❌ {dt:.2f}s · исключение: <code>{str(e)[:200]}</code>")
                continue

            if not resp:
                lines.append(f"  ❌ {dt:.2f}s · пустой ответ (см. логи)")
                continue

            actual = (llm.last_usage or {}).get("model") or "?"
            fallback_note = " ⚠️ <i>fallback</i>" if actual and actual != model_requested else ""
            preview = resp.strip().replace("\n", " ")
            if len(preview) > 120:
                preview = preview[:117] + "…"
            usage = llm.last_usage or {}
            lines.append(
                f"  ✅ {dt:.2f}s · модель: <code>{actual}</code>{fallback_note}"
                f" · in/out: {usage.get('in', 0)}/{usage.get('out', 0)}"
            )
            from html import escape as _html_escape
            lines.append(f"  ↳ <i>{_html_escape(preview)}</i>")

        bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")

    @bot.message_handler(commands=["sentry_test"])
    def cmd_sentry_test(message: types.Message) -> None:
        """Триггерит тестовое исключение — проверить что Sentry ловит. Только для админа."""
        if message.chat.id not in config.admin_ids:
            return
        bot.send_message(message.chat.id,
            "🧪 Бросаю тестовое исключение. Через ~1 минуту проверь Sentry — должна появиться ошибка "
            f"'{ctx.kb.brand.get('name', 'Bot')} Sentry test from admin'.")
        raise RuntimeError(f"{ctx.kb.brand.get('name', 'Bot')} Sentry test from admin")

    @bot.message_handler(commands=["find_inconsistencies", "найти_проблемы"])
    def cmd_find_inconsistencies(message: types.Message) -> None:
        """Ищет клиентов с потенциальными расхождениями между Redis и long-term DB.
        Только для админа."""
        if message.chat.id not in config.admin_ids:
            return
        if db is None:
            bot.send_message(message.chat.id, "Long-term DB недоступна.")
            return

        try:
            bot.send_message(message.chat.id, "🔍 Ищу клиентов с возможными расхождениями...")

            suspicious = []
            for uid in state_store.all_users()[:500]:
                try:
                    cs = state_store.get(uid) or {}
                    redis_visits = cs.get("total_visits", 0)
                    if redis_visits == 0:
                        try:
                            client_row = db.clients.get_client(uid)
                            if client_row and client_row.get("total_visits", 0) > 0:
                                suspicious.append((uid, redis_visits, client_row.get("total_visits")))
                        except Exception:
                            pass
                except Exception:
                    pass

            if suspicious:
                text = "⚠️ Найдены потенциальные проблемы:\n"
                for uid, redis_v, db_v in suspicious[:20]:
                    text += f"• {uid}: Redis={redis_v}, DB={db_v}\n"
                bot.send_message(message.chat.id, text)
            else:
                bot.send_message(message.chat.id, "✅ Явных расхождений среди активных клиентов не найдено.")
        except Exception as e:
            bot.send_message(message.chat.id, f"Ошибка при сканировании: {e}")

    @bot.message_handler(commands=["repair_bonuses", "починить_бонусы"])
    def cmd_repair_bonuses(message: types.Message) -> None:
        """Пересчитать только дизайн-бонусы клиента (без визитов).
        Использование: /repair_bonuses <user_id>"""
        if message.chat.id not in config.admin_ids:
            return

        args = message.text.split()
        if len(args) < 2:
            bot.send_message(message.chat.id, "Использование: /repair_bonuses <user_id>")
            return

        try:
            target_user_id = int(args[1])
        except ValueError:
            bot.send_message(message.chat.id, "user_id должен быть числом.")
            return

        if db is None:
            bot.send_message(message.chat.id, "Long-term DB недоступна.")
            return

        try:
            used = db.bookings.count_design_bonuses_used(target_user_id)
            brought = db.clients.get_referrals_brought(target_user_id)
            correct = max(0, brought - used)

            db.clients.update_referral_info(target_user_id, design_bonuses=correct)

            cs = state_store.get(target_user_id) or {}
            cs.setdefault("card", {})["design_bonuses"] = correct
            state_store.set(target_user_id, cs)

            bot.send_message(
                message.chat.id,
                f"✅ Дизайн-бонусы для {target_user_id} пересчитаны.\n"
                f"Приведено рефералов: {brought}\n"
                f"Использовано: {used}\n"
                f"Новый баланс: {correct}"
            )
            log.info("admin: repair_bonuses for user=%s → %s", target_user_id, correct)
        except Exception as e:
            bot.send_message(message.chat.id, f"Ошибка: {e}")
            log.exception("repair_bonuses failed")

    @bot.message_handler(commands=["backup", "бэкап"])
    def cmd_backup(message: types.Message) -> None:
        """Ручной запуск бэкапа. Только для админа. Всегда (force), даже без записей сегодня."""
        if message.chat.id not in config.admin_ids:
            return
        bot.send_message(message.chat.id, "💾 Запускаю бэкап...")
        _backup_state_to_admin(force=True)

    @bot.message_handler(commands=["repair_visits", "починить_визиты"])
    def cmd_repair_visits(message: types.Message) -> None:
        """Админская команда для починки данных клиента.
        Использование:
          /repair_visits <user_id>           → починить визиты
          /repair_visits <user_id> bonuses   → визиты + дизайн-бонусы
          /repair_visits <user_id> audit     → показать проблемы без изменений"""
        if message.chat.id not in config.admin_ids:
            return

        args = message.text.split()
        if len(args) < 2:
            bot.send_message(message.chat.id, "Использование: /repair_visits <user_id> [bonuses|audit]")
            return

        try:
            target_user_id = int(args[1])
        except ValueError:
            bot.send_message(message.chat.id, "user_id должен быть числом.")
            return

        mode = (args[2].lower() if len(args) > 2 else "visits")
        is_audit = mode == "audit"
        repair_bonuses = mode in ("bonuses", "bonus")

        if db is None:
            bot.send_message(message.chat.id, "Long-term DB недоступна.")
            return

        try:
            cs = state_store.get(target_user_id) or {}
            redis_visits = cs.get("total_visits", "N/A")
            db_visits = db.repair_client_visits(target_user_id)

            if not is_audit:
                cs["total_visits"] = db_visits
                state_store.set(target_user_id, cs)

            lines = [f"User {target_user_id}:"]
            lines.append(f"  Redis visits:   {redis_visits}")
            lines.append(f"  DB visits:      {db_visits}")

            if repair_bonuses or is_audit:
                try:
                    used = db.bookings.count_design_bonuses_used(target_user_id)
                    brought = db.clients.get_referrals_brought(target_user_id)
                    current = (cs.get("card") or {}).get("design_bonuses", 0)
                    correct = max(0, brought - used)

                    if not is_audit:
                        db.clients.update_referral_info(target_user_id, design_bonuses=correct)
                        cs.setdefault("card", {})["design_bonuses"] = correct
                        state_store.set(target_user_id, cs)

                    lines.append(f"  Referrals brought: {brought}")
                    lines.append(f"  Bonuses used:      {used}")
                    lines.append(f"  Current bonuses:   {current}")
                    lines.append(f"  Correct bonuses:   {correct}")
                except Exception as ex:
                    lines.append(f"  ⚠️ Bonus check/repair failed: {ex}")

            if is_audit:
                lines.append("\n(Audit mode — no writes performed)")
            else:
                lines.append("\n✅ Changes applied.")

            bot.send_message(message.chat.id, "\n".join(lines))
            log.info("admin: repair_visits user=%s mode=%s", target_user_id, mode)
        except Exception as e:
            bot.send_message(message.chat.id, f"Ошибка: {e}")
            log.exception("repair_visits failed")

    @bot.message_handler(commands=["repair_booking", "починить_запись"])
    def cmd_repair_booking(message: types.Message) -> None:
        """Изменить статус конкретной записи в long-term DB.
        Использование: /repair_booking <booking_id> <new_status>
        Допустимые статусы: confirmed, cancelled, rescheduled, completed, no_show"""
        if message.chat.id not in config.admin_ids:
            return

        args = message.text.split()
        if len(args) < 3:
            bot.send_message(message.chat.id, "Использование: /repair_booking <booking_id> <status>")
            return

        booking_id = args[1]
        new_status = args[2].lower()

        allowed = {"confirmed", "cancelled", "rescheduled", "completed", "no_show"}
        if new_status not in allowed:
            bot.send_message(message.chat.id, f"Недопустимый статус. Разрешено: {', '.join(sorted(allowed))}")
            return

        if db is None:
            bot.send_message(message.chat.id, "Long-term DB недоступна.")
            return

        try:
            success = db.bookings.update_booking(booking_id, status=new_status)
            if success:
                bot.send_message(message.chat.id, f"✅ Запись {booking_id} переведена в статус '{new_status}'")
                log.info("admin: repair_booking %s → %s by admin %s", booking_id, new_status, message.chat.id)
            else:
                bot.send_message(message.chat.id, f"Запись {booking_id} не найдена или не была изменена.")
        except Exception as e:
            bot.send_message(message.chat.id, f"Ошибка: {e}")
            log.exception("repair_booking failed")

    @bot.message_handler(commands=["repair", "ремонт"])
    def cmd_repair(message: types.Message) -> None:
        """Универсальная мощная ремонтная команда.
        Использование:
          /repair <user_id>                  → визиты
          /repair <user_id> bonuses          → визиты + бонусы
          /repair <user_id> bookings         → почистить «Мои записи» (убрать призраки/заброшенные переносы по статусам в БД)
          /repair <user_id> full             → полный ремонт (визиты + бонусы + bookings reconcile)
          /repair <user_id> audit            → показать проблемы без изменений"""
        if message.chat.id not in config.admin_ids:
            return

        args = message.text.split()
        if len(args) < 2:
            bot.send_message(message.chat.id, "Использование: /repair <user_id> [bonuses|bookings|full|audit]")
            return

        try:
            target_user_id = int(args[1])
        except ValueError:
            bot.send_message(message.chat.id, "user_id должен быть числом.")
            return

        mode = args[2].lower() if len(args) > 2 else "visits"
        is_audit = mode == "audit"
        full = mode == "full"
        repair_bonuses = mode in ("bonuses", "bonus", "full")
        reconcile_bookings = "bookings" in mode or "reconcile" in mode or full

        if db is None:
            bot.send_message(message.chat.id, "Long-term DB недоступна.")
            return

        try:
            result = db.full_repair_user(target_user_id, repair_bonuses=repair_bonuses)

            cs = state_store.get(target_user_id) or {}

            if not is_audit:
                cs["total_visits"] = result["visits"]["new"]
                if repair_bonuses and "bonuses" in result:
                    cs.setdefault("card", {})["design_bonuses"] = result["bonuses"]["corrected_to"]
                state_store.set(target_user_id, cs)

            lines = [f"User {target_user_id}:"]
            lines.append(f"  Visits: {result['visits']['old']} → {result['visits']['new']}")

            if repair_bonuses and "bonuses" in result:
                b = result["bonuses"]
                lines.append(f"  Bonuses: brought {b['brought']}, used {b['used']} → {b['corrected_to']}")

            # --- New: reconcile + clean state["bookings"] against DB (fixes ghosts from abandoned reschedules etc.)
            bookings_cleaned = 0
            if reconcile_bookings and not is_audit:
                try:
                    bookings = list(cs.get("bookings") or [])
                    if bookings:
                        db_rows = db.bookings.get_bookings_for_user(target_user_id, limit=100)
                        db_statuses = {row.get("id"): (row.get("status") or "confirmed") for row in db_rows}

                        original_count = len(bookings)
                        cleaned_bookings = []
                        removed_ids = []
                        for b in bookings:
                            bid = b.get("id")
                            st = db_statuses.get(bid) if bid else None
                            if st in ("cancelled", "rescheduled", "no_show"):
                                removed_ids.append(bid)
                                # Best-effort: remove any dangling reminder/review jobs for this ghost
                                for jid in (b.get("reminder_job_ids") or []):
                                    try:
                                        if scheduler:
                                            scheduler.remove_job(jid)
                                    except Exception:
                                        pass
                                if b.get("review_job_id"):
                                    try:
                                        if scheduler:
                                            scheduler.remove_job(b["review_job_id"])
                                    except Exception:
                                        pass
                            else:
                                cleaned_bookings.append(b)

                        if removed_ids:
                            cs["bookings"] = cleaned_bookings
                            state_store.set(target_user_id, cs)
                            bookings_cleaned = len(removed_ids)
                            lines.append(f"  Bookings cleaned (ghosts removed): {bookings_cleaned} (ids: {', '.join(removed_ids[:5])}{'…' if len(removed_ids)>5 else ''})")
                            log.info("admin repair: cleaned %d ghost bookings for user=%s (reschedule/cancel desync)", bookings_cleaned, target_user_id)
                except Exception as ex:
                    lines.append(f"  Bookings reconcile warning: {ex}")

            if full or reconcile_bookings:
                lines.append("  (Full repair executed)" if full else "  (Bookings reconciled)")

            if is_audit:
                lines.append("\n(Audit mode — no changes written)")
            else:
                lines.append("\n✅ Repairs applied.")

            if result.get("errors"):
                lines.append("\nErrors/Warnings:")
                for err in result["errors"]:
                    lines.append(f"  - {err}")

            bot.send_message(message.chat.id, "\n".join(lines))
            log.info("admin: repair command user=%s mode=%s", target_user_id, mode)
        except Exception as e:
            bot.send_message(message.chat.id, f"Ошибка: {e}")
            log.exception("repair command failed")

    @bot.message_handler(commands=["db_backfill_from_redis", "бэкфилл_из_redis"])
    def cmd_db_backfill_from_redis(message: types.Message) -> None:
        """
        Одноразовая команда: восстановить long-term DB из Redis state.

        Зачем: до fix(db) от 2026-05-28 порядок DB-операций в booking flow
        был обратным (create_booking ДО upsert_client), из-за чего ВСЕ записи
        новых клиентов молча терялись в DB (FK violation проглатывался).
        Скрипт идёт по state_store.all_users() и:
          - upsert_client(uid, name, chat_id, notes из state.card)
          - create_booking(b) для каждой b в state.bookings, если её id
            нет в DB

        Статус определяется по datetime:
          - в будущем → confirmed
          - в прошлом без rating → completed
          - в прошлом с rating → completed

        Использование:
          /db_backfill_from_redis            → реальный запуск, лимит по умолч.
          /db_backfill_from_redis audit      → превью без записи
          /db_backfill_from_redis 50         → ограничить первыми 50 юзерами
          /db_backfill_from_redis audit 20   → превью первых 20
        """
        if message.chat.id not in config.admin_ids:
            return
        if db is None:
            bot.send_message(message.chat.id, "❌ Long-term DB недоступна — нечего бэкфиллить.")
            return

        args = (message.text or "").split()[1:]
        audit = False
        limit = 10_000  # по факту больше клиентов не бывает на free тарифе
        for a in args:
            al = a.lower().strip()
            if al in ("audit", "a", "--audit", "dry", "dry-run"):
                audit = True
                continue
            try:
                limit = max(1, min(int(a), 10_000))
            except ValueError:
                pass

        bot.send_message(message.chat.id,
            f"⏳ {'Аудит' if audit else 'Запуск'} backfill (макс {limit} юзеров)...")

        users = list(state_store.all_users())[:limit]
        users_processed = 0
        clients_upserted = 0
        bookings_added = 0
        bookings_already = 0
        errors = 0
        per_user_log: list[str] = []

        now = ctx.now_moscow()
        from datetime import datetime as _dt_dt

        for uid in users:
            try:
                cs = state_store.get(uid) or {}
                state_bookings = cs.get("bookings") or []
                if not state_bookings:
                    continue  # клиент без записей в Redis — не интересно

                # Уже существующие записи в DB по этому юзеру
                db_rows = db.bookings.get_bookings_for_user(uid, limit=500)
                db_ids = {row.get("id") for row in db_rows if row.get("id")}

                # Что есть в Redis, но нет в DB
                missing = [b for b in state_bookings
                           if b.get("id") and b.get("id") not in db_ids]
                if not missing:
                    bookings_already += len(state_bookings)
                    continue

                if not audit:
                    # 1) upsert_client с данными из карточки
                    card = cs.get("card") or {}
                    db.clients.upsert_client(
                        uid,
                        chat_id=uid,                  # в private chat user_id == chat_id
                        name=card.get("name") or None,
                        notes=card.get("notes") or None,
                        birthday=card.get("birthday") or None,
                        tags=card.get("tags") or None,
                    )
                    clients_upserted += 1

                # 2) Для каждой записи определяем статус по datetime и rating
                for b in missing:
                    iso = b.get("datetime_iso")
                    status = "confirmed"
                    if iso:
                        try:
                            dt = ctx.parse_iso_aware(iso)
                            if dt < now:
                                # в прошлом — completed (вне зависимости от rating;
                                # no_show / cancelled должны были быть в state как
                                # удалённые из bookings, но если осталось — completed
                                # для статистики корректнее чем confirmed)
                                status = "completed"
                        except Exception:
                            pass

                    booking_payload = {
                        "id":               b.get("id"),
                        "user_id":          uid,
                        "master":           b.get("master") or "—",
                        "services":         b.get("services") or [],
                        "services_raw":     b.get("services_raw") or "",
                        "datetime":         b.get("datetime") or "",
                        "datetime_iso":     iso,
                        "duration_min":     b.get("duration_min") or 60,
                        "phone":            b.get("phone"),
                        "chat_id":          b.get("chat_id") or uid,
                        "status":           status,
                        "calendar_event_id": b.get("calendar_event_id"),
                        "promo_code":       b.get("promo_code"),
                        "notes":            b.get("notes") or [],
                        "reminder_job_ids": b.get("reminder_job_ids") or [],
                    }

                    if not audit:
                        try:
                            db.bookings.create_booking(booking_payload)
                            # Если есть rating — пишем тоже
                            if b.get("rating"):
                                db.bookings.update_rating_and_review(
                                    booking_payload["id"],
                                    rating=int(b["rating"]),
                                    review_text=b.get("review_text"),
                                )
                        except Exception as ex:
                            log.error("backfill: create_booking failed for %s/%s: %s",
                                      uid, booking_payload["id"], ex)
                            errors += 1
                            continue

                    bookings_added += 1

                users_processed += 1
                if len(per_user_log) < 30:
                    name = (cs.get("card") or {}).get("name") or "—"
                    per_user_log.append(f"  • {uid} ({name}): +{len(missing)} bookings")
            except Exception as ex:
                log.warning("backfill: user %s failed: %s", uid, ex)
                errors += 1

        report = [
            f"{'📋 АУДИТ' if audit else '✅ BACKFILL'} <b>завершён</b>\n",
            f"Юзеров просмотрено: <b>{len(users)}</b>",
            f"Юзеров с пропущенными записями: <b>{users_processed}</b>",
        ]
        if not audit:
            report.append(f"upsert_client сделано: <b>{clients_upserted}</b>")
            report.append(f"Записей добавлено в DB: <b>{bookings_added}</b>")
        else:
            report.append(f"<i>Будет добавлено записей: {bookings_added}</i>")
        report.append(f"Записей уже на месте: {bookings_already}")
        if errors:
            report.append(f"❌ Ошибок: {errors} (см. логи)")
        if per_user_log:
            report.append("\n<b>Детали:</b>")
            report.extend(per_user_log)
            if users_processed > len(per_user_log):
                report.append(f"  …и ещё {users_processed - len(per_user_log)}")

        bot.send_message(message.chat.id, "\n".join(report), parse_mode="HTML")
        log.info("admin: db_backfill_from_redis audit=%s users=%d/%d bookings=%d errors=%d",
                 audit, users_processed, len(users), bookings_added, errors)

    def _do_sync_calendar(days: int = 14, verbose: bool = False) -> dict:
        """
        Чистая функция синка календаря — без message/chat зависимостей.
        Используется и /sync_calendar (вручную), и cron-job каждые 30 мин.

        Параметры:
          days: горизонт сканирования (1–90 дней вперёд)
          verbose: если True — обходит ВСЕ события (включая без Telegram ID)
                   и пишет в отчёт причину пропуска для каждого. Используется
                   для разовой диагностики "почему /sync_calendar ничего не нашёл".

        Возвращает:
          {
            "created": int, "skipped": int, "errors": int,
            "report": str (HTML),
            "per_master": [{"name": str, "created": int, "errors": int}, ...],
          }
        """
        import re as _re
        import uuid as _uuid
        import datetime as _dt

        svc = ctx.calendar_service()
        if not svc:
            return {
                "created": 0, "skipped": 0, "errors": 0,
                "report": "❌ Google Calendar не настроен.",
                "per_master": [],
            }

        now = ctx.now_moscow()
        end = now + _dt.timedelta(days=days)
        schedule_reminder = getattr(ctx, "schedule_reminder", None)

        created_total = 0
        skipped_total = 0
        errors_total  = 0
        per_master: list[dict] = []
        verbose_lines: list[str] = []  # подробный лог пропусков для verbose-режима
        VERBOSE_MAX = 40  # не больше 40 строк (Telegram-сообщение всё равно ограничено)
        report_lines  = [
            f"🔄 <b>Синхронизация календаря</b> (горизонт: {days} дн.)"
            + (" <i>[verbose]</i>" if verbose else "")
        ]

        def _vlog(line: str) -> None:
            """Добавить строку в verbose-лог (с ограничением)."""
            if not verbose:
                return
            if len(verbose_lines) < VERBOSE_MAX:
                verbose_lines.append(line)
            elif len(verbose_lines) == VERBOSE_MAX:
                verbose_lines.append("  …обрезано (слишком много событий)")

        def _summary(event) -> str:
            """Короткое описание события для verbose-лога."""
            s = (event.get("summary") or "").strip() or "(без названия)"
            t = (event.get("start") or {}).get("dateTime", "") or \
                (event.get("start") or {}).get("date", "")
            try:
                if "T" in t:
                    dt = _dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
                    t = dt.strftime("%d.%m %H:%M")
            except Exception:
                pass
            return f"«{s[:40]}» @ {t}"

        for master in ctx.kb.masters.values():
            cal_id = (master.calendar_id or "").strip()
            if not cal_id or "заполнить" in cal_id.lower():
                if verbose:
                    _vlog(f"⚠️ <b>{master.name}</b>: calendar_id не настроен")
                continue

            try:
                events = ctx.fetch_events_for_range(master, now, end)
            except Exception as e:
                report_lines.append(f"\n❌ {master.name}: ошибка чтения: {e}")
                continue

            if verbose:
                _vlog(f"\n<b>{master.name}</b> — событий в календаре: {len(events)}")

            created_m = skipped_m = errors_m = 0
            for event in events:
                desc = event.get("description", "") or ""
                # Только события с Telegram ID
                m_tg = _re.search(r"Telegram ID клиента:\s*(\d+)", desc)
                if not m_tg:
                    _vlog(f"  ⏭ {_summary(event)} — нет «Telegram ID клиента:» в описании")
                    continue

                cal_event_id = event.get("id")
                if not cal_event_id:
                    _vlog(f"  ⏭ {_summary(event)} — нет event.id (странно)")
                    continue

                client_tg_id = int(m_tg.group(1))

                # Проверяем DB — уже есть такая запись?
                if db is not None:
                    try:
                        existing = db.bookings.get_booking_by_calendar_event_id(cal_event_id)
                        if existing:
                            skipped_m += 1
                            _vlog(f"  ✓ {_summary(event)} — уже в DB (booking={existing.get('id')})")
                            continue
                    except Exception:
                        pass

                # Проверяем Redis-состояние клиента
                client_state = state_store.get(client_tg_id) or {}
                already_in_state = any(
                    b.get("calendar_event_id") == cal_event_id
                    for b in (client_state.get("bookings") or [])
                )
                if already_in_state:
                    skipped_m += 1
                    _vlog(f"  ✓ {_summary(event)} — уже в Redis-state клиента {client_tg_id}")
                    continue

                # Событие уже в прошлом — не регистрируем (нечего напоминать)
                start_iso = (event.get("start") or {}).get("dateTime", "")
                try:
                    event_dt = _dt.datetime.fromisoformat(
                        start_iso.replace("Z", "+00:00"))
                    if event_dt < now:
                        skipped_m += 1
                        _vlog(f"  ⏮ {_summary(event)} — в прошлом, пропуск")
                        continue
                    when_str = event_dt.strftime("%d.%m.%Y %H:%M")
                except Exception:
                    when_str = start_iso
                    if not start_iso:
                        errors_m += 1
                        _vlog(f"  ❗ {_summary(event)} — нет start.dateTime")
                        continue

                # Парсим поля из описания
                m_phone = _re.search(r"Телефон клиента:\s*(\+?[\d\s\-\(\)]+)", desc)
                phone = (m_phone.group(1).strip() if m_phone else "").rstrip()
                m_name = _re.search(r"Имя клиента:\s*(.+)", desc)
                client_name = m_name.group(1).strip() if m_name else ""
                summary = (event.get("summary") or "").strip() or "Услуга"

                # Формируем booking-запись
                booking_id = _uuid.uuid4().hex[:12]
                booking: dict = {
                    "id":               booking_id,
                    "master":           master.name,
                    "services":         [summary],
                    "services_raw":     summary,
                    "datetime":         when_str,
                    "datetime_iso":     start_iso,
                    "duration_min":     60,
                    "phone":            phone,
                    "client_name":      client_name,
                    "chat_id":          client_tg_id,
                    "user_id":          client_tg_id,
                    "calendar_event_id": cal_event_id,
                    "reminder_job_ids": [],
                    "promo_code":       None,
                    "notes":            ["ручная запись"],
                }

                # Сохраняем в DB.
                # ВАЖНО: upsert_client перед create_booking — у bookings.user_id
                # есть FK на clients(user_id). Клиент мог никогда не писать боту
                # (мастер занёс его номер в Calendar вручную), тогда в clients
                # его нет, и create_booking упал бы с FK constraint failed.
                if db is not None:
                    try:
                        db.clients.upsert_client(
                            client_tg_id,
                            chat_id=client_tg_id,  # в private chat user_id == chat_id
                            name=client_name or None,
                        )
                    except Exception as e:
                        log.warning("sync_calendar: upsert_client failed for tg=%s: %s",
                                    client_tg_id, e)
                        # Не прерываем — create_booking ниже попробует и выдаст
                        # понятную ошибку в errors_m

                    try:
                        db.bookings.create_booking(booking)
                    except Exception as e:
                        log.error("sync_calendar: DB create failed for %s: %s", booking_id, e)
                        errors_m += 1
                        _vlog(f"  ❌ {_summary(event)} — DB create failed: {e}")
                        continue

                # Добавляем в Redis-состояние клиента
                try:
                    bookings_list = list(client_state.get("bookings") or [])
                    bookings_list.append(booking)
                    if len(bookings_list) > 50:
                        bookings_list = bookings_list[-50:]
                    client_state["bookings"] = bookings_list
                    # Инкрементим счётчик визитов (если клиент уже в системе)
                    if client_state.get("total_visits") is not None:
                        client_state["total_visits"] = int(client_state["total_visits"]) + 1
                    state_store.set(client_tg_id, client_state)
                except Exception as e:
                    log.error("sync_calendar: state update failed for tg=%s: %s",
                              client_tg_id, e)
                    errors_m += 1
                    _vlog(f"  ❌ {_summary(event)} — state update failed: {e}")
                    continue

                # Планируем напоминания (24h и 1h до визита)
                if schedule_reminder:
                    try:
                        schedule_reminder(booking)
                    except Exception as e:
                        log.warning("sync_calendar: reminder failed for %s: %s", booking_id, e)

                log.info("sync_calendar: registered manual booking %s, "
                         "client_tg=%s, master=%s, when=%s",
                         booking_id, client_tg_id, master.name, when_str)
                _vlog(f"  ✅ {_summary(event)} — создана запись {booking_id}")
                created_m += 1

            created_total += created_m
            skipped_total += skipped_m
            errors_total  += errors_m
            per_master.append({"name": master.name, "created": created_m, "errors": errors_m})
            if created_m or errors_m:
                report_lines.append(
                    f"\n<b>{master.name}</b>: создано {created_m}"
                    + (f", ошибок {errors_m}" if errors_m else "")
                )

        report_lines.append(
            f"\n<b>Итого:</b> создано {created_total}  |  "
            f"уже существовало {skipped_total}"
            + (f"  |  ошибок {errors_total}" if errors_total else "")
        )
        if created_total == 0 and skipped_total == 0:
            report_lines.append("\nВ календарях не найдено событий с «Telegram ID клиента:» на указанный период.")
        elif created_total == 0:
            report_lines.append("\nНовых записей не обнаружено — всё уже синхронизировано.")

        if verbose and verbose_lines:
            report_lines.append("\n<b>Подробности по событиям:</b>")
            report_lines.extend(verbose_lines)

        return {
            "created": created_total,
            "skipped": skipped_total,
            "errors":  errors_total,
            "report":  "\n".join(report_lines),
            "per_master": per_master,
        }

    @bot.message_handler(commands=["sync_calendar"])
    def cmd_sync_calendar(message: types.Message) -> None:
        """
        Синхронизирует ручные записи из Google Calendar в бота (ручной запуск).

        Находит события в календарях мастеров, которые содержат
        «Telegram ID клиента: <id>» в описании, но ещё не зарегистрированы
        в боте — создаёт запись в DB и планирует напоминания клиенту.

        Шаблон описания события в Google Calendar:
          Telegram ID клиента: 123456789
          Телефон клиента: +79991234567
          Имя клиента: Анна

        Использование:
          /sync_calendar              — за 14 дней вперёд, без подробностей
          /sync_calendar 30           — за 30 дней
          /sync_calendar verbose      — за 14 дней, с подробным отчётом по
                                         каждому пропущенному событию
          /sync_calendar 7 verbose    — комбинируем
        """
        if message.chat.id not in config.admin_ids:
            # Мастера тоже могут запускать — проверяем через master_by_telegram_id
            if not ctx.master_by_telegram_id(message.chat.id):
                return

        # Парсим аргументы: число (дни) и/или слово 'verbose'/'v'
        args = (message.text or "").split()[1:]
        days = 14
        verbose = False
        for a in args:
            al = a.lower().strip()
            if al in ("verbose", "v", "-v", "--verbose"):
                verbose = True
                continue
            try:
                days = max(1, min(int(a), 90))
            except ValueError:
                pass

        try:
            result = _do_sync_calendar(days=days, verbose=verbose)
        except Exception as e:
            log.exception("cmd_sync_calendar failed: %s", e)
            bot.send_message(message.chat.id, f"❌ Ошибка синка: {e}")
            return

        # В verbose-режиме отчёт длинный — режем по 4000 символов через safe_send_long
        text = result["report"]
        if verbose and hasattr(ctx, "safe_send_long"):
            ctx.safe_send_long(message.chat.id, text, parse_mode="HTML")
        else:
            bot.send_message(message.chat.id, text, parse_mode="HTML")

    def sync_calendar_cron() -> None:
        """
        Cron-таргет: автоматический /sync_calendar каждые 30 минут.

        Логика:
          - Тихо логируем "нашли N новых записей, всё OK" или "ничего нового".
          - Уведомление админам шлём ТОЛЬКО если created > 0 или errors > 0.
          - Чтобы не спамить чат, при created==0 и errors==0 — молчим.
        """
        try:
            result = _do_sync_calendar(days=14)
        except Exception as e:
            log.exception("sync_calendar_cron crashed: %s", e)
            # Алерт админам про падение cron'а
            for admin_id in config.admin_ids:
                try:
                    bot.send_message(admin_id, f"⚠️ Cron sync_calendar упал: {e}")
                except Exception:
                    pass
            return

        log.info("sync_calendar_cron: created=%d skipped=%d errors=%d",
                 result["created"], result["skipped"], result["errors"])

        # Молчим если ничего интересного не нашли
        if result["created"] == 0 and result["errors"] == 0:
            return

        # Уведомляем админов
        for admin_id in config.admin_ids:
            try:
                bot.send_message(
                    admin_id,
                    f"🔁 <i>Авто-синк календаря:</i>\n{result['report']}",
                    parse_mode="HTML",
                )
            except Exception as e:
                log.warning("sync_calendar_cron: notify admin %s failed: %s", admin_id, e)

    @bot.message_handler(commands=["calendar_check", "проверить_календарь"])
    def cmd_calendar_check(message: types.Message) -> None:
        """Диагностика доступа к Google Calendar.
        Админ видит все календари; мастер — только свой собственный.
        Проверяет: сервис создан, чтение событий, создание/удаление тестового события.
        """
        is_admin = message.chat.id in config.admin_ids
        own_master = ctx.master_by_telegram_id(message.chat.id)
        if not is_admin and not own_master:
            return  # ни админ, ни мастер — молча игнорируем

        import datetime as _dt
        from services.calendar import create_event as _cal_create, delete_event as _cal_delete

        svc = ctx.calendar_service()
        if not svc:
            bot.send_message(
                message.chat.id,
                "❌ Google Calendar сервис не создан.\n"
                "Проверь переменную окружения <code>GOOGLE_SERVICE_ACCOUNT_JSON</code>.",
                parse_mode="HTML",
            )
            return

        # Админ проверяет все календари, мастер — только свой
        if is_admin:
            masters_to_check = list(ctx.kb.masters.values())
        else:
            masters_to_check = [own_master]

        lines = ["📅 <b>Диагностика Google Calendar</b>\n"]
        now = ctx.now_moscow()
        test_start = (now + _dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        test_end = test_start + _dt.timedelta(minutes=30)

        for master in masters_to_check:
            cal_id = (master.calendar_id or "").strip()
            if not cal_id or "заполнить" in cal_id.lower():
                lines.append(f"⚪ <b>{master.name}</b> — calendar_id не задан")
                continue

            read_ok = False
            write_ok = False
            error_msg = ""

            # Тест чтения
            try:
                svc.events().list(
                    calendarId=cal_id,
                    timeMin=now.isoformat(),
                    timeMax=(now + _dt.timedelta(days=1)).isoformat(),
                    maxResults=1,
                    singleEvents=True,
                ).execute()
                read_ok = True
            except Exception as e:
                error_msg = f"чтение: {e}"

            # Тест записи: создаём и сразу удаляем тестовое событие
            if read_ok:
                test_event = {
                    "summary": "🤖 [Тест бота — можно удалить]",
                    "description": "Автоматическая проверка доступа. Можно удалить.",
                    "start": {"dateTime": test_start.isoformat(), "timeZone": "Europe/Moscow"},
                    "end": {"dateTime": test_end.isoformat(), "timeZone": "Europe/Moscow"},
                }
                try:
                    created = _cal_create(svc, cal_id, test_event)
                    if created:
                        write_ok = True
                        _cal_delete(svc, cal_id, created["id"])
                    else:
                        error_msg = "create_event вернул None (нет прав на запись)"
                except Exception as e:
                    error_msg = f"запись: {e}"

            if read_ok and write_ok:
                lines.append(f"✅ <b>{master.name}</b> (<code>{cal_id}</code>) — чтение ОК, запись ОК")
            elif read_ok:
                lines.append(
                    f"⚠️ <b>{master.name}</b> (<code>{cal_id}</code>) — "
                    f"чтение ОК, <b>запись ОШИБКА</b>\n"
                    f"   └ {error_msg}"
                )
            else:
                lines.append(
                    f"❌ <b>{master.name}</b> (<code>{cal_id}</code>) — "
                    f"<b>нет доступа</b>\n"
                    f"   └ {error_msg}"
                )

        lines.append(
            "\n<i>Если запись недоступна — открой Google Календарь → Настройки → "
            "«Поделиться с конкретными людьми» → добавь email сервисного аккаунта "
            "с правом «Изменять события».</i>"
        )
        bot.send_message(message.chat.id, "\n".join(lines), parse_mode="HTML")

    return {
        "backup_state_to_admin": _backup_state_to_admin,
        "cmd_broadcast_start": cmd_broadcast_start,
        "cmd_clear_reminders": cmd_clear_reminders,
        "sync_calendar_cron": sync_calendar_cron,
    }
