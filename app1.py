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
APP_DB = os.path.join(BASE_DIR, "pmp.db")

app = Flask(__name__)
app.secret_key = "change-this-secret-please"

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

    c.execute("SELECT COUNT(*) AS n FROM users")
    if c.fetchone()["n"] == 0:
        c.execute("""
            INSERT INTO users(username,password_hash,role)
            VALUES (?,?,?)
        """, ("admin", generate_password_hash("1234"), "admin"))

    conn.commit()
    conn.close()

# ⚠️ IMPORTANT : initialisation DB AU CHARGEMENT (Render / Gunicorn)
init_db()

# -------------------------------------------------------
# AUTH HELPERS
# -------------------------------------------------------
def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE id=?",
        (user_id,)   # ✅ CORRECTION ICI
    ).fetchone()
    db.close()
    return user

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
# ROUTES
# -------------------------------------------------------
@app.route("/")
@login_required()
def index():
    return render_template("index.html", current_year=datetime.now().year)

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","")
        password = request.form.get("password","")

        db = get_db()
        u = db.execute(
            "SELECT * FROM users WHERE username=?",
            (username,)
        ).fetchone()
        db.close()

        if u and check_password_hash(u["password_hash"], password):
            session["user_id"] = u["id"]
            session["role"] = u["role"]
            return redirect(url_for("index"))

        return render_template("login.html", error="Login incorrect")

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/documentation")
@login_required()
def documentation():
    docs_dir = os.path.join(app.root_path, "static", "images", "docs")  # ✅ CORRECTION

    pdfs = []
    if os.path.exists(docs_dir):
        pdfs = [f for f in os.listdir(docs_dir) if f.lower().endswith(".pdf")]

    return render_template("documentation.html", pdfs=pdfs)

# -------------------------------------------------------
# MAIN LOCAL ONLY
# -------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
