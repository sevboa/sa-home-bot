from __future__ import annotations

SERVICE_NAME = "graph_memory"

NODE_ID = "mycraft"

MAX_EPISODE_CHARS = 400
EPISODE_TIMEOUT_S = 2.0

ACTION_ADD_EPISODE = "add_episode"
ACTION_SEARCH = "search"
ACTION_QUEUE_STATUS = "queue_status"

EPISODE_SOURCE_MEMORY_FACT = "memory_fact"
EPISODE_SOURCE_DIALOGUE_TURN = "dialogue_turn"
EPISODE_SOURCE_LOOK_AT_PHOTO = "look_at_photo"
EPISODE_SOURCE_LINK = "link"
EPISODE_SOURCE_WEB_SEARCH = "web_search"
EPISODE_SOURCE_IMAGE = "image"

ACTION_PERSON_CLAIM = "person_claim"
ACTION_PERSON_CARDS = "person_cards"
PEOPLE_GROUP_ID = "people"

PERSON_FIELD_GENDER = "gender"
PERSON_FIELD_NAME = "name"
PERSON_FIELD_ALIAS = "alias"
GENDER_MALE = "m"
GENDER_FEMALE = "f"

CLAIM_STRENGTH_SELF = "self"
CLAIM_STRENGTH_ACQUAINTANCE = "acquaintance"
