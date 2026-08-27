#!/usr/bin/env python3
"""LocalHBT Translator - local Hebrew<->English translation UI backed by Ollama.

Stdlib only. Serves index.html and proxies streaming generation to Ollama on the
same origin, so the browser never hits a CORS wall.

On top of the quick paste-box translator it runs *document jobs*: every .txt fed
in gets its own JSON file under Translations/ holding the chunk list, the
per-chunk translation, a bookmark and a status line. The JSON is rewritten after
every single chunk, so a crash or a power cut costs at most one chunk and
pressing Translate again picks up exactly where it stopped.
"""
import http.server
import socketserver
import json
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import webbrowser
import hashlib
import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("HBT_PORT", "8770"))
DEFAULT_MODEL = os.environ.get("HBT_MODEL", "dictalm-fast")

JOBS_DIR = os.path.join(ROOT, "Translations")      # one .json per document
OUT_DIR = os.path.join(ROOT, "Translated")         # default place for the .txt
UPLOAD_DIR = os.path.join(ROOT, "Texts", "Uploads")

# The phone reaches this box over Tailscale, so the socket cannot stay on
# loopback. Binding every interface is the only way to catch both 127.0.0.1 and
# the tailnet address, but it also puts the port on whatever cafe WiFi the
# laptop later joins. So: listen everywhere, then refuse any client that is not
# loopback or inside 100.64.0.0/10, the CGNAT range Tailscale allocates from.
BIND = os.environ.get("HBT_BIND", "0.0.0.0")
ALLOW_TAILNET = os.environ.get("HBT_ALLOW_TAILNET", "1") != "0"


def client_allowed(ip):
    if ip.startswith("127.") or ip in ("::1", "localhost"):
        return True
    if not ALLOW_TAILNET:
        return False
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return a == 100 and 64 <= b <= 127          # 100.64.0.0/10


def port_in_use(port):
    """Is another copy of this server already answering on this port?

    allow_reuse_address = False only blocks a second bind to the *same* address.
    Binding 0.0.0.0 so the phone can get in means a wildcard socket and an older
    loopback socket are different addresses, so both succeed - and Windows then
    hands 127.0.0.1 to one process and the tailnet address to the other. Two
    servers would share the Translations folder and fight over every job, which
    is precisely what refusing to reuse the address was meant to prevent. So
    probe the port before binding anything.
    """
    import socket
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=0.6):
                return True
        except OSError:
            continue
    return False


def tailnet_ip():
    """Best guess at this machine's own 100.x address, for the banner."""
    import socket
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ip = info[4][0]
            if client_allowed(ip) and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return None

_host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").strip()
if not _host.startswith("http"):
    _host = "http://" + _host
OLLAMA = _host.rstrip("/")

# One source line per chunk keeps the bookmark fine-grained and the output
# aligned 1:1 with the input; only genuinely huge lines get split further.
MAX_CHUNK_CHARS = int(os.environ.get("HBT_MAX_CHUNK", "1500"))

HEB = re.compile(r"[\u0590-\u05FF]")
HEB_LETTER = re.compile(r"[\u05D0-\u05EA]")        # alef..tav, no nikkud marks
LAT = re.compile(r"[A-Za-z]")
NIKKUD = re.compile(r"[\u0591-\u05C7]")

# How much of a line's letters must be in the source language before it is worth
# sending to the model. "Contains one Hebrew letter" is far too loose: a title
# like 'Rambam Introduction to the Mishnah / \u05D4\u05E7\u05D3\u05DE\u05EA \u05D4\u05E8\u05DE\u05D1"\u05DD \u05DC\u05DE\u05E9\u05E0\u05D4' is two thirds
# English, and asking a model to vocalize and translate that sends it into a
# repetition loop. Lines below the bar are copied to the output untouched.
MIN_SRC_RATIO = float(os.environ.get("HBT_MIN_SRC_RATIO", "0.4"))

# A chunk is one short line, so thousands of characters without an answer means
# the model is looping rather than working. Cut it loose and move on.
RUNAWAY_CHARS = int(os.environ.get("HBT_RUNAWAY_CHARS", "6000"))


class RunawayGeneration(RuntimeError):
    """The model kept generating instead of answering."""

# ---------------------------------------------------------------- prompts

HE2EN_NATURAL = (
    "You are a professional Hebrew-to-English translator. Translate the user's "
    "Hebrew text into natural, fluent English that reads as if originally written "
    "in English. Preserve meaning, tone and register. "
    "Output ONLY the translation - no preamble, no notes, no transliteration, "
    "and do not repeat the Hebrew."
)
HE2EN_LITERAL = (
    "You are a precise Hebrew-to-English translator working on source texts. "
    "Translate the user's Hebrew literally, staying close to the original wording "
    "and sentence structure even when the English reads stiffly. Keep proper nouns "
    "and technical or religious terms transliterated where there is no clean "
    "English equivalent. Output ONLY the translation - no preamble, no notes."
)
EN2HE_NATURAL = (
    "You are a professional English-to-Hebrew translator. Translate the user's "
    "English text into natural, fluent modern Hebrew with correct grammar and "
    "gender agreement. Output ONLY the Hebrew translation - no preamble, no notes, "
    "no transliteration, and do not repeat the English."
)
EN2HE_LITERAL = (
    "You are a precise English-to-Hebrew translator. Translate the user's English "
    "literally into Hebrew, staying close to the original wording and structure. "
    "Output ONLY the Hebrew translation - no preamble, no notes."
)

PROMPTS = {
    ("he2en", "natural"): HE2EN_NATURAL,
    ("he2en", "literal"): HE2EN_LITERAL,
    ("en2he", "natural"): EN2HE_NATURAL,
    ("en2he", "literal"): EN2HE_LITERAL,
}

# Interlinear asks for two jobs at once - vocalise, then translate - and pins the
# answer to a tagged format so the reply can be split apart mechanically.
INTERLINEAR_NIKKUD = (
    "You are a Hebrew grammarian preparing an interlinear study edition. "
    "You receive ONE short line of Hebrew. Do exactly two things:\n"
    "1. Reproduce that SAME line word for word, in the same order, with nothing "
    "added or removed, but fully vocalized with nikkud (vowel points) according "
    "to standard Hebrew grammar.\n"
    "2. Translate the line into clear, faithful English.\n"
    "Reply in exactly this format, two lines, nothing else:\n"
    "HEB: <the vocalized Hebrew line>\n"
    "ENG: <the English translation>"
)
INTERLINEAR_PLAIN = (
    "You are a Hebrew scholar preparing an interlinear study edition. "
    "You receive ONE short line of Hebrew. Translate it into clear, faithful "
    "English. Reply with the translation only - one line, no preamble, no notes, "
    "and do not repeat the Hebrew."
)


def system_prompt(direction, style):
    return PROMPTS.get((direction, style), HE2EN_NATURAL)


def job_prompt(job):
    if job["mode"] == "interlinear":
        return INTERLINEAR_NIKKUD if job.get("nikkud", True) else INTERLINEAR_PLAIN
    return system_prompt(job["direction"], job["style"])


# ---------------------------------------------------------------- small helpers

def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sha8(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


def slug(s):
    s = re.sub(r"[^A-Za-z0-9\u0590-\u05FF]+", "_", s).strip("_")
    return (s or "doc")[:60]


def has_hebrew(s):
    return bool(HEB.search(s))


def is_source_line(s, direction):
    """True when a line is mostly written in the language we translate FROM.

    Lines with no letters at all - rules, numbers, section markers, blanks - are
    never source text; they pass straight through to the output.
    """
    heb = len(HEB_LETTER.findall(s))
    lat = len(LAT.findall(s))
    total = heb + lat
    if not total:
        return False
    want = heb if direction == "he2en" else lat
    return want / total >= MIN_SRC_RATIO


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def atomic_write(path, text):
    """Write via a temp file + os.replace so a crash never leaves a half file."""
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def strip_think(s):
    """Some models emit their reasoning inline and close it with </think>."""
    i = s.rfind("</think>")
    if i >= 0:
        s = s[i + len("</think>"):]
    return s.strip()


def iter_lines(text):
    """Yield (line without its newline, char offset of the line start)."""
    off = 0
    for raw in text.splitlines(keepends=True):
        yield raw.rstrip("\r\n"), off
        off += len(raw)


# ---------------------------------------------------------------- chunking

CLAUSE_END = ".:;?!\u05C3\u05F4\u05F3\u2019\"'\u00BB)]}"


def _split_long(line, start, limit):
    """Break an over-long line at clause boundaries, keeping char offsets."""
    out, buf, buf_start = [], "", start
    for m in re.finditer(r"\S+\s*", line):
        w = m.group(0)
        if buf and len(buf) + len(w) > limit and buf.rstrip()[-1:] in CLAUSE_END:
            out.append((buf.rstrip(), buf_start))
            buf, buf_start = "", start + m.start()
        buf += w
        if len(buf) > limit * 1.6:            # no clause break in sight; cut anyway
            out.append((buf.rstrip(), buf_start))
            buf, buf_start = "", start + m.end()
    if buf.strip():
        out.append((buf.rstrip(), buf_start))
    return out


def _word_lines(line, start, n):
    """Group a line into runs of n..n+1 words, preferring a clause break at n."""
    spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", line)]
    out, i = [], 0
    while i < len(spans):
        take = min(n, len(spans) - i)
        # n words unless the next one finishes the clause - then take it along.
        if take == n and i + take < len(spans):
            word_n = line[spans[i + take - 1][0]:spans[i + take - 1][1]]
            nxt = line[spans[i + take][0]:spans[i + take][1]]
            if word_n[-1:] not in CLAUSE_END and nxt[-1:] in CLAUSE_END:
                take += 1
        a, b = spans[i][0], spans[i + take - 1][1]
        out.append((line[a:b], start + a))
        i += take
    return out


def build_chunks(text, mode, direction, words_per_line):
    """Turn a document into the chunk list a job walks through.

    Lines with no source-language letters (headers, rules, blank lines) become
    'pass' chunks: copied to the output verbatim, never sent to the model.
    """
    chunks = []

    def add(kind, src, start):
        chunks.append({"i": len(chunks), "kind": kind, "src": src,
                       "start": start, "heb": "", "out": "",
                       "done": False, "warn": ""})

    for line, off in iter_lines(text):
        if not is_source_line(line, direction):
            add("pass", line, off)
            continue
        pieces = ([(line, off)] if len(line) <= MAX_CHUNK_CHARS
                  else _split_long(line, off, MAX_CHUNK_CHARS))
        for piece, poff in pieces:
            if mode == "interlinear":
                for wl, woff in _word_lines(piece, poff, words_per_line):
                    add("text", wl, woff)
            else:
                add("text", piece, poff)
    return chunks


# ---------------------------------------------------------------- ollama

def ollama_chat(model, system, user, think=False, temperature=0.2,
                on_token=None, timeout=900, runaway=0):
    """Stream one chat completion. Returns (content, thinking, stats).

    Raises RunawayGeneration if `runaway` is set and the model produces that many
    characters without ever closing its reasoning, or triples it overall.
    """
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": True,
        "think": bool(think),
        "keep_alive": "30m",
        "options": {"temperature": float(temperature)},
    }
    req = urllib.request.Request(
        OLLAMA + "/api/chat", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    content, thinking, stats = "", "", {}
    with urllib.request.urlopen(req, timeout=timeout) as up:
        for line in up:
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if j.get("error"):
                raise RuntimeError(j["error"])
            msg = j.get("message") or {}
            if msg.get("thinking"):
                thinking += msg["thinking"]
            if msg.get("content"):
                content += msg["content"]
                if on_token:
                    on_token(msg["content"])
                if runaway and len(content) > runaway and "</think>" not in content:
                    raise RunawayGeneration(
                        "looped for %d characters without answering" % len(content))
                if runaway and len(content) > runaway * 3:
                    raise RunawayGeneration(
                        "produced %d characters for one line" % len(content))
            if j.get("done"):
                stats = {"eval_count": j.get("eval_count", 0),
                         "eval_duration": j.get("eval_duration", 0)}
    return content, thinking, stats


def parse_interlinear(raw, source_line):
    """Pull the vocalized Hebrew and the English out of a tagged reply.

    Tags first; if the model ignored them, fall back to classifying each line by
    which script it is written in, which is hard to get wrong for he/en.
    """
    txt = strip_think(raw)
    heb = eng = ""
    for line in txt.splitlines():
        s = line.replace("*", "").replace("`", "").strip()   # models love markdown
        if not s:
            continue
        m = re.match(r"^(HEB|HEBREW|ENG|ENGLISH)\s*[:\-\u2013]\s*(.*)$", s, re.I)
        body = (m.group(2).strip() if m else s)
        tag = (m.group(1).upper() if m else "")
        if not body:
            continue
        if tag.startswith("HEB") or (not tag and has_hebrew(body)):
            heb = heb or body
        elif tag.startswith("ENG") or (not tag and LAT.search(body)):
            eng = eng or body
    if not heb:
        heb = source_line
    if not eng:
        eng = txt
    return heb.strip(), eng.strip()


# ---------------------------------------------------------------- job store

JOB_LOCK = threading.RLock()
RUNNING = {}          # id -> {"stop": Event, "live": str, "thread": Thread}


def job_path(jid):
    return os.path.join(JOBS_DIR, jid + ".json")


def status_line(job):
    t, d = job["total_text"], job["done_text"]
    pct = (100.0 * d / t) if t else 0.0
    label = {"running": "IN PROGRESS", "done": "COMPLETE",
             "paused": "PAUSED", "interrupted": "CUT OFF MID-TRANSLATION",
             "error": "ERROR", "pending": "NOT STARTED"}.get(job["status"],
                                                             job["status"].upper())
    return "%s - %d of %d translatable chunks (%.1f%%)" % (label, d, t, pct)


def save_job(job):
    with JOB_LOCK:
        job["updated"] = now()
        job["done_text"] = sum(1 for c in job["chunks"]
                               if c["kind"] == "text" and c["done"])
        job["percent"] = (round(100.0 * job["done_text"] / job["total_text"], 2)
                          if job["total_text"] else 0.0)
        job["status_line"] = status_line(job)
        os.makedirs(JOBS_DIR, exist_ok=True)
        atomic_write(job_path(job["id"]),
                     json.dumps(job, ensure_ascii=False, indent=1))
    return job


def load_job(jid):
    if not jid:
        return None
    p = job_path(jid)
    if not os.path.exists(p):
        return None
    with JOB_LOCK:
        try:
            return json.loads(read_text(p))
        except ValueError:
            return None


def list_jobs():
    out = []
    if not os.path.isdir(JOBS_DIR):
        return out
    for fn in sorted(os.listdir(JOBS_DIR)):
        if not fn.endswith(".json"):
            continue
        j = load_job(fn[:-5])
        if not j:
            continue
        out.append({k: j.get(k) for k in
                    ("id", "title", "status", "status_line", "percent",
                     "done_text", "total_text", "bookmark", "mode", "direction",
                     "style", "model", "source_file", "output_file", "created",
                     "updated", "elapsed_sec", "error")})
    out.sort(key=lambda x: x.get("updated") or "", reverse=True)
    return out


def job_id_for(src, mode, direction):
    base = os.path.splitext(os.path.basename(os.path.abspath(src)))[0]
    return "%s-%s-%s" % (slug(base), mode[:4],
                         sha8(os.path.abspath(src) + mode + direction))


def make_job(source_file, opts):
    src = os.path.abspath(source_file)
    text = read_text(src)
    mode = opts.get("mode", "paragraph")
    direction = opts.get("direction", "he2en")
    wpl = int(opts.get("words_per_line", 16) or 16)
    chunks = build_chunks(text, mode, direction, wpl)
    base = os.path.splitext(os.path.basename(src))[0]
    title = next((l.strip() for l, _ in iter_lines(text) if l.strip()), base)[:120]
    outdir = os.path.abspath(opts.get("output_dir") or OUT_DIR)
    suffix = ".interlinear.txt" if mode == "interlinear" else ".translated.txt"

    # Key order is the on-disk reading order: status first, chunk data last.
    job = {
        "id": job_id_for(src, mode, direction),
        "title": title,
        "status": "pending",
        "status_line": "",
        "percent": 0.0,
        "bookmark": 0,
        "done_text": 0,
        "total_text": sum(1 for c in chunks if c["kind"] == "text"),
        "total_chunks": len(chunks),
        "chars_total": sum(len(c["src"]) for c in chunks if c["kind"] == "text"),
        "source_file": src,
        "source_sha": sha8(text),
        "source_chars": len(text),
        "output_dir": outdir,
        "output_file": os.path.join(outdir, base + suffix),
        "mode": mode,
        "direction": direction,
        "style": opts.get("style", "natural"),
        "nikkud": bool(opts.get("nikkud", True)),
        "words_per_line": wpl,
        "include_source": bool(opts.get("include_source", False)),
        "mark_cuts": bool(opts.get("mark_cuts", True)),
        "model": opts.get("model") or DEFAULT_MODEL,
        "temperature": float(opts.get("temperature", 0.2)),
        "think": bool(opts.get("think", False)),
        "created": now(),
        "updated": now(),
        "started": "",
        "finished": "",
        "elapsed_sec": 0.0,
        "tokens_out": 0,
        "skipped": 0,
        "error": "",
        "cut_marks": [],
        "chunks": chunks,
    }
    return save_job(job)


# ---------------------------------------------------------------- output file

def build_output(job, upto=None):
    """Assemble the translation as it stands.

    Stops at the bookmark, so a partial job yields a clean prefix of the finished
    document rather than a file with holes in it.
    """
    parts = []
    end = job["bookmark"] if upto is None else upto
    for c in job["chunks"][:end]:
        if c["kind"] == "pass":
            parts.append(c["src"])
        elif job["mode"] == "interlinear":
            parts.append(c.get("heb") or c["src"])
            parts.append(c["out"])
            parts.append("")
        elif job.get("include_source"):
            parts.append(c["src"])
            parts.append(c["out"])
            parts.append("")
        else:
            parts.append(c["out"])
    return "\n".join(parts).rstrip() + "\n"


def current_output(job):
    """What the translation actually is right now.

    build_output() re-assembles it from the chunks, and normally that is exactly
    what the .txt holds - every finished chunk rewrites the file. The two part
    company only after somebody hand-edits the full text, and then it is the
    file that holds the correction. So the file wins whenever it exists, which is
    also what makes a hand edit survive the next refresh instead of silently
    reverting to the model's wording.
    """
    try:
        if os.path.exists(job["output_file"]):
            return read_text(job["output_file"])
    except OSError:
        pass
    return build_output(job)


def build_side_by_side(job):
    """Source line, its translation, blank line - regardless of job mode.

    build_output() honours the job's own layout, which for a plain paragraph job
    means the source never appears. The phone's "save to device" wants the pair
    either way, so this assembles it independently.
    """
    parts = []
    for c in job["chunks"][:job["bookmark"]]:
        if c["kind"] == "pass":
            parts.append(c["src"])
            parts.append("")
            continue
        parts.append(c.get("heb") or c["src"])
        parts.append(c["out"])
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def write_output(job):
    try:
        atomic_write(job["output_file"], build_output(job))
    except OSError as e:
        job["error"] = "could not write output: %s" % e


# ---------------------------------------------------------------- cut marks

def mark_cut(job):
    """Drop three blank lines into the SOURCE .txt where translation stopped.

    Purely a human cue - resuming uses the bookmark in the JSON - so it is
    located by content rather than by offset and stays correct even after
    earlier marks have shifted the file around.
    """
    if not job.get("mark_cuts"):
        return
    chunks = job["chunks"]
    # The bookmark can sit on a blank or passed-through line, which gives the
    # mark nothing to anchor to; walk on to the next line that has real text.
    idx = next((i for i in range(job["bookmark"], len(chunks))
                if chunks[i]["src"].strip()), -1)
    if idx <= 0:
        return
    try:
        text = read_text(job["source_file"])
    except OSError:
        return
    needle = chunks[idx]["src"].strip()[:40]
    hint = max(0, chunks[idx].get("start", 0) - 400)
    pos = text.find(needle, hint)
    if pos < 0:
        pos = text.find(needle)
    if pos < 0:
        return
    if text[max(0, pos - 4):pos].count("\n") >= 3:
        return                                  # this spot is already marked
    text = text[:pos] + "\n\n\n" + text[pos:]
    try:
        atomic_write(job["source_file"], text)
    except OSError:
        return
    job["cut_marks"].append({"at": now(), "chunk": idx, "preview": needle})
    job["source_sha"] = sha8(text)


# ---------------------------------------------------------------- worker

def run_job(jid):
    job = load_job(jid)
    if not job:
        RUNNING.pop(jid, None)
        return
    state = RUNNING[jid]
    stop = state["stop"]
    # Hand-edits arriving while this runs must land on the object the worker is
    # about to save, or the next chunk would write the stale copy back over them.
    state["job"] = job
    job["status"] = "running"
    job["started"] = job["started"] or now()
    job["error"] = ""
    save_job(job)
    t0 = time.time()
    base_elapsed = job.get("elapsed_sec", 0.0)

    try:
        while job["bookmark"] < len(job["chunks"]):
            if stop.is_set():
                job["status"] = "paused"
                break
            c = job["chunks"][job["bookmark"]]

            if c["kind"] == "pass":
                c["done"] = True
                job["bookmark"] += 1
                continue

            state["live"] = ""
            state["live_i"] = c["i"]

            def on_token(t, _s=state):
                _s["live"] += t

            try:
                raw, _think, stats = ollama_chat(
                    job["model"], job_prompt(job), c["src"],
                    think=job.get("think", False),
                    temperature=job.get("temperature", 0.2),
                    on_token=on_token, runaway=RUNAWAY_CHARS)
            except RunawayGeneration as e:
                # One bad line must not stall the whole document. Flag it, leave
                # the translation blank for hand-editing, and carry on.
                c["heb"] = c["src"] if job["mode"] == "interlinear" else ""
                c["out"] = ""
                c["warn"] = "skipped - %s" % e
                c["done"] = True
                job["skipped"] = job.get("skipped", 0) + 1
                job["bookmark"] += 1
                state["live"] = ""
                save_job(job)
                write_output(job)
                continue

            if job["mode"] == "interlinear":
                heb, eng = parse_interlinear(raw, c["src"])
                c["heb"], c["out"] = heb, eng
                if job.get("nikkud", True):
                    if not NIKKUD.search(heb):
                        c["warn"] = "model returned no nikkud"
                    elif len(heb.split()) != len(c["src"].split()):
                        c["warn"] = "vocalized line has a different word count"
            else:
                c["out"] = strip_think(raw)

            c["done"] = True
            job["tokens_out"] += stats.get("eval_count", 0)
            job["bookmark"] += 1
            job["elapsed_sec"] = round(base_elapsed + (time.time() - t0), 1)
            state["live"] = ""
            save_job(job)         # durable point: one chunk is the blast radius
            write_output(job)
        else:
            job["status"] = "done"
            job["finished"] = now()
    except Exception as e:
        job["status"] = "error"
        job["error"] = "%s: %s" % (type(e).__name__, e)

    job["elapsed_sec"] = round(base_elapsed + (time.time() - t0), 1)
    if job["status"] in ("paused", "error"):
        mark_cut(job)
    save_job(job)
    write_output(job)
    RUNNING.pop(jid, None)


def start_job(jid):
    if jid in RUNNING:
        return False, "already running"
    job = load_job(jid)
    if not job:
        return False, "no such job"
    if job["bookmark"] >= len(job["chunks"]):
        return False, "already finished - use Restart to run it again"
    RUNNING[jid] = {"stop": threading.Event(), "live": "", "live_i": -1}
    t = threading.Thread(target=run_job, args=(jid,), daemon=True)
    RUNNING[jid]["thread"] = t
    t.start()
    return True, "started"


def recover_jobs():
    """Any job still flagged 'running' at boot was killed mid-translation."""
    n = 0
    for summary in list_jobs():
        if summary["status"] != "running":
            continue
        job = load_job(summary["id"])
        if not job:
            continue
        job["status"] = "interrupted"
        job["error"] = "translation was cut off (crash, power loss or close)"
        mark_cut(job)
        save_job(job)
        write_output(job)
        n += 1
    return n


def shutdown_jobs():
    for jid in list(RUNNING):
        RUNNING[jid]["stop"].set()
    for jid in list(RUNNING):
        t = RUNNING.get(jid, {}).get("thread")
        if t:
            t.join(timeout=25)


# ---------------------------------------------------------------- http

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "LocalHBT/2.0"

    def log_message(self, fmt, *args):
        pass  # keep the console clean; errors still surface in the UI

    # ---- plumbing

    def _send(self, code, ctype, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False))

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _arg(self, name, default=""):
        parts = self.path.split("?", 1)
        if len(parts) < 2:
            return default
        return (urllib.parse.parse_qs(parts[1]).get(name) or [default])[0]

    # ---- GET

    def _blocked(self):
        """True (and already answered) if this client is off the tailnet."""
        ip = self.client_address[0]
        if client_allowed(ip):
            return False
        self._send(403, "text/plain; charset=utf-8",
                   "LocalHBT is reachable from this machine and from the "
                   "tailnet only.")
        return True

    def do_GET(self):
        if self._blocked():
            return
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                self._file("index.html", "text/html; charset=utf-8")
            elif path == "/api/models":
                self._models()
            elif path == "/api/fs/list":
                self._fs_list()
            elif path == "/api/fs/read":
                self._fs_read()
            elif path == "/api/jobs":
                self._json({"ok": True, "jobs": list_jobs(),
                            "jobs_dir": JOBS_DIR, "out_dir": OUT_DIR,
                            "root": ROOT})
            elif path == "/api/job":
                self._job_detail()
            elif path == "/api/job/output":
                self._job_output()
            elif path == "/api/job/bundle":
                self._job_bundle()
            elif path == "/api/sync/pull":
                self._sync_pull()
            else:
                self._send(404, "text/plain; charset=utf-8", "not found")
        except Exception as e:
            self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

    def _file(self, name, ctype):
        try:
            with open(os.path.join(ROOT, name), "rb") as f:
                self._send(200, ctype, f.read())
        except OSError as e:
            self._send(500, "text/plain; charset=utf-8", "%s missing: %s" % (name, e))

    def _models(self):
        try:
            with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=10) as r:
                tags = json.load(r)
            names = sorted(m["name"] for m in tags.get("models", []))
            self._json({"ok": True, "models": names, "default": DEFAULT_MODEL})
        except Exception as e:
            self._json({"ok": False,
                        "error": "Cannot reach Ollama at %s (%s)" % (OLLAMA, e)})

    def _fs_list(self):
        d = self._arg("dir") or ROOT
        if d == "@drives":
            drives = ["%s:\\" % c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                      if os.path.exists("%s:\\" % c)]
            self._json({"ok": True, "dir": "@drives", "parent": "",
                        "dirs": drives, "files": [], "root": ROOT})
            return
        d = os.path.abspath(d)
        if not os.path.isdir(d):
            self._json({"ok": False, "error": "not a folder: %s" % d}, 400)
            return
        dirs, files = [], []
        for name in sorted(os.listdir(d), key=str.lower):
            p = os.path.join(d, name)
            try:
                if os.path.isdir(p):
                    if not name.startswith("."):
                        dirs.append(p)
                elif name.lower().endswith((".txt", ".md")):
                    files.append({"path": p, "name": name,
                                  "size": os.path.getsize(p),
                                  "mtime": time.strftime(
                                      "%Y-%m-%d %H:%M",
                                      time.localtime(os.path.getmtime(p)))})
            except OSError:
                continue
        parent = os.path.dirname(d.rstrip("\\/")) or "@drives"
        if parent == d:
            parent = "@drives"
        self._json({"ok": True, "dir": d, "parent": parent,
                    "dirs": dirs, "files": files, "root": ROOT})

    def _fs_read(self):
        p = self._arg("path")
        if not p or not os.path.isfile(p):
            self._json({"ok": False, "error": "no such file"}, 404)
            return
        text = read_text(p)
        self._json({"ok": True, "path": os.path.abspath(p), "text": text,
                    "chars": len(text), "sha": sha8(text)})

    def _job_detail(self):
        job = load_job(self._arg("id"))
        if not job:
            self._json({"ok": False, "error": "no such job"}, 404)
            return
        try:
            frm = max(0, int(self._arg("from", "0") or 0))
        except ValueError:
            frm = 0
        live = RUNNING.get(job["id"], {})
        meta = {k: v for k, v in job.items() if k != "chunks"}
        meta["running"] = job["id"] in RUNNING
        # The model streams its reasoning chain inline before the answer. Send
        # the answer in full, but only the tail of the reasoning - a model stuck
        # repeating itself should read as one line, not a wall of text.
        raw = live.get("live", "")
        thinking = bool(raw) and "</think>" not in raw
        body = strip_think(raw)
        if thinking:
            body = body[-240:]
        self._json({"ok": True, "job": meta,
                    "from": frm, "chunks": job["chunks"][frm:],
                    "live": body,
                    "live_thinking": thinking,
                    "live_chars": len(raw),
                    "live_i": live.get("live_i", -1)})

    def _job_output(self):
        job = load_job(self._arg("id"))
        if not job:
            self._json({"ok": False, "error": "no such job"}, 404)
            return
        text = current_output(job)
        self._json({"ok": True, "text": text, "sha": sha8(text),
                    "output_file": job["output_file"]})

    # ---- POST

    def do_POST(self):
        if self._blocked():
            return
        path = self.path.split("?")[0]
        try:
            if path == "/api/translate":
                self._quick()
            elif path == "/api/fs/write":
                self._fs_write()
            elif path == "/api/fs/upload":
                self._fs_upload()
            elif path == "/api/fs/open":
                self._fs_open()
            elif path == "/api/job/create":
                self._job_create()
            elif path == "/api/job/start":
                self._job_start()
            elif path == "/api/job/pause":
                self._job_pause()
            elif path == "/api/job/reset":
                self._job_reset()
            elif path == "/api/job/delete":
                self._job_delete()
            elif path == "/api/job/edit":
                self._job_edit()
            elif path == "/api/job/export":
                self._job_export()
            elif path == "/api/job/settings":
                self._job_settings()
            elif path == "/api/sync/push":
                self._sync_push()
            else:
                self._send(404, "text/plain; charset=utf-8", "not found")
        except Exception as e:
            self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

    # quick paste-box translate: unchanged streaming passthrough
    def _quick(self):
        req = self._read_json()
        text = (req.get("text") or "").strip()
        if not text:
            self._json({"ok": False, "error": "empty text"}, 400)
            return
        payload = {
            "model": req.get("model") or DEFAULT_MODEL,
            "messages": [
                {"role": "system",
                 "content": system_prompt(req.get("direction", "he2en"),
                                          req.get("style", "natural"))},
                {"role": "user", "content": text},
            ],
            "stream": True,
            # Thinking off by default: translation does not need a reasoning
            # chain, and skipping it cuts time-to-first-token substantially.
            "think": bool(req.get("think", False)),
            "keep_alive": "30m",
            "options": {"temperature": float(req.get("temperature", 0.2))},
        }
        self._stream(payload)

    def _stream(self, payload):
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            OLLAMA + "/api/chat", data=data,
            headers={"Content-Type": "application/json"})
        try:
            upstream = urllib.request.urlopen(request, timeout=900)
        except Exception as e:
            self._json({"ok": False, "error": "Ollama request failed: %s" % e}, 502)
            return

        # No Content-Length: HTTP/1.0 semantics mean the client reads until we
        # close, which is all fetch()'s ReadableStream needs.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with upstream:
                for line in upstream:
                    if not line.strip():
                        continue
                    self.wfile.write(line if line.endswith(b"\n") else line + b"\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # user hit Stop or closed the tab
        except Exception as e:
            try:
                self.wfile.write(
                    (json.dumps({"error": str(e)}) + "\n").encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass

    def _fs_write(self):
        req = self._read_json()
        p = req.get("path")
        if not p:
            self._json({"ok": False, "error": "no path"}, 400)
            return
        text = req.get("text", "")
        atomic_write(p, text)
        self._json({"ok": True, "path": os.path.abspath(p), "chars": len(text),
                    "sha": sha8(text)})

    def _fs_upload(self):
        """Browsers hide real paths, so a picked file is copied in under Texts/."""
        req = self._read_json()
        name = os.path.basename(req.get("name") or "pasted.txt") or "pasted.txt"
        if not name.lower().endswith((".txt", ".md")):
            name += ".txt"
        text = req.get("text", "")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        p = os.path.join(UPLOAD_DIR, name)
        stem, ext = os.path.splitext(p)
        n = 2
        while os.path.exists(p) and read_text(p) != text:
            p = "%s_%d%s" % (stem, n, ext)
            n += 1
        atomic_write(p, text)
        self._json({"ok": True, "path": p})

    def _fs_open(self):
        p = self._read_json().get("path") or ROOT
        p = p if os.path.isdir(p) else os.path.dirname(os.path.abspath(p))
        try:
            os.startfile(p)                                    # Windows only
            self._json({"ok": True, "path": p})
        except Exception as e:
            self._json({"ok": False, "error": str(e)})

    def _job_create(self):
        req = self._read_json()
        src = req.get("source_file")
        if not src or not os.path.isfile(src):
            self._json({"ok": False, "error": "source file not found"}, 400)
            return
        jid = job_id_for(src, req.get("mode", "paragraph"),
                         req.get("direction", "he2en"))
        existing = None if req.get("force") else load_job(jid)
        if existing:
            self._json({"ok": True, "job_id": jid, "resumed": True,
                        "status": existing["status"],
                        "status_line": existing["status_line"]})
            return
        job = make_job(src, req)
        # Nothing to translate almost always means the direction is backwards -
        # an English file with Hebrew->English selected, or vice versa. Say so
        # rather than handing back a job that reads "0 of 0".
        warn = ""
        if not job["total_text"]:
            want = "Hebrew" if job["direction"] == "he2en" else "English"
            warn = ("No %s text found in %s - nothing to translate. Check the "
                    "Direction setting." % (want, os.path.basename(src)))
        self._json({"ok": True, "job_id": job["id"], "resumed": False,
                    "status": job["status"], "status_line": job["status_line"],
                    "warning": warn})

    def _job_start(self):
        ok, msg = start_job(self._read_json().get("id"))
        self._json({"ok": ok, "message": msg, "error": "" if ok else msg})

    def _job_pause(self):
        jid = self._read_json().get("id")
        st = RUNNING.get(jid)
        if not st:
            self._json({"ok": False, "error": "not running"})
            return
        st["stop"].set()
        self._json({"ok": True, "message": "pausing after the current chunk"})

    def _job_reset(self):
        job = load_job(self._read_json().get("id"))
        if not job:
            self._json({"ok": False, "error": "no such job"}, 404)
            return
        if job["id"] in RUNNING:
            self._json({"ok": False, "error": "pause it first"}, 409)
            return
        text = read_text(job["source_file"])
        job["chunks"] = build_chunks(text, job["mode"], job["direction"],
                                     job["words_per_line"])
        job["total_chunks"] = len(job["chunks"])
        job["total_text"] = sum(1 for c in job["chunks"] if c["kind"] == "text")
        job["chars_total"] = sum(len(c["src"]) for c in job["chunks"]
                                 if c["kind"] == "text")
        job["source_sha"] = sha8(text)
        job["bookmark"] = 0
        job["status"] = "pending"
        job["elapsed_sec"] = 0.0
        job["tokens_out"] = 0
        job["error"] = ""
        job["started"] = job["finished"] = ""
        save_job(job)
        write_output(job)
        self._json({"ok": True, "status_line": job["status_line"]})

    def _job_delete(self):
        jid = self._read_json().get("id")
        if jid in RUNNING:
            RUNNING[jid]["stop"].set()
        try:
            os.remove(job_path(jid))
        except OSError:
            pass
        self._json({"ok": True})

    def _job_edit(self):
        """Hand-correct one chunk; the .txt is rebuilt from the corrected text."""
        req = self._read_json()
        jid = req.get("id")
        # Edit the worker's own copy when one is running, otherwise the on-disk
        # copy; either way the edit is what gets saved next.
        job = RUNNING.get(jid, {}).get("job") or load_job(jid)
        if not job:
            self._json({"ok": False, "error": "no such job"}, 404)
            return
        try:
            i = int(req.get("i", -1))
        except (TypeError, ValueError):
            i = -1
        if not (0 <= i < len(job["chunks"])):
            self._json({"ok": False, "error": "bad chunk index"}, 400)
            return
        with JOB_LOCK:
            c = job["chunks"][i]
            if "out" in req:
                c["out"] = req["out"]
            if "heb" in req:
                c["heb"] = req["heb"]
            c["warn"] = ""
            save_job(job)
            write_output(job)
        self._json({"ok": True})

    def _job_export(self):
        req = self._read_json()
        job = load_job(req.get("id"))
        if not job:
            self._json({"ok": False, "error": "no such job"}, 404)
            return
        dest = req.get("path") or job["output_file"]
        if os.path.isdir(dest):
            dest = os.path.join(dest, os.path.basename(job["output_file"]))
        atomic_write(dest, req.get("text") or build_output(job))
        self._json({"ok": True, "path": os.path.abspath(dest)})

    def _job_settings(self):
        """Change output location / model / options without losing progress."""
        req = self._read_json()
        job = load_job(req.get("id"))
        if not job:
            self._json({"ok": False, "error": "no such job"}, 404)
            return
        if job["id"] in RUNNING:
            self._json({"ok": False, "error": "pause it first"}, 409)
            return
        for k in ("model", "style", "temperature", "think", "nikkud",
                  "include_source", "mark_cuts"):
            if k in req:
                job[k] = req[k]
        if req.get("output_dir"):
            job["output_dir"] = os.path.abspath(req["output_dir"])
            job["output_file"] = os.path.join(
                job["output_dir"], os.path.basename(job["output_file"]))
        save_job(job)
        write_output(job)
        self._json({"ok": True, "output_file": job["output_file"]})


    # ---- phone sync

    def _doc_snapshot(self, job):
        try:
            src = read_text(job["source_file"])
        except OSError:
            src = ""
        out = current_output(job)
        return {"id": job["id"],
                "meta": {k: v for k, v in job.items() if k != "chunks"},
                "src_text": src,
                "out_text": out,
                "src_sha": sha8(src),
                "out_sha": sha8(out),
                "updated": job["updated"],
                "running": job["id"] in RUNNING}

    def _sync_pull(self):
        """What the phone needs to work with the PC unreachable.

        Bare, this is the index: one small row per document. Naming ids pulls
        their full source and translation, which for a long tractate is most of
        a megabyte each - so the phone asks for those a document at a time
        rather than dragging the whole library down every sync.
        """
        want = [x for x in (self._arg("ids") or "").split(",") if x]
        docs = []
        for meta in list_jobs():
            if want and meta["id"] not in want:
                continue
            job = load_job(meta["id"])
            if not job:
                continue
            if want:
                docs.append(self._doc_snapshot(job))
            else:
                meta["running"] = job["id"] in RUNNING
                docs.append({"id": job["id"], "meta": meta,
                             "updated": job["updated"],
                             "running": meta["running"]})
        self._json({"ok": True, "server_time": now(),
                    "with_text": bool(want), "docs": docs})

    def _sync_push(self):
        """Write phone-side edits back, refusing to clobber a newer PC copy.

        The phone sends the hash of the text it started editing from. If what is
        on disk no longer hashes to that, the computer has moved on since the
        last sync: nothing is written and the server's copy comes back so the
        phone can ask which side wins. `force` then commits the phone's version.

        Hashing the two files separately rather than stamping the job as a whole
        matters twice over. A clock stamp only has one-second resolution, so two
        writes in the same second would look identical; and translating a
        document rewrites the output constantly, which would make every source
        edit look like a conflict when the source has not been touched at all.
        """
        req = self._read_json()
        jid = req.get("id")
        job = load_job(jid)
        if not job:
            self._json({"ok": False, "error": "no such job"}, 404)
            return
        if jid in RUNNING and "out_text" in req:
            self._json({"ok": False, "error":
                        "that document is translating right now - pause it "
                        "before pushing a translation edit"}, 409)
            return

        try:
            cur_src = read_text(job["source_file"])
        except OSError:
            cur_src = ""
        cur_out = current_output(job)

        if not req.get("force"):
            stale = None
            if "src_text" in req and req.get("base_src_sha")                     and req["base_src_sha"] != sha8(cur_src):
                stale = "source text"
            if "out_text" in req and req.get("base_out_sha")                     and req["base_out_sha"] != sha8(cur_out):
                stale = "translation"
            if stale:
                self._json({"ok": False, "conflict": True, "which": stale,
                            "error": "the computer's %s changed since the phone "
                                     "last synced" % stale,
                            "server": self._doc_snapshot(job)}, 409)
                return

        wrote = []
        try:
            if "src_text" in req:
                atomic_write(job["source_file"], req["src_text"])
                wrote.append("source")
            if "out_text" in req:
                atomic_write(job["output_file"], req["out_text"])
                wrote.append("translation")
        except OSError as e:
            self._json({"ok": False, "error": "could not write: %s" % e}, 500)
            return

        save_job(job)
        self._json({"ok": True, "wrote": wrote, "updated": job["updated"],
                    "src_sha": sha8(req.get("src_text", cur_src)),
                    "out_sha": sha8(req.get("out_text", cur_out))})

    def _job_bundle(self):
        """Source, translation and side-by-side as one folder's worth of files."""
        job = load_job(self._arg("id"))
        if not job:
            self._json({"ok": False, "error": "no such job"}, 404)
            return
        try:
            src = read_text(job["source_file"])
        except OSError:
            src = ""
        folder = slug(job.get("title") or job["id"])
        self._json({"ok": True, "folder": folder, "files": [
            {"name": "source.txt", "text": src},
            {"name": "translation.txt", "text": current_output(job)},
            {"name": "side-by-side.txt", "text": build_side_by_side(job)},
        ]})


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    # SO_REUSEADDR does NOT mean the same thing on Windows as it does on Unix:
    # there it lets a second process bind a port another process is already
    # listening on, and the two then split incoming connections at random. That
    # is how a stale copy of this server ends up serving a fresh index.html with
    # an old API and 404-ing every new endpoint. Refusing to reuse the address
    # makes a second launch fail loudly instead, which is what we want.
    allow_reuse_address = (os.name != "nt")


def main():
    if port_in_use(PORT):
        print("=" * 58)
        print("  LocalHBT is ALREADY RUNNING on port %d." % PORT)
        print("=" * 58)
        print("  Close every LocalHBT console window, wait a moment, then run")
        print("  Run Translator.bat again. Starting a second one would give you")
        print("  two servers sharing the same Translations folder.")
        print("  Already-open page: http://127.0.0.1:%d" % PORT)
        print("=" * 58)
        return 1

    for d in (JOBS_DIR, OUT_DIR):
        os.makedirs(d, exist_ok=True)
    cut = recover_jobs()

    try:
        httpd = Server((BIND, PORT), Handler)
    except OSError as e:
        print("Could not bind port %d: %s" % (PORT, e))
        print("Another copy may already be running -> http://127.0.0.1:%d" % PORT)
        return 1

    url = "http://127.0.0.1:%d" % PORT
    print("=" * 58)
    print("  LocalHBT Translator")
    print("=" * 58)
    print("  UI      : %s" % url)
    tsip = tailnet_ip()
    if tsip:
        print("  Phone   : http://%s:%d   (Tailscale)" % (tsip, PORT))
    elif ALLOW_TAILNET:
        print("  Phone   : http://<this-machine's 100.x address>:%d" % PORT)
    print("  Ollama  : %s" % OLLAMA)
    print("  Model   : %s" % DEFAULT_MODEL)
    print("  Jobs    : %s" % JOBS_DIR)
    print("  Output  : %s" % OUT_DIR)
    if cut:
        print("  NOTE    : %d job(s) were cut off mid-translation." % cut)
        print("            Press Translate to resume from the bookmark.")
    print("  Stop    : Ctrl+C (or just close this window)")
    print("=" * 58)

    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down - finishing the current chunk")
    finally:
        shutdown_jobs()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
