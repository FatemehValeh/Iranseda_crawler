#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iranseda_crawler.py — download MP3 episodes from radio.iranseda.ir programs.

    python iranseda_crawler.py                       # reads input.txt
    python iranseda_crawler.py -i myprograms.txt
    python iranseda_crawler.py "http://radio.iranseda.ir/Program/?VALID=TRUE&ch=29&m=170128"

input.txt holds one URL per line ('#' starts a comment, blank lines ignored).
Program pages and single episode pages (epgarchivePart) are both accepted.

Files land in ./<latin-slug>/ , e.g. ./radio-goftego/ , taken from the channel's
own slug on the page (picturebymmc?chn=radio-goftego). The CSV keeps the Persian
channel name. Only files that download completely and verify are written to the
CSV; anything truncated stays as a .part file and is retried on the next run.

Requirements:  pip install requests beautifulsoup4
"""

import argparse
import csv
import datetime
import random
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse, unquote

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests beautifulsoup4")
try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Run:  pip install beautifulsoup4")

for _s in ("stdout", "stderr"):
    try:
        getattr(sys, _s).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REFERER = "http://radio.iranseda.ir/"

CHANNEL_SLUG = {"29": "radio-goftego"}         # ch id -> folder name, if ever needed

# Folder slug -> the name written to the CSV. The slug comes off the page itself
# (picturebymmc?chn=...), so this is the reliable key. Add your own lines here.
SLUG_FA = {
    "radio-iran": "رادیو ایران",
    "radio-payam": "رادیو پیام",
    "radio-javan": "رادیو جوان",
    "radio-farhang": "رادیو فرهنگ",
    "radio-maaref": "رادیو معارف",
    "radio-quran": "رادیو قرآن",
    "radio-varzesh": "رادیو ورزش",
    "radio-salamat": "رادیو سلامت",
    "radio-eghtesad": "رادیو اقتصاد",
    "radio-goftego": "رادیو گفت و گو",
    "radio-namayesh": "رادیو نمایش",
    "radio-saba": "رادیو صبا",
    "radio-tehran": "رادیو تهران",
    "radio-ava": "رادیو آوا",
    "radio-nama": "رادیو نما",
}

# the site is called ایران صدا — that is never the name of a channel
SITE_NAMES = ("ایران صدا", "ایرانصدا", "iranseda", "صدای جمهوری اسلامی ایران",
              "پخش زنده", "رادیو اینترنتی", "صفحه اصلی", "خانه")

FA_MONTHS = {"فروردین": 1, "اردیبهشت": 2, "خرداد": 3, "تیر": 4, "مرداد": 5,
             "امرداد": 5, "شهریور": 6, "مهر": 7, "آبان": 8, "ابان": 8,
             "آذر": 9, "اذر": 9, "دی": 10, "بهمن": 11, "اسفند": 12}

SLUG_RE = re.compile(r"chn=([A-Za-z0-9._-]+)")
DL_ABS_RE = re.compile(r"""https?://[^\s"'<>\\()]*?DLFile[^\s"'<>\\()]*?vid=([A-Za-z0-9_\-]+)""", re.I)
DL_REL_RE = re.compile(r"""["'](/[^\s"'<>\\]*?DLFile[^\s"'<>\\]*?vid=([A-Za-z0-9_\-]+))["']""", re.I)
NAME_DATE_RE = re.compile(r"(\d{2})_(\d{2})_(\d{2})-(\d{2})_(\d{2})")

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

AUDIO_WORDS = ("mp3", "صوت", "شنیدن", "audio", "mpeg", "m4a")
VIDEO_WORDS = ("mp4", "تصوير", "تصویر", "ویدئو", "ويدئو", "فیلم", "video", "flv")

CSV_FIELDS = ["file_name", "program_name", "episode_name", "channel",
              "link", "episode_link",
              "date_of_program", "duration", "download_date"]   # duration in minutes
MIN_BYTES = 8192
SUFFIX_KIND = {}

# ASP.NET "yellow screen of death" and friends, served with either 500 or 200
ERROR_MARKERS = (
    "an application error occurred on the server",
    "server error in '/'",
    "runtime error",
    "customerrors",
    "<title>error",
    "service unavailable",
)
RETRIES = 4          # set from --retries
BASE_DELAY = 2.0     # set from --delay


def looks_like_error_page(text):
    t = (text or "")[:4000].lower()
    return any(m in t for m in ERROR_MARKERS)


def pace(seconds=None):
    """Sleep with a little jitter so requests are not perfectly periodic."""
    s = BASE_DELAY if seconds is None else seconds
    if s > 0:
        time.sleep(s * random.uniform(0.8, 1.4))


def backoff(attempt, why=""):
    wait = min(BASE_DELAY * (3 ** attempt), 120)
    if why:
        print(f"  · {why} — waiting {wait:.0f}s")
    time.sleep(wait * random.uniform(0.9, 1.2))


def norm_digits(s):
    return (s or "").translate(PERSIAN_DIGITS)


def fa_norm(s):
    return (s or "").replace("\u064a", "\u06cc").replace("\u0643", "\u06a9") \
                    .replace("\u200c", " ").replace("\u200f", "")


def clean_text(s):
    return re.sub(r"\s+", " ", (s or "")).strip().strip(" -–—|·:،,")


def safe_name(s):
    return ILLEGAL_FS.sub("_", clean_text(s)).rstrip(". ")


def slugify(s, fallback):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (s or "")).strip("-.")
    return s.lower() or fallback


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------

def jalali_to_gregorian(jy, jm, jd):
    jy += 1595
    days = -355668 + (365 * jy) + ((jy // 33) * 8) + (((jy % 33) + 3) // 4) + jd
    days += (jm - 1) * 31 if jm < 7 else ((jm - 7) * 30) + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    months = [0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0:
        months[2] = 29
    gm = 0
    while gm < 13 and gd > months[gm]:
        gd -= months[gm]
        gm += 1
    return gy, gm, gd


def format_date(parts, gregorian=False):
    if not parts:
        return ""
    jy, jm, jd, hh, mi = parts
    if gregorian:
        try:
            gy, gm, gd = jalali_to_gregorian(jy, jm, jd)
            return f"{gy:04d}-{gm:02d}-{gd:02d} {hh:02d}:{mi:02d}"
        except Exception:
            pass
    return f"{jy:04d}/{jm:02d}/{jd:02d} {hh:02d}:{mi:02d}"


def parts_from_filename(fname):
    m = NAME_DATE_RE.search(fname or "")
    if not m:
        return None
    yy, mm, dd, hh, mi = (int(x) for x in m.groups())
    return (1400 + yy if yy < 50 else 1300 + yy), mm, dd, hh, mi


def parts_from_dt_title(node):
    if node is None:
        return None
    text = norm_digits(fa_norm(clean_text(node.get_text(" "))))
    month = None
    for name in sorted(FA_MONTHS, key=len, reverse=True):
        if name in text:
            month = FA_MONTHS[name]
            break
    ym = re.search(r"\b(1[34]\d{2})\b", text)
    if month is None or not ym:
        return None
    day = None
    h2 = node.find("h2")
    if h2:
        d = norm_digits(clean_text(h2.get_text()))
        if d.isdigit():
            day = int(d)
    if day is None:
        for n in re.findall(r"\b(\d{1,2})\b", text):
            if 1 <= int(n) <= 31:
                day = int(n)
                break
    if day is None:
        return None
    tm = re.search(r"(\d{1,2}):(\d{2})", text)
    hh, mi = (int(tm.group(1)), int(tm.group(2))) if tm else (0, 0)
    return int(ym.group(1)), month, day, hh, mi


# --------------------------------------------------------------------------
# media duration (pure python, no external tools)
# --------------------------------------------------------------------------

# bitrate tables in kbps, indexed by (mpeg group, layer group)
_BITRATES = {
    (1, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (1, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (1, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (2, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_RATES = {1: [44100, 48000, 32000], 2: [22050, 24000, 16000], 25: [11025, 12000, 8000]}


def _frame_at(b, i):
    """Decode an MPEG audio frame header at b[i:i+4], or None."""
    if i + 4 > len(b) or b[i] != 0xFF or (b[i + 1] & 0xE0) != 0xE0:
        return None
    vbits, lbits = (b[i + 1] >> 3) & 3, (b[i + 1] >> 1) & 3
    if vbits == 1 or lbits == 0:
        return None
    ver = {0: 25, 2: 2, 3: 1}[vbits]
    layer = {1: 3, 2: 2, 3: 1}[lbits]
    br_idx, sr_idx = (b[i + 2] >> 4) & 0xF, (b[i + 2] >> 2) & 3
    pad = (b[i + 2] >> 1) & 1
    if br_idx in (0, 15) or sr_idx == 3:
        return None
    bitrate = (_BITRATES[(1, layer)] if ver == 1
               else _BITRATES[(2, 1 if layer == 1 else 2)])[br_idx]
    rate = _RATES[ver][sr_idx]
    if layer == 1:
        length, samples = (12 * bitrate * 1000 // rate + pad) * 4, 384
    else:
        samples = 1152 if (layer == 2 or ver == 1) else 576
        mult = 72 if (layer == 3 and ver != 1) else 144
        length = mult * bitrate * 1000 // rate + pad
    if length < 8:
        return None
    return {"ver": ver, "bitrate": bitrate, "rate": rate, "length": length,
            "samples": samples, "mode": (b[i + 3] >> 6) & 3}


def mp3_duration(path):
    """Seconds, from a Xing/Info/VBRI frame count when present, else CBR maths."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(10)
        start = 0
        if head[:3] == b"ID3":
            start = 10 + ((head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14
                          | (head[8] & 0x7F) << 7 | (head[9] & 0x7F))
            if head[5] & 0x10:
                start += 10
        fh.seek(max(0, size - 128))
        end = size - 128 if fh.read(3) == b"TAG" else size      # drop ID3v1
        fh.seek(start)
        buf = fh.read(200000)

    for i in range(max(0, len(buf) - 4)):
        f = _frame_at(buf, i)
        if not f:
            continue
        nxt = i + f["length"]
        if nxt + 4 <= len(buf) and not _frame_at(buf, nxt):
            continue                                    # lone 0xFF, keep looking
        off = i + ((36 if f["mode"] != 3 else 21) if f["ver"] == 1
                   else (21 if f["mode"] != 3 else 13))
        frames = None
        if buf[off:off + 4] in (b"Xing", b"Info"):
            if int.from_bytes(buf[off + 4:off + 8], "big") & 1:
                frames = int.from_bytes(buf[off + 8:off + 12], "big")
        elif buf[i + 36:i + 40] == b"VBRI":
            frames = int.from_bytes(buf[i + 50:i + 54], "big")
        if frames:
            return frames * f["samples"] / f["rate"]
        return (end - start - i) * 8 / (f["bitrate"] * 1000)
    return None


def mp4_duration(path):
    """Seconds from the mvhd atom."""
    def scan(fh, stop):
        while fh.tell() < stop:
            pos = fh.tell()
            hdr = fh.read(8)
            if len(hdr) < 8:
                return None
            size, typ = int.from_bytes(hdr[:4], "big"), hdr[4:8]
            body = pos + 8
            if size == 1:
                size, body = int.from_bytes(fh.read(8), "big"), pos + 16
            elif size == 0:
                size = stop - pos
            if size < 8:
                return None
            if typ == b"moov":
                fh.seek(body)
                got = scan(fh, pos + size)
                if got:
                    return got
            elif typ == b"mvhd":
                fh.seek(body)
                d = fh.read(min(size - 8, 120))
                if d[0] == 1:
                    ts, dur = (int.from_bytes(d[20:24], "big"),
                               int.from_bytes(d[24:32], "big"))
                else:
                    ts, dur = (int.from_bytes(d[12:16], "big"),
                               int.from_bytes(d[16:20], "big"))
                return dur / ts if ts else None
            fh.seek(pos + size)
        return None

    with open(path, "rb") as fh:
        return scan(fh, os.path.getsize(path))


def duration_minutes(path):
    """Minutes as a 2-decimal string, or '' if it cannot be determined."""
    try:
        secs = (mp4_duration(path) if path.lower().endswith((".mp4", ".m4a"))
                else mp3_duration(path))
        return f"{secs / 60:.2f}" if secs and secs > 0 else ""
    except Exception:
        return ""


# --------------------------------------------------------------------------
# network
# --------------------------------------------------------------------------

def _quiet_tls(on):
    if on:
        try:
            import urllib3
            urllib3.disable_warnings()
        except Exception:
            pass


def make_session(insecure=False):
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fa,en-US;q=0.9,en;q=0.8",
        "Referer": REFERER,
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    })
    s.verify = not insecure
    _quiet_tls(insecure)
    return s


def get(session, url, tries=None, delay=None, timeout=45):
    tries = tries or RETRIES
    last = None
    for i in range(tries):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code >= 500 or r.status_code == 429:
                last = f"HTTP {r.status_code}"
                r.close()
                backoff(i, f"server said {last}")
                continue
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"
            if looks_like_error_page(r.text):
                last = "application error page"
                backoff(i, "server returned an error page")
                continue
            return r
        except requests.exceptions.SSLError as e:
            last = e
            if session.verify:
                print("  (TLS verification failed — continuing without it)")
                session.verify = False
                _quiet_tls(True)
        except Exception as e:
            last = e
            backoff(i)
    print(f"  ! gave up on {url}  ({last})", file=sys.stderr)
    return None


def refresh_session(old, insecure):
    """Drop the connection pool and cookies, then warm up a new session."""
    keep_insecure = insecure or not getattr(old, "verify", True)
    try:
        old.close()
    except Exception:
        pass
    s = make_session(keep_insecure)
    try:
        s.get(REFERER, timeout=30)
    except Exception:
        pass
    return s


def cd_filename(header):
    if not header:
        return ""
    m = re.search(r"filename\*\s*=\s*([\w\-]*)'[^']*'([^;]+)", header, re.I)
    if m:
        return unquote(m.group(2).strip().strip('"'),
                       encoding=m.group(1) or "utf-8", errors="replace")
    m = (re.search(r'filename\s*=\s*"([^"]+)"', header, re.I)
         or re.search(r"filename\s*=\s*([^;]+)", header, re.I))
    return unquote(m.group(1).strip().strip('"')) if m else ""


def kind_of(content_type, filename):
    ct, fn = (content_type or "").lower(), (filename or "").lower()
    if ct.startswith("audio") or "mpeg" in ct or fn.endswith((".mp3", ".m4a", ".ogg")):
        return "audio"
    if ct.startswith("video") or fn.endswith((".mp4", ".flv", ".ts", ".mkv")):
        return "video"
    return "unknown"


def label_kind(text):
    t = norm_digits(fa_norm((text or "").lower()))
    if any(w in t for w in VIDEO_WORDS):
        return "video"
    if any(w in t for w in AUDIO_WORDS):
        return "audio"
    return "unknown"


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def soup_of(html):
    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return BeautifulSoup(html, "html.parser")


def scrape_channel_name(html):
    """Look for the channel's own name on the page, ignoring the site name."""
    s = soup_of(html)
    raw = []
    for tag in s.select("meta[property='og:title'], meta[name='keywords']"):
        if tag.get("content"):
            raw.append(tag["content"])
    for sel in (".channel-name", ".channelName", "#channelName", ".breadcrumb a",
                ".logo img[alt]", "header a[title]", "a[href*='/ch/']", "h1", "title"):
        for t in s.select(sel):
            raw.append(t.get("alt") or t.get("title") or t.get_text(" "))
    for text in raw:
        for piece in re.split(r"[|/><»«\n]", clean_text(text)):
            piece = clean_text(piece)
            if not piece or len(piece) > 40:
                continue
            low = fa_norm(piece).lower()
            if any(b.lower() in low for b in SITE_NAMES):
                continue
            if "رادیو" in piece or "شبکه" in piece:
                return piece
    return ""


def channel_names(html, url):
    """(display name for the CSV, latin slug for the folder)"""
    ch = parse_qs(urlparse(url).query).get("ch", [""])[0]

    slug = CHANNEL_SLUG.get(ch, "")
    if not slug:
        m = SLUG_RE.search(html or "")            # picturebymmc?chn=radio-eghtesad
        slug = slugify(m.group(1), "") if m else ""
    if not slug:
        slug = f"channel-{ch}" if ch else "downloads"

    # the slug is the reliable key, so prefer the table; scraping is the backup
    fa = SLUG_FA.get(slug) or scrape_channel_name(html)
    return (fa or slug), slug


def find_episode_links(html, base_url):
    s = soup_of(html)
    anchors = s.select("a[href*='epgarchivePart']")
    if not anchors:
        anchors = [a for a in s.find_all("a", href=True)
                   if "e=" in a["href"] and "epg" in a["href"].lower()]
    out, seen = [], set()
    for a in anchors:
        full = urljoin(base_url, a["href"].replace("&amp;", "&"))
        if full in seen:
            continue
        seen.add(full)
        node = a.select_one(".ott-name")
        title = clean_text(node.get_text(" ")) if node else ""
        if not title:
            syn = a.select_one(".ott-synopsis")
            title = clean_text(syn.get_text(" "))[:120] if syn else clean_text(a.get_text(" "))[:120]
        out.append({"url": full, "title": title,
                    "parts": parts_from_dt_title(a.select_one(".dt-title"))})
    return out


def episode_meta(html, url):
    """Title / date for a page opened directly (not reached via a listing)."""
    s = soup_of(html)
    title = ott_name(s)
    if not title:
        h1 = s.select_one("h1")
        title = clean_text(h1.get_text(" ")) if h1 else ""
    return {"url": url, "title": title, "parts": parts_from_dt_title(s.select_one(".dt-title"))}


def ott_name(html_or_soup):
    """<p class="ott-name ...">آسیب شناسی مصرف سیگار ...</p> -> that text"""
    s = html_or_soup if hasattr(html_or_soup, "select_one") else soup_of(html_or_soup)
    node = s.select_one(".ott-name")
    return clean_text(node.get_text(" ")) if node else ""


def page_title(html):
    """<title>برنامه 1035</title> -> 'برنامه 1035'"""
    s = soup_of(html)
    t = s.find("title")
    if not t:
        return ""
    txt = norm_digits(clean_text(t.get_text(" ")))
    return clean_text(txt.split("|")[0])


def find_downloads(html, base_url):
    s = soup_of(html)
    groups = {}

    def add(url, vid, kind="unknown"):
        g = groups.setdefault(vid, {"urls": [], "kind": "unknown"})
        if url not in g["urls"]:
            g["urls"].append(url)
        if g["kind"] == "unknown":
            g["kind"] = kind

    for a in s.select("div.download a[href], a[href*='DLFile']"):
        href = a.get("href", "").replace("&amp;", "&")
        if "DLFile" not in href and not href.lower().split("?")[0].endswith((".mp3", ".mp4")):
            continue
        full = urljoin(base_url, href)
        vid = parse_qs(urlparse(full).query).get("vid", [""])[0] or full
        kind = label_kind(" ".join([a.get_text(" "), a.get("title", ""), href]))
        if kind == "unknown" and a.parent is not None:
            kind = label_kind(a.parent.get_text(" "))
        add(full, vid, kind)

    text = html.replace("\\/", "/").replace("&amp;", "&")
    for m in DL_ABS_RE.finditer(text):
        add(m.group(0), m.group(1))
    for m in DL_REL_RE.finditer(text):
        add(urljoin(base_url, m.group(1)), m.group(2))

    for vid, g in groups.items():
        if g["kind"] == "unknown":
            g["kind"] = SUFFIX_KIND.get(vid.rsplit("_", 1)[-1], "unknown")
    return groups


PAGE_RE = re.compile(r"[?&](pn|page|pageindex)=(\d+)", re.I)


def detect_pagination(html, url):
    """(param, last_page) taken from the URL and from links on the page."""
    seen = {}
    for k, v in parse_qs(urlparse(url).query).items():
        if k.lower() in ("pn", "page", "pageindex") and v and norm_digits(v[0]).isdigit():
            seen[k] = max(seen.get(k, 0), int(norm_digits(v[0])))
    for m in PAGE_RE.finditer(norm_digits((html or "").replace("&amp;", "&"))):
        seen[m.group(1)] = max(seen.get(m.group(1), 0), int(m.group(2)))
    if not seen:
        return "", 0
    param = max(seen, key=lambda k: seen[k])       # the one with the highest page seen
    return param, seen[param]


def with_page(url, param, n):
    u = urlparse(url)
    q = parse_qs(u.query)
    q[param] = [str(n)]
    return urlunparse(u._replace(query=urlencode(q, doseq=True)))


# --------------------------------------------------------------------------
# downloading + verification
# --------------------------------------------------------------------------

def open_stream(session, url, resume_from=0, timeout=90, tries=None):
    """Open a download, retrying through server errors. HTML is never audio."""
    tries = tries or RETRIES
    for i in range(tries):
        headers = {"Referer": REFERER}
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"
        try:
            r = session.get(url, headers=headers, stream=True, timeout=timeout)
            if r.status_code in (200, 206):
                ct = (r.headers.get("Content-Type") or "").lower()
                if ct.startswith("text/") or "html" in ct or "xml" in ct:
                    body = ""
                    try:
                        body = r.text[:2000]
                    except Exception:
                        pass
                    r.close()
                    why = ("server returned its error page instead of the file"
                           if looks_like_error_page(body) else
                           "server returned a web page instead of the file")
                    backoff(i, why)
                    continue
                return r
            code = r.status_code
            r.close()
            if code >= 500 or code == 429:
                backoff(i, f"server said HTTP {code}")
                continue
            return None                      # 403/404: retrying will not help
        except requests.exceptions.SSLError:
            if session.verify:
                session.verify = False
                _quiet_tls(True)
                continue
            return None
        except Exception as e:
            backoff(i, f"connection problem ({type(e).__name__})")
    return None


def verify(path, expected_total):
    """True only for a complete, plausible media file."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return False, "missing"
    if size < MIN_BYTES:
        return False, f"too small ({size} B)"
    if expected_total and size != expected_total:
        return False, f"incomplete ({size}/{expected_total} B)"
    with open(path, "rb") as fh:
        head = fh.read(12)
    if head.lstrip()[:1] in (b"<", b"{"):
        return False, "server returned a web page, not audio"
    if expected_total:
        return True, ""
    ok = (head[:3] == b"ID3" or (head[:1] == b"\xff" and (head[1] & 0xE0) == 0xE0)
          or head[4:8] == b"ftyp" or head[:4] == b"RIFF" or head[:4] == b"OggS")
    return (True, "") if ok else (False, "unrecognised file header")


def download(session, url, dest, delay=0.5, first_response=None):
    """Stream to dest with resume. Returns True only if verified complete."""
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    r = first_response

    if have:
        if r is not None:
            r.close()
        r = open_stream(session, url, resume_from=have)
        if r is None:
            return False
        if r.status_code == 206:
            print(f"  ↻ resuming at {have/1048576:.1f} MB")
        else:
            have = 0                       # no range support, start over
    if r is None:
        r = open_stream(session, url)
        if r is None:
            return False

    total = int(r.headers.get("Content-Length") or 0) + have
    done = have
    try:
        with r, open(part, "ab" if have else "wb") as fh:
            for chunk in r.iter_content(262144):
                if not chunk:
                    continue
                fh.write(chunk)
                done += len(chunk)
                pct = f"{done * 100 // total:3d}%" if total else "    "
                print(f"\r  ↓ {os.path.basename(dest)}  {pct} {done/1048576:6.1f} MB",
                      end="", flush=True)
        print()
    except Exception as e:
        print(f"\n  ! transfer error: {e} (.part kept for resume)", file=sys.stderr)
        return False

    ok, why = verify(part, total)
    if not ok:
        print(f"  ! not saved: {why}", file=sys.stderr)
        if "web page" in why or "header" in why:
            os.remove(part)                # junk, no point resuming it
        return False
    os.replace(part, dest)
    pace(delay)
    return True


def option_kind(vid, group):
    """audio / video / unknown for one download option."""
    if group["kind"] != "unknown":
        return group["kind"]
    return SUFFIX_KIND.get(vid.rsplit("_", 1)[-1], "unknown")


def try_option(session, vid, group, folder, want, delay, store, seen_names):
    """
    Try each server for ONE download option.
      (file_name, url) -> got it
      "skip"           -> not wanted here (duplicate, or the wrong format)
      None             -> genuinely failed
    """
    for url in group["urls"]:
        prev = store["known"].get(url)
        if prev and os.path.exists(os.path.join(folder, prev.get("file_name", ""))):
            print(f"  = have {prev['file_name']}")
            store["used"].add(prev["file_name"])
            seen_names.add(prev["file_name"])
            return prev["file_name"], url

        r = open_stream(session, url)
        if r is None:
            print("  · that server did not deliver, trying the next one")
            continue
        fname = safe_name(cd_filename(r.headers.get("Content-Disposition")))
        actual = kind_of(r.headers.get("Content-Type"), fname)
        if actual != "unknown":
            SUFFIX_KIND[vid.rsplit("_", 1)[-1]] = actual
        if actual not in ("unknown", want):
            r.close()
            print(f"  · this one is {actual}, not {want}")
            return "skip"                   # its mirrors serve the same thing
        if fname and fname in seen_names:
            r.close()
            print(f"  · same file as one already taken ({fname})")
            return "skip"
        if not fname:
            fname = f"{safe_name(vid)}.{'mp4' if want == 'video' else 'mp3'}"
        stem, ext = os.path.splitext(fname)
        n = 2
        while fname in store["used"]:
            fname = f"{stem}-{n}{ext}"
            n += 1
        dest = os.path.join(folder, fname)
        if os.path.exists(dest) and os.path.getsize(dest) > MIN_BYTES:
            r.close()
            print(f"  = have {fname}")
            store["used"].add(fname)
            seen_names.add(fname)
            return fname, url
        if download(session, url, dest, delay, first_response=r):
            store["used"].add(fname)
            seen_names.add(fname)
            return fname, url
        print("  · that download did not complete, trying the next server")
    return None


def grab(session, groups, folder, want, delay, store, fallback=True):
    """
    Download every file of the preferred format on this episode page.

    An episode page can carry several separate recordings, and each recording is
    usually offered as mp3 and mp4 across several servers. Links are classified
    by their own label — "(.mp3)" / "(.mp4)" — and mirrors collapse because they
    share a vid. So the number of DISTINCT mp3 options is the number of files to
    fetch. Only if there is no mp3 at all does it fall back to mp4.

    (The vid number is deliberately not used to group: the mp3 and mp4 of one
    recording can have completely different vid numbers.)

    Returns [(file_name, url), ...].
    """
    other = "video" if want == "audio" else "audio"
    seen_names, results = set(), []

    for pass_no, kind in enumerate([want] + ([other] if fallback else [])):
        labelled = [(v, g) for v, g in groups.items() if option_kind(v, g) == kind]
        unlabelled = [(v, g) for v, g in groups.items() if option_kind(v, g) == "unknown"]
        if not labelled and not unlabelled:
            continue
        if pass_no and labelled:
            print(f"  · no {want} on this page — trying {kind}")

        total, fails = len(labelled), 0
        if total > 1:
            print(f"  {total} separate {kind} file(s) on this page")
        for i, (vid, g) in enumerate(labelled, 1):
            if total > 1:
                print(f"  -- item {i}/{total}")
            got = try_option(session, vid, g, folder, kind, delay, store, seen_names)
            if isinstance(got, tuple):
                results.append(got)
            elif got != "skip":
                fails += 1

        if not results:              # nothing labelled worked — probe the rest
            for vid, g in unlabelled:
                got = try_option(session, vid, g, folder, kind, delay, store, seen_names)
                if isinstance(got, tuple):
                    results.append(got)

        if results:
            if fails:
                print(f"  ! {fails} of {total} item(s) on this page could not be "
                      f"fetched — rerun with --recheck to retry", file=sys.stderr)
            return results
    return results


# --------------------------------------------------------------------------
# csv store (one per folder)
# --------------------------------------------------------------------------

def episode_key(url):
    """Identify an episode page by its ch/e ids, so a changed VALID= or a
    reordered query string still matches a previous run."""
    q = parse_qs(urlparse(url or "").query)
    e, ch = q.get("e", [""])[0], q.get("ch", [""])[0]
    return f"{ch}:{e}" if e else (url or "")


def now_stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def open_store(folder):
    path = os.path.join(folder, "episodes.csv")
    known = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                # keep old rows only if their file is still on disk
                if row.get("file_name") and os.path.exists(os.path.join(folder, row["file_name"])):
                    fp = os.path.join(folder, row["file_name"])
                    if not row.get("duration"):          # older CSV, or new column
                        row["duration"] = duration_minutes(fp)
                    if not row.get("download_date"):     # use when the file was written
                        try:
                            row["download_date"] = datetime.datetime.fromtimestamp(
                                os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
                        except OSError:
                            pass
                    known[row.get("link", "")] = row
    return {"folder": folder, "csv": path, "known": known, "rows": [],
            "used": {r["file_name"] for r in known.values()}}


def flush_store(store):
    merged = {r["link"]: r for r in store["known"].values()}
    for r in store["rows"]:
        merged[r["link"]] = r
    rows = sorted(merged.values(), key=lambda r: (r.get("date_of_program", ""),
                                                  r.get("file_name", "")))
    with open(store["csv"], "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows([{k: r.get(k, "") for k in CSV_FIELDS} for r in rows])
    return len(rows)


# --------------------------------------------------------------------------
# per-URL work
# --------------------------------------------------------------------------

def collect_episodes(session, start_url, args):
    """Walk a listing and all its pages. -> (episodes, fa_name, slug, program)"""
    episodes, seen_urls, fa, slug, program = [], set(), "", "", ""
    param = args.page_param
    cap = args.max_pages if args.max_pages > 0 else 500
    q = parse_qs(urlparse(start_url).query)
    page_no = 1
    if param and q.get(param) and norm_digits(q[param][0]).isdigit():
        page_no = int(norm_digits(q[param][0]))

    url, last, pre, walked = start_url, 0, None, 0
    while walked < cap:
        print(f"[page {page_no}] {url}")
        r = pre or get(session, url, delay=args.delay)
        pre = None
        if not r:
            break
        walked += 1
        if args.debug_dump and walked == 1:
            dbg = os.path.join(args.outdir, "debug_listing.html")
            open(dbg, "w", encoding="utf-8").write(r.text)
            print(f"  (saved {dbg})")
        if not slug:
            fa, slug = channel_names(r.text, url)
            program = page_title(r.text)          # <title>برنامه ارغنون</title>
            print(f"[channel] {fa}  ->  folder: {slug}")
            if program:
                print(f"[program] {program}")

        fresh = [e for e in find_episode_links(r.text, url) if e["url"] not in seen_urls]
        for e in fresh:
            seen_urls.add(e["url"])
        episodes.extend(fresh)
        print(f"  {len(fresh)} new episode(s), {len(episodes)} total")

        if walked == 1:
            found_param, last = detect_pagination(r.text, start_url)
            if not param and found_param:
                param = found_param
            if param and last > 1:
                print(f"  (pagination: {param}=1..{last})")
        if not fresh:                       # past the last page, or nothing here
            break
        page_no += 1
        if last and page_no > last:
            break
        if param:
            url = with_page(start_url, param, page_no)
        else:                               # no links to learn from — probe
            for cand in ("pn", "page"):     # last resort: page 1 had no pager
                probe = with_page(start_url, cand, page_no)
                pr = get(session, probe, delay=args.delay)
                if pr and any(e["url"] not in seen_urls
                              for e in find_episode_links(pr.text, probe)):
                    param, url, pre = cand, probe, pr
                    print(f"  (pagination parameter: {cand})")
                    break
            if not param:
                print("  (no further pages)")
                break
        time.sleep(args.delay)

    if walked >= cap:
        print(f"  (stopped at the --max-pages limit of {cap})")
    return episodes, fa, slug, program


def expand(path):
    """~/x, $VARS and relative paths -> absolute path."""
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))


def resolve_input(path):
    """Accept a file path or a directory holding input.txt. Returns None if absent."""
    p = expand(path)
    if os.path.isdir(p):
        p = os.path.join(p, "input.txt")
    return p if os.path.isfile(p) else None


def read_input(path):
    urls = []
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line.startswith(("http://", "https://")):
                urls.append(line)
            elif line:
                print(f"  (skipping non-URL line: {line[:60]})", file=sys.stderr)
    return urls


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Download MP3 episodes from radio.iranseda.ir program pages.")
    ap.add_argument("target", nargs="?",
                    help="a program/episode URL, or a path to a file of URLs")
    ap.add_argument("-i", "--input", default="input.txt", metavar="PATH",
                    help="file with one URL per line, or a directory holding "
                         "input.txt (default: ./input.txt)")
    ap.add_argument("-o", "--outdir", default=".", metavar="DIR",
                    help="where channel folders are created (default: current "
                         "directory); created if it does not exist")
    ap.add_argument("--flat", action="store_true",
                    help="save straight into --outdir, no channel subfolder")
    ap.add_argument("--folder", help="force the download folder name")
    ap.add_argument("--channel", help="force the channel name written to the CSV")
    ap.add_argument("--max-pages", type=int, default=0,
                    help="cap on listing pages per URL (0 = all, default)")
    ap.add_argument("--page-param", default="",
                    help="pagination query parameter (auto-detected: pn/page)")
    ap.add_argument("--delay", type=float, default=2.0,
                    help="seconds between requests (default 2; raise if the "
                         "server starts erroring)")
    ap.add_argument("--retries", type=int, default=4,
                    help="attempts per request, with exponential backoff")
    ap.add_argument("--cool-after", type=int, default=3, metavar="N",
                    help="pause after N consecutive failures")
    ap.add_argument("--cooldown", type=float, default=90, metavar="SEC",
                    help="how long that pause lasts")
    ap.add_argument("--limit", type=int, default=0, help="stop after N episodes per URL")
    ap.add_argument("--video", action="store_true",
                    help="prefer mp4, falling back to mp3")
    ap.add_argument("--no-fallback", action="store_true",
                    help="do not fall back to the other format when the "
                         "preferred one is missing")
    ap.add_argument("--recheck", action="store_true",
                    help="open every episode page even if the CSV says it is done")
    ap.add_argument("--gregorian", action="store_true")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--debug-dump", action="store_true")
    args = ap.parse_args()

    # ---- where the URLs come from ----------------------------------------
    src = None
    if args.target and args.target.startswith(("http://", "https://")):
        targets = [args.target]
    else:
        src = resolve_input(args.target or args.input)
        if not src:
            wanted = expand(args.target or args.input)
            ap.error(f"no URL given and no input file at {wanted}\n"
                     f"       create it (one URL per line) or pass -i /path/to/input.txt")
        targets = read_input(src)
        print(f"{len(targets)} URL(s) from {src}")
    if not targets:
        ap.error(f"{src} contains no URLs")

    # ---- where the audio goes --------------------------------------------
    args.outdir = expand(args.outdir)
    try:
        os.makedirs(args.outdir, exist_ok=True)
    except OSError as e:
        ap.error(f"cannot create output directory {args.outdir}: {e}")
    if not os.access(args.outdir, os.W_OK):
        ap.error(f"output directory is not writable: {args.outdir}")
    print(f"saving to {args.outdir}{'' if args.flat else os.sep + '<channel>' + os.sep}\n")

    global RETRIES, BASE_DELAY
    RETRIES, BASE_DELAY = args.retries, args.delay

    session = make_session(args.insecure)
    want = "video" if args.video else "audio"
    stores, saved, failed, skipped = {}, 0, 0, 0
    misses, stalls, abort = 0, 0, False

    for n, target in enumerate(targets, 1):
        if abort:
            print(f"\n(skipping the remaining {len(targets) - n + 1} URL(s))",
                  file=sys.stderr)
            break
        print(f"\n===== [{n}/{len(targets)}] {target}")
        if "epgarchivePart" in target:                      # a single episode URL
            r = get(session, target, delay=args.delay)
            if not r:
                failed += 1
                continue
            fa, slug = channel_names(r.text, target)
            program = page_title(r.text)
            episodes, pages = [episode_meta(r.text, target)], {target: r.text}
        else:
            try:
                episodes, fa, slug, program = collect_episodes(session, target, args)
            except Exception as e:
                print(f"  ! could not crawl this URL: {type(e).__name__}: {e}\n"
                      f"  … moving on to the next one", file=sys.stderr)
                failed += 1
                continue
            pages = {}
        if args.limit:
            episodes = episodes[:args.limit]
        if not episodes:
            print("  ! no episodes found", file=sys.stderr)
            failed += 1
            continue

        folder = (args.outdir if args.flat else
                  os.path.join(args.outdir, args.folder or slug or "downloads"))
        os.makedirs(folder, exist_ok=True)
        if folder not in stores:
            stores[folder] = open_store(folder)
        store = stores[folder]
        channel_label = args.channel or fa or slug

        # A channel can only broadcast one thing at a given date+time, so the
        # date shown on the listing identifies an episode uniquely. That lets us
        # recognise what we already have WITHOUT opening each episode page.
        # An episode page URL identifies an episode exactly, so prefer it.
        # Rows written before episode_link existed fall back to matching on the
        # broadcast date, scoped to this programme so that sibling programmes
        # sharing the channel folder cannot mask each other.
        have_by_episode, have_by_date = {}, {}
        for row in store["known"].values():
            el = (row.get("episode_link") or "").strip()
            if el:
                have_by_episode.setdefault(episode_key(el), []).append(row)
                continue
            if program and (row.get("program_name") or "") not in ("", program):
                continue
            d = (row.get("date_of_program") or "").strip()
            if d:
                have_by_date.setdefault(d, []).append(row)

        print(f"\n{len(episodes)} episode(s) -> {folder}\n")
        for i, ep in enumerate(episodes, 1):
            # both date formats, in case an earlier run used --gregorian
            done = have_by_episode.get(episode_key(ep["url"]))
            if done is None:                      # older CSV: match on date
                keys = ([format_date(ep["parts"], g) for g in (False, True)]
                        if ep["parts"] else [])
                done = next((have_by_date[k] for k in keys if k in have_by_date), None)
            if done and not args.recheck and all(
                    os.path.exists(os.path.join(folder, r.get("file_name", "")))
                    for r in done):
                skipped += 1
                if skipped == 1 or skipped % 25 == 0:
                    print(f"[{i}/{len(episodes)}] skipping {skipped} already-downloaded "
                          f"episode(s)…")
                continue

            print(f"[{i}/{len(episodes)}] {ep['title'][:70]}")
            html = pages.get(ep["url"])
            if html is None:
                er = get(session, ep["url"], delay=args.delay)
                pace(args.delay)
                if not er:
                    failed += 1
                    continue
                html = er.text
                if args.debug_dump and i == 1:
                    dbg = os.path.join(args.outdir, "debug_episode.html")
                    open(dbg, "w", encoding="utf-8").write(html)
                    print(f"  (saved {dbg})")
            groups = find_downloads(html, ep["url"])
            if not groups:
                print("  ! no download link on this page", file=sys.stderr)
                failed += 1
                continue
            files = grab(session, groups, folder, want, args.delay, store,
                         fallback=not args.no_fallback)
            if not files:
                print(f"  ! no complete {want} file — not added to the CSV", file=sys.stderr)
                failed += 1
                misses += 1
                if misses >= args.cool_after:
                    stalls += 1
                    if stalls > 3:
                        print("\n! the server has been failing continuously — stopping.\n"
                              "  Everything downloaded so far is saved and listed in the CSV;\n"
                              "  rerun later and it will pick up where it left off.",
                              file=sys.stderr)
                        abort = True
                        break
                    print(f"\n… {misses} failures in a row. Pausing {args.cooldown:.0f}s "
                          f"and reconnecting (cooldown {stalls}/3).\n", file=sys.stderr)
                    time.sleep(args.cooldown)
                    session = refresh_session(session, args.insecure)
                    misses = 0
                continue
            misses, stalls = 0, 0
            for fname, link in files:
                store["rows"].append({
                    "file_name": fname,
                    "program_name": (program or page_title(html)
                                     or os.path.splitext(fname)[0]),
                    "episode_name": ep["title"] or ott_name(html),
                    "channel": channel_label,
                    "link": link,
                    "episode_link": ep["url"],
                    "date_of_program": format_date(ep["parts"] or parts_from_filename(fname),
                                                   args.gregorian),
                    "duration": duration_minutes(os.path.join(folder, fname)),
                    "download_date": now_stamp(),
                })
                saved += 1
            flush_store(store)              # keep the CSV correct if interrupted

    print("\n" + "=" * 50)
    grand = 0.0
    for folder, store in stores.items():
        n = flush_store(store)
        mins = 0.0
        for r in list(store["known"].values()) + store["rows"]:
            try:
                mins += float(r.get("duration") or 0)
            except ValueError:
                pass
        grand += mins
        print(f"{n} row(s) in {store['csv']}  ({mins/60:.1f} h)")
    print(f"{saved} new file(s), {skipped} already had, {failed} failed, "
          f"{grand:.0f} minutes ({grand/60:.1f} hours) total")
    return 0 if saved or not failed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted — rerun to resume.", file=sys.stderr)
        sys.exit(130)