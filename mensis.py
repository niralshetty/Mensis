"""
MENSIS - period tracker 

languages used : Python, HTML, CSS

How the file is organised :
  1. Database      - tables, and a helper to open them
  2. Cycle maths   - one function that the dashboard and calendar both use
  3. Pages         - login, signup, onboarding, home, calendar, settings
  4. Small APIs    - save a daily log, toggle a period day
  5. Email         - reminder sender 
  6. Templates     - the HTML template (Jinja)
  7. CSS           - design
"""
# import = borrow a ready-made toolbox.
# os = talk to the computer.
# sqlite3 = the database.
# calendar = knows the days of every month.
# smtplib = sends email.
# threading = do two jobs at once.
# time = wait/sleep.
# json = turn lists into text and back.
import os, sqlite3, calendar, smtplib, threading, time, json
# date = a calendar day, timedelta = an amount of days to add or subtract, datetime = date plus clock time.
from datetime import date, timedelta, datetime
# EmailMessage = the envelope we write emails in.
from email.message import EmailMessage
# Flask = helps write web apps in python. g = temporary pocket for one visit. request = what the browser sent. session = the wristband remembering who you are.
# redirect = send to another page. render_template = fill an HTML page with data. jsonify = reply with small data. Response = custom reply (our CSS).
from flask import Flask, g, request, session, redirect, render_template, jsonify, Response
# DictLoader = lets us keep HTML pages inside this Python file.
#Jinja2 = the HTML engine that fills templates with pythondata.
from jinja2 import DictLoader
# Password scrambler (hash) and checker
from werkzeug.security import generate_password_hash, check_password_hash

# Create the app (the waiter). __name__ tells Flask which file it lives in.
app = Flask(__name__)
# Secret stamp used to sign the session wristband so nobody can fake it.
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret")
# Name of the database file created next to this script.
DB_FILE = "mensis.db"

# ------------------------------------------------------------------ 1. DATABASE
# IMPORTANT: every table below has a user_id column. Every query further down
# filters with "WHERE user_id = ?" so one account can never see another's data.
# SCHEMA = the recipe for our 4 tables (drawers). 'IF NOT EXISTS' means: only build it the first time.
# PRIMARY KEY = a value that must be unique. user_id on each table = the name tag that keeps accounts separate.
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, email_login TEXT UNIQUE, password TEXT,
  onboarded INTEGER DEFAULT 0,            -- 0 until all 4 onboarding answers are saved
  goal TEXT, cycle_len INTEGER, period_len INTEGER,
  notify_email TEXT DEFAULT '',           -- address for reminders (can be removed)
  n_period INTEGER DEFAULT 0, n_ovul INTEGER DEFAULT 0, n_fertile INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS periods(        -- each row = one period START date
  user_id INTEGER NOT NULL, start TEXT NOT NULL, PRIMARY KEY(user_id, start));
CREATE TABLE IF NOT EXISTS logs(           -- one daily log per user per date
  user_id INTEGER NOT NULL, date TEXT NOT NULL,
  sex TEXT, protection TEXT, symptoms TEXT DEFAULT '[]', mood TEXT,
  PRIMARY KEY(user_id, date));
CREATE TABLE IF NOT EXISTS sent(           -- remembers which emails we already sent
  user_id INTEGER, kind TEXT, day TEXT, PRIMARY KEY(user_id, kind, day));
"""

# connect() = open the database file and hand it back.
def connect():
    # Open (or create) the database file.
    c = sqlite3.connect(DB_FILE)
    # Lets us read a row by column name instead of number.
    c.row_factory = sqlite3.Row
    # Give the open database to whoever called it
    return c

# db() = open the database ONCE per page visit and reuse it.
#basically connecting to db everytime a req is made, using it once, and then closing it at the end of the request.
#each connection is brand new and stored in g, which is a temporary pocket for the current request.
def db():                                   # one connection per request
    # If we haven’t opened it yet during this visit
    if "db" not in g:
        # ...open it and keep it in the pocket (g).
        g.db = connect()
    # Hand over the connection.
    return g.db

# A decorator (@) = 'run the next function automatically' - here, at the end of every visit.
@app.teardown_appcontext
def close_db(_):
    # Take the connection out of the pocket (None if there is none).
    d = g.pop("db", None)
    # If it was open, close it so nothing stays locked.
    if d: d.close()

# me() = who is logged in right now?
def me():                                   # the logged-in user (or None)
    # Read the user’s ID from their wristband (None if not logged in).
    uid = session.get("uid")
    # Fetch that user’s row. '?' is filled safely by the value - this blocks SQL injection attacks. Returns None if no one is logged in.
    return db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None

# GATEKEEPER: runs before EVERY page request.
@app.before_request
def gatekeeper():
    """Runs before EVERY page. Not logged in -> login. Onboarding not done -> onboarding."""
    # These pages are open to everyone (login, signup, stylesheet, unknown).
    if request.endpoint in ("login", "signup", "css", None):
        # Stop here and let them in.
        return
    # Look up the logged-in user.
    u = me()
    # No wristband, or the account no longer exists...
    if not u:
        # ...throw away the bad wristband...
        session.clear()
        # ...and send them to the login page.
        return redirect("/login")
    # Account exists but onboarding isn’t finished: they may only visit onboarding or logout.
    if not u["onboarded"] and request.endpoint not in ("onboarding", "logout"):
        # Send them to the onboarding questions. This is how every new account is forced through onboarding.
        return redirect("/onboarding")

# ------------------------------------------------------------------ 2. CYCLE MATHS
# Fertile window = 5 days before ovulation up to 1 day after. Constants are named so we can change them in one place.
FERT_BEFORE, FERT_AFTER = 5, 1              # fertile window = ovulation -5 ... +1 days

# info() = THE one place all cycle numbers are calculated. Dashboard, calendar and emails all use it, so they always agree.
def info(c, u, today=None):
    """Everything about the cycle, calculated from the periods table."""
    # Use the given day, otherwise the real today.
    today = today or date.today()
    # cl = cycle length, pl = period length (days), read from the user’s row.
    cl, pl = u["cycle_len"], u["period_len"]
    # Load THIS user’s period start dates (WHERE user_id), turn the text into real dates, sort oldest to newest.
    starts = sorted(date.fromisoformat(r["start"]) for r in
                    c.execute("SELECT start FROM periods WHERE user_id=?", (u["id"],)))
    # Keep only start dates that are not in the future.
    past = [s for s in starts if s <= today]
    # The most recent start date. If none, use today.
    last = past[-1] if past else today
    # Next predicted period = last start + cycle length.
    nxt = last + timedelta(cl)
    # Cycle day. Start date itself is day 1, hence +1.
    day = (today - last).days + 1
    # Days after the predicted date (0 if we’re not late yet).
    late = (today - nxt).days if today > nxt else 0
    # Ovulation happens about 14 days before the next period, so ovulation day = cycle length - 14.
    ovd = cl - 14                                       # ovulation cycle day
    # The actual calendar date of ovulation.
    ovul = last + timedelta(ovd - 1)
    # Inside the period length = menstrual phase.
    if day <= pl: phase = "menstrual"
    # Before ovulation day = follicular phase.
    elif day < ovd: phase = "follicular"
    # Exactly ovulation day = ovulation phase.
    elif day == ovd: phase = "ovulation"
    # Everything after = luteal phase.
    else: phase = "luteal"
    # Start by assuming the fertile window is not active.
    fert = "inactive"                                   # check this and next cycle
    # Check this cycle (k=0) and the next one (k=1).
    for k in (0, 1):
        # Ovulation date for that cycle.
        o = ovul + timedelta(cl * k)
        # s = window start, e = window end.
        s, e = o - timedelta(FERT_BEFORE), o + timedelta(FERT_AFTER)
        # Today is inside the window: active.
        if s <= today <= e: fert = "active"; break
        # Window starts within 1-3 days: approaching.
        if 0 < (s - today).days <= 3: fert = "approaching"; break
    # Look up today’s mood for THIS user from the logs table - the same table the calendar saves to.
    row = c.execute("SELECT mood FROM logs WHERE user_id=? AND date=?",
                    (u["id"], today.isoformat())).fetchone()
    # Return everything in one labelled package (dictionary).
    return dict(today=today, cl=cl, pl=pl, last=last, next=nxt, day=day, late=late,
                next_in=max((nxt - today).days, 0), phase=phase, fert=fert,
                ovul=ovul, mood=row["mood"] if row else None, starts=starts)

# calendar_marks() = decide the colour of each calendar date.
def calendar_marks(i):
    """Colour of every date: period / pred (predicted period) / ovul / fert."""
    # mk starts as an empty colour list: date text -> colour name.
    cl, pl, mk = i["cl"], i["pl"], {}
    # Real logged period starts.
    actual = i["starts"]
    # Predictions count forward from the newest real start.
    base = max(actual) if actual else i["last"]
    # Make the next 4 predicted period start dates by adding the cycle length again and again.
    predicted = [base + timedelta(cl * k) for k in range(1, 5)]
    # Colour real period days 'period'. setdefault = only if the date has no colour yet, so earlier colours win.
    for s in actual:
        for n in range(pl): mk.setdefault((s + timedelta(n)).isoformat(), "period")
    # Colour predicted period days 'pred'.
    for s in predicted:
        for n in range(pl): mk.setdefault((s + timedelta(n)).isoformat(), "pred")
    # For every cycle, real or predicted...
    for s in actual + predicted:
        # ...find its ovulation date...
        o = s + timedelta(cl - 15)
        # ...colour it 'ovul'...
        mk.setdefault(o.isoformat(), "ovul")
        # ...and colour the days around it 'fert' (fertile).
        for n in range(-FERT_BEFORE, FERT_AFTER + 1):
            mk.setdefault((o + timedelta(n)).isoformat(), "fert")
    # Hand back the colour list.
    return mk

# Content for the dashboard -------------------------------------------------------
# Dictionary: mood name -> emoji. Used for the mood buttons and the dashboard card.
MOODS = {"happy": "😊", "calm": "😌", "sad": "😢", "irritable": "😠", "anxious": "😰", "tired": "😴"}
# List of (name, emoji) pairs for the 3-column symptom cards.
SYMPTOMS = [("Cramps", "😣"), ("Headache", "🤯"), ("Bloating", "🎈"), ("Fatigue", "🥱"),
            ("Backache", "🦴"), ("Nausea", "🤢"), ("Acne", "🔴"), ("Tenderness", "💗"), ("Insomnia", "🌙")]
# phase -> (background, text colour, heading, days, description, bullets)
# Phase guide text: phase -> (card colour, text colour, heading, days, description, 3 bullets).
GUIDE = {
 "menstrual": ("#7C0E44", "#fff", "Menstrual", "Day 1–5", "Your body is shedding the uterine lining.",
    ["Energy levels are at their lowest", "Cramps and bloating are common", "Rest and light movement are recommended"]),
 "follicular": ("#D49A9A", "#2b0a1a", "Follicular", "Day 6–13", "Estrogen is rising and your body is preparing to ovulate.",
    ["Energy and mood begin to improve", "Skin may look clearer and brighter", "A great time for new goals and social plans"]),
 "ovulation": ("#C9B6F2", "#2b0a1a", "Ovulation", "Day 14", "Your body is releasing an egg — this is your most fertile day.",
    ["Energy and confidence are at their peak", "Libido may be higher than usual", "Fertile window is now open"]),
 "luteal": ("#AA5E7A", "#fff", "Luteal", "Day 15–28", "Progesterone rises to prepare the body in case of pregnancy.",
    ["PMS symptoms like bloating and mood swings may appear", "Cravings and fatigue are common", "Wind down and practice self-care"]),
}
# Small tip cards shown for each phase: (emoji, title, text).
TIPS = {
 "menstrual": [("🛁", "Warmth helps", "A heat pack eases cramps."), ("💧", "Stay hydrated", "Water can reduce bloating."), ("🥬", "Iron-rich food", "Spinach and lentils replace lost iron.")],
 "follicular": [("🏃‍♀️", "Try something new", "Energy is climbing — good for workouts."), ("📝", "Plan ahead", "Great time to start projects."), ("🥗", "Fresh foods", "Light, colourful meals suit this phase.")],
 "ovulation": [("✨", "Peak energy", "Schedule big conversations now."), ("💬", "Be social", "Confidence is high."), ("🌡️", "Fertile days", "Most fertile day of your cycle.")],
 "luteal": [("🍫", "Cravings are normal", "Pair treats with protein."), ("😴", "Sleep first", "Aim for an earlier bedtime."), ("🧘‍♀️", "Slow down", "Gentle yoga calms PMS.")],
}

# ------------------------------------------------------------------ 3. PAGES
# @app.route = 'when the browser asks for /signup, run the function below'. GET = show the page, POST = form submitted.
@app.route("/signup", methods=["GET", "POST"])
# Create-account page.
def signup():
    # Empty error message to start.
    err = ""
    # Only do this when the form was submitted.
    if request.method == "POST":
        # Read the form boxes; strip() removes extra spaces, lower() makes the email lowercase.
        name, em, pw = request.form["name"].strip(), request.form["email"].strip().lower(), request.form["password"]
        # Basic checks: name present, email has @, password 6+ characters.
        if not name or "@" not in em or len(pw) < 6:
            err = "Enter your name, a valid email and a password of 6+ characters."
        # Does this email already have an account?
        elif db().execute("SELECT 1 FROM users WHERE email_login=?", (em,)).fetchone():
            err = "That email already has an account."
        # All good - create the account.
        else:
            # Insert the new user with the password scrambled. onboarded defaults to 0.
            cur = db().execute("INSERT INTO users(name,email_login,password) VALUES(?,?,?)",
                               (name, em, generate_password_hash(pw)))
            # commit = actually save the change to the file.
            db().commit()
            # Put the wristband on: remember this user’s ID.
            session["uid"] = cur.lastrowid
            # New users ALWAYS go to onboarding first.
            return redirect("/onboarding")         # brand new user -> onboarding, always
    # Show the form (again, with an error if there is one).
    return render_template("auth.html", mode="signup", err=err)

# Login page.
@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        # Find the account with this email.
        u = db().execute("SELECT * FROM users WHERE email_login=?", (request.form["email"].strip().lower(),)).fetchone()
        # Account exists AND the typed password matches the scrambled one.
        if u and check_password_hash(u["password"], request.form["password"]):
            # Put on the wristband.
            session["uid"] = u["id"]
            # Go home. The gatekeeper redirects to onboarding if it isn’t finished.
            return redirect("/")                    # gatekeeper() sends to onboarding if needed
        # Same message for both mistakes so strangers can’t guess which emails exist.
        err = "Wrong email or password."
    # Show the login form.
    return render_template("auth.html", mode="login", err=err)

# Logout: forget the wristband, go to login.
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# The onboarding questions page.
@app.route("/onboarding", methods=["GET", "POST"])
def onboarding():
    # Current user.
    u = me()
    # Skip to the dashboard.
    if u["onboarded"]:
        return redirect("/")
    if request.method == "POST":
        # try/except = attempt the risky code, jump to except if anything goes wrong.
        try:
            # Read the goal answer.
            goal = request.form["goal"]
            # Turn typed text into whole numbers.
            cl, pl = int(request.form["cycle_len"]), int(request.form["period_len"])
            # Turn the typed date into a real date.
            lp = date.fromisoformat(request.form["last_period"])
            # Check everything is valid; a false check causes an error and jumps to except.
            assert goal in ("track", "avoid", "conceive") and 20 <= cl <= 45 and 2 <= pl <= 10 and lp <= date.today()
        # Something was invalid: show the form again with a message.
        except Exception:
            return render_template("onboarding.html", err="Please complete every step with valid answers.", today=date.today().isoformat())
        # Open the database.
        c = db()
        # Save the last period start date for THIS user (ignore if it already exists).
        c.execute("INSERT OR IGNORE INTO periods(user_id,start) VALUES(?,?)", (u["id"], lp.isoformat()))
        # Save goal, cycle length, period length AND set onboarded=1 - only now is onboarding complete.
        c.execute("UPDATE users SET goal=?,cycle_len=?,period_len=?,onboarded=1 WHERE id=?", (goal, cl, pl, u["id"]))
        # Write it to the file.
        c.commit()
        return redirect("/")
    return render_template("onboarding.html", err="", today=date.today().isoformat())

# Home dashboard.
@app.route("/")
def home():
    # Get the user and the database.
    u, c = me(), db()
    # Calculate all cycle numbers.
    i = info(c, u)
    # Current hour (0-23).
    h = datetime.now().hour
    # Pick the greeting from the hour.
    hello = "Good morning" if h < 12 else "Good afternoon" if h < 18 else "Good evening"
    # Build the late banner text: none, 1 day, 2-6 days, or 7+ with the pregnancy-test advice.
    if i["late"] == 0: banner = ""
    elif i["late"] == 1: banner = "Your period is 1 day late."
    elif i["late"] < 7: banner = f"Your period is {i['late']} days late."
    else: banner = f"Your period is {i['late']} days late. Consider taking a pregnancy test."
    # Turn the internal fertile status into the label for the card.
    fert_label = {"active": "Active", "approaching": "Approaching", "inactive": "Not Active"}[i["fert"]]
    # Send everything to the HTML. 'dash' = how much of the ring is filled (326.7 = the circle’s edge length).
    return render_template("home.html", page="home", u=u, i=i, hello=hello, banner=banner,
                           fert_label=fert_label, tips=TIPS[i["phase"]], g=GUIDE[i["phase"]],
                           MOODS=MOODS, dash=round(min(i["day"] / i["cl"], 1) * 326.7, 1))

# Calendar page.
@app.route("/calendar")
def calendar_page():
    u, c = me(), db()
    i = info(c, u)
    try:
        # Read the month from the address (?m=2026-11) and validate it.
        y, m = map(int, request.args.get("m", i["today"].strftime("%Y-%m")).split("-"))
        date(y, m, 1)
    # Bad month in the address: use the current month.
    except Exception:
        y, m = i["today"].year, i["today"].month
    # First day of the month shown.
    first = date(y, m, 1)
    # Links to the previous and next months (+32 days always lands in next month).
    prev_m, next_m = (first - timedelta(1)).strftime("%Y-%m"), (first + timedelta(32)).strftime("%Y-%m")
    # Find dates this month where this user logged sex = yes, to show a heart.
    hearts = {r["date"] for r in c.execute(
        "SELECT date FROM logs WHERE user_id=? AND sex='yes' AND date LIKE ?", (u["id"], f"{y:04d}-{m:02d}-%"))}
    # Pass the month grid (weeks, Sunday first), colours, hearts and lists to the HTML.
    return render_template("calendar.html", page="calendar", weeks=calendar.Calendar(6).monthdatescalendar(y, m),
                           mo=m, title=first.strftime("%B %Y"), prev_m=prev_m, next_m=next_m, mk=calendar_marks(i),
                           hearts=hearts, today=i["today"], MOODS=MOODS, SYMPTOMS=SYMPTOMS,
                           sel=request.args.get("sel", ""), cur=f"{y:04d}-{m:02d}")

# Settings page.
@app.route("/settings", methods=["GET", "POST"])
def settings():
    u, c = me(), db()
    if request.method == "POST":
        # Which of the 3 small forms was sent (hidden 'form' field)?
        f = request.form.get("form")
        # Profile form: update name, goal and lengths if valid.
        if f == "profile":
            try:
                cl, pl = int(request.form["cycle_len"]), int(request.form["period_len"])
                if 20 <= cl <= 45 and 2 <= pl <= 10 and request.form["goal"] in ("track", "avoid", "conceive"):
                    c.execute("UPDATE users SET name=?,goal=?,cycle_len=?,period_len=? WHERE id=?",
                              (request.form["name"].strip() or u["name"], request.form["goal"], cl, pl, u["id"]))
            except ValueError:
                pass
        # Email form: save, update, or remove. Removing also switches reminders off.
        elif f == "email":
            if request.form.get("action") == "remove":     # removing email also turns reminders off
                c.execute("UPDATE users SET notify_email='',n_period=0,n_ovul=0,n_fertile=0 WHERE id=?", (u["id"],))
            elif "@" in request.form.get("email", ""):
                c.execute("UPDATE users SET notify_email=? WHERE id=?", (request.form["email"].strip(), u["id"]))
        # Toggle form: save the 3 reminder switches (only if an email exists).
        elif f == "notify" and u["notify_email"]:
            c.execute("UPDATE users SET n_period=?,n_ovul=?,n_fertile=? WHERE id=?",
                      (int("period" in request.form), int("ovul" in request.form), int("fertile" in request.form), u["id"]))
        # sister finally save changes.
        c.commit()
        # Reload the page so you see the fresh data.
        return redirect("/settings")
    # Show the page.
    return render_template("settings.html", page="settings", u=u)

# ------------------------------------------------------------------ 4. SMALL APIs
# valid_day() = return a clean date text, or None if it isn’t a real date.
def valid_day(s):
    try: return date.fromisoformat(s).isoformat()
    except Exception: return None

# Small data endpoint: get one day’s saved log.
@app.route("/api/log/<d>")
def get_log(d):
    u, c = me(), db()
    d = valid_day(d)
    if not d: return jsonify(error="bad date"), 400
    # Look up that day, for THIS user only.
    r = c.execute("SELECT * FROM logs WHERE user_id=? AND date=?", (u["id"], d)).fetchone()
    is_p = c.execute("SELECT 1 FROM periods WHERE user_id=? AND start=?", (u["id"], d)).fetchone() is not None
    # Send the data back as JSON, which the page’s JavaScript reads.
    return jsonify(sex=r["sex"] if r else None, prot=r["protection"] if r else None,
                   sym=json.loads(r["symptoms"]) if r else [], mood=r["mood"] if r else None, period=is_p)

# Small data endpoint: save a day’s log.
@app.route("/api/log", methods=["POST"])
def save_log():
    u, c = me(), db()
    # Read the JSON that JavaScript sent.
    d = request.get_json(force=True)
    day = valid_day(d.get("date", ""))
    if not day: return jsonify(error="bad date"), 400
    # Keep only symptoms that are on our official list.
    sym = [s for s in d.get("sym", []) if s in dict(SYMPTOMS)]
    # Keep the mood only if valid.
    mood = d.get("mood") if d.get("mood") in MOODS else None
    # Keep sex only if 'yes' or 'no'.
    sex = d.get("sex") if d.get("sex") in ("yes", "no") else None
    # Keep protection only if sex is yes and it’s one of the 3 options.
    prot = d.get("prot") if sex == "yes" and d.get("prot") in ("Condom", "No condom", "Other contraception") else None
    # UPSERT: insert a new log, or if one exists for this user+date, update it. json.dumps turns the symptom list into text.
    c.execute("""INSERT INTO logs(user_id,date,sex,protection,symptoms,mood) VALUES(?,?,?,?,?,?)
                 ON CONFLICT(user_id,date) DO UPDATE SET sex=?,protection=?,symptoms=?,mood=?""",
              (u["id"], day, sex, prot, json.dumps(sym), mood, sex, prot, json.dumps(sym), mood))
    c.commit()
    return jsonify(ok=True)

# Small endpoint: toggle a period start on a date.
@app.route("/api/period", methods=["POST"])
def toggle_period():
    u, c = me(), db()
    day = valid_day(request.get_json(force=True).get("date", ""))
    if not day: return jsonify(error="bad date"), 400
    # If the date is already a period start, delete it, otherwise add it.
    if c.execute("SELECT 1 FROM periods WHERE user_id=? AND start=?", (u["id"], day)).fetchone():
        c.execute("DELETE FROM periods WHERE user_id=? AND start=?", (u["id"], day))
    else:
        c.execute("INSERT INTO periods(user_id,start) VALUES(?,?)", (u["id"], day))
    c.commit()
    return jsonify(ok=True)

# ------------------------------------------------------------------ 5. EMAIL
# send_email() = the only place that sends mail. Swap it to use a different email service.
def send_email(to, subject, body):
    """I could not use a 'SendEmail' integration here, so this uses normal SMTP.
    Set SMTP_HOST, SMTP_USER, SMTP_PASS (e.g. a Gmail app password).
    To use a different service, just replace what's inside this one function."""
    # Read the mail server name from computer settings.
    host = os.environ.get("SMTP_HOST")
    # Not configured: print the email in the terminal instead of sending.
    if not host:
        print(f"[email not configured] To: {to} | {subject}\n{body}\n")
        return
    # Create a blank email.
    msg = EmailMessage()
    # Fill in sender, receiver and subject.
    msg["From"], msg["To"], msg["Subject"] = os.environ["SMTP_USER"], to, subject
    # Write the email body.
    msg.set_content(body)
    # Connect securely to the mail server, log in, send, then disconnect (with = auto-close).
    with smtplib.SMTP_SSL(host, 465) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)

# run_reminders() = check every user and send what is due today.
def run_reminders():
    """Checks every user; sends each reminder at most once."""
    # This runs in a background thread, so it opens its own database connection.
    c = connect()
    today = date.today()
    # Loop over users who finished onboarding and have a reminder email.
    for u in c.execute("SELECT * FROM users WHERE onboarded=1 AND notify_email!=''").fetchall():
        # Calculate that user’s cycle numbers.
        i = info(c, u, today)
        # List of emails to send.
        todo = []
        # Period reminder: switch on AND today is 2 days before the predicted start.
        if u["n_period"] and i["next"] - timedelta(2) == today:
            todo.append(("period", "Your period is expected in 2 days", "Heads up from Mensis: your period is predicted to start in 2 days."))
        # Check this and the next cycle’s ovulation.
        for k in (0, 1):
            o = i["ovul"] + timedelta(u["cycle_len"] * k)
            # Ovulation reminder: switch on AND today is ovulation day.
            if u["n_ovul"] and o == today:
                todo.append(("ovul", "Your ovulation window is open", "Mensis: ovulation is predicted for today."))
            # Fertile-window-closing reminder: switch on AND today is the day after ovulation.
            if u["n_fertile"] and o + timedelta(FERT_AFTER) == today:
                todo.append(("fertile", "Your fertile window closes today", "Mensis: today is the last day of your fertile window."))
        # Go through each due email.
        for kind, subj, body in todo:
            # Already sent today, if so then Skip karo, so no duplicates.
            if c.execute("SELECT 1 FROM sent WHERE user_id=? AND kind=? AND day=?", (u["id"], kind, today.isoformat())).fetchone():
                continue
            # Try sending; one failure must not stop the others.
            try:
                # Send it.
                send_email(u["notify_email"], subj, body)
                # Record it in the 'sent' notebook.
                c.execute("INSERT INTO sent VALUES(?,?,?)", (u["id"], kind, today.isoformat()))
                c.commit()
            # If sending failed, print why and carry on.
            except Exception as e:
                print("email failed:", e)
    c.close()

# Background loop that runs forever.
def reminder_loop():
    while True:
        # Run the check; catch errors so the loop never dies i.e it keeps running in background.
        try: run_reminders()
        except Exception as e: print("reminder error:", e)
        # Sleep one hour (3600 seconds), then repeat.
        time.sleep(3600)                                   # check once an hour

# ------------------------------------------------------------------ 6. TEMPLATES
# BASE = the frame shared by all pages. HTML templates are blueprints: {{ x }} fills a value, {% if %}/{% for %} decide and repeat, {# #} are hidden comments.
BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Mensis</title>
{# Load our CSS from the /style.css route. #}
<link rel="stylesheet" href="/style.css"></head><body>
{# Top bar - shown only on the 3 main pages (not login/onboarding). #}
{% if page %}<header class="top"><b class="brand">Mensis</b>
{# The 3 tabs. 'on' class highlights the current page. Sits at the bottom on mobile (see CSS). #}
<nav class="tabs">
<a href="/" class="{{ 'on' if page=='home' }}"><span>🏠</span>Home</a>
<a href="/calendar" class="{{ 'on' if page=='calendar' }}"><span>📅</span>Calendar</a>
<a href="/settings" class="{{ 'on' if page=='settings' }}"><span>⚙️</span>Settings</a></nav>
{# Notification bell, top right, links to the email settings. #}
<a class="bell" href="/settings#email" aria-label="Notifications">🔔</a></header>{% endif %}
{# Page content goes here ({% block body %}); 'padtab' leaves room for the bottom tab bar. #}
<main class="wrap {{ 'padtab' if page }}">{% block body %}{% endblock %}</main>
{# Empty box that becomes the 'Log saved' toast. #}
<div id="toast"></div>
{# toast(msg): fill the box, add class 'show' (slides in), after 3000 ms add 'out' (fade) then clear it. So it never stays. #}
<script>function toast(m){var t=document.getElementById('toast');t.innerHTML='<span class="ck">✓</span><div><b>'+m+'</b><small>Your day has been updated</small></div>';
t.className='show';clearTimeout(window._t);window._t=setTimeout(function(){t.className='out';setTimeout(function(){t.className='';t.innerHTML=''},300)},3000)}</script>
</body></html>"""

# AUTH = one page used for both login and signup (mode decides).
AUTH = """{% extends "base.html" %}{% block body %}
<div class="auth card"><h1>Mensis</h1><p class="muted">{{ 'Create your account' if mode=='signup' else 'Welcome back' }}</p>
{# Show the error message only if there is one. #}
{% if err %}<p class="err">{{ err }}</p>{% endif %}
<form method="post">
{# The name box appears only when signing up. #}
{% if mode=='signup' %}<label>Your name<input name="name" required></label>{% endif %}
<label>Email<input type="email" name="email" required></label>
<label>Password<input type="password" name="password" required minlength="6"></label>
{# Button text changes with the mode. #}
<button class="btn">{{ 'Sign up' if mode=='signup' else 'Log in' }}</button></form>
<p class="muted center">{% if mode=='signup' %}Have an account? <a href="/login">Log in</a>{% else %}New here? <a href="/signup">Sign up</a>{% endif %}</p></div>{% endblock %}"""

# ONBOARD = the 4-step questions page.
ONBOARD = """{% extends "base.html" %}{% block body %}
<div class="auth card"><div class="bar"><i id="fill"></i></div><p class="muted" id="count"></p>
{% if err %}<p class="err">{{ err }}</p>{% endif %}
<form method="post" id="f">
{# Step 1: goal (radio buttons: only one can be chosen). #}
<section class="step"><h1>What's your goal?</h1>
<label class="opt"><input type="radio" name="goal" value="track" checked><span>🩸 Track my period</span></label>
<label class="opt"><input type="radio" name="goal" value="avoid"><span>🛡️ Avoid pregnancy</span></label>
<label class="opt"><input type="radio" name="goal" value="conceive"><span>🌱 Try to conceive</span></label></section>
{# Step 2: cycle length, default 28. #}
<section class="step"><h1>Average cycle length</h1><p class="muted">Days from one period start to the next.</p>
<input type="number" name="cycle_len" value="28" min="20" max="45"></section>
{# Step 3: period length, default 5. #}
<section class="step"><h1>Average period duration</h1><p class="muted">How many days does your period last?</p>
<input type="number" name="period_len" value="5" min="2" max="10"></section>
{# Step 4: last period date (max = today, so no future dates). #}
<section class="step"><h1>When did your last period start?</h1>
<input type="date" name="last_period" max="{{ today }}" value="{{ today }}"></section>
{# Back and Next buttons. #}
<div class="row"><button type="button" class="btn ghost" id="back">Back</button><button type="button" class="btn" id="next">Next</button></div>
</form></div>
<script>
{# JS: show one step at a time, n = current step. show() updates the visible step, the progress bar width, 'Step X of 4', hides Back on step 1, and renames Next to Finish on the last. Finish submits the form. #}
var steps=document.querySelectorAll('.step'),n=0;
function show(){steps.forEach(function(s,i){s.style.display=i==n?'block':'none'});
document.getElementById('fill').style.width=((n+1)/steps.length*100)+'%';
document.getElementById('count').textContent='Step '+(n+1)+' of '+steps.length;
document.getElementById('back').style.visibility=n?'visible':'hidden';
document.getElementById('next').textContent=n==steps.length-1?'Finish':'Next'}
document.getElementById('next').onclick=function(){if(n<steps.length-1){n++;show()}else{document.getElementById('f').submit()}};
document.getElementById('back').onclick=function(){if(n){n--;show()}};show();
</script>{% endblock %}"""

# HOME = the dashboard.
HOME = """{% extends "base.html" %}{% block body %}
{# Late-period banner, only if banner text exists. #}
{% if banner %}<div class="late">⚠️ <span>{{ banner }}</span></div>{% endif %}
{# Greeting with the user’s name from signup, date, subtitle. #}
<h1>{{ hello }}, {{ u['name'] }}!</h1>
<p class="muted">{{ i.today.strftime('%A, %B %d') }} · Listening to your body is a superpower.</p>
{# Grid of cards: 1 column on mobile, 2 on tablet, 3 on desktop (CSS). #}
<div class="grid">
{# Cycle ring: an SVG circle; the dash length (dash) shows progress. Inside: day, phase, countdown. #}
<div class="card ringcard"><svg viewBox="0 0 120 120" class="ring"><circle cx="60" cy="60" r="52" class="track"/>
<circle cx="60" cy="60" r="52" class="prog" stroke-dasharray="{{ dash }} 326.7" transform="rotate(-90 60 60)"/></svg>
<div class="ringtxt"><b>Day {{ i.day }}</b><span>{{ g[2] }} phase</span>
<small>{% if i.late %}Period is late{% else %}Period in {{ i.next_in }} day{{ '' if i.next_in==1 else 's' }}{% endif %}</small></div></div>
{# Fertile Window card: colour class f-active/f-approaching/f-inactive. #}
<div class="card"><h3>Fertile Window</h3><span class="pill f-{{ i.fert }}">{{ fert_label }}</span></div>
{# Mood card: emoji + name, or 'Not logged yet'. #}
<div class="card"><h3>Today's Mood</h3>{% if i.mood %}<div class="big">{{ MOODS[i.mood] }}</div><span>{{ i.mood|capitalize }}</span>
{% else %}<div class="big gray">·</div><span class="muted">Not logged yet</span>{% endif %}</div>
{# Days until the next period. #}
<div class="card"><h3>Next Period</h3><div class="big">{{ i.next_in }}</div><span class="muted">days remaining</span></div>
{# Last period start, average cycle length, current phase. #}
<div class="card"><h3>Cycle Overview</h3>
<p class="kv"><span>Last period started</span><b>{{ i.last.strftime('%b %d, %Y') }}</b></p>
<p class="kv"><span>Average cycle length</span><b>{{ i.cl }} days</b></p>
<p class="kv"><span>Current phase</span><b>{{ g[2] }}</b></p></div></div>
{# Three tip cards for the current phase (loop). #}
<h2>Today's Insights</h2>
<div class="grid">{% for e,t,d in tips %}<div class="card"><div class="big">{{ e }}</div><h3>{{ t }}</h3><p class="muted">{{ d }}</p></div>{% endfor %}</div>
{# One guide card; background colour comes from the same phase as the ring. #}
<h2>Your Phase Guide</h2>
<div class="card guide" style="background:{{ g[0] }};color:{{ g[1] }}"><h3>{{ g[2] }} <small>({{ g[3] }})</small></h3>
<p>{{ g[4] }}</p><ul>{% for b in g[5] %}<li>{{ b }}</li>{% endfor %}</ul></div>
{% endblock %}"""

# CAL = the calendar page.
CAL = """{% extends "base.html" %}{% block body %}
<div class="card">
{# Month title with previous/next arrows. #}
<div class="mhead"><a class="nav" href="/calendar?m={{ prev_m }}">‹</a><h2>{{ title }}</h2><a class="nav" href="/calendar?m={{ next_m }}">›</a></div>
{# Weekday letters. #}
<div class="week">{% for d in ['S','M','T','W','T','F','S'] %}<span>{{ d }}</span>{% endfor %}</div>
{# Loop over weeks and dates, drawing one button per date. #}
<div class="days">{% for w in weeks %}{% for d in w %}
{# One date. The coloured circle (class from the colour list) sits behind the number. 'out' fades days from other months. #}
<button type="button" class="cell {{ 'out' if d.month!=mo }}" data-date="{{ d.isoformat() }}">
<span class="d {{ mk.get(d.isoformat(),'') }} {{ 'today' if d==today }}">{{ d.day }}</span>
{% if d.isoformat() in hearts %}<i class="heart">♥</i>{% endif %}</button>{% endfor %}{% endfor %}</div>
{# Colour key. #}
<div class="legend"><span><i class="period"></i>Period</span><span><i class="pred"></i>Predicted</span><span><i class="fert"></i>Fertile</span><span><i class="ovul"></i>Ovulation</span></div></div>

{# Daily log: lives on the SAME page under the calendar (no pop-up). 'hidden' until a date is tapped. #}
<div class="card" id="log" hidden>
<h2 id="logtitle"></h2>
<button type="button" class="btn ghost" id="periodbtn"></button>
{# Two-step flow: Yes/No first... #}
<h3>Sexual Activity</h3><p>Did you have sex today?</p>
<div class="row"><button type="button" class="big-btn" data-sex="yes">Yes</button><button type="button" class="big-btn" data-sex="no">No</button></div>
<p class="muted" id="none" hidden>None logged</p>
{# ...then, only if Yes, protection options. #}
<div id="prot" hidden><p>Was protection used?</p><div class="row wrap-row">
<button type="button" class="chip" data-prot="Condom">Condom</button><button type="button" class="chip" data-prot="No condom">No condom</button>
<button type="button" class="chip" data-prot="Other contraception">Other contraception</button></div></div>
{# Symptom cards (3-column grid), loop over the SYMPTOMS list. #}
<h3>Symptoms</h3><div class="sym">{% for n,e in SYMPTOMS %}<button type="button" class="symc" data-sym="{{ n }}"><span>{{ e }}</span>{{ n }}</button>{% endfor %}</div>
{# Mood cards, loop over MOODS. #}
<h3>Mood</h3><div class="moods">{% for k,e in MOODS.items() %}<button type="button" class="moodc" data-mood="{{ k }}"><span>{{ e }}</span>{{ k|capitalize }}</button>{% endfor %}</div>
{# Save button; sticky so it is always visible. #}
<div class="savebar"><button type="button" class="btn" id="save">Save Log</button></div></div>

{# JS for the calendar. #}
<script>
{# S = notebook of the current choices. #}
var S={date:null,sex:null,prot:null,sym:[],mood:null},cur="{{ cur }}";
function $(q){return document.querySelector(q)} function $$(q){return document.querySelectorAll(q)}
{# paint(): highlight chosen buttons, show/hide the protection row and 'None logged'. #}
function paint(){
 $$('[data-sex]').forEach(function(b){b.classList.toggle('on',b.dataset.sex==S.sex)});
 $$('[data-prot]').forEach(function(b){b.classList.toggle('on',b.dataset.prot==S.prot)});
 $$('[data-sym]').forEach(function(b){b.classList.toggle('on',S.sym.indexOf(b.dataset.sym)>-1)});
 $$('[data-mood]').forEach(function(b){b.classList.toggle('on',b.dataset.mood==S.mood)});
 $('#prot').hidden=S.sex!='yes'; $('#none').hidden=S.sex!='no'}
{# post(): send JSON to the server with fetch. #}
function post(u,d){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(function(r){return r.json()})}
{# pick(date): highlight the cell, load that day’s saved log, fill the form, reveal it and scroll to it. #}
function pick(d){
 S.date=d; $$('.cell').forEach(function(c){c.classList.toggle('sel',c.dataset.date==d)});
 fetch('/api/log/'+d).then(function(r){return r.json()}).then(function(x){
  S.sex=x.sex;S.prot=x.prot;S.sym=x.sym;S.mood=x.mood;
  $('#logtitle').textContent=new Date(d+'T00:00').toLocaleDateString(undefined,{weekday:'long',month:'long',day:'numeric'});
  $('#periodbtn').textContent=x.period?'✕ Remove period start':'🩸 Period started this day';
  $('#log').hidden=false; paint(); $('#log').scrollIntoView({behavior:'smooth',block:'nearest'})})}
$$('.cell').forEach(function(c){c.onclick=function(){pick(c.dataset.date)}});
{# One listener for all button taps: updates S then repaints. Symptoms toggle on/off. #}
document.addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;
 if(b.dataset.sex){S.sex=b.dataset.sex;if(S.sex=='no')S.prot=null;paint()}
 if(b.dataset.prot){S.prot=b.dataset.prot;paint()}
 if(b.dataset.sym){var k=S.sym.indexOf(b.dataset.sym);k>-1?S.sym.splice(k,1):S.sym.push(b.dataset.sym);paint()}
 if(b.dataset.mood){S.mood=S.mood==b.dataset.mood?null:b.dataset.mood;paint()}});
{# Save: send S to /api/log, add or remove the heart, show the toast. #}
$('#save').onclick=function(){post('/api/log',S).then(function(){
 var c=document.querySelector('.cell[data-date="'+S.date+'"]'),h=c.querySelector('.heart');
 if(S.sex=='yes'&&!h){c.insertAdjacentHTML('beforeend','<i class="heart">♥</i>')}
 if(S.sex!='yes'&&h){h.remove()} toast('Log saved')})};
{# Toggle a period start, then reload so colours redraw. #}
$('#periodbtn').onclick=function(){post('/api/period',{date:S.date}).then(function(){location.href='/calendar?m='+cur+'&sel='+S.date})};
{% if sel %}pick("{{ sel }}");{% endif %}
</script>{% endblock %}"""

# SETTINGS = the settings page.
SETTINGS = """{% extends "base.html" %}{% block body %}
<h1>Settings</h1>
<div class="grid2">
{# Profile form (hidden 'form' field tells the server which form this is). #}
<form method="post" class="card"><input type="hidden" name="form" value="profile"><h2>Profile</h2>
<label>Name<input name="name" value="{{ u['name'] }}"></label>
<label>Goal<select name="goal">{% for k,v in [('track','Track my period'),('avoid','Avoid pregnancy'),('conceive','Try to conceive')] %}
<option value="{{ k }}" {{ 'selected' if u['goal']==k }}>{{ v }}</option>{% endfor %}</select></label>
<label>Average cycle length (days)<input type="number" name="cycle_len" min="20" max="45" value="{{ u['cycle_len'] }}"></label>
<label>Average period duration (days)<input type="number" name="period_len" min="2" max="10" value="{{ u['period_len'] }}"></label>
<button class="btn">Save</button></form>

{# Email Notifications: address form first... #}
<div class="card" id="email"><h2>Email Notifications</h2>
<form method="post"><input type="hidden" name="form" value="email">
<label>Email address<input type="email" name="email" value="{{ u['notify_email'] }}" placeholder="you@example.com"></label>
<div class="row"><button class="btn" name="action" value="save">{{ 'Update' if u['notify_email'] else 'Add email' }}</button>
{% if u['notify_email'] %}<button class="btn ghost" name="action" value="remove">Remove</button>{% endif %}</div></form>
{# ...then the toggles. Class 'off' greys them out when there is no email. #}
<form method="post" id="nf" class="{{ '' if u['notify_email'] else 'off' }}"><input type="hidden" name="form" value="notify">
{% set on = u['notify_email'] %}
{# Master toggle. #}
<label class="tg"><span><b>All email notifications</b></span><input type="checkbox" id="master" {{ 'checked' if u['n_period'] and u['n_ovul'] and u['n_fertile'] }} {{ '' if on else 'disabled' }}></label>
<label class="tg"><span>Period reminder<small>2 days before predicted start</small></span><input type="checkbox" name="period" class="sub" {{ 'checked' if u['n_period'] }} {{ '' if on else 'disabled' }}></label>
<label class="tg"><span>Ovulation reminder<small>When ovulation window opens</small></span><input type="checkbox" name="ovul" class="sub" {{ 'checked' if u['n_ovul'] }} {{ '' if on else 'disabled' }}></label>
<label class="tg"><span>Fertile window reminder<small>When the window closes</small></span><input type="checkbox" name="fertile" class="sub" {{ 'checked' if u['n_fertile'] }} {{ '' if on else 'disabled' }}></label>
{% if not on %}<p class="muted">Add an email address to turn these on.</p>{% endif %}</form></div></div>
<a class="btn ghost" href="/logout">Log out</a>
{# JS: master flips all three; any single toggle updates the master; every change auto-submits so it is saved. #}
<script>var m=document.getElementById('master'),s=document.querySelectorAll('.sub'),f=document.getElementById('nf');
m.onchange=function(){s.forEach(function(x){x.checked=m.checked});f.submit()};
s.forEach(function(x){x.onchange=function(){m.checked=[].every.call(s,function(y){return y.checked});f.submit()}});</script>{% endblock %}"""

# Hand all templates to Flask.
app.jinja_loader = DictLoader({"base.html": BASE, "auth.html": AUTH, "onboarding.html": ONBOARD,
                               "home.html": HOME, "calendar.html": CAL, "settings.html": SETTINGS})

# ------------------------------------------------------------------ 7. CSS
# CSS = the design. Colours live in variables so they are defined once.
CSS = """
/* Colour variables: the 4 brand colours + pink/lilac for calendar. */
:root{--wine:#7C0E44;--rose:#AA5E7A;--dust:#D49A9A;--blush:#F2DACE;--bg:#FAFAFA;--card:#fff;--text:#1d1d1f;--mute:#6e6e73;--pink:#D63384;--lilac:#C9B6F2;--line:rgba(0,0,0,.08)}
/* Dark mode: swap background and card colours when the device uses dark mode. */
@media(prefers-color-scheme:dark){:root{--bg:#141414;--card:#1f1b1d;--text:#f5f5f7;--mute:#a1a1a6;--line:rgba(255,255,255,.1);--blush:#3a1f2b}}
/* Every element: border-box sizing, no default margins, 0.2s ease transitions. */
*{box-sizing:border-box;margin:0;transition:all .2s ease}
/* Page background. */
html{min-height:100%;background:var(--bg)}
/* Font (SF Pro Display + fallbacks) and the blush gradient. fixed + min-height 100vh = no white gaps. */
body{min-height:100vh;width:100%;color:var(--text);font:400 16px/1.5 "SF Pro Display",-apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif;
background:linear-gradient(160deg,var(--blush),var(--bg) 60%) fixed;overflow-x:hidden}
/* Headings: bold, tight tracking. Same sizes on every page. */
h1{font-size:28px;font-weight:700;letter-spacing:-.03em;line-height:1.15;margin-bottom:6px}
h2{font-size:21px;font-weight:700;letter-spacing:-.02em;margin:22px 0 12px}
h3{font-size:15px;font-weight:600;letter-spacing:-.01em;margin:14px 0 8px}
.card h2,.card h3:first-child{margin-top:0}
.muted{color:var(--mute)} .center{text-align:center} .err{color:#c0183c;margin:8px 0}
a{color:var(--wine)} small{font-size:13px;color:var(--mute)}
.wrap{width:100%;max-width:1100px;margin:0 auto;padding:20px 16px}
.padtab{padding-bottom:110px}
/* Sticky frosted top bar. */
.top{position:sticky;top:0;z-index:20;display:flex;align-items:center;justify-content:space-between;padding:12px 16px;
background:color-mix(in srgb,var(--bg) 80%,transparent);backdrop-filter:blur(20px);border-bottom:1px solid var(--line)}
.brand{font-size:20px;font-weight:700;letter-spacing:-.03em;color:var(--wine)}
.bell{font-size:22px;text-decoration:none;order:3}
/* Mobile tab bar fixed at the bottom. */
.tabs{position:fixed;bottom:0;left:0;right:0;display:flex;justify-content:space-around;padding:8px 8px calc(8px + env(safe-area-inset-bottom));
background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(20px);border-top:1px solid var(--line);z-index:30}
.tabs a{display:flex;flex-direction:column;align-items:center;font-size:11px;text-decoration:none;color:var(--mute);padding:4px 16px;border-radius:12px}
.tabs a span{font-size:20px} .tabs a.on{color:var(--wine);font-weight:600}
/* Shared card look: same padding, radius, soft shadow. */
.card{background:var(--card);border-radius:20px;padding:18px;box-shadow:0 2px 12px rgba(124,14,68,.08);min-width:0}
/* Mobile layout: 1 column. */
.grid{display:grid;grid-template-columns:1fr;gap:14px;margin-top:16px}
.grid2{display:grid;grid-template-columns:1fr;gap:14px;margin:16px 0}
.btn,.big-btn,.chip,.symc,.moodc,.cell{font:inherit;cursor:pointer;border:0}
/* Buttons use #7C0E44. */
.btn{display:inline-block;background:var(--wine);color:#fff;font-weight:600;padding:13px 22px;border-radius:14px;text-decoration:none;margin-top:12px;text-align:center}
.btn.ghost{background:transparent;color:var(--wine);border:1.5px solid var(--wine)}
/* Press effect: scale(0.97). */
.btn:active,.big-btn:active,.chip:active,.symc:active,.moodc:active,.cell:active,.tabs a:active{transform:scale(.97)}
.btn[disabled]{opacity:.4}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap} .row>.btn,.row>.big-btn{flex:1}
label{display:block;font-size:14px;font-weight:500;margin-top:12px}
input,select{display:block;width:100%;font:inherit;padding:12px 14px;margin-top:6px;border:1px solid var(--line);border-radius:12px;background:var(--bg);color:var(--text)}
input:focus,select:focus{outline:2px solid var(--rose)}
.auth{max-width:440px;margin:6vh auto}
.bar{height:6px;background:var(--line);border-radius:6px;overflow:hidden;margin-bottom:8px} .bar i{display:block;height:100%;background:var(--wine);width:0}
.opt{margin-top:10px} .opt input{display:none}
.opt span{display:block;padding:14px 16px;border:1.5px solid var(--line);border-radius:14px;cursor:pointer}
.opt input:checked+span{border-color:var(--wine);background:rgba(124,14,68,.08);font-weight:600}
/* Late banner; overflow-wrap stops long text breaking the layout. */
.late{background:var(--wine);color:#fff;padding:14px 18px;border-radius:16px;margin-bottom:16px;display:flex;gap:10px;font-weight:600;overflow-wrap:anywhere}
/* Ring: text centred over the SVG. */
.ringcard{position:relative;display:grid;place-items:center;min-height:240px}
.ring{width:200px;max-width:100%} .track{fill:none;stroke:var(--blush);stroke-width:10}
.prog{fill:none;stroke:var(--wine);stroke-width:10;stroke-linecap:round}
.ringtxt{position:absolute;display:flex;flex-direction:column;align-items:center;text-align:center}
.ringtxt b{font-size:30px;letter-spacing:-.03em} .ringtxt span{color:var(--rose);font-weight:600}
.big{font-size:38px;font-weight:700;letter-spacing:-.03em;line-height:1.2} .big.gray{color:var(--mute)}
.pill{display:inline-block;padding:6px 14px;border-radius:20px;font-weight:600;color:#fff}
.f-active{background:var(--lilac);color:#2b0a1a}.f-approaching{background:var(--pink)}.f-inactive{background:#9a9a9f}
.kv{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-bottom:1px solid var(--line)} .kv:last-child{border:0}
.guide ul{margin:12px 0 0 20px} .guide li{margin:4px 0} .guide small{color:inherit;opacity:.8}
.mhead{display:flex;justify-content:space-between;align-items:center} .mhead h2{margin:0}
.nav{font-size:28px;text-decoration:none;padding:0 12px}
.week,.days{display:grid;grid-template-columns:repeat(7,1fr);text-align:center}
.week span{font-size:12px;color:var(--mute);padding:10px 0 4px}
.cell{background:none;color:var(--text);padding:3px 0;position:relative;display:grid;place-items:center;border-radius:12px}
.cell.out{opacity:.35} .cell.sel{background:rgba(124,14,68,.1)}
/* Coloured circle behind each date number. */
.d{width:38px;height:38px;border-radius:50%;display:grid;place-items:center;font-weight:500}
.d.period{background:var(--wine);color:#fff}.d.pred{background:var(--rose);color:#fff}
.d.fert{background:var(--lilac);color:#2b0a1a}.d.ovul{background:#8c6fd9;color:#fff}
.d.today{outline:2px solid var(--text);outline-offset:2px}
/* Heart marker. */
.heart{position:absolute;right:18%;top:2px;font-size:11px;color:var(--pink);font-style:normal}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--mute);margin-top:12px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}
.legend .period{background:var(--wine)}.legend .pred{background:var(--rose)}.legend .fert{background:var(--lilac)}.legend .ovul{background:#8c6fd9}
/* Log panel slides in. */
#log{margin-top:16px;animation:in .2s ease} @keyframes in{from{opacity:0;transform:translateY(10px)}}
.big-btn{flex:1;padding:18px;font-size:18px;font-weight:600;border-radius:16px;background:var(--bg);color:var(--text);border:1.5px solid var(--line)}
.big-btn.on,.chip.on{background:var(--wine);color:#fff;border-color:var(--wine)}
.chip{padding:11px 16px;border-radius:20px;background:var(--bg);color:var(--text);border:1.5px solid var(--line)}
.wrap-row{margin-top:8px}
/* Symptom cards: 3 columns. */
.sym{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.symc,.moodc{display:flex;flex-direction:column;align-items:center;gap:4px;padding:12px 4px;border-radius:16px;background:var(--bg);color:var(--text);font-size:13px;border:1.5px solid var(--line);min-width:0}
.symc span,.moodc span{font-size:26px} .symc.on{background:var(--pink);border-color:var(--pink);color:#fff}
.moods{display:grid;grid-template-columns:repeat(3,1fr);gap:10px} .moodc.on{background:var(--wine);color:#fff;border-color:var(--wine)}
/* Sticky Save area. */
.savebar{position:sticky;bottom:78px;padding:10px 0 2px;margin-top:14px;background:linear-gradient(transparent,var(--card) 30%)}
.savebar .btn{width:100%;margin:0}
.tg{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--line);margin:0}
.tg span{display:flex;flex-direction:column} .tg input{width:50px;height:28px;margin:0;accent-color:var(--wine);flex-shrink:0}
/* Greyed, unclickable toggles. */
form.off .tg{opacity:.4;pointer-events:none}
/* Toast: gradient card, hidden until .show. */
#toast{position:fixed;left:50%;top:70px;transform:translate(-50%,-20px);opacity:0;pointer-events:none;z-index:99;max-width:calc(100vw - 32px);
display:flex;gap:12px;align-items:center;padding:12px 20px 12px 14px;border-radius:18px;color:#fff;background:linear-gradient(135deg,var(--wine),var(--rose));box-shadow:0 10px 30px rgba(124,14,68,.35)}
#toast.show{opacity:1;transform:translate(-50%,0)} #toast.out{opacity:0;transform:translate(-50%,-10px)}
#toast .ck{width:30px;height:30px;border-radius:50%;background:rgba(255,255,255,.25);display:grid;place-items:center;font-weight:700}
#toast small{display:block;color:rgba(255,255,255,.8)}
/* Tablet: 2-column grids. */
@media(min-width:768px){.grid,.grid2{grid-template-columns:repeat(2,1fr)} .wrap{padding:28px 24px} .padtab{padding-bottom:110px}}
/* Wide screens: tabs move into the top bar. */
@media(min-width:900px){.tabs{position:static;border:0;background:none;backdrop-filter:none;padding:0;gap:4px;margin-left:auto;margin-right:12px}
.tabs a{flex-direction:row;gap:6px;font-size:15px}.tabs a span{font-size:16px}.padtab{padding-bottom:40px}.savebar{bottom:0}}
/* Desktop: 3-column grid. */
@media(min-width:1280px){.grid{grid-template-columns:repeat(3,1fr)}}
"""

# Serve the CSS text as a stylesheet.
@app.route("/style.css")
def css():
    return Response(CSS, mimetype="text/css")

# ------------------------------------------------------------------ START
# Only runs when you start this file directly.
_c = connect(); _c.executescript(SCHEMA); _c.commit(); _c.close()
threading.thread(target=reminder_loop, daemon=True).start()

if name__ == "__main__":
    app.run(debug=True, use_reloader=False)
