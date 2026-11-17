"""Мем-радар для GitHub Actions: присылает в Telegram самые популярные мемы. Ваш аккаунт Telegram не нужен.

Источники:
  * публичные Telegram-каналы через открытую веб-версию https://t.me/s/<канал> (без входа);
  * Lemmy: открытая альтернатива Reddit, публичный API без ключей;
  * Reddit: если в секретах указаны REDDIT_CLIENT_ID и REDDIT_CLIENT_SECRET.

Режимы (python memradar.py --mode ...):
  auto     запускается по расписанию: обрабатывает команды из чата и, если пора, шлёт подборку
  digest   прислать подборку сейчас (кнопка Run workflow на GitHub)
  command  выполнить одну команду (--cmd "/top 5 ua"), так работает мгновенный режим через воркер
  test     проверить источники: прислать отчёт, сколько постов удалось прочитать с каждого
"""
import argparse
import html
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from scoring import hot_scores, is_ad

BASE = Path(__file__).resolve().parent
SRC_FILE, STATE_FILE = BASE / "sources.json", BASE / "state.json"
TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER = os.getenv("OWNER_ID", "").strip()
REDDIT_ID = os.getenv("REDDIT_CLIENT_ID", "").strip()
REDDIT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
API = f"https://api.telegram.org/bot{TOKEN}"
LANGS = ("ru", "ua", "en")

HELP = """<b>Мем-радар</b>: самые залетевшие мемы из Telegram, Lemmy и Reddit.

Всё управление — <b>кнопками внизу чата</b>:
🔥 <b>Топ за сутки</b>: лучшие мемы за 24 часа
⚡ <b>Залетает сейчас</b>: что набирает обороты за 4 часа
🎬 <b>Только видео</b>: для вставок в монтаж
📋 <b>Источники</b>: откуда берутся мемы
➕ <b>Добавить</b> / ➖ <b>Удалить</b> источник
⚙️ <b>Настройки</b>: как часто и сколько присылать, пауза

Добавить канал можно и проще: просто пришлите сюда ссылку на него (t.me/…) или @ник.

⏳ Если мгновенный режим не настроен (шаг 9 инструкции), бот проверяет чат раз в 10 минут,
поэтому ответ на кнопку приходит в течение 10–20 минут."""


def log(*a):
    print(*a, flush=True)


def load(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


SRC = load(SRC_FILE, {})
STATE = load(STATE_FILE, {})
STATE.setdefault("sent", [])
STATE.setdefault("offset", 0)
STATE.setdefault("last_digest", 0)
SET = SRC.setdefault("settings", {})
for k, v in {"every_hours": 4, "count": 8, "max_per_source": 2, "window_hours": 24, "paused": False}.items():
    SET.setdefault(k, v)
SRC.setdefault("telegram", {l: [] for l in LANGS})
SRC.setdefault("lemmy", [])
SRC.setdefault("reddit", [])


def save():
    STATE["sent"] = STATE["sent"][-4000:]
    SRC_FILE.write_text(json.dumps(SRC, ensure_ascii=False, indent=2) + "\n", "utf-8")
    STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False, indent=1) + "\n", "utf-8")


def owner_chat():
    return OWNER or STATE.get("owner")


# ---------------------------------------------------------------- Telegram Bot API
def tg(method, **params):
    files = params.pop("files", None)
    try:
        r = requests.post(f"{API}/{method}", data=params, files=files, timeout=120)
        j = r.json()
    except Exception as e:
        log("Bot API error:", method, e)
        return {"ok": False, "description": str(e)}
    if not j.get("ok"):
        log("Bot API:", method, j.get("error_code"), j.get("description"))
    return j


def say(text, chat=None, kb=None):
    extra = {"reply_markup": json.dumps(kb, ensure_ascii=False)} if kb else {}
    return tg("sendMessage", chat_id=chat or owner_chat(), text=text, parse_mode="HTML",
              disable_web_page_preview="true", **extra)


def edit(chat, mid, text, kb=None):
    extra = {"reply_markup": json.dumps(kb, ensure_ascii=False)} if kb else {}
    j = tg("editMessageText", chat_id=chat, message_id=mid, text=text, parse_mode="HTML",
           disable_web_page_preview="true", **extra)
    if not j.get("ok") and "not modified" not in str(j.get("description", "")):
        say(text, chat, kb)                    # старое сообщение не редактируется: шлём новое


# ---------------------------------------------------------------- источники
def parse_count(s):
    s = (s or "").strip().upper().replace(",", ".").replace(" ", "")
    m = re.search(r"([\d.]+)([KM]?)", s)
    if not m:
        return 0
    n = float(m.group(1))
    return int(n * (1000 if m.group(2) == "K" else 1_000_000 if m.group(2) == "M" else 1))


def parse_tme(html_text, channel, lang):
    """Разбор страницы t.me/s/<канал>. Возвращает (посты, id самого старого поста)."""
    soup = BeautifulSoup(html_text, "html.parser")
    posts, oldest = [], None
    for m in soup.select("div.tgme_widget_message[data-post]"):
        try:
            pid = int(m["data-post"].split("/")[-1])
        except ValueError:
            continue
        oldest = pid if oldest is None else min(oldest, pid)
        t = m.select_one("time[datetime]")
        v = m.select_one(".tgme_widget_message_views")
        if not t or not v:
            continue
        ts = datetime.fromisoformat(t["datetime"].replace("Z", "+00:00")).timestamp()
        text_el = m.select_one(".tgme_widget_message_text")
        text = text_el.get_text(" ", strip=True) if text_el else ""
        if is_ad(text):
            continue
        kind, media = None, None
        vid = m.select_one("video.tgme_widget_message_video, video[src]")
        if vid and vid.get("src"):
            kind, media = "video", vid["src"]
        elif m.select_one(".tgme_widget_message_video_player, .tgme_widget_message_roundvideo_player"):
            kind = "video"                                   # большое видео без прямой ссылки
        else:
            ph = m.select_one("a.tgme_widget_message_photo_wrap[style]")
            if ph:
                u = re.search(r"url\(['\"]?(.*?)['\"]?\)", ph["style"])
                if u:
                    kind, media = "photo", u.group(1)
        if not kind:
            continue
        reacts = sum(parse_count(x.get_text()) for x in m.select(".tgme_reaction"))
        posts.append({"src": "tg", "source": "@" + channel, "lang": lang, "id": f"tg:{channel}/{pid}",
                      "kind": kind, "media": media, "views": parse_count(v.get_text()), "reactions": reacts,
                      "forwards": 0, "ts": ts, "text": text[:300], "link": f"https://t.me/{channel}/{pid}"})
    return posts, oldest


def fetch_tg(channel, lang, hours):
    cutoff = time.time() - hours * 3600
    out, before = [], None
    for _page in range(3):                                   # до ~60 последних постов
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        except Exception as e:
            log(f"@{channel}: сеть", e)
            break
        if r.status_code != 200 or "tgme_widget_message" not in r.text:
            log(f"@{channel}: нет веб-версии (код {r.status_code})")
            break
        posts, oldest = parse_tme(r.text, channel, lang)
        out += posts
        if not oldest or not posts or min(p["ts"] for p in posts) < cutoff:
            break
        before = oldest
        time.sleep(1)
    return [p for p in out if p["ts"] >= cutoff]


def fetch_lemmy(comm, hours):
    name, _, inst = comm.partition("@")
    inst = inst or "lemmy.world"
    sort = "TopSixHour" if hours <= 6 else "TopDay"
    try:
        r = requests.get(f"https://{inst}/api/v3/post/list", params={"community_name": comm, "sort": sort, "limit": 30},
                         headers={"User-Agent": "memradar/2.0"}, timeout=30)
        items = r.json().get("posts", [])
    except Exception as e:
        log(f"lemmy {comm}: ошибка", e)
        return []
    out = []
    for it in items:
        p, c = it.get("post", {}), it.get("counts", {})
        if p.get("nsfw") or p.get("removed") or p.get("deleted"):
            continue
        url = p.get("url") or ""
        low = url.lower().split("?")[0]
        if low.endswith((".mp4", ".webm", ".mov")):
            kind, media = "video", url
        elif low.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            kind, media = "photo", url
        elif p.get("thumbnail_url"):
            kind, media = "photo", p["thumbnail_url"]
        else:
            continue
        try:
            ts = datetime.fromisoformat(p["published"].replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            ts = time.time()
        out.append({"src": "lemmy", "source": comm, "lang": "en", "id": f"lemmy:{p.get('ap_id', p.get('id'))}",
                    "kind": kind, "media": media, "views": c.get("score", 0), "reactions": c.get("comments", 0),
                    "forwards": 0, "ts": ts, "text": p.get("name", "")[:300], "link": p.get("ap_id") or url})
    return out


_reddit_token = None


def fetch_reddit(sub, hours):
    global _reddit_token
    if not (REDDIT_ID and REDDIT_SECRET):
        return []
    ua = "memradar/2.0 (personal meme digest)"
    try:
        if not _reddit_token:
            r = requests.post("https://www.reddit.com/api/v1/access_token", auth=(REDDIT_ID, REDDIT_SECRET),
                              data={"grant_type": "client_credentials"}, headers={"User-Agent": ua}, timeout=30)
            _reddit_token = r.json().get("access_token")
            if not _reddit_token:
                log("reddit: ключи не подошли")
                return []
        r = requests.get(f"https://oauth.reddit.com/r/{sub}/top", params={"t": "day" if hours > 6 else "hour", "limit": 30},
                         headers={"Authorization": f"bearer {_reddit_token}", "User-Agent": ua}, timeout=30)
        children = r.json().get("data", {}).get("children", [])
    except Exception as e:
        log(f"reddit r/{sub}: ошибка", e)
        return []
    out = []
    for ch in children:
        d = ch.get("data", {})
        if d.get("over_18") or d.get("stickied"):
            continue
        url = d.get("url_overridden_by_dest") or d.get("url", "")
        if d.get("is_video"):
            kind, media = "video", ((d.get("media") or {}).get("reddit_video") or {}).get("fallback_url")
        elif url.lower().endswith((".jpg", ".jpeg", ".png", ".gif")):
            kind, media = "photo", url
        else:
            continue
        out.append({"src": "reddit", "source": f"r/{sub}", "lang": "en", "id": f"reddit:{d.get('id')}",
                    "kind": kind, "media": media, "views": d.get("ups", 0), "reactions": d.get("num_comments", 0),
                    "forwards": d.get("num_crossposts", 0), "ts": d.get("created_utc", time.time()),
                    "text": d.get("title", "")[:300], "link": "https://reddit.com" + d.get("permalink", "")})
    return out


def collect(lang="all", hours=24, only_video=False, report=None):
    pool = []
    for lg, chans in SRC["telegram"].items():
        if lang in ("all", lg):
            for ch in chans:
                ps = hot_scores(fetch_tg(ch, lg, hours), time.time())
                if report is not None:
                    report.append(f"@{ch}: {len(ps)}")
                pool += ps
    if lang in ("all", "en"):
        for comm in SRC["lemmy"]:
            ps = hot_scores(fetch_lemmy(comm, hours), time.time())
            if report is not None:
                report.append(f"lemmy {comm}: {len(ps)}")
            pool += ps
        for sub in SRC["reddit"]:
            ps = hot_scores(fetch_reddit(sub, hours), time.time())
            if report is not None and (REDDIT_ID and REDDIT_SECRET):
                report.append(f"r/{sub}: {len(ps)}")
            pool += ps
    sent = set(STATE["sent"])
    pool = [p for p in pool if p["id"] not in sent and (not only_video or p["kind"] == "video")]
    pool.sort(key=lambda p: p["score"], reverse=True)
    per, top = {}, []
    for p in pool:
        if per.get(p["source"], 0) >= SET["max_per_source"]:
            continue
        per[p["source"]] = per.get(p["source"], 0) + 1
        top.append(p)
    return top


# ---------------------------------------------------------------- отправка
def caption(p, n):
    if p["src"] == "tg":
        stats = f"👁 {p['views']:,}".replace(",", " ") + (f"  ❤️ {p['reactions']}" if p["reactions"] else "")
    else:
        stats = f"⬆️ {p['views']:,}  💬 {p['reactions']}".replace(",", " ")
    text = html.escape(p["text"])
    text = text[:180] + "…" if len(text) > 180 else text
    return (f"<b>#{n}</b> · {html.escape(p['source'])} · {p['lang'].upper()} · залёт ×{p['score']:.1f}\n{stats}\n"
            + (f"{text}\n" if text else "") + f'<a href="{p["link"]}">Оригинал</a>')


def send_post(chat, p, n):
    cap = caption(p, n)
    if p.get("media"):
        try:
            r = requests.get(p["media"], headers={"User-Agent": UA}, timeout=90, stream=True)
            limit = (49 if p["kind"] == "video" else 9) * 1024 * 1024
            data = b""
            for chunk in r.iter_content(1 << 16):
                data += chunk
                if len(data) > limit:
                    raise ValueError("файл слишком большой")
            ext = ".mp4" if p["kind"] == "video" else ".jpg"
            method, field = ("sendVideo", "video") if p["kind"] == "video" else ("sendPhoto", "photo")
            extra = {"supports_streaming": "true"} if p["kind"] == "video" else {}
            j = tg(method, chat_id=chat, caption=cap, parse_mode="HTML", files={field: ("meme" + ext, data)}, **extra)
            if j.get("ok"):
                return True
        except Exception as e:
            log("медиа не отправилось:", p["link"], e)
    # запасной вариант: ссылка, Telegram покажет превью поста
    j = tg("sendMessage", chat_id=chat, text=cap, parse_mode="HTML")
    return j.get("ok", False)


def digest(chat, n=None, lang="all", hours=None, only_video=False, title="Топ мемов"):
    n = n or SET["count"]
    hours = hours or SET["window_hours"]
    top = collect(lang, hours, only_video)
    if not top:
        say("Свежих залётов не нашёл. Попробуйте /top без фильтров или /reset.", chat)
        return
    now = datetime.now(timezone.utc).astimezone()
    say(f"🔥 <b>{title}</b> · {min(n, len(top))} шт. · {lang.upper() if lang != 'all' else 'RU/UA/EN'}", chat)
    for i, p in enumerate(top[:n], 1):
        if send_post(chat, p, i):
            STATE["sent"].append(p["id"])
        time.sleep(1.5)
    say("Ещё подборку?", chat, MORE_KB)
    log(f"Отправлено {min(n, len(top))} мемов, {now:%Y-%m-%d %H:%M}")


# ---------------------------------------------------------------- кнопки
B_TOP, B_HOT, B_VIDEO = "🔥 Топ за сутки", "⚡ Залетает сейчас", "🎬 Только видео"
B_SRC, B_ADD, B_DEL = "📋 Источники", "➕ Добавить источник", "➖ Удалить источник"
B_SET, B_HELP = "⚙️ Настройки", "❓ Помощь"
MAIN_KB = {"keyboard": [[{"text": B_TOP}, {"text": B_HOT}],
                        [{"text": B_VIDEO}, {"text": B_SRC}],
                        [{"text": B_ADD}, {"text": B_DEL}],
                        [{"text": B_SET}, {"text": B_HELP}]],
           "resize_keyboard": True, "is_persistent": True,
           "input_field_placeholder": "Нажмите кнопку или пришлите ссылку на канал"}
FLAGS = {"ru": "🇷🇺 RU", "ua": "🇺🇦 UA", "en": "🇬🇧 EN"}
BOT_COMMANDS = [("start", "Меню и кнопки"), ("top", "Топ мемов за сутки"), ("hot", "Что залетает сейчас"),
                ("video", "Только видео"), ("sources", "Источники"), ("add", "Добавить источник"),
                ("del", "Удалить источник"), ("settings", "Настройки"), ("help", "Помощь")]


def btn(text, data):
    return {"text": text, "callback_data": data[:64]}


def ikb(rows):
    return {"inline_keyboard": rows}


MORE_KB = ikb([[btn("🇷🇺 RU", "top:ru"), btn("🇺🇦 UA", "top:ua"), btn("🇬🇧 EN", "top:en")],
               [btn("🎬 Видео", "video:all"), btn("⚡ Свежак", "hot:all")]])


def norm_channel(s):
    s = s.strip().lstrip("@")
    s = re.sub(r"^(https?://)?(t\.me|telegram\.me)/(s/)?", "", s)
    return s.split("/")[0].split("?")[0]


def detect_source(text, loose=False):
    """Понимает, что прислал пользователь: Telegram-канал, сообщество Lemmy или сабреддит."""
    t = text.strip()
    m = re.match(r"^(?:https?://)?(?:www\.|old\.)?(?:reddit\.com/)?/?r/([A-Za-z0-9_]{2,30})/?$", t)
    if m:
        return "reddit", m.group(1)
    m = re.match(r"^(?:https?://([\w.-]+)/c/([\w]+)|!?([\w]+)@([\w-]+\.[\w.-]+))$", t)
    if m:
        name, inst = (m.group(2), m.group(1)) if m.group(1) else (m.group(3), m.group(4))
        return "lemmy", f"{name.lower()}@{inst.lower()}"
    if re.match(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?[A-Za-z0-9_]{4,32}", t) or re.match(r"^@[A-Za-z0-9_]{4,32}$", t):
        return "tg", norm_channel(t)
    if loose and re.match(r"^[A-Za-z][A-Za-z0-9_]{3,31}$", t):
        return "tg", t
    return None


def sources_text():
    lines = ["<b>📋 Источники</b>"]
    for lg, ch in SRC["telegram"].items():
        lines.append(f"{FLAGS.get(lg, lg)}: " + (", ".join("@" + c for c in ch) or "—"))
    lines.append("🌐 Lemmy: " + (", ".join(SRC["lemmy"]) or "—"))
    rd = ", ".join("r/" + x for x in SRC["reddit"]) or "—"
    lines.append(f"👽 Reddit: {rd}" + ("" if REDDIT_ID else " <i>(выключен: нет ключей)</i>"))
    return "\n".join(lines)


def del_kb():
    items = [(f"❌ @{c} · {lg.upper()}", f"del:tg:{c}") for lg, ch in SRC["telegram"].items() for c in ch]
    items += [(f"❌ {c}", f"del:lm:{c}") for c in SRC["lemmy"]]
    items += [(f"❌ r/{c}", f"del:rd:{c}") for c in SRC["reddit"]]
    rows = [[btn(*x) for x in items[i:i + 2]] for i in range(0, len(items), 2)]
    return ikb(rows + [[btn("✅ Готово", "close")]])


def settings_view():
    on = lambda cond, label: ("✅ " if cond else "") + label
    text = (f"<b>⚙️ Настройки</b>\nПодборка: каждые <b>{SET['every_hours']} ч</b>, по <b>{SET['count']}</b> мемов\n"
            f"Статус: {'⏸ на паузе' if SET['paused'] else '▶️ работает'}\n\nНажмите, чтобы изменить:")
    kb = ikb([
        [btn(on(SET["every_hours"] == h, f"каждые {h} ч" if h == 2 else f"{h} ч"), f"every:{h}") for h in (2, 4, 6)],
        [btn(on(SET["every_hours"] == h, f"{h} ч"), f"every:{h}") for h in (8, 12, 24)],
        [btn(on(SET["count"] == c, f"{c} мемов" if c == 5 else str(c)), f"count:{c}") for c in (5, 8, 10, 15)],
        [btn("▶️ Включить рассылку", "resume") if SET["paused"] else btn("⏸ Пауза", "pause")],
        [btn("🧪 Проверить источники", "test"), btn("♻️ Забыть историю", "reset")],
    ])
    return text, kb


def menu(chat):
    tg("setMyCommands", commands=json.dumps([{"command": c, "description": d} for c, d in BOT_COMMANDS], ensure_ascii=False))
    say(HELP, chat, MAIN_KB)


# ---------------------------------------------------------------- действия с источниками
def add_tg(ch, lg, chat):
    test = fetch_tg(ch, lg, 24 * 7)
    if not test:
        try:
            ok = "tgme_widget_message" in requests.get(f"https://t.me/s/{ch}", headers={"User-Agent": UA}, timeout=30).text
        except Exception:
            ok = False
        if not ok:
            return say(f"😕 У @{html.escape(ch)} нет открытой веб-версии (t.me/s/{html.escape(ch)}), "
                       "поэтому без входа в аккаунт его не прочитать. Попробуйте другой канал.", chat)
    for l in SRC["telegram"].values():
        if ch in l:
            l.remove(ch)
    SRC["telegram"].setdefault(lg, []).append(ch)
    say(f"✅ Добавил @{html.escape(ch)} в {FLAGS[lg]}" + (f"\nМедиапостов за неделю: {len(test)}" if test else ""), chat, MAIN_KB)


def add_lemmy(c, chat):
    if not fetch_lemmy(c, 24 * 7):
        return say(f"😕 Не получилось прочитать {html.escape(c)}. Проверьте название в формате <code>сообщество@сервер</code>, "
                   "например <code>memes@lemmy.world</code>.", chat)
    if c not in SRC["lemmy"]:
        SRC["lemmy"].append(c)
    say(f"✅ Добавил Lemmy {html.escape(c)}", chat, MAIN_KB)


def add_reddit(x, chat):
    if x not in SRC["reddit"]:
        SRC["reddit"].append(x)
    say(f"✅ Добавил r/{html.escape(x)}" + ("" if REDDIT_ID else "\n⚠️ Reddit заработает, когда добавите ключи (инструкция, шаг 8)."), chat, MAIN_KB)


def remove_source(typ, name):
    if typ == "tg":
        found = False
        for l in SRC["telegram"].values():
            if name in l:
                l.remove(name)
                found = True
        return found
    key = "lemmy" if typ == "lm" else "reddit"
    before = len(SRC[key])
    SRC[key] = [x for x in SRC[key] if x != name]
    return len(SRC[key]) < before


def offer_tg(ch, chat):
    say(f"В какой раздел добавить <b>@{html.escape(ch)}</b>?", chat,
        ikb([[btn(FLAGS[l], f"add:tg:{l}:{ch}") for l in LANGS], [btn("✖️ Отмена", "close")]]))


def ask_add(chat):
    STATE["await"] = "add"
    say("➕ <b>Пришлите источник</b> одним сообщением:\n\n"
        "• Telegram-канал: ссылка <code>t.me/имя</code> или <code>@имя</code>\n"
        "• Lemmy: <code>memes@lemmy.world</code>\n"
        "• Reddit: <code>r/memes</code>", chat,
        {"force_reply": True, "input_field_placeholder": "@канал, memes@lemmy.world или r/memes"})


def run_test(chat):
    rep = []
    top = collect("all", 24, report=rep)
    bad = [r for r in rep if r.endswith(": 0")]
    say("🧪 <b>Проверка источников</b> (медиапостов за 24 ч)\n" + "\n".join(rep)
        + f"\n\nВсего кандидатов: {len(top)}"
        + (f"\n⚠️ Пустые: {len(bad)}. Канал мог закрыть веб-версию или ничего не публиковать, такие можно удалить." if bad else ""), chat)


# ---------------------------------------------------------------- обработка сообщений и кнопок
def handle(text, chat):
    text = (text or "").strip()
    if not text:
        return
    # нижние кнопки
    simple = {B_TOP: "/top", B_HOT: "/hot", B_VIDEO: "/video", B_SRC: "/sources", B_ADD: "/add",
              B_DEL: "/del", B_SET: "/settings", B_HELP: "/help"}
    if text in simple:
        text = simple[text]
    if not text.startswith("/"):
        waiting = STATE.pop("await", None) == "add"
        src = detect_source(text, loose=waiting)
        if not src:
            return say("Не понял 🤔 Пользуйтесь кнопками внизу или пришлите ссылку на канал (t.me/…).", chat, MAIN_KB)
        typ, name = src
        return offer_tg(name, chat) if typ == "tg" else add_lemmy(name, chat) if typ == "lemmy" else add_reddit(name, chat)

    STATE.pop("await", None)
    parts = text.split()
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    n, lang = None, "all"
    for x in args:
        if x.isdigit():
            n = max(1, min(int(x), 20))
        elif x.lower() in LANGS:
            lang = x.lower()

    if cmd == "/start":
        menu(chat)
    elif cmd == "/help":
        say(HELP, chat, MAIN_KB)
    elif cmd == "/top":
        digest(chat, n, lang, 24)
    elif cmd == "/hot":
        digest(chat, n, lang, 4, title="Залетает прямо сейчас")
    elif cmd == "/video":
        digest(chat, n, lang, 24, only_video=True, title="Топ видеомемов")
    elif cmd == "/sources":
        say(sources_text(), chat, ikb([[btn("➕ Добавить", "ask:add"), btn("➖ Удалить", "ask:del")]]))
    elif cmd == "/add":
        if not args:
            return ask_add(chat)
        src = detect_source(args[0], loose=True)
        if not src:
            return say("Не понял, что добавить. Пример: <code>/add @канал ua</code>", chat)
        typ, name = src
        if typ == "tg":
            lg = next((x.lower() for x in args[1:] if x.lower() in LANGS), None)
            return add_tg(name, lg, chat) if lg else offer_tg(name, chat)
        return add_lemmy(name, chat) if typ == "lemmy" else add_reddit(name, chat)
    elif cmd == "/del":
        if args:
            src = detect_source(args[0], loose=True)
            typ, name = src if src else ("tg", norm_channel(args[0]))
            ok = remove_source({"tg": "tg", "lemmy": "lm", "reddit": "rd"}[typ], name)
            return say(f"🗑 Убрал {html.escape(name)}" if ok else f"{html.escape(name)} нет в списке", chat)
        say("➖ Нажмите на источник, чтобы удалить его:", chat, del_kb())
    elif cmd == "/settings":
        text_, kb_ = settings_view()
        say(text_, chat, kb_)
    elif cmd == "/every" and args and args[0].isdigit() and 1 <= int(args[0]) <= 24:
        SET["every_hours"] = int(args[0])
        say(f"⏰ Подборка каждые {args[0]} ч.", chat)
    elif cmd == "/count" and args and args[0].isdigit() and 1 <= int(args[0]) <= 20:
        SET["count"] = int(args[0])
        say(f"В подборке будет {args[0]} мемов.", chat)
    elif cmd in ("/pause", "/resume"):
        SET["paused"] = cmd == "/pause"
        say("⏸ Рассылка на паузе." if SET["paused"] else "▶️ Рассылка включена.", chat)
    elif cmd == "/reset":
        STATE["sent"] = []
        say("♻️ Забыл историю: мемы могут повторяться.", chat)
    else:
        say("Не знаю такой команды 🤔 Пользуйтесь кнопками внизу.", chat, MAIN_KB)


def handle_cb(data, chat, mid, cb_id=None):
    if cb_id:
        tg("answerCallbackQuery", callback_query_id=cb_id)
    kind, _, rest = data.partition(":")
    if kind in ("top", "hot", "video"):
        lang = rest if rest in LANGS else "all"
        if kind == "top":
            digest(chat, None, lang, 24, title=f"Топ мемов {FLAGS.get(lang, '')}".strip())
        elif kind == "hot":
            digest(chat, None, lang, 4, title="Залетает прямо сейчас")
        else:
            digest(chat, None, lang, 24, only_video=True, title="Топ видеомемов")
    elif kind == "ask":
        ask_add(chat) if rest == "add" else say("➖ Нажмите на источник, чтобы удалить его:", chat, del_kb())
    elif kind == "add":
        typ, lg, name = (rest.split(":", 2) + ["", ""])[:3]
        if typ == "tg" and lg in LANGS and name:
            tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup=json.dumps(ikb([])))
            add_tg(name, lg, chat)
    elif kind == "del":
        typ, _, name = rest.partition(":")
        ok = remove_source(typ, name)
        label = ("@" + name) if typ == "tg" else ("r/" + name) if typ == "rd" else name
        edit(chat, mid, (f"🗑 Удалил {html.escape(label)}\n\n" if ok else "Этого источника уже нет.\n\n")
             + "➖ Нажмите на источник, чтобы удалить его:", del_kb())
    elif kind in ("every", "count") and rest.isdigit():
        SET["every_hours" if kind == "every" else "count"] = int(rest)
        edit(chat, mid, *settings_view())
    elif kind in ("pause", "resume"):
        SET["paused"] = kind == "pause"
        edit(chat, mid, *settings_view())
    elif kind == "reset":
        STATE["sent"] = []
        edit(chat, mid, settings_view()[0] + "\n\n♻️ История очищена.", settings_view()[1])
    elif kind == "test":
        run_test(chat)
    elif kind == "close":
        tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup=json.dumps(ikb([])))


def check_owner(uid, chat):
    if not owner_chat():
        STATE["owner"] = uid
        say("Вы назначены владельцем бота. Чтобы закрепить это, добавьте свой ID в секрет OWNER_ID (инструкция, шаг 5).", chat)
    if uid != str(owner_chat()):
        tg("sendMessage", chat_id=chat, text="Это личный бот.")
        return False
    return True


def poll_commands():
    """Забирает сообщения и нажатия кнопок, которые пришли боту с прошлого запуска."""
    j = tg("getUpdates", offset=STATE["offset"] + 1, timeout=0, allowed_updates='["message","callback_query"]')
    if not j.get("ok"):
        if j.get("error_code") == 409:
            log("Включён webhook (мгновенный режим): команды приходят через воркер")
        return
    for u in j.get("result", []):
        STATE["offset"] = max(STATE["offset"], u["update_id"])
        if "callback_query" in u:
            cq = u["callback_query"]
            uid = str(cq["from"]["id"])
            msg = cq.get("message") or {}
            chat = (msg.get("chat") or {}).get("id") or uid
            if check_owner(uid, chat):
                log("Кнопка:", cq.get("data"))
                handle_cb(cq.get("data", ""), chat, msg.get("message_id"), cq.get("id"))
            continue
        msg = u.get("message") or {}
        uid = str((msg.get("from") or {}).get("id", ""))
        chat = msg.get("chat", {}).get("id")
        text = msg.get("text", "")
        if not text or not chat:
            continue
        if check_owner(uid, chat):
            log("Сообщение:", text)
            handle(text, chat)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto", choices=["auto", "digest", "command", "test"])
    ap.add_argument("--cmd", default="")
    ap.add_argument("--lang", default="all")
    ap.add_argument("--count", default="")
    ap.add_argument("--video", default="false")
    a = ap.parse_args()
    if not TOKEN:
        sys.exit("Нет BOT_TOKEN: добавьте его в Settings -> Secrets and variables -> Actions (инструкция, шаг 5).")

    if a.mode == "auto":
        poll_commands()
        due = time.time() - STATE["last_digest"] >= SET["every_hours"] * 3600 - 600
        if owner_chat() and not SET["paused"] and due:
            STATE["last_digest"] = time.time()
            digest(owner_chat(), title="Подборка мемов")
        else:
            log("Подборка пока не нужна" if owner_chat() else "Владелец неизвестен: напишите боту /start")
    elif a.mode == "digest":
        if not owner_chat():
            sys.exit("Неизвестен владелец: добавьте секрет OWNER_ID или напишите боту /start и подождите 10 минут.")
        STATE["last_digest"] = time.time()
        digest(owner_chat(), int(a.count) if a.count.isdigit() else None,
               a.lang if a.lang in LANGS else "all", None, a.video == "true", title="Подборка по запросу")
    elif a.mode == "command":
        c = a.cmd.strip()
        if c.startswith("__cb__ "):                      # нажатие кнопки, переданное воркером
            _, mid, data = (c.split(" ", 2) + ["", ""])[:3]
            log("Кнопка из воркера:", data)
            handle_cb(data, owner_chat(), int(mid) if mid.isdigit() else None)
        elif c:
            log("Сообщение из воркера:", c)
            handle(c, owner_chat())
    elif a.mode == "test":
        run_test(owner_chat())
    save()


if __name__ == "__main__":
    main()
