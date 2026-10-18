# Устройство кода

`Interactives` (engine.py) — пустой класс, собранный из миксинов. Общее состояние и инфраструктура —
`core.EngineCore` (хранилище, часы, ссылка на ноду llm, реестр фоновых задач, `canary_ok`,
`canary_scope`, `_track`); каждый миксин наследует `EngineCore`, поэтому зависимость от него явная.

| Модуль | Ответственность |
|---|---|
| `core` | `EngineCore`, очередь форм `_Queued`, фоновые задачи |
| `registry` | `REGISTRY` сценариев (порядок: радио, подвал, катакомбы) |
| `turns` | `before_turn` / `after_turn`, заметки, эффекты вещей, продвижение канареечной сцены |
| `offers` | согласие: предложение, спонтанный триггер, ответ словами, кнопки согласия |
| `forms` | очередь форм, живые кнопки, `handle_click`, тул `swap_radio`, реплики после кнопок |
| `positions` | где стоит Альфред, комнаты, возврат домой, первые особенности кабинета |
| `search` / `acts_flow` / `underground_flow` | тулы `search`, `act`, `walk`/`go`, кадры катакомб |
| `photos` / `mood_selfie` / `photo_review` | снимки, портрет к приветствию, селфи к ответу, сверка снимка |
| `item_flow` / `found_items` | предметы: портреты, карточки, опись, действия; найденные вещи |
| `debug` | скрытые подкоманды `/interactives` |
| `keyboards` | callback-коды, клавиатуры, разбор `ia:`/`it:` |
| `scene` | чистая логика темпа сцены (`apply_decision`, `build_scene_note`, `TurnPlan`) |
| `photo_text` | чистые функции описания снимков для промптера |
| `llm_link` | единственный вызов `chat` к службе llm: `ask_chat`, `ask_as_director` |

Данные сценариев остаются отдельно от потоков: `radio`, `cellar`, `underground` (сценарий и тексты),
`acts` (тексты и декларация тула), `places`, `cabinet`, `catacombs` (чистая топология), `items`.
Конвейер «нарисовать -> записать -> отправить -> отметить» для картинок — `image_tools.record_generated`
и `deliver_generated` (используют `generate_image`, портрет, снимки, селфи и отладочный `/draw`).

## Не сделано сознательно

- слияние `Cabinet` и `Room` в один класс (кабинет отличается ключом хранения, `subject_ru`,
  `pose_room_en`; вместо `isinstance(.., Room)` введены свойства `is_study`, `name_ru`, `in_ru`);
- преамбулы промптов Ведущего (D10) — персонажный текст;
- единый `SearchTarget` для обыска (`tool_search` и `search_labyrinth` пока две реализации с общей таблицей
  `places.search_room`);
- `Scenario.trigger` как enum и явные поля вместо `or radio.X` у `ItemKind`/`Scenario`.
