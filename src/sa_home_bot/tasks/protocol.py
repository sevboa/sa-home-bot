from __future__ import annotations

SERVICE_NAME = "tasks"

NODE_ID = "alfred"

ACTION_CREATE = "create"
ACTION_CHAT_LOOP = "chat_loop"


ACTION_TIMER = "timer"

ACTION_FIRE_NOW = "fire_now"

ACTION_MATCH_EVENT = "match_event"

EVENT_TASK_PREWAKE = "task_prewake"
EVENT_TASK_RESULT = "task_result"

EVENT_TOOL_CALL = "tool_call"

TASK_KIND_LLM_CHAT = "llm_chat"
TASK_KIND_PENDING_ACTION_EXPIRE = "pending_action_expire"

EVENT_DELIVER_MESSAGE = "deliver_message"
