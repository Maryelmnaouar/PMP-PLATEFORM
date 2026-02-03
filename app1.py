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
    cur = db.cursor()

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

    cur.execute("SELECT COUNT(*) AS n FROM users")
    if cur.fetchone()["n"] == 0:
        cur.execute("""
            INSERT INTO users(username,password_hash,role,prod_line,machine_assigned)
            VALUES (%s,%s,%s,%s,%s)
        """, ("admin", generate_password_hash("1234"), "admin", None, None))

    db.commit()
    db.close()

# Initialisation DB (Render / Gunicorn)
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
# AUTH HELPERS
# -------------------------------------------------------
def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM users WHERE id=%s", (session["user_id"],))
    u = cur.fetchone()
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
# EXCEL : Ajout tâche
# -------------------------------------------------------
def append_task_to_excel(line, machine, description, frequence, intervenant):
    if not os.path.exists(EXCEL_PATH):
        return

    wb = load_workbook(EXCEL_PATH)
    ws = wb[EXCEL_SHEET]

    headers = {}
    for idx, cell in enumerate(ws[1], 1):
        title = str(cell.value).strip().upper() if cell.value else ""
        headers[title] = idx

    new_row = [None] * len(ws[1])

    def set_col(title, value):
        col = headers.get(title)
        if col:
            new_row[col - 1] = value

    task_header = "TÂCHE" if "TÂCHE" in headers else ("TACHE" if "TACHE" in headers else None)

    set_col("LINE", line)
    set_col("EQUIPEMENT", machine)
    if task_header:
        set_col(task_header, description)
    set_col("FREQUENCE", frequence)
    set_col("INTERVENANT", intervenant)

    ws.append(new_row)
    wb.save(EXCEL_PATH)
    wb.close()

# -------------------------------------------------------
# KPI (LOGIQUE IDENTIQUE)
# -------------------------------------------------------
def get_global_kpis(filters=None):
    filters = filters or {}
    db = get_db()
    cur = db.cursor()

    where = []
    params = []

    if filters.get("line"):
        where.append("line=%s")
        params.append(filters["line"])
    if filters.get("machine"):
        where.append("machine=%s")
        params.append(filters["machine"])

    where_sql = "WHERE " + " AND ".join(where) if where else ""

    # Total tâches
    cur.execute(f"SELECT COUNT(*) n FROM tasks {where_sql}", params)
    total = cur.fetchone()["n"]

    # Tâches clôturées
    if where_sql:
        cur.execute(
            f"SELECT COUNT(*) n FROM tasks {where_sql} AND status='cloturee'",
            params
        )
    else:
        cur.execute(
            "SELECT COUNT(*) n FROM tasks WHERE status='cloturee'"
        )
    done = cur.fetchone()["n"]


    taux = round(done * 100 / total) if total else 0
    color = "green" if taux >= 80 else "orange" if taux >= 60 else "red"

    if where_sql:
        cur.execute(
        f"SELECT COALESCE(SUM(points),0) s FROM tasks {where_sql} AND status='cloturee'",
        params
    )
    else:
        cur.execute(
        "SELECT COALESCE(SUM(points),0) s FROM tasks WHERE status='cloturee'"
    )
    score = cur.fetchone()["s"]

    db.close()

    return {
        "total_taches": total,
        "taches_realisees": done,
        "taux_realisation": taux,
        "taux_couleur": color,
        "score_global": score
    }

# -------------------------------------------------------
# ROUTES PUBLIQUES
# -------------------------------------------------------
@app.route("/")
@login_required()
def index():
    filters = {
        "line": request.args.get("line",""),
        "machine": request.args.get("machine","")
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
        cur = db.cursor()
        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        u = cur.fetchone()
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
# ADMIN DASHBOARD
# -------------------------------------------------------
@app.route("/admin")
@login_required(role="admin")
def admin_dashboard():
    _, lignes, machines_L, intervenants, frequences = load_task_templates()
    return render_template(
        "admin_dashboard.html",
        lignes=lignes,
        machines_par_ligne=machines_L,
        intervenants=intervenants,
        frequences=frequences,
        current_year=datetime.now().year
    )

# -------------------------------------------------------
# ADMIN : USERS
# -------------------------------------------------------
@app.route("/admin/users")
@login_required(role="admin")
def admin_users():
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, username, role, prod_line, machine_assigned
        FROM users
        WHERE role!='admin'
        ORDER BY username
    """)
    users = cur.fetchall()
    db.close()

    _, lignes, machines_pl, intervenants, frequences = load_task_templates()

    return render_template(
        "admin_users.html",
        users=users,
        lignes=lignes,
        machines_par_ligne=machines_pl,
        intervenants=intervenants,
        frequences=frequences,
        current_year=datetime.now().year
    )

# -------------------------------------------------------
# OPERATOR DASHBOARD
# -------------------------------------------------------
@app.route("/me")
@login_required()
def operator_dashboard():
    user = current_user()
    db = get_db()
    cur = db.cursor()

    cur.execute("""
        SELECT *
        FROM tasks
        WHERE assigned_to=%s
        ORDER BY CASE status WHEN 'en_cours' THEN 0 ELSE 1 END, created_at DESC
    """, (user["id"],))
    tasks = cur.fetchall()

    cur.execute("""
        SELECT COALESCE(SUM(points),0)
        FROM tasks
        WHERE assigned_to=%s AND status='cloturee'
    """, (user["id"],))
    score = cur.fetchone()["coalesce"]

    db.close()

    return render_template(
        "operator_dashboard.html",
        me=user,
        tasks=tasks,
        score_total=score
    )

# -------------------------------------------------------
# CLOSE TASK
# -------------------------------------------------------
@app.route("/me/task/close/<int:task_id>", methods=["POST"])
@login_required()
def me_close_task(task_id):
    user = current_user()
    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT * FROM tasks WHERE id=%s", (task_id,))
    task = cur.fetchone()

    if not task or task["assigned_to"] != user["id"]:
        flash("Action interdite.", "err")
        db.close()
        return redirect(url_for("operator_dashboard"))

    cur.execute("""
        UPDATE tasks
        SET status='cloturee', closed_at=%s
        WHERE id=%s
    """, (datetime.now().isoformat(), task_id))

    db.commit()
    db.close()

    flash("Tâche validée, bravo !", "ok")
    return redirect(url_for("operator_dashboard"))

# -------------------------------------------------------
# CONTEXT PROCESSOR
# -------------------------------------------------------
@app.context_processor
def inject_routes():
    return dict(index=url_for("index"))

# -------------------------------------------------------
# MAIN
# -------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)