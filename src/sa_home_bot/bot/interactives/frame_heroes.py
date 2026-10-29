from __future__ import annotations

import zlib
from collections.abc import Iterable

HEROES: tuple[tuple[str, str], ...] = (
    ("skull_niche", "a deep wall niche filled with stacked human skulls"),
    ("roots", "thick gnarled tree roots breaking through the vaulted ceiling"),
    ("puddle", "a large black puddle of water on the floor with a bright reflection of a flame"),
    ("rubble", "a collapsed heap of fallen stones and rubble half blocking the way"),
    ("rat", "a big grey rat sitting on a heap of rags and straw"),
    ("bone_pile", "a big heap of white human bones and skulls on the floor"),
    ("oil_lamp", "an old bronze oil lamp hanging on a chain from the ceiling"),
    ("dripping", "water dripping from the ceiling into a worn stone basin"),
    ("glow_mold", "patches of glowing mold creeping over the damp wall"),
    ("steps_down", "narrow worn stone steps leading down into darkness"),
    ("statue", "a broken stone statue of a robed saint missing its head"),
    ("coffin", "a large stone coffin with a carved lid on the floor"),
    ("chains", "rusty iron chains and shackles hanging from the wall"),
    ("chest", "a rotten iron-bound wooden chest, lid half open"),
    ("altar", "a small stone altar covered in melted candle stubs"),
    ("stone_face", "a huge carved stone face on the wall with an open mouth"),
    ("skeleton", "a skeleton slumped against the wall in rotten rags"),
    ("urns", "three big cracked clay urns standing on the floor"),
    ("cart", "an old wooden handcart with a broken wheel"),
    ("herbs", "bundles of dried herbs and bones hanging from iron hooks"),
    ("well", "a round stone well with a wooden bucket hanging on a rope"),
    ("cross", "a weathered carved stone cross in a wall recess"),
    ("books", "a pile of rotting leather-bound books on a stone ledge"),
    ("channel", "a channel of dark water running along the floor"),
    ("sword", "a rusty sword stuck between the stones of the wall"),
    ("pillar", "a stone column carved with coiling serpents"),
    ("barrels", "a pile of rotten barrels and broken planks"),
    ("bats", "a cluster of bats hanging from the ceiling"),
    ("mushrooms", "pale tall mushrooms growing from a rotten log"),
)

LIGHTS: tuple[str, ...] = (
    "single torch burning in an iron wall bracket, warm orange glow, long shadows",
    "warm lantern light from the viewer, deep darkness beyond",
    "a lone candle flickering, small warm pool of light",
    "dim torchlight from one side, deep shadows",
    "warm lantern glow, pitch black corners",
)
GLOW_LIGHT = "eerie green glow of luminous fungus, dark surroundings"
HATCH_LIGHT = "faint grey light from the hatch above, lantern glow below"
MOODS: tuple[str, ...] = (
    "eerie atmosphere", "somber mood", "mysterious atmosphere", "gloomy atmosphere", "ominous mood",
)

SPECIAL_HEROES = {
    "shaft": "a frayed rope hanging down from a square hatch high above",
    "lever": "a big rusty iron lever on a blank stone wall",
    "door:darkroom": "a low old wooden door with a small red glass window",
    "door:laboratory": "a heavy iron-bound door, green light leaking under it",
    "door:cave": "a jagged crack in the wall opening into a natural cave",
    "door:wine_cellar": "an arched oak door with barrels stacked beside it",
}
DOOR_DEFAULT = "a low old wooden door with iron studs set in the wall"

HERO_WEIGHT = 1.3
PLACE_WEIGHT = 1.0

CLOSED_VIEWS = frozenset({"dead_end", "door", "lever"})
DEPTH_SHAFT = (
    "the rough walls of a stone shaft behind it, dark catacomb passages leading away, "
    "warm light far in the distance"
)
DEPTH_CLOSED = (
    "a vaulted catacomb chamber with stone arches behind it, warm light far in the background"
)
DEPTH_OPEN = (
    "a deep catacomb passage with stone arches receding behind it, warm light far in the distance"
)

FRAME_NEGATIVE_EN = "outdoors, landscape, mountains, forest, trees, building exterior, ruins"


def _crc(salt: str) -> int:
    return zlib.crc32(salt.encode())


def hero_order(salt: str) -> list[int]:
    n = len(HEROES)
    start = _crc(salt + "#hero") % n
    step = 1 + _crc(salt + "#step") % (n - 1)
    order: list[int] = []
    k = start
    for _ in range(n):
        if k not in order:
            order.append(k)
        k = (k + step) % n
    order += [i for i in range(n) if i not in order]
    return order


def own_hero(salt: str) -> str:
    return HEROES[hero_order(salt)[0]][0]


def place_hero(
    salt: str, view: str, special: str | None = None, avoid: Iterable[str] = ()
) -> dict[str, str]:
    mood = MOODS[_crc(salt + "#mood") % len(MOODS)]
    light = LIGHTS[_crc(salt + "#light") % len(LIGHTS)]
    side = "left" if _crc(salt + "#side") % 2 == 0 else "right"
    depth = DEPTH_SHAFT if view == "shaft" else DEPTH_CLOSED if view in CLOSED_VIEWS else DEPTH_OPEN
    base = {"mood": mood, "side": side, "depth": depth}
    if view == "shaft":
        return {"key": "shaft", "hero": SPECIAL_HEROES["shaft"], "light": HATCH_LIGHT, **base}
    if view == "lever":
        return {"key": "lever", "hero": SPECIAL_HEROES["lever"], "light": light, **base}
    if view == "door":
        hero = SPECIAL_HEROES.get(f"door:{special}", DOOR_DEFAULT)
        return {"key": f"door:{special}", "hero": hero, "light": light, **base}
    skip = set(avoid)
    order = hero_order(salt)
    key, hero = next((HEROES[k] for k in order if HEROES[k][0] not in skip), HEROES[order[0]])
    if key == "glow_mold":
        light = GLOW_LIGHT
    return {"key": key, "hero": hero, "light": light, **base}


def frame_request(p: dict[str, str], describe: str) -> str:
    walls = f" Walls (one short secondary detail): {describe}" if describe else ""
    return (
        f"Main subject, in the foreground on the {p['side']} side of the frame: {p['hero']}. "
        f"Behind it: {p['depth']}. The only light: {p['light']}. Mood: {p['mood']}.{walls}"
    )
