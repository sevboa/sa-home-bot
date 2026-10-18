# Extras пакета (pyproject.toml)

Установка любого extra: `pipx install "sa-home-bot[<extra>] @ git+…@vX.Y.Z"`. Пины и состав зависимостей в pyproject — часть решений ниже, менять только осознанно.

## windows

`pythonnet>=3.0` (только `sys_platform == 'win32'`) — загрузка `LibreHardwareMonitorLib.dll` (`sensors/lhm.py`).

## stt — распознавание голосовых /ai

Нода с распознаванием (сейчас mycraft — сильный CPU, Xeon E5-2678 v3): faster-whisper на CPU, не Ollama/Gemma (`llm/stt.py`).

- `av<19`: av 19 убрал аргумент `metadata_errors` у `av.open()`, а faster-whisper 1.2.1 его передаёт → `TypeError` на каждом голосовом. Приехал молча при переустановке v0.120.1; 18.1.0 проверен на стенде.

## tts — синтез голосовых ответов /ai

Coqui XTTS v2, voice cloning по референсному клипу (`llm/tts.py`), нода та же, что у stt. Помимо pip-пакета нужны ТРИ инфраструктурных шага, extras-группа их не покрывает:

1. системный ffmpeg (`apt install ffmpeg`) — конвертация WAV → OGG/Opus для Telegram sendVoice, вызывается внешним процессом, не биндингом;
2. `COQUI_TOS_AGREED=1` в окружении юнита службы llm — иначе первая загрузка весов XTTS v2 (лицензия CPML, некоммерческая) зависнет на `input()` в неинтерактивном процессе;
3. референсный голос — WAV 6–10 с чистой речи по пути `settings.llm.tts_reference_voice_path`, кладётся вручную, в репозиторий не входит.

Пины (явные, не расчёт на резолвер):

- `transformers>=4.57,<5`, `torch>=2.2,<2.9`, `torchaudio>=2.2,<2.9`: coqui-tts 0.27.5 объявляет только `transformers>=4.57` и `torch>=2.2` без верхних границ (находка при деплое 2026-08-11). pip тянет transformers 5.x (убрал `isin_mps_friendly` из `pytorch_utils`, ломает импорт `TTS.api`) и torch>=2.9 (требует отдельно torchcodec для audio I/O, а тот на Debian trixie не совместим с системным ffmpeg 7.1/libavutil59 — нет собранного бэкенда). `torch<2.9` обходит torchcodec целиком: coqui-tts грузит референсный WAV через классический torchaudio-бэкенд.
- `numpy<2.5`: numba 0.66 (XTTS → librosa → numba) требует `numpy<2.5`, а numpy 2.5.3 молча приехал в venv при переустановке и сломал импорт `TTS.api` («Numba needs NumPy 2.4 or less», находка 2026-09-24).

## graph_memory — графовая память Альфреда (Этап 41)

Служба graph_memory только на mycraft, там же Neo4j и Ollama. `graphiti-core` тянет официальный `neo4j-driver` (протокол bolt бинарный, urllib его не осилит) — осознанное исключение из принципа «минимум зависимостей, urllib+to_thread», по аналогии с qbittorrent-api/pythonnet/coqui-tts (тяжёлая/специфичная зависимость только на нужной ноде). Версии по факту пилота на mycraft 2026-09-22 (`~/.venvs/graphiti`): graphiti-core 0.30.2, neo4j 6.3.1.

- `httpx>=0.28,<1` — явный пин: graphiti-core 0.30.2 импортирует классический httpx (`graphiti_core/llm_client/client.py`), но в метаданных не объявляет (пакует новый `openai`, который тянет httpx2 — форк-переименование, а не совместимую замену). Без пина импорт падает с `ModuleNotFoundError` (находка пилота 2026-09-22).

## mcp — MCP-сервер умений роя (Этап 42.4)

Вторая попытка (первая жила на alfred как HTTP+bearer-токены — неправильная форма, см. IMPLEMENTATION_PLAN.md; `agent/mcp_tools.py`): базовая инфраструктура под агент-фреймворк (Phase 2 плана vast-skipping-crane.md), ещё не сам агент. Ставится на ноду, где будет жить агент — сейчас mycraft (там же Ollama), НЕ alfred: `bot.tools` переиспользуется как чистый Python через собственный node_link процесса, живые Telegram-объекты (notifier/store) не нужны. Официальный SDK, не самописный JSON-RPC — второе осознанное исключение из принципа «минимум зависимостей» после graphiti-core (решение пользователя 2026-09-23). Транспорт stdio (`mcp.server.stdio`), не HTTP: агент и процесс на одной машине, доверие = общий процесс/оператор, сети и auth-слоя не нужно.

## imagegen — генерация картинок /ai (Этап 48)

Сейчас mycraft, тот же CPU, что у stt/tts. Stable Diffusion 1.5 (`Lykon/dreamshaper-8`) + LCM-LoRA (`latent-consistency/lcm-lora-sdv1-5`), 4 шага на CPU в fp32 (`llm/imagegen.py`). Не V100 и не Ollama — обоснование в IMPLEMENTATION_PLAN.md, Этап 48. `peft` — загрузчик LoRA в diffusers (`load_lora_weights` без него падает), `accelerate` — `low_cpu_mem_usage` при загрузке весов.

Живёт в том же pipx-venv, что tts, поэтому torch/transformers/numpy повторяют её пины один в один: иначе резолвер при установке `[imagegen]` вместе с `[tts]` мог бы поднять torch>=2.9, transformers 5.x или numpy 2.5 (см. tts). Версии diffusers/peft/accelerate — по факту живой установки на mycraft 2026-09-28 (diffusers 0.35.2, peft 0.17.1, accelerate 1.15.0 при torch 2.8.0, transformers 4.57.6, numpy 2.4.6); верхние границы — чтобы следующий минор с новыми требованиями к torch/transformers не приехал молча при переустановке. Веса (~2.2 ГБ) качаются с Hugging Face при первой загрузке пайплайна в `settings.llm.imagegen_model_dir`.

- `rembg>=2.0.85,<2.1`, `onnxruntime>=1.23,<2` — Этап 49.3: вырезка портрета предмета (isnet-general-use) для вставки в сцены.
- `compel>=2.1.1,<2.2` — веса черт предмета в промпте портрета («(rust)1.3») и длина без обрезки по 77 токенам (Этап 49.3, стенд 49.3.0). 2.1: с 2.2 тянет notebook, 2.4 — transformers 5.
