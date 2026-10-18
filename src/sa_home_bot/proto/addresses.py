from __future__ import annotations

from sa_home_bot.graph_memory import protocol as graph_memory_protocol
from sa_home_bot.memory import protocol as memory_protocol
from sa_home_bot.net import protocol as net_protocol
from sa_home_bot.proto.messages import Address
from sa_home_bot.tasks import protocol as tasks_protocol

NODE_SERVICE = "node"
MONITOR_SERVICE = "monitor"
LLM_NODE = "mycraft"
LLM_SERVICE = "llm"
ROLE_DIRECTOR = "director"
ROLE_SUMMARIZER = "summarizer"

LLM_ADDRESS = Address(node=LLM_NODE, service=LLM_SERVICE)
MEMORY_ADDRESS = Address(node=memory_protocol.NODE_ID, service=memory_protocol.SERVICE_NAME)
GRAPH_MEMORY_ADDRESS = Address(
    node=graph_memory_protocol.NODE_ID, service=graph_memory_protocol.SERVICE_NAME
)
NET_ADDRESS = Address(node=net_protocol.NODE_ID, service=net_protocol.SERVICE_NAME)
TASKS_ADDRESS = Address(node=tasks_protocol.NODE_ID, service=tasks_protocol.SERVICE_NAME)


def node_address(node_id: str | None) -> Address | None:
    return Address(node=node_id, service=NODE_SERVICE) if node_id else None
