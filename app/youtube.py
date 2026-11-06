"""Interpret playback evidence, separately from ancillary host reachability."""


def _number(value):
    try:
        return max(0, int(value))
    except (ValueError, TypeError, OverflowError):
        return 0


def _http(kv, name):
    parts = kv.get(name, "").split()
    return parts[0] if parts else "000"


def evaluate(kv):
    if kv.get("youtube_probe_version") != "2":
        return "unknown", "агент обновляет проверку воспроизведения"

    received = _number(kv.get("youtube_active_bytes"))
    clients = _number(kv.get("youtube_active_clients"))
    if clients and received >= 65536:
        return "ok", f"устройства получают видео · {received // 1024} КБ за время проверки"

    media = kv.get("youtube_media", "").split()
    if (len(media) == 4 and media[0] in ("200", "206")
            and kv.get("youtube_media_exit") == "0"
            and 4096 <= _number(media[2]) <= 131072 and media[3].startswith("video/")):
        return "ok", f"загружен фрагмент видео · {_number(media[2]) // 1024} КБ · {media[1]} с"

    watch, api = _http(kv, "youtube_watch"), _http(kv, "youtube_api")
    if watch == api == "000":
        return "fail", "с роутера не отвечают страница видео и API YouTube"
    if watch.startswith("5") and api.startswith("5"):
        return "fail", f"страница и API YouTube вернули HTTP {watch}/{api}"

    if kv.get("youtube_media_candidate") == "1":
        code = media[0] if media else "000"
        if code == "000" or code.startswith("5"):
            return "fail", "страница видео доступна, но с роутера не загружается фрагмент видео"
        return "unknown", "YouTube ответил, но загрузку видео подтвердить не удалось"

    if kv.get("youtube_parser") == "unavailable":
        return "unknown", "YouTube ответил; на роутере нет парсера проверки видео"
    if kv.get("youtube_watch_valid") == "1" and kv.get("youtube_api_valid") == "1":
        return "unknown", "страница и API доступны; автоматический запуск видео не подтверждён"
    if watch in ("403", "429") or api in ("403", "429"):
        return "unknown", "YouTube ограничил автоматическую проверку; воспроизведение не проверено"
    return "unknown", "воспроизведение не подтверждено; полного отказа YouTube не обнаружено"
