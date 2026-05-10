from __future__ import annotations

from os import getenv
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from flask import Flask, Response, abort, redirect, render_template, request, send_from_directory, session, url_for

ROOT_DIR = Path(__file__).resolve().parents[1]
SCOUT_DIR = ROOT_DIR / "scout"

# Reuse existing env values when available.
load_dotenv(ROOT_DIR / "api" / ".env")
load_dotenv(ROOT_DIR / ".env")

API_BASE_URL = getenv("FASTAPI_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
POWERBI_EMBED_URL = "https://app.powerbi.com/reportEmbed?reportId=ef57a05c-c3cb-45f2-868b-c64fbafea4b3&appId=42582a56-2181-4b9b-84d5-272aece5a79e&autoAuth=true&ctid=604f1a96-cbe8-43f8-abbf-f8eaf5d85730"



app = Flask(__name__, template_folder="templates", static_folder=None)
app.secret_key = getenv("FLASK_SECRET_KEY", getenv("APP_SESSION_SECRET", "change-me-please"))

PROTECTED_PROXY_PREFIXES = (
    "chatbot",
    "client",
    "admin",
    "events",
    "models",
    "predict",
    "forecast",
    "validate-matricule",
    "api/me",
    "api/notifications",
    "api/favorites",
    "api/messenger",
)


def _session_bridge_url(token: str, next_path: str = "/chatbot") -> str:
    safe_token = quote(token, safe="")
    safe_next = quote(next_path, safe="/")
    return f"{API_BASE_URL}/session-login?token={safe_token}&next={safe_next}"


def _api_headers() -> dict[str, str]:
    token = session.get("api_token")
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _proxy_to_api(path: str) -> Any:
    normalized_path = path.lstrip("/")
    protected = any(
        normalized_path == prefix or normalized_path.startswith(f"{prefix}/")
        for prefix in PROTECTED_PROXY_PREFIXES
    )
    if protected and not _require_login():
        return {"detail": "Authentification requise"}, 401

    target = f"{API_BASE_URL}/{normalized_path}"
    headers: dict[str, str] = {}
    session_auth = _api_headers().get("Authorization")
    incoming_auth = request.headers.get("Authorization", "").strip()
    if session_auth:
        headers["Authorization"] = session_auth
    elif incoming_auth:
        headers["Authorization"] = incoming_auth

    content_type = request.headers.get("Content-Type")
    if content_type:
        headers["Content-Type"] = content_type

    try:
        resp = requests.request(
            method=request.method,
            url=target,
            params=request.args,
            data=request.get_data(),
            headers=headers,
            allow_redirects=False,
            timeout=30,
        )
    except requests.RequestException:
        return {"detail": "API indisponible"}, 503

    excluded = {
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
    }
    response_headers = []
    for key, value in resp.headers.items():
        if key.lower() in excluded:
            continue
        if key.lower() == "location" and value.startswith(API_BASE_URL):
            value = value[len(API_BASE_URL):] or "/"
        response_headers.append((key, value))

    return Response(resp.content, status=resp.status_code, headers=response_headers)


def _require_login() -> bool:
    return bool(session.get("api_token"))


def _resolve_scout_path(page: str) -> Path | None:
    clean = page.strip().lstrip("/")
    if not clean:
        return SCOUT_DIR / "index.html"
    direct = SCOUT_DIR / clean
    if direct.exists() and direct.is_file():
        return direct
    html_fallback = SCOUT_DIR / f"{clean}.html"
    if html_fallback.exists() and html_fallback.is_file():
        return html_fallback
    return None


def _json_proxy_response(resp: requests.Response) -> tuple[Any, int]:
    try:
        return resp.json(), resp.status_code
    except ValueError:
        return {"detail": resp.text or "Erreur API"}, resp.status_code


def _fetch_api_json(path: str) -> tuple[dict[str, Any] | None, str | None, int | None]:
    try:
        resp = requests.get(
            f"{API_BASE_URL}{path}",
            headers=_api_headers(),
            timeout=15,
        )
    except requests.RequestException:
        return None, "API indisponible.", None

    if resp.status_code == 401:
        return None, "Session expiree.", 401
    if resp.status_code >= 400:
        return None, f"Erreur API ({resp.status_code}).", resp.status_code

    try:
        return resp.json(), None, resp.status_code
    except ValueError:
        return None, "Reponse API invalide.", resp.status_code


@app.get("/")
def root() -> Any:
    return redirect(url_for("front_index"))


@app.get("/front/")
def front_index() -> Any:
    return send_from_directory(SCOUT_DIR, "index.html")


@app.get("/front/<path:page>")
def front_pages(page: str) -> Any:
    target = _resolve_scout_path(page)
    if target is not None:
        rel = target.relative_to(SCOUT_DIR).as_posix()
        return send_from_directory(SCOUT_DIR, rel)
    # Fallback to FastAPI while preserving origin on :5000.
    return _proxy_to_api(f"front/{page}")


@app.get("/public/events")
def public_events_proxy() -> Any:
    try:
        resp = requests.get(
            f"{API_BASE_URL}/public/events",
            headers=_api_headers(),
            timeout=15,
        )
    except requests.RequestException:
        return {"detail": "API indisponible"}, 503
    return _json_proxy_response(resp)


@app.get("/chatbot")
def chatbot_proxy() -> Any:
    if not _require_login():
        return redirect(url_for("login"))
    return _proxy_to_api("chatbot")


@app.get("/client")
def client_proxy() -> Any:
    if not _require_login():
        return redirect(url_for("login"))
    return _proxy_to_api("client")


@app.get("/admin")
def admin_proxy() -> Any:
    if not _require_login():
        return redirect(url_for("login"))
    return _proxy_to_api("admin")


@app.get("/events")
def events_proxy() -> Any:
    if not _require_login():
        return redirect(url_for("login"))
    return _proxy_to_api("events")


@app.get("/admin/ratings")
def admin_ratings_proxy() -> Any:
    if not _require_login():
        return redirect(url_for("login"))
    return _proxy_to_api("admin/ratings")




@app.get("/admin/bi-dashboard")
def admin_bi_dashboard_proxy() -> Any:
    if not _require_login():
        return redirect(url_for("login"))

    me_data, me_error, status_code = _fetch_api_json("/api/me")

    if status_code == 401:
        session.clear()
        return redirect(url_for("login"))

    return render_template(
        "admin_bi_dashboard.html",
        powerbi_url=POWERBI_EMBED_URL,
        user_email=(me_data or {}).get("email", ""),
        username=(me_data or {}).get("username", session.get("username", "Utilisateur")),
        role=(me_data or {}).get("role", "user"),
    )

@app.get("/admin/face-setup")
def admin_face_setup_proxy() -> Any:
    if not _require_login():
        return redirect(url_for("login"))
    return _proxy_to_api("admin/face-setup")


@app.get("/admin/events")
def admin_events_list_proxy() -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api("admin/events")


@app.post("/admin/events")
def admin_events_create_proxy() -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api("admin/events")


@app.post("/admin/events/upload")
def admin_events_upload_proxy() -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api("admin/events/upload")


@app.post("/admin/events/weather-preview")
def admin_events_weather_preview_proxy() -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api("admin/events/weather-preview")


@app.patch("/admin/events/<int:event_id>")
def admin_events_update_proxy(event_id: int) -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api(f"admin/events/{event_id}")


@app.post("/admin/events/<int:event_id>/upload")
def admin_events_update_with_image_proxy(event_id: int) -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api(f"admin/events/{event_id}/upload")


@app.delete("/admin/events/<int:event_id>")
def admin_events_delete_proxy(event_id: int) -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api(f"admin/events/{event_id}")


@app.get("/admin/events/<int:event_id>/ratings")
def admin_events_ratings_proxy(event_id: int) -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api(f"admin/events/{event_id}/ratings")


@app.get("/admin/events/<int:event_id>/favorites")
def admin_events_favorites_proxy(event_id: int) -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api(f"admin/events/{event_id}/favorites")


@app.post("/public/events/<int:event_id>/rating")
def public_event_rating_proxy(event_id: int) -> Any:
    payload = request.get_json(silent=True) or {}
    try:
        resp = requests.post(
            f"{API_BASE_URL}/public/events/{event_id}/rating",
            headers={**_api_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
    except requests.RequestException:
        return {"detail": "API indisponible"}, 503
    return _json_proxy_response(resp)


@app.get("/public/events/<int:event_id>/comments")
def public_event_comments_proxy(event_id: int) -> Any:
    try:
        resp = requests.get(
            f"{API_BASE_URL}/public/events/{event_id}/comments",
            headers=_api_headers(),
            timeout=15,
        )
    except requests.RequestException:
        return {"detail": "API indisponible"}, 503
    return _json_proxy_response(resp)


@app.post("/public/events/<int:event_id>/comments")
def public_add_event_comment_proxy(event_id: int) -> Any:
    payload = request.get_json(silent=True) or {}
    try:
        resp = requests.post(
            f"{API_BASE_URL}/public/events/{event_id}/comments",
            headers={**_api_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
    except requests.RequestException:
        return {"detail": "API indisponible"}, 503
    return _json_proxy_response(resp)


@app.post("/public/comments/<int:comment_id>/reaction")
def public_comment_reaction_proxy(comment_id: int) -> Any:
    payload = request.get_json(silent=True) or {}
    try:
        resp = requests.post(
            f"{API_BASE_URL}/public/comments/{comment_id}/reaction",
            headers={**_api_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
    except requests.RequestException:
        return {"detail": "API indisponible"}, 503
    return _json_proxy_response(resp)


@app.post("/public/events/<int:event_id>/favorite")
def public_event_favorite_proxy(event_id: int) -> Any:
    payload = request.get_json(silent=True) or {}
    try:
        resp = requests.post(
            f"{API_BASE_URL}/public/events/{event_id}/favorite",
            headers={**_api_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
    except requests.RequestException:
        return {"detail": "API indisponible"}, 503
    return _json_proxy_response(resp)


@app.get("/api/messenger/access-status")
def messenger_access_status_proxy() -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api("api/messenger/access-status")


@app.post("/api/messenger/access-request")
def messenger_access_request_proxy() -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api("api/messenger/access-request")


@app.get("/api/messenger/requests")
def messenger_requests_list_proxy() -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api("api/messenger/requests")


@app.post("/api/messenger/requests/<int:target_user_id>/review")
def messenger_request_review_proxy(target_user_id: int) -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api(f"api/messenger/requests/{target_user_id}/review")


@app.get("/api/messenger/messages")
def messenger_messages_list_proxy() -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api("api/messenger/messages")


@app.post("/api/messenger/messages")
def messenger_messages_create_proxy() -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api("api/messenger/messages")


@app.get("/api/messenger/analysis")
def messenger_analysis_proxy() -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api("api/messenger/analysis")


@app.post("/api/messenger/analysis/send-alert")
def messenger_analysis_alert_proxy() -> Any:
    if not _require_login():
        return {"detail": "Authentification requise"}, 401
    return _proxy_to_api("api/messenger/analysis/send-alert")


@app.route("/register", methods=["GET", "POST"])
def register() -> Any:
    """Proxy GET and POST /register to FastAPI (no auth required)."""
    if request.method == "POST":
        try:
            resp = requests.post(
                f"{API_BASE_URL}/register",
                data=request.form,
                allow_redirects=False,
                timeout=15,
            )
        except requests.RequestException:
            return "API indisponible", 503
        if resp.is_redirect:
            location = resp.headers.get("Location", "/register")
            return redirect(location, code=resp.status_code)
        return resp.text, resp.status_code, {"Content-Type": "text/html; charset=utf-8"}
    else:
        params = dict(request.args)
        try:
            resp = requests.get(
                f"{API_BASE_URL}/register",
                params=params,
                cookies={"session": request.cookies.get("session", "")},
                allow_redirects=False,
                timeout=15,
            )
        except requests.RequestException:
            return "API indisponible", 503
        return resp.text, resp.status_code, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/login", methods=["GET", "POST"])
def login() -> Any:
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            error = "Identifiants requis."
        else:
            try:
                resp = requests.post(
                    f"{API_BASE_URL}/api/token",
                    json={"username": username, "password": password},
                    timeout=15,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    access_token = data.get("access_token", "")
                    session["api_token"] = access_token
                    session["username"] = username
                    me_resp = requests.get(
                        f"{API_BASE_URL}/api/me",
                        headers={"Authorization": f"Bearer {access_token}"},
                        timeout=15,
                    )
                    if me_resp.status_code == 200:
                        me = me_resp.json()
                        role = str(me.get("role", "user"))
                        session["role"] = role
                        return redirect(url_for("dashboard"))
                    return redirect(url_for("dashboard"))
                if resp.status_code == 401:
                    error = "Identifiants invalides ou compte non verifie."
                elif resp.status_code == 429:
                    error = "Trop de tentatives. Attendez quelques minutes puis reessayez."
                else:
                    error = f"Echec de connexion (code API {resp.status_code})."
            except requests.RequestException:
                error = "API indisponible. Verifiez que FastAPI est lancee sur le port 8000."

    return render_template("login.html", error=error)


@app.get("/dashboard")
def dashboard() -> Any:
    if not _require_login():
        return redirect(url_for("login"))
    access_token = str(session.get("api_token", ""))
    if not access_token:
        session.clear()
        return redirect(url_for("login"))

    me_data, me_error, status_code = _fetch_api_json("/api/me")
    if status_code == 401:
        session.clear()
        return redirect(url_for("login"))

    notifications_data, notifications_error, notifications_status = _fetch_api_json("/api/notifications")
    if notifications_status == 401:
        session.clear()
        return redirect(url_for("login"))

    favorites_data, favorites_error, favorites_status = _fetch_api_json("/api/favorites")
    if favorites_status == 401:
        session.clear()
        return redirect(url_for("login"))

    notifications = [] if notifications_data is None else list(notifications_data.get("notifications", []))
    favorites = [] if favorites_data is None else list(favorites_data.get("favorites", []))
    unread_count = sum(1 for item in notifications if not int(item.get("is_read", 0) or 0))

    return render_template(
        "dashboard.html",
        username=(me_data or {}).get("username", session.get("username", "Utilisateur")),
        role=(me_data or {}).get("role", "user"),
        notifications=notifications,
        favorites=favorites,
        unread_count=unread_count,
        api_error=me_error or notifications_error or favorites_error,
    )


@app.post("/dashboard/read-all")
def dashboard_read_all() -> Any:
    if not _require_login():
        return redirect(url_for("login"))
    try:
        requests.post(
            f"{API_BASE_URL}/api/notifications/read-all",
            headers=_api_headers(),
            timeout=15,
        )
    except requests.RequestException:
        pass
    return redirect(url_for("dashboard"))


@app.get("/logout")
def logout() -> Any:
    session.clear()
    return redirect(url_for("front_index"))


@app.get("/assets/<path:filename>")
def scout_assets(filename: str) -> Any:
    return send_from_directory(SCOUT_DIR / "assets", filename)


@app.get("/_next/<path:filename>")
def scout_next(filename: str) -> Any:
    return send_from_directory(SCOUT_DIR / "_next", filename)


@app.get("/front/assets/<path:filename>")
def scout_front_assets(filename: str) -> Any:
    return send_from_directory(SCOUT_DIR / "assets", filename)


@app.get("/front/_next/<path:filename>")
def scout_front_next(filename: str) -> Any:
    return send_from_directory(SCOUT_DIR / "_next", filename)


@app.get("/uploads/<path:filename>")
def uploads_proxy(filename: str) -> Any:
    uploads_dir = ROOT_DIR / "uploads"
    target = uploads_dir / filename
    if target.exists() and target.is_file():
        return send_from_directory(uploads_dir, filename)
    return redirect(f"{API_BASE_URL}/uploads/{filename}")


@app.get("/favicon.ico")
def favicon() -> Any:
    return send_from_directory(SCOUT_DIR, "favicon.ico")


@app.get("/<path:page>")
def scout_pages(page: str) -> Any:
    if page in {"login", "dashboard", "logout"}:
        abort(404)
    target = _resolve_scout_path(page)
    if target is not None:
        rel = target.relative_to(SCOUT_DIR).as_posix()
        return send_from_directory(SCOUT_DIR, rel)
    return _proxy_to_api(page)


@app.route("/<path:page>", methods=["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def proxy_non_get(page: str) -> Any:
    if page in {"login", "dashboard", "logout"}:
        abort(404)
    return _proxy_to_api(page)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
