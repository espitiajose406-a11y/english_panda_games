import os
import re

import pymysql
from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from pymysql.cursors import DictCursor
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------------------------------------------------------- configuracion
BASE = os.path.dirname(os.path.abspath(__file__))

# Valores por defecto de XAMPP (usuario root, sin contrasena). Se pueden cambiar
# con variables de entorno: DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME.
# Para bases en la nube que exigen conexion segura (Aiven, por ejemplo), define
# DB_SSL_CA con el nombre del certificado (ca.pem) guardado junto a app.py.
DB_CONFIG = dict(
    host=os.environ.get("DB_HOST", "127.0.0.1"),
    port=int(os.environ.get("DB_PORT", "3306")),
    user=os.environ.get("DB_USER", "root"),
    password=os.environ.get("DB_PASSWORD", ""),
    database=os.environ.get("DB_NAME", "panda_games"),
    charset="utf8mb4",
    cursorclass=DictCursor,
)
_ca = os.environ.get("DB_SSL_CA")
if _ca:
    DB_CONFIG["ssl"] = {"ca": _ca if os.path.isabs(_ca) else os.path.join(BASE, _ca)}

GAMES = {"surprise", "run", "shot"}
LEVELS = {"A1", "A2", "B1", "B2"}
MAX_XP_PER_GAME = 210          # 10 preguntas x (15 XP + 6 de bonus por racha)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-esta-clave-en-produccion")


def login_required(view):
    """Redirige a /login si no hay sesion iniciada. Se usa en las paginas
    que exigen cuenta: el Home y los tres juegos."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("uid"):
            return redirect(url_for("login_page", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def redirect_if_logged_in(view):
    """En /login y /register: si ya hay sesion, va directo al Home."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("uid"):
            return redirect(url_for("home"))
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------- base de datos
def db():
    return pymysql.connect(**DB_CONFIG)


def fetch_one(sql, args=()):
    con = db()
    try:
        with con.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone()
    finally:
        con.close()


def fetch_all(sql, args=()):
    con = db()
    try:
        with con.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        con.close()


# Puesto = 1 + cuantos jugadores tienen mas XP (los empates comparten puesto)
RANK_SQL = """
SELECT (SELECT COUNT(*) FROM ranking r2 WHERE r2.xp > r.xp) + 1 AS `position`,
       r.user_id AS user_id, u.username AS username,
       r.xp AS xp, r.games_played AS games_played
FROM ranking r
JOIN users u ON u.id = r.user_id
"""


def my_row(uid):
    return fetch_one(RANK_SQL + " WHERE r.user_id = %s", (uid,))


@app.errorhandler(pymysql.MySQLError)
def database_error(e):
    app.logger.error("Database error: %s", e)
    if request.path.startswith("/api/"):
        return jsonify(error="Database unavailable. Is MySQL running?"), 503
    return "Database unavailable", 503


# ---------------------------------------------------------------- paginas
@app.route("/")
@login_required
def home():
    return render_template("index.html")


@app.route("/surprise")
@login_required
def surprise():
    return render_template("surprise.html")


@app.route("/run")
@login_required
def run():
    return render_template("run.html")


@app.route("/shot")
@login_required
def shot():
    return render_template("shot.html")


@app.route("/register")
@redirect_if_logged_in
def register_page():
    return render_template("register.html")


@app.route("/login")
@redirect_if_logged_in
def login_page():
    return render_template("login.html")


@app.route("/ranking")
def ranking_page():
    return render_template("ranking.html")


@app.route("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- API: cuentas
def clean_credentials(data):
    username = str((data or {}).get("username", "")).strip()
    password = str((data or {}).get("password", ""))
    return username, password


@app.post("/api/register")
def api_register():
    username, password = clean_credentials(request.get_json(silent=True))
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", username):
        return jsonify(error="Username: 3-20 letters, numbers or _"), 400
    if len(password) < 6:
        return jsonify(error="Password must have at least 6 characters"), 400

    con = db()
    try:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                (username, generate_password_hash(password)),
            )
            uid = cur.lastrowid
            cur.execute("INSERT INTO ranking (user_id) VALUES (%s)", (uid,))
        con.commit()
    except pymysql.err.IntegrityError:
        con.rollback()
        return jsonify(error="That username is already taken"), 409
    finally:
        con.close()

    session["uid"], session["username"] = uid, username
    return jsonify(ok=True, username=username)


@app.post("/api/login")
def api_login():
    username, password = clean_credentials(request.get_json(silent=True))
    u = fetch_one("SELECT id, username, password_hash FROM users WHERE username = %s", (username,))
    if not u or not check_password_hash(u["password_hash"], password):
        return jsonify(error="Wrong username or password"), 401

    con = db()
    try:
        with con.cursor() as cur:
            cur.execute("INSERT IGNORE INTO ranking (user_id) VALUES (%s)", (u["id"],))
        con.commit()
    finally:
        con.close()

    session["uid"], session["username"] = u["id"], u["username"]
    return jsonify(ok=True, username=u["username"])


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def api_me():
    uid = session.get("uid")
    row = my_row(uid) if uid else None
    if not row:
        return jsonify(logged_in=False)
    return jsonify(logged_in=True, username=row["username"], xp=int(row["xp"]),
                   position=int(row["position"]), games_played=int(row["games_played"]))


# ---------------------------------------------------------------- API: puntos y puestos
@app.post("/api/score")
def api_score():
    uid = session.get("uid")
    if not uid:
        return jsonify(error="not_logged_in"), 401

    data = request.get_json(silent=True) or {}
    xp, game, level = data.get("xp"), data.get("game"), data.get("level")
    if (not isinstance(xp, int) or isinstance(xp, bool) or not 0 <= xp <= MAX_XP_PER_GAME
            or game not in GAMES or level not in LEVELS):
        return jsonify(error="Invalid score"), 400

    con = db()
    try:
        with con.cursor() as cur:
            cur.execute("INSERT INTO scores (user_id, game, `level`, xp) VALUES (%s, %s, %s, %s)",
                        (uid, game, level, xp))
            cur.execute("UPDATE ranking SET xp = xp + %s, games_played = games_played + 1 "
                        "WHERE user_id = %s", (xp, uid))
        con.commit()
    finally:
        con.close()

    row = my_row(uid)
    return jsonify(ok=True, xp=int(row["xp"]), position=int(row["position"]))


@app.get("/api/ranking")
def api_ranking():
    rows = fetch_all(RANK_SQL + " ORDER BY `position`, u.username LIMIT 50")
    uid = session.get("uid")
    me = my_row(uid) if uid else None
    clean = lambda r: {k: (int(v) if k != "username" else v) for k, v in r.items()}
    return jsonify(ranking=[clean(r) for r in rows], me=clean(me) if me else None)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
