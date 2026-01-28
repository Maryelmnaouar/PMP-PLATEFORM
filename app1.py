from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
import psycopg2.extras
from psycopg2 import IntegrityError
import os
from datetime import datetime
import pandas as pd
from openpyxl import load_workbook

# -------------------------------------------------------
# CONFIG
# -------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
EXCEL_PATH = os.path.join(BASE_DIR, "data", "plan_pmp.xlsx")
EXCEL_SHEET = "CSD PET3"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret")

# -------------------------------------------------------
# DB HELPERS (PostgreSQL)
# -------------------------------------------------------
def get_db():
    return psycopg2.connect(os.environ["DATABASE_URL"])

def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','operator','technician','chief')),
        prod_line TEXT,
        machine_assigned TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks(
        id SERIAL PRIMARY KEY,
        line TEXT NOT NULL,
        machine TEXT NOT NULL,
        description TEXT NOT NULL,
        assigned_to INTEGER REFERENCES users(id),
        status TEXT NOT NULL CHECK(status IN ('en_cours','cloturee')) DEFAULT 'en_cours',
        documentation TEXT,
        points INTEGER NOT NULL DEFAULT 1,
        frequency TEXT,
        created_at TIMESTAMP NOT NULL,
        closed_at TIMESTAMP
    )
    """)

    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        cur.execute("""
            INSERT INTO users(username,password_hash,role)
            VALUES (%s,%s,'admin')
        """, ("admin", generate_password_hash("1234")))

    conn.commit()
    conn.close()
with app.app_context():
    init_db() 

# -------------------------------------------------------
# AUTH
# -------------------------------------------------------
def current_user():
    if "user_id" not in session:
        return None

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE id=%s", (session["user_id"],))
    u = cur.fetchone()
    conn.close()

    if not u:
        session.clear()
    return u

def login_required(role=None):
    def decorator(f):
        from functools import wraps
        @wraps(f)
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u:
                return redirect(url_for("login"))
            if role and u["role"] != role:
                return redirect(url_for("index"))
            return f(*args, **kwargs)
        return wrapper
    return decorator

# -------------------------------------------------------
# KPI
# -------------------------------------------------------
def get_global_kpis(filters=None):
    filters = filters or {}
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    where = ["1=1"]
    params = []

    if filters.get("line"):
        where.append("line=%s")
        params.append(filters["line"])

    where_sql = "WHERE " + " AND ".join(where)

    # Total tâches
    cur.execute(f"""
        SELECT COUNT(*) n
        FROM tasks
        {where_sql}
    """, params)
    total = cur.fetchone()["n"]

    # Tâches clôturées
    cur.execute(f"""
        SELECT COUNT(*) n
        FROM tasks
        {where_sql} AND status='cloturee'
    """, params)
    done = cur.fetchone()["n"]

    taux = round(done * 100 / total) if total else 0

    # Score global
    cur.execute(f"""
        SELECT COALESCE(SUM(points), 0) s
        FROM tasks
        {where_sql} AND status='cloturee'
    """, params)
    score = cur.fetchone()["s"]

    conn.close()

    return {
        "total_taches": total,
        "taches_realisees": done,
        "taux_realisation": taux,
        "taux_couleur": "green" if taux >= 80 else "orange" if taux >= 60 else "red",
        "score_global": score
    }
# -------------------------------------------------------
# ROUTES
# -------------------------------------------------------
@app.route("/")
@login_required()
def index():
    filters = {
        "line": request.args.get("line", "")
    }
    kpi = get_global_kpis(filters)
    machines_par_ligne=machines_par_ligne()
    
    return render_template("index.html", **kpi, machines_par_ligne=machines_par_ligne, current_year=datetime.now().year)

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        u = cur.fetchone()
        conn.close()

        if u and check_password_hash(u["password_hash"], password):
            session["user_id"] = u["id"]
            session["role"] = u["role"]
            return redirect(url_for("index"))

        return render_template("login.html", error="Identifiants invalides")

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# -------------------------------------------------------
# ADMIN
# -------------------------------------------------------
@app.route("/admin")
@login_required("admin")
def admin_dashboard():
    return render_template("admin_dashboard.html", current_year=datetime.now().year)

@app.route("/admin/settings")
@login_required("admin")
def admin_settings():
    return render_template("admin_settings.html", current_year=datetime.now().year)

@app.route("/admin/user/create", methods=["POST"])
@login_required("admin")
def admin_create_user():
    username = request.form["username"]
    password = request.form["password"]
    role = request.form["role"]

    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute("""
            INSERT INTO users(username,password_hash,role)
            VALUES (%s,%s,%s)
        """, (username, generate_password_hash(password), role))
        conn.commit()
        flash("Utilisateur créé", "ok")
    except IntegrityError:
        flash("Utilisateur existe déjà", "err")
    finally:
        conn.close()

    return redirect(url_for("admin_dashboard"))

# -------------------------------------------------------
# OPERATOR
# -------------------------------------------------------
@app.route("/me")
@login_required()
def operator_dashboard():
    user = current_user()
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT * FROM tasks
        WHERE assigned_to=%s
        ORDER BY status, created_at DESC
    """, (user["id"],))
    tasks = cur.fetchall()

    conn.close()

    return render_template("operator_dashboard.html", me=user, tasks=tasks)

# -------------------------------------------------------
# START
# -------------------------------------------------------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
