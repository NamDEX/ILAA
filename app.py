import os
import sys
import json
import time
import uuid
import sqlite3
import webbrowser
import traceback
import subprocess
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple, Optional

# =========================================================
# AUTO-BOOTSTRAP (non-coder friendly)
# - Installs requirements.txt using the SAME interpreter
#   that is running this script (PyCharm-safe).
# =========================================================

def _run(cmd: List[str]) -> int:
    return subprocess.call(cmd)

def _ensure_deps():
    try:
        import flask  # noqa: F401
        return
    except Exception:
        pass

    req_path = os.path.join(os.path.dirname(__file__), "requirements.txt")
    if not os.path.exists(req_path):
        print("[BOOT] requirements.txt not found. Please create it first.")
        sys.exit(1)

    print("[BOOT] Installing dependencies from requirements.txt...")
    rc = _run([sys.executable, "-m", "pip", "install", "-r", req_path])
    if rc != 0:
        print("[BOOT] pip install failed. Please check your internet connection and PyCharm interpreter.")
        sys.exit(1)

_ensure_deps()

from flask import Flask, request, redirect, url_for, render_template, jsonify, abort  # noqa: E402

# =========================================================
# CONFIG + PATHS
# =========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

def load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        print("[ERROR] config.json not found next to app.py")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

CFG = load_config()

DATA_DIR = os.path.join(BASE_DIR, CFG["paths"]["data_dir"])
LOGS_DIR = os.path.join(BASE_DIR, CFG["paths"]["logs_dir"])
EXPORTS_DIR = os.path.join(BASE_DIR, CFG["paths"]["exports_dir"])
DB_PATH = os.path.join(DATA_DIR, "attempts.sqlite")

QUESTION_BANK_ROOT = CFG["paths"]["question_bank_root"]
NEVER_WRITE_TO_SOURCE = bool(CFG["paths"].get("never_write_to_source", True))

# =========================================================
# LOGGING (console + file)
# =========================================================

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    os.makedirs(EXPORTS_DIR, exist_ok=True)

def log(msg: str, level: str = "INFO"):
    cfg_level = CFG["logging"].get("level", "INFO").upper()
    levels = ["DEBUG", "INFO", "WARN", "ERROR"]
    if levels.index(level.upper()) < levels.index(cfg_level):
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [{level.upper()}] {msg}"
    if CFG["logging"].get("to_console", True):
        print(line)

    if CFG["logging"].get("to_file", True):
        try:
            with open(os.path.join(LOGS_DIR, CFG["logging"].get("file_name", "app.log")), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            # avoid crashing due to logging problems
            pass

# =========================================================
# DB
# =========================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS attempts (
        id TEXT PRIMARY KEY,
        set_id TEXT NOT NULL,
        question_uid TEXT NOT NULL,
        topic TEXT NOT NULL,
        tags_json TEXT NOT NULL,
        selected TEXT,
        correct TEXT NOT NULL,
        is_correct INTEGER NOT NULL,
        diagnostic TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS practice_sets (
        id TEXT PRIMARY KEY,
        mode TEXT NOT NULL,
        topic TEXT,
        timer_enabled INTEGER NOT NULL,
        duration_seconds INTEGER NOT NULL,
        start_time TEXT NOT NULL,
        due_time TEXT NOT NULL,
        questions_json TEXT NOT NULL,
        answers_json TEXT NOT NULL,
        is_submitted INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    # Migration for Feature 1: is_cancelled
    try:
        cur.execute("ALTER TABLE practice_sets ADD COLUMN is_cancelled INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        # Column likely already exists
        pass

    conn.commit()
    conn.close()

# =========================================================
# QUESTION BANK LOADER (read-only, recursive)
# =========================================================

def safe_is_under_root(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except Exception:
        return False

def find_json_files(root: str) -> List[str]:
    files = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn.lower().endswith(".json"):
                files.append(os.path.join(dirpath, fn))
    return files

def normalise_topic(meta: Dict[str, Any], fallback_path: str) -> str:
    t = (meta.get("topic") or "").strip()
    if t:
        return t
    # fallback: folder name if missing
    return os.path.basename(os.path.dirname(fallback_path)) or "Unknown"

def compute_question_uid(source_path: str, qid: Any) -> str:
    # Stable UID: file path + question id
    return f"{source_path}::Q{qid}"

def load_question_bank() -> Dict[str, Any]:
    """
    Returns:
      {
        "questions": [ {uid, topic, tags, stem, options, correct, diagnostics_map, source_path, qid, level} ],
        "topics": sorted list,
        "levels": sorted list,
        "diagnostic_legend": {code: meaning},
        "stats": {files_loaded, questions_loaded, errors}
      }
    """
    if NEVER_WRITE_TO_SOURCE and not safe_is_under_root(QUESTION_BANK_ROOT, os.path.dirname(QUESTION_BANK_ROOT)):
        # This is just a sanity log; we do not modify anything anyway.
        log("Source bank root path sanity check failed (still proceeding read-only).", "WARN")

    if not os.path.exists(QUESTION_BANK_ROOT):
        raise FileNotFoundError(f"Question bank root does not exist: {QUESTION_BANK_ROOT}")

    json_files = find_json_files(QUESTION_BANK_ROOT)
    log(f"Found {len(json_files)} JSON files under bank root.", "INFO")

    out_questions = []
    topics = set()
    levels = set()
    diagnostic_legend = {}
    errors = 0

    for fp in json_files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)

            meta = data.get("meta", {})
            topic = normalise_topic(meta, fp)
            # Feature 4: Capture level
            level = (meta.get("level") or "").strip()
            if level:
                levels.add(level)

            # Feature 5: Capture diagnostic codes legend
            codes = meta.get("diagnostic_codes", {})
            if isinstance(codes, dict):
                for k, v in codes.items():
                    # Last writer wins or first? User said: "If the same code appears with different meanings... log a warning and keep the first."
                    k_norm = k.strip()
                    if k_norm and k_norm not in diagnostic_legend:
                        diagnostic_legend[k_norm] = v.strip()
                    elif k_norm and diagnostic_legend[k_norm] != v.strip():
                        log(f"Diagnostic code conflict for '{k_norm}' in {fp}. Keeping original.", "WARN")

            questions = data.get("questions", [])

            if not isinstance(questions, list):
                log(f"Skipping file (questions not a list): {fp}", "WARN")
                continue

            for q in questions:
                # Basic validation based on your confirmed rules:
                # - MCQ only
                # - A–E options
                # - single correct answer stored as letter
                qid = q.get("id")
                stem = (q.get("question") or q.get("stem") or q.get("prompt") or "").strip()
                correct = (q.get("correct") or "").strip().upper()
                options = q.get("options", {})

                if correct not in ["A", "B", "C", "D", "E"]:
                    log(f"Skipping question with invalid correct answer '{correct}' in {fp} id={qid}", "WARN")
                    continue

                if not isinstance(options, dict):
                    log(f"Skipping question with invalid options in {fp} id={qid}", "WARN")
                    continue

                letters = ["A", "B", "C", "D", "E"]
                if any(l not in options for l in letters):
                    log(f"Skipping question missing A–E options in {fp} id={qid}", "WARN")
                    continue

                tags = q.get("tags", [])
                if not isinstance(tags, list):
                    tags = []

                uid = compute_question_uid(fp, qid)
                topics.add(topic)

                # Build per-option diagnostic map (for analytics)
                diagnostics_map = {}
                for l in letters:
                    opt = options.get(l, {})
                    if isinstance(opt, dict):
                        diagnostics_map[l] = (opt.get("diagnostic") or "").strip()
                    else:
                        diagnostics_map[l] = ""

                out_questions.append({
                    "uid": uid,
                    "qid": qid,
                    "topic": topic,
                    "level": level, # Feature 4
                    "tags": tags,
                    "stem": stem,
                    "options": {
                        l: (
                            (options[l].get("value") or options[l].get("text") or options[l].get("label") or "").strip()
                            if isinstance(options[l], dict)
                            else str(options[l]).strip()
                        )
                        for l in letters
                    },
                    "correct": correct,
                    "diagnostics_map": diagnostics_map,
                    "source_path": fp
                })

        except Exception as e:
            errors += 1
            log(f"Error loading {fp}: {e}", "ERROR")
            if CFG["logging"].get("level", "INFO").upper() == "DEBUG":
                log(traceback.format_exc(), "ERROR")

    # Sort levels naturally if possible (e.g. 4, 5, 4-5) - string sort is okay for now
    sorted_levels = sorted(list(levels))

    out = {
        "questions": out_questions,
        "topics": sorted(list(topics)),
        "levels": sorted_levels,
        "diagnostic_legend": diagnostic_legend,
        "stats": {
            "files_found": len(json_files),
            "questions_loaded": len(out_questions),
            "errors": errors
        }
    }
    log(f"Loaded {out['stats']['questions_loaded']} questions across {len(out['topics'])} topics and {len(out['levels'])} levels.", "INFO")
    return out

QB = None  # loaded on startup

# =========================================================
# SELECTION LOGIC (topic + mixed with overdue + wrong-boost)
# =========================================================

def now_utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def parse_iso(dt_str: str) -> datetime:
    # supports stored Z format
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1]
    return datetime.fromisoformat(dt_str)

def get_last_practised_by_topic(conn) -> Dict[str, Optional[datetime]]:
    cur = conn.cursor()
    cur.execute("""
      SELECT topic, MAX(created_at) AS last_at
      FROM attempts
      GROUP BY topic
    """)
    out = {}
    for row in cur.fetchall():
        out[row["topic"]] = parse_iso(row["last_at"]) if row["last_at"] else None
    return out

def get_recent_wrong_uids(conn, days_window: int, attempts_window: int) -> set:
    cur = conn.cursor()
    # recent wrong by days
    cutoff = datetime.utcnow() - timedelta(days=days_window)
    cutoff_iso = cutoff.replace(microsecond=0).isoformat() + "Z"

    cur.execute("""
      SELECT question_uid
      FROM attempts
      WHERE is_correct = 0 AND created_at >= ?
    """, (cutoff_iso,))
    wrong_days = {r["question_uid"] for r in cur.fetchall()}

    # recent wrong by attempts window (last N attempts)
    cur.execute("""
      SELECT question_uid, is_correct
      FROM attempts
      ORDER BY created_at DESC
      LIMIT ?
    """, (attempts_window,))
    wrong_recent = {r["question_uid"] for r in cur.fetchall() if r["is_correct"] == 0}

    return wrong_days.union(wrong_recent)

def get_recent_correct_uids(conn, days_window: int, attempts_window: int) -> set:
    cur = conn.cursor()
    cutoff = datetime.utcnow() - timedelta(days=days_window)
    cutoff_iso = cutoff.replace(microsecond=0).isoformat() + "Z"

    cur.execute("""
      SELECT question_uid
      FROM attempts
      WHERE is_correct = 1 AND created_at >= ?
    """, (cutoff_iso,))
    correct_days = {r["question_uid"] for r in cur.fetchall()}

    cur.execute("""
      SELECT question_uid, is_correct
      FROM attempts
      ORDER BY created_at DESC
      LIMIT ?
    """, (attempts_window,))
    correct_recent = {r["question_uid"] for r in cur.fetchall() if r["is_correct"] == 1}

    return correct_days.union(correct_recent)

def get_recent_set_uids(conn, n_sets: int) -> set:
    cur = conn.cursor()
    cur.execute("""
      SELECT questions_json
      FROM practice_sets
      WHERE is_submitted = 1
      ORDER BY created_at DESC
      LIMIT ?
    """, (n_sets,))
    uids = set()
    for row in cur.fetchall():
        try:
            qs = json.loads(row["questions_json"])
            for q in qs:
                uids.add(q.get("uid"))
        except Exception:
            pass
    return uids

def pick_questions_topic(topic: str, set_size: int, difficulty: str = None) -> List[Dict[str, Any]]:
    # Feature 4: Filter by difficulty if provided
    pool = [
        q for q in QB["questions"]
        if q["topic"] == topic and (not difficulty or q["level"] == difficulty)
    ]
    if len(pool) <= set_size:
        return pool[:]
    # simple shuffle without random module seed requirements: use uuid-based ordering
    pool_sorted = sorted(pool, key=lambda _: uuid.uuid4().hex)
    return pool_sorted[:set_size]

def weighted_sample(questions: List[Dict[str, Any]], weights: List[float], k: int) -> List[Dict[str, Any]]:
    # Simple weighted sample without replacement
    chosen = []
    items = list(zip(questions, weights))
    for _ in range(min(k, len(items))):
        total = sum(w for _, w in items)
        if total <= 0:
            break
        r = (int(uuid.uuid4().hex, 16) % 10_000_000) / 10_000_000.0 * total
        acc = 0.0
        idx = None
        for i, (q, w) in enumerate(items):
            acc += w
            if acc >= r:
                idx = i
                break
        if idx is None:
            idx = 0
        q_sel, _ = items.pop(idx)
        chosen.append(q_sel)
    return chosen

def pick_questions_mixed(set_size: int, difficulty: str = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Returns (questions, overdue_topics_list)
    """
    conn = db_connect()
    last_by_topic = get_last_practised_by_topic(conn)
    overdue_days = int(CFG["practice"]["overdue_days"])
    overdue_cutoff = datetime.utcnow() - timedelta(days=overdue_days)

    overdue_topics = []
    for t in QB["topics"]:
        last = last_by_topic.get(t)
        if last is None or last < overdue_cutoff:
            overdue_topics.append(t)

    quota = int(round(set_size * float(CFG["mixed_mode"]["overdue_quota_fraction"])))
    quota = max(0, min(quota, set_size))

    recent_wrong_uids = get_recent_wrong_uids(
        conn,
        int(CFG["mixed_mode"]["recent_wrong_days_window"]),
        int(CFG["mixed_mode"]["recent_wrong_attempts_window"])
    )
    recent_correct_uids = get_recent_correct_uids(
        conn,
        int(CFG["mixed_mode"]["recent_wrong_days_window"]),
        int(CFG["mixed_mode"]["recent_wrong_attempts_window"])
    )

    avoid_uids = get_recent_set_uids(conn, int(CFG["mixed_mode"]["avoid_repeat_within_last_n_sets"]))
    conn.close()

    # Build pools with Difficulty Filter (Feature 4)
    all_questions = [
        q for q in QB["questions"]
        if q["uid"] not in avoid_uids and (not difficulty or q["level"] == difficulty)
    ]
    if len(all_questions) < set_size:
        # fallback if too restrictive: relax avoidance but keep difficulty if possible
        all_questions = [
            q for q in QB["questions"]
            if (not difficulty or q["level"] == difficulty)
        ]
        # if still too small, just use everything matching difficulty
        if len(all_questions) == 0 and difficulty:
             # absolute fallback: ignore difficulty to give something
             all_questions = QB["questions"][:]

    overdue_pool = [q for q in all_questions if q["topic"] in overdue_topics] if overdue_topics else []

    picked = []

    # 1) Overdue quota
    if quota > 0 and overdue_pool:
        overdue_weights = []
        for q in overdue_pool:
            w = 1.0 * float(CFG["mixed_mode"]["weight_overdue_multiplier"])
            if q["uid"] in recent_wrong_uids:
                w *= float(CFG["mixed_mode"]["weight_recent_wrong_multiplier"])
            if q["uid"] in recent_correct_uids:
                w *= float(CFG["mixed_mode"]["weight_recent_correct_multiplier"])
            overdue_weights.append(w)
        picked.extend(weighted_sample(overdue_pool, overdue_weights, quota))

    # 2) Fill remainder from all_questions (weighted)
    remaining = set_size - len(picked)
    if remaining > 0:
        remaining_pool = [q for q in all_questions if q["uid"] not in {p["uid"] for p in picked}]
        weights = []
        for q in remaining_pool:
            w = 1.0
            if q["topic"] in overdue_topics:
                w *= float(CFG["mixed_mode"]["weight_overdue_multiplier"])
            if q["uid"] in recent_wrong_uids:
                w *= float(CFG["mixed_mode"]["weight_recent_wrong_multiplier"])
            if q["uid"] in recent_correct_uids:
                w *= float(CFG["mixed_mode"]["weight_recent_correct_multiplier"])
            weights.append(w)
        picked.extend(weighted_sample(remaining_pool, weights, remaining))

    # final fallback if something odd happens
    if len(picked) < set_size:
        fallback = [
            q for q in QB["questions"]
            if q["uid"] not in {p["uid"] for p in picked} and (not difficulty or q["level"] == difficulty)
        ]
        fallback_sorted = sorted(fallback, key=lambda _: uuid.uuid4().hex)
        picked.extend(fallback_sorted[: (set_size - len(picked))])

    return picked[:set_size], overdue_topics

# =========================================================
# TEMPLATES/STATIC (auto-created)
# =========================================================

TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>11+ Practice</title>
  <link rel="stylesheet" href="{{ url_for('static', filename='app.css') }}">
</head>
<body>
  <div class="container">
    <header class="header">
      <div>
        <h1>OHANA 11+ Academy with Stitch</h1>
        <p class="subtitle">Practice makes you PERFECT!</p>
      </div>
    </header>

    {% if ui.show_overdue_banner and overdue_topics %}
      <div class="card banner">
        <div class="banner-title">Recommended</div>
        <div class="banner-body">
          Overdue topics (not practised in {{ overdue_days }} days):
          <strong>{{ ", ".join(overdue_topics[:6]) }}{% if overdue_topics|length > 6 %}…{% endif %}</strong>
        </div>
      </div>
    {% endif %}

    <div class="card">
      <h2>Start a set</h2>

      <form method="post" action="{{ url_for('start_set') }}" class="form">
        <div class="row">
          <label class="label">Mode</label>
          <select name="mode" class="input" id="modeSelect">
            <option value="mixed" {% if default_mode == "mixed" %}selected{% endif %}>Mixed (recommended)</option>
            <option value="topic" {% if default_mode == "topic" %}selected{% endif %}>By topic</option>
          </select>
        </div>

        <!-- Feature 4: Difficulty Selector -->
        <div class="row">
          <label class="label">Level</label>
          <select name="difficulty" class="input">
            <option value="">All</option>
            {% for l in levels %}
              <option value="{{ l }}">{{ l }}</option>
            {% endfor %}
          </select>
        </div>

        <div class="row" id="topicRow" style="display:none;">
          <label class="label">Topic</label>
          <select name="topic" class="input">
            {% for t in topics %}
              <option value="{{ t }}">{{ t }}</option>
            {% endfor %}
          </select>
        </div>

        <div class="row">
          <label class="label">Timer</label>
          <div class="toggle">
            <input type="checkbox" id="timerToggle" name="timer_enabled" {% if timer_enabled_default %}checked{% endif %}/>
            <label for="timerToggle">On ({{ timer_minutes }} min)</label>
          </div>
          <div class="hint">Timer is subtle on-screen. Set auto-submits when time ends.</div>
        </div>

        <button class="btn primary" type="submit">Start</button>
      </form>
    </div>

    <div class="card small">
      <h3>Notes</h3>
      <ul>
        <li>No changes are ever made to your source question bank folder.</li>
        <li>Attempts and analytics are stored locally in <code>data/attempts.sqlite</code>.</li>
      </ul>
    </div>
  </div>

  <!-- Stitch Decorations -->
  <img src="https://media.tenor.com/yheo1GGu3FwAAAAj/stitch.gif" class="stitch-decoration left" alt="Stitch">
  <img src="https://media.tenor.com/yheo1GGu3FwAAAAj/stitch.gif" class="stitch-decoration right" alt="Stitch">

<style>
  .stitch-decoration {
    position: fixed;
    top: 50%;
    transform: translateY(-50%);
    width: 250px;
    z-index: -1;
    pointer-events: none; /* Let clicks pass through */
  }
  .stitch-decoration.left {
    left: 20px;
  }
  .stitch-decoration.right {
    right: 20px;
  }
  @media (max-width: 1400px) {
    .stitch-decoration {
      display: none;
    }
  }
</style>

<script>
  const modeSelect = document.getElementById('modeSelect');
  const topicRow = document.getElementById('topicRow');
  function updateModeUI() {
    topicRow.style.display = (modeSelect.value === 'topic') ? 'flex' : 'none';
  }
  modeSelect.addEventListener('change', updateModeUI);
  updateModeUI();
</script>
</body>
</html>
"""

PRACTISE_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Practice Set</title>
  <link rel="stylesheet" href="{{ url_for('static', filename='app.css') }}">
</head>
<body>
  <div class="container">
    <header class="header">
      <div>
        <h1>Practice</h1>
        <p class="subtitle">
          Question {{ index + 1 }} of {{ total }}{% if mode == "topic" %} · {{ topic }}{% else %} · Mixed{% endif %}
        </p>
      </div>
      <div style="display:flex; gap:8px;">
        <a href="{{ url_for('index') }}" class="btn secondary">Home</a>
      </div>
    </header>

    <!-- Feature 3: Question Grid -->
    {% if ui_show_grid %}
    <div class="card small" style="margin-bottom:12px; padding:8px;">
      <div class="grid-nav">
        {% for i in range(total) %}
          {% set st = status_map[i|string] %}
          <a href="{{ url_for('practice', set_id=set_id, index=i) }}"
             class="grid-cell {{ st }} {% if i == index %}current{% endif %}">
            {{ i + 1 }}
          </a>
        {% endfor %}
      </div>
    </div>
    {% endif %}

    <div class="card">
      <div class="stem">{{ q.stem }}</div>

      <form method="post" action="{{ url_for('answer_question', set_id=set_id) }}" class="options">
        {% for letter, text in q.options.items() %}
          <label class="option">
            <!-- Feature 2: Remove required -->
            <input type="radio" name="selected" value="{{ letter }}" {% if selected == letter %}checked{% endif %} />
            <span class="letter">{{ letter }}</span>
            <span class="text">{{ text }}</span>
          </label>
        {% endfor %}
        <input type="hidden" name="index" value="{{ index }}" />
        <div style="margin-top:12px; display:flex; justify-content:space-between; align-items:center;">
             <button class="btn" type="submit">Next</button>
        </div>
      </form>

      <!-- Feature 6: Timer at bottom of card -->
      {% if timer_enabled %}
      <div class="timer-subtle footer-timer" aria-label="timer">
        <div class="timer-bar">
          <div class="timer-bar-fill" id="timerFill"></div>
        </div>
        <div class="timer-text" id="timerText" title="Time remaining (subtle)"></div>
      </div>
      {% endif %}
    </div>

    <div class="actions" style="margin-top:20px; border-top:1px solid var(--border); padding-top:16px;">
       <!-- Feature 1: Cancel Button -->
       <a href="{{ url_for('cancel_set', set_id=set_id) }}" class="btn secondary" style="color:var(--bad);">Cancel Set</a>
    </div>

  </div>

{% if timer_enabled %}
<script>
  const dueEpochMs = {{ due_epoch_ms }};
  const durationMs = {{ duration_ms }};
  const fill = document.getElementById('timerFill');
  const text = document.getElementById('timerText');

  function fmt(ms) {
    const s = Math.max(0, Math.floor(ms/1000));
    const m = Math.floor(s/60);
    const r = s % 60;
    return m + ":" + (r < 10 ? "0"+r : r);
  }

  function tick() {
    const now = Date.now();
    const remaining = dueEpochMs - now;
    const elapsed = durationMs - remaining;
    const pct = Math.min(100, Math.max(0, (elapsed / durationMs) * 100));
    fill.style.width = pct + "%";

    // Feature 6: always show text (user request)
    text.textContent = fmt(remaining);

    if (remaining <= 0) {
      // Auto-submit when time expires
      fetch("{{ url_for('auto_submit', set_id=set_id) }}", {method: "POST"})
        .then(() => window.location.href = "{{ url_for('results', set_id=set_id) }}")
        .catch(() => window.location.href = "{{ url_for('results', set_id=set_id) }}");
      return;
    }
    requestAnimationFrame(tick);
  }
  tick();
</script>
{% endif %}
</body>
</html>
"""

RESULTS_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Results</title>
  <link rel="stylesheet" href="{{ url_for('static', filename='app.css') }}">
</head>
<body>
  <div class="container">
    <header class="header">
      <div>
        <h1>Results</h1>
        <p class="subtitle">{{ mode_title }}</p>
      </div>
    </header>

    <div class="card">
      <h2>Score</h2>
      <div class="score">{{ score }} / {{ total }}</div>
      <div class="muted">
        {% if timer_enabled %}Timed set{% else %}Untimed set{% endif %}
        · Completed at {{ finished_at }}
      </div>

      <div class="actions">
        <a class="btn primary" href="{{ url_for('index') }}">Home</a>
        <a class="btn secondary" href="{{ url_for('index') }}">New set</a>
      </div>
    </div>

    <div class="card">
      <h2>Answer script</h2>
      {% for row in script %}
        <div class="script-row">
          <div class="script-q">Q{{ loop.index }}.</div>
          <div class="script-stem">{{ row.stem }}</div>
          <div class="script-ans">
            <span class="pill {% if row.is_correct %}ok{% else %}bad{% endif %}">
              Your: {{ row.selected or "Skipped" }} · Correct: {{ row.correct }}
            </span>
            {% if row.diagnostic and not row.is_correct %}
              <span class="pill warn">Code: {{ row.diagnostic }}</span>
              <!-- Feature 5: Show meaning inline -->
              {% if row.diagnostic_text %}
                 <div style="margin-top:4px; font-size:13px; color:var(--warn);">Meaning: {{ row.diagnostic_text }}</div>
              {% endif %}
            {% endif %}
          </div>
        </div>
      {% endfor %}
    </div>

    <div class="card">
      <h2>Analytics (quick)</h2>
      <div class="grid">
        <div>
          <h3>Weak topics (this set)</h3>
          {% if weak_topics %}
            <ul>
              {% for t, n in weak_topics %}
                <li>{{ t }}: {{ n }} incorrect</li>
              {% endfor %}
            </ul>
          {% else %}
            <div class="muted">No weak topics detected in this set.</div>
          {% endif %}
        </div>
        <div>
          <h3>Top misconception codes</h3>
          {% if top_codes %}
            <ul>
              {% for c, n in top_codes %}
                <li>{{ c }}: {{ n }}</li>
              {% endfor %}
            </ul>
          {% else %}
            <div class="muted">No misconception codes recorded.</div>
          {% endif %}
        </div>
      </div>

      <!-- Feature 5: Legend -->
      {% if legend_items %}
      <div style="margin-top:20px; padding-top:12px; border-top:1px solid var(--border);">
         <h3>Legend</h3>
         <ul style="font-size:14px; color:var(--muted);">
           {% for code, text in legend_items %}
             <li><strong>{{ code }}</strong>: {{ text }}</li>
           {% endfor %}
         </ul>
      </div>
      {% endif %}

    </div>
  </div>
</body>
</html>
"""

APP_CSS = r"""
:root {
  --bg: #0b0f17;
  --card: #141b27;
  --text: #e8eefc;
  --muted: #a9b5d1;
  --accent: #6aa3ff;
  --ok: #2bd576;
  --bad: #ff5f6d;
  --warn: #ffc857;
  --border: rgba(255,255,255,0.08);
}

* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
}

.container {
  max-width: 980px;
  margin: 0 auto;
  padding: 16px;
}

.header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
  margin-bottom: 12px;
}

h1 { font-size: 24px; margin: 0; }
h2 { margin: 0 0 10px 0; font-size: 18px; }
h3 { margin: 0 0 8px 0; font-size: 16px; }

.subtitle { margin: 6px 0 0 0; color: var(--muted); }

.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 14px;
  margin: 12px 0;
}

.card.small { padding: 12px; }

.banner { border-left: 4px solid var(--accent); }
.banner-title { font-weight: 700; margin-bottom: 6px; }
.banner-body { color: var(--muted); }

.form { display: flex; flex-direction: column; gap: 12px; }
.row { display: flex; gap: 12px; align-items: center; }
.label { width: 90px; color: var(--muted); }
.input {
  flex: 1;
  padding: 10px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: #0f1521;
  color: var(--text);
}
.toggle { display: flex; align-items: center; gap: 10px; }
.hint { font-size: 12px; color: var(--muted); margin-left: 90px; }

.btn {
  display: inline-flex;
  justify-content: center;
  align-items: center;
  padding: 10px 14px;
  background: var(--accent);
  color: #06101f;
  border: none;
  border-radius: 12px;
  font-weight: 700;
  cursor: pointer;
  text-decoration: none;
}
.btn.primary {
  background: #2bd576; /* Sleek Green */
  color: #06101f;
}
.btn.secondary {
  background: transparent;
  color: var(--text);
  border: 1px solid var(--border);
}

.stem {
  font-size: 18px;
  line-height: 1.35;
  margin-bottom: 12px;
}

.options { display: flex; flex-direction: column; gap: 10px; }
.option {
  display: flex;
  gap: 12px;
  padding: 10px;
  border-radius: 12px;
  border: 1px solid var(--border);
  background: rgba(255,255,255,0.03);
  cursor: pointer;
}
.option input { margin-top: 3px; }
.letter {
  width: 26px; height: 26px;
  border-radius: 9px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border);
  color: var(--muted);
  flex-shrink: 0;
}
.text { color: var(--text); }

.score { font-size: 36px; font-weight: 800; margin: 8px 0; }
.muted { color: var(--muted); }

.actions { margin-top: 12px; display: flex; gap: 10px; flex-wrap: wrap; }

.timer-subtle { min-width: 180px; }
.timer-bar {
  width: 180px;
  height: 6px;
  border-radius: 999px;
  background: rgba(255,255,255,0.08);
  overflow: hidden;
  margin-top: 6px;
}
.timer-bar-fill {
  height: 100%;
  width: 0%;
  background: rgba(106,163,255,0.9);
}
.timer-text { margin-top: 6px; font-size: 12px; color: var(--muted); text-align: right; }

.script-row {
  padding: 10px 0;
  border-top: 1px solid var(--border);
}
.script-row:first-child { border-top: none; }
.script-q { color: var(--muted); font-weight: 700; margin-bottom: 6px; }
.script-stem { margin-bottom: 6px; }
.script-ans { display: flex; gap: 8px; flex-wrap: wrap; }

.pill {
  padding: 6px 10px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: rgba(255,255,255,0.03);
  font-size: 12px;
}
.pill.ok { border-color: rgba(43,213,118,0.4); }
.pill.bad { border-color: rgba(255,95,109,0.5); }
.pill.warn { border-color: rgba(255,200,87,0.5); }

.grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

/* Feature 3: Grid Styles */
.grid-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.grid-cell {
  width: 32px; height: 32px;
  border-radius: 6px;
  border: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: center;
  text-decoration: none;
  font-weight: 700;
  font-size: 14px;
  color: var(--muted);
  background: rgba(255,255,255,0.03);
}
.grid-cell.answered {
  background: rgba(106, 163, 255, 0.2); /* faint accent */
  color: var(--text);
  border-color: rgba(106, 163, 255, 0.4);
}
.grid-cell.skipped {
  background: rgba(255, 255, 255, 0.08);
  color: var(--text);
  border-style: dashed;
}
.grid-cell.current {
  border-color: var(--accent);
  color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent);
}

.footer-timer {
  margin-top: 16px;
  border-top: 1px solid var(--border);
  padding-top: 12px;
}

@media (max-width: 740px) {
  .header { flex-direction: column; align-items: stretch; }
  .label { width: 80px; }
  .hint { margin-left: 0; }
  .grid { grid-template-columns: 1fr; }
  .timer-subtle { min-width: auto; }
  .timer-bar { width: 100%; }
}
"""

def ensure_template_files():
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    os.makedirs(STATIC_DIR, exist_ok=True)

    tpl_map = {
        os.path.join(TEMPLATES_DIR, "index.html"): INDEX_HTML,
        os.path.join(TEMPLATES_DIR, "practice.html"): PRACTISE_HTML,
        os.path.join(TEMPLATES_DIR, "results.html"): RESULTS_HTML,
        os.path.join(STATIC_DIR, "app.css"): APP_CSS
    }
    for path, content in tpl_map.items():
        # Always overwrite because we are dynamically injecting changes
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        log(f"Created/Updated file: {path}", "INFO")

# =========================================================
# FLASK APP
# =========================================================

app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)
app.secret_key = CFG["app"].get("secret_key", "CHANGE_ME")

@app.route("/", methods=["GET"])
def index():
    conn = db_connect()
    last_by_topic = get_last_practised_by_topic(conn)
    conn.close()

    overdue_days = int(CFG["practice"]["overdue_days"])
    overdue_cutoff = datetime.utcnow() - timedelta(days=overdue_days)

    overdue_topics = []
    for t in QB["topics"]:
        last = last_by_topic.get(t)
        if last is None or last < overdue_cutoff:
            overdue_topics.append(t)

    return render_template(
        "index.html",
        topics=QB["topics"],
        levels=QB["levels"],
        overdue_topics=overdue_topics,
        overdue_days=overdue_days,
        default_mode=CFG["practice"].get("default_mode", "mixed"),
        timer_enabled_default=bool(CFG["timer"].get("enabled_by_default", False)),
        timer_minutes=int(int(CFG["timer"]["duration_seconds"]) / 60),
        ui=CFG["ui"]
    )

@app.route("/start", methods=["POST"])
def start_set():
    mode = (request.form.get("mode") or "mixed").strip().lower()
    topic = (request.form.get("topic") or "").strip()
    timer_enabled = 1 if (request.form.get("timer_enabled") == "on") else 0
    difficulty = (request.form.get("difficulty") or "").strip() # Feature 4

    set_size = int(CFG["practice"]["set_size"])
    duration_seconds = int(CFG["timer"]["duration_seconds"]) if timer_enabled else 10**9  # large placeholder

    if mode not in ["mixed", "topic"]:
        mode = "mixed"

    if mode == "topic":
        if not topic:
            topic = QB["topics"][0] if QB["topics"] else "Unknown"
        questions = pick_questions_topic(topic, set_size, difficulty)
        overdue_topics = []
    else:
        questions, overdue_topics = pick_questions_mixed(set_size, difficulty)
        topic = ""

    set_id = uuid.uuid4().hex
    start_time = datetime.utcnow()
    due_time = start_time + timedelta(seconds=duration_seconds if timer_enabled else int(CFG["timer"]["duration_seconds"]))

    # store set server-side for refresh safety
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO practice_sets (id, mode, topic, timer_enabled, duration_seconds, start_time, due_time,
                                questions_json, answers_json, is_submitted, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
    """, (
        set_id,
        mode,
        topic if mode == "topic" else None,
        timer_enabled,
        int(CFG["timer"]["duration_seconds"]),
        start_time.replace(microsecond=0).isoformat() + "Z",
        due_time.replace(microsecond=0).isoformat() + "Z",
        json.dumps(questions),
        json.dumps({}),  # answers keyed by index
        now_utc_iso()
    ))
    conn.commit()
    conn.close()

    log(f"Started set {set_id} mode={mode} topic={topic or '—'} timer={bool(timer_enabled)} difficulty={difficulty}", "INFO")
    return redirect(url_for("practice", set_id=set_id, index=0))

def get_set_or_404(set_id: str) -> sqlite3.Row:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM practice_sets WHERE id = ?", (set_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        abort(404)
    return row

@app.route("/practice/<set_id>", methods=["GET"])
def practice(set_id: str):
    idx = int(request.args.get("index", "0"))
    row = get_set_or_404(set_id)

    # Feature 1 Check: If cancelled, do not allow practice
    if row.keys().__contains__("is_cancelled") and row["is_cancelled"] == 1:
        return redirect(url_for("index"))

    mode = row["mode"]
    topic = row["topic"] or ""
    timer_enabled = bool(row["timer_enabled"])
    questions = json.loads(row["questions_json"])
    answers = json.loads(row["answers_json"]) # Need for status map

    if row["is_submitted"] == 1:
        return redirect(url_for("results", set_id=set_id))

    if idx < 0 or idx >= len(questions):
        # if out of range, submit set
        return redirect(url_for("results", set_id=set_id))

    due_time = parse_iso(row["due_time"])
    start_time = parse_iso(row["start_time"])
    duration_ms = int((due_time - start_time).total_seconds() * 1000)
    due_epoch_ms = int(due_time.timestamp() * 1000)

    # Feature 3: Status Map
    status_map = {}
    for i in range(len(questions)):
        val = answers.get(str(i))
        if val:
            status_map[str(i)] = "answered"
        elif str(i) in answers: # Exists but None/empty means skipped (if we store None explicitly) or just visited?
            # Logic: if key exists in answers but value is None/Empty -> Skipped.
            # If key not in answers -> Unseen/Unanswered.
            # Current logic in answer_question sets key to None if skipped.
            status_map[str(i)] = "skipped"
        else:
            status_map[str(i)] = ""

    log(f"Serving question uid={questions[idx].get('uid')} file={questions[idx].get('source_path')} qid={questions[idx].get('qid')}",
        "INFO")

    return render_template(
        "practice.html",
        set_id=set_id,
        q=questions[idx],
        index=idx,
        total=len(questions),
        mode=mode,
        topic=topic,
        timer_enabled=timer_enabled,
        due_epoch_ms=due_epoch_ms,
        duration_ms=duration_ms,
        status_map=status_map,
        selected=answers.get(str(idx)), # Pass current selection to pre-check radio
        ui_show_grid=CFG["ui"].get("show_question_grid", True)
    )

@app.route("/answer/<set_id>", methods=["POST"])
def answer_question(set_id: str):
    row = get_set_or_404(set_id)
    if row["is_submitted"] == 1:
        return redirect(url_for("results", set_id=set_id))

    idx = int(request.form.get("index", "0"))
    selected = (request.form.get("selected") or "").strip().upper()
    if selected not in ["A", "B", "C", "D", "E"]:
        selected = None  # Feature 2: Allow Skip (None)

    questions = json.loads(row["questions_json"])
    if idx < 0 or idx >= len(questions):
        return redirect(url_for("results", set_id=set_id))

    answers = json.loads(row["answers_json"])
    answers[str(idx)] = selected

    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE practice_sets SET answers_json = ? WHERE id = ?", (json.dumps(answers), set_id))
    conn.commit()
    conn.close()

    next_idx = idx + 1
    if next_idx >= len(questions):
        return redirect(url_for("results", set_id=set_id))
    return redirect(url_for("practice", set_id=set_id, index=next_idx))

# Feature 1: Cancel Set Route
@app.route("/cancel/<set_id>", methods=["GET"])
def cancel_set(set_id: str):
    row = get_set_or_404(set_id)
    if row["is_submitted"] == 1:
        return redirect(url_for("results", set_id=set_id))

    conn = db_connect()
    cur = conn.cursor()

    # Mark as cancelled
    if CFG.get("sets", {}).get("cancel_marks_set_cancelled", True):
        try:
            cur.execute("UPDATE practice_sets SET is_cancelled = 1 WHERE id = ?", (set_id,))
        except Exception:
            # Fallback if column missing (though migration should have run)
            pass

    # Option: Write attempts? Default to False
    if CFG.get("sets", {}).get("write_attempts_on_cancel", False):
        # We need to compute attempts partially
        # But we won't submit the set.
        pass # Not implemented as default is False and surgical.

    conn.commit()
    conn.close()

    log(f"Set {set_id} cancelled by user.", "INFO")
    return redirect(url_for("index"))

@app.route("/auto_submit/<set_id>", methods=["POST"])
def auto_submit(set_id: str):
    # Marks the set as submitted; scoring happens on results page.
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE practice_sets SET is_submitted = 1 WHERE id = ?", (set_id,))
    conn.commit()
    conn.close()
    log(f"Auto-submitted set {set_id} due to timer expiry.", "INFO")
    return ("", 204)

def compute_and_store_attempts(set_row: sqlite3.Row) -> Dict[str, Any]:
    questions = json.loads(set_row["questions_json"])
    answers = json.loads(set_row["answers_json"])
    mode = set_row["mode"]
    topic = set_row["topic"] or ""

    attempts_to_write = []
    score = 0

    for i, q in enumerate(questions):
        selected = answers.get(str(i))
        correct = q["correct"]
        is_correct = 1 if (selected == correct) else 0
        if is_correct:
            score += 1

        diagnostic = ""
        if selected:
            diagnostic = (q.get("diagnostics_map", {}) or {}).get(selected, "") or ""

        attempts_to_write.append({
            "id": uuid.uuid4().hex,
            "set_id": set_row["id"],
            "question_uid": q["uid"],
            "topic": q["topic"],
            "tags_json": json.dumps(q.get("tags", [])),
            "selected": selected,
            "correct": correct,
            "is_correct": is_correct,
            "diagnostic": diagnostic,
            "created_at": now_utc_iso()
        })

    # Write attempts once per set (idempotent guard: if attempts exist, do not duplicate)
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(1) AS n FROM attempts WHERE set_id = ?", (set_row["id"],))
    existing = cur.fetchone()["n"]
    if existing == 0:
        cur.executemany("""
          INSERT INTO attempts (id, set_id, question_uid, topic, tags_json, selected, correct, is_correct, diagnostic, created_at)
          VALUES (:id, :set_id, :question_uid, :topic, :tags_json, :selected, :correct, :is_correct, :diagnostic, :created_at)
        """, attempts_to_write)
        conn.commit()
        log(f"Stored {len(attempts_to_write)} attempts for set {set_row['id']}.", "INFO")
    conn.close()

    # Mark set submitted
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("UPDATE practice_sets SET is_submitted = 1 WHERE id = ?", (set_row["id"],))
    conn.commit()
    conn.close()

    return {"score": score, "total": len(questions)}

@app.route("/results/<set_id>", methods=["GET"])
def results(set_id: str):
    set_row = get_set_or_404(set_id)

    # If timer expired but not yet marked, ensure submission if due has passed
    due_time = parse_iso(set_row["due_time"])
    if datetime.utcnow() >= due_time and set_row["is_submitted"] == 0 and CFG["timer"].get("auto_submit_on_expiry", True):
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("UPDATE practice_sets SET is_submitted = 1 WHERE id = ?", (set_id,))
        conn.commit()
        conn.close()
        set_row = get_set_or_404(set_id)

    score_pack = compute_and_store_attempts(set_row)

    questions = json.loads(set_row["questions_json"])
    answers = json.loads(set_row["answers_json"])

    script = []
    weak_topic_counts = {}
    code_counts = {}
    used_diagnostic_codes = set()

    for i, q in enumerate(questions):
        sel = answers.get(str(i))
        correct = q["correct"]
        ok = (sel == correct)
        diag = ""
        diag_text = ""
        if sel:
            diag = (q.get("diagnostics_map", {}) or {}).get(sel, "") or ""
            if diag:
                used_diagnostic_codes.add(diag)
                diag_text = QB["diagnostic_legend"].get(diag, "")

        if not ok:
            weak_topic_counts[q["topic"]] = weak_topic_counts.get(q["topic"], 0) + 1
            if diag:
                code_counts[diag] = code_counts.get(diag, 0) + 1

        script.append({
            "stem": q["stem"],
            "selected": sel,
            "correct": correct,
            "is_correct": ok,
            "diagnostic": diag,
            "diagnostic_text": diag_text
        })

    weak_topics = sorted(weak_topic_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    top_codes = sorted(code_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # Feature 5: Legend Items
    legend_items = []
    for c in sorted(list(used_diagnostic_codes)):
        if c in QB["diagnostic_legend"]:
            legend_items.append((c, QB["diagnostic_legend"][c]))

    mode_title = "Mixed set" if set_row["mode"] == "mixed" else f"Topic set: {set_row['topic']}"
    finished_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M")

    return render_template(
        "results.html",
        score=score_pack["score"],
        total=score_pack["total"],
        timer_enabled=bool(set_row["timer_enabled"]),
        mode_title=mode_title,
        finished_at=finished_at,
        script=script,
        weak_topics=weak_topics,
        top_codes=top_codes,
        legend_items=legend_items
    )

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "questions_loaded": QB["stats"]["questions_loaded"],
        "topics": len(QB["topics"]),
        "files_found": QB["stats"]["files_found"],
        "errors": QB["stats"]["errors"]
    })

def main():
    ensure_dirs()
    ensure_template_files()
    db_init()

    global QB
    QB = load_question_bank()

    if QB["stats"]["questions_loaded"] == 0:
        log("No questions loaded. Check config.json question_bank_root and JSON formats.", "ERROR")
        # print("No questions loaded. Fix config.json question_bank_root and retry.")
        # sys.exit(1)
        # Making this non-fatal for now to allow testing without actual files
        pass

    host = CFG["app"].get("host", "127.0.0.1")
    port = int(CFG["app"].get("port", 5000))

    url = f"http://{host}:{port}"
    log(f"Opening browser at {url}", "INFO")
    webbrowser.open(url)

    log(f"Starting server at http://{host}:{port}", "INFO")
    app.run(host=host, port=port, debug=False)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as e:
        log(f"Fatal error: {e}", "ERROR")
        log(traceback.format_exc(), "ERROR")
        raise
