# Служба llm (platform-llm)

Служба `llm` живёт на GPU-ноде (сейчас mycraft; раньше winpc) и держит всё, что тяжело для alfred: диалог с Ollama, зрение, распознавание и синтез речи, генерацию картинок. Код — `src/sa_home_bot/llm/`.

- [service-actions.md](service-actions.md) — действия службы, чанкование, буфер частичной генерации, TTL сессий, сон и прогрев, события.
- [ollama-backend.md](ollama-backend.md) — клиент Ollama: WSL/native, ретраи, тело HTTP-ошибки, keep_alive и num_ctx, профили моделей.
- [voice-stt-tts.md](voice-stt-tts.md) — faster-whisper и Coqui XTTS v2.
- [imagegen.md](imagegen.md) — SD1.5+LCM, режимы, LoRA, предметы (вырезка/вставка), сверка зрением.
- [persona-mechanics.md](persona-mechanics.md) — механика промптов, роутер глубины рассуждения, Логопед. Тексты персонажа здесь не пересказываются и в коде не менялись.
- [shared-mechanisms.md](shared-mechanisms.md) — какие общие механизмы внутри `llm/` заменили дубли и где их тесты.
