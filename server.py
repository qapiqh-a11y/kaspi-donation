from copy import deepcopy
from pathlib import Path
from urllib.parse import unquote_plus

import asyncio
import base64
import json
import os
import re
import subprocess
import time
import uuid

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BASE_DIR = Path(__file__).resolve().parent
RUNNING_ON_VERCEL = bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_URL"))
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
ASSETS_DIR = BASE_DIR / "assets"
EFFECTS_DIR = ASSETS_DIR / "effects"
SOUNDS_DIR = ASSETS_DIR / "sounds"
ICONS_DIR = ASSETS_DIR / "icons"
TTS_DIR = ASSETS_DIR / "tts"
TTS_STORAGE_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "kaspi-donation-tts" if RUNNING_ON_VERCEL else TTS_DIR
DATA_FILE = DATA_DIR / "donate_totals.json"
ALIASES_FILE = DATA_DIR / "aliases.json"
GOAL_FILE = DATA_DIR / "goal.json"

DEFAULT_GOAL = {
    "title": "СБОР НА АПГРЕЙД СТРИМА",
    "subtitle": "",
    "target": 50000,
    "current": 0,
    "currency": "₸",
    "celebration_gif": "effects/4444.gif",
    "celebration_sound": "sounds/4444.MP3",
    "achievement_label": "Ð¦Ð•Ð›Ð¬ Ð”ÐžÐ¡Ð¢Ð˜Ð“ÐÐ£Ð¢Ð!",
    "achievement_text": "Ð¡ÐŸÐÐ¡Ð˜Ð‘Ðž Ð—Ð ÐŸÐžÐ”Ð”Ð•Ð Ð–ÐšÐ£!",
    "achievement_sound": "sounds/minecraft-rare-achievement.mp3",
    "completion_count": 0,
}

DEFAULT_GOAL["achievement_label"] = "\u0426\u0415\u041b\u042c \u0414\u041e\u0421\u0422\u0418\u0413\u041d\u0423\u0422\u0410!"
DEFAULT_GOAL["achievement_text"] = "\u0421\u041f\u0410\u0421\u0418\u0411\u041e \u0417\u0410 \u041f\u041e\u0414\u0414\u0415\u0420\u0416\u041a\u0423!"

last_donate = {}
donate_totals = {}
goal_cache = None


def safe_print(*values, **kwargs):
    sep = kwargs.pop("sep", " ")
    end = kwargs.pop("end", "\n")
    file = kwargs.pop("file", None)
    flush = kwargs.pop("flush", False)
    text = sep.join(str(value) for value in values)

    try:
        print(text, end=end, file=file, flush=flush)
    except UnicodeEncodeError:
        safe_text = text.encode("ascii", "backslashreplace").decode("ascii")
        print(safe_text, end=end, file=file, flush=flush)


def load_json_file(path, default):
    if not path.exists():
        return deepcopy(default)

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return deepcopy(default)


def save_json_file(path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        return True
    except OSError as error:
        safe_print(f"JSON SAVE WARNING: {path}: {error}")
        return False


def coerce_int(value, fallback=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def format_amount(value):
    return f"{value:,}".replace(",", " ")


def sanitize_name(value, fallback="Аноним"):
    clean_value = str(value).strip()
    return clean_value or fallback


def sanitize_message(value):
    return str(value or "").strip()


def sanitize_goal_text(value, fallback):
    clean_value = str(value or "").strip()

    if not clean_value:
        return fallback

    if "Ã" in clean_value or "Ð" in clean_value:
        return fallback

    return clean_value


def normalize_media_path(path_value, media_type):
    clean_value = str(path_value or "").strip().replace("\\", "/").lstrip("/")
    if not clean_value:
        return ""

    for prefix in ("media/", "assets/"):
        if clean_value.startswith(prefix):
            clean_value = clean_value[len(prefix):]

    if "/" in clean_value:
        return clean_value

    if media_type == "gif":
        return f"effects/{clean_value}"
    if media_type == "sound":
        return f"sounds/{clean_value}"
    if media_type == "icon":
        return f"icons/{clean_value}"
    if media_type == "tts":
        return f"tts/{clean_value}"

    return clean_value


def load_aliases():
    aliases = load_json_file(ALIASES_FILE, {})
    return aliases if isinstance(aliases, dict) else {}


def load_donate_totals():
    raw_totals = load_json_file(DATA_FILE, {})
    totals = {}

    if not isinstance(raw_totals, dict):
        return totals

    for name, total in raw_totals.items():
        clean_name = str(name).strip() or "Аноним"
        totals[clean_name] = max(0, coerce_int(total))

    return totals


def save_totals():
    save_json_file(DATA_FILE, donate_totals)


def normalize_goal_data(raw_goal):
    goal = deepcopy(DEFAULT_GOAL)

    if isinstance(raw_goal, dict):
        goal["title"] = str(raw_goal.get("title", goal["title"])).strip() or goal["title"]
        goal["subtitle"] = str(raw_goal.get("subtitle", goal["subtitle"])).strip()
        goal["currency"] = str(raw_goal.get("currency", goal["currency"])).strip() or goal["currency"]
        goal["target"] = max(1, coerce_int(raw_goal.get("target"), goal["target"]))
        goal["current"] = max(0, coerce_int(raw_goal.get("current"), goal["current"]))
        goal["achievement_label"] = sanitize_goal_text(
            raw_goal.get("achievement_label", goal["achievement_label"]),
            goal["achievement_label"],
        )
        goal["achievement_text"] = sanitize_goal_text(
            raw_goal.get("achievement_text", goal["achievement_text"]),
            goal["achievement_text"],
        )
        goal["celebration_gif"] = (
            normalize_media_path(raw_goal.get("celebration_gif", goal["celebration_gif"]), "gif")
            or goal["celebration_gif"]
        )
        goal["celebration_sound"] = (
            normalize_media_path(raw_goal.get("celebration_sound", goal["celebration_sound"]), "sound")
            or goal["celebration_sound"]
        )
        goal["achievement_sound"] = (
            normalize_media_path(
                raw_goal.get("achievement_sound", raw_goal.get("celebration_sound", goal["achievement_sound"])),
                "sound",
            )
            or goal["achievement_sound"]
        )
        goal["completion_count"] = max(
            0,
            coerce_int(raw_goal.get("completion_count"), goal["completion_count"]),
        )

    goal["goal_reached"] = goal["current"] >= goal["target"]
    return goal


def load_goal_data():
    global goal_cache

    if RUNNING_ON_VERCEL and goal_cache is not None:
        return deepcopy(goal_cache)

    raw_goal = load_json_file(GOAL_FILE, DEFAULT_GOAL)
    goal = normalize_goal_data(raw_goal)
    goal_cache = goal

    if raw_goal != goal:
        save_json_file(GOAL_FILE, goal)

    return deepcopy(goal)


def save_goal_data(goal):
    global goal_cache

    goal_cache = normalize_goal_data(goal)
    save_json_file(GOAL_FILE, goal_cache)


def build_goal_payload(goal):
    raw_percent = (goal["current"] / goal["target"] * 100) if goal["target"] else 0
    remaining = max(goal["target"] - goal["current"], 0)
    overflow = max(goal["current"] - goal["target"], 0)

    payload = dict(goal)
    payload.update(
        {
            "remaining": remaining,
            "overflow": overflow,
            "progress_percent": round(min(raw_percent, 100), 2),
            "progress_percent_raw": round(raw_percent, 2),
            "display_current": format_amount(goal["current"]),
            "display_target": format_amount(goal["target"]),
            "display_remaining": format_amount(remaining),
            "display_overflow": format_amount(overflow),
        }
    )
    return payload


def apply_goal_donation(amount_number):
    goal = load_goal_data()
    previous_current = goal["current"]

    goal["current"] = max(0, previous_current + amount_number)
    goal["goal_reached"] = goal["current"] >= goal["target"]

    just_completed = previous_current < goal["target"] <= goal["current"]
    if just_completed:
        goal["completion_count"] += 1

    save_goal_data(goal)
    return goal, just_completed


def get_top_donors(limit=5):
    positive_totals = [
        (name, total) for name, total in donate_totals.items() if coerce_int(total) > 0
    ]
    sorted_donors = sorted(positive_totals, key=lambda item: item[1], reverse=True)
    return sorted_donors[:limit]


def choose_effects(amount_number):
    amount_text = str(amount_number)

    if len(set(amount_text)) == 1 and amount_text[0] == "4":
        return "effects/4444.gif", "sounds/4444.MP3"

    if amount_number == 404:
        return "effects/error.gif", "sounds/error.mp3"

    if amount_text.startswith("4") and "0" in amount_text:
        return "effects/fantastic-four.gif", "sounds/fantastic-four.mp3"

    if amount_number >= 1000:
        return "effects/the-goat.gif", "sounds/cr7-the-goat.mp3"

    if amount_number >= 500:
        return "effects/500.gif", "sounds/500.mp3"

    return "effects/kiss-ronaldo.gif", "sounds/kiss-ronaldo.MP3"


def save_tts_with_edge_tts(tts_text, tts_file):
    try:
        import edge_tts
    except ImportError:
        return False

    async def save_audio():
        communicate = edge_tts.Communicate(tts_text, "ru-RU-DmitryNeural")
        await communicate.save(str(tts_file))

    asyncio.run(save_audio())
    return True


def resolve_tts_file(media_path):
    clean_path = normalize_media_path(media_path, "tts")

    if not clean_path.startswith("tts/"):
        return None

    tts_filename = clean_path.split("/", 1)[1].strip()
    if not tts_filename:
        return None

    return TTS_STORAGE_DIR / tts_filename


def build_tts_data_url(media_path):
    tts_file = resolve_tts_file(media_path)

    if not tts_file or not tts_file.exists():
        return ""

    try:
        encoded_audio = base64.b64encode(tts_file.read_bytes()).decode("ascii")
    except OSError as error:
        safe_print("TTS READ ERROR:", error)
        return ""

    return f"data:audio/mpeg;base64,{encoded_audio}"


def generate_tts_file(name, amount_text, message):
    if message:
        tts_text = f"{name} задонатил {amount_text}. {message}"
    else:
        tts_text = f"{name} задонатил {amount_text}"

    tts_filename = f"tts_{uuid.uuid4().hex}.mp3"
    tts_file = TTS_STORAGE_DIR / tts_filename

    try:
        TTS_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        if not save_tts_with_edge_tts(tts_text, tts_file):
            subprocess.run(
                [
                    "edge-tts",
                    "--voice",
                    "ru-RU-DmitryNeural",
                    "--text",
                    tts_text,
                    "--write-media",
                    str(tts_file),
                ],
                check=True,
                timeout=20,
            )
    except Exception as error:
        safe_print("TTS ERROR:", error)
        return ""

    if not tts_file.exists():
        return ""

    return normalize_media_path(tts_filename, "tts")


def process_donation(name, amount_number, message="", amount_text=None, apply_alias=True):
    global last_donate, donate_totals

    clean_name = sanitize_name(name)
    clean_message = sanitize_message(message)
    clean_amount = max(0, coerce_int(amount_number))
    clean_amount_text = amount_text or f"{format_amount(clean_amount)} ₸"

    if apply_alias:
        aliases = load_aliases()
        if clean_name in aliases:
            safe_print(f"ALIAS APPLIED: {clean_name} -> {aliases[clean_name]}")
            clean_name = aliases[clean_name]

    donate_totals[clean_name] = donate_totals.get(clean_name, 0) + clean_amount
    save_totals()

    goal_data, goal_completed = apply_goal_donation(clean_amount)
    gif_file, sound_file = choose_effects(clean_amount)
    tts_file = generate_tts_file(clean_name, clean_amount_text, clean_message)

    last_donate = {
        "id": uuid.uuid4().hex,
        "created_at": int(time.time() * 1000),
        "amount": clean_amount_text,
        "amount_number": clean_amount,
        "name": clean_name,
        "message": clean_message,
        "tts": tts_file,
        "tts_data_url": build_tts_data_url(tts_file),
        "gif": gif_file,
        "sound": sound_file,
        "goal_completed": goal_completed,
        "goal_completion_count": goal_data["completion_count"],
    }

    safe_print("NEW DONATE:", last_donate)
    return last_donate


@app.route("/donate", methods=["POST"])
def donate():
    raw_text = request.form.get("text", "")
    text = unquote_plus(raw_text)

    safe_print("DECODED TEXT:\n", text)

    if not text.strip():
        return jsonify({"status": "empty"}), 200

    amount_match = re.search(r"Пополнение:\s*([\d\s]+(?:\s*[^\d\s]+)?)", text)
    amount = amount_match.group(1).strip() if amount_match else "0 ₸"
    amount_number = int(re.sub(r"\D", "", amount)) if re.sub(r"\D", "", amount) else 0

    name = "Аноним"
    message = ""

    lines = [line.strip() for line in text.split("\n") if line.strip()]

    for index, line in enumerate(lines):
        if ":" in line and "Пополнение" not in line:
            parts = line.split(":", 1)
            name = parts[0].strip()
            message = parts[1].strip()
            break

        if "Пополнение" not in line and index > 0:
            name = line.strip()
            break

    process_donation(name=name, amount_number=amount_number, message=message, amount_text=amount)
    return jsonify({"status": "ok"})


@app.route("/last")
def last():
    response = jsonify(last_donate)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.route("/top")
def top():
    return jsonify(get_top_donors())


@app.route("/goal")
def goal():
    return jsonify(build_goal_payload(load_goal_data()))


@app.route("/goal/update", methods=["POST"])
def goal_update():
    payload = request.get_json(silent=True) or request.form
    current_goal = load_goal_data()
    updated_goal = dict(current_goal)

    updated_goal["title"] = sanitize_name(
        payload.get("title", current_goal["title"]),
        fallback=current_goal["title"],
    )
    updated_goal["target"] = max(1, coerce_int(payload.get("target"), current_goal["target"]))
    updated_goal["current"] = max(0, coerce_int(payload.get("current"), current_goal["current"]))
    updated_goal["subtitle"] = sanitize_message(payload.get("subtitle", current_goal["subtitle"]))

    save_goal_data(updated_goal)
    return jsonify({"status": "ok", "goal": build_goal_payload(load_goal_data())})


@app.route("/test-donate", methods=["POST"])
def test_donate():
    payload = request.get_json(silent=True) or request.form
    amount_number = max(0, coerce_int(payload.get("amount"), 0))

    if amount_number <= 0:
        return jsonify({"status": "error", "message": "amount must be greater than zero"}), 400

    donation = process_donation(
        name=payload.get("name", "Тестовый донат"),
        amount_number=amount_number,
        message=payload.get("message", ""),
        amount_text=f"{format_amount(amount_number)} ₸",
    )

    return jsonify({"status": "ok", "donation": donation, "goal": build_goal_payload(load_goal_data())})


@app.route("/goal/test-achievement", methods=["POST"])
def goal_test_achievement():
    goal_data = load_goal_data()
    goal_data["completion_count"] = max(0, coerce_int(goal_data.get("completion_count"), 0)) + 1
    save_goal_data(goal_data)
    return jsonify({"status": "ok", "goal": build_goal_payload(load_goal_data())})


def serve_static_html(filename):
    response = send_from_directory(str(STATIC_DIR), filename)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.route("/overlay")
def overlay():
    return serve_static_html("overlay.html")


@app.route("/topview")
def topview():
    return serve_static_html("top.html")


@app.route("/goalview")
def goalview():
    return serve_static_html("goal.html")


@app.route("/dashboard")
def dashboard():
    return serve_static_html("dashboard.html")


@app.route("/media/<path:filename>")
def media(filename):
    if filename.startswith("tts/"):
        tts_filename = filename.split("/", 1)[1].strip()
        if tts_filename:
            return send_from_directory(str(TTS_STORAGE_DIR), tts_filename)

    return send_from_directory(str(ASSETS_DIR), filename)


@app.route("/health")
def home():
    return "Kaspi Donate PRO FULL running 🔥"


@app.route("/")
def root():
    return serve_static_html("dashboard.html")


donate_totals = load_donate_totals()
load_goal_data()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
