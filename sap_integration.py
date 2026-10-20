# -*- coding: utf-8 -*-
"""
AssetPilot — Intégration SAP S/4HANA (avis de maintenance PM)

Principe : un TAG RCM ou Safety est validé par un chef d'équipe (qui possède une licence S/4HANA)
et devient un AVIS SAP créé EN SON NOM. Modes :

  - "fiori"      : AssetPilot ouvre l'application Fiori de création d'avis préremplie ; le chef
                   enregistre dans son Fiori (avec son identifiant et son mot de passe SAP) puis
                   reporte le N° d'avis (ou il est retrouvé par la synchronisation si la lecture API est active).
  - "api"        : le chef relie une fois son compte SAP (OAuth 2.0 de la passerelle SAP, transaction SOAUTH2) ;
                   AssetPilot crée ensuite l'avis directement en son nom via le service OData standard
                   API_MAINTNOTIFICATION. Aucun mot de passe SAP n'est stocké.

La synchronisation (mode "api") récupère le N° d'OT et la phase de l'avis :
  - OT créé dans SAP         → N° OT rempli, TAG « En cours »
  - avis clôturé dans SAP    → TAG « Traité »
"""
import json
import secrets
import threading
import time
import datetime as dt
import urllib.parse

from flask import render_template, request, redirect, url_for, flash, session, abort, g, jsonify

try:
    import requests
except ImportError:  # le mode "api" a besoin de requests (pip install requests)
    requests = None

H = {}  # fonctions partagées par app.py (q, ex, now, can, is_admin, load_tag, scope_sga_ids, in_clause, ...)

SAP_TYPES = ("RCM", "SAFETY")

SAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS sap_config(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sap_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, tag_id INTEGER, user_id INTEGER,
    action TEXT, ok INTEGER, message TEXT);
CREATE TABLE IF NOT EXISTS sap_tokens(
    user_id INTEGER PRIMARY KEY, access_token TEXT, refresh_token TEXT, expires_at REAL, sap_user TEXT, linked_at TEXT);
"""
TAG_COLUMNS = [("validated_by", "INTEGER"), ("validated_at", "TEXT"), ("sap_notif", "TEXT"), ("sap_order", "TEXT"),
               ("sap_status", "TEXT"), ("sap_created_by", "TEXT"), ("sap_sent_at", "TEXT"), ("sap_synced_at", "TEXT")]
USER_COLUMNS = [("sap_user", "TEXT")]

DEFAULTS = {
    "mode": "off",
    "fiori_url": "",
    "fiori_create_tpl": "{fiori}#MaintenanceNotification-create?TechnicalObject={equipment}&NotificationType={type}&NotificationText={text}",
    "fiori_display_tpl": "{fiori}#MaintenanceNotification-displayFactSheet?MaintenanceNotification={notif}",
    "api_base": "",
    "service_path": "/sap/opu/odata/sap/API_MAINTNOTIFICATION",
    "notif_type_rcm": "M2",
    "notif_type_safety": "M1",
    "prio_rcm": "2",
    "prio_safety": "1",
    "work_center": "",
    "plant": "",
    "oauth_authorize": "/sap/bc/sec/oauth2/authorize",
    "oauth_token": "/sap/bc/sec/oauth2/token",
    "oauth_client_id": "",
    "oauth_client_secret": "",
    "oauth_scope": "API_MAINTNOTIFICATION_0001",
    "sap_client": "",
    "verify_tls": "1",
    "sync_minutes": "15",
    "last_sync": "",
}
MODES = [("off", "Désactivée"),
         ("fiori", "Fiori prérempli — le chef enregistre l'avis dans son Fiori"),
         ("api", "API directe — compte SAP du chef relié une fois (OAuth)")]
PHASES = {"1": "Avis en attente", "2": "Avis reporté", "3": "Avis en cours", "4": "Avis clôturé"}


# ---------------------------------------------------------------------------
# Base de données
# ---------------------------------------------------------------------------
def migrate(conn):
    """Ajoute les tables / colonnes SAP sans toucher aux données existantes."""
    conn.executescript(SAP_SCHEMA)
    for table, cols in (("tags", TAG_COLUMNS), ("users", USER_COLUMNS)):
        existing = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
        for name, typ in cols:
            if name not in existing:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, typ))
    for k, v in DEFAULTS.items():
        conn.execute("INSERT OR IGNORE INTO sap_config(key,value) VALUES(?,?)", (k, v))
    conn.commit()


def cfg(key=None):
    rows = {r["key"]: r["value"] for r in H["q"]("SELECT key, value FROM sap_config")}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in rows.items() if v is not None})
    return merged[key] if key else merged


def set_cfg(values):
    for k, v in values.items():
        H["ex"]("INSERT OR REPLACE INTO sap_config(key,value) VALUES(?,?)", (k, v))


def log(tag_id, action, ok, message, user_id=None):
    if not ok and tag_id:
        last = H["q"]("SELECT ok, message FROM sap_log WHERE tag_id=? ORDER BY id DESC LIMIT 1", (tag_id,), one=True)
        if last and not last["ok"] and last["message"] == message:
            return  # même erreur déjà journalisée : pas de doublon
    H["ex"]("INSERT INTO sap_log(at,tag_id,user_id,action,ok,message) VALUES(?,?,?,?,?,?)",
            (H["now"](), tag_id, user_id if user_id is not None else (g.user["id"] if g.get("user") else None),
             action, 1 if ok else 0, message))


def mode():
    return cfg("mode")


def enabled():
    return mode() != "off"


# ---------------------------------------------------------------------------
# Contenu de l'avis
# ---------------------------------------------------------------------------
def notif_payload(t, chef):
    c = cfg()
    is_safety = t["type"] == "SAFETY"
    short = ("%s %s" % (t["tag_code"], t["description"]))[:40]
    long_text = "\n".join([
        "%s – TAG %s (AssetPilot)" % (t["tag_code"], "Safety" if is_safety else "RCM"),
        "Type d'anomalie : %s" % (t["anomaly_type"] or ""),
        "Machine : %s %s" % (t["eq_ref"] or "", t["eq_name"] or ""),
        "Organe : %s" % (t["organe"] or "—"),
        "Description : %s" % t["description"],
        "Anomalie découverte par : %s le %s" % (t["observed_name"] or "", t["observed_at"] or ""),
        "Confirmé par : %s" % chef["name"],
    ])
    payload = {
        "NotificationType": c["notif_type_safety"] if is_safety else c["notif_type_rcm"],
        "NotificationText": short,
        "MaintNotificationLongText": long_text,
        "TechnicalObject": t["eq_ref"] or "",
        "TechObjIsEquipOrFuncnlLoc": "EAMS_EQUI",
        "MaintPriority": c["prio_safety"] if is_safety else c["prio_rcm"],
    }
    if chef["sap_user"]:
        payload["ReportedByUser"] = chef["sap_user"]
    if c["work_center"]:
        payload["MainWorkCenter"] = c["work_center"]
    if c["plant"]:
        payload["MaintenancePlanningPlant"] = c["plant"]
    return payload


def fiori_create_url(t, chef):
    c = cfg()
    if not c["fiori_url"]:
        return ""
    p = notif_payload(t, chef)
    enc = lambda v: urllib.parse.quote(str(v or ""), safe="")
    return c["fiori_create_tpl"].format(fiori=c["fiori_url"].rstrip("/"), equipment=enc(p["TechnicalObject"]),
                                        type=enc(p["NotificationType"]), text=enc(p["NotificationText"]),
                                        priority=enc(p["MaintPriority"]), tag=enc(t["tag_code"]))


def fiori_display_url(notif):
    c = cfg()
    if not c["fiori_url"] or not notif:
        return ""
    return c["fiori_display_tpl"].format(fiori=c["fiori_url"].rstrip("/"), notif=urllib.parse.quote(str(notif)))


# ---------------------------------------------------------------------------
# Client OData (mode "api")
# ---------------------------------------------------------------------------
class SapError(Exception):
    pass


def _url(path):
    c = cfg()
    return c["api_base"].rstrip("/") + path


def _params():
    c = cfg()
    p = {"$format": "json"}
    if c["sap_client"]:
        p["sap-client"] = c["sap_client"]
    return p


def _token_for(user_id):
    tok = H["q"]("SELECT * FROM sap_tokens WHERE user_id=?", (user_id,), one=True)
    if not tok:
        return None
    if tok["expires_at"] and tok["expires_at"] < time.time() + 60 and tok["refresh_token"]:
        try:
            return _refresh(user_id, tok["refresh_token"])
        except SapError:
            return None
    return tok["access_token"]


def _token_request(data):
    if requests is None:
        raise SapError("Le module Python « requests » est absent (pip install requests).")
    c = cfg()
    try:
        r = requests.post(_url(c["oauth_token"]), data=data, auth=(c["oauth_client_id"], c["oauth_client_secret"]),
                          params={"sap-client": c["sap_client"]} if c["sap_client"] else None,
                          verify=c["verify_tls"] == "1", timeout=20)
    except requests.RequestException as e:
        raise SapError("SAP injoignable : %s" % e)
    if r.status_code != 200:
        raise SapError("Jeton refusé par SAP (%s) : %s" % (r.status_code, r.text[:200]))
    return r.json()


def _store_token(user_id, j):
    H["ex"]("""INSERT OR REPLACE INTO sap_tokens(user_id,access_token,refresh_token,expires_at,sap_user,linked_at)
             VALUES(?,?,?,?,COALESCE((SELECT sap_user FROM users WHERE id=?),''),?)""",
            (user_id, j.get("access_token"), j.get("refresh_token"), time.time() + float(j.get("expires_in") or 3600),
             user_id, H["now"]()))
    return j.get("access_token")


def _refresh(user_id, refresh_token):
    j = _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
    return _store_token(user_id, j)


def _session(token):
    if requests is None:
        raise SapError("Le module Python « requests » est absent (pip install requests).")
    s = requests.Session()
    s.verify = cfg("verify_tls") == "1"
    s.headers.update({"Authorization": "Bearer " + token, "Accept": "application/json"})
    return s


def api_create(t, chef):
    token = _token_for(chef["id"])
    if not token:
        raise SapError("Compte SAP non relié")
    s = _session(token)
    url = _url(cfg("service_path") + "/MaintenanceNotification")
    try:
        r = s.get(url, params=dict(_params(), **{"$top": "0"}), headers={"x-csrf-token": "Fetch"}, timeout=20)
        csrf = r.headers.get("x-csrf-token")
        if r.status_code in (401, 403):
            raise SapError("Accès refusé par SAP (%s) : reliez à nouveau votre compte SAP." % r.status_code)
        r = s.post(url, params={k: v for k, v in _params().items() if k != "$format"}, json=notif_payload(t, chef),
                   headers={"x-csrf-token": csrf or "", "Content-Type": "application/json"}, timeout=30)
    except requests.RequestException as e:
        raise SapError("SAP injoignable : %s" % e)
    if r.status_code not in (200, 201):
        try:
            msg = r.json()["error"]["message"]["value"]
        except Exception:
            msg = r.text[:300]
        raise SapError("Création refusée par SAP (%s) : %s" % (r.status_code, msg))
    d = r.json().get("d", r.json())
    return str(d.get("MaintenanceNotification") or "").lstrip("0") or d.get("MaintenanceNotification")


def api_read(notif, token):
    s = _session(token)
    url = _url(cfg("service_path") + "/MaintenanceNotification('%s')" % notif)
    try:
        r = s.get(url, params=_params(), timeout=20)
    except requests.RequestException as e:
        raise SapError("SAP injoignable : %s" % e)
    if r.status_code != 200:
        raise SapError("Lecture de l'avis %s impossible (%s)" % (notif, r.status_code))
    d = r.json().get("d", r.json())
    return {"order": (d.get("MaintenanceOrder") or "").lstrip("0"), "phase": str(d.get("NotifProcessingPhase") or "")}


# ---------------------------------------------------------------------------
# Création et synchronisation
# ---------------------------------------------------------------------------
def apply_sap_state(t, order, phase, user_id=None):
    """Répercute le N° d'OT et la phase de l'avis SAP sur le TAG."""
    q, ex, now = H["q"], H["ex"], H["now"]
    changes = []
    ex("UPDATE tags SET sap_synced_at=?, sap_status=? WHERE id=?", (now(), PHASES.get(phase, t["sap_status"] or ""), t["id"]))
    if order and order != (t["sap_order"] or ""):
        ex("UPDATE tags SET sap_order=?, ot_number=? WHERE id=?", (order, order, t["id"]))
        changes.append("OT %s" % order)
        if t["status"] == "Nouveau":
            ex("UPDATE tags SET status='En cours', treatment_start=COALESCE(treatment_start, ?), updated_at=? WHERE id=?",
               (now(), now(), t["id"]))
            ex("INSERT INTO tag_history(tag_id,user_id,from_status,to_status,comment,at) VALUES(?,?,?,?,?,?)",
               (t["id"], user_id, "Nouveau", "En cours", "OT %s créé dans SAP" % order, now()))
    if phase == "4" and t["status"] in ("Nouveau", "En cours"):
        ex("UPDATE tags SET status='Traité', resolved_at=?, updated_at=?, treatment_start=COALESCE(treatment_start, ?) WHERE id=?",
           (now(), now(), now(), t["id"]))
        ex("INSERT INTO tag_history(tag_id,user_id,from_status,to_status,comment,at) VALUES(?,?,?,?,?,?)",
           (t["id"], user_id, t["status"], "Traité", "Avis clôturé dans SAP", now()))
        changes.append("avis clôturé → TAG traité")
    return changes


def sync_all(user_id=None):
    """Met à jour tous les TAGs ayant un avis SAP non clôturé. Renvoie (nb mis à jour, nb erreurs)."""
    q = H["q"]
    m = mode()
    rows = q("""SELECT * FROM tags WHERE sap_notif IS NOT NULL AND sap_notif!='' AND status IN ('Nouveau','En cours')""")
    updated = errors = 0
    for t in rows:
        try:
            if m == "api":
                token = _token_for(t["validated_by"]) if t["validated_by"] else None
                if not token:  # lecture possible avec le compte de n'importe quel chef relié
                    other = H["q"]("SELECT user_id FROM sap_tokens ORDER BY linked_at DESC LIMIT 1", one=True)
                    token = _token_for(other["user_id"]) if other else None
                if not token:
                    raise SapError("Aucun compte SAP relié : lecture impossible")
                st = api_read(t["sap_notif"], token)
                order, phase = st["order"], st["phase"]
            else:
                continue
            ch = apply_sap_state(t, order, phase, user_id)
            if ch:
                updated += 1
                log(t["id"], "Synchronisation", True, "Avis %s : %s" % (t["sap_notif"], ", ".join(ch)), user_id)
        except SapError as e:
            errors += 1
            log(t["id"], "Synchronisation", False, str(e), user_id)
    H["ex"]("INSERT OR REPLACE INTO sap_config(key,value) VALUES('last_sync',?)", (H["now"](),))
    return updated, errors


def create_notification(t, chef):
    """Crée l'avis selon le mode. Renvoie (statut, info) : ('ok', n°) | ('fiori', url) | ('link', None) | ('error', msg)."""
    m = mode()
    ex, now = H["ex"], H["now"]
    if m == "fiori":
        url = fiori_create_url(t, chef)
        log(t["id"], "Ouverture Fiori", True, "Création de l'avis à faire dans Fiori par %s" % (chef["sap_user"] or chef["name"]))
        return "fiori", url
    if m == "api":
        if not _token_for(chef["id"]):
            return "link", None
        try:
            notif = api_create(t, chef)
        except SapError as e:
            log(t["id"], "Création avis", False, str(e))
            return "error", str(e)
        ex("UPDATE tags SET sap_notif=?, sap_status=?, sap_created_by=?, sap_sent_at=? WHERE id=?",
           (notif, PHASES["1"], chef["sap_user"] or "", now(), t["id"]))
        log(t["id"], "Création avis", True, "Avis %s créé dans SAP par %s" % (notif, chef["sap_user"] or chef["name"]))
        return "ok", notif
    return "error", "Intégration SAP désactivée"


def pending_validation(user):
    """TAGs RCM / Safety nouveaux, pas encore validés, dans le périmètre du chef."""
    clause, args = H["in_clause"]("t.sga_id", H["scope_sga_ids"](user))
    return H["q"]("""SELECT t.*, e.name eq_name, e.reference eq_ref, ob.name observed_name FROM tags t
                     JOIN equipment e ON e.id=t.equipment_id LEFT JOIN users ob ON ob.id=t.observed_by
                     WHERE """ + clause + """ AND t.type IN ('RCM','SAFETY') AND t.validated_at IS NULL
                     AND (t.sap_notif IS NULL OR t.sap_notif='') ORDER BY t.created_at""", args)


_sync_thread = {"started": False}


def _background_sync(app):
    while True:
        try:
            minutes = 15
            with app.app_context():
                if mode() == "api":
                    minutes = max(5, int(cfg("sync_minutes") or 15))
                    sync_all()
        except Exception as e:  # ne jamais arrêter la boucle
            print("Synchronisation SAP :", e)
        time.sleep(minutes * 60)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def register(app, helpers):
    H.update(helpers)
    require = H["require"]

    @app.context_processor
    def sap_ctx():
        try:
            m = mode()
        except Exception:
            m = "off"
        return dict(sap_mode=m, sap_enabled=m != "off", sap_fiori_display=fiori_display_url, SAP_TYPES=SAP_TYPES)

    @app.before_request
    def start_sync_thread():
        if not _sync_thread["started"]:
            _sync_thread["started"] = True
            threading.Thread(target=_background_sync, args=(app,), daemon=True).start()

    @app.route("/tags/<int:tag_id>/sap/validate", methods=["POST"])
    @require("confirm")
    def sap_validate(tag_id):
        t = H["load_tag"](tag_id)
        if t["type"] not in SAP_TYPES or t["sap_notif"]:
            flash("Ce TAG n'est pas concerné ou a déjà un avis SAP.", "error")
            return redirect(request.referrer or url_for("home"))
        ids = H["scope_sga_ids"]()
        if not (H["is_admin"]() or ids is None or t["sga_id"] in ids):
            abort(403)
        chef = g.user
        H["ex"]("UPDATE tags SET validated_by=?, validated_at=COALESCE(validated_at, ?) WHERE id=?", (chef["id"], H["now"](), tag_id))
        H["ex"]("INSERT INTO tag_history(tag_id,user_id,from_status,to_status,comment,at) VALUES(?,?,?,?,?,?)",
                (tag_id, chef["id"], t["status"], t["status"], "TAG validé pour SAP par %s" % chef["name"], H["now"]()))
        if not enabled():
            flash("%s validé." % t["tag_code"], "success")
            return redirect(request.referrer or url_for("home"))
        t = H["load_tag"](tag_id)
        status, info = create_notification(t, chef)
        if status == "ok":
            flash("%s validé : avis SAP %s créé au nom de %s." % (t["tag_code"], info, chef["sap_user"] or chef["name"]), "success")
            return redirect(request.referrer or url_for("home"))
        if status == "fiori":
            return redirect(url_for("tag_detail", tag_id=tag_id, fiori=1))
        if status == "link":
            session["sap_after_link"] = url_for("sap_validate_resume", tag_id=tag_id)
            return redirect(url_for("sap_link"))
        flash("Avis non créé : %s. Le TAG reste validé, vous pouvez réessayer depuis l'étiquette." % info, "error")
        return redirect(url_for("tag_detail", tag_id=tag_id))

    @app.route("/tags/<int:tag_id>/sap/resume")
    @require("confirm")
    def sap_validate_resume(tag_id):
        t = H["load_tag"](tag_id)
        if t["sap_notif"]:
            return redirect(url_for("tag_detail", tag_id=tag_id))
        status, info = create_notification(t, g.user)
        if status == "ok":
            flash("Avis SAP %s créé au nom de %s." % (info, g.user["sap_user"] or g.user["name"]), "success")
        elif status == "fiori":
            return redirect(url_for("tag_detail", tag_id=tag_id, fiori=1))
        else:
            flash("Avis non créé : %s" % (info or "compte SAP non relié"), "error")
        return redirect(url_for("tag_detail", tag_id=tag_id))

    @app.route("/tags/<int:tag_id>/sap/number", methods=["POST"])
    def sap_set_number(tag_id):
        if not (H["can"]("confirm") or H["is_admin"]()):
            abort(403)
        t = H["load_tag"](tag_id)
        notif = "".join(ch for ch in request.form.get("sap_notif", "") if ch.isalnum())
        order = "".join(ch for ch in request.form.get("sap_order", "") if ch.isalnum())
        if notif:
            H["ex"]("UPDATE tags SET sap_notif=?, sap_created_by=COALESCE(sap_created_by, ?), sap_sent_at=COALESCE(sap_sent_at, ?), "
                    "sap_status=COALESCE(sap_status, ?) WHERE id=?",
                    (notif, g.user["sap_user"] or g.user["name"], H["now"](), PHASES["1"], tag_id))
            log(tag_id, "Saisie N° avis", True, "Avis %s reporté par %s" % (notif, g.user["name"]))
        if order:
            H["ex"]("UPDATE tags SET sap_order=?, ot_number=? WHERE id=?", (order, order, tag_id))
            log(tag_id, "Saisie N° OT", True, "OT %s reporté par %s" % (order, g.user["name"]))
            if t["status"] == "Nouveau":
                H["ex"]("UPDATE tags SET status='En cours', treatment_start=COALESCE(treatment_start, ?), updated_at=? WHERE id=?",
                        (H["now"](), H["now"](), tag_id))
                H["ex"]("INSERT INTO tag_history(tag_id,user_id,from_status,to_status,comment,at) VALUES(?,?,?,?,?,?)",
                        (tag_id, g.user["id"], "Nouveau", "En cours", "OT %s créé dans SAP" % order, H["now"]()))
        flash("Références SAP enregistrées.", "success")
        return redirect(url_for("tag_detail", tag_id=tag_id))

    @app.route("/sap", methods=["GET", "POST"])
    @require("settings")
    def sap_settings():
        if request.method == "POST":
            keys = [k for k in DEFAULTS if k not in ("last_sync",)]
            vals = {}
            for k in keys:
                if k in ("oauth_client_secret",) and not request.form.get(k):
                    continue  # ne pas effacer le secret si le champ est laissé vide
                if k in request.form:
                    vals[k] = request.form.get(k, "").strip()
            if "verify_tls" not in request.form:
                vals["verify_tls"] = "0" if request.form.get("_has_verify") else cfg("verify_tls")
            set_cfg(vals)
            flash("Paramètres SAP enregistrés.", "success")
            return redirect(url_for("sap_settings"))
        logs = H["q"]("""SELECT l.*, u.name user_name, t.tag_code FROM sap_log l LEFT JOIN users u ON u.id=l.user_id
                         LEFT JOIN tags t ON t.id=l.tag_id ORDER BY l.id DESC LIMIT 80""")
        chefs = H["q"]("""SELECT u.*, k.linked_at FROM users u LEFT JOIN sap_tokens k ON k.user_id=u.id
                          WHERE u.active=1 AND (u.role IN (?,?,?) OR u.permissions LIKE '%"confirm"%') ORDER BY u.role, u.name""",
                       ("Chef d'équipe Production", "Chef d'équipe Maintenance", "Administrateur"))
        stats = H["q"]("""SELECT COUNT(*) n, SUM(CASE WHEN sap_notif IS NOT NULL AND sap_notif!='' THEN 1 ELSE 0 END) sent,
                          SUM(CASE WHEN sap_order IS NOT NULL AND sap_order!='' THEN 1 ELSE 0 END) orders
                          FROM tags WHERE type IN ('RCM','SAFETY')""", one=True)
        c = cfg()
        c["oauth_client_secret"] = "••••••" if c["oauth_client_secret"] else ""
        return render_template("sap_settings.html", c=c, modes=MODES, logs=logs, chefs=chefs, stats=stats,
                               redirect_uri=url_for("sap_callback", _external=True))

    @app.route("/sap/sync", methods=["POST"])
    def sap_sync_now():
        if not (H["can"]("settings") or H["can"]("confirm")):
            abort(403)
        if mode() != "api":
            flash("La synchronisation automatique n'est disponible qu'en mode API.", "error")
        else:
            up, err = sync_all(g.user["id"])
            flash("Synchronisation SAP : %d TAG(s) mis à jour%s." % (up, (", %d erreur(s) — voir le journal" % err) if err else ""),
                  "success" if not err else "error")
        return redirect(request.referrer or url_for("sap_settings"))

    @app.route("/sap/link")
    def sap_link():
        c = cfg()
        if mode() != "api":
            flash("La liaison de compte SAP n'est utile qu'en mode API.", "error")
            return redirect(url_for("home"))
        if not c["api_base"] or not c["oauth_client_id"]:
            flash("L'intégration SAP n'est pas encore paramétrée (adresse SAP / client OAuth). Contactez l'administrateur.", "error")
            return redirect(url_for("home"))
        state = secrets.token_urlsafe(16)
        session["sap_oauth_state"] = state
        params = {"response_type": "code", "client_id": c["oauth_client_id"], "scope": c["oauth_scope"],
                  "redirect_uri": url_for("sap_callback", _external=True), "state": state}
        if c["sap_client"]:
            params["sap-client"] = c["sap_client"]
        return redirect(_url(c["oauth_authorize"]) + "?" + urllib.parse.urlencode(params))

    @app.route("/sap/callback")
    def sap_callback():
        if request.args.get("state") != session.pop("sap_oauth_state", None):
            flash("Liaison SAP refusée (session expirée). Recommencez.", "error")
            return redirect(url_for("home"))
        if request.args.get("error") or not request.args.get("code"):
            flash("Liaison SAP annulée : %s" % request.args.get("error_description", request.args.get("error", "")), "error")
            return redirect(url_for("home"))
        try:
            j = _token_request({"grant_type": "authorization_code", "code": request.args["code"],
                                "redirect_uri": url_for("sap_callback", _external=True)})
            _store_token(g.user["id"], j)
            log(None, "Liaison compte SAP", True, "Compte SAP relié par %s" % g.user["name"])
            flash("Votre compte SAP est relié : les avis seront créés à votre nom.", "success")
        except SapError as e:
            log(None, "Liaison compte SAP", False, str(e))
            flash(str(e), "error")
        return redirect(session.pop("sap_after_link", None) or url_for("home"))

    @app.route("/sap/unlink", methods=["POST"])
    def sap_unlink():
        H["ex"]("DELETE FROM sap_tokens WHERE user_id=?", (g.user["id"],))
        flash("Compte SAP délié.", "success")
        return redirect(request.referrer or url_for("home"))

    @app.route("/sap/test", methods=["POST"])
    @require("settings")
    def sap_test():
        if mode() != "api":
            flash("Le test de connexion concerne le mode API.", "error")
            return redirect(url_for("sap_settings"))
        token = _token_for(g.user["id"])
        if not token:
            flash("Reliez d'abord votre propre compte SAP pour tester la connexion.", "error")
            return redirect(url_for("sap_settings"))
        try:
            s = _session(token)
            r = s.get(_url(cfg("service_path") + "/MaintenanceNotification"), params=dict(_params(), **{"$top": "1"}), timeout=20)
            ok = r.status_code == 200
            flash("Connexion SAP %s (code %s)." % ("réussie" if ok else "en échec", r.status_code), "success" if ok else "error")
            log(None, "Test connexion", ok, "Code HTTP %s" % r.status_code)
        except Exception as e:
            flash("Connexion SAP impossible : %s" % e, "error")
            log(None, "Test connexion", False, str(e))
        return redirect(url_for("sap_settings"))

    return {"pending_validation": pending_validation, "fiori_create_url": fiori_create_url, "notif_payload": notif_payload,
            "cfg": cfg, "has_token": lambda uid: bool(H["q"]("SELECT 1 FROM sap_tokens WHERE user_id=?", (uid,), one=True))}
