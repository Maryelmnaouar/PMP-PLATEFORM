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
app.secret_key = "change-this-secret-please"

# -------------------------------------------------------
# DB HELPERS (POSTGRESQL)
# -------------------------------------------------------
def get_db():
    return psycopg2.connect(
        os.environ["DATABASE_URL"],
        cursor_factory=psycopg2.extras.RealDictCursor
    )

def init_db():
    db = get_db()
    c = db.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','operator','technician','chief')),
        prod_line TEXT,
        machine_assigned TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS tasks(
        id SERIAL PRIMARY KEY,
        line TEXT NOT NULL,
        machine TEXT NOT NULL,
        description TEXT NOT NULL,
        assigned_to INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('en_cours','cloturee')) DEFAULT 'en_cours',
        documentation TEXT,
        points INTEGER NOT NULL DEFAULT 1,
        frequency TEXT,
        created_at TEXT NOT NULL,
        closed_at TEXT,
        FOREIGN KEY(assigned_to) REFERENCES users(id)
    )
    """)

    # seed admin
    c.execute("SELECT COUNT(*) AS n FROM users")
    if c.fetchone()["n"] == 0:
        c.execute("""
            INSERT INTO users(username,password_hash,role,prod_line,machine_assigned)
            VALUES (%s,%s,%s,%s,%s)
        """, ("admin", generate_password_hash("1234"), "admin", None, None))

    db.commit()
    db.close()

# ⚠️ CRITIQUE : initialisation DB au chargement (Render / Gunicorn)
init_db()

# -------------------------------------------------------
# LECTURE EXCEL (INCHANGÉ)
# -------------------------------------------------------
def load_task_templates():
    if not os.path.exists(EXCEL_PATH):
        return [], [], {}, [], []

    df = pd.read_excel(EXCEL_PATH, sheet_name=EXCEL_SHEET)

    df = df.rename(columns={
        "Line": "Ligne",
        "EQUIPEMENT": "Machine",
        "TÂCHE": "Description",
        "FREQUENCE": "Frequence",
        "INTERVENANT": "Intervenant",
        "Emplacement Documentation": "Documentation"
    })

    for col in ["Ligne", "Machine", "Description", "Frequence", "Intervenant", "Documentation"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    records = df.to_dict(orient="records")
    lignes = sorted({r["Ligne"] for r in records if r["Ligne"]})

    machines_par_ligne = {}
    for r in records:
        if r["Ligne"] and r["Machine"]:
            machines_par_ligne.setdefault(r["Ligne"], set()).add(r["Machine"])

    machines_par_ligne = {k: sorted(v) for k, v in machines_par_ligne.items()}
    intervenants = sorted({r["Intervenant"] for r in records})
    frequences = sorted({r["Frequence"] for r in records})

    return records, lignes, machines_par_ligne, intervenants, frequences

# -------------------------------------------------------
# AUTH
# -------------------------------------------------------
def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    u = db.cursor().execute(
        "SELECT * FROM users WHERE id=%s",
        (session["user_id"],)
    ).fetchone()
    db.close()
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
# KPI (LOGIQUE STRICTEMENT IDENTIQUE)
# -------------------------------------------------------
def get_global_kpis(filters=None):
    filters = filters or {}
    line = (filters.get("line") or "").strip()
    machine = (filters.get("machine") or "").strip()
    start_date = (filters.get("start_date") or "").strip()
    end_date = (filters.get("end_date") or "").strip()

    db = get_db()
    c = db.cursor()

    where = []
    params = []

    if line:
        where.append("line=%s")
        params.append(line)
    if machine:
        where.append("machine=%s")
        params.append(machine)
    if start_date:
        where.append("DATE(created_at) >= %s")
        params.append(start_date)
    if end_date:
        where.append("DATE(created_at) <= %s")
        params.append(end_date)

    where_sql = "WHERE " + " AND ".join(where) if where else ""

    c.execute(f"SELECT COUNT(*) n FROM tasks {where_sql}", params)
    total = c.fetchone()["n"]

    c.execute(f"SELECT COUNT(*) n FROM tasks {where_sql} AND status='cloturee'", params)
    done = c.fetchone()["n"]

    taux = round(done * 100 / total) if total else 0
    color = "green" if taux >= 80 else "orange" if taux >= 60 else "red"

    c.execute(
        f"SELECT COALESCE(SUM(points),0) s FROM tasks {where_sql} AND status='cloturee'",
        params
    )
    score = c.fetchone()["s"]

    db.close()

    return {
        "total_taches": total,
        "taches_realisees": done,
        "taux_realisation": taux,
        "taux_couleur": color,
        "score_global": score
    }

# -------------------------------------------------------
# ROUTES (INCHANGÉES)
# -------------------------------------------------------
@app.route("/")
@login_required()
def index():
    filters = {
        "line": request.args.get("line",""),
        "machine": request.args.get("machine",""),
        "start_date": request.args.get("start_date",""),
        "end_date": request.args.get("end_date",""),
    }

    kpi = get_global_kpis(filters)
    _, lignes, machines_par_ligne, _, _ = load_task_templates()

    return render_template(
        "index.html",
        **kpi,
        lignes=lignes,
        machines_par_ligne=machines_par_ligne,
        filters=filters,
        current_year=datetime.now().year
    )

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","")
        password = request.form.get("password","")

        db = get_db()
        u = db.cursor().execute(
            "SELECT * FROM users WHERE username=%s",
            (username,)
        ).fetchone()
        db.close()

        if u and check_password_hash(u["password_hash"], password):
            session["user_id"] = u["id"]
            session["role"] = u["role"]
            return redirect(url_for("index"))

        return render_template("login.html", error="Nom ou mot de passe incorrect")

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# -------------------------------------------------------
# MAIN
# -------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)