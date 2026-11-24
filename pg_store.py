"""Persistance de MAKINTI dans PostgreSQL (variable DATABASE_URL).

L'application travaille sur sa base SQLite locale, et une copie complète de cette base
est enregistrée dans PostgreSQL après chaque modification. Au démarrage, la base est
rechargée depuis PostgreSQL. Ainsi, rien n'est perdu quand l'hébergeur efface les fichiers
du service (déploiement, redémarrage, mise en veille), même sans disque persistant.

Tables utilisées (préfixe assetpilot_, aucune autre table de la base n'est lue ni modifiée) :
  assetpilot_store : la base (nom 'assetpilot.db') et une copie par jour (PG_DAILY_KEEP jours, 7 par défaut) ;
  assetpilot_files : les photos des TAGs.
"""
import datetime as dt
import os
import sqlite3
import tempfile
import threading
import time

URL = (os.environ.get("DATABASE_URL") or "").strip()
MAIN = "assetpilot.db"
DAILY_KEEP = max(1, int(os.environ.get("PG_DAILY_KEEP", "7") or 7))

_lock = threading.Lock()
_state = {"saved_mtime": 0.0, "last_error": None, "last_save": None, "thread": False}


def enabled():
    return bool(URL)


def _connect():
    import psycopg  # installé par requirements.txt (psycopg[binary])
    url = URL.replace("postgres://", "postgresql://", 1)
    return psycopg.connect(url, connect_timeout=20)


def _ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS assetpilot_store (
                      name TEXT PRIMARY KEY, data BYTEA NOT NULL, size INTEGER NOT NULL,
                      updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS assetpilot_files (
                      path TEXT PRIMARY KEY, data BYTEA NOT NULL, updated_at TEXT NOT NULL)""")


def _now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def status():
    return dict(enabled=enabled(), last_save=_state["last_save"], last_error=_state["last_error"])


# --------------------------------------------------------------------------- base de données
def load_db(db_path):
    """Au démarrage : remplace la base locale par celle enregistrée dans PostgreSQL.
    Retourne True si une base a été rechargée, False si PostgreSQL n'en contient pas encore."""
    if not enabled():
        return False
    with _connect() as conn:
        _ensure(conn)
        row = conn.execute("SELECT data FROM assetpilot_store WHERE name=%s", (MAIN,)).fetchone()
    if not row:
        return False
    data = bytes(row[0])
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    tmp = db_path + ".chargement"
    with open(tmp, "wb") as f:
        f.write(data)
    chk = sqlite3.connect(tmp)  # contrôle : le fichier doit être une base lisible
    try:
        chk.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    finally:
        chk.close()
    for ext in ("-wal", "-shm", "-journal"):
        if os.path.exists(db_path + ext):
            os.remove(db_path + ext)
    os.replace(tmp, db_path)
    _state["saved_mtime"] = os.path.getmtime(db_path)
    print("Base MAKINTI rechargée depuis PostgreSQL (%d Ko)" % (len(data) // 1024))
    return True


def _snapshot(db_path):
    """Copie cohérente de la base SQLite (même si une écriture est en cours)."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(tmp)
        src.backup(dst)
        dst.close()
        src.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        os.remove(tmp)


def save_db(db_path):
    """Enregistre la base dans PostgreSQL. Ne lève pas d'exception : l'erreur est journalisée
    et l'enregistrement est retenté automatiquement."""
    if not enabled() or not os.path.exists(db_path):
        return False
    with _lock:  # un seul enregistrement à la fois, dans l'ordre : le plus récent gagne toujours
        try:
            mtime = os.path.getmtime(db_path)
            data = _snapshot(db_path)
            day = MAIN + "@" + dt.date.today().isoformat()
            with _connect() as conn:
                _ensure(conn)
                conn.execute("""INSERT INTO assetpilot_store(name, data, size, updated_at) VALUES (%s, %s, %s, %s)
                                ON CONFLICT (name) DO UPDATE SET data=EXCLUDED.data, size=EXCLUDED.size,
                                updated_at=EXCLUDED.updated_at""", (MAIN, data, len(data), _now()))
                # copie du jour : écrite une seule fois par jour (état au premier enregistrement de la journée).
                # Réécrire une copie complète à chaque modification remplissait la base de versions périmées.
                conn.execute("""INSERT INTO assetpilot_store(name, data, size, updated_at) VALUES (%s, %s, %s, %s)
                                ON CONFLICT (name) DO NOTHING""", (day, data, len(data), _now()))
                limit = MAIN + "@" + (dt.date.today() - dt.timedelta(days=DAILY_KEEP)).isoformat()
                conn.execute("DELETE FROM assetpilot_store WHERE name LIKE %s AND name < %s", (MAIN + "@%", limit))
            _state.update(saved_mtime=mtime, last_error=None, last_save=_now())
            return True
        except Exception as e:  # PostgreSQL momentanément injoignable : nouvel essai plus tard
            _state["last_error"] = "%s — %s" % (_now(), e)
            print("Enregistrement PostgreSQL impossible :", e)
            return False


def save_named(db_path, label):
    """Copie supplémentaire nommée (ex. « avant-restauration »), conservée jusqu'à la suivante du même nom."""
    if not enabled() or not os.path.exists(db_path):
        return
    data = _snapshot(db_path)
    with _connect() as conn:
        _ensure(conn)
        conn.execute("""INSERT INTO assetpilot_store(name, data, size, updated_at) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (name) DO UPDATE SET data=EXCLUDED.data, size=EXCLUDED.size,
                        updated_at=EXCLUDED.updated_at""", (MAIN + "@" + label, data, len(data), _now()))


def daily_copies():
    if not enabled():
        return []
    with _connect() as conn:
        _ensure(conn)
        rows = conn.execute("""SELECT name, size, updated_at FROM assetpilot_store WHERE name LIKE %s
                               ORDER BY name DESC""", (MAIN + "@%",)).fetchall()
    return [dict(day=r[0].split("@", 1)[1], size=r[1], updated_at=r[2]) for r in rows]


def read_copy(day):
    with _connect() as conn:
        row = conn.execute("SELECT data FROM assetpilot_store WHERE name=%s", (MAIN + "@" + day,)).fetchone()
    return bytes(row[0]) if row else None


def start_watch(db_path, every=60):
    """Filet de sécurité : enregistre toute modification qui n'aurait pas encore été envoyée."""
    if not enabled() or _state["thread"]:
        return
    _state["thread"] = True

    def loop():
        while True:
            time.sleep(every)
            try:
                if os.path.exists(db_path) and (os.path.getmtime(db_path) > _state["saved_mtime"] or _state["last_error"]):
                    save_db(db_path)
            except Exception as e:
                print("Surveillance PostgreSQL :", e)

    threading.Thread(target=loop, daemon=True, name="assetpilot-pg").start()


# --------------------------------------------------------------------------- photos
def save_file(rel_path, full_path):
    if not enabled():
        return
    try:
        with open(full_path, "rb") as f:
            data = f.read()
        with _connect() as conn:
            _ensure(conn)
            conn.execute("""INSERT INTO assetpilot_files(path, data, updated_at) VALUES (%s, %s, %s)
                            ON CONFLICT (path) DO UPDATE SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at""",
                         (rel_path, data, _now()))
    except Exception as e:
        print("Photo non enregistrée dans PostgreSQL :", e)


def delete_file(rel_path):
    if not enabled():
        return
    try:
        with _connect() as conn:
            _ensure(conn)
            conn.execute("DELETE FROM assetpilot_files WHERE path=%s", (rel_path,))
    except Exception as e:
        print("Suppression de la photo dans PostgreSQL impossible :", e)


def load_files(upload_dir):
    """Au démarrage : recopie les photos absentes du dossier local."""
    if not enabled():
        return 0
    os.makedirs(upload_dir, exist_ok=True)
    n = 0
    with _connect() as conn:
        _ensure(conn)
        names = [r[0] for r in conn.execute("SELECT path FROM assetpilot_files").fetchall()]
        for name in names:
            dest = os.path.join(upload_dir, os.path.basename(name))
            if os.path.exists(dest):
                continue
            row = conn.execute("SELECT data FROM assetpilot_files WHERE path=%s", (name,)).fetchone()
            if row:
                with open(dest, "wb") as f:
                    f.write(bytes(row[0]))
                n += 1
    return n


def read_file(rel_path):
    """Contenu d'une photo enregistrée dans PostgreSQL (None si absente)."""
    if not enabled():
        return None
    with _connect() as conn:
        _ensure(conn)
        row = conn.execute("SELECT data FROM assetpilot_files WHERE path=%s", (rel_path,)).fetchone()
    return bytes(row[0]) if row else None


def list_files():
    if not enabled():
        return []
    with _connect() as conn:
        _ensure(conn)
        return [r[0] for r in conn.execute("SELECT path FROM assetpilot_files").fetchall()]


def delete_files(paths):
    """Supprime plusieurs photos en une seule connexion. Retourne le nombre de lignes supprimées."""
    paths = list(paths)
    if not enabled() or not paths:
        return 0
    n = 0
    with _connect() as conn:
        _ensure(conn)
        for i in range(0, len(paths), 500):
            cur = conn.execute("DELETE FROM assetpilot_files WHERE path = ANY(%s)", (paths[i:i + 500],))
            n += cur.rowcount or 0
    return n


def usage():
    """Place occupée dans PostgreSQL (octets) : base entière, photos, copies de la base."""
    if not enabled():
        return None
    with _connect() as conn:
        _ensure(conn)
        r = conn.execute("""SELECT pg_database_size(current_database()), pg_total_relation_size('assetpilot_files'),
                                   pg_total_relation_size('assetpilot_store'), (SELECT COUNT(*) FROM assetpilot_files)""").fetchone()
    return dict(database=r[0], files=r[1], store=r[2], n_files=r[3])


def _autocommit():
    import psycopg
    return psycopg.connect(URL.replace("postgres://", "postgresql://", 1), connect_timeout=20, autocommit=True)


def vacuum(tables=("assetpilot_store", "assetpilot_files")):
    """VACUUM simple : la place des lignes supprimées est réutilisable (sans espace disque supplémentaire)."""
    if not enabled():
        return
    with _autocommit() as conn:
        for t in tables:
            try:
                conn.execute("VACUUM " + t)
            except Exception as e:
                print("VACUUM %s :" % t, e)


def compact(tables=("assetpilot_store", "assetpilot_files")):
    """Rend au disque la place libérée (VACUUM FULL). Il faut assez de place libre pour réécrire les données conservées :
    en cas d'échec, un VACUUM simple rend au moins la place réutilisable. Retourne la liste des erreurs."""
    if not enabled():
        return None
    errors = []
    with _autocommit() as conn:
        for t in tables:
            try:
                conn.execute("VACUUM FULL " + t)
            except Exception as e:
                errors.append("%s : %s" % (t, str(e).splitlines()[0]))
                try:
                    conn.execute("VACUUM " + t)
                except Exception:
                    pass
    return errors


def prune_copies(keep_days):
    """Ne garde que les copies journalières des « keep_days » derniers jours (et supprime les copies nommées)."""
    if not enabled():
        return 0
    limit = MAIN + "@" + (dt.date.today() - dt.timedelta(days=max(0, keep_days - 1))).isoformat()
    with _connect() as conn:
        _ensure(conn)
        cur = conn.execute("DELETE FROM assetpilot_store WHERE name LIKE %s AND (name < %s OR name NOT LIKE %s)",
                           (MAIN + "@%", limit, MAIN + "@____-__-__"))
        return cur.rowcount or 0


def write_file(rel_path, data):
    if not enabled():
        return
    with _connect() as conn:
        _ensure(conn)
        conn.execute("""INSERT INTO assetpilot_files(path, data, updated_at) VALUES (%s, %s, %s)
                        ON CONFLICT (path) DO UPDATE SET data=EXCLUDED.data, updated_at=EXCLUDED.updated_at""",
                     (rel_path, data, _now()))


def diagnostics():
    """Ce qui occupe la base PostgreSQL : plus grosses tables (toutes applications confondues), lignes mortes, WAL."""
    if not enabled():
        return None
    out = {"tables": [], "wal": None}
    with _connect() as conn:
        _ensure(conn)
        out["database"] = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        out["tables"] = [dict(name=r[0], size=r[1], live=r[2], dead=r[3]) for r in conn.execute("""
            SELECT c.relname, pg_total_relation_size(c.oid), COALESCE(s.n_live_tup, 0), COALESCE(s.n_dead_tup, 0)
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            LEFT JOIN pg_stat_user_tables s ON s.relid=c.oid
            WHERE c.relkind IN ('r','m') AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')
            ORDER BY 2 DESC LIMIT 15""").fetchall()]
        r = conn.execute("""SELECT COALESCE(SUM(octet_length(data)),0), COUNT(*) FROM assetpilot_store""").fetchone()
        out["store_live"], out["store_rows"] = r[0], r[1]
        r = conn.execute("""SELECT COALESCE(SUM(octet_length(data)),0), COUNT(*) FROM assetpilot_files""").fetchone()
        out["files_live"], out["files_rows"] = r[0], r[1]
        try:
            out["wal"] = conn.execute("SELECT COALESCE(SUM(size),0) FROM pg_ls_waldir()").fetchone()[0]
        except Exception:
            conn.rollback()
    return out
