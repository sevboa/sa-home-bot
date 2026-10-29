from __future__ import annotations

from sa_home_bot.bot.interactives import catacombs, frame_heroes, underground


def test_place_hero_is_stable_and_special_places_keep_their_object():
    first = frame_heroes.place_hero("7:node:1,0", "open")
    assert first == frame_heroes.place_hero("7:node:1,0", "open")
    shaft = frame_heroes.place_hero("7:node:0,0", "shaft")
    assert shaft["hero"] == frame_heroes.SPECIAL_HEROES["shaft"]
    assert shaft["depth"] == frame_heroes.DEPTH_SHAFT
    door = frame_heroes.place_hero("7:node:3,1", "door", "darkroom")
    assert door["hero"] == frame_heroes.SPECIAL_HEROES["door:darkroom"]
    assert door["depth"] == frame_heroes.DEPTH_CLOSED
    assert frame_heroes.place_hero("7:node:3,2", "door", None)["hero"] == frame_heroes.DOOR_DEFAULT


def test_place_hero_skips_heroes_of_neighbours():
    salt = "7:node:2,2"
    own = frame_heroes.own_hero(salt)
    assert frame_heroes.place_hero(salt, "open")["key"] == own
    assert frame_heroes.place_hero(salt, "open", avoid={own})["key"] != own


def test_frame_request_puts_hero_first_and_depth_behind_it():
    hero = frame_heroes.place_hero("7:node:1,0", "open")
    text = frame_heroes.frame_request(hero, "black slate walls")
    lead = f"Main subject, in the foreground on the {hero['side']} side of the frame: "
    assert text.startswith(f"{lead}{hero['hero']}.")
    assert f"Behind it: {frame_heroes.DEPTH_OPEN}." in text
    assert text.endswith("Walls (one short secondary detail): black slate walls")
    assert "Walls" not in frame_heroes.frame_request(hero, "")


def test_neighbouring_nodes_of_a_maze_get_different_heroes():
    checked = 0
    for seed in range(11, 23):
        maze = catacombs.Maze.new(1, seed=seed)
        state = catacombs.CatacombsState.at_entrance(maze)
        for step in range(40):
            catacombs.walk(state, maze, catacombs.RELATIVE[step % len(catacombs.RELATIVE)])
        heroes = {}
        for node in maze.nodes.values():
            frame = underground.node_frame(1, maze, node)
            assert frame.request_en.startswith("Main subject")
            tail = frame.request_en.split(" side of the frame: ", 1)[1]
            heroes[node.cell] = tail.split(". Behind it:", 1)[0]
        for passage in maze.passages.values():
            a, b = maze.nodes.get(passage.a), maze.nodes.get(passage.other(passage.a))
            if a is None or b is None or underground.hero_view(maze, a) != "open":
                continue
            if underground.hero_view(maze, b) != "open":
                continue
            assert heroes[a.cell] != heroes[b.cell]
            checked += 1
    assert checked > 5
