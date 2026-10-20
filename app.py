# -*- coding: utf-8 -*-
"""
ASSETPILOT — Maintenance Autonome
Flask + SQLite. Configuration par variables d'environnement (voir README.md et .env.example).
Local : python app.py   ·   Production : gunicorn app:app
"""
import os
import json
import random
import sqlite3
import datetime as dt
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for, flash,
                   session, abort, g, jsonify, send_from_directory)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

import sap_integration as sap


def _load_dotenv(path):
    """Charge un fichier .env local s'il existe (les variables déjà définies sont prioritaires)."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.split(" #")[0].strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR", BASE))      # sur Render : le disque persistant
DB = os.path.join(DATA_DIR, "assetpilot.db")
UPLOADS = os.path.join(DATA_DIR, "uploads")
SCHEMA_VERSION = "6.1"
MIGRATABLE_VERSIONS = ("6.0",)  # mises à jour sans perte de données

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)   # derrière le proxy HTTPS de Render
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    import secrets as _secrets
    _secret = _secrets.token_hex(32)
    print("ATTENTION : SECRET_KEY non définie — clé temporaire (les sessions seront perdues au redémarrage).")
app.secret_key = _secret
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
app.config["PERMANENT_SESSION_LIFETIME"] = dt.timedelta(hours=12)

# Compte administrateur créé au premier démarrage (base vide) — jamais de mot de passe dans le code
ADMIN_USER = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_NAME = os.environ.get("ADMIN_NAME", "Administrateur")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")

# ---------------------------------------------------------------------------
# Référentiels
# ---------------------------------------------------------------------------
ROLES = [
    "Administrateur", "Opérateur", "Technicien",
    "Chef d'équipe Production", "Chef d'équipe Maintenance", "Leader SGA",
    "Plant Manager", "Groupe OE",
    "Responsable magasin", "Responsable maintenance", "Expert ligne",
]
FIELD_ROLES = ("Opérateur", "Technicien")
TEAM_ROLES = ("Chef d'équipe Production", "Chef d'équipe Maintenance", "Leader SGA")
PLANT_ROLES = ("Plant Manager", "Responsable magasin", "Responsable maintenance", "Expert ligne")
GLOBAL_ROLES = ("Administrateur", "Groupe OE")

TAG_TYPES = [
    {"key": "AM", "label": "AM", "color_name": "Bleu", "color": "#1687e8", "head": "#0d1f4f", "text": "#fff",
     "hint": "Anomalie traitable par l'opérateur"},
    {"key": "RCM", "label": "RCM", "color_name": "Rouge", "color": "#d71920", "head": "#d71920", "text": "#fff",
     "hint": "Nécessite l'intervention de la maintenance"},
    {"key": "SAFETY", "label": "Safety", "color_name": "Jaune", "color": "#f4c400", "head": "#f4c400", "text": "#1d1d1d",
     "hint": "Risque sécurité / HSE"},
]
TAG_TYPE = {t["key"]: t for t in TAG_TYPES}

STATUSES = ["Nouveau", "En cours", "Traité", "Contre-mesure"]
STATUS_INFO = {
    "Nouveau": {"color": "#d71920", "text": "#fff", "board": "Anomalies découvertes"},
    "En cours": {"color": "#f08a00", "text": "#fff", "board": "Anomalies en cours de traitement"},
    "Traité": {"color": "#58c48a", "text": "#0b3d22", "board": "Anomalies traitées"},
    "Contre-mesure": {"color": "#0b5e34", "text": "#fff", "board": "Contre-mesure (action préventive)"},
}

ANOMALY_TYPES = [
    "Défauts mineurs",
    "Absence des conditions de base",
    "Zones difficiles d'accès (HTR – Hard to Reach)",
    "Sources de contamination",
    "Défauts qualité",
    "Équipements / éléments inutiles",
    "Conditions dangereuses / situations à risque",
]
SOC_HTR_TYPES = ("Sources de contamination", "Zones difficiles d'accès (HTR – Hard to Reach)")

FREQUENCIES = ["Quotidienne", "Hebdomadaire", "Mensuelle"]
MACHINE_STATES = ["Machine à l'arrêt", "Machine en marche"]
DOC_TYPES = [("SOP", "SOP"), ("OPL", "OPL"), ("CHECK", "Checklist"), ("TECH", "Fiche technique")]
TASK_OPEN = ("À réaliser", "En cours")
TASK_DONE = ("Réalisée", "Confirmée")

PERMS = [
    ("tags_create", "Lancer un TAG"),
    ("tags_move", "Changer le statut des TAGs de son périmètre"),
    ("tags_all", "Agir sur tous les TAGs"),
    ("countermeasure", "Remplir le formulaire de contre-mesure"),
    ("tasks_exec", "Exécuter ses tâches / checklists"),
    ("confirm", "Confirmer les travaux de l'équipe"),
    ("checklists_manage", "Gérer les checklists préventives"),
    ("docs_manage", "Gérer la documentation"),
    ("sga_board_edit", "Modifier le tableau SGA"),
    ("dashboard_global", "Tableau de bord global (périmètre)"),
    ("audit_self", "Réaliser un self audit"),
    ("audit_pm", "Réaliser un audit Plant Manager"),
    ("audit_oe", "Réaliser un audit Groupe OE"),
    ("pdr_view", "Voir la gestion PDR"),
    ("pdr_request", "Créer une demande PDR"),
    ("pdr_validate", "Valider une demande PDR"),
    ("pdr_order", "Commander / réceptionner les PDR (magasin)"),
    ("settings", "Paramètres & utilisateurs"),
]
PERM_KEYS = [p[0] for p in PERMS]
ROLE_DEFAULTS = {
    "Administrateur": PERM_KEYS,
    "Opérateur": ["tags_create", "tags_move", "tasks_exec"],
    "Technicien": ["tags_create", "tags_move", "tasks_exec"],
    "Chef d'équipe Production": ["tags_create", "tags_move", "tasks_exec", "confirm"],
    "Chef d'équipe Maintenance": ["tags_create", "tags_move", "tasks_exec", "confirm"],
    "Leader SGA": ["tags_create", "tags_move", "tasks_exec", "audit_self", "sga_board_edit"],
    "Plant Manager": ["tags_create", "dashboard_global", "audit_pm"],
    "Groupe OE": ["tags_create", "dashboard_global", "audit_oe"],
    "Responsable magasin": ["tags_create", "pdr_view", "pdr_order"],
    "Responsable maintenance": ["tags_create", "tags_move", "dashboard_global", "pdr_view", "pdr_request", "pdr_validate"],
    "Expert ligne": ["tags_create", "tags_move", "pdr_view", "pdr_request"],
}

AUDIT_LEVELS = [
    {"key": "Self", "label": "Self audit", "perm": "audit_self", "color": "#1687e8",
     "who": "Chaque SGA s'auto-évalue", "scope": "Sa propre SGA"},
    {"key": "Plant Manager", "label": "Audit Plant Manager", "perm": "audit_pm", "color": "#f08a00",
     "who": "Le Plant Manager audite les SGA de son usine", "scope": "Toutes les SGA de l'usine"},
    {"key": "Groupe OE", "label": "Audit Groupe OE", "perm": "audit_oe", "color": "#d71920",
     "who": "L'équipe Groupe OE audite toutes les usines", "scope": "Global"},
]
AUDIT_THRESHOLD = 80
AUDIT_GRID = {
    1: [("Nettoyage initial réalisé sur tous les équipements de la SGA", None),
        ("Anomalies détectées et étiquetées (TAGs émis)", "tags_count"),
        ("Taux de traitement des étiquettes ≥ 80 %", "tag_rate"),
        ("Sources de contamination et zones difficiles d'accès identifiées", "soc_htr"),
        ("Standards provisoires de nettoyage et d'inspection rédigés", "sop_count"),
        ("Tableau SGA tenu à jour", "board_update"),
        ("Membres formés à la sécurité et aux règles d'étiquetage", None)],
    2: [("Sources de contamination éliminées ou maîtrisées", "soc_htr"),
        ("Zones difficiles d'accès améliorées", None),
        ("Temps de nettoyage / inspection réduit", "ci_time"),
        ("Contre-mesures en place pour les anomalies récurrentes", "cm_count"),
        ("Taux de traitement des étiquettes ≥ 80 %", "tag_rate"),
        ("Tableau SGA tenu à jour", "board_update"),
        ("Kaizens réalisés et documentés", None)],
    3: [("Standards CIL (nettoyage, inspection, lubrification) établis", "sop_count"),
        ("Checklists exécutées selon la fréquence prévue", "realization"),
        ("OPL créées et diffusées", "opl_count"),
        ("Points de lubrification identifiés et repérés", None),
        ("Transfert d'activités RCM → AM engagé", "transfer"),
        ("Respect des temps standards", "ci_time"),
        ("Tableau SGA tenu à jour", "board_update")],
}
AUDIT_GRID_DEFAULT = [
    ("Objectifs de l'étape atteints", None),
    ("Standards à jour et respectés", "sop_count"),
    ("Checklists exécutées selon la fréquence prévue", "realization"),
    ("Taux de traitement des étiquettes ≥ 80 %", "tag_rate"),
    ("Tableau SGA tenu à jour", "board_update"),
    ("Implication de toute l'équipe SGA", None),
]

PDR_STATUSES = ["Demandée", "Validée", "Commandée", "Réceptionnée", "Refusée"]
PDR_COLORS = {"Demandée": "#6b7480", "Validée": "#f08a00", "Commandée": "#1687e8",
              "Réceptionnée": "#17a45a", "Refusée": "#d71920"}

# ---------------------------------------------------------------------------
# Base de données
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS plants(
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
    city TEXT, status TEXT DEFAULT 'Actif');
CREATE TABLE IF NOT EXISTS lines(
    id INTEGER PRIMARY KEY AUTOINCREMENT, plant_id INTEGER NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
    code TEXT, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sga(
    id INTEGER PRIMARY KEY AUTOINCREMENT, line_id INTEGER NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
    name TEXT NOT NULL, description TEXT, am_step INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS equipment(
    id INTEGER PRIMARY KEY AUTOINCREMENT, line_id INTEGER NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
    sga_id INTEGER REFERENCES sga(id) ON DELETE SET NULL, reference TEXT, name TEXT NOT NULL,
    position INTEGER DEFAULT 0, description TEXT);
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, username TEXT UNIQUE NOT NULL,
    email TEXT, role TEXT NOT NULL, password_hash TEXT NOT NULL, plant_id INTEGER REFERENCES plants(id),
    permissions TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS user_sga(
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, sga_id INTEGER REFERENCES sga(id) ON DELETE CASCADE,
    PRIMARY KEY(user_id, sga_id));
CREATE TABLE IF NOT EXISTS tags(
    id INTEGER PRIMARY KEY AUTOINCREMENT, tag_code TEXT UNIQUE, type TEXT NOT NULL, anomaly_type TEXT,
    equipment_id INTEGER NOT NULL REFERENCES equipment(id), sga_id INTEGER, line_id INTEGER,
    organe TEXT, description TEXT NOT NULL, photo TEXT,
    observed_by INTEGER REFERENCES users(id), observed_at TEXT, created_at TEXT, ot_number TEXT,
    treated_by INTEGER REFERENCES users(id), treatment_start TEXT, resolved_at TEXT,
    status TEXT DEFAULT 'Nouveau', countermeasure TEXT,
    confirmed_by INTEGER, confirmed_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS tag_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT, tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
    user_id INTEGER, from_status TEXT, to_status TEXT, comment TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS checklists(
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, name TEXT NOT NULL,
    equipment_id INTEGER NOT NULL REFERENCES equipment(id) ON DELETE CASCADE, organe TEXT,
    frequency TEXT DEFAULT 'Quotidienne', duration_min INTEGER DEFAULT 10, machine_state TEXT,
    people INTEGER DEFAULT 1, sop_url TEXT, activity_type TEXT DEFAULT 'AM',
    assigned_to INTEGER REFERENCES users(id), origin_tag_id INTEGER REFERENCES tags(id),
    countermeasure TEXT, transferred INTEGER DEFAULT 0, transfer_date TEXT,
    active INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS checklist_items(
    id INTEGER PRIMARY KEY AUTOINCREMENT, checklist_id INTEGER REFERENCES checklists(id) ON DELETE CASCADE,
    position INTEGER, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT, checklist_id INTEGER REFERENCES checklists(id) ON DELETE CASCADE,
    assigned_to INTEGER REFERENCES users(id), due_date TEXT, status TEXT DEFAULT 'À réaliser',
    started_at TEXT, completed_at TEXT, duration_min INTEGER, confirmed_by INTEGER, confirmed_at TEXT,
    reject_comment TEXT);
CREATE TABLE IF NOT EXISTS task_items(
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    position INTEGER, text TEXT, ok INTEGER, validated_at TEXT, tag_id INTEGER);
CREATE TABLE IF NOT EXISTS documents(
    id INTEGER PRIMARY KEY AUTOINCREMENT, equipment_id INTEGER REFERENCES equipment(id) ON DELETE CASCADE,
    title TEXT NOT NULL, doc_type TEXT, version TEXT, owner TEXT, url TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS sga_board(
    sga_id INTEGER PRIMARY KEY REFERENCES sga(id) ON DELETE CASCADE,
    charte TEXT, objectifs TEXT, centrage TEXT, score_5s INTEGER, plan_5s TEXT, kaizen TEXT,
    mtbf TEXT, mttr TEXT, pertes_cadence TEXT, pannes TEXT, arrets_mineurs TEXT, preparatifs TEXT,
    updated_at TEXT, updated_by INTEGER);
CREATE TABLE IF NOT EXISTS audits(
    id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT, sga_id INTEGER REFERENCES sga(id) ON DELETE CASCADE,
    am_step INTEGER, auditor_id INTEGER, audit_date TEXT, score REAL, comment TEXT);
CREATE TABLE IF NOT EXISTS audit_items(
    id INTEGER PRIMARY KEY AUTOINCREMENT, audit_id INTEGER REFERENCES audits(id) ON DELETE CASCADE,
    criterion TEXT, note INTEGER, proof TEXT);
CREATE TABLE IF NOT EXISTS spare_parts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, reference TEXT UNIQUE, designation TEXT, stock REAL DEFAULT 0,
    min_stock REAL DEFAULT 0, unit TEXT DEFAULT 'pcs', location TEXT);
CREATE TABLE IF NOT EXISTS pdr_requests(
    id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, part_id INTEGER REFERENCES spare_parts(id),
    designation TEXT, qty REAL, equipment_id INTEGER, tag_id INTEGER, requester_id INTEGER,
    status TEXT DEFAULT 'Demandée', comment TEXT, created_at TEXT, validated_by INTEGER, validated_at TEXT,
    ordered_at TEXT, expected_date TEXT, received_at TEXT);
"""


def connect():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def q(sql, args=(), one=False):
    cur = db().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def ex(sql, args=()):
    cur = db().execute(sql, args)
    db().commit()
    return cur.lastrowid


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def today():
    return dt.date.today()


def fmt_date(value, with_time=False):
    if not value:
        return "—"
    try:
        if len(value) > 10:
            d = dt.datetime.strptime(value[:16], "%Y-%m-%d %H:%M")
            return d.strftime("%d/%m/%Y %H:%M") if with_time else d.strftime("%d/%m/%Y")
        return dt.datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return value


app.jinja_env.filters["d"] = fmt_date
app.jinja_env.filters["dt"] = lambda v: fmt_date(v, True)
app.jinja_env.filters["short"] = lambda n: (n or "").replace(" - CSD PET 4", "").replace("- CSD PET 4", "").strip()


# ---------------------------------------------------------------------------
# Initialisation de la base et données de départ
# ---------------------------------------------------------------------------
# Données de base réelles : usine Cobomi, ligne MOR_CA_CSD_PET4 et ses 26 équipements (références SAP).
# Désactivables avec la variable d'environnement SEED_BASE_DATA=0 (base totalement vide, à part l'administrateur).
BASE_PLANT = ("COB", "Cobomi", "Casablanca")
BASE_LINE = ("MOR_CA_CSD_PET4", "MOR_CA_CSD_PET4")
EQUIPMENT_COBOMI = [
    ("5001126", "ALIMENTATION DE PREFORMES"), ("5001127", "INSPECTRICE PREFORMES"),
    ("5001128", "RINCEUSE PREFORME"), ("5001129", "SOUFFLEUSE"),
    ("5001130", "INSPECTRICE BTLLES SOUFFLEES"), ("5001131", "SOUTIREUSE"),
    ("5001132", "VISSEUSE"), ("5001133", "MIXEUR"), ("5001134", "COBRIX"),
    ("5001135", "TRANSPORTEUR DE BOUCHONS"), ("5001136", "SECHEUR"),
    ("5001137", "INSPECTRICE NIVEAU"), ("5001138", "TRANSPORTEUR DE RECIPIENTS"),
    ("5001139", "ACCUMULATEUR"), ("5001140", "ETIQUETEUSE"), ("5001141", "INSPECTRICE ETIQUETTE"),
    ("5001142", "DATEUSE"), ("5001143", "FARDELEUSE"), ("5001144", "TRANSPORTEUR D'EMBALLAGES"),
    ("5001145", "ROBOT BOX"), ("5001146", "TRANSPORTEUR DE PALETTES"), ("5001147", "PALETTISEUR"),
    ("5001148", "STRECHEUSE"), ("5001149", "ETIQUETAGE PALETTE - CSD PET 4 LOGOPACK"),
    ("5001150", "IMPRIMANTE SICPA"), ("5001151", "STAT DE LUBRIF CONV A CHAINE"),
]


def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    if os.path.exists(DB):
        conn = connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        except sqlite3.Error:
            row = None
        if row and row[0] == SCHEMA_VERSION:
            sap.migrate(conn)
            conn.close()
            return
        if row and row[0] in MIGRATABLE_VERSIONS:
            sap.migrate(conn)
            conn.execute("UPDATE meta SET value=? WHERE key='schema'", (SCHEMA_VERSION,))
            conn.commit()
            conn.close()
            print("Base mise à jour en version", SCHEMA_VERSION, "(données conservées)")
            return
        conn.close()
        raise SystemExit("La base %s n'est pas une base AssetPilot %s. Elle n'a pas été modifiée : "
                         "déplacez-la ou indiquez un autre DATA_DIR." % (DB, SCHEMA_VERSION))
    conn = connect()
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema',?)", (SCHEMA_VERSION,))
    seed(conn)
    sap.migrate(conn)
    conn.commit()
    conn.close()


def seed(conn):
    """Première installation : compte administrateur + données de base (sans aucune donnée d'exemple)."""
    password = ADMIN_PASSWORD
    if not password:
        import secrets as _secrets
        password = _secrets.token_urlsafe(12)
        print("=" * 70)
        print(" Compte administrateur créé : %s" % ADMIN_USER)
        print(" Mot de passe provisoire    : %s" % password)
        print(" (définissez ADMIN_PASSWORD pour le choisir vous-même ; changez-le après connexion)")
        print("=" * 70)
    plant_id = None
    if os.environ.get("SEED_BASE_DATA", "1") != "0":
        cur = conn.execute("INSERT INTO plants(code,name,city) VALUES(?,?,?)", BASE_PLANT)
        plant_id = cur.lastrowid
        cur = conn.execute("INSERT INTO lines(plant_id,code,name) VALUES(?,?,?)", (plant_id,) + BASE_LINE)
        line_id = cur.lastrowid
        for pos, (ref, name) in enumerate(EQUIPMENT_COBOMI, 1):
            full = name if "CSD PET 4" in name else name + " - CSD PET 4"
            conn.execute("INSERT INTO equipment(line_id,sga_id,reference,name,position) VALUES(?,?,?,?,?)",
                         (line_id, None, ref, full, pos))
    conn.execute("INSERT INTO users(name,username,email,role,password_hash,plant_id,permissions,active) VALUES(?,?,?,?,?,?,?,1)",
                 (ADMIN_NAME, ADMIN_USER, ADMIN_EMAIL, "Administrateur", generate_password_hash(password), plant_id,
                  json.dumps(ROLE_DEFAULTS["Administrateur"])))


# ---------------------------------------------------------------------------
# Tâches planifiées (génération selon la fréquence)
# ---------------------------------------------------------------------------
def period_start(freq, d):
    if freq == "Hebdomadaire":
        return d - dt.timedelta(days=d.weekday())
    if freq == "Mensuelle":
        return d.replace(day=1)
    return d


def ensure_tasks(conn=None, checklist_id=None):
    """Crée la tâche de la période en cours pour chaque checklist active et affectée."""
    conn = conn or db()
    T = today()
    sql = "SELECT * FROM checklists WHERE active=1 AND assigned_to IS NOT NULL"
    args = ()
    if checklist_id:
        sql += " AND id=?"
        args = (checklist_id,)
    for cl in conn.execute(sql, args).fetchall():
        ps = period_start(cl["frequency"], T).isoformat()
        exists = conn.execute("SELECT id FROM tasks WHERE checklist_id=? AND due_date=?", (cl["id"], ps)).fetchone()
        if exists:
            continue
        conn.execute("UPDATE tasks SET status='Non réalisée' WHERE checklist_id=? AND due_date<? AND status IN ('À réaliser','En cours')",
                     (cl["id"], ps))
        cur = conn.execute("INSERT INTO tasks(checklist_id,assigned_to,due_date,status) VALUES(?,?,?, 'À réaliser')",
                           (cl["id"], cl["assigned_to"], ps))
        for it in conn.execute("SELECT * FROM checklist_items WHERE checklist_id=? ORDER BY position", (cl["id"],)):
            conn.execute("INSERT INTO task_items(task_id,position,text) VALUES(?,?,?)", (cur.lastrowid, it["position"], it["text"]))
    conn.commit()


_last_generation = {"day": None}


# ---------------------------------------------------------------------------
# Authentification, droits et périmètre
# ---------------------------------------------------------------------------
@app.before_request
def load_user():
    g.user = None
    g.perms = set()
    if request.endpoint in ("static", "healthz"):
        return
    if _last_generation["day"] != today():
        ensure_tasks()
        _last_generation["day"] = today()
    uid = session.get("uid")
    if uid:
        u = q("SELECT * FROM users WHERE id=? AND active=1", (uid,), one=True)
        if u:
            g.user = u
            g.perms = set(PERM_KEYS) if u["role"] == "Administrateur" else set(json.loads(u["permissions"] or "[]"))
    if g.user is None and request.endpoint not in ("login", "static", "service_worker", "healthz"):
        return redirect(url_for("login", next=request.path))


def can(perm):
    return g.user is not None and (g.user["role"] == "Administrateur" or perm in g.perms)


def is_admin():
    return g.user is not None and g.user["role"] == "Administrateur"


def require(perm):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if not can(perm):
                abort(403)
            return f(*a, **kw)
        return wrapper
    return deco


def scope_sga_ids(u=None):
    """None = toutes les SGA ; sinon liste d'identifiants."""
    u = u or g.user
    if u["role"] in GLOBAL_ROLES:
        return None
    if u["role"] in PLANT_ROLES:
        return [r["id"] for r in q("SELECT s.id FROM sga s JOIN lines l ON l.id=s.line_id WHERE l.plant_id=?", (u["plant_id"],))]
    return [r["sga_id"] for r in q("SELECT sga_id FROM user_sga WHERE user_id=?", (u["id"],))]


def in_clause(col, ids):
    if ids is None:
        return "1=1", []
    if not ids:
        return "1=0", []
    return "%s IN (%s)" % (col, ",".join("?" * len(ids))), list(ids)


def scoped_sgas():
    clause, args = in_clause("s.id", scope_sga_ids())
    return q("SELECT s.*, l.name line_name, l.plant_id, p.name plant_name FROM sga s JOIN lines l ON l.id=s.line_id "
             "JOIN plants p ON p.id=l.plant_id WHERE " + clause + " ORDER BY s.name", args)


def scoped_equipment():
    clause, args = in_clause("e.sga_id", scope_sga_ids())
    return q("SELECT e.*, s.name sga_name FROM equipment e LEFT JOIN sga s ON s.id=e.sga_id WHERE " + clause +
             " ORDER BY e.position, e.name", args)


def team_members(u=None, sga_id=None):
    """Membres suivis par un chef d'équipe / leader SGA."""
    u = u or g.user
    ids = scope_sga_ids(u)
    if sga_id:
        ids = [int(sga_id)] if ids is None or int(sga_id) in ids else []
    clause, args = in_clause("us.sga_id", ids)
    roles = FIELD_ROLES
    if u["role"] == "Chef d'équipe Production":
        roles = ("Opérateur",)
    elif u["role"] == "Chef d'équipe Maintenance":
        roles = ("Technicien",)
    return q("SELECT DISTINCT us_u.* FROM users us_u JOIN user_sga us ON us.user_id=us_u.id WHERE us_u.active=1 AND " + clause +
             " AND us_u.role IN (%s) ORDER BY us_u.role, us_u.name" % ",".join("?" * len(roles)), args + list(roles))


def can_act_on_tag(tag):
    if can("tags_all"):
        return True
    if not can("tags_move"):
        return False
    ids = scope_sga_ids()
    return ids is None or tag["sga_id"] in ids


def user_sga_names(user_id):
    return ", ".join(r["name"] for r in q("SELECT s.name FROM sga s JOIN user_sga us ON us.sga_id=s.id WHERE us.user_id=? ORDER BY s.name", (user_id,)))


@app.context_processor
def inject():
    ctx = dict(can=can, is_admin=is_admin, me=g.get("user"), TAG_TYPES=TAG_TYPES, TAG_TYPE=TAG_TYPE, STATUSES=STATUSES,
               STATUS_INFO=STATUS_INFO, ANOMALY_TYPES=ANOMALY_TYPES, FREQUENCIES=FREQUENCIES, MACHINE_STATES=MACHINE_STATES,
               DOC_TYPES=DOC_TYPES, ROLES=ROLES, PERMS=PERMS, PDR_COLORS=PDR_COLORS, today_str=today().strftime("%d/%m/%Y"))
    if g.get("user"):
        ctx["me_sgas"] = user_sga_names(g.user["id"])
        ctx["nav_tasks"] = q("SELECT COUNT(*) n FROM tasks WHERE assigned_to=? AND status IN ('À réaliser','En cours')",
                             (g.user["id"],), one=True)["n"]
    return ctx


@app.errorhandler(403)
def forbidden(e):
    return render_template("message.html", title="Accès refusé",
                           message="Votre profil n'a pas accès à cette page. Contactez l'administrateur pour obtenir le droit."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("message.html", title="Page introuvable", message="L'élément demandé n'existe pas."), 404


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q("SELECT * FROM users WHERE username=? AND active=1", (request.form.get("username", "").strip(),), one=True)
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session["uid"] = u["id"]
            nxt = request.args.get("next") or url_for("home")
            return redirect(nxt if nxt.startswith("/") else url_for("home"))
        flash("Identifiant ou mot de passe incorrect.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Indicateurs
# ---------------------------------------------------------------------------
def pct(a, b):
    return round(100 * a / b) if b else 0


def week_bounds(d=None):
    d = d or today()
    start = d - dt.timedelta(days=d.weekday())
    return start, start + dt.timedelta(days=6)


def period_since(key):
    T = today()
    return {"semaine": week_bounds()[0], "mois": T - dt.timedelta(days=30), "trimestre": T - dt.timedelta(days=90)}.get(key)


def user_kpis(user_id, since=None):
    since = since or week_bounds()[0]
    rows = q("SELECT status FROM tasks WHERE assigned_to=? AND due_date>=? AND due_date<=?", (user_id, since.isoformat(), today().isoformat()))
    planned = len(rows)
    done = sum(1 for r in rows if r["status"] in TASK_DONE)
    open_tags = q("SELECT COUNT(*) n FROM tags WHERE (treated_by=? OR (observed_by=? AND treated_by IS NULL)) AND status IN ('Nouveau','En cours')",
                  (user_id, user_id), one=True)["n"]
    emitted = q("SELECT COUNT(*) n FROM tags WHERE observed_by=? AND created_at>=?", (user_id, since.isoformat()), one=True)["n"]
    s5 = q("SELECT b.score_5s FROM sga_board b JOIN user_sga us ON us.sga_id=b.sga_id WHERE us.user_id=? LIMIT 1", (user_id,), one=True)
    return {"planned": planned, "done": done, "rate": pct(done, planned), "open_tags": open_tags, "emitted": emitted,
            "score_5s": s5["score_5s"] if s5 and s5["score_5s"] is not None else None}


def dashboard_data(f):
    """f : dict de filtres (plant, line, sga, chef, user, period)."""
    T = today()
    since = period_since(f.get("period") or "mois")
    clauses_t, args_t = [], []
    clauses_k, args_k = [], []
    sc, sa = in_clause("t.sga_id", scope_sga_ids())
    clauses_t.append(sc); args_t += sa
    sc, sa = in_clause("e.sga_id", scope_sga_ids())
    clauses_k.append(sc); args_k += sa
    for col, key in (("l.plant_id", "plant"), ("e.line_id", "line"), ("e.sga_id", "sga")):
        if f.get(key):
            clauses_t.append(col + "=?"); args_t.append(f[key])
            clauses_k.append(col + "=?"); args_k.append(f[key])
    users = None
    if f.get("user"):
        users = [int(f["user"])]
    elif f.get("chef"):
        chef = q("SELECT * FROM users WHERE id=?", (f["chef"],), one=True)
        users = [m["id"] for m in team_members(chef)] if chef else []
    if users is not None:
        c1, a1 = in_clause("t.observed_by", users)
        c2, a2 = in_clause("t.treated_by", users)
        clauses_t.append("(%s OR %s)" % (c1, c2)); args_t += a1 + a2
        c3, a3 = in_clause("k.assigned_to", users)
        clauses_k.append(c3); args_k += a3
    base_t = " FROM tags t JOIN equipment e ON e.id=t.equipment_id JOIN lines l ON l.id=e.line_id WHERE " + " AND ".join(clauses_t)
    base_k = (" FROM tasks k JOIN checklists c ON c.id=k.checklist_id JOIN equipment e ON e.id=c.equipment_id "
              "JOIN lines l ON l.id=e.line_id WHERE " + " AND ".join(clauses_k))
    per_t, per_ta = "", []
    per_k, per_ka = " AND k.due_date<=?", [T.isoformat()]
    if since:
        per_t, per_ta = " AND t.created_at>=?", [since.isoformat()]
        per_k += " AND k.due_date>=?"; per_ka.append(since.isoformat())
    tags = q("SELECT t.*, e.name eq_name" + base_t + per_t, args_t + per_ta)
    tasks = q("SELECT k.status, k.duration_min, c.duration_min std" + base_k + per_k, args_k + per_ka)
    total = len(tags)
    treated = sum(1 for t in tags if t["status"] in ("Traité", "Contre-mesure"))
    by_type = [{"t": tt, "n": sum(1 for t in tags if t["type"] == tt["key"]),
                "treated": sum(1 for t in tags if t["type"] == tt["key"] and t["status"] in ("Traité", "Contre-mesure"))} for tt in TAG_TYPES]
    by_status = [{"s": s, "n": sum(1 for t in tags if t["status"] == s)} for s in STATUSES]
    by_anomaly = sorted([{"a": a, "n": sum(1 for t in tags if t["anomaly_type"] == a)} for a in ANOMALY_TYPES], key=lambda x: -x["n"])
    planned = len(tasks)
    done = sum(1 for k in tasks if k["status"] in TASK_DONE)
    to_confirm = sum(1 for k in tasks if k["status"] == "Réalisée") + sum(
        1 for t in tags if t["status"] in ("Traité", "Contre-mesure") and not t["confirmed_at"])
    # Transfert RCM -> AM
    tr_clause, tr_args = in_clause("e.sga_id", scope_sga_ids())
    extra, extra_a = "", []
    for col, key in (("l.plant_id", "plant"), ("e.line_id", "line"), ("e.sga_id", "sga")):
        if f.get(key):
            extra += " AND %s=?" % col; extra_a.append(f[key])
    tr = q("SELECT SUM(c.transferred) tr, SUM(CASE WHEN c.activity_type='RCM' THEN 1 ELSE 0 END) rcm FROM checklists c "
           "JOIN equipment e ON e.id=c.equipment_id JOIN lines l ON l.id=e.line_id WHERE c.active=1 AND " + tr_clause + extra,
           tr_args + extra_a, one=True)
    transfer = pct(tr["tr"] or 0, (tr["tr"] or 0) + (tr["rcm"] or 0))
    # Audits
    au_clause, au_args = in_clause("a.sga_id", scope_sga_ids())
    au = q("SELECT AVG(a.score) s FROM audits a JOIN sga s ON s.id=a.sga_id JOIN lines l ON l.id=s.line_id WHERE " + au_clause +
           (" AND l.plant_id=?" if f.get("plant") else "") + (" AND s.line_id=?" if f.get("line") else "") +
           (" AND a.sga_id=?" if f.get("sga") else ""),
           au_args + [x for x in (f.get("plant"), f.get("line"), f.get("sga")) if x], one=True)
    # SGA ranking
    sga_rows = []
    for s in scoped_sgas():
        if f.get("sga") and str(s["id"]) != str(f["sga"]):
            continue
        if f.get("line") and str(s["line_id"]) != str(f["line"]):
            continue
        if f.get("plant") and str(s["plant_id"]) != str(f["plant"]):
            continue
        st = [t for t in tags if t["sga_id"] == s["id"]]
        tk = q("SELECT k.status FROM tasks k JOIN checklists c ON c.id=k.checklist_id JOIN equipment e ON e.id=c.equipment_id "
               "WHERE e.sga_id=? AND k.due_date<=?" + (" AND k.due_date>=?" if since else ""),
               [s["id"], T.isoformat()] + ([since.isoformat()] if since else []))
        last_audit = q("SELECT score, level FROM audits WHERE sga_id=? ORDER BY audit_date DESC, id DESC LIMIT 1", (s["id"],), one=True)
        sga_rows.append({"s": s, "open": sum(1 for t in st if t["status"] in ("Nouveau", "En cours")), "total": len(st),
                         "rate": pct(sum(1 for k in tk if k["status"] in TASK_DONE), len(tk)),
                         "audit": last_audit})
    # Tendance 8 semaines
    trend = []
    ws, _ = week_bounds()
    for i in range(7, -1, -1):
        a = ws - dt.timedelta(weeks=i)
        b = a + dt.timedelta(days=7)
        created = q("SELECT COUNT(*) n" + base_t + " AND t.created_at>=? AND t.created_at<?", args_t + [a.isoformat(), b.isoformat()], one=True)["n"]
        resolved = q("SELECT COUNT(*) n" + base_t + " AND t.resolved_at>=? AND t.resolved_at<?", args_t + [a.isoformat(), b.isoformat()], one=True)["n"]
        trend.append({"label": a.strftime("%d/%m"), "created": created, "resolved": resolved})
    return {"total": total, "treated": treated, "treat_rate": pct(treated, total), "by_type": by_type, "by_status": by_status,
            "by_anomaly": by_anomaly, "planned": planned, "done": done, "real_rate": pct(done, planned),
            "to_confirm": to_confirm, "transfer": transfer, "audit_avg": round(au["s"]) if au and au["s"] else None,
            "sga_rows": sga_rows, "trend": trend, "trend_max": max([1] + [max(x["created"], x["resolved"]) for x in trend]),
            "open": sum(1 for t in tags if t["status"] in ("Nouveau", "En cours"))}


# ---------------------------------------------------------------------------
# Accueil
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    u = g.user
    if u["role"] in FIELD_ROLES:
        return home_field()
    if u["role"] in TEAM_ROLES:
        return home_team()
    return dashboard(home=True)


def task_rows(where, args):
    return q("""SELECT k.*, c.name cl_name, c.organe, c.frequency, c.duration_min std, e.name eq_name, u.name user_name,
               (SELECT COUNT(*) FROM task_items i WHERE i.task_id=k.id) n_items,
               (SELECT COUNT(*) FROM task_items i WHERE i.task_id=k.id AND i.ok IS NOT NULL) n_done
               FROM tasks k JOIN checklists c ON c.id=k.checklist_id JOIN equipment e ON e.id=c.equipment_id
               LEFT JOIN users u ON u.id=k.assigned_to WHERE """ + where, args)


def home_field():
    u = g.user
    k = user_kpis(u["id"])
    tasks = task_rows("k.assigned_to=? AND (k.status IN ('À réaliser','En cours') OR k.due_date>=?) ORDER BY "
                      "CASE k.status WHEN 'En cours' THEN 0 WHEN 'À réaliser' THEN 1 ELSE 2 END, k.due_date DESC LIMIT 10",
                      (u["id"], week_bounds()[0].isoformat()))
    clause, args = in_clause("t.sga_id", scope_sga_ids())
    tags = q("SELECT t.*, e.name eq_name FROM tags t JOIN equipment e ON e.id=t.equipment_id WHERE " + clause +
             " AND (t.observed_by=? OR t.treated_by=? OR t.status IN ('Nouveau','En cours')) ORDER BY t.created_at DESC LIMIT 6",
             args + [u["id"], u["id"]])
    docs = q("SELECT d.*, e.name eq_name FROM documents d JOIN equipment e ON e.id=d.equipment_id WHERE " +
             in_clause("e.sga_id", scope_sga_ids())[0] + " ORDER BY d.doc_type, d.title LIMIT 6", in_clause("e.sga_id", scope_sga_ids())[1])
    return render_template("home_field.html", k=k, tasks=tasks, tags=tags, docs=docs)


def home_team():
    u = g.user
    sga_id = request.args.get("sga", "")
    period = request.args.get("period", "semaine")
    member_id = request.args.get("member", "")
    since = period_since(period)
    members = team_members(u, sga_id or None)
    member_ids = [m["id"] for m in members]
    shown = [m for m in members if not member_id or str(m["id"]) == member_id]
    perf = []
    for m in shown:
        k = user_kpis(m["id"], since)
        perf.append({"m": m, "k": k, "sga": user_sga_names(m["id"])})
    ids = [int(sga_id)] if sga_id else scope_sga_ids()
    clause, args = in_clause("t.sga_id", ids)
    tags = q("SELECT t.status, t.confirmed_at FROM tags t WHERE " + clause + " AND t.created_at>=?", args + [since.isoformat()])
    to_confirm_tasks, to_confirm_tags = [], []
    if can("confirm") and member_ids:
        mc, ma = in_clause("k.assigned_to", member_ids)
        to_confirm_tasks = task_rows(mc + " AND k.status='Réalisée' ORDER BY k.completed_at DESC", ma)
        tc, ta = in_clause("t.treated_by", member_ids)
        to_confirm_tags = q("SELECT t.*, e.name eq_name, u.name user_name FROM tags t JOIN equipment e ON e.id=t.equipment_id "
                            "LEFT JOIN users u ON u.id=t.treated_by WHERE " + tc +
                            " AND t.status IN ('Traité','Contre-mesure') AND t.confirmed_at IS NULL ORDER BY t.resolved_at DESC", ta)
    planned = sum(p["k"]["planned"] for p in perf)
    done = sum(p["k"]["done"] for p in perf)
    kpi = {"tags": len(tags), "open": sum(1 for t in tags if t["status"] == "En cours"),
           "new": sum(1 for t in tags if t["status"] == "Nouveau"),
           "to_confirm": len(to_confirm_tasks) + len(to_confirm_tags),
           "treated": sum(1 for t in tags if t["status"] in ("Traité", "Contre-mesure")), "rate": pct(done, planned)}
    my_tasks = task_rows("k.assigned_to=? AND k.status IN ('À réaliser','En cours') ORDER BY k.due_date", (u["id"],))
    to_validate = SAP["pending_validation"](u) if can("confirm") else []
    return render_template("home_team.html", to_validate=to_validate, sap_linked=SAP["has_token"](u["id"]), perf=perf, kpi=kpi, members=members, sgas=scoped_sgas(), sel_sga=sga_id,
                           sel_member=member_id, period=period, to_confirm_tasks=to_confirm_tasks,
                           to_confirm_tags=to_confirm_tags, my_tasks=my_tasks)


@app.route("/dashboard")
def dashboard(home=False):
    if not home and not (can("dashboard_global") or g.user["role"] in TEAM_ROLES):
        abort(403)
    f = {k: request.args.get(k, "") for k in ("plant", "line", "sga", "chef", "user", "period")}
    f["period"] = f["period"] or "mois"
    data = dashboard_data(f)
    sc, sa = in_clause("us.sga_id", scope_sga_ids())
    users = q("SELECT DISTINCT u.* FROM users u JOIN user_sga us ON us.user_id=u.id WHERE u.active=1 AND " + sc +
              " AND u.role IN ('Opérateur','Technicien') ORDER BY u.name", sa)
    chefs = q("SELECT * FROM users WHERE active=1 AND role IN (?,?,?) ORDER BY name", TEAM_ROLES)
    plants = q("SELECT * FROM plants ORDER BY name")
    lines = q("SELECT * FROM lines ORDER BY name")
    return render_template("dashboard.html", d=data, f=f, sgas=scoped_sgas(), users=users, chefs=chefs, plants=plants,
                           lines=lines, is_home=home)


# ---------------------------------------------------------------------------
# Tâches (exécution des checklists)
# ---------------------------------------------------------------------------
@app.route("/tasks")
def my_tasks():
    u = g.user
    open_tasks = task_rows("k.assigned_to=? AND k.status IN ('À réaliser','En cours') ORDER BY k.due_date", (u["id"],))
    history = task_rows("k.assigned_to=? AND k.status NOT IN ('À réaliser','En cours') ORDER BY k.due_date DESC, k.id DESC LIMIT 40", (u["id"],))
    return render_template("tasks.html", open_tasks=open_tasks, history=history, k=user_kpis(u["id"]))


def load_task(task_id):
    t = q("""SELECT k.*, c.name cl_name, c.organe, c.frequency, c.duration_min std, c.sop_url, c.machine_state, c.people,
             c.equipment_id, c.code cl_code, c.countermeasure, e.name eq_name, e.sga_id, u.name user_name
             FROM tasks k JOIN checklists c ON c.id=k.checklist_id JOIN equipment e ON e.id=c.equipment_id
             LEFT JOIN users u ON u.id=k.assigned_to WHERE k.id=?""", (task_id,), one=True)
    if not t:
        abort(404)
    return t


def can_view_task(t):
    if t["assigned_to"] == g.user["id"] or is_admin() or can("checklists_manage"):
        return True
    ids = scope_sga_ids()
    return ids is None or t["sga_id"] in ids


@app.route("/tasks/<int:task_id>")
def task_run(task_id):
    t = load_task(task_id)
    if not can_view_task(t):
        abort(403)
    items = q("SELECT i.*, tg.tag_code FROM task_items i LEFT JOIN tags tg ON tg.id=i.tag_id WHERE i.task_id=? ORDER BY i.position", (task_id,))
    editable = t["assigned_to"] == g.user["id"] and t["status"] in TASK_OPEN and can("tasks_exec")
    docs = q("SELECT * FROM documents WHERE equipment_id=? ORDER BY doc_type", (t["equipment_id"],))
    return render_template("task_run.html", t=t, items=items, editable=editable, docs=docs,
                           all_done=all(i["ok"] is not None for i in items))


@app.route("/tasks/<int:task_id>/item/<int:item_id>", methods=["POST"])
def task_item(task_id, item_id):
    t = load_task(task_id)
    if t["assigned_to"] != g.user["id"] or t["status"] not in TASK_OPEN:
        abort(403)
    ex("UPDATE task_items SET ok=1, validated_at=? WHERE id=? AND task_id=?", (now(), item_id, task_id))
    if t["status"] == "À réaliser":
        ex("UPDATE tasks SET status='En cours', started_at=? WHERE id=?", (now(), task_id))
    return redirect(url_for("task_run", task_id=task_id))


@app.route("/tasks/<int:task_id>/item/<int:item_id>/reset", methods=["POST"])
def task_item_reset(task_id, item_id):
    t = load_task(task_id)
    if t["assigned_to"] != g.user["id"] or t["status"] not in TASK_OPEN:
        abort(403)
    ex("UPDATE task_items SET ok=NULL, validated_at=NULL WHERE id=? AND task_id=? AND tag_id IS NULL", (item_id, task_id))
    return redirect(url_for("task_run", task_id=task_id))


@app.route("/tasks/<int:task_id>/finish", methods=["POST"])
def task_finish(task_id):
    t = load_task(task_id)
    if t["assigned_to"] != g.user["id"] or t["status"] not in TASK_OPEN:
        abort(403)
    pending = q("SELECT COUNT(*) n FROM task_items WHERE task_id=? AND ok IS NULL", (task_id,), one=True)["n"]
    if pending:
        flash("Tous les points doivent être validés avant de terminer la tâche.", "error")
        return redirect(url_for("task_run", task_id=task_id))
    started = t["started_at"] or now()
    try:
        dur = int((dt.datetime.now() - dt.datetime.strptime(started, "%Y-%m-%d %H:%M")).total_seconds() // 60)
    except ValueError:
        dur = None
    ex("UPDATE tasks SET status='Réalisée', completed_at=?, started_at=?, duration_min=?, reject_comment=NULL WHERE id=?",
       (now(), started, max(dur or 1, 1), task_id))
    flash("Tâche terminée : elle est envoyée au chef d'équipe pour confirmation.", "success")
    return redirect(url_for("home"))


@app.route("/confirm/task/<int:task_id>", methods=["POST"])
@require("confirm")
def confirm_task(task_id):
    t = load_task(task_id)
    if request.form.get("action") == "reject":
        comment = request.form.get("comment", "").strip()
        if not comment:
            flash("Un commentaire est obligatoire pour rejeter.", "error")
            return redirect(request.referrer or url_for("home"))
        ex("UPDATE tasks SET status='À réaliser', reject_comment=?, completed_at=NULL WHERE id=?", (comment, task_id))
        ex("UPDATE task_items SET ok=NULL, validated_at=NULL WHERE task_id=? AND tag_id IS NULL", (task_id,))
        flash("Tâche rejetée : elle revient chez %s." % t["user_name"], "success")
    else:
        ex("UPDATE tasks SET status='Confirmée', confirmed_by=?, confirmed_at=? WHERE id=?", (g.user["id"], now(), task_id))
        flash("Tâche confirmée.", "success")
    return redirect(request.referrer or url_for("home"))


@app.route("/confirm/tag/<int:tag_id>", methods=["POST"])
@require("confirm")
def confirm_tag(tag_id):
    tag = load_tag(tag_id)
    if request.form.get("action") == "reject":
        comment = request.form.get("comment", "").strip() or "Traitement non conforme"
        ex("UPDATE tags SET status='En cours', resolved_at=NULL, updated_at=? WHERE id=?", (now(), tag_id))
        ex("INSERT INTO tag_history(tag_id,user_id,from_status,to_status,comment,at) VALUES(?,?,?,?,?,?)",
           (tag_id, g.user["id"], tag["status"], "En cours", "Rejeté : " + comment, now()))
        flash("TAG renvoyé en cours de traitement.", "success")
    else:
        ex("UPDATE tags SET confirmed_by=?, confirmed_at=? WHERE id=?", (g.user["id"], now(), tag_id))
        ex("INSERT INTO tag_history(tag_id,user_id,from_status,to_status,comment,at) VALUES(?,?,?,?,?,?)",
           (tag_id, g.user["id"], tag["status"], tag["status"], "Traitement confirmé", now()))
        flash("Traitement du TAG confirmé.", "success")
    return redirect(request.referrer or url_for("home"))


# ---------------------------------------------------------------------------
# TAGs
# ---------------------------------------------------------------------------
def load_tag(tag_id):
    t = q("""SELECT t.*, e.name eq_name, e.reference eq_ref, s.name sga_name, l.name line_name, p.name plant_name,
             ob.name observed_name, tr.name treated_name, cf.name confirmed_name
             FROM tags t JOIN equipment e ON e.id=t.equipment_id LEFT JOIN sga s ON s.id=t.sga_id
             JOIN lines l ON l.id=e.line_id JOIN plants p ON p.id=l.plant_id
             LEFT JOIN users ob ON ob.id=t.observed_by LEFT JOIN users tr ON tr.id=t.treated_by
             LEFT JOIN users cf ON cf.id=t.confirmed_by WHERE t.id=?""", (tag_id,), one=True)
    if not t:
        abort(404)
    return t


def tag_query(extra="", args=()):
    clause, sargs = in_clause("t.sga_id", scope_sga_ids())
    return q("""SELECT t.*, e.name eq_name, s.name sga_name, ob.name observed_name, tr.name treated_name
               FROM tags t JOIN equipment e ON e.id=t.equipment_id LEFT JOIN sga s ON s.id=t.sga_id
               JOIN lines l ON l.id=e.line_id
               LEFT JOIN users ob ON ob.id=t.observed_by LEFT JOIN users tr ON tr.id=t.treated_by
               WHERE """ + clause + extra, sargs + list(args))


def tag_filters():
    f = {k: request.args.get(k, "") for k in ("plant", "line", "sga", "equipment", "type", "status", "anomaly", "search")}
    extra, args = "", []
    for col, key in (("l.plant_id", "plant"), ("e.line_id", "line"), ("t.sga_id", "sga"), ("t.equipment_id", "equipment"),
                     ("t.type", "type"), ("t.status", "status"), ("t.anomaly_type", "anomaly")):
        if f[key]:
            extra += " AND %s=?" % col
            args.append(f[key])
    if f["search"]:
        extra += " AND (t.tag_code LIKE ? OR t.description LIKE ? OR t.organe LIKE ? OR e.name LIKE ?)"
        args += ["%" + f["search"] + "%"] * 4
    return f, extra, args


def assignable_users(sga_id=None):
    clause, args = in_clause("us.sga_id", [sga_id] if sga_id else scope_sga_ids())
    rows = q("SELECT DISTINCT u.* FROM users u JOIN user_sga us ON us.user_id=u.id WHERE u.active=1 AND " + clause +
             " ORDER BY CASE u.role WHEN 'Opérateur' THEN 0 WHEN 'Technicien' THEN 1 ELSE 2 END, u.name", args)
    if not rows:
        rows = q("SELECT * FROM users WHERE active=1 AND role IN ('Opérateur','Technicien') ORDER BY name")
    return rows


@app.route("/tags")
def tags_board():
    f, extra, args = tag_filters()
    rows = tag_query(extra + " ORDER BY t.created_at DESC", args)
    board = {tt["key"]: {s: [] for s in STATUSES} for tt in TAG_TYPES}
    for t in rows:
        if t["type"] in board and t["status"] in STATUSES:
            board[t["type"]][t["status"]].append(t)
    sel_sga = q("SELECT s.*, l.name line_name, p.name plant_name FROM sga s JOIN lines l ON l.id=s.line_id JOIN plants p ON p.id=l.plant_id WHERE s.id=?",
                (f["sga"],), one=True) if f["sga"] else None
    return render_template("tags_board.html", board=board, f=f, sgas=scoped_sgas(), equipment=scoped_equipment(),
                           sel_sga=sel_sga, plants=q("SELECT * FROM plants"), lines=q("SELECT * FROM lines"),
                           users=assignable_users(), count=len(rows))


@app.route("/tags/registre")
def tags_list():
    f, extra, args = tag_filters()
    rows = tag_query(extra + " ORDER BY t.created_at DESC", args)
    return render_template("tags_list.html", rows=rows, f=f, sgas=scoped_sgas(), equipment=scoped_equipment(),
                           plants=q("SELECT * FROM plants"), lines=q("SELECT * FROM lines"), users=assignable_users())


@app.route("/tags/new", methods=["GET", "POST"])
@require("tags_create")
def tag_new():
    equipment = scoped_equipment() if not is_admin() else q(
        "SELECT e.*, s.name sga_name FROM equipment e LEFT JOIN sga s ON s.id=e.sga_id ORDER BY e.position, e.name")
    if request.method == "POST":
        form = request.form
        e = q("SELECT * FROM equipment WHERE id=?", (form.get("equipment_id") or 0,), one=True)
        errors = []
        if form.get("type") not in TAG_TYPE:
            errors.append("Choisissez le type d'étiquette (AM, RCM ou Safety).")
        if form.get("anomaly_type") not in ANOMALY_TYPES:
            errors.append("Choisissez le type d'anomalie.")
        if not e or e["id"] not in [x["id"] for x in equipment]:
            errors.append("Choisissez la machine.")
        if not form.get("description", "").strip():
            errors.append("Décrivez l'anomalie.")
        if errors:
            for er in errors:
                flash(er, "error")
            return render_template("tag_form.html", equipment=equipment, v=form)
        photo = ""
        file = request.files.get("photo")
        if file and file.filename:
            ext = os.path.splitext(file.filename)[1].lower()
            if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"):
                photo = secure_filename("tag_%s_%s%s" % (dt.datetime.now().strftime("%Y%m%d%H%M%S"), g.user["id"], ext))
                file.save(os.path.join(UPLOADS, photo))
        n = q("SELECT COALESCE(MAX(id),0)+1 n FROM tags", one=True)["n"]
        code = "TAG-%04d" % n
        while q("SELECT 1 FROM tags WHERE tag_code=?", (code,), one=True):
            n += 1
            code = "TAG-%04d" % n
        tid = ex("""INSERT INTO tags(tag_code,type,anomaly_type,equipment_id,sga_id,line_id,organe,description,photo,
                 observed_by,observed_at,created_at,ot_number,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, 'Nouveau', ?)""",
                 (code, form["type"], form["anomaly_type"], e["id"], e["sga_id"], e["line_id"], form.get("organe", "").strip(),
                  form["description"].strip(), photo, g.user["id"], form.get("observed_at") or today().isoformat(), now(),
                  form.get("ot_number", "").strip(), now()))
        ex("INSERT INTO tag_history(tag_id,user_id,from_status,to_status,comment,at) VALUES(?,?,?,?,?,?)",
           (tid, g.user["id"], None, "Nouveau", "TAG créé", now()))
        item_id = form.get("task_item_id")
        if item_id:
            it = q("SELECT i.*, k.assigned_to, k.id task_id FROM task_items i JOIN tasks k ON k.id=i.task_id WHERE i.id=?", (item_id,), one=True)
            if it and it["assigned_to"] == g.user["id"]:
                ex("UPDATE task_items SET ok=0, validated_at=?, tag_id=? WHERE id=?", (now(), tid, item_id))
                ex("UPDATE tasks SET status='En cours', started_at=COALESCE(started_at, ?) WHERE id=? AND status='À réaliser'", (now(), it["task_id"]))
                flash("%s créé et rattaché au point non conforme." % code, "success")
                return redirect(url_for("task_run", task_id=it["task_id"]))
        flash("%s créé : il apparaît dans « Anomalies découvertes »." % code, "success")
        return redirect(url_for("tag_detail", tag_id=tid))
    v = {k: request.args.get(k, "") for k in ("type", "equipment_id", "organe", "description", "task_item_id")}
    return render_template("tag_form.html", equipment=equipment, v=v)


@app.route("/tags/<int:tag_id>")
def tag_detail(tag_id):
    t = load_tag(tag_id)
    ids = scope_sga_ids()
    if not (is_admin() or can("tags_all") or ids is None or t["sga_id"] in ids or t["observed_by"] == g.user["id"]):
        abort(403)
    history = q("SELECT h.*, u.name user_name FROM tag_history h LEFT JOIN users u ON u.id=h.user_id WHERE h.tag_id=? ORDER BY h.at, h.id", (tag_id,))
    cm = q("SELECT c.*, u.name user_name FROM checklists c LEFT JOIN users u ON u.id=c.assigned_to WHERE c.origin_tag_id=?", (tag_id,))
    pdr = q("SELECT * FROM pdr_requests WHERE tag_id=? ORDER BY id DESC", (tag_id,))
    idx = STATUSES.index(t["status"]) if t["status"] in STATUSES else 0
    targets = []
    if can_act_on_tag(t):
        if is_admin():
            targets = [s for s in STATUSES if s != t["status"]]
        elif idx + 1 < len(STATUSES):
            targets = [STATUSES[idx + 1]]
        if "Contre-mesure" in targets and not can("countermeasure"):
            targets.remove("Contre-mesure")
    fiori_open = ""
    if request.args.get("fiori") and sap.mode() == "fiori" and t["type"] in sap.SAP_TYPES:
        fiori_open = SAP["fiori_create_url"](t, g.user)
    sap_payload = SAP["notif_payload"](t, g.user) if t["type"] in sap.SAP_TYPES else None
    can_validate = (can("confirm") and t["type"] in sap.SAP_TYPES and not t["sap_notif"])
    sap_log = q("SELECT l.*, u.name user_name FROM sap_log l LEFT JOIN users u ON u.id=l.user_id WHERE l.tag_id=? ORDER BY l.id DESC LIMIT 10", (tag_id,))
    validated_name = q("SELECT name FROM users WHERE id=?", (t["validated_by"],), one=True) if t["validated_by"] else None
    return render_template("tag_detail.html", fiori_open=fiori_open, sap_payload=sap_payload, can_validate=can_validate,
                           sap_log=sap_log, validated_name=validated_name["name"] if validated_name else "",
                           sap_linked=SAP["has_token"](g.user["id"]), t=t, history=history, cm=cm, pdr=pdr, targets=targets,
                           users=assignable_users(t["sga_id"]))


@app.route("/tags/<int:tag_id>/move", methods=["POST"])
def tag_move(tag_id):
    t = load_tag(tag_id)
    wants_json = request.headers.get("X-Requested-With") == "fetch"
    target = request.form.get("to") or (request.get_json(silent=True) or {}).get("to")

    def fail(msg):
        if wants_json:
            return jsonify(ok=False, message=msg), 400
        flash(msg, "error")
        return redirect(request.referrer or url_for("tag_detail", tag_id=tag_id))

    if target not in STATUSES or target == t["status"]:
        return fail("Statut cible invalide.")
    if not can_act_on_tag(t):
        return fail("Vous n'avez pas le droit de déplacer ce TAG.")
    cur_i, new_i = STATUSES.index(t["status"]), STATUSES.index(target)
    if not is_admin() and new_i != cur_i + 1:
        return fail("Un TAG descend d'un niveau à la fois. Seul l'administrateur peut le remonter.")
    if target == "Contre-mesure":
        if not can("countermeasure"):
            return fail("Le passage en contre-mesure est réservé à l'administrateur.")
        url = url_for("tag_countermeasure", tag_id=tag_id)
        return jsonify(ok=True, redirect=url) if wants_json else redirect(url)
    sets, vals = ["status=?", "updated_at=?", "confirmed_by=NULL", "confirmed_at=NULL"], [target, now()]
    comment = request.form.get("comment", "")
    if target == "En cours":
        treated_by = request.form.get("treated_by") or t["treated_by"] or g.user["id"]
        sets += ["treated_by=?", "treatment_start=COALESCE(treatment_start, ?)"]
        vals += [treated_by, now()]
        if request.form.get("ot_number"):
            sets.append("ot_number=?"); vals.append(request.form["ot_number"])
    if target == "Traité":
        sets += ["resolved_at=?", "treated_by=COALESCE(treated_by, ?)", "treatment_start=COALESCE(treatment_start, ?)"]
        vals += [now(), g.user["id"], now()]
    if new_i < 2:
        sets.append("resolved_at=NULL")
    if new_i < 1:
        sets += ["treatment_start=NULL", "treated_by=NULL"]
    ex("UPDATE tags SET " + ", ".join(sets) + " WHERE id=?", vals + [tag_id])
    ex("INSERT INTO tag_history(tag_id,user_id,from_status,to_status,comment,at) VALUES(?,?,?,?,?,?)",
       (tag_id, g.user["id"], t["status"], target, comment, now()))
    msg = "%s → %s" % (t["tag_code"], STATUS_INFO[target]["board"])
    if wants_json:
        return jsonify(ok=True, message=msg)
    flash(msg, "success")
    return redirect(request.referrer or url_for("tags_board"))


@app.route("/tags/<int:tag_id>/countermeasure", methods=["GET", "POST"])
@require("countermeasure")
def tag_countermeasure(tag_id):
    t = load_tag(tag_id)
    users = assignable_users(t["sga_id"])
    if request.method == "POST":
        f = request.form
        items = [x.strip() for x in f.get("items", "").splitlines() if x.strip()]
        errors = []
        if not f.get("countermeasure", "").strip():
            errors.append("Décrivez la contre-mesure.")
        if not items:
            errors.append("Ajoutez au moins un point à vérifier ou à faire.")
        if not f.get("assigned_to"):
            errors.append("Affectez la tâche à un opérateur ou un technicien.")
        if errors:
            for er in errors:
                flash(er, "error")
            return render_template("countermeasure.html", t=t, users=users, v=f)
        code = "CM-%04d" % tag_id
        cid = ex("""INSERT INTO checklists(code,name,equipment_id,organe,frequency,duration_min,machine_state,people,sop_url,
                 activity_type,assigned_to,origin_tag_id,countermeasure,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                 (code, f.get("name") or "Contre-mesure %s" % t["tag_code"], t["equipment_id"], f.get("organe", ""),
                  f.get("frequency", "Hebdomadaire"), int(f.get("duration_min") or 10), f.get("machine_state", ""),
                  int(f.get("people") or 1), f.get("sop_url", ""), f.get("activity_type", "AM"), f["assigned_to"], tag_id,
                  f["countermeasure"].strip(), now()))
        for p, txt in enumerate(items, 1):
            ex("INSERT INTO checklist_items(checklist_id,position,text) VALUES(?,?,?)", (cid, p, txt))
        ex("UPDATE tags SET status='Contre-mesure', countermeasure=?, updated_at=?, resolved_at=COALESCE(resolved_at, ?) WHERE id=?",
           (f["countermeasure"].strip(), now(), now(), tag_id))
        ex("INSERT INTO tag_history(tag_id,user_id,from_status,to_status,comment,at) VALUES(?,?,?,?,?,?)",
           (tag_id, g.user["id"], t["status"], "Contre-mesure", "Contre-mesure créée : checklist " + code, now()))
        ensure_tasks(checklist_id=cid)
        flash("Contre-mesure enregistrée : la tâche %s est ajoutée aux checklists préventives de l'équipement." % code, "success")
        return redirect(url_for("tag_detail", tag_id=tag_id))
    v = {"name": "Contre-mesure %s – %s" % (t["tag_code"], t["organe"] or (t["eq_name"] or "")),
         "organe": t["organe"], "frequency": "Hebdomadaire", "duration_min": 10, "people": 1,
         "activity_type": "RCM" if t["type"] == "RCM" else "AM", "machine_state": MACHINE_STATES[0]}
    return render_template("countermeasure.html", t=t, users=users, v=v)


# ---------------------------------------------------------------------------
# Checklists préventives (gestion admin)
# ---------------------------------------------------------------------------
@app.route("/checklists")
def checklists():
    clause, args = in_clause("e.sga_id", scope_sga_ids())
    sga = request.args.get("sga", "")
    extra, ea = "", []
    if sga:
        extra, ea = " AND e.sga_id=?", [sga]
    rows = q("""SELECT c.*, e.name eq_name, s.name sga_name, u.name user_name, u.role user_role, tg.tag_code origin_code,
               (SELECT COUNT(*) FROM checklist_items i WHERE i.checklist_id=c.id) n_items,
               (SELECT COUNT(*) FROM tasks k WHERE k.checklist_id=c.id AND k.due_date>=? AND k.status IN ('Réalisée','Confirmée')) n_done,
               (SELECT COUNT(*) FROM tasks k WHERE k.checklist_id=c.id AND k.due_date>=?) n_planned
               FROM checklists c JOIN equipment e ON e.id=c.equipment_id LEFT JOIN sga s ON s.id=e.sga_id
               LEFT JOIN users u ON u.id=c.assigned_to LEFT JOIN tags tg ON tg.id=c.origin_tag_id
               WHERE """ + clause + extra + " ORDER BY c.active DESC, s.name, e.position, c.code",
             [(today() - dt.timedelta(days=30)).isoformat()] * 2 + args + ea)
    operators = q("SELECT * FROM users WHERE active=1 AND role='Opérateur' ORDER BY name")
    return render_template("checklists.html", rows=rows, sgas=scoped_sgas(), sel_sga=sga, operators=operators)


def checklist_form(cl=None):
    equipment = q("SELECT e.*, s.name sga_name FROM equipment e LEFT JOIN sga s ON s.id=e.sga_id ORDER BY e.position")
    users = q("SELECT u.*, (SELECT GROUP_CONCAT(s.name, ', ') FROM user_sga us JOIN sga s ON s.id=us.sga_id WHERE us.user_id=u.id) sgas "
              "FROM users u WHERE u.active=1 AND u.role IN ('Opérateur','Technicien','Leader SGA') ORDER BY u.role, u.name")
    if request.method == "POST":
        f = request.form
        items = [x.strip() for x in f.get("items", "").splitlines() if x.strip()]
        if not f.get("name") or not f.get("equipment_id") or not items:
            flash("Nom, équipement et au moins un point sont obligatoires.", "error")
            return render_template("checklist_form.html", cl=cl, v=f, equipment=equipment, users=users)
        vals = (f["name"].strip(), f["equipment_id"], f.get("organe", "").strip(), f.get("frequency", "Quotidienne"),
                int(f.get("duration_min") or 10), f.get("machine_state", ""), int(f.get("people") or 1), f.get("sop_url", "").strip(),
                f.get("activity_type", "AM"), f.get("assigned_to") or None, 1 if f.get("active", "1") == "1" else 0)
        if cl:
            ex("""UPDATE checklists SET name=?, equipment_id=?, organe=?, frequency=?, duration_min=?, machine_state=?, people=?,
               sop_url=?, activity_type=?, assigned_to=?, active=? WHERE id=?""", vals + (cl["id"],))
            cid = cl["id"]
            ex("DELETE FROM checklist_items WHERE checklist_id=?", (cid,))
            # mettre à jour la tâche en cours non commencée
            for k in q("SELECT id FROM tasks WHERE checklist_id=? AND status='À réaliser'", (cid,)):
                ex("DELETE FROM tasks WHERE id=?", (k["id"],))
        else:
            n = q("SELECT COUNT(*)+1 n FROM checklists WHERE code LIKE 'CHK-%'", one=True)["n"]
            cid = ex("""INSERT INTO checklists(name,equipment_id,organe,frequency,duration_min,machine_state,people,sop_url,
                     activity_type,assigned_to,active,code,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     vals + ("CHK-%03d" % n, now()))
        for p, txt in enumerate(items, 1):
            ex("INSERT INTO checklist_items(checklist_id,position,text) VALUES(?,?,?)", (cid, p, txt))
        ensure_tasks(checklist_id=cid)
        flash("Checklist enregistrée et affectée.", "success")
        return redirect(url_for("checklists"))
    v = dict(cl) if cl else {"frequency": "Quotidienne", "duration_min": 10, "people": 1, "activity_type": "AM", "active": 1,
                             "equipment_id": request.args.get("equipment_id", "")}
    if cl:
        v["items"] = "\n".join(i["text"] for i in q("SELECT text FROM checklist_items WHERE checklist_id=? ORDER BY position", (cl["id"],)))
    return render_template("checklist_form.html", cl=cl, v=v, equipment=equipment, users=users)


@app.route("/checklists/new", methods=["GET", "POST"])
@require("checklists_manage")
def checklist_new():
    return checklist_form()


@app.route("/checklists/<int:cid>/edit", methods=["GET", "POST"])
@require("checklists_manage")
def checklist_edit(cid):
    cl = q("SELECT * FROM checklists WHERE id=?", (cid,), one=True) or abort(404)
    return checklist_form(cl)


@app.route("/checklists/<int:cid>/transfer", methods=["POST"])
@require("checklists_manage")
def checklist_transfer(cid):
    cl = q("SELECT * FROM checklists WHERE id=?", (cid,), one=True) or abort(404)
    assignee = request.form.get("assigned_to") or cl["assigned_to"]
    ex("UPDATE checklists SET transferred=1, transfer_date=?, activity_type='AM', assigned_to=? WHERE id=?",
       (today().isoformat(), assignee, cid))
    ex("DELETE FROM tasks WHERE checklist_id=? AND status='À réaliser'", (cid,))
    ensure_tasks(checklist_id=cid)
    flash("Activité transférée de RCM vers AM.", "success")
    return redirect(url_for("checklists"))


@app.route("/checklists/<int:cid>/history")
def checklist_history(cid):
    cl = q("SELECT c.*, e.name eq_name FROM checklists c JOIN equipment e ON e.id=c.equipment_id WHERE c.id=?", (cid,), one=True) or abort(404)
    rows = task_rows("k.checklist_id=? ORDER BY k.due_date DESC", (cid,))
    return render_template("checklist_history.html", cl=cl, rows=rows)


# ---------------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------------
@app.route("/documentation")
def documentation():
    eq_id = request.args.get("equipment", "")
    plants = q("SELECT * FROM plants ORDER BY name")
    tree = []
    for p in plants:
        lines = []
        for l in q("SELECT * FROM lines WHERE plant_id=? ORDER BY name", (p["id"],)):
            sgas = []
            for s in q("SELECT * FROM sga WHERE line_id=? ORDER BY name", (l["id"],)):
                sgas.append({"s": s, "eq": q("SELECT * FROM equipment WHERE sga_id=? ORDER BY position", (s["id"],))})
            orphans = q("SELECT * FROM equipment WHERE line_id=? AND sga_id IS NULL ORDER BY position", (l["id"],))
            lines.append({"l": l, "sgas": sgas, "orphans": orphans})
        tree.append({"p": p, "lines": lines})
    eq = None
    docs = []
    if eq_id:
        eq = q("SELECT e.*, s.name sga_name, l.name line_name, p.name plant_name FROM equipment e LEFT JOIN sga s ON s.id=e.sga_id "
               "JOIN lines l ON l.id=e.line_id JOIN plants p ON p.id=l.plant_id WHERE e.id=?", (eq_id,), one=True)
        docs = q("SELECT * FROM documents WHERE equipment_id=? ORDER BY doc_type, title", (eq_id,))
        cls = q("SELECT * FROM checklists WHERE equipment_id=? AND active=1 ORDER BY code", (eq_id,))
    else:
        cls = []
    dtype = request.args.get("type", "")
    return render_template("documentation.html", tree=tree, eq=eq, docs=docs, cls=cls, dtype=dtype,
                           all_equipment=q("SELECT * FROM equipment ORDER BY position"))


@app.route("/documentation/add", methods=["POST"])
@require("docs_manage")
def doc_add():
    f = request.form
    if not f.get("equipment_id") or not f.get("title"):
        flash("Équipement et titre obligatoires.", "error")
    else:
        ex("INSERT INTO documents(equipment_id,title,doc_type,version,owner,url,updated_at) VALUES(?,?,?,?,?,?,?)",
           (f["equipment_id"], f["title"], f.get("doc_type", "SOP"), f.get("version", "1.0"), f.get("owner", ""),
            f.get("url", ""), now()))
        flash("Document ajouté.", "success")
    return redirect(url_for("documentation", equipment=f.get("equipment_id", "")))


@app.route("/documentation/<int:doc_id>/delete", methods=["POST"])
@require("docs_manage")
def doc_delete(doc_id):
    d = q("SELECT * FROM documents WHERE id=?", (doc_id,), one=True) or abort(404)
    ex("DELETE FROM documents WHERE id=?", (doc_id,))
    flash("Document supprimé.", "success")
    return redirect(url_for("documentation", equipment=d["equipment_id"]))


# ---------------------------------------------------------------------------
# Synoptique
# ---------------------------------------------------------------------------
@app.route("/synoptic")
def synoptic():
    lines = q("SELECT l.*, p.name plant_name FROM lines l JOIN plants p ON p.id=l.plant_id ORDER BY p.name, l.name")
    line_id = request.args.get("line") or (str(lines[0]["id"]) if lines else "")
    eqs = q("SELECT e.*, s.name sga_name FROM equipment e LEFT JOIN sga s ON s.id=e.sga_id WHERE e.line_id=? ORDER BY e.position", (line_id or 0,))
    data = []
    for e in eqs:
        tags = q("SELECT type, status FROM tags WHERE equipment_id=? AND status IN ('Nouveau','En cours')", (e["id"],))
        counts = {tt["key"]: sum(1 for t in tags if t["type"] == tt["key"]) for tt in TAG_TYPES}
        worst = "SAFETY" if counts["SAFETY"] else ("RCM" if counts["RCM"] else ("AM" if counts["AM"] else None))
        data.append({"e": e, "counts": counts, "worst": worst, "total": len(tags)})
    sel = next((l for l in lines if str(l["id"]) == str(line_id)), None)
    return render_template("synoptic.html", lines=lines, line_id=str(line_id), data=data, sel=sel)


# ---------------------------------------------------------------------------
# Tableau SGA
# ---------------------------------------------------------------------------
def sga_board_data(sga_id):
    s = q("SELECT s.*, l.name line_name, p.name plant_name FROM sga s JOIN lines l ON l.id=s.line_id JOIN plants p ON p.id=l.plant_id WHERE s.id=?",
          (sga_id,), one=True)
    if not s:
        abort(404)
    b = q("SELECT b.*, u.name updated_name FROM sga_board b LEFT JOIN users u ON u.id=b.updated_by WHERE b.sga_id=?", (sga_id,), one=True)
    eqs = q("SELECT * FROM equipment WHERE sga_id=? ORDER BY position", (sga_id,))
    eq_ids = [e["id"] for e in eqs]
    ec, ea = in_clause("equipment_id", eq_ids)
    members = q("SELECT u.* FROM users u JOIN user_sga us ON us.user_id=u.id WHERE us.sga_id=? AND u.active=1 AND u.role IN "
                "('Leader SGA','Opérateur','Technicien') ORDER BY CASE u.role WHEN 'Leader SGA' THEN 0 WHEN 'Opérateur' THEN 1 ELSE 2 END, u.name", (sga_id,))
    ws = week_bounds()[0].isoformat()
    m30 = (today() - dt.timedelta(days=30)).isoformat()
    cls = q("""SELECT c.*, u.name user_name,
              (SELECT COUNT(*) FROM tasks k WHERE k.checklist_id=c.id AND k.due_date>=? AND k.due_date<=?) planned,
              (SELECT COUNT(*) FROM tasks k WHERE k.checklist_id=c.id AND k.due_date>=? AND k.due_date<=? AND k.status IN ('Réalisée','Confirmée')) done,
              (SELECT ROUND(AVG(k.duration_min)) FROM tasks k WHERE k.checklist_id=c.id AND k.duration_min IS NOT NULL AND k.due_date>=?) avg_dur
              FROM checklists c LEFT JOIN users u ON u.id=c.assigned_to WHERE c.active=1 AND """ + in_clause("c.equipment_id", eq_ids)[0] + " ORDER BY c.code",
            [m30, today().isoformat(), m30, today().isoformat(), m30] + ea)
    docs = q("SELECT * FROM documents WHERE " + ec + " ORDER BY title", ea)
    tags = q("SELECT * FROM tags WHERE sga_id=?", (sga_id,))
    def rate(tp):
        sel = [t for t in tags if t["type"] == tp]
        return pct(sum(1 for t in sel if t["status"] in ("Traité", "Contre-mesure")), len(sel)), len(sel)
    rcm_open = sum(1 for c in cls if c["activity_type"] == "RCM" and not c["transferred"])
    transferred = [c for c in cls if c["transferred"]]
    planned = sum(c["planned"] for c in cls)
    done = sum(c["done"] for c in cls)
    audits = {lv["key"]: q("SELECT a.*, u.name auditor FROM audits a LEFT JOIN users u ON u.id=a.auditor_id WHERE a.sga_id=? AND a.level=? "
                           "ORDER BY a.audit_date DESC, a.id DESC LIMIT 1", (sga_id, lv["key"]), one=True) for lv in AUDIT_LEVELS}
    soc = [t for t in tags if t["anomaly_type"] in SOC_HTR_TYPES]
    return {"s": s, "b": b, "eqs": eqs, "members": members, "cls": cls, "docs": docs,
            "sop": [d for d in docs if d["doc_type"] == "SOP"], "opl": [d for d in docs if d["doc_type"] == "OPL"],
            "tech": [d for d in docs if d["doc_type"] == "TECH"],
            "am_rate": rate("AM"), "rcm_rate": rate("RCM"), "safety_rate": rate("SAFETY"),
            "transferred": transferred, "transfer_rate": pct(len(transferred), len(transferred) + rcm_open),
            "planned": planned, "done": done, "real_rate": pct(done, planned), "audits": audits,
            "soc": [t for t in soc if t["anomaly_type"] == "Sources de contamination"],
            "htr": [t for t in soc if t["anomaly_type"] != "Sources de contamination"],
            "cm_count": sum(1 for t in tags if t["status"] == "Contre-mesure"), "tags_count": len(tags)}


@app.route("/sga-board")
def sga_board():
    sgas = scoped_sgas() if not is_admin() else q("SELECT s.*, l.name line_name FROM sga s JOIN lines l ON l.id=s.line_id ORDER BY s.name")
    if not sgas:
        sgas = q("SELECT s.*, l.name line_name FROM sga s JOIN lines l ON l.id=s.line_id ORDER BY s.name")
    sga_id = request.args.get("sga") or (str(sgas[0]["id"]) if sgas else None)
    if not sga_id:
        return render_template("message.html", title="Tableau SGA", message="Aucune SGA n'est encore créée.")
    data = sga_board_data(sga_id)
    editable = can("sga_board_edit") and (is_admin() or scope_sga_ids() is None or int(sga_id) in scope_sga_ids())
    return render_template("sga_board.html", d=data, sgas=sgas, sga_id=str(sga_id), editable=editable,
                           edit=request.args.get("edit") == "1" and editable)


@app.route("/sga-board/<int:sga_id>", methods=["POST"])
@require("sga_board_edit")
def sga_board_save(sga_id):
    ids = scope_sga_ids()
    if not (is_admin() or ids is None or sga_id in ids):
        abort(403)
    fields = ["charte", "objectifs", "centrage", "score_5s", "plan_5s", "kaizen", "mtbf", "mttr", "pertes_cadence", "pannes",
              "arrets_mineurs", "preparatifs"]
    vals = []
    for k in fields:
        v = request.form.get(k, "").strip()
        if k == "score_5s":
            v = int(v) if v.isdigit() else None
        vals.append(v)
    if q("SELECT 1 FROM sga_board WHERE sga_id=?", (sga_id,), one=True):
        ex("UPDATE sga_board SET " + ", ".join(k + "=?" for k in fields) + ", updated_at=?, updated_by=? WHERE sga_id=?",
           vals + [now(), g.user["id"], sga_id])
    else:
        ex("INSERT INTO sga_board(sga_id," + ",".join(fields) + ",updated_at,updated_by) VALUES(" + ",".join("?" * (len(fields) + 3)) + ")",
           [sga_id] + vals + [now(), g.user["id"]])
    if request.form.get("am_step") and is_admin():
        ex("UPDATE sga SET am_step=? WHERE id=?", (int(request.form["am_step"]), sga_id))
    flash("Tableau SGA mis à jour.", "success")
    return redirect(url_for("sga_board", sga=sga_id))


# ---------------------------------------------------------------------------
# Audits AM
# ---------------------------------------------------------------------------
def audit_auto_proofs(sga_id):
    d = sga_board_data(sga_id)
    b = d["b"]
    upd = "—"
    if b and b["updated_at"]:
        days = (today() - dt.datetime.strptime(b["updated_at"][:10], "%Y-%m-%d").date()).days
        upd = "MAJ il y a %d j" % days
    tag_total = d["tags_count"]
    treated = sum(1 for t in q("SELECT status FROM tags WHERE sga_id=?", (sga_id,)) if t["status"] in ("Traité", "Contre-mesure"))
    times = [c for c in d["cls"] if c["avg_dur"]]
    ci = "%d / %d min (réel / standard)" % (sum(c["avg_dur"] for c in times), sum(c["duration_min"] for c in times)) if times else "—"
    return {"tags_count": "%d TAGs émis" % tag_total, "tag_rate": "%d %% traités" % pct(treated, tag_total),
            "soc_htr": "%d SOC · %d HTR" % (len(d["soc"]), len(d["htr"])), "sop_count": "%d SOP" % len(d["sop"]),
            "board_update": upd, "cm_count": "%d contre-mesures" % d["cm_count"], "ci_time": ci,
            "realization": "%d %% réalisées (30 j)" % d["real_rate"], "opl_count": "%d OPL" % len(d["opl"]),
            "transfer": "%d %% transférées" % d["transfer_rate"]}


def allowed_levels():
    return [lv for lv in AUDIT_LEVELS if can(lv["perm"])]


def audit_sgas_for(level):
    if level == "Groupe OE" or is_admin():
        return q("SELECT s.*, l.name line_name FROM sga s JOIN lines l ON l.id=s.line_id ORDER BY s.name")
    if level == "Plant Manager":
        return q("SELECT s.*, l.name line_name FROM sga s JOIN lines l ON l.id=s.line_id WHERE l.plant_id=? ORDER BY s.name",
                 (g.user["plant_id"],))
    return scoped_sgas()


@app.route("/audits")
def audits():
    clause, args = in_clause("a.sga_id", scope_sga_ids())
    rows = q("""SELECT a.*, s.name sga_name, u.name auditor FROM audits a JOIN sga s ON s.id=a.sga_id LEFT JOIN users u ON u.id=a.auditor_id
               WHERE """ + clause + " ORDER BY a.audit_date DESC, a.id DESC", args)
    status = []
    sgas = scoped_sgas()
    for s in sgas:
        lv = {}
        for L in AUDIT_LEVELS:
            lv[L["key"]] = q("SELECT score, audit_date FROM audits WHERE sga_id=? AND level=? AND am_step=? ORDER BY audit_date DESC, id DESC LIMIT 1",
                             (s["id"], L["key"], s["am_step"]), one=True)
        ready = all(lv[L["key"]] and lv[L["key"]]["score"] >= AUDIT_THRESHOLD for L in AUDIT_LEVELS)
        status.append({"s": s, "lv": lv, "ready": ready})
    return render_template("audits.html", rows=rows, levels=AUDIT_LEVELS, allowed=allowed_levels(), status=status,
                           threshold=AUDIT_THRESHOLD)


@app.route("/audits/new", methods=["GET", "POST"])
def audit_new():
    allowed = allowed_levels()
    if not allowed:
        abort(403)
    level = request.values.get("level") or allowed[0]["key"]
    if level not in [lv["key"] for lv in allowed]:
        abort(403)
    sgas = audit_sgas_for(level)
    sga_id = request.values.get("sga") or (str(sgas[0]["id"]) if sgas else "")
    if not sga_id or int(sga_id) not in [s["id"] for s in sgas]:
        abort(403)
    s = q("SELECT * FROM sga WHERE id=?", (sga_id,), one=True)
    grid = AUDIT_GRID.get(s["am_step"], AUDIT_GRID_DEFAULT)
    proofs = audit_auto_proofs(sga_id)
    if request.method == "POST" and request.form.get("save") == "1":
        notes = []
        for i, (crit, key) in enumerate(grid):
            n = request.form.get("note_%d" % i, "")
            notes.append(int(n) if n.isdigit() else 0)
        score = round(sum(notes) / (5 * len(notes)) * 100)
        aid = ex("INSERT INTO audits(level,sga_id,am_step,auditor_id,audit_date,score,comment) VALUES(?,?,?,?,?,?,?)",
                 (level, sga_id, s["am_step"], g.user["id"], today().isoformat(), score, request.form.get("comment", "")))
        for i, (crit, key) in enumerate(grid):
            proof = request.form.get("proof_%d" % i, "").strip() or (proofs.get(key, "") if key else "")
            ex("INSERT INTO audit_items(audit_id,criterion,note,proof) VALUES(?,?,?,?)", (aid, crit, notes[i], proof))
        flash("Audit enregistré : score %d %%." % score, "success")
        return redirect(url_for("audit_detail", audit_id=aid))
    return render_template("audit_form.html", allowed=allowed, level=level, sgas=sgas, sga_id=str(sga_id), s=s, grid=grid,
                           proofs=proofs, threshold=AUDIT_THRESHOLD)


@app.route("/audits/<int:audit_id>")
def audit_detail(audit_id):
    a = q("SELECT a.*, s.name sga_name, u.name auditor FROM audits a JOIN sga s ON s.id=a.sga_id LEFT JOIN users u ON u.id=a.auditor_id WHERE a.id=?",
          (audit_id,), one=True) or abort(404)
    items = q("SELECT * FROM audit_items WHERE audit_id=? ORDER BY id", (audit_id,))
    return render_template("audit_detail.html", a=a, items=items, threshold=AUDIT_THRESHOLD)


@app.route("/sga/<int:sga_id>/advance", methods=["POST"])
def sga_advance(sga_id):
    if not is_admin():
        abort(403)
    ex("UPDATE sga SET am_step=MIN(am_step+1, 7) WHERE id=?", (sga_id,))
    flash("La SGA passe à l'étape AM suivante.", "success")
    return redirect(url_for("audits"))


# ---------------------------------------------------------------------------
# PDR
# ---------------------------------------------------------------------------
@app.route("/pdr")
@require("pdr_view")
def pdr():
    parts = q("SELECT * FROM spare_parts ORDER BY reference")
    st = request.args.get("status", "")
    rows = q("""SELECT r.*, p.reference part_ref, p.stock, p.min_stock, e.name eq_name, t.tag_code, u.name requester
               FROM pdr_requests r LEFT JOIN spare_parts p ON p.id=r.part_id LEFT JOIN equipment e ON e.id=r.equipment_id
               LEFT JOIN tags t ON t.id=r.tag_id LEFT JOIN users u ON u.id=r.requester_id """ +
             ("WHERE r.status=? " if st else "") + "ORDER BY r.id DESC", (st,) if st else ())
    kpi = {"low": sum(1 for p in parts if p["stock"] <= p["min_stock"]),
           "waiting": q("SELECT COUNT(*) n FROM pdr_requests WHERE status IN ('Demandée','Validée')", one=True)["n"],
           "ordered": q("SELECT COUNT(*) n FROM pdr_requests WHERE status='Commandée'", one=True)["n"],
           "received": q("SELECT COUNT(*) n FROM pdr_requests WHERE status='Réceptionnée' AND received_at>=?",
                         ((today() - dt.timedelta(days=7)).isoformat(),), one=True)["n"]}
    tags = q("SELECT t.id, t.tag_code, t.description FROM tags t WHERE t.status IN ('Nouveau','En cours') AND t.type='RCM' ORDER BY t.id DESC")
    return render_template("pdr.html", parts=parts, rows=rows, kpi=kpi, tags=tags, st=st, statuses=PDR_STATUSES,
                           equipment=q("SELECT * FROM equipment ORDER BY position"))


@app.route("/pdr/request", methods=["POST"])
@require("pdr_request")
def pdr_request():
    f = request.form
    part = q("SELECT * FROM spare_parts WHERE id=?", (f.get("part_id") or 0,), one=True)
    des = part["designation"] if part else f.get("designation", "").strip()
    if not des or not f.get("qty"):
        flash("Article et quantité obligatoires.", "error")
        return redirect(url_for("pdr"))
    n = q("SELECT COUNT(*)+41 n FROM pdr_requests", one=True)["n"]
    ex("""INSERT INTO pdr_requests(code,part_id,designation,qty,equipment_id,tag_id,requester_id,status,comment,created_at)
       VALUES(?,?,?,?,?,?,?, 'Demandée', ?, ?)""",
       ("DA-%04d" % n, part["id"] if part else None, des, float(f["qty"]), f.get("equipment_id") or None, f.get("tag_id") or None,
        g.user["id"], f.get("comment", ""), now()))
    flash("Demande PDR créée.", "success")
    return redirect(url_for("pdr"))


@app.route("/pdr/<int:rid>/<action>", methods=["POST"])
def pdr_action(rid, action):
    r = q("SELECT * FROM pdr_requests WHERE id=?", (rid,), one=True) or abort(404)
    if action in ("validate", "refuse") and can("pdr_validate") and r["status"] == "Demandée":
        ex("UPDATE pdr_requests SET status=?, validated_by=?, validated_at=? WHERE id=?",
           ("Validée" if action == "validate" else "Refusée", g.user["id"], now(), rid))
    elif action == "order" and can("pdr_order") and r["status"] == "Validée":
        ex("UPDATE pdr_requests SET status='Commandée', ordered_at=?, expected_date=? WHERE id=?",
           (now(), request.form.get("expected_date") or (today() + dt.timedelta(days=7)).isoformat(), rid))
    elif action == "receive" and can("pdr_order") and r["status"] == "Commandée":
        ex("UPDATE pdr_requests SET status='Réceptionnée', received_at=? WHERE id=?", (now(), rid))
        if r["part_id"]:
            ex("UPDATE spare_parts SET stock=stock+? WHERE id=?", (r["qty"], r["part_id"]))
    else:
        abort(403)
    flash("Demande %s mise à jour." % r["code"], "success")
    return redirect(url_for("pdr"))


@app.route("/pdr/part", methods=["POST"])
def pdr_part():
    if not (can("pdr_order") or is_admin()):
        abort(403)
    f = request.form
    if f.get("id"):
        ex("UPDATE spare_parts SET stock=?, min_stock=? WHERE id=?", (float(f.get("stock") or 0), float(f.get("min_stock") or 0), f["id"]))
    elif f.get("reference") and f.get("designation"):
        try:
            ex("INSERT INTO spare_parts(reference,designation,stock,min_stock,unit,location) VALUES(?,?,?,?,?,?)",
               (f["reference"], f["designation"], float(f.get("stock") or 0), float(f.get("min_stock") or 0), f.get("unit") or "pcs", f.get("location", "")))
        except sqlite3.IntegrityError:
            flash("Cette référence existe déjà.", "error")
            return redirect(url_for("pdr"))
    flash("Article enregistré.", "success")
    return redirect(url_for("pdr"))


# ---------------------------------------------------------------------------
# Paramètres : usines, lignes, SGA, équipements
# ---------------------------------------------------------------------------
ENTITIES = {
    "plant": {"table": "plants", "label": "usine"},
    "line": {"table": "lines", "label": "ligne"},
    "sga": {"table": "sga", "label": "SGA"},
    "equipment": {"table": "equipment", "label": "équipement"},
}


@app.route("/settings")
@require("settings")
def settings():
    tab = request.args.get("tab", "plants")
    plants = q("SELECT p.*, (SELECT COUNT(*) FROM lines l WHERE l.plant_id=p.id) n_lines FROM plants p ORDER BY name")
    lines = q("SELECT l.*, p.name plant_name, (SELECT COUNT(*) FROM equipment e WHERE e.line_id=l.id) n_eq, "
              "(SELECT COUNT(*) FROM sga s WHERE s.line_id=l.id) n_sga FROM lines l JOIN plants p ON p.id=l.plant_id ORDER BY p.name, l.name")
    sgas = q("SELECT s.*, l.name line_name, p.name plant_name, (SELECT COUNT(*) FROM equipment e WHERE e.sga_id=s.id) n_eq, "
             "(SELECT COUNT(*) FROM user_sga us WHERE us.sga_id=s.id) n_users FROM sga s JOIN lines l ON l.id=s.line_id "
             "JOIN plants p ON p.id=l.plant_id ORDER BY s.name")
    eqs = q("SELECT e.*, l.name line_name, p.name plant_name, s.name sga_name FROM equipment e JOIN lines l ON l.id=e.line_id "
            "JOIN plants p ON p.id=l.plant_id LEFT JOIN sga s ON s.id=e.sga_id ORDER BY p.name, l.name, e.position")
    return render_template("settings.html", tab=tab, plants=plants, lines=lines, sgas=sgas, eqs=eqs)


@app.route("/settings/<entity>", methods=["POST"])
@app.route("/settings/<entity>/<int:item_id>", methods=["POST"])
@require("settings")
def settings_save(entity, item_id=None):
    f = request.form
    try:
        if entity == "plant":
            vals = (f["code"].strip(), f["name"].strip(), f.get("city", "").strip())
            if item_id:
                ex("UPDATE plants SET code=?, name=?, city=? WHERE id=?", vals + (item_id,))
            else:
                ex("INSERT INTO plants(code,name,city) VALUES(?,?,?)", vals)
            tab = "plants"
        elif entity == "line":
            vals = (f["plant_id"], f.get("code", "").strip(), f["name"].strip())
            if item_id:
                ex("UPDATE lines SET plant_id=?, code=?, name=? WHERE id=?", vals + (item_id,))
            else:
                ex("INSERT INTO lines(plant_id,code,name) VALUES(?,?,?)", vals)
            tab = "lines"
        elif entity == "sga":
            vals = (f["line_id"], f["name"].strip(), f.get("description", "").strip(), int(f.get("am_step") or 1))
            if item_id:
                ex("UPDATE sga SET line_id=?, name=?, description=?, am_step=? WHERE id=?", vals + (item_id,))
                sid = item_id
            else:
                sid = ex("INSERT INTO sga(line_id,name,description,am_step,created_at) VALUES(?,?,?,?,?)", vals + (now(),))
            eq_ids = [int(x) for x in f.getlist("equipment_ids")]
            ex("UPDATE equipment SET sga_id=NULL WHERE sga_id=?", (sid,))
            for e in eq_ids:
                ex("UPDATE equipment SET sga_id=? WHERE id=?", (sid, e))
                ex("UPDATE tags SET sga_id=? WHERE equipment_id=?", (sid, e))
            tab = "sga"
        elif entity == "equipment":
            line = q("SELECT * FROM lines WHERE id=?", (f["line_id"],), one=True)
            if not line:
                raise ValueError("Ligne obligatoire")
            sga_id = f.get("sga_id") or None
            if sga_id:
                s = q("SELECT * FROM sga WHERE id=?", (sga_id,), one=True)
                if not s or s["line_id"] != line["id"]:
                    raise ValueError("La SGA doit appartenir à la même ligne que l'équipement.")
            vals = (f["line_id"], sga_id, f.get("reference", "").strip(), f["name"].strip(), int(f.get("position") or 0))
            if item_id:
                ex("UPDATE equipment SET line_id=?, sga_id=?, reference=?, name=?, position=? WHERE id=?", vals + (item_id,))
                ex("UPDATE tags SET sga_id=?, line_id=? WHERE equipment_id=?", (sga_id, f["line_id"], item_id))
            else:
                ex("INSERT INTO equipment(line_id,sga_id,reference,name,position) VALUES(?,?,?,?,?)", vals)
            tab = "equipment"
        else:
            abort(404)
        flash("Enregistrement effectué.", "success")
    except (KeyError, ValueError, sqlite3.IntegrityError) as e:
        flash("Enregistrement impossible : %s" % e, "error")
        tab = {"plant": "plants", "line": "lines", "sga": "sga", "equipment": "equipment"}.get(entity, "plants")
    return redirect(url_for("settings", tab=tab))


@app.route("/settings/<entity>/<int:item_id>/edit")
@require("settings")
def settings_edit(entity, item_id):
    if entity not in ENTITIES:
        abort(404)
    item = q("SELECT * FROM %s WHERE id=?" % ENTITIES[entity]["table"], (item_id,), one=True) or abort(404)
    ctx = dict(entity=entity, item=item, label=ENTITIES[entity]["label"], plants=q("SELECT * FROM plants ORDER BY name"),
               lines=q("SELECT l.*, p.name plant_name FROM lines l JOIN plants p ON p.id=l.plant_id ORDER BY p.name, l.name"),
               sgas=q("SELECT s.*, l.name line_name FROM sga s JOIN lines l ON l.id=s.line_id ORDER BY s.name"))
    if entity == "sga":
        ctx["line_eq"] = q("SELECT e.*, s.name sga_name FROM equipment e LEFT JOIN sga s ON s.id=e.sga_id WHERE e.line_id=? ORDER BY e.position",
                           (item["line_id"],))
    return render_template("settings_edit.html", **ctx)


@app.route("/settings/<entity>/<int:item_id>/delete", methods=["POST"])
@require("settings")
def settings_delete(entity, item_id):
    if entity not in ENTITIES:
        abort(404)
    table = ENTITIES[entity]["table"]
    used = {
        "plant": "SELECT COUNT(*) n FROM lines WHERE plant_id=?",
        "line": "SELECT COUNT(*) n FROM equipment WHERE line_id=?",
        "sga": "SELECT COUNT(*) n FROM equipment WHERE sga_id=?",
        "equipment": "SELECT COUNT(*) n FROM tags WHERE equipment_id=?",
    }[entity]
    if q(used, (item_id,), one=True)["n"]:
        flash("Suppression impossible : cet élément contient encore des données rattachées.", "error")
    else:
        ex("DELETE FROM %s WHERE id=?" % table, (item_id,))
        flash("Élément supprimé.", "success")
    return redirect(url_for("settings", tab={"plant": "plants", "line": "lines", "sga": "sga", "equipment": "equipment"}[entity]))


# ---------------------------------------------------------------------------
# Utilisateurs & accès
# ---------------------------------------------------------------------------
@app.route("/users", methods=["GET", "POST"])
@require("settings")
def users():
    if request.method == "POST":
        return save_user(None)
    rows = q("SELECT u.*, p.name plant_name FROM users u LEFT JOIN plants p ON p.id=u.plant_id ORDER BY u.active DESC, "
             "CASE u.role WHEN 'Administrateur' THEN 0 ELSE 1 END, u.role, u.name")
    data = [{"u": r, "sgas": user_sga_names(r["id"]), "n_perms": len(json.loads(r["permissions"] or "[]"))} for r in rows]
    return render_template("users.html", data=data, plants=q("SELECT * FROM plants"),
                           sgas=q("SELECT s.*, l.name line_name FROM sga s JOIN lines l ON l.id=s.line_id ORDER BY s.name"),
                           role_defaults=ROLE_DEFAULTS)


def save_user(user_id):
    f = request.form
    role = f.get("role")
    if role not in ROLES or not f.get("name") or not f.get("username"):
        flash("Nom, identifiant et type d'utilisateur obligatoires.", "error")
        return redirect(request.referrer or url_for("users"))
    if role == "Administrateur":
        perms = PERM_KEYS
    elif f.get("perm_mode") == "custom":
        perms = [p for p in PERM_KEYS if f.get("perm_" + p)]
    else:
        perms = ROLE_DEFAULTS[role]
    try:
        if user_id:
            ex("UPDATE users SET name=?, username=?, email=?, role=?, plant_id=?, permissions=?, active=? WHERE id=?",
               (f["name"].strip(), f["username"].strip(), f.get("email", ""), role, f.get("plant_id") or None, json.dumps(perms),
                1 if f.get("active", "1") == "1" else 0, user_id))
            if f.get("password"):
                ex("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(f["password"]), user_id))
        else:
            if not f.get("password"):
                flash("Mot de passe obligatoire à la création.", "error")
                return redirect(url_for("users"))
            user_id = ex("INSERT INTO users(name,username,email,role,password_hash,plant_id,permissions,active) VALUES(?,?,?,?,?,?,?,1)",
                         (f["name"].strip(), f["username"].strip(), f.get("email", ""), role, generate_password_hash(f["password"]),
                          f.get("plant_id") or None, json.dumps(perms)))
    except sqlite3.IntegrityError:
        flash("Cet identifiant existe déjà.", "error")
        return redirect(request.referrer or url_for("users"))
    ex("UPDATE users SET sap_user=? WHERE id=?", ((f.get("sap_user") or "").strip().upper() or None, user_id))
    ex("DELETE FROM user_sga WHERE user_id=?", (user_id,))
    for s in f.getlist("sga_ids"):
        ex("INSERT OR IGNORE INTO user_sga(user_id,sga_id) VALUES(?,?)", (user_id, int(s)))
    flash("Utilisateur enregistré.", "success")
    return redirect(url_for("users"))


@app.route("/users/<int:user_id>", methods=["GET", "POST"])
@require("settings")
def user_edit(user_id):
    u = q("SELECT * FROM users WHERE id=?", (user_id,), one=True) or abort(404)
    if request.method == "POST":
        if u["username"] == ADMIN_USER and request.form.get("role") != "Administrateur":
            flash("Le compte administrateur principal doit rester administrateur.", "error")
            return redirect(url_for("user_edit", user_id=user_id))
        return save_user(user_id)
    perms = set(json.loads(u["permissions"] or "[]"))
    custom = sorted(perms) != sorted(ROLE_DEFAULTS.get(u["role"], []))
    my_sgas = [r["sga_id"] for r in q("SELECT sga_id FROM user_sga WHERE user_id=?", (user_id,))]
    return render_template("user_edit.html", u=u, perms=perms, custom=custom, my_sgas=my_sgas, plants=q("SELECT * FROM plants"),
                           sgas=q("SELECT s.*, l.name line_name FROM sga s JOIN lines l ON l.id=s.line_id ORDER BY s.name"),
                           role_defaults=ROLE_DEFAULTS)


# ---------------------------------------------------------------------------
# Divers
# ---------------------------------------------------------------------------
@app.route("/account", methods=["GET", "POST"])
def account():
    u = g.user
    if request.method == "POST":
        f = request.form
        if not check_password_hash(u["password_hash"], f.get("current", "")):
            flash("Mot de passe actuel incorrect.", "error")
        elif len(f.get("new", "")) < 8:
            flash("Le nouveau mot de passe doit contenir au moins 8 caractères.", "error")
        elif f.get("new") != f.get("confirm"):
            flash("Les deux nouveaux mots de passe ne sont pas identiques.", "error")
        else:
            ex("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(f["new"]), u["id"]))
            flash("Mot de passe modifié.", "success")
            return redirect(url_for("home"))
    return render_template("account.html")


@app.route("/admin/backup")
def admin_backup():
    """Téléchargement d'une copie cohérente de la base (administrateur uniquement)."""
    if not is_admin():
        abort(403)
    import tempfile
    from flask import send_file
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    dst = sqlite3.connect(tmp.name)
    db().backup(dst)
    dst.close()
    name = "assetpilot_sauvegarde_%s.db" % dt.datetime.now().strftime("%Y%m%d_%H%M")
    return send_file(tmp.name, as_attachment=True, download_name=name, mimetype="application/octet-stream")


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/sw.js")
def service_worker():
    resp = send_from_directory(os.path.join(BASE, "static"), "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/uploads/<path:filename>")
def uploads(filename):
    return send_from_directory(UPLOADS, filename)


@app.route("/api/lines")
def api_lines():
    return jsonify([dict(id=r["id"], name=r["name"]) for r in q("SELECT * FROM lines WHERE plant_id=? ORDER BY name", (request.args.get("plant_id", 0),))])


@app.route("/api/sgas")
def api_sgas():
    return jsonify([dict(id=r["id"], name=r["name"]) for r in q("SELECT * FROM sga WHERE line_id=? ORDER BY name", (request.args.get("line_id", 0),))])


SAP = sap.register(app, dict(q=q, ex=ex, now=now, can=can, is_admin=is_admin, require=require, load_tag=load_tag,
                             scope_sga_ids=scope_sga_ids, in_clause=in_clause))

os.makedirs(UPLOADS, exist_ok=True)
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("AssetPilot — http://127.0.0.1:%d" % port)
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", host="127.0.0.1", port=port)
