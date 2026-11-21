"""Оценка «популярности» поста и фильтр «это не мем». Модуль без зависимостей, чтобы его было легко тестировать."""
from statistics import median
import re

# реклама, розыгрыши, продажи, переливы аудитории (RU + UA)
AD_RE = re.compile(
    r"реклам|#ad\b|#промо|erid|промокод|скидк|знижк|розыгр|розіграш|розігру|розыгрыв|giveaway|конкурс|"
    r"\bприз(ы|и|ів|ов)?\b|подпиш|підпиш|подписк|підписк|переходи|переходь|по ссылке|за посиланням|"
    r"посилання в|ссылка в|ссылку в|жми |тисни|t\.me/\+|t\.me/joinchat|bit\.ly|спонсор|партнёр|партнер|"
    r"купить|купити|замовити|заказать|казино|casino|букмекер|ставки на|бонус|акци[яи]|акці[яї]|"
    r"заработ|заробі|донат|збір на|сбор на|монобанк|monobank|send\.monobank|реквізит|реквизит", re.I)

# 18+
ADULT_RE = re.compile(
    r"18\+|фаптайм|#фап|nsfw|порн|секс|\bsex|голая|голые|оголен|еротик|эротик|білизн|бельё|белье|"
    r"сиськ|цицьк|onlyfans|онліфанс|онлифанс|xxx|интим|інтим|стриптиз", re.I)

# новости, война, трагедии
NEWS_RE = re.compile(
    r"катастроф|загинув|загинул|загибел|погиб|гибел|убит|вбит|вбив|обстріл|обстрел|ракетн|балістик|баллистик|"
    r"шахед|shahed|удар по|тривог|тревог|жертв|поранен|ранен[оы]|теракт|вибух|взрыв|збито|сбит|"
    r"новини|новости|срочно|терміново|офіційно|официально|заявил|заявив|повідомля|сообщает|"
    r"по даним|по данным|генштаб|мінобор|минобор|окупант|оккупант|фронт|наступ", re.I)


def junk_reason(text: str):
    """Почему пост не похож на мем, или None, если всё в порядке."""
    if not text:
        return None
    if AD_RE.search(text):
        return "реклама"
    if ADULT_RE.search(text):
        return "18+"
    if NEWS_RE.search(text):
        return "новости"
    return None


def is_ad(text: str) -> bool:          # совместимость со старым кодом
    return junk_reason(text) is not None


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
        p["score"] = round(ratio * (1 + 25 * eng) * (1 + p.get("laugh", 0)), 4)   # смешные выше
    return posts


# ---------------------------------------------------------------- реакции: смех = мем
LAUGH = set("😁😂🤣😆😄😃😀😅😹🤪😜😝🤭🙃")
SERIOUS = set("😢😭💔🙏🕊😡🤬😱🫡🤯😨😰😥😞😔🥺🖤🤮💩👎😐🤔")
MIN_REACTS = 5          # меньше 5 реакций: статистики нет, правило не применяем


def norm_emoji(e):
    e = re.sub("[️‍\U0001F3FB-\U0001F3FF]", "", e or "")
    return e[:1] if e else ""


def laugh_verdict(reacts, mode="strict"):
    """reacts: [(emoji, count)]. None = пропустить, строка = причина отказа."""
    if mode == "off" or not reacts:
        return None
    reacts = sorted(reacts, key=lambda x: -x[1])
    if sum(c for _, c in reacts) < MIN_REACTS:
        return None
    top = [norm_emoji(e) for e, _ in reacts]
    if top[0] in LAUGH:
        return None
    if mode == "soft" and (any(e in LAUGH for e in top[:3]) or top[0] not in SERIOUS):
        return None
    return f"не смешно ({top[0]})"


def laugh_share(reacts):
    total = sum(c for _, c in reacts) or 1
    return sum(c for e, c in reacts if norm_emoji(e) in LAUGH) / total
