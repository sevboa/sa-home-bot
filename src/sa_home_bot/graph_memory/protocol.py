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

# Источник эпизода — на этой итерации единственный: piggyback поверх
# memory.ACTION_REMEMBER (см. bot/tools.py::tool_memory). Раздельные
# источники — чтобы следующий этап (web_search/история диалогов) не путал
# свои эпизоды под тем же source.
EPISODE_SOURCE_MEMORY_FACT = "memory_fact"
