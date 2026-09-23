"""Константы протокола службы graph_memory — намеренно без единого импорта
внутри пакета проекта (только строковые литералы), как memory/protocol.py,
net/protocol.py и tasks/protocol.py.

Живут отдельно от graph_memory/service.py, чтобы bot/tools.py и
bot/ai_flow.py могли их импортировать, не утягивая саму службу (и тяжёлые
зависимости graphiti-core/neo4j) в бота.
"""

from __future__ import annotations

SERVICE_NAME = "graph_memory"

# ОСЛАБЛЕННЫЙ инвариант в отличие от memory.NODE_ID: там нода обязана быть
# доступна ВСЕГДА (см. memory/protocol.py), здесь — нет. graph_memory пинуется
# на mycraft, потому что там живёт Neo4j и Ollama (LLM-экстракция/эмбеддинги),
# а mycraft штатно уходит в suspend при простое GPU. Недоступность этой
# службы во время сна mycraft — предусмотренная деградация («деградация при
# недоступности пира», ARCHITECTURE.md §11), не авария: recall_graph_facts
# (bot/ai_flow.py) в этом случае молча отдаёт [], а обычная память (`memory`,
# NODE_ID=alfred) продолжает работать как всегда. Не путать эти два NODE_ID.
NODE_ID = "mycraft"

ACTION_ADD_EPISODE = "add_episode"
ACTION_SEARCH = "search"
ACTION_QUEUE_STATUS = "queue_status"

# Источник эпизода — раздельные константы, чтобы разные piggyback-пути
# (bot/tools.py, bot/ai_flow.py) не путали свои эпизоды под одним source и
# чтобы будущий фильтр по episode_source (IMPLEMENTATION_PLAN.md, Этап 42.2,
# "при необходимости") мог их различить.
EPISODE_SOURCE_MEMORY_FACT = "memory_fact"
# Этап 42.2: piggyback поверх завершённого хода /ai (bot/ai_flow.py::
# piggyback_dialogue_episode) — реплика собеседника + ответ Альфреда.
EPISODE_SOURCE_DIALOGUE_TURN = "dialogue_turn"
# Этап 42.2: повторный просмотр фото (bot/tools.py::tool_look_at_photo) —
# по образцу EPISODE_SOURCE_MEMORY_FACT.
EPISODE_SOURCE_LOOK_AT_PHOTO = "look_at_photo"
# Этап 42.2: ссылка (URL), упомянутая в ходе диалога — тот же текст хода,
# что и EPISODE_SOURCE_DIALOGUE_TURN, но отдельным эпизодом/source, чтобы
# граф впоследствии мог отвечать "что мы обсуждали по этой ссылке".
EPISODE_SOURCE_LINK = "link"
