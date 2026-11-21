"""Мем-радар для GitHub Actions: присылает в Telegram самые популярные мемы. Ваш аккаунт Telegram не нужен.

Источники:
  * публичные Telegram-каналы через открытую веб-версию https://t.me/s/<канал> (без входа);
  * Lemmy: открытая альтернатива Reddit, публичный API без ключей;
  * Reddit: если в секретах указаны REDDIT_CLIENT_ID и REDDIT_CLIENT_SECRET.

Кто чем управляет:
  * владелец (секрет OWNER_ID): источники, настройки, подписчики;
  * подписчики (любой, кто нажал /start): получают подборки и могут запросить их вручную.
  Данные подписчиков хранятся в state.json в зашифрованном виде (ключ получается из BOT_TOKEN).

Режимы (python memradar.py --mode ...):
  auto     по расписанию: обрабатывает сообщения из чата и, если пора, рассылает подборку всем
  digest   прислать подборку владельцу сейчас (кнопка Run workflow на GitHub)
  command  обработать одно сообщение или нажатие кнопки, переданное воркером (--cmd JSON)
  test     проверить источники: прислать владельцу отчёт
  merge    служебный: слить состояние с параллельным запуском перед git push
"""
import argparse
import base64
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken

from scoring import hot_scores, junk_reason, laugh_verdict, laugh_share, norm_emoji

BASE = Path(__file__).resolve().parent
SRC_FILE, STATE_FILE = BASE / "sources.json", BASE / "state.json"
TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER = os.getenv("OWNER_ID", "").strip()
REDDIT_ID = os.getenv("REDDIT_CLIENT_ID", "").strip()
REDDIT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "").strip()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
API = f"https://api.telegram.org/bot{TOKEN}"
LANGS = ("ru", "ua", "en")

HELP_OWNER = """<b>Мем-радар</b>: самые залетевшие мемы из Telegram, Lemmy и Reddit.

Всё управление — <b>кнопками внизу чата</b>:
🔥 <b>Топ за сутки</b> · ⚡ <b>Залетает сейчас</b> · 🎬 <b>Только видео</b>
📋 <b>Источники</b>: откуда берутся мемы
➕ <b>Добавить</b> / ➖ <b>Удалить</b> источник
⚙️ <b>Настройки</b>: как часто и сколько присылать, пауза
👥 <b>Подписчики</b>: кто получает подборки, блокировка
🌐 <b>Языки мемов</b>: какие присылать вам — 🇺🇦 / 🇷🇺 / 🇬🇧

Добавить канал можно и проще: пришлите ссылку на него (t.me/…) или @ник.
Друзьям отправьте ссылку на бота: после /start они будут получать подборки,
а источники и настройки меняете только вы."""

HELP_USER = """<b>Мем-радар</b> присылает самые залетевшие мемы из Telegram, Lemmy и Reddit.

📬 Подборка приходит автоматически каждые {every} ч.
🔥 <b>Топ за сутки</b>: лучшие мемы за 24 часа
⚡ <b>Залетает сейчас</b>: что набирает обороты за 4 часа
🎬 <b>Только видео</b>: для монтажа
🌐 <b>Языки мемов</b>: выберите, какие присылать — 🇺🇦 / 🇷🇺 / 🇬🇧
🔕 <b>Отписаться</b>: перестать получать подборки

Ответ на кнопку приходит за 1–2 минуты.{limit}"""


def user_help():
    m = SET["user_cooldown_min"]
    return HELP_USER.format(every=SET["every_hours"], limit=f"\nРучной запрос: не чаще раза в {m} мин." if m else "")


def log(*a):
    print(*a, flush=True)


def load(path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


# ---------------------------------------------------------------- шифрование данных подписчиков
FERNET = Fernet(base64.urlsafe_b64encode(hashlib.sha256(("memradar:" + TOKEN).encode()).digest())) if TOKEN else None


def decrypt(blob):
    if not blob or not FERNET:
        return {}
    try:
        return json.loads(FERNET.decrypt(blob.encode()))
    except (InvalidToken, ValueError):
        log("Не удалось расшифровать данные подписчиков (сменился BOT_TOKEN?), начинаю список заново")
        return {}


def encrypt(obj):
    return FERNET.encrypt(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).decode()


SRC = load(SRC_FILE, {})
STATE = load(STATE_FILE, {})
STATE.setdefault("sent", [])
STATE.setdefault("offset", 0)
STATE.setdefault("last_digest", 0)
SET = SRC.setdefault("settings", {})
for k, v in {"every_hours": 4, "count": 8, "max_per_source": 2, "window_hours": 24, "paused": False,
             "user_cooldown_min": 0, "laugh": "strict"}.items():
    SET.setdefault(k, v)
SRC.setdefault("telegram", {l: [] for l in LANGS})
SRC.setdefault("lemmy", [])
SRC.setdefault("reddit", [])
PRIV = decrypt(STATE.get("private"))
_PRIV_SNAPSHOT = json.dumps(PRIV, sort_keys=True, ensure_ascii=False)
PRIV.setdefault("subs", {})       # chat_id -> {"name", "since", "last", "active"}
PRIV.setdefault("banned", {})     # chat_id -> name
PRIV.setdefault("seen", {})       # chat_id -> [id мемов, которые уже получил вручную]
PRIV.setdefault("await", {})      # chat_id -> "add" (владелец ждёт ввода источника)
PRIV.setdefault("langs", {})      # chat_id -> ["ua", "ru", "en"]: какие мемы присылать
if STATE.get("owner"):            # старая версия хранила владельца открыто
    PRIV.setdefault("owner", STATE.pop("owner"))


def save():
    STATE["sent"] = STATE["sent"][-4000:]
    STATE["junk"] = STATE.get("junk", [])[-3000:]
    if len(STATE.get("recent", {})) > 400:
        STATE["recent"] = dict(list(STATE["recent"].items())[-400:])
    for k in list(PRIV["seen"]):
        PRIV["seen"][k] = PRIV["seen"][k][-500:]
    if FERNET and (json.dumps(PRIV, sort_keys=True, ensure_ascii=False) != _PRIV_SNAPSHOT or "private" not in STATE):
        STATE["private"] = encrypt(PRIV)
    SRC_FILE.write_text(json.dumps(SRC, ensure_ascii=False, indent=2) + "\n", "utf-8")
    STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False, indent=1) + "\n", "utf-8")


def owner_chat():
    return OWNER or PRIV.get("owner")


def user_langs(chat):
    got = [l for l in PRIV["langs"].get(str(chat), LANGS) if l in LANGS]
    return got or list(LANGS)


def is_owner(uid):
    return bool(owner_chat()) and str(uid) == str(owner_chat())


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
        say(text, chat, kb)


# ---------------------------------------------------------------- источники
STATS = {}


def parse_reactions(m):
    """[(emoji, count)] из t.me/s; платные звёзды пропускаем."""
    out = []
    for x in m.select(".tgme_reaction"):
        if "tgme_reaction_paid" in (x.get("class") or []):
            continue
        raw = x.get_text("", strip=True)
        cnt = parse_count(raw)
        emo = re.sub(r"[\d.,\sKMkm]+$", "", raw).strip()
        if not emo:                                        # картинка-эмодзи: код в имени файла
            el = x.select_one("[style*='emoji/']")
            h = re.search(r"emoji/\d+/([0-9A-F]+)\.png", el["style"]) if el else None
            if h:
                try:
                    emo = bytes.fromhex(h.group(1)).decode("utf-8", "ignore")
                except ValueError:
                    emo = ""
        out.append((norm_emoji(emo) or "?", cnt))
    return out


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
        if m.select_one(".tgme_widget_message_forwarded_from"):
            continue                                         # репост из другого канала: чаще новости или реклама
        text_el = m.select_one(".tgme_widget_message_text")
        text = text_el.get_text(" ", strip=True) if text_el else ""
        if len(text) > 400 or junk_reason(text):
            continue                                         # длинный текст = новость/пост, а не мем
        foreign = [a.get("href", "") for a in (text_el.select("a[href]") if text_el else [])
                   if re.search(r"t\.me/(?!" + re.escape(channel) + r"\b)", a.get("href", ""), re.I)]
        if foreign:
            continue                                         # ссылки на чужие каналы = взаимопиар
        kind, media = None, None
        thumb = None
        th = m.select_one(".tgme_widget_message_video_thumb[style]")
        if th:
            u = re.search(r"url\(['\"]?(.*?)['\"]?\)", th["style"])
            thumb = u.group(1) if u else None
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
        rlist = parse_reactions(m)
        if laugh_verdict(rlist, SET["laugh"]):
            STATS[channel] = STATS.get(channel, 0) + 1
            continue                                         # первая реакция не смех: не мем
        reacts = sum(c for _, c in rlist)
        posts.append({"src": "tg", "source": "@" + channel, "lang": lang, "id": f"tg:{channel}/{pid}",
                      "kind": kind, "media": media, "views": parse_count(v.get_text()), "reactions": reacts,
                      "forwards": 0, "ts": ts, "text": text[:300], "link": f"https://t.me/{channel}/{pid}",
                      "thumb": thumb, "laugh": laugh_share(rlist) if rlist else 0})
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
        if p.get("nsfw") or p.get("removed") or p.get("deleted") or junk_reason(p.get("name", "")):
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
        if d.get("over_18") or d.get("stickied") or junk_reason(d.get("title", "")):
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


def as_langs(lang):
    """'all' | 'ua' | ['ua','en'] -> множество языков."""
    if not lang or lang == "all":
        return set(LANGS)
    return {lang} if isinstance(lang, str) else set(lang)


def gather(langs, hours, report=None):
    """Собирает и оценивает посты из всех источников нужных языков."""
    pool = []
    for lg, chans in SRC["telegram"].items():
        if lg in langs:
            for ch in chans:
                ps = hot_scores(fetch_tg(ch, lg, hours), time.time())
                if report is not None:
                    report.append(f"@{ch}: {len(ps)}" + (f" (−{STATS[ch]} не смешно)" if STATS.get(ch) else ""))
                pool += ps
    if "en" in langs:
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
    return pool


def select(pool, langs, exclude=None, only_video=False):
    """Отбирает лучшее: нужные языки, без уже присланного, не больше max_per_source с одного источника."""
    ex = (exclude if exclude is not None else set(STATE["sent"])) | set(STATE.get("junk", []))
    items = [p for p in pool if p["lang"] in langs and p["id"] not in ex and (not only_video or p["kind"] == "video")]
    items.sort(key=lambda p: p["score"], reverse=True)
    per, top = {}, []
    for p in items:
        if per.get(p["source"], 0) >= SET["max_per_source"]:
            continue
        per[p["source"]] = per.get(p["source"], 0) + 1
        top.append(p)
    return top


def collect(lang="all", hours=24, only_video=False, report=None, exclude=None):
    langs = as_langs(lang)
    return select(gather(langs, hours, report), langs, exclude, only_video)


# ---------------------------------------------------------------- проверка текста на картинке (OCR)
MEDIA, OCR_CACHE, _OCR = {}, {}, {"ready": None}
OCR_LANGS = "ukr+rus+eng"


def fetch_media(url, limit):
    if url in MEDIA:
        return MEDIA[url]
    r = requests.get(url, headers={"User-Agent": UA}, timeout=90, stream=True)
    data = b""
    for chunk in r.iter_content(1 << 16):
        data += chunk
        if len(data) > limit:
            raise ValueError("файл слишком большой")
    MEDIA[url] = data
    return data


def ocr_ready():
    """Tesseract ставится только когда нужен (≈20 с), чтобы нажатия настроек не замедлялись."""
    if _OCR["ready"] is not None:
        return _OCR["ready"]
    ok = bool(shutil.which("tesseract"))
    if not ok and os.getenv("GITHUB_ACTIONS"):
        log("Ставлю распознавание текста (tesseract)…")
        try:
            subprocess.run("sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
                           "tesseract-ocr tesseract-ocr-ukr tesseract-ocr-rus >/dev/null", shell=True, timeout=240, check=True)
            ok = bool(shutil.which("tesseract"))
        except Exception as e:
            log("Не удалось поставить tesseract, проверяю только подписи:", e)
    _OCR["ready"] = ok
    return ok


def image_junk(p):
    """Читает текст на картинке (или на превью видео) и ищет рекламу, новости, 18+."""
    url = p.get("media") if p["kind"] == "photo" else p.get("thumb")
    if not url or not ocr_ready():
        return None
    if p["id"] in OCR_CACHE:
        return OCR_CACHE[p["id"]]
    try:
        data = fetch_media(url, 9 * 1024 * 1024)
        out = subprocess.run(["tesseract", "stdin", "stdout", "-l", OCR_LANGS, "--psm", "3"],
                             input=data, capture_output=True, timeout=40).stdout.decode("utf-8", "ignore")
    except Exception as e:
        log("OCR не получился:", p["link"], e)
        out = ""
    reason = junk_reason(" ".join(out.split()))
    OCR_CACHE[p["id"]] = reason
    return reason


def clean(top, n):
    """Берёт лучшие n постов, отбрасывая те, где на картинке реклама, новость или 18+."""
    out = []
    for p in top:
        if len(out) >= n:
            break
        why = image_junk(p)
        if why:
            log(f"Отсеян ({why}, текст на картинке):", p["link"])
            STATE.setdefault("junk", []).append(p["id"])
            continue
        out.append(p)
    return out


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


def file_id_of(res):
    if not isinstance(res, dict):
        return None
    for k in ("video", "animation", "document"):
        if res.get(k):
            return res[k].get("file_id")
    if res.get("photo"):
        return res["photo"][-1].get("file_id")
    return None


def send_post(chat, p, n, cache, kb=None):
    """Отправляет мем. cache[id] хранит file_id, чтобы не заливать один файл много раз.
    Возвращает 'ok', 'blocked' (пользователь заблокировал бота) или 'fail'."""
    cap = caption(p, n)
    method, field = ("sendVideo", "video") if p["kind"] == "video" else ("sendPhoto", "photo")
    extra = {"supports_streaming": "true"} if p["kind"] == "video" else {}
    if kb:
        extra["reply_markup"] = json.dumps(kb, ensure_ascii=False)
    j = None
    if cache.get(p["id"]):
        j = tg(method, chat_id=chat, caption=cap, parse_mode="HTML", **{field: cache[p["id"]]}, **extra)
    elif p.get("media"):
        try:
            data = fetch_media(p["media"], (49 if p["kind"] == "video" else 9) * 1024 * 1024)
            ext = ".mp4" if p["kind"] == "video" else ".jpg"
            j = tg(method, chat_id=chat, caption=cap, parse_mode="HTML", files={field: ("meme" + ext, data)}, **extra)
            fid = file_id_of(j.get("result")) if j.get("ok") else None
            if fid:
                cache[p["id"]] = fid
        except Exception as e:
            log("медиа не отправилось:", p["link"], e)
    if not (j and j.get("ok")):
        if j and j.get("error_code") == 403:
            return "blocked"
        j = tg("sendMessage", chat_id=chat, text=cap, parse_mode="HTML",       # ссылка: Telegram покажет превью
               **({"reply_markup": extra["reply_markup"]} if kb else {}))
    if j.get("error_code") == 403:
        return "blocked"
    return "ok" if j.get("ok") else "fail"


def recipients():
    out = [str(owner_chat())] if owner_chat() else []
    for cid, s in PRIV["subs"].items():
        if s.get("active", True) and cid not in PRIV["banned"] and cid not in out:
            out.append(cid)
    return out


def deliver(chats, top, n, title, more=True, cache=None):
    cache = {} if cache is None else cache
    for chat in chats:
        if say(f"🔥 <b>{title}</b> · {min(n, len(top))} шт.", chat).get("error_code") == 403:
            mark_blocked(chat)
            continue
        seen = PRIV["seen"].setdefault(str(chat), [])
        owner = is_owner(chat)
        for i, p in enumerate(top[:n], 1):
            kb = None
            if owner:                                       # владельцу — кнопка «не мем», чтобы чистить источники
                h = hashlib.md5(p["id"].encode()).hexdigest()[:10]
                STATE.setdefault("recent", {})[h] = {"id": p["id"], "src": p["source"]}
                kb = ikb([[btn("👎 Не мем", f"bad:{h}")]])
            res = send_post(chat, p, i, cache, kb)
            if res == "blocked":
                mark_blocked(chat)
                break
            if res == "ok":
                seen.append(p["id"])
            time.sleep(1.0 if len(cache) < i else 0.1)
        else:
            if more:
                say("Ещё подборку?", chat, MORE_KB)


def mark_blocked(chat):
    s = PRIV["subs"].get(str(chat))
    if s:
        s["active"] = False
        log("Подписчик заблокировал бота, отписываю:", chat)


def personal_digest(chat, n=None, lang=None, hours=None, only_video=False, title="Топ мемов"):
    """lang=None: языки из настроек пользователя; 'ua' и т.п.: разовый выбор (кнопки «Ещё»)."""
    n = n or SET["count"]
    hours = hours or SET["window_hours"]
    langs = as_langs(lang) if lang and lang != "all" else set(user_langs(chat))
    top = clean(collect(langs, hours, only_video, exclude=set(PRIV["seen"].get(str(chat), []))), n)
    if not top:
        return say("Свежих залётов пока нет, загляните чуть позже 🙂", chat)
    deliver([chat], top, n, title)


def broadcast(title="Подборка мемов"):
    chats = recipients()
    if not chats:
        return log("Некому слать: нет владельца и подписчиков")
    groups = {}
    for c in chats:                                   # одинаковый набор языков = одна подборка
        groups.setdefault(tuple(sorted(user_langs(c))), []).append(c)
    need = set().union(*map(set, groups))
    pool = gather(need, SET["window_hours"])
    n, cache, sent_now = SET["count"], {}, set()
    for langs, members in groups.items():
        top = clean(select(pool, set(langs)), n)
        if not top:
            log("Нет свежих мемов для", langs)
            continue
        sent_now |= {p["id"] for p in top[:n]}
        deliver(members, top, n, title + " · " + "/".join(FLAGS[l].split()[0] for l in LANGS if l in langs), cache=cache)
    STATE["sent"] += list(sent_now)
    log(f"Рассылка: групп по языкам {len(groups)}, получателей {len(chats)}")


# ---------------------------------------------------------------- кнопки
B_TOP, B_HOT, B_VIDEO = "🔥 Топ за сутки", "⚡ Залетает сейчас", "🎬 Только видео"
B_SRC, B_ADD, B_DEL = "📋 Источники", "➕ Добавить источник", "➖ Удалить источник"
B_SET, B_HELP, B_USERS = "⚙️ Настройки", "❓ Помощь", "👥 Подписчики"
B_UNSUB, B_SUB = "🔕 Отписаться", "🔔 Подписаться"
B_LANG = "🌐 Языки мемов"
OWNER_KB = {"keyboard": [[{"text": B_TOP}, {"text": B_HOT}], [{"text": B_VIDEO}, {"text": B_SRC}],
                         [{"text": B_ADD}, {"text": B_DEL}], [{"text": B_SET}, {"text": B_USERS}],
                         [{"text": B_LANG}, {"text": B_HELP}]],
            "resize_keyboard": True, "is_persistent": True,
            "input_field_placeholder": "Нажмите кнопку или пришлите ссылку на канал"}


def user_kb(chat):
    sub = PRIV["subs"].get(str(chat), {}).get("active", False)
    return {"keyboard": [[{"text": B_TOP}, {"text": B_HOT}], [{"text": B_VIDEO}, {"text": B_LANG}],
                         [{"text": B_HELP}, {"text": B_UNSUB if sub else B_SUB}]],
            "resize_keyboard": True, "is_persistent": True}


def kb_for(chat):
    return OWNER_KB if is_owner(chat) else user_kb(chat)


FLAGS = {"ru": "🇷🇺 RU", "ua": "🇺🇦 UA", "en": "🇬🇧 EN"}
OWNER_COMMANDS = [("start", "Меню и кнопки"), ("top", "Топ мемов за сутки"), ("hot", "Что залетает сейчас"),
                  ("video", "Только видео"), ("sources", "Источники"), ("add", "Добавить источник"),
                  ("del", "Удалить источник"), ("settings", "Настройки"), ("users", "Подписчики"),
                  ("lang", "Какие мемы присылать: UA/RU/EN"), ("help", "Помощь")]
USER_COMMANDS = [("start", "Подписаться и открыть меню"), ("top", "Топ мемов за сутки"), ("hot", "Что залетает сейчас"),
                 ("video", "Только видео"), ("lang", "Какие мемы присылать: UA/RU/EN"), ("stop", "Отписаться"), ("help", "Помощь")]


def btn(text, data):
    return {"text": text, "callback_data": data[:64]}


def ikb(rows):
    return {"inline_keyboard": rows}


MORE_KB = ikb([[btn("🇷🇺 RU", "top:ru"), btn("🇺🇦 UA", "top:ua"), btn("🇬🇧 EN", "top:en")],
               [btn("🎬 Видео", "video:all"), btn("⚡ Свежак", "hot:all")]])


LANG_NAMES = {"ua": "🇺🇦 Українські", "ru": "🇷🇺 Русские", "en": "🇬🇧 Английские"}


def lang_view(chat, note=""):
    cur = user_langs(chat)
    text = ((note + "\n\n") if note else "") + ("<b>🌐 Какие мемы присылать?</b>\nНажмите, чтобы включить или выключить. "
            "Это влияет и на подборки по расписанию, и на кнопки 🔥 ⚡ 🎬.\n\nСейчас: "
            + ", ".join(LANG_NAMES[l] for l in LANGS if l in cur))
    kb = ikb([[btn(("✅ " if l in cur else "▫️ ") + LANG_NAMES[l], f"lang:{l}")] for l in ("ua", "ru", "en")]
             + [[btn("Готово", "close")]])
    return text, kb


def norm_channel(s):
    s = s.strip().lstrip("@")
    s = re.sub(r"^(https?://)?(t\.me|telegram\.me)/(s/)?", "", s)
    return s.split("/")[0].split("?")[0]


def detect_source(text, loose=False):
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


LAUGH_MODES = {"strict": "😂 первая", "soft": "😂 мягко", "off": "😂 выкл"}


def settings_view():
    on = lambda cond, label: ("✅ " if cond else "") + label
    text = (f"<b>⚙️ Настройки</b>\nПодборка: каждые <b>{SET['every_hours']} ч</b>, по <b>{SET['count']}</b> мемов\n"
            f"Друзья могут запрашивать вручную: {'без ограничений' if not SET['user_cooldown_min'] else 'раз в ' + str(SET['user_cooldown_min']) + ' мин'}\n"
            f"Фильтр по реакциям: <b>{LAUGH_MODES[SET['laugh']]}</b>\n"
            f"Статус: {'⏸ на паузе' if SET['paused'] else '▶️ работает'}\n\nНажмите, чтобы изменить:")
    kb = ikb([
        [btn(on(SET["every_hours"] == h, f"каждые {h} ч" if h == 2 else f"{h} ч"), f"every:{h}") for h in (2, 4, 6)],
        [btn(on(SET["every_hours"] == h, f"{h} ч"), f"every:{h}") for h in (8, 12, 24)],
        [btn(on(SET["count"] == c, f"{c} мемов" if c == 5 else str(c)), f"count:{c}") for c in (5, 8, 10, 15)],
        [btn(on(SET["user_cooldown_min"] == m, "друзья: без лимита" if m == 0 else f"{m} мин"), f"cool:{m}") for m in (0, 2, 10)],
        [btn(on(SET["laugh"] == k, v), f"laugh:{k}") for k, v in LAUGH_MODES.items()],
        [btn("▶️ Включить рассылку", "resume") if SET["paused"] else btn("⏸ Пауза", "pause")],
        [btn("🧪 Проверить источники", "test"), btn("♻️ Забыть историю", "reset")],
    ])
    return text, kb


def users_view():
    active = {k: v for k, v in PRIV["subs"].items() if v.get("active", True) and k not in PRIV["banned"]}
    off = {k: v for k, v in PRIV["subs"].items() if not v.get("active", True) and k not in PRIV["banned"]}
    lines = [f"<b>👥 Подписчики: {len(active)}</b>"]
    lines += [f"• {html.escape(v.get('name', k))}" for k, v in active.items()] or ["пока никого: отправьте друзьям ссылку на бота"]
    if off:
        lines.append(f"\nОтписались: {len(off)}")
    if PRIV["banned"]:
        lines.append(f"Заблокированы: {len(PRIV['banned'])}")
    rows = [[btn(f"🚫 {v.get('name', k)[:28]}", f"ban:{k}")] for k, v in active.items()]
    rows += [[btn(f"✅ Разблокировать {name[:20]}", f"unban:{k}")] for k, name in PRIV["banned"].items()]
    return "\n".join(lines) + ("\n\nНажмите на человека, чтобы заблокировать." if active else ""), ikb(rows + [[btn("✅ Готово", "close")]])


def set_commands(chat, owner):
    cmds = OWNER_COMMANDS if owner else USER_COMMANDS
    tg("setMyCommands", commands=json.dumps([{"command": c, "description": d} for c, d in cmds], ensure_ascii=False),
       scope=json.dumps({"type": "chat", "chat_id": int(chat)}))


# ---------------------------------------------------------------- действия владельца с источниками
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
    say(f"✅ Добавил @{html.escape(ch)} в {FLAGS[lg]}" + (f"\nМедиапостов за неделю: {len(test)}" if test else ""), chat, OWNER_KB)


def bulk_add(chs, lg, chat):
    """Добавляет сразу много каналов: только живые (открытая веб-версия и медиапосты за последнюю неделю)."""
    say(f"⏳ Проверяю {len(chs)} каналов, это займёт 1–2 минуты…", chat) if len(chs) > 3 else None
    added, stale, closed, had, moved = [], [], [], [], []
    where = {c: l for l, cs in SRC["telegram"].items() for c in cs}
    for ch in dict.fromkeys(chs):
        if where.get(ch) == lg:
            had.append(ch)
            continue
        if ch in where:                                   # уже есть в другом разделе: переносим без проверки
            SRC["telegram"][where[ch]].remove(ch)
            SRC["telegram"].setdefault(lg, []).append(ch)
            moved.append(f"@{ch} ({where[ch].upper()} → {lg.upper()})")
            continue
        posts = fetch_tg(ch, lg, 24 * 7)
        if posts:
            SRC["telegram"].setdefault(lg, []).append(ch)
            added.append(f"@{ch} ({len(posts)})")
            continue
        try:
            page = requests.get(f"https://t.me/s/{ch}", headers={"User-Agent": UA}, timeout=30).text
        except Exception:
            page = ""
        (stale if "tgme_widget_message" in page else closed).append("@" + ch)
    lines = [f"<b>Готово: раздел {FLAGS[lg]}</b>"]
    if added:
        lines.append(f"✅ Добавлены ({len(added)}), в скобках медиапостов за неделю:\n" + ", ".join(added))
    if stale:
        lines.append(f"💤 Не добавлены, нет картинок и видео за неделю ({len(stale)}): " + ", ".join(stale))
    if closed:
        lines.append(f"🚫 Не добавлены, нет веб-версии или канал не существует ({len(closed)}): " + ", ".join(closed))
    if moved:
        lines.append(f"🔀 Перенесены ({len(moved)}): " + ", ".join(moved))
    if had:
        lines.append("Уже были в этом разделе: " + ", ".join("@" + c for c in had))
    say("\n\n".join(lines), chat, OWNER_KB)


def add_lemmy(c, chat):
    if not fetch_lemmy(c, 24 * 7):
        return say(f"😕 Не получилось прочитать {html.escape(c)}. Проверьте название в формате <code>сообщество@сервер</code>, "
                   "например <code>memes@lemmy.world</code>.", chat)
    if c not in SRC["lemmy"]:
        SRC["lemmy"].append(c)
    say(f"✅ Добавил Lemmy {html.escape(c)}", chat, OWNER_KB)


def add_reddit(x, chat):
    if x not in SRC["reddit"]:
        SRC["reddit"].append(x)
    say(f"✅ Добавил r/{html.escape(x)}" + ("" if REDDIT_ID else "\n⚠️ Reddit заработает, когда добавите ключи (инструкция, шаг 8)."), chat, OWNER_KB)


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
    PRIV["await"][str(chat)] = "add"
    say("➕ <b>Пришлите источник</b> одним сообщением:\n\n"
        "• Telegram-канал: ссылка <code>t.me/имя</code> или <code>@имя</code>\n"
        "• много каналов сразу: <code>/add @один @второй @третий ua</code>\n"
        "• Lemmy: <code>memes@lemmy.world</code>\n"
        "• Reddit: <code>r/memes</code>", chat,
        {"force_reply": True, "input_field_placeholder": "@канал, memes@lemmy.world или r/memes"})


def run_test(chat):
    rep = []
    top = collect("all", 24, report=rep, exclude=set())
    bad = [r for r in rep if r.split(" (")[0].endswith(": 0")]
    say("🧪 <b>Проверка источников</b> (медиапостов за 24 ч)\n" + "\n".join(rep)
        + f"\n\nВсего кандидатов: {len(top)}"
        + (f"\n😂 Отсеяно по реакциям: {sum(STATS.values())} (режим: {LAUGH_MODES[SET['laugh']]})" if STATS else "")
        + (f"\n⚠️ Пустые: {len(bad)}. Канал мог закрыть веб-версию или ничего не публиковать, такие можно удалить." if bad else ""), chat)


# ---------------------------------------------------------------- подписчики
def subscribe(chat, name):
    c = str(chat)
    s = PRIV["subs"].get(c)
    is_new = s is None
    PRIV["subs"][c] = {**(s or {"since": int(time.time()), "last": 0}), "name": name or c, "active": True}
    if is_new and owner_chat():
        say(f"👤 Новый подписчик: <b>{html.escape(name or c)}</b>\nВсего: {len(recipients()) - 1}", owner_chat(),
            ikb([[btn("🚫 Заблокировать", f"ban:{c}")]]))


def cooldown_left(chat):
    if is_owner(chat):
        return 0
    s = PRIV["subs"].get(str(chat), {})
    return max(0, int(s.get("last", 0) + SET["user_cooldown_min"] * 60 - time.time()))


def user_request(chat, fn):
    left = cooldown_left(chat)
    if left:
        return say(f"⏳ Следующую подборку можно запросить через {left // 60 + 1} мин. "
                   f"А по расписанию она придёт сама каждые {SET['every_hours']} ч.", chat)
    if not is_owner(chat):
        PRIV["subs"].setdefault(str(chat), {"since": int(time.time()), "name": str(chat), "active": False})["last"] = int(time.time())
    fn()


# ---------------------------------------------------------------- обработка сообщений и кнопок
SIMPLE = {B_TOP: "/top", B_HOT: "/hot", B_VIDEO: "/video", B_SRC: "/sources", B_ADD: "/add", B_DEL: "/del",
          B_SET: "/settings", B_HELP: "/help", B_USERS: "/users", B_UNSUB: "/stop", B_SUB: "/start", B_LANG: "/lang"}


def handle(text, chat, name=""):
    text = (text or "").strip()
    if not text:
        return
    chat = str(chat)
    if chat in PRIV["banned"]:
        return
    owner = is_owner(chat)
    text = SIMPLE.get(text, text)

    if not text.startswith("/"):
        if not owner:
            return say("Пользуйтесь кнопками внизу 👇", chat, user_kb(chat))
        waiting = PRIV["await"].pop(chat, None) == "add"
        src = detect_source(text, loose=waiting)
        if not src:
            return say("Не понял 🤔 Пользуйтесь кнопками внизу или пришлите ссылку на канал (t.me/…).", chat, OWNER_KB)
        typ, val = src
        return offer_tg(val, chat) if typ == "tg" else add_lemmy(val, chat) if typ == "lemmy" else add_reddit(val, chat)

    PRIV["await"].pop(chat, None)
    parts = text.split()
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    n, lang = None, "all"
    for x in args:
        if x.isdigit():
            n = max(1, min(int(x), 20))
        elif x.lower() in LANGS:
            lang = x.lower()

    # --- доступно всем
    if cmd == "/start":
        set_commands(chat, owner)
        if owner:
            return say(HELP_OWNER, chat, OWNER_KB)
        subscribe(chat, name)
        say("✅ Вы подписаны!\n\n" + user_help(), chat, user_kb(chat))
        t, k = lang_view(chat)
        return say(t, chat, k)
    if cmd == "/help":
        return say(HELP_OWNER if owner else user_help(), chat, kb_for(chat))
    if cmd == "/lang":
        if args:                                       # /lang ua en
            picked = [x.lower() for x in args if x.lower() in LANGS]
            if picked:
                PRIV["langs"][chat] = picked
        t, k = lang_view(chat)
        return say(t, chat, k)
    if cmd == "/top":
        return user_request(chat, lambda: personal_digest(chat, n, lang, 24))
    if cmd == "/hot":
        return user_request(chat, lambda: personal_digest(chat, n, lang, 4, title="Залетает прямо сейчас"))
    if cmd == "/video":
        return user_request(chat, lambda: personal_digest(chat, n, lang, 24, only_video=True, title="Топ видеомемов"))
    if cmd == "/stop" and not owner:
        if chat in PRIV["subs"]:
            PRIV["subs"][chat]["active"] = False
        return say("🔕 Вы отписались от рассылки. Вернуться: кнопка 🔔 Подписаться.", chat, user_kb(chat))
    if not owner:
        return say("Эта команда доступна только владельцу бота. Пользуйтесь кнопками внизу 👇", chat, user_kb(chat))

    # --- только владелец
    if cmd == "/sources":
        say(sources_text(), chat, ikb([[btn("➕ Добавить", "ask:add"), btn("➖ Удалить", "ask:del")]]))
    elif cmd == "/add":
        if not args:
            return ask_add(chat)
        tg_many = [detect_source(x, loose=True) for x in args if x.lower() not in LANGS]
        tg_many = [v for t_, v in (x for x in tg_many if x) if t_ == "tg"]
        if len(tg_many) > 1:
            lg = next((x.lower() for x in args if x.lower() in LANGS), None)
            if not lg:
                return say("В какой раздел добавить? Допишите в конце сообщения <code>ru</code>, <code>ua</code> или <code>en</code>, "
                           "например: <code>/add @один @второй ua</code>", chat)
            return bulk_add(tg_many, lg, chat)
        src = detect_source(args[0], loose=True)
        if not src:
            return say("Не понял, что добавить. Пример: <code>/add @канал ua</code>", chat)
        typ, val = src
        if typ == "tg":
            lg = next((x.lower() for x in args[1:] if x.lower() in LANGS), None)
            return add_tg(val, lg, chat) if lg else offer_tg(val, chat)
        return add_lemmy(val, chat) if typ == "lemmy" else add_reddit(val, chat)
    elif cmd == "/del":
        if args:
            src = detect_source(args[0], loose=True)
            typ, val = src if src else ("tg", norm_channel(args[0]))
            ok = remove_source({"tg": "tg", "lemmy": "lm", "reddit": "rd"}[typ], val)
            return say(f"🗑 Убрал {html.escape(val)}" if ok else f"{html.escape(val)} нет в списке", chat)
        say("➖ Нажмите на источник, чтобы удалить его:", chat, del_kb())
    elif cmd == "/settings":
        t, k = settings_view()
        say(t, chat, k)
    elif cmd == "/users":
        t, k = users_view()
        say(t, chat, k)
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
        PRIV["seen"] = {}
        say("♻️ Забыл историю: мемы могут повторяться.", chat)
    else:
        say("Не знаю такой команды 🤔 Пользуйтесь кнопками внизу.", chat, OWNER_KB)


def handle_cb(data, chat, mid, cb_id=None):
    chat = str(chat)
    if cb_id:
        tg("answerCallbackQuery", callback_query_id=cb_id)
    if chat in PRIV["banned"]:
        return
    kind, _, rest = data.partition(":")
    if kind in ("top", "hot", "video"):
        lang = rest if rest in LANGS else "all"
        if kind == "top":
            return user_request(chat, lambda: personal_digest(chat, None, lang, 24, title=f"Топ мемов {FLAGS.get(lang, '')}".strip()))
        if kind == "hot":
            return user_request(chat, lambda: personal_digest(chat, None, lang, 4, title="Залетает прямо сейчас"))
        return user_request(chat, lambda: personal_digest(chat, None, lang, 24, only_video=True, title="Топ видеомемов"))
    if kind == "close":
        return tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup=json.dumps(ikb([])))
    if kind == "lang" and rest in LANGS:
        cur = user_langs(chat)
        if rest in cur and len(cur) == 1:
            return edit(chat, mid, *lang_view(chat, "⚠️ Хотя бы один язык должен остаться включённым."))
        new = [l for l in cur if l != rest] if rest in cur else cur + [rest]
        PRIV["langs"][chat] = [l for l in LANGS if l in new]
        return edit(chat, mid, *lang_view(chat))
    if not is_owner(chat):
        return

    if kind == "bad":
        info = STATE.get("recent", {}).get(rest)
        tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup=json.dumps(ikb([])))
        if not info:
            return say("Этот пост уже слишком старый, отметить не получится.", chat)
        STATE.setdefault("junk", []).append(info["id"])
        marks = SRC.setdefault("bad_marks", {})
        marks[info["src"]] = marks.get(info["src"], 0) + 1
        src = info["src"]
        if src.startswith("@"):
            data = f"del:tg:{src[1:]}"
        elif src.startswith("r/"):
            data = f"del:rd:{src[2:]}"
        else:
            data = f"del:lm:{src}"
        return say(f"👌 Учту. У {html.escape(src)} отмечено «не мем»: {marks[src]} раз.\n"
                   "Если от этого источника часто приходит не то, его лучше удалить.", chat,
                   ikb([[btn(f"🗑 Удалить {src}"[:40], data), btn("Оставить", "close")]]))
    if kind == "ask":
        ask_add(chat) if rest == "add" else say("➖ Нажмите на источник, чтобы удалить его:", chat, del_kb())
    elif kind == "add":
        typ, lg, val = (rest.split(":", 2) + ["", ""])[:3]
        if typ == "tg" and lg in LANGS and val:
            tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup=json.dumps(ikb([])))
            add_tg(val, lg, chat)
    elif kind == "del":
        typ, _, val = rest.partition(":")
        ok = remove_source(typ, val)
        label = ("@" + val) if typ == "tg" else ("r/" + val) if typ == "rd" else val
        edit(chat, mid, (f"🗑 Удалил {html.escape(label)}\n\n" if ok else "Этого источника уже нет.\n\n")
             + "➖ Нажмите на источник, чтобы удалить его:", del_kb())
    elif kind in ("every", "count", "cool") and rest.isdigit():
        SET[{"every": "every_hours", "count": "count", "cool": "user_cooldown_min"}[kind]] = int(rest)
        edit(chat, mid, *settings_view())
    elif kind == "laugh" and rest in LAUGH_MODES:
        SET["laugh"] = rest
        edit(chat, mid, *settings_view())
    elif kind in ("pause", "resume"):
        SET["paused"] = kind == "pause"
        edit(chat, mid, *settings_view())
    elif kind == "reset":
        STATE["sent"] = []
        PRIV["seen"] = {}
        t, k = settings_view()
        edit(chat, mid, t + "\n\n♻️ История очищена.", k)
    elif kind == "test":
        run_test(chat)
    elif kind == "ban" and rest and not is_owner(rest):
        name = PRIV["subs"].get(rest, {}).get("name", rest)
        PRIV["banned"][rest] = name
        if rest in PRIV["subs"]:
            PRIV["subs"][rest]["active"] = False
        t, k = users_view()
        edit(chat, mid, f"🚫 {html.escape(name)} заблокирован.\n\n" + t, k)
    elif kind == "unban" and rest:
        name = PRIV["banned"].pop(rest, rest)
        t, k = users_view()
        edit(chat, mid, f"✅ {html.escape(name)} разблокирован (подписаться снова он может через /start).\n\n" + t, k)


def display_name(frm):
    name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x).strip()
    if frm.get("username"):
        name = f"{name} (@{frm['username']})".strip()
    return name or str(frm.get("id", ""))


def ensure_owner(uid, chat):
    """Если секрет OWNER_ID не задан, владельцем становится первый, кто напишет боту."""
    if not owner_chat():
        PRIV["owner"] = str(uid)
        say("Вы назначены владельцем бота. Чтобы закрепить это, добавьте свой ID в секрет OWNER_ID (инструкция, шаг 5).", chat)


def poll_commands():
    """Забирает сообщения и нажатия кнопок, пришедшие с прошлого запуска (когда мгновенный режим не включён)."""
    j = tg("getUpdates", offset=STATE["offset"] + 1, timeout=0, allowed_updates='["message","callback_query"]')
    if not j.get("ok"):
        if j.get("error_code") == 409:
            log("Включён webhook (мгновенный режим): сообщения приходят через воркер")
        return
    for u in j.get("result", []):
        STATE["offset"] = max(STATE["offset"], u["update_id"])
        if "callback_query" in u:
            cq = u["callback_query"]
            msg = cq.get("message") or {}
            chat = (msg.get("chat") or {}).get("id") or cq["from"]["id"]
            ensure_owner(cq["from"]["id"], chat)
            log("Кнопка:", cq.get("data"))
            handle_cb(cq.get("data", ""), chat, msg.get("message_id"), cq.get("id"))
            continue
        msg = u.get("message") or {}
        if (msg.get("chat") or {}).get("type") != "private" or not msg.get("text"):
            continue
        frm = msg.get("from") or {}
        ensure_owner(frm.get("id"), msg["chat"]["id"])
        log("Сообщение:", msg["text"][:50])
        handle(msg["text"], msg["chat"]["id"], display_name(frm))


# ---------------------------------------------------------------- слияние состояния параллельных запусков
def merge3(base, ours, theirs):
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if isinstance(ours, dict) and isinstance(theirs, dict):
        b = base if isinstance(base, dict) else {}
        out = {}
        for k in list(dict.fromkeys(list(theirs) + list(ours))):
            if k in ours and k in theirs:
                out[k] = merge3(b.get(k), ours[k], theirs[k])
            elif k in ours:
                if not (k in b and b[k] == ours[k]):
                    out[k] = ours[k]
            elif not (k in b and b[k] == theirs[k]):
                out[k] = theirs[k]
        return out
    if isinstance(ours, list) and isinstance(theirs, list):
        b = base if isinstance(base, list) else []
        removed = [x for x in b if x not in ours or x not in theirs]
        out = [x for x in theirs if x not in removed]
        out += [x for x in ours if x not in out and x not in removed]
        return out
    if isinstance(ours, (int, float)) and isinstance(theirs, (int, float)) and not isinstance(ours, bool):
        return max(ours, theirs)                     # offset, last_digest, last: берём более позднее
    return ours


def git_json(rev, path):
    try:
        return json.loads(subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True, text=True, check=True).stdout)
    except Exception:
        return {}


def merge_mode():
    """Наш коммит (HEAD) не запушился, потому что другой запуск успел раньше: сливаем HEAD~1 (база), HEAD и origin/main."""
    for path in ("sources.json", "state.json"):
        base, ours, theirs = git_json("HEAD~1", path), git_json("HEAD", path), git_json("origin/main", path)
        if path == "state.json":
            for d in (base, ours, theirs):
                d["private"] = decrypt(d.get("private"))
        merged = merge3(base, ours, theirs)
        if path == "state.json":
            merged["private"] = encrypt(merged.get("private") or {})
        (BASE / path).write_text(json.dumps(merged, ensure_ascii=False, indent=2 if path == "sources.json" else 1) + "\n", "utf-8")
    log("Состояние слито с параллельным запуском")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto", choices=["auto", "digest", "command", "test", "merge"])
    ap.add_argument("--cmd", default="")
    ap.add_argument("--lang", default="all")
    ap.add_argument("--count", default="")
    ap.add_argument("--video", default="false")
    a = ap.parse_args()
    if not TOKEN:
        sys.exit("Нет BOT_TOKEN: добавьте его в Settings -> Secrets and variables -> Actions (инструкция, шаг 5).")
    if a.mode == "merge":
        return merge_mode()

    if a.mode == "auto":
        poll_commands()
        due = time.time() - STATE["last_digest"] >= SET["every_hours"] * 3600 - 600
        if not SET["paused"] and due and recipients():
            STATE["last_digest"] = time.time()
            broadcast()
        else:
            log("Подборка пока не нужна")
    elif a.mode == "digest":
        if not owner_chat():
            sys.exit("Неизвестен владелец: добавьте секрет OWNER_ID.")
        personal_digest(owner_chat(), int(a.count) if a.count.isdigit() else None,
                        a.lang if a.lang in LANGS else "all", None, a.video == "true", title="Подборка по запросу")
    elif a.mode == "command":
        c = a.cmd.strip()
        if c.startswith("{"):                              # формат воркера: JSON
            try:
                d = json.loads(c)
            except ValueError:
                d = {}
            chat = str(d.get("c") or d.get("u") or "")
            if chat:
                ensure_owner(d.get("u"), chat)
                if d.get("cb") is not None:
                    log("Кнопка:", d["cb"])
                    handle_cb(d["cb"], chat, d.get("mid"))
                else:
                    log("Сообщение:", (d.get("t") or "")[:50])
                    handle(d.get("t", ""), chat, d.get("n", ""))
        elif c.startswith("__cb__ ") and owner_chat():     # старый формат воркера
            _, mid, data = (c.split(" ", 2) + ["", ""])[:3]
            handle_cb(data, owner_chat(), int(mid) if mid.isdigit() else None)
        elif c and owner_chat():
            handle(c, owner_chat())
    elif a.mode == "test":
        run_test(owner_chat())
    save()


if __name__ == "__main__":
    main()
