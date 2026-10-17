# Требования умений (`utils/requirements.py`)

Первую установку ноды (питон, git, сама программа) админ делает вручную — это не автоматизируется.
Отдельные умения внутри развёрнутой ноды (smartctl, journalctl, nvidia-smi, …) не должны падать и
шуметь в логи при нехватке внешней программы, неподходящей ОС или недостатке прав. `Requirement`
различает три причины (`RequirementStatus`: `MISSING_PROGRAM`, `UNSUPPORTED_PLATFORM`,
`NEEDS_PRIVILEGE`; `OK` — всё в порядке) и даёт готовую подсказку:
- для нехватки программы — команда под менеджер пакетов, реально найденный в PATH (порядок
  `_PACKAGE_MANAGERS`: apt, pacman, dnf, zypper, apk, brew, winget); если ни одного нет — «установите
  пакет … через пакетный менеджер системы»;
- для нехватки прав — отсылка к `nodectl fix` (`node/fixups.py`);
- `install_argv` — та же установка как argv без `sudo` для `nodectl fix`: fixups сами решают, как
  звать sudo (интерактивно, наследуя TTY). Нет известного менеджера → `None`, фикс пропускается с
  предупреждением.

Нехватку прав нельзя определить статически (программа есть в PATH, но реальный вызов упирается в
permission denied) — это узнаётся только по факту неудачного вызова подпроцесса, глубоко внутри
`sensors/*.py`. Оттуда диагноз репортится в модульный `requirements_registry`, а наружу (`get_state`
службы monitor) его читает `monitor/service.py`.

`RequirementRegistry.status_for` всегда пересчитывает статику (`diagnose()` — дешёвая и не протухает)
и добавляет из реестра только `NEEDS_PRIVILEGE`, и то пока статика говорит «программа на месте»:
иначе реестр хранил бы протухший диагноз для удалённой или переустановленной программы. Ключ
записи реестра — `Requirement.key`, а если не задан, то `program` (явный `key` нужен, когда одна
программа используется несколькими `Requirement` с разными правами).

## Детекторы отказа

- `looks_like_permission_error(stderr)` — маркеры `_PERMISSION_MARKERS` (permission denied, operation
  not permitted, must be root, requires root, EACCES, access is denied для Windows), регистронезависимо.
  Не дубликат `proto/endpoints.py::is_virtual_iface` — совпадение лишь по форме (поиск подстрок).
- `looks_like_sudo_denied(stderr)` и `looks_like_sudo_message(stderr)` — для `sudo -n`; различие и
  потребители описаны в [README.md](README.md).
  `sudo -n` без права печатает локализованное сообщение («a password is required» / «требуется указать
  пароль») — но всегда с префиксом `sudo:`; на него опирается вторая функция, чтобы не зависеть от
  локали ноды.
