from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
from datetime import datetime
import pandas as pd
from sqlite3 import IntegrityError
from openpyxl import load_workbook
# -------------------------------------------------------
# CONFIG
# -------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
EXCEL_PATH = os.path.join(BASE_DIR, "data", "plan_pmp.xlsx")
EXCEL_SHEET = "CSD PET3"
APP_DB = "pmp.db"

app = Flask(__name__)
app.secret_key = "change-this-secret-please"
def sturtup():
    init_db()

# -------------------------------------------------------
# DB HELPERS
# -------------------------------------------------------
def get_db():
    conn = sqlite3.connect(APP_DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    # USERS TABLE
    c.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','operator','technician','chief')),
        prod_line TEXT,
        machine_assigned TEXT
    )
    """)

    # TASKS TABLE
    c.execute("""
    CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    # Add frequency column if missing
    c.execute("PRAGMA table_info(tasks)")
    cols = [row[1] for row in c.fetchall()]
    if "frequency" not in cols:
        c.execute("ALTER TABLE tasks ADD COLUMN frequency TEXT")

    # Seed admin
    c.execute("SELECT COUNT(*) AS n FROM users")
    if c.fetchone()["n"] == 0:
        c.execute("""
            INSERT INTO users(username,password_hash,role,prod_line,machine_assigned)
            VALUES (?,?,?,?,?)
        """, ("admin", generate_password_hash("1234"), "admin", None, None))

    conn.commit()
    conn.close()

# -------------------------------------------------------
# LECTURE EXCEL
# -------------------------------------------------------
def load_task_templates():
    """
    Retourne :
      records, lignes, machines_par_ligne, intervenants, frequences
    """
    if not os.path.exists(EXCEL_PATH):
        print("⚠ Excel NOT FOUND:", EXCEL_PATH)
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

    # Nettoyage
    for col in ["Ligne", "Machine", "Description", "Frequence", "Intervenant","Documentation"]:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    records = df.to_dict(orient="records")
    lignes = sorted({r["Ligne"] for r in records if r["Ligne"]})

    machines_par_ligne = {}
    for r in records:
        L = r["Ligne"]
        M = r["Machine"]
        if L and M:
            machines_par_ligne.setdefault(L, set()).add(M)

    machines_par_ligne = {L: sorted(list(Ms)) for L, Ms in machines_par_ligne.items()}
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
    u = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    db.close()
    if u is None :
        session.clear()
        return None
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
                return redirect(url_for("admin_dashboard" if u["role"] == "admin" else "operator_dashboard"))
            return f(*args, **kwargs)
        return wrapper
    return decorator

# -------------------------------------------------------
# EXCEL : Ajout d’une ligne
# -------------------------------------------------------
def append_task_to_excel(line, machine, description, frequence, intervenant):
    if not os.path.exists(EXCEL_PATH):
        print("⚠ Excel not found:", EXCEL_PATH)
        return

    wb = load_workbook(EXCEL_PATH)
    ws = wb[EXCEL_SHEET]

    # Mapping colonnes
    headers = {}
    for idx, cell in enumerate(ws[1], 1):
        title = str(cell.value).strip().upper() if cell.value else ""
        headers[title] = idx

    row_len = len(ws[1])
    new_row = [None] * row_len

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
# MAPPING INTERVENANT → rôle
# -------------------------------------------------------
def _role_from_intervenant(x):
    x = (x or "").lower()
    if "conduct" in x:
        return "operator"
    if "mec" in x or "élec" in x or "elec" in x:
        return "technician"
    return "operator"
def get_global_kpis(filters=None):
    """
    Calcule les KPIs globaux avec filtres possibles :
    filters = {
        "line": "...",
        "machine": "...",
        "start_date": "YYYY-MM-DD",
        "end_date":   "YYYY-MM-DD",
    }
    """
    if filters is None:
        filters = {}

    line       = (filters.get("line") or "").strip()
    machine    = (filters.get("machine") or "").strip()
    start_date = (filters.get("start_date") or "").strip()
    end_date   = (filters.get("end_date") or "").strip()

    db = get_db()
    c = db.cursor()

    # -------- construction du WHERE commun (sur created_at) ----------
    where_clauses = []
    params = []

    if line:
        where_clauses.append("line = ?")
        params.append(line)

    if machine:
        where_clauses.append("machine = ?")
        params.append(machine)

    if start_date:
        where_clauses.append("DATE(substr(created_at,1,10)) >= ?")
        params.append(start_date)

    if end_date:
        where_clauses.append("DATE(substr(created_at,1,10)) <= ?")
        params.append(end_date)

    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)
    else:
        where_sql = ""

    base_params = list(params)
    # -------- Total tâches ----------
    sql_total = f"SELECT COUNT(*) AS n FROM tasks {where_sql}"
    c.execute(sql_total, base_params)
    total = c.fetchone()["n"]

    # -------- Tâches réalisées ----------
    if where_sql:
        where_done = where_sql + " AND status='cloturee'"
        params_done = base_params
    else:
        where_done = "WHERE status='cloturee'"
        params_done = []
    sql_done = f"SELECT COUNT(*) AS n FROM tasks {where_done}"
    c.execute(sql_done, params_done)
    done = c.fetchone()["n"]

    # -------- Taux de réalisation ----------
    taux = round(done * 100 / total) if total > 0 else 0
    if taux >= 80:
        color = "green"
    elif taux >= 60:
        color = "orange"
    else:
        color = "red"
    # -------- Score global ----------
    sql_score = f"SELECT COALESCE(SUM(points),0) AS s FROM tasks {where_done}"
    c.execute(sql_score, params_done)
    score_global = c.fetchone()["s"]

    # -------- Top opérateurs / techniciens ----------
    # On réutilise les mêmes filtres (line/machine/date) mais sur t.*
    extra_cond = ["t.status='cloturee'"]
    top_params = []

    if line:
        extra_cond.append("t.line = ?")
        top_params.append(line)
    if machine:
        extra_cond.append("t.machine = ?")
        top_params.append(machine)
    if start_date:
        extra_cond.append("DATE(substr(t.created_at,1,10)) >= ?")
        top_params.append(start_date)
    if end_date:
        extra_cond.append("DATE(substr(t.created_at,1,10)) <= ?")
        top_params.append(end_date)

    extra_sql = " AND ".join(extra_cond)

    # Top opérateurs
    c.execute(f"""
        SELECT u.username As NOM, COALESCE(SUM(t.points),0) AS score
        FROM users u
        LEFT JOIN tasks t
               ON t.assigned_to = u.id
              AND {extra_sql}
        WHERE u.role = 'operator'
        GROUP BY u.id
        ORDER BY score DESC
        LIMIT 3
    """, top_params)
    top_op = c.fetchall()

    # Top techniciens
    c.execute(f"""
        SELECT u.username As NOM, COALESCE(SUM(t.points),0) AS score
        FROM users u
        LEFT JOIN tasks t
               ON t.assigned_to = u.id
              AND {extra_sql}
        WHERE u.role = 'technician'
        GROUP BY u.id
        ORDER BY score DESC
        LIMIT 3
    """, top_params)
    top_tech = c.fetchall()
    
    # -------- Actions critiques (en cours) ----------
    if where_sql:
        where_crit = where_sql + " AND status='en_cours'"
        params_crit = base_params
    else:
        where_crit = "WHERE status='en_cours'"
        params_crit = []

    sql_crit = f"""
        SELECT line AS ligne, machine, points
        FROM tasks
        {where_crit}
        ORDER BY points DESC, id DESC
        LIMIT 3
    """
    c.execute(sql_crit, params_crit)
    critical = c.fetchall()

    db.close()

    return {
        "total_taches": total,
        "taches_realisees": done,
        "taux_realisation": taux,
        "taux_couleur": color,
        "score_global": score_global,
        "top_operateurs": top_op,
        "top_techniciens": top_tech,
        "actions_critiques": critical
    }
# -------------------------------------------------------
# ROUTES PUBLIQUES
# -------------------------------------------------------
@app.route("/")
@login_required()
def index():
    # Récupération des filtres depuis la barre de filtre (GET)
    line       = (request.args.get("line") or "").strip()
    machine    = (request.args.get("machine") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date   = (request.args.get("end_date") or "").strip()

    filters = {
        "line": line,
        "machine": machine,
        "start_date": start_date,
        "end_date": end_date,
    }

    kpi = get_global_kpis(filters)

    # Pour alimenter les listes déroulantes
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
        username = request.form.get("username","").strip()
        password = request.form.get("password","")

        db = get_db()
        u = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        db.close()

        if u and check_password_hash(u["password_hash"], password):
            session["user_id"] = u["id"]
            session["role"] = u["role"]
            return redirect(url_for("index"))
        else:
            return render_template("login.html", error="Nom ou mot de passe incorrect")

    return render_template("login.html")

@app.route("/platform")
def platform_redirect():
    # Pas connecté → login
    if "user_id" not in session:
        return redirect(url_for("login"))

    # Connecté → redirection selon rôle
    role = session.get("role")

    if role == "admin":
        return redirect(url_for("admin_dashboard"))
    else:
        return redirect(url_for("operator_dashboard"))

# -------------------------------------------------------
# LOGOUT
# -------------------------------------------------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# -------------------------------------------------------
# ADMIN : Tableau de bord principal
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
# ADMIN : Création utilisateur
# -------------------------------------------------------
@app.route("/admin/user/create", methods=["POST"])
@login_required(role="admin")
def admin_create_user():
    username = request.form.get("username","").strip()
    password = request.form.get("password","").strip()
    interv_choice = request.form.get("intervenant_type","").strip()
    prod_line = request.form.get("prod_line","").strip()
    machines = request.form.getlist("machine_assigned")
    machines = [m.strip() for m in machines if m.strip()]
    machine_assigned = "|".join(machines)

    role = _role_from_intervenant(interv_choice)

    if not username or not password or not prod_line or not machines:
        flash("Remplissez bien tous les champs.", "err")
        return redirect(url_for("admin_users"))

    db = get_db()
    try:
        # Vérifier si username existe
        cur = db.execute("SELECT id FROM users WHERE username=?", (username,))
        if cur.fetchone():
            flash("Nom d'utilisateur déjà utilisé.", "err")
            db.close()
            return redirect(url_for("admin_users"))

        # insertion
        db.execute("""
            INSERT INTO users(username, password_hash, role, prod_line, machine_assigned)
            VALUES (?,?,?,?,?)
        """, (username, generate_password_hash(password), role, prod_line, machine_assigned))
        db.commit()
        flash(f"Utilisateur {username} créé.", "ok")

    except IntegrityError as e:
        flash("Erreur SQL création utilisateur", "err")
        print("SQL ERROR USER:", e)

    finally:
        db.close()

    return redirect(url_for("admin_users"))

@app.route("/documentation")
def documentation():
    docs_dir = os.path.join(app.root_path, "static\images", "docs")

    pdfs = []
    if os.path.exists(docs_dir):
        pdfs = [f for f in os.listdir(docs_dir) if f.lower().endswith(".pdf")]

    return render_template("documentation.html", pdfs=pdfs)

# -------------------------------------------------------
# PAGE ADMIN : gestion utilisateurs
# -------------------------------------------------------
@app.route("/admin/users")
@login_required(role="admin")
def admin_users():
    db = get_db()
    users = db.execute("""
        SELECT id, username, role, prod_line, machine_assigned
        FROM users
        WHERE role!='admin'
        ORDER BY username
    """).fetchall()
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
# ADMIN : PAGE assignation automatique
# -------------------------------------------------------
@app.route("/admin/auto")
@login_required(role="admin")
def admin_auto_page():
    _, lignes, machines_L, intervenants, frequences = load_task_templates()
    return render_template(
        "admin_auto_page.html",
        lignes=lignes,
        machines_par_ligne=machines_L,
        intervenants=intervenants,
        frequences=frequences,
        current_year=datetime.now().year
    )

# -------------------------------------------------------
# ROTATION AUTOMATIQUE
# -------------------------------------------------------
def _auto_assign_pmp(line: str, freq_prefix: str, offset=0):
    """
    Répartition automatique avec rotation.
    """
    records, _, _, _, _ = load_task_templates()

    # filtrage freq + ligne
    freq_prefix = freq_prefix.lower()
    r_filtered = [r for r in records
                  if r["Ligne"] == line
                  and freq_prefix in str(r["Frequence"]).lower()]

    if not r_filtered:
        return 0

    # regrouper par machine + role
    by_machine_role = {}
    for r in r_filtered:
        machine = r["Machine"]
        role = _role_from_intervenant(r["Intervenant"])
        by_machine_role.setdefault((machine, role), []).append(r)

    db = get_db()
    c = db.cursor()
    created = 0

    # empêcher double assignation machine/user dans un même run
    used_users = set()

    for (machine, role), rows in by_machine_role.items():
        # trouver users
        users = c.execute("""
            SELECT id, machine_assigned
            FROM users
            WHERE role=? AND prod_line=?
        """, (role, line)).fetchall()

        # filtrer ceux liés à cette machine
        user_ids = []
        for u in users:
            mlist = (u["machine_assigned"] or "").split("|")
            mlist = [x.strip() for x in mlist if x.strip()]
            if machine in mlist:
                user_ids.append(u["id"])

        if not user_ids:
            continue

        # rotation
        candidate_ids = [u for u in user_ids if u not in used_users]
        if not candidate_ids:
            candidate_ids = user_ids

        # tri rotation
        candidate_ids = candidate_ids[offset:] + candidate_ids[:offset]
        chosen = candidate_ids[0]
        used_users.add(chosen)

        now = datetime.now().isoformat()

        for r in rows:
            desc = r["Description"]
            freq = r["Frequence"]
            doc = r.get("Documentation")

            c.execute("""
                INSERT INTO tasks(line, machine, description, assigned_to, status, points, frequency, documentation, created_at)
                VALUES (?,?,?,?, 'en_cours', ?, ?, ?,?)
            """, (line, machine, desc, chosen, 3, freq, doc, now))
            created += 1

    db.commit()
    db.close()
    return created

# -------------------------------------------------------
# ROUTES assignation automatique
# -------------------------------------------------------
@app.route("/admin/auto_assign_hebdo", methods=["POST"])
@login_required(role="admin")
def admin_auto_assign_hebdo():
    line = request.form.get("line","")
    created = _auto_assign_pmp(line, "hebdo", offset=0)
    flash(f"{created} tâches hebdo créées." if created else "Aucune tâche hebdo créée.", "ok" if created else "err")
    return redirect(url_for("admin_auto_page"))

@app.route("/admin/auto_assign_mensuel", methods=["POST"])
@login_required(role="admin")
def admin_auto_assign_mensuel():
    line = request.form.get("line","")
    created = _auto_assign_pmp(line, "mensu", offset=1)
    flash(f"{created} tâches mensuelles créées." if created else "Aucune tâche mensuelle créée.", "ok" if created else "err")
    return redirect(url_for("admin_auto_page"))

# -------------------------------------------------------
# PAGE : Ajout manuel tâche
# -------------------------------------------------------
@app.route("/admin/manual")
@login_required(role="admin")
def admin_manual_page():
    templates, lignes, machines_pl, intervenants, frequences = load_task_templates()

    db = get_db()
    users = db.execute("""
        SELECT id, username, role
        FROM users
        WHERE role!='admin'
        ORDER BY username
    """).fetchall()
    db.close()

    return render_template(
        "admin_manual_page.html",
        lignes=lignes,
        machines_par_ligne=machines_pl,
        intervenants=intervenants,
        frequences=frequences,
        users=users,
        current_year=datetime.now().year
    )
# -------------------------------------------------------
# CREATE tâche manuelle
# -------------------------------------------------------
@app.route("/admin/manual/create", methods=["POST"])
@login_required(role="admin")
def admin_manual_create():
    line = request.form["line"]
    machine = request.form["machine"]
    frequence = request.form["frequence"]
    intervenant = request.form["intervenant_type"]
    description = request.form["description"]
    assigned_to = int(request.form["assigned_to"])
    points = int(request.form["points"])

    # Ajouter dans Excel
    append_task_to_excel(line, machine, description, frequence, intervenant)

    # Ajouter dans DB
    db = get_db()
    db.execute("""
        INSERT INTO tasks(line, machine, description, assigned_to, status, points, frequency, created_at)
        VALUES (?, ?, ?, ?, 'en_cours', ?, ?, ?)
    """, (line, machine, description, assigned_to, points, frequence, datetime.now().isoformat()))
    db.commit()
    db.close()

    flash("Tâche manuelle créée et ajoutée au plan PMP.", "ok")
    return redirect(url_for("admin_manual_page"))
# -------------------------------------------------------
# PAGE : Tâches en cours (ADMIN)
# -------------------------------------------------------
@app.route("/admin/tasks/open")
@login_required(role="admin")
def admin_tasks_open():
    line       = (request.args.get("line") or "").strip()
    machine    = (request.args.get("machine") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date   = (request.args.get("end_date") or "").strip()

    db = get_db()
    where = "WHERE t.status='en_cours'"
    params = []

    if line:
        where += " AND t.line = ?"
        params.append(line)
    if machine:
        where += " AND t.machine = ?"
        params.append(machine)
    if start_date:
        where += " AND DATE(substr(t.created_at,1,10)) >= ?"
        params.append(start_date)
    if end_date:
        where += " AND DATE(substr(t.created_at,1,10)) <= ?"
        params.append(end_date)

    query = f"""
        SELECT t.*, u.username
        FROM tasks t
        JOIN users u ON u.id = t.assigned_to
        {where}
        ORDER BY t.created_at DESC
    """
    tasks = db.execute(query, params).fetchall()
    db.close()

    _, lignes, machines_par_ligne, _, _ = load_task_templates()

    return render_template(
        "admin_tasks_open.html",
        tasks=tasks,
        lignes=lignes,
        machines_par_ligne=machines_par_ligne,
        filters={
            "line": line,
            "machine": machine,
            "start_date": start_date,
            "end_date": end_date
        },
        current_year=datetime.now().year
    )
# -------------------------------------------------------
# PAGE : Tâches clôturées (ADMIN)
# -------------------------------------------------------
@app.route("/admin/tasks/closed")
@login_required(role="admin")
def admin_tasks_closed():
    line       = (request.args.get("line") or "").strip()
    machine    = (request.args.get("machine") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date   = (request.args.get("end_date") or "").strip()

    db = get_db()
    where = "WHERE t.status='cloturee'"
    params = []

    if line:
        where += " AND t.line = ?"
        params.append(line)
    if machine:
        where += " AND t.machine = ?"
        params.append(machine)
    if start_date:
        where += " AND DATE(substr(t.closed_at,1,10)) >= ?"
        params.append(start_date)
    if end_date:
        where += " AND DATE(substr(t.closed_at,1,10)) <= ?"
        params.append(end_date)

    query = f"""
        SELECT t.*, u.username
        FROM tasks t
        JOIN users u ON u.id = t.assigned_to
        {where}
        ORDER BY t.closed_at DESC
    """
    tasks = db.execute(query, params).fetchall()
    db.close()

    _, lignes, machines_par_ligne, _, _ = load_task_templates()

    return render_template(
        "admin_tasks_closed.html",
        tasks=tasks,
        lignes=lignes,
        machines_par_ligne=machines_par_ligne,
        filters={
            "line": line,
            "machine": machine,
            "start_date": start_date,
            "end_date": end_date
        },
        current_year=datetime.now().year
    )
# -------------------------------------------------------
# OPÉRATEUR : tableau de bord
# -------------------------------------------------------
@app.route("/me")
@login_required()
def operator_dashboard():
    user = current_user()
    db = get_db()

    tasks = db.execute("""
        SELECT *
        FROM tasks
        WHERE assigned_to=?
        ORDER BY CASE status WHEN 'en_cours' THEN 0 ELSE 1 END, created_at DESC
    """, (user["id"],)).fetchall()

    score = db.execute("""
        SELECT COALESCE(SUM(points),0)
        FROM tasks
        WHERE assigned_to=? AND status='cloturee'
    """, (user["id"],)).fetchone()[0]

    db.close()

    return render_template(
        "operator_dashboard.html",
        me=user,
        tasks=tasks,
        score_total=score
    )
# -------------------------------------------------------
# OPÉRATEUR : Clôturer une tâche
# -------------------------------------------------------
@app.route("/me/task/close/<int:task_id>", methods=["POST"])
@login_required()
def me_close_task(task_id):
    user = current_user()
    db = get_db()

    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    if not task or task["assigned_to"] != user["id"]:
        flash("Action interdite.", "err")
        db.close()
        return redirect(url_for("operator_dashboard"))

    db.execute("""
        UPDATE tasks
        SET status='cloturee', closed_at=?
        WHERE id=?
    """, (datetime.now().isoformat(), task_id))

    db.commit()
    db.close()

    flash("Tâche validée, bravo !", "ok")
    return redirect(url_for("operator_dashboard"))

# -------------------------------------------------------
# CONTEXT PROCESSOR (permet d'utiliser index partout)
# -------------------------------------------------------
@app.context_processor
def inject_routes():
    return dict(index=url_for("index"))

# -------------------------------------------------------
# MAIN : lancement app
# -------------------------------------------------------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
