"""Расчёт «популярности» поста. Отдельный модуль без зависимостей, чтобы его можно было тестировать."""
from statistics import median
import re

AD_RE = re.compile(r"реклам|erid|#ad\b|промокод|скидк|розыгрыш|giveaway|подпишись на|переходи по ссылке|t\.me/\+|bit\.ly", re.I)


def is_ad(text: str) -> bool:
    return bool(text and AD_RE.search(text))


def hot_scores(posts, now_ts):
    """posts: список dict с ключами views, reactions, forwards, ts (unix).
    Возвращает тот же список, у каждого поста добавлен ключ score.

    Идея: сравниваем пост не с другими каналами, а с обычными просмотрами своего же канала.
    Пост, набравший в 3 раза больше медианы канала, «залетел», даже если канал маленький.
    Свежим постам даём поправку: они ещё не успели набрать просмотры.
    Реакции и особенно репосты повышают оценку: мемы, которые пересылают, самые живые.
    """
    if not posts:
        return posts
    med = median([max(p["views"], 1) for p in posts]) or 1
    for p in posts:
        age_h = max((now_ts - p["ts"]) / 3600, 0.25)
        ratio = max(p["views"], 1) / med
        if age_h < 6:                       # свежак ещё набирает просмотры
            ratio *= min((6 / age_h) ** 0.5, 3.0)
        eng = (p["reactions"] + 3 * p["forwards"]) / max(p["views"], 1)
        p["score"] = round(ratio * (1 + 25 * eng), 4)
    return posts
