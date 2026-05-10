from __future__ import annotations

import hashlib
import hmac
import imghdr
import base64
import binascii
import re
import sqlite3
import random
import secrets
import smtplib
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from os import getenv
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import unquote
import urllib.parse
import urllib.request
import urllib.error
import json as _json
from uuid import uuid4

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# Google reCAPTCHA v2 — use official test keys for dev (always pass).
# Replace with real keys from https://www.google.com/recaptcha/admin for production.
RECAPTCHA_SITE_KEY   = getenv("RECAPTCHA_SITE_KEY",   "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI")
RECAPTCHA_SECRET_KEY = getenv("RECAPTCHA_SECRET_KEY", "6LeIxAcTAAAAAGG-vFI1TnRWxMZNFuojJ4WifJWe")
from typing import Any, Literal, Union

import joblib
import numpy as np
import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware


ROOT_DIR = Path(__file__).resolve().parents[1]
DB_PATH = Path(__file__).parent / "matricules.db"
EVENT_UPLOAD_DIR = ROOT_DIR / "uploads" / "events"
MESSENGER_UPLOAD_DIR = ROOT_DIR / "uploads" / "messenger"
JWT_SECRET = getenv("JWT_SECRET", getenv("APP_SESSION_SECRET", "dev-jwt-secret-change-me"))
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(getenv("JWT_EXPIRE_MINUTES", "60") or "60")
SUPERADMIN_ACCOUNT_ID = int(getenv("SUPERADMIN_ACCOUNT_ID", "3") or "3")
SUPERADMIN_EMAIL = getenv("SUPERADMIN_EMAIL", "teamdev43@gmail.com").strip().lower()
USER_DASHBOARD_URL = getenv("USER_DASHBOARD_URL", "/dashboard").strip() or "/dashboard"
PUBLIC_APP_BASE_URL = getenv("PUBLIC_APP_BASE_URL", "http://127.0.0.1:5000").strip().rstrip("/")
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_ATTEMPTS = 5
LOGIN_BLOCK_SECONDS = 300
EVENT_REMINDER_DEFAULT_HOUR = int(getenv("EVENT_REMINDER_DEFAULT_HOUR", "9") or "9")
EVENT_REMINDER_DISPATCH_INTERVAL_SECONDS = int(
    getenv("EVENT_REMINDER_DISPATCH_INTERVAL_SECONDS", "45") or "45"
)
EVENT_REMINDER_MILESTONES: list[tuple[str, timedelta, str]] = [
    ("72h", timedelta(hours=72), "dans 72 heures"),
    ("48h", timedelta(hours=48), "dans 48 heures"),
    ("24h", timedelta(hours=24), "dans 24 heures"),
    ("5h", timedelta(hours=5), "dans 5 heures"),
    ("3h", timedelta(hours=3), "dans 3 heures"),
    ("1h", timedelta(hours=1), "dans 1 heure"),
    ("30m", timedelta(minutes=30), "dans 30 minutes"),
    ("start", timedelta(0), "maintenant"),
]
DEFAULT_CORS_ORIGINS = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:8080",
    "http://localhost:8080",
]
MASKED_COMMENT_TEXT = "Ce commentaire a ete masque par admin."

BADWORD_PATTERNS: list[tuple[str, list[str]]] = [
    ("fr", ["con", "connard", "connasse", "encule", "enculé", "pute", "salope", "merde", "batard", "bâtard"]),
    ("en", ["fuck", "fucking", "bitch", "asshole", "bastard", "shit", "dick", "slut", "whore"]),
    ("darija", ["zebi", "zebbi", "nayek", "nayyek", "kahba", "kahb", "bhim", "7mar", "hmar"]),
    ("ar", ["زب", "زبي", "نيك", "ينيك", "قحبة", "شرموطة", "كلب", "حمار"]),
]


def _load_cors_origins() -> list[str]:
    raw = getenv("CORS_ALLOW_ORIGINS", "")
    if not raw.strip():
        return DEFAULT_CORS_ORIGINS
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _normalize_text_for_moderation(text: str) -> str:
    value = (text or "").lower().strip()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.replace("5", "kh")
    value = value.replace("7", "h")
    value = value.replace("9", "q")
    value = value.replace("3", "a")
    value = value.replace("2", "a")
    value = value.replace("8", "gh")
    value = re.sub(r"[^\w\s\u0600-\u06FF]", " ", value, flags=re.UNICODE)
    value = re.sub(r"(.)\1{2,}", r"\1\1", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _find_badword(text: str) -> str | None:
    normalized = _normalize_text_for_moderation(text)
    padded = f" {normalized} "
    for _, words in BADWORD_PATTERNS:
        for word in words:
            normalized_word = _normalize_text_for_moderation(word)
            if not normalized_word:
                continue
            if re.search(rf"(?<!\w){re.escape(normalized_word)}(?!\w)", normalized, flags=re.UNICODE):
                return word
            if f" {normalized_word} " in padded:
                return word
    return None


_login_attempts: dict[str, list[float]] = {}
_blocked_clients: dict[str, float] = {}
_auth_lock = Lock()
_event_reminder_lock = Lock()
_last_event_reminder_dispatch_ts = 0.0


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS matricules (
                matricule TEXT PRIMARY KEY,
                created_at TEXT DEFAULT (datetime('now')),
                active    INTEGER DEFAULT 1
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO matricules (matricule) VALUES (?)",
            ("LY_2016",),
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                verified INTEGER DEFAULT 0,
                verification_token TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                event_date TEXT,
                location TEXT,
                description TEXT,
                image_path TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                email TEXT,
                stars INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(event_id, user_id),
                FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_ratings_event_id ON event_ratings(event_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                email TEXT,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comment_reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                comment_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reaction TEXT NOT NULL CHECK (reaction IN ('like', 'dislike')),
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(comment_id, user_id),
                FOREIGN KEY(comment_id) REFERENCES event_comments(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_comments_event_id ON event_comments(event_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comment_reactions_comment_id ON comment_reactions(comment_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                notification_type TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                event_id INTEGER,
                comment_id INTEGER,
                actor_user_id INTEGER,
                actor_username TEXT,
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_notifications_user_id ON user_notifications(user_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                email TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(event_id, user_id),
                FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_favorites_event_id ON event_favorites(event_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_favorites_user_id ON event_favorites(user_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_reminder_dispatch (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                milestone_key TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(event_id, user_id, milestone_key),
                FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_reminder_dispatch_event_user ON event_reminder_dispatch(event_id, user_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_reminder_email_dispatch (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                milestone_key TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(event_id, user_id, milestone_key),
                FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_reminder_email_dispatch_event_user ON event_reminder_email_dispatch(event_id, user_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messenger_access_requests (
                user_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
                requested_at TEXT DEFAULT (datetime('now')),
                reviewed_at TEXT,
                reviewed_by_user_id INTEGER,
                review_note TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(reviewed_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messenger_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                message_text TEXT,
                audio_path TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messenger_messages_created_at ON messenger_messages(created_at, id)"
        )

        user_columns = {
            str(r["name"]).lower()
            for r in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        event_columns = {
            str(r["name"]).lower()
            for r in conn.execute("PRAGMA table_info(events)").fetchall()
        }
        comment_columns = {
            str(r["name"]).lower()
            for r in conn.execute("PRAGMA table_info(event_comments)").fetchall()
        }
        if "role" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        if "image_path" not in event_columns:
            conn.execute("ALTER TABLE events ADD COLUMN image_path TEXT")
        if "original_content" not in comment_columns:
            conn.execute("ALTER TABLE event_comments ADD COLUMN original_content TEXT")
            conn.execute("UPDATE event_comments SET original_content = content WHERE original_content IS NULL")
        if "is_masked" not in comment_columns:
            conn.execute("ALTER TABLE event_comments ADD COLUMN is_masked INTEGER NOT NULL DEFAULT 0")
        if "moderation_reason" not in comment_columns:
            conn.execute("ALTER TABLE event_comments ADD COLUMN moderation_reason TEXT")
        if "matched_badword" not in comment_columns:
            conn.execute("ALTER TABLE event_comments ADD COLUMN matched_badword TEXT")
        if "parent_comment_id" not in comment_columns:
            conn.execute("ALTER TABLE event_comments ADD COLUMN parent_comment_id INTEGER")

        # Face descriptors table (permanent biometric storage per user)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS face_descriptors (
                user_id   INTEGER PRIMARY KEY,
                descriptor TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        conn.execute(
            "UPDATE users SET role = 'superadmin' WHERE id = ? OR lower(email) = ?",
            (SUPERADMIN_ACCOUNT_ID, SUPERADMIN_EMAIL),
        )
        conn.commit()


@dataclass
class ModelAsset:
    key: str
    task: str
    model_path: Path
    scaler_path: Path | None = None
    metadata_path: Path | None = None


MODEL_REGISTRY: dict[str, ModelAsset] = {
    "budget_notebook_4": ModelAsset(
        key="budget_notebook_4",
        task="regression",
        model_path=ROOT_DIR / "models" / "notebook_4_model_enhanced.pkl",
        scaler_path=ROOT_DIR / "models" / "notebook_4_scaler.pkl",
        metadata_path=ROOT_DIR / "models" / "notebook_4_metadata.json",
    ),
    "participation_notebook_5": ModelAsset(
        key="participation_notebook_5",
        task="regression",
        model_path=ROOT_DIR / "models" / "notebook_5_model_enhanced.pkl",
        scaler_path=ROOT_DIR / "models" / "notebook_5_scaler.pkl",
        metadata_path=ROOT_DIR / "models" / "notebook_5_metadata.json",
    ),
    "engagement_notebook_7": ModelAsset(
        key="engagement_notebook_7",
        task="classification",
        model_path=ROOT_DIR / "models" / "notebook_7_model.pkl",
        scaler_path=ROOT_DIR / "models" / "notebook_7_scaler.pkl",
    ),
    "members_notebook_3": ModelAsset(
        key="members_notebook_3",
        task="regression",
        model_path=ROOT_DIR / "models" / "notebook_3_model.pkl",
        scaler_path=ROOT_DIR / "models" / "notebook_3_scaler.pkl",
    ),
    "segmentation_notebook_9": ModelAsset(
        key="segmentation_notebook_9",
        task="clustering",
        model_path=ROOT_DIR / "notebook_9_model_enhanced.pkl",
        scaler_path=ROOT_DIR / "notebook_9_scaler.pkl",
        metadata_path=ROOT_DIR / "notebook_9_metadata.json",
    ),
    "timeseries_sarima": ModelAsset(
        key="timeseries_sarima",
        task="timeseries",
        model_path=ROOT_DIR / "models_timeseries" / "best_model_sarima.pkl",
    ),
}


class PredictRequest(BaseModel):
    features: dict[str, float | int | bool | str] = Field(
        ..., description="Valeurs d'entree sous forme {feature: valeur}"
    )


class MessengerAccessReviewRequest(BaseModel):
    action: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=500)


class MessengerMessageCreateRequest(BaseModel):
    text: str | None = Field(default=None, max_length=2000)
    audio_data_url: str | None = None


class ForecastRequest(BaseModel):
    periods: int = Field(12, ge=1, le=120, description="Nombre de periodes a predire")


class AuthTokenRequest(BaseModel):
    username: str = Field(..., min_length=1, description="Nom d'utilisateur ou email")
    password: str = Field(..., min_length=1, description="Mot de passe du compte")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int

class UserRoleUpdateRequest(BaseModel):
    role: Literal["user", "admin", "logistique"]

class EventCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    event_date: str | None = None
    location: str | None = None
    description: str | None = None


class EventUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    event_date: str | None = None
    location: str | None = None
    description: str | None = None


class EventWeatherPreviewRequest(BaseModel):
    event_date: str = Field(..., min_length=10, max_length=32)
    location: str = Field(..., min_length=3, max_length=400)


class EventRatingRequest(BaseModel):
    stars: int = Field(..., ge=1, le=5, description="Note en etoiles entre 1 et 5")


class EventCommentRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=1000, description="Commentaire sur l'evenement")
    parent_comment_id: int | None = Field(default=None, ge=1, description="Commentaire parent si c'est une reponse")


class CommentReactionRequest(BaseModel):
    reaction: Literal["like", "dislike", "none"] = Field(
        ..., description="Reaction sur commentaire: like, dislike ou none"
    )


class EventFavoriteRequest(BaseModel):
    action: Literal["add", "remove", "toggle"] = Field(
        default="toggle", description="Action sur favori: add, remove ou toggle"
    )


app = FastAPI(
    title="ML Models API",
    version="1.0.0",
    description="API pour consommer les modeles ML de ce workspace",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_load_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.add_middleware(
    SessionMiddleware,
    secret_key=getenv("APP_SESSION_SECRET", secrets.token_urlsafe(32)),
    session_cookie="ml_admin_session",
    same_site="lax",
    https_only=False,
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next: Any) -> Response:
    response = await call_next(request)

    is_bi_page = request.url.path.rstrip("/") == "/admin/bi-dashboard"
    is_face_setup_page = request.url.path.rstrip("/") == "/admin/face-setup"
    is_register_page = request.url.path.rstrip("/") == "/register"

    if is_bi_page or is_face_setup_page:        # BI dashboard needs to load powerbi-client SDK and embed Power BI frames
        if is_bi_page and "x-frame-options" in response.headers:
            del response.headers["x-frame-options"]
        elif not is_bi_page:
            response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data: blob: https:; "
            "style-src 'self' 'unsafe-inline' https:; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://api.mapbox.com https://fonts.googleapis.com; "
            "media-src 'self' blob: data:; "
            "font-src 'self' https: data:; "
            "connect-src 'self' https://app.powerbi.com https://api.powerbi.com https://login.microsoftonline.com https://api.mapbox.com https://events.mapbox.com https://cdn.jsdelivr.net https://api.open-meteo.com; "
            "frame-src 'self' https://app.powerbi.com https://login.microsoftonline.com https://msit.powerbi.com; "
            "worker-src blob:; "
            "child-src blob: https://app.powerbi.com"
        )
    else:
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        if is_register_page:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "img-src 'self' data: blob: https:; "
                "style-src 'self' 'unsafe-inline' https:; "
                "script-src 'self' 'unsafe-inline' https://www.google.com https://www.gstatic.com; "
                "frame-src https://www.google.com; "
                "font-src 'self' https: data:; "
                "connect-src 'self' https://www.google.com; "
                "worker-src blob:; "
                "child-src blob: https://www.google.com"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "img-src 'self' data: blob: https:; "
                "style-src 'self' 'unsafe-inline' https:; "
                "script-src 'self' 'unsafe-inline' https://api.mapbox.com https://fonts.googleapis.com; "
                "media-src 'self' blob: data:; "
                "font-src 'self' https: data:; "
                "connect-src 'self' http://127.0.0.1:8000 http://localhost:8000 https://api.mapbox.com https://events.mapbox.com https://*.mapbox.com https://api.open-meteo.com; "
                "worker-src blob:; "
                "child-src blob:"
            )
    return response


# ─────────────────────────────────────────────
# NLP MESSENGER ANALYSIS
# ─────────────────────────────────────────────

# Lexicons (French + Arabic + Tunisian)
_NLP_TENSION = {
    "colère","énervé","enervé","enervé","furieux","agressif","dispute",
    "conflit","problème","probleme","insulte","grossier","honte","idiot",
    "nul","inutile","incompétent","incompetent","arrête","arrete","suffit",
    "catastrophe","horrible","inacceptable","scandaleux","inadmissible",
    "déteste","deteste","haïs","hais","ras-le-bol","marre","assez",
    "bêtise","betise","stupide","menteur","menteurs","lâche","lache",
    # Arabic / Tunisian
    "يلعن","حرام","عيب","كذاب","غلط","مشكلة","مشاكل","حقير","وقح",
    "9atalha","barra","7mar","hmara","klib","chnowa","ma3andeksh","chbik",
    "labes","malek","tfeh","mrigel","mkhayba","5esra","5ayeb",
}
_NLP_POSITIVE = {
    "super","excellent","bravo","félicitations","merci","bien","parfait",
    "génial","top","formidable","magnifique","bonne","bon","agree","ok",
    "d'accord","accord","ensemble","collaboration","aide","soutien",
    "progresser","avancer","réussi","reussi","accompli","fier","fière",
    # Arabic / Tunisian
    "برافو","شكرا","مزيان","باهي","تمام","صح","نجاح","ممتاز",
    "bhi","mzyen","baraka","merci","sahh","bien","top","3aychek",
}
_NLP_LEADERSHIP = {
    "on devrait","il faut","je propose","je suggère","je suggere",
    "mon idée","ma suggestion","voici le plan","je pense que","selon moi",
    "نقترح","أقترح","يلزم","لازم","نفكر","نقول","selon","plan","idée",
}

# Cooldown : une seule alerte toutes les 5 min max (évite le spam)
_nlp_alert_cooldown: float = 300.0  # secondes
_nlp_last_alert_ts: float = 0.0
_nlp_alert_lock = Lock()

# ── Faster-Whisper (chargé une seule fois, paresseux) ──────────────────────
_whisper_model: Any = None
_whisper_lock = Lock()


def _get_whisper_model() -> Any:
    """Retourne le modèle Whisper, chargé à la première demande."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model
        try:
            from faster_whisper import WhisperModel  # type: ignore
            # "tiny" : rapide sur CPU, ~40 Mo, suffisant pour détecter des mots-clés
            _whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
        except Exception:
            _whisper_model = None  # faster-whisper pas disponible
    return _whisper_model


def _transcribe_audio(audio_file_path: str) -> str:
    """Transcrit un fichier audio et renvoie le texte (ou '' si échec)."""
    model = _get_whisper_model()
    if model is None:
        return ""
    try:
        segments, _ = model.transcribe(audio_file_path, beam_size=1, language=None)
        return " ".join(seg.text.strip() for seg in segments).strip()
    except Exception:
        return ""


    """Analyse NLP légère sans dépendances externes."""
    if not messages:
        return {
            "total_messages": 0,
            "tension_score": 0,
            "tension_level": "calme",
            "ambiance": "neutre",
            "positive_ratio": 0,
            "negative_ratio": 0,
            "leader": None,
            "leader_score": 0,
            "tension_triggers": [],
            "user_stats": [],
            "alert_needed": False,
        }

    user_stats: dict[str, dict] = {}
    tension_words_found: list[str] = []
    pos_count = 0
    neg_count = 0
    total_words = 0

    for msg in messages:
        text = (msg.get("message_text") or "").lower()
        username = msg.get("username") or "inconnu"
        if username not in user_stats:
            user_stats[username] = {
                "username": username,
                "message_count": 0,
                "total_words": 0,
                "tension_hits": 0,
                "leadership_hits": 0,
                "positive_hits": 0,
                "has_audio": 0,
            }
        st = user_stats[username]
        st["message_count"] += 1
        if msg.get("audio_path"):
            st["has_audio"] += 1

        words = re.findall(r'\w+', text)
        st["total_words"] += len(words)
        total_words += len(words)

        for w in words:
            if w in _NLP_TENSION:
                st["tension_hits"] += 1
                neg_count += 1
                if w not in tension_words_found:
                    tension_words_found.append(w)
            elif w in _NLP_POSITIVE:
                st["positive_hits"] += 1
                pos_count += 1
        for phrase in _NLP_LEADERSHIP:
            if phrase in text:
                st["leadership_hits"] += 1

    total_msg = len(messages)
    total_scored = pos_count + neg_count or 1
    positive_ratio = round(pos_count / total_scored * 100)
    negative_ratio = round(neg_count / total_scored * 100)
    tension_score = round(neg_count / (total_words or 1) * 1000, 1)  # per-1000 words

    if tension_score >= 15:
        tension_level = "critique"
    elif tension_score >= 7:
        tension_level = "élevée"
    elif tension_score >= 3:
        tension_level = "modérée"
    else:
        tension_level = "calme"

    if positive_ratio >= 60:
        ambiance = "positive"
    elif negative_ratio >= 40:
        ambiance = "tendue"
    elif negative_ratio >= 20:
        ambiance = "mitigée"
    else:
        ambiance = "neutre"

    # Leadership: highest combined score (message_count*2 + leadership_hits*5 + positive_hits)
    leader = None
    leader_score = 0
    user_list = []
    for st in user_stats.values():
        score = st["message_count"] * 2 + st["leadership_hits"] * 5 + st["positive_hits"]
        st["leadership_score"] = score
        if score > leader_score:
            leader_score = score
            leader = st["username"]
        user_list.append(st)

    user_list.sort(key=lambda x: x["message_count"], reverse=True)

    return {
        "total_messages": total_msg,
        "tension_score": tension_score,
        "tension_level": tension_level,
        "ambiance": ambiance,
        "positive_ratio": positive_ratio,
        "negative_ratio": negative_ratio,
        "leader": leader,
        "leader_score": leader_score,
        "tension_triggers": tension_words_found[:10],
        "user_stats": user_list,
        "alert_needed": tension_level in ("élevée", "critique"),
    }


def _send_nlp_alert_email(analysis: dict, admin_email: str) -> None:
    smtp_host = getenv("SMTP_HOST", "").strip()
    smtp_port = int(getenv("SMTP_PORT", "587"))
    smtp_user = getenv("SMTP_USER", "").strip()
    smtp_pass = getenv("SMTP_PASSWORD", "").strip()
    smtp_from = getenv("SMTP_FROM", smtp_user).strip()
    if not smtp_host or not smtp_from:
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = f"⚠️ Alerte Messenger — Tensions détectées (niveau: {analysis['tension_level']})"
        msg["From"] = smtp_from
        msg["To"] = admin_email
        triggers = ", ".join(analysis["tension_triggers"]) or "—"
        body = f"""Bonjour,

Une analyse NLP du groupe Messenger a détecté des tensions.

📊 Résumé :
  • Niveau de tension : {analysis['tension_level'].upper()}
  • Score de tension  : {analysis['tension_score']} / 1000 mots
  • Ambiance générale : {analysis['ambiance']}
  • Messages analysés : {analysis['total_messages']}
  • Ratio positif     : {analysis['positive_ratio']}%
  • Ratio négatif     : {analysis['negative_ratio']}%
  • Leader naturel    : {analysis['leader'] or '—'}
  • Mots déclencheurs : {triggers}

Connectez-vous au panneau d'administration pour consulter l'analyse complète.

Cordialement,
Système Scout Tunisien
"""
        msg.set_content(body)
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.ehlo()
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)
    except Exception:
        pass  # email failure is non-blocking


def _maybe_send_auto_tension_alert(msg_text: str) -> None:
    """Send automatic alert email if tension keywords are detected in the message."""
    global _nlp_last_alert_ts
    if not msg_text:
        return

    now = time.time()
    with _nlp_alert_lock:
        if now - _nlp_last_alert_ts < _nlp_alert_cooldown:
            return

    words = set(re.findall(r'\w+', msg_text.lower()))
    if not (words & _NLP_TENSION):
        return

    admin_email = getenv("SUPERADMIN_EMAIL", "").strip()
    if not admin_email:
        return

    try:
        with _get_db() as conn:
            rows = conn.execute(
                "SELECT id, user_id, username, message_text, audio_path, created_at FROM messenger_messages ORDER BY created_at ASC"
            ).fetchall()
        analysis = _nlp_analyse_messages([dict(r) for r in rows])
        _send_nlp_alert_email(analysis, admin_email)
        with _nlp_alert_lock:
            _nlp_last_alert_ts = now
    except Exception:
        pass


def _postprocess_messenger_audio(message_id: int, audio_path: str | None, base_text: str | None) -> None:
    """Background task: transcribe audio (if needed), persist text, then run auto-alert."""
    effective_text = base_text or ""
    if audio_path and not base_text:
        try:
            full_path = str(ROOT_DIR / audio_path.lstrip("/"))
            transcription = (_transcribe_audio(full_path) or "").strip()
            if transcription:
                effective_text = transcription
                with _get_db() as conn:
                    conn.execute(
                        "UPDATE messenger_messages SET message_text = ? WHERE id = ? AND (message_text IS NULL OR TRIM(message_text) = '')",
                        (transcription, message_id),
                    )
                    conn.commit()
        except Exception:
            pass

    _maybe_send_auto_tension_alert(effective_text)


@app.get("/api/messenger/analysis")
def messenger_analysis(request: Request) -> dict:
    _require_superadmin_api(request)
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT id, user_id, username, message_text, audio_path, created_at FROM messenger_messages ORDER BY created_at ASC"
        ).fetchall()
    messages = [dict(r) for r in rows]
    result = _nlp_analyse_messages(messages)
    return result


@app.post("/api/messenger/analysis/send-alert")
def messenger_analysis_send_alert(request: Request) -> dict:
    _require_superadmin_api(request)
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT id, user_id, username, message_text, audio_path, created_at FROM messenger_messages ORDER BY created_at ASC"
        ).fetchall()
    messages = [dict(r) for r in rows]
    analysis = _nlp_analyse_messages(messages)
    admin_email = getenv("SUPERADMIN_EMAIL", "").strip()
    if admin_email:
        _send_nlp_alert_email(analysis, admin_email)
    return {"sent": bool(admin_email), "analysis": analysis}


@app.on_event("startup")
def on_startup() -> None:
    _init_db()
    _dispatch_event_reminders(force=True)


# Serve videos folder as static files
_videos_dir = ROOT_DIR / "videos"
if _videos_dir.exists():
    app.mount("/videos", StaticFiles(directory=str(_videos_dir)), name="videos")

# Serve front-office template from scout folder
_front_dir = ROOT_DIR / "scout"
if _front_dir.exists():
    app.mount("/front", StaticFiles(directory=str(_front_dir), html=True), name="front")

# Serve face recognition models
_face_models_dir = Path(__file__).parent / "face_models"
if _face_models_dir.exists():
    app.mount("/face-models", StaticFiles(directory=str(_face_models_dir)), name="face-models")

# Serve uploaded event images
EVENT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MESSENGER_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(ROOT_DIR / "uploads")), name="uploads")


def _store_event_image(image: UploadFile | None) -> str | None:
    if image is None or not image.filename:
        return None

    content = image.file.read()
    if not content:
        return None
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image trop volumineuse (max 5MB)")

    detected = imghdr.what(None, h=content)
    ext_map = {"jpeg": "jpg", "png": "png", "gif": "gif", "webp": "webp"}
    ext = ext_map.get(detected)
    if ext is None:
        raise HTTPException(status_code=415, detail="Format image non supporte")

    filename = f"{uuid4().hex}.{ext}"
    output_path = EVENT_UPLOAD_DIR / filename
    output_path.write_bytes(content)
    return f"/uploads/events/{filename}"


def _store_messenger_audio_data_url(audio_data_url: str) -> str:
    raw = (audio_data_url or "").strip()
    if not raw.startswith("data:audio/") or ";base64," not in raw:
        raise HTTPException(status_code=422, detail="Format audio invalide")

    header, b64_part = raw.split(",", 1)
    mime = header[5:].split(";", 1)[0].lower()
    ext_map = {
        "audio/webm": "webm",
        "audio/wav": "wav",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/ogg": "ogg",
    }
    ext = ext_map.get(mime)
    if ext is None:
        raise HTTPException(status_code=415, detail="Type audio non supporte")

    try:
        audio_bytes = base64.b64decode(b64_part, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="Audio base64 invalide") from exc

    if not audio_bytes:
        raise HTTPException(status_code=422, detail="Audio vide")
    if len(audio_bytes) > 4 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio trop volumineux (max 4MB)")

    filename = f"{uuid4().hex}.{ext}"
    output_path = MESSENGER_UPLOAD_DIR / filename
    output_path.write_bytes(audio_bytes)
    return f"/uploads/messenger/{filename}"


# ---------------------------------------------------------------------------
# Matricule models
# ---------------------------------------------------------------------------

class MatriculeValidateRequest(BaseModel):
    matricule: str


class MatriculeAddRequest(BaseModel):
    matricule: str


_loaded_models: dict[str, Any] = {}
_loaded_scalers: dict[str, Any] = {}
_loaded_metadata: dict[str, dict[str, Any]] = {}


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    return pd.read_json(path, typ="series").to_dict()


def _load_model(asset: ModelAsset) -> Any:
    if asset.key not in _loaded_models:
        if not asset.model_path.exists():
            raise HTTPException(status_code=500, detail=f"Modele introuvable: {asset.model_path}")
        _loaded_models[asset.key] = joblib.load(asset.model_path)
    return _loaded_models[asset.key]


def _load_scaler(asset: ModelAsset) -> Any | None:
    if asset.scaler_path is None or not asset.scaler_path.exists():
        return None
    if asset.key not in _loaded_scalers:
        _loaded_scalers[asset.key] = joblib.load(asset.scaler_path)
    return _loaded_scalers[asset.key]


def _load_metadata(asset: ModelAsset) -> dict[str, Any]:
    if asset.key not in _loaded_metadata:
        _loaded_metadata[asset.key] = _load_json(asset.metadata_path)
    return _loaded_metadata[asset.key]


def _resolve_feature_order(model: Any, metadata: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    if isinstance(metadata.get("features"), list) and metadata["features"]:
        return metadata["features"]
    if hasattr(model, "feature_names_in_"):
        return [str(x) for x in model.feature_names_in_]
    return list(payload.keys())


def _prepare_input(asset: ModelAsset, model: Any, payload: dict[str, Any]) -> tuple[np.ndarray | pd.DataFrame, list[str]]:
    metadata = _load_metadata(asset)
    feature_order = _resolve_feature_order(model, metadata, payload)

    missing = [f for f in feature_order if f not in payload]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Features manquantes",
                "missing_features": missing,
                "expected_features": feature_order,
            },
        )

    row = {name: payload[name] for name in feature_order}
    df = pd.DataFrame([row], columns=feature_order)

    scaler = _load_scaler(asset)
    if scaler is not None:
        try:
            return scaler.transform(df), feature_order
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Erreur de scaling: {exc}") from exc

    return df, feature_order


def _to_native(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    return value


# ---------------------------------------------------------------------------
# CAPTCHA helpers (stateless HMAC — works through proxy without session cookie)
# ---------------------------------------------------------------------------
_CAPTCHA_SECRET = getenv("APP_SESSION_SECRET", "").encode() or b"captcha-fallback-key-change-me"
_CAPTCHA_TTL = 600  # 10 minutes


def _make_captcha_token(answer: int) -> str:
    """Return a signed token: '{answer}:{timestamp}:{hmac}'."""
    ts = int(time.time())
    msg = f"{answer}:{ts}".encode()
    sig = hmac.new(_CAPTCHA_SECRET, msg, hashlib.sha256).hexdigest()
    return f"{answer}:{ts}:{sig}"


def _verify_captcha_token(token: str, user_answer: str) -> bool:
    """Return True if token is valid, not expired, and answer matches."""
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return False
        expected_answer, ts_str, sig = parts
        ts = int(ts_str)
        if int(time.time()) - ts > _CAPTCHA_TTL:
            return False
        msg = f"{expected_answer}:{ts_str}".encode()
        expected_sig = hmac.new(_CAPTCHA_SECRET, msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return False
        return int(user_answer) == int(expected_answer)
    except (ValueError, TypeError):
        return False


def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        120000,
    ).hex()
    return f"{salt}${digest}"


def _verify_password(password: str, stored_hash: str) -> bool:
    if "$" not in stored_hash:
        return False
    salt, expected = stored_hash.split("$", 1)
    actual = _hash_password(password, salt).split("$", 1)[1]
    return secrets.compare_digest(actual, expected)


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").strip()
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _prune_attempts(now_ts: float, key: str) -> list[float]:
    attempts = [ts for ts in _login_attempts.get(key, []) if now_ts - ts < LOGIN_WINDOW_SECONDS]
    if attempts:
        _login_attempts[key] = attempts
    else:
        _login_attempts.pop(key, None)
    return attempts


def _ensure_not_rate_limited(request: Request) -> None:
    key = _get_client_ip(request)
    now_ts = time.time()
    with _auth_lock:
        blocked_until = _blocked_clients.get(key, 0.0)
        if blocked_until > now_ts:
            retry_after = int(blocked_until - now_ts)
            raise HTTPException(
                status_code=429,
                detail=f"Trop de tentatives. Reessayez dans {retry_after} secondes.",
            )
        if blocked_until:
            _blocked_clients.pop(key, None)
        _prune_attempts(now_ts, key)


def _record_auth_failure(request: Request) -> None:
    key = _get_client_ip(request)
    now_ts = time.time()
    with _auth_lock:
        attempts = _prune_attempts(now_ts, key)
        attempts.append(now_ts)
        _login_attempts[key] = attempts
        if len(attempts) >= LOGIN_MAX_ATTEMPTS:
            _blocked_clients[key] = now_ts + LOGIN_BLOCK_SECONDS
            _login_attempts.pop(key, None)


def _clear_auth_failures(request: Request) -> None:
    key = _get_client_ip(request)
    with _auth_lock:
        _login_attempts.pop(key, None)
        _blocked_clients.pop(key, None)


def _create_access_token(row: sqlite3.Row) -> tuple[str, int]:
    expires_delta = timedelta(minutes=JWT_EXPIRE_MINUTES)
    expires_at = datetime.now(timezone.utc) + expires_delta
    user_id = int(row["id"])
    username = str(row["username"])
    role = str(row["role"])
    email = str(row["email"]).lower()
    is_superadmin = (
        role == "superadmin"
        and (user_id == SUPERADMIN_ACCOUNT_ID or email == SUPERADMIN_EMAIL)
    )
    payload = {
        "uid": user_id,
        "sub": username,
        "role": role,
        "email": email,
        "is_superadmin": is_superadmin,
        "type": "access",
        "exp": expires_at,
        "iat": datetime.now(timezone.utc),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token, int(expires_delta.total_seconds())


def _get_bearer_token(request: Request) -> str | None:
    auth_header = request.headers.get("authorization", "").strip()
    if not auth_header:
        return None
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _authenticate_credentials(username: str, password: str) -> sqlite3.Row | None:
    login_value = username.strip().lower()
    if not login_value:
        return None

    with _get_db() as conn:
        row = conn.execute(
            "SELECT id, username, email, role, password_hash, verified FROM users WHERE lower(username) = ? OR lower(email) = ?",
            (login_value, login_value),
        ).fetchone()
    if row is None:
        return None
    if not _verify_password(password, str(row["password_hash"])):
        return None
    return row


def _send_verification_email(email_to: str, verify_url: str) -> bool:
    smtp_host = getenv("SMTP_HOST", "").strip()
    smtp_port = int(getenv("SMTP_PORT", "587") or "587")
    smtp_user = getenv("SMTP_USER", "").strip()
    smtp_pass = getenv("SMTP_PASSWORD", "").strip()
    smtp_from = getenv("SMTP_FROM", smtp_user or "")

    # Dev fallback when SMTP is not configured.
    if not smtp_host or not smtp_from:
        print(f"[EMAIL VERIFICATION LINK] {email_to}: {verify_url}")
        return False

    message = EmailMessage()
    message["Subject"] = "Verification de votre compte Scout Assistant"
    message["From"] = smtp_from
    message["To"] = email_to
    message.set_content(
        "Bonjour,\n\n"
        "Cliquez sur ce lien pour verifier votre compte:\n"
        f"{verify_url}\n\n"
        "Si vous n'etes pas a l'origine de cette demande, ignorez cet email."
    )

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(message)
        return True
    except Exception as exc:
        print(f"[EMAIL ERROR] {exc}")
        print(f"[EMAIL VERIFICATION LINK] {email_to}: {verify_url}")
        return False


def _get_admin_notification_recipients() -> list[str]:
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT lower(email) AS email
            FROM users
            WHERE role IN ('admin', 'superadmin')
              AND verified = 1
              AND email IS NOT NULL
              AND trim(email) != ''
            """
        ).fetchall()
    return [str(r["email"]).strip().lower() for r in rows if str(r["email"]).strip()]


def _send_event_rating_notification(recipients: list[str], event_title: str, rater: str, stars: int) -> bool:
    if not recipients:
        return False

    smtp_host = getenv("SMTP_HOST", "").strip()
    smtp_port = int(getenv("SMTP_PORT", "587") or "587")
    smtp_user = getenv("SMTP_USER", "").strip()
    smtp_pass = getenv("SMTP_PASSWORD", "").strip()
    smtp_from = getenv("SMTP_FROM", smtp_user or "")

    if not smtp_host or not smtp_from:
        print(
            f"[EVENT RATING NOTICE] event='{event_title}' by {rater}: {stars}/5 -> {', '.join(recipients)}"
        )
        return False

    message = EmailMessage()
    message["Subject"] = f"Nouvelle evaluation evenement: {event_title}"
    message["From"] = smtp_from
    message["To"] = ", ".join(recipients)
    message.set_content(
        "Bonjour,\n\n"
        "Une nouvelle evaluation a ete soumise sur un evenement.\n\n"
        f"Evenement: {event_title}\n"
        f"Utilisateur: {rater}\n"
        f"Note: {stars}/5\n\n"
        "Consultez le tableau de bord admin pour plus de details."
    )

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(message)
        return True
    except Exception as exc:
        print(f"[EMAIL ERROR] {exc}")
        print(
            f"[EVENT RATING NOTICE] event='{event_title}' by {rater}: {stars}/5 -> {', '.join(recipients)}"
        )
        return False


def _send_event_comment_notification(
    recipients: list[str],
    event_title: str,
    commenter: str,
    comment_content: str,
) -> bool:
    if not recipients:
        return False

    smtp_host = getenv("SMTP_HOST", "").strip()
    smtp_port = int(getenv("SMTP_PORT", "587") or "587")
    smtp_user = getenv("SMTP_USER", "").strip()
    smtp_pass = getenv("SMTP_PASSWORD", "").strip()
    smtp_from = getenv("SMTP_FROM", smtp_user or "")
    preview = comment_content.strip().replace("\n", " ")[:220]

    if not smtp_host or not smtp_from:
        print(
            f"[EVENT COMMENT NOTICE] event='{event_title}' by {commenter}: '{preview}' -> {', '.join(recipients)}"
        )
        return False

    message = EmailMessage()
    message["Subject"] = f"Nouveau commentaire evenement: {event_title}"
    message["From"] = smtp_from
    message["To"] = ", ".join(recipients)
    message.set_content(
        "Bonjour,\n\n"
        "Un nouveau commentaire a ete ajoute sur un evenement.\n\n"
        f"Evenement: {event_title}\n"
        f"Utilisateur: {commenter}\n"
        f"Commentaire: {preview}\n\n"
        "Consultez le tableau de bord admin pour plus de details."
    )

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(message)
        return True
    except Exception as exc:
        print(f"[EMAIL ERROR] {exc}")
        print(
            f"[EVENT COMMENT NOTICE] event='{event_title}' by {commenter}: '{preview}' -> {', '.join(recipients)}"
        )
        return False


def _send_comment_reaction_notification(
    recipients: list[str],
    event_title: str,
    reactor: str,
    reaction: str,
    comment_author: str,
    comment_content: str,
) -> bool:
    if not recipients:
        return False

    smtp_host = getenv("SMTP_HOST", "").strip()
    smtp_port = int(getenv("SMTP_PORT", "587") or "587")
    smtp_user = getenv("SMTP_USER", "").strip()
    smtp_pass = getenv("SMTP_PASSWORD", "").strip()
    smtp_from = getenv("SMTP_FROM", smtp_user or "")
    preview = comment_content.strip().replace("\n", " ")[:220]

    if not smtp_host or not smtp_from:
        print(
            f"[COMMENT REACTION NOTICE] event='{event_title}' by {reactor}: {reaction} on '{preview}' -> {', '.join(recipients)}"
        )
        return False

    message = EmailMessage()
    message["Subject"] = f"Nouvelle reaction commentaire: {event_title}"
    message["From"] = smtp_from
    message["To"] = ", ".join(recipients)
    message.set_content(
        "Bonjour,\n\n"
        "Une nouvelle reaction a ete ajoutee sur un commentaire d'evenement.\n\n"
        f"Evenement: {event_title}\n"
        f"Reaction: {reaction}\n"
        f"Par: {reactor}\n"
        f"Auteur du commentaire: {comment_author}\n"
        f"Commentaire: {preview}\n\n"
        "Consultez le tableau de bord admin pour plus de details."
    )

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(message)
        return True
    except Exception as exc:
        print(f"[EMAIL ERROR] {exc}")
        print(
            f"[COMMENT REACTION NOTICE] event='{event_title}' by {reactor}: {reaction} on '{preview}' -> {', '.join(recipients)}"
        )
        return False


def _send_flagged_comment_notification(
    recipients: list[str],
    event_title: str,
    commenter: str,
    comment_content: str,
    matched_badword: str,
) -> bool:
    if not recipients:
        return False

    smtp_host = getenv("SMTP_HOST", "").strip()
    smtp_port = int(getenv("SMTP_PORT", "587") or "587")
    smtp_user = getenv("SMTP_USER", "").strip()
    smtp_pass = getenv("SMTP_PASSWORD", "").strip()
    smtp_from = getenv("SMTP_FROM", smtp_user or "")
    preview = comment_content.strip().replace("\n", " ")[:300]

    if not smtp_host or not smtp_from:
        print(
            f"[FLAGGED COMMENT NOTICE] event='{event_title}' by {commenter}: badword='{matched_badword}' comment='{preview}' -> {', '.join(recipients)}"
        )
        return False

    message = EmailMessage()
    message["Subject"] = f"Commentaire masque automatiquement: {event_title}"
    message["From"] = smtp_from
    message["To"] = ", ".join(recipients)
    message.set_content(
        "Bonjour,\n\n"
        "Un commentaire a ete masque automatiquement suite a la detection d'un mot interdit.\n\n"
        f"Evenement: {event_title}\n"
        f"Utilisateur: {commenter}\n"
        f"Badword detecte: {matched_badword}\n"
        f"Commentaire original: {preview}\n\n"
        "Consultez l'admin pour suivi."
    )

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(message)
        return True
    except Exception as exc:
        print(f"[EMAIL ERROR] {exc}")
        print(
            f"[FLAGGED COMMENT NOTICE] event='{event_title}' by {commenter}: badword='{matched_badword}' comment='{preview}' -> {', '.join(recipients)}"
        )
        return False

    message = EmailMessage()
    message["Subject"] = f"Nouvelle reaction commentaire: {event_title}"
    message["From"] = smtp_from
    message["To"] = ", ".join(recipients)
    message.set_content(
        "Bonjour,\n\n"
        "Une nouvelle reaction a ete ajoutee sur un commentaire d'evenement.\n\n"
        f"Evenement: {event_title}\n"
        f"Reaction: {reaction}\n"
        f"Par: {reactor}\n"
        f"Auteur du commentaire: {comment_author}\n"
        f"Commentaire: {preview}\n\n"
        "Consultez le tableau de bord admin pour plus de details."
    )

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(message)
        return True
    except Exception as exc:
        print(f"[EMAIL ERROR] {exc}")
        print(
            f"[COMMENT REACTION NOTICE] event='{event_title}' by {reactor}: {reaction} on '{preview}' -> {', '.join(recipients)}"
        )
        return False


def _parse_event_datetime(raw_value: str | None) -> datetime | None:
    if raw_value is None:
        return None
    value = str(raw_value).strip()
    if not value:
        return None

    datetime_formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%d/%m/%Y %H:%M",
        "%d-%m-%Y %H:%M",
    )
    date_only_formats = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y")

    for fmt in datetime_formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    for fmt in date_only_formats:
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(hour=EVENT_REMINDER_DEFAULT_HOUR, minute=0, second=0, microsecond=0)
        except ValueError:
            continue
    return None


def _send_event_reminder_email(
    email_to: str,
    username: str,
    event_title: str,
    reminder_label: str,
    event_datetime_text: str,
    event_location: str | None,
) -> bool:
    smtp_host = getenv("SMTP_HOST", "").strip()
    smtp_port = int(getenv("SMTP_PORT", "587") or "587")
    smtp_user = getenv("SMTP_USER", "").strip()
    smtp_pass = getenv("SMTP_PASSWORD", "").strip()
    smtp_from = getenv("SMTP_FROM", smtp_user or "")
    dashboard_url = USER_DASHBOARD_URL
    if dashboard_url.startswith("/"):
        dashboard_url = f"{PUBLIC_APP_BASE_URL}{dashboard_url}"

    if not smtp_host or not smtp_from:
        print(
            "[EVENT REMINDER EMAIL] "
            f"to={email_to} user={username} event='{event_title}' reminder='{reminder_label}' datetime='{event_datetime_text}'"
        )
        return False

    where_text = event_location.strip() if event_location else "Lieu non precise"
    message = EmailMessage()
    message["Subject"] = f"Rappel evenement: {event_title}"
    message["From"] = smtp_from
    message["To"] = email_to
    message.set_content(
        "Bonjour,\n\n"
        f"{username}, votre evenement favori/commente approche.\n\n"
        f"Evenement: {event_title}\n"
        f"Debut: {event_datetime_text}\n"
        f"Lieu: {where_text}\n"
        f"Rappel: debut {reminder_label}.\n\n"
        f"Consultez votre dashboard: {dashboard_url}\n"
    )

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(message)
        return True
    except Exception as exc:
        print(f"[EMAIL ERROR] {exc}")
        print(
            "[EVENT REMINDER EMAIL] "
            f"to={email_to} user={username} event='{event_title}' reminder='{reminder_label}' datetime='{event_datetime_text}'"
        )
        return False


def _dispatch_event_reminders(*, force: bool = False) -> int:
    global _last_event_reminder_dispatch_ts

    with _event_reminder_lock:
        now_ts = time.time()
        if not force and now_ts - _last_event_reminder_dispatch_ts < EVENT_REMINDER_DISPATCH_INTERVAL_SECONDS:
            return 0
        _last_event_reminder_dispatch_ts = now_ts

        now = datetime.now()
        created_count = 0
        with _get_db() as conn:
            events = conn.execute(
                """
                SELECT id, title, event_date, location
                FROM events
                WHERE event_date IS NOT NULL AND trim(event_date) != ''
                """
            ).fetchall()

            for ev in events:
                event_id = int(ev["id"])
                event_title = str(ev["title"] or "Evenement")
                event_location = str(ev["location"] or "").strip() or None
                event_dt = _parse_event_datetime(str(ev["event_date"] or ""))
                if event_dt is None:
                    continue

                if now > event_dt + timedelta(minutes=15):
                    continue

                participants = conn.execute(
                    """
                    SELECT DISTINCT
                        u.id AS user_id,
                        COALESCE(NULLIF(trim(u.username), ''), 'Utilisateur') AS username,
                        lower(trim(COALESCE(u.email, ''))) AS email
                    FROM users u
                    JOIN (
                        SELECT user_id FROM event_favorites WHERE event_id = ?
                        UNION
                        SELECT user_id FROM event_comments WHERE event_id = ?
                    ) engaged ON engaged.user_id = u.id
                    WHERE u.verified = 1
                    """,
                    (event_id, event_id),
                ).fetchall()
                if not participants:
                    continue

                event_dt_text = event_dt.strftime("%d/%m/%Y %H:%M")
                for milestone_key, milestone_delta, reminder_label in EVENT_REMINDER_MILESTONES:
                    trigger_dt = event_dt - milestone_delta
                    if milestone_key == "start":
                        if not (event_dt <= now <= event_dt + timedelta(minutes=15)):
                            continue
                    else:
                        if not (trigger_dt <= now < event_dt):
                            continue

                    for user in participants:
                        user_id = int(user["user_id"] or 0)
                        if user_id <= 0:
                            continue

                        reminder_created = conn.execute(
                            """
                            SELECT 1
                            FROM event_reminder_dispatch
                            WHERE event_id = ? AND user_id = ? AND milestone_key = ?
                            LIMIT 1
                            """,
                            (event_id, user_id, milestone_key),
                        ).fetchone()
                        if reminder_created is None:
                            conn.execute(
                                """
                                INSERT INTO event_reminder_dispatch (event_id, user_id, milestone_key)
                                VALUES (?, ?, ?)
                                """,
                                (event_id, user_id, milestone_key),
                            )
                            _create_user_notification(
                                conn,
                                user_id,
                                "event_reminder",
                                "Evenement a venir",
                                f"Rappel: '{event_title}' commence {reminder_label} ({event_dt_text}).",
                                event_id=event_id,
                            )
                            created_count += 1

                        email_already_sent = conn.execute(
                            """
                            SELECT 1
                            FROM event_reminder_email_dispatch
                            WHERE event_id = ? AND user_id = ? AND milestone_key = ?
                            LIMIT 1
                            """,
                            (event_id, user_id, milestone_key),
                        ).fetchone()
                        if email_already_sent is not None:
                            continue

                        email = str(user["email"] or "").strip().lower()
                        if email:
                            sent_ok = _send_event_reminder_email(
                                email,
                                str(user["username"] or "Utilisateur"),
                                event_title,
                                reminder_label,
                                event_dt_text,
                                event_location,
                            )
                            if sent_ok:
                                conn.execute(
                                    """
                                    INSERT OR IGNORE INTO event_reminder_email_dispatch (event_id, user_id, milestone_key)
                                    VALUES (?, ?, ?)
                                    """,
                                    (event_id, user_id, milestone_key),
                                )

            conn.commit()

        return created_count


def _get_comment_reaction_stats(conn: sqlite3.Connection, comment_id: int, user_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN reaction = 'like' THEN 1 ELSE 0 END), 0) AS like_count,
            COALESCE(SUM(CASE WHEN reaction = 'dislike' THEN 1 ELSE 0 END), 0) AS dislike_count,
            (
                SELECT r2.reaction
                FROM comment_reactions r2
                WHERE r2.comment_id = ? AND r2.user_id = ?
                LIMIT 1
            ) AS my_reaction
        FROM comment_reactions
        WHERE comment_id = ?
        """,
        (comment_id, user_id, comment_id),
    ).fetchone()
    return {
        "like_count": int(row["like_count"] or 0),
        "dislike_count": int(row["dislike_count"] or 0),
        "my_reaction": row["my_reaction"],
    }


def _create_user_notification(
    conn: sqlite3.Connection,
    user_id: int,
    notification_type: str,
    title: str,
    message: str,
    *,
    event_id: int | None = None,
    comment_id: int | None = None,
    actor_user_id: int | None = None,
    actor_username: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO user_notifications (
            user_id, notification_type, title, message,
            event_id, comment_id, actor_user_id, actor_username
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            notification_type,
            title,
            message,
            event_id,
            comment_id,
            actor_user_id,
            actor_username,
        ),
    )


def _is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated", False))


def _is_superadmin_identity(identity: dict[str, Any]) -> bool:
    try:
        user_id = int(identity.get("id", 0))
    except Exception:
        user_id = 0
    role = str(identity.get("role", ""))
    email = str(identity.get("email", "")).lower()
    explicit = bool(identity.get("is_superadmin", False))
    return (
        explicit
        or role == "superadmin"
        or user_id == SUPERADMIN_ACCOUNT_ID
        or email == SUPERADMIN_EMAIL
    )


def _sync_identity_with_db(identity: dict[str, Any]) -> dict[str, Any]:
    """Refresh role/email from DB to avoid stale session/token elevation."""
    try:
        user_id = int(identity.get("id", 0) or 0)
    except Exception:
        user_id = 0
    email = str(identity.get("email", "") or "").strip().lower()
    if user_id <= 0 and not email:
        return identity

    with _get_db() as conn:
        row = None
        if user_id > 0:
            row = conn.execute(
                "SELECT id, username, email, role FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row is None and email:
            row = conn.execute(
                "SELECT id, username, email, role FROM users WHERE lower(email) = ?",
                (email,),
            ).fetchone()

    if row is None:
        return identity

    identity["id"] = int(row["id"])
    identity["username"] = str(row["username"] or identity.get("username", ""))
    identity["email"] = str(row["email"] or email).lower()
    identity["role"] = str(row["role"] or "user")
    identity["is_superadmin"] = _is_superadmin_identity(identity)
    return identity


def _require_admin_api(request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    role = str(identity.get("role", "user"))
    if role not in ("admin", "superadmin", "logistique"):
        raise HTTPException(status_code=403, detail="Action reservee aux admins")
    return identity


def _resolve_request_identity(request: Request) -> dict[str, Any] | None:
    if _is_authenticated(request):
        identity = {
            "id": request.session.get("user_id", 0),
            "username": request.session.get("username", ""),
            "email": request.session.get("email", ""),
            "role": request.session.get("role", "user"),
            "is_superadmin": request.session.get("is_superadmin", False),
        }
        return _sync_identity_with_db(identity)

    token = _get_bearer_token(request)
    if not token:
        return None

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Token invalide ou expire") from exc

    if payload.get("type") != "access" or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Token invalide")

    identity = {
        "id": payload.get("uid", 0),
        "username": payload.get("sub", ""),
        "email": payload.get("email", ""),
        "role": payload.get("role", "user"),
        "is_superadmin": payload.get("is_superadmin", False),
    }
    return _sync_identity_with_db(identity)


def _require_auth_or_redirect(request: Request) -> RedirectResponse | None:
    identity = _resolve_request_identity(request)
    if identity is not None:
        return None
    return RedirectResponse(url="/login", status_code=303)


def _require_superadmin_or_redirect(request: Request) -> RedirectResponse | None:
    redirect = _require_auth_or_redirect(request)
    if redirect is not None:
        return redirect
    identity = _resolve_request_identity(request)
    if not identity or not _is_superadmin_identity(identity):
        role = identity.get("role", "user") if identity else "user"
        if role in ("admin", "superadmin"):
            return RedirectResponse(url="/chatbot", status_code=303)
        return RedirectResponse(url=USER_DASHBOARD_URL, status_code=303)
    return None


def _require_admin_or_redirect(request: Request) -> RedirectResponse | None:
    """Allow admin + superadmin; redirect regular users to public events page."""
    redirect = _require_auth_or_redirect(request)
    if redirect is not None:
        return redirect
    identity = _resolve_request_identity(request)
    if not identity:
        return RedirectResponse(url="/login", status_code=303)
    role = str(identity.get("role", "user"))
    if role not in ("admin", "superadmin", "logistique"):
        return RedirectResponse(url=USER_DASHBOARD_URL, status_code=303)
    return None


def _require_auth_api(request: Request) -> dict[str, Any]:
    identity = _resolve_request_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="Authentification requise")
    return identity


def _require_superadmin_api(request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    if not _is_superadmin_identity(identity):
        raise HTTPException(status_code=403, detail="Action reservee au superadmin")
    return identity


@app.get("/")
def root(request: Request) -> Response:
    if _is_authenticated(request):
        identity = _resolve_request_identity(request) or {}
        role = str(identity.get("role", "user"))
        if role in ("admin", "superadmin"):
            return RedirectResponse(url="/chatbot", status_code=303)
        return RedirectResponse(url=USER_DASHBOARD_URL, status_code=303)
    return RedirectResponse(url="/login", status_code=303)


@app.get("/login")
def login_page(request: Request) -> Response:
    if _is_authenticated(request):
        identity = _resolve_request_identity(request) or {}
        role = str(identity.get("role", "user"))
        if role in ("admin", "superadmin"):
            return RedirectResponse(url="/chatbot", status_code=303)
        return RedirectResponse(url=USER_DASHBOARD_URL, status_code=303)

    info = request.query_params.get("info", "")
    notices = {
        "invalid": '<div class="hint hint-error">Identifiants invalides.</div>',
        "not_verified": '<div class="hint hint-error">Compte non verifie. Verifiez votre email.</div>',
        "verified": '<div class="hint hint-success">Email verifie. Vous pouvez vous connecter.</div>',
        "created": '<div class="hint hint-success">Compte cree. Verifiez votre email avant connexion.</div>',
    }
    notice_html = notices.get(info, "")

    html_content = """
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Connexion Admin</title>
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body {
                font-family: 'Barlow', sans-serif;
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                background: linear-gradient(140deg, #11161f 0%, #2a3040 65%, #e90b35 140%);
                padding: 20px;
            }
            .login-card {
                width: 100%;
                max-width: 420px;
                background: #ffffff;
                border-top: 5px solid #e90b35;
                box-shadow: 0 24px 60px rgba(17, 22, 31, 0.25);
                padding: 34px 30px;
            }
            h1 {
                color: #11161f;
                font-size: 1.55em;
                margin-bottom: 6px;
                text-transform: uppercase;
                letter-spacing: 0.04em;
            }
            p {
                color: #5c6575;
                margin-bottom: 24px;
            }
            label {
                display: block;
                color: #11161f;
                margin-bottom: 6px;
                font-weight: 600;
                font-size: 0.92em;
            }
            input {
                width: 100%;
                border: 1px solid #ccd2de;
                padding: 11px 12px;
                margin-bottom: 16px;
                font-size: 0.95em;
            }
            input:focus {
                outline: none;
                border-color: #e90b35;
            }
            button {
                width: 100%;
                border: none;
                background: #e90b35;
                color: #fff;
                padding: 12px;
                font-size: 0.95em;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                cursor: pointer;
            }
            .hint {
                margin-top: 14px;
                font-size: 0.82em;
                color: #6b7383;
            }
            .hint-error { color: #c62828; }
            .hint-success { color: #2e7d32; }
            .secondary {
                margin-top: 10px;
                width: 100%;
                border: 1px solid #e90b35;
                background: transparent;
                color: #e90b35;
                display: block;
                text-align: center;
                text-decoration: none;
                padding: 11px;
                font-size: 0.9em;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }
        </style>
    </head>
    <body>
        <form class="login-card" method="post" action="/login">
            <h1>Admin Login</h1>
            <p>Connectez-vous pour acceder au chatbot et aux pages admin.</p>
            <label for="username">Nom d'utilisateur ou email</label>
            <input id="username" name="username" type="text" required>
            <label for="password">Mot de passe</label>
            <input id="password" name="password" type="password" required>
            <button type="submit">Se connecter</button>
            <a class="secondary" href="/register">Creer un compte</a>
            __NOTICE__
        </form>
    </body>
    </html>
    """
    html_content = html_content.replace("__NOTICE__", notice_html)
    return HTMLResponse(
        html_content,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.post("/login")
async def login_submit(request: Request) -> RedirectResponse:
    _ensure_not_rate_limited(request)
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    row = _authenticate_credentials(username, password)

    if row is None:
        _record_auth_failure(request)
        return RedirectResponse(url="/login?info=invalid", status_code=303)
    if int(row["verified"]) != 1:
        _record_auth_failure(request)
        return RedirectResponse(url="/login?info=not_verified", status_code=303)

    _clear_auth_failures(request)
    request.session["authenticated"] = True
    request.session["user_id"] = int(row["id"])
    request.session["username"] = row["username"]
    request.session["email"] = str(row["email"]).lower()
    request.session["role"] = str(row["role"])
    request.session["is_superadmin"] = _is_superadmin_identity(
        {
            "id": int(row["id"]),
            "role": str(row["role"]),
            "email": str(row["email"]).lower(),
        }
    )
    role = str(row["role"])
    if role in ("admin", "superadmin"):
        return RedirectResponse(url="/chatbot", status_code=303)
    return RedirectResponse(url="/front/events-front.html", status_code=303)


@app.post("/api/token", response_model=TokenResponse)
def issue_api_token(payload: AuthTokenRequest, request: Request) -> TokenResponse:
    _ensure_not_rate_limited(request)
    row = _authenticate_credentials(payload.username.strip(), payload.password)
    if row is None or int(row["verified"]) != 1:
        _record_auth_failure(request)
        raise HTTPException(status_code=401, detail="Identifiants invalides ou compte non verifie")

    _clear_auth_failures(request)
    access_token, expires_in = _create_access_token(row)
    return TokenResponse(access_token=access_token, expires_in=expires_in)


@app.get("/session-login")
def session_login(request: Request, token: str = "", next: str = "/chatbot") -> RedirectResponse:
    token = token.strip()
    if not token:
        return RedirectResponse(url="/login?info=invalid", status_code=303)

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return RedirectResponse(url="/login?info=invalid", status_code=303)

    if payload.get("type") != "access" or not payload.get("sub"):
        return RedirectResponse(url="/login?info=invalid", status_code=303)

    request.session["authenticated"] = True
    request.session["user_id"] = int(payload.get("uid", 0) or 0)
    request.session["username"] = str(payload.get("sub", ""))
    request.session["email"] = str(payload.get("email", "")).lower()
    request.session["role"] = str(payload.get("role", "user"))
    request.session["is_superadmin"] = bool(payload.get("is_superadmin", False))

    role = str(payload.get("role", "user"))
    default_target = "/chatbot" if role in ("admin", "superadmin") else "/front/events-front.html"
    target = next if next.startswith("/") else default_target
    return RedirectResponse(url=target, status_code=303)


@app.get("/auth/bridge")
def auth_bridge(request: Request, token: str = "") -> RedirectResponse:
    raw_token = unquote(token).strip()
    if not raw_token:
        return RedirectResponse(url="/login?info=invalid", status_code=303)

    try:
        payload = jwt.decode(raw_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return RedirectResponse(url="/login?info=invalid", status_code=303)

    if payload.get("type") != "access" or not payload.get("sub"):
        return RedirectResponse(url="/login?info=invalid", status_code=303)

    request.session["authenticated"] = True
    request.session["user_id"] = int(payload.get("uid", 0) or 0)
    request.session["username"] = str(payload.get("sub", ""))
    request.session["email"] = str(payload.get("email", "")).lower()
    request.session["role"] = str(payload.get("role", "user"))
    request.session["is_superadmin"] = bool(payload.get("is_superadmin", False))
    role = str(payload.get("role", "user"))
    target = "/chatbot" if role in ("admin", "superadmin") else "/front/events-front.html"
    return RedirectResponse(url=target, status_code=303)


@app.get("/register")
def register_page(request: Request) -> Response:
    if _is_authenticated(request):
        role = request.session.get("role", "user")
        if role in ("admin", "superadmin"):
            return RedirectResponse(url="/chatbot", status_code=303)
        return RedirectResponse(url="/front/events-front.html", status_code=303)

    info = request.query_params.get("info", "")
    messages = {
        "pwd_mismatch": '<div class="msg error">Les mots de passe ne correspondent pas.</div>',
        "exists": '<div class="msg error">Nom utilisateur ou email deja utilise.</div>',
        "weak_password": '<div class="msg error">Le mot de passe doit contenir au moins 8 caracteres.</div>',
        "captcha": '<div class="msg error">Verification CAPTCHA echouee. Veuillez cocher la case.</div>',
    }
    msg_html = messages.get(info, "")
    site_key = RECAPTCHA_SITE_KEY

    html_content = f"""
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Creation de compte</title>
        <script src="https://www.google.com/recaptcha/api.js" async defer></script>
        <style>
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{
                font-family: 'Barlow', sans-serif;
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                background: linear-gradient(140deg, #11161f 0%, #2a3040 65%, #e90b35 140%);
                padding: 20px;
            }}
            .card {{
                width: 100%;
                max-width: 460px;
                background: #fff;
                border-top: 5px solid #e90b35;
                box-shadow: 0 24px 60px rgba(17, 22, 31, 0.25);
                padding: 30px;
            }}
            h1 {{ font-size: 1.45em; color: #11161f; margin-bottom: 6px; text-transform: uppercase; }}
            p {{ color: #5c6575; margin-bottom: 18px; }}
            label {{ display: block; margin-bottom: 6px; color: #11161f; font-weight: 600; font-size: 0.9em; }}
            input {{ width: 100%; border: 1px solid #ccd2de; padding: 10px 12px; margin-bottom: 14px; font-size: 0.95em; }}
            input:focus {{ outline: none; border-color: #e90b35; }}
            button {{ width: 100%; border: none; background: #e90b35; color: #fff; padding: 11px; font-weight: 700; text-transform: uppercase; cursor: pointer; margin-top: 4px; }}
            button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
            .back {{ margin-top: 10px; display: block; text-align: center; color: #e90b35; text-decoration: none; font-weight: 700; }}
            .msg {{ margin-bottom: 12px; font-size: 0.88em; }}
            .error {{ color: #c62828; }}
            .captcha-wrap {{ margin-bottom: 16px; }}
        </style>
    </head>
    <body>
        <form class="card" method="post" action="/register" id="regForm">
            <h1>Creer un compte</h1>
            <p>Inscription avec verification email avant l'acces.</p>
            {msg_html}
            <label for="username">Nom d'utilisateur</label>
            <input id="username" name="username" type="text" minlength="3" required>
            <label for="email">Email</label>
            <input id="email" name="email" type="email" required>
            <label for="password">Mot de passe</label>
            <input id="password" name="password" type="password" minlength="8" required>
            <label for="confirm_password">Confirmer le mot de passe</label>
            <input id="confirm_password" name="confirm_password" type="password" minlength="8" required>
            <div class="captcha-wrap">
                <div class="g-recaptcha" data-sitekey="{site_key}" data-callback="onCaptchaDone" data-expired-callback="onCaptchaExpired"></div>
            </div>
            <button type="submit" id="submitBtn" disabled>Creer le compte</button>
            <a class="back" href="/login">Retour login</a>
        </form>
        <script>
            function onCaptchaDone() {{
                document.getElementById('submitBtn').disabled = false;
            }}
            function onCaptchaExpired() {{
                document.getElementById('submitBtn').disabled = true;
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html_content)


@app.post("/register")
async def register_submit(request: Request) -> Response:
    _ensure_not_rate_limited(request)
    form = await request.form()
    username = str(form.get("username", "")).strip()
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    confirm_password = str(form.get("confirm_password", ""))
    recaptcha_response = str(form.get("g-recaptcha-response", "")).strip()

    # Validate reCAPTCHA with Google
    if not recaptcha_response:
        return RedirectResponse(url="/register?info=captcha", status_code=303)
    try:
        data = urllib.parse.urlencode({
            "secret": RECAPTCHA_SECRET_KEY,
            "response": recaptcha_response,
        }).encode()
        req = urllib.request.Request("https://www.google.com/recaptcha/api/siteverify", data=data)
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = _json.loads(resp.read())
        if not result.get("success"):
            return RedirectResponse(url="/register?info=captcha", status_code=303)
    except Exception:
        return RedirectResponse(url="/register?info=captcha", status_code=303)

    if len(username) < 3 or len(password) < 8:
        return RedirectResponse(url="/register?info=weak_password", status_code=303)
    if password != confirm_password:
        return RedirectResponse(url="/register?info=pwd_mismatch", status_code=303)

    token = secrets.token_urlsafe(32)
    password_hash = _hash_password(password)

    try:
        with _get_db() as conn:
            conn.execute(
                """
                INSERT INTO users (username, email, password_hash, verified, verification_token)
                VALUES (?, ?, ?, 0, ?)
                """,
                (username, email, password_hash, token),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        _record_auth_failure(request)
        return RedirectResponse(url="/register?info=exists", status_code=303)

    _clear_auth_failures(request)

    verify_url = f"{request.base_url}verify-email?token={token}"
    email_sent = _send_verification_email(email, verify_url)

    safe_email = escape(email)
    safe_url = escape(verify_url)
    delivery_note = (
        "Un email de verification vient d'etre envoye."
        if email_sent
        else "SMTP non configure: utilisez le lien ci-dessous pour verifier le compte."
    )

    html_content = f"""
    <!DOCTYPE html>
    <html lang=\"fr\">
    <head>
        <meta charset=\"UTF-8\">
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
        <title>Verification requise</title>
        <style>
            body {{ font-family: 'Barlow', sans-serif; background: #f5f5f8; padding: 24px; }}
            .box {{ max-width: 700px; margin: 30px auto; background: #fff; border-top: 4px solid #e90b35; padding: 22px; box-shadow: 0 16px 36px rgba(17,22,31,0.15); }}
            h1 {{ margin-bottom: 10px; color: #11161f; text-transform: uppercase; font-size: 1.3em; }}
            p {{ color: #5b6270; margin-bottom: 12px; }}
            a.btn {{ display: inline-block; text-decoration: none; background: #e90b35; color: #fff; padding: 10px 14px; font-weight: 700; margin-top: 8px; }}
            .link {{ font-size: 0.86em; word-break: break-all; color: #1a1f2b; background: #f2f4f8; padding: 10px; margin-top: 8px; }}
        </style>
    </head>
    <body>
        <div class=\"box\">
            <h1>Compte cree</h1>
            <p>Compte enregistre pour <strong>{safe_email}</strong>.</p>
            <p>{delivery_note}</p>
            <a class=\"btn\" href=\"{safe_url}\">Verifier maintenant</a>
            <div class=\"link\">{safe_url}</div>
            <p style=\"margin-top:12px;\"><a href=\"/login\">Retour au login</a></p>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(html_content)


@app.get("/verify-email")
def verify_email(token: str = "") -> RedirectResponse:
    token = token.strip()
    if not token:
        return RedirectResponse(url="/login?info=invalid", status_code=303)

    with _get_db() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE verification_token = ? AND verified = 0",
            (token,),
        ).fetchone()
        if row is None:
            return RedirectResponse(url="/login?info=invalid", status_code=303)

        conn.execute(
            "UPDATE users SET verified = 1, verification_token = NULL WHERE id = ?",
            (row["id"],),
        )
        conn.commit()

    return RedirectResponse(url="/login?info=verified", status_code=303)


@app.get("/logout")
def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/dashboard")
def user_dashboard_entry(request: Request) -> Response:
    redirect = _require_auth_or_redirect(request)
    if redirect is not None:
        return redirect
    identity = _resolve_request_identity(request) or {}
    role = str(identity.get("role", "user"))
    if role in ("admin", "superadmin"):
        return RedirectResponse(url="/chatbot", status_code=303)
    return RedirectResponse(url="/front/events-front.html", status_code=303)


@app.get("/landing")
def landing(request: Request) -> Response:
    return RedirectResponse(url="/front/", status_code=307)


@app.get("/admin")
def admin_page(request: Request) -> Response:
    redirect = _require_superadmin_or_redirect(request)
    if redirect is not None:
        return redirect

    html_content = """
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Admin Matricules</title>
        <link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;600;700;800&display=swap" rel="stylesheet">
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body {
                font-family: 'Barlow', sans-serif;
                background: #f5f5f8;
                color: #11161f;
                display: flex;
                min-height: 100vh;
            }
            /* ---- Sidebar ---- */
            .admin-sidebar {
                width: 220px;
                min-height: 100vh;
                background: #11161f;
                color: #fff;
                display: flex;
                flex-direction: column;
                padding: 32px 0 24px 0;
                flex-shrink: 0;
            }
            .admin-sidebar h2 {
                font-size: 1.1em;
                text-transform: uppercase;
                letter-spacing: 0.08em;
                color: #e90b35;
                padding: 0 24px 24px 24px;
                border-bottom: 1px solid #2a3040;
            }
            .admin-nav {
                display: flex;
                flex-direction: column;
                margin-top: 18px;
                flex: 1;
            }
            .admin-nav a {
                text-decoration: none;
                color: #bfc5d0;
                padding: 12px 24px;
                font-size: 0.97em;
                font-weight: 600;
                transition: background 0.15s, color 0.15s;
            }
            .admin-nav a:hover { background: #1e2635; color: #fff; }
            .admin-nav a.active { background: #e90b35; color: #fff; }
            .admin-nav a.logout-link {
                margin-top: auto;
                color: #e90b35;
            }
            .admin-nav a.logout-link:hover { background: #e90b35; color: #fff; }
                        /* Face ID button */
                        .face-id-btn {
                            display: inline-flex;
                            align-items: center;
                            gap: 8px;
                            background: #11161f;
                            color: #fff;
                            border: none;
                            padding: 10px 18px;
                            font-family: 'Barlow', sans-serif;
                            font-size: 0.92em;
                            font-weight: 700;
                            cursor: pointer;
                            text-decoration: none;
                            transition: background 0.15s;
                            margin-top: 18px;
                        }
                        .face-id-btn:hover { background: #e90b35; }
                        .face-id-section {
                            margin-top: 28px;
                            display: flex;
                            align-items: center;
                            gap: 16px;
                            flex-wrap: wrap;
                        }
                        .face-id-badge { font-size: 0.85em; color: #636b7a; }
                        .face-id-badge span { font-weight: 700; }
            /* ---- Main content ---- */
            .page {
                flex: 1;
                padding: 32px;
                background: #f5f5f8;
                overflow-y: auto;
            }
            .page-card {
                background: #fff;
                border-top: 4px solid #e90b35;
                box-shadow: 0 20px 50px rgba(17, 22, 31, 0.10);
                padding: 28px;
            }
            h1 {
                font-size: 1.35em;
                text-transform: uppercase;
                letter-spacing: 0.04em;
                margin-bottom: 8px;
            }
            p { color: #5f6675; margin-bottom: 18px; }
            form {
                display: grid;
                grid-template-columns: 1fr auto;
                gap: 10px;
                margin-bottom: 16px;
            }
            input {
                border: 1px solid #ccd3dd;
                padding: 10px;
                font-size: 0.95em;
            }
            button {
                border: none;
                background: #11161f;
                color: #fff;
                padding: 10px 14px;
                cursor: pointer;
                font-weight: 700;
            }
            table {
                width: 100%;
                border-collapse: collapse;
                margin-top: 10px;
            }
            th, td {
                border-bottom: 1px solid #eceff5;
                text-align: left;
                padding: 10px 6px;
                font-size: 0.92em;
            }
            th { text-transform: uppercase; font-size: 0.76em; color: #636b7a; }
            .danger { background: #e90b35; }
            #msg { margin-top: 10px; min-height: 20px; font-size: 0.88em; }
            .badge {
                display: inline-block;
                padding: 3px 10px;
                border-radius: 12px;
                font-size: 0.8em;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }
            .badge-pending  { background: #fff3cd; color: #856404; }
            .badge-approved { background: #d4edda; color: #155724; }
            .badge-rejected { background: #f8d7da; color: #721c24; }
            .approve-btn { background: #2e7d32; color: #fff; border: none; padding: 6px 12px; cursor: pointer; font-weight: 700; font-size: 0.85em; }
            .approve-btn:hover { background: #1b5e20; }
            @media (max-width: 700px) {
                body { flex-direction: column; }
                .admin-sidebar { width: 100%; min-height: unset; flex-direction: row; padding: 12px; align-items: center; flex-wrap: wrap; }
                .admin-sidebar h2 { border-bottom: none; padding: 0 16px 0 0; }
                .admin-nav { flex-direction: row; margin-top: 0; flex-wrap: wrap; }
                .admin-nav a { padding: 8px 12px; }
                .page { padding: 14px; }
                form { grid-template-columns: 1fr; }
            }
        </style>
    </head>
    <body>
        <aside class="admin-sidebar">
            <h2>Admin Panel</h2>
            <nav class="admin-nav">
                <a href="/chatbot">Chatbot</a>
                <a href="/client">Client ML</a>
                <a class="active" href="/admin">Matricules</a>
                <a href="/events">Evenements</a>
                <a href="/admin/ratings">Evaluations</a>
                <a href="/admin/bi-dashboard">Dashboard BI</a>
                <a href="/landing">Accueil</a>
                <a href="/admin/face-setup">Face ID</a>
                <a href="/admin/messenger">Messenger</a>
                <a href="/admin/nlp">NLP Analyse</a>
                <a href="/logout" class="logout-link">Logout</a>
            </nav>
        </aside>

        <div class="page">
        <div class="page-card">
            <h1>Administration Matricules</h1>
            <p>Superadmin uniquement: matricules, roles et suppression comptes.</p>

            <form id="add-form">
                <input id="matricule-input" placeholder="Ex: LY_2016" required>
                <button type="submit">Ajouter</button>
            </form>

            <table>
                <thead>
                    <tr><th>Matricule</th><th>Date</th><th>Action</th></tr>
                </thead>
                <tbody id="rows"></tbody>
            </table>
            <div id="msg"></div>

            <h1 style="margin-top:28px;">Gestion Des Utilisateurs</h1>
            <p>Changer le role (user/admin) ou supprimer un compte.</p>

                <div class="face-id-section">
                    <a href="/admin/face-setup" class="face-id-btn">&#128065; Configurer Face ID</a>
                    <span class="face-id-badge" id="face-id-status">Vérification du statut...</span>
                </div>

            <table>
                <thead>
                    <tr><th>ID</th><th>Username</th><th>Email</th><th>Role</th><th>Verified</th><th>Action</th></tr>
                </thead>
                <tbody id="user-rows"></tbody>
            </table>
        </div>

        <script>
            async function loadRows() {
                const res = await fetch('/admin/matricules');
                if (!res.ok) return;
                const data = await res.json();
                const tbody = document.getElementById('rows');
                tbody.innerHTML = '';

                for (const row of data.matricules) {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td>${row.matricule}</td>
                        <td>${row.created_at || '-'}</td>
                        <td><button class="danger" data-m="${row.matricule}">Supprimer</button></td>
                    `;
                    tbody.appendChild(tr);
                }

                tbody.querySelectorAll('button').forEach(btn => {
                    btn.addEventListener('click', async () => {
                        const m = btn.getAttribute('data-m');
                        const r = await fetch(`/admin/matricules/${m}`, { method: 'DELETE' });
                        const msg = document.getElementById('msg');
                        if (r.ok) {
                            msg.textContent = `Matricule ${m} supprime.`;
                            msg.style.color = '#2e7d32';
                            loadRows();
                        } else {
                            msg.textContent = 'Suppression impossible.';
                            msg.style.color = '#c62828';
                        }
                    });
                });
            }

            document.getElementById('add-form').addEventListener('submit', async (e) => {
                e.preventDefault();
                const input = document.getElementById('matricule-input');
                const msg = document.getElementById('msg');
                const r = await fetch('/admin/matricules', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ matricule: input.value })
                });

                if (r.ok) {
                    msg.textContent = 'Matricule ajoute.';
                    msg.style.color = '#2e7d32';
                    input.value = '';
                    loadRows();
                } else {
                    msg.textContent = 'Ajout impossible (deja existant?).';
                    msg.style.color = '#c62828';
                }
            });

            loadRows();

            async function loadUsers() {
                const res = await fetch('/admin/users');
                if (!res.ok) return;
                const data = await res.json();
                const tbody = document.getElementById('user-rows');
                tbody.innerHTML = '';

                for (const user of data.users) {
                    const tr = document.createElement('tr');
                    const canEdit = Boolean(user.can_manage);
                    tr.innerHTML = `
                        <td>${user.id}</td>
                        <td>${user.username}</td>
                        <td>${user.email}</td>
                        <td>
                            ${canEdit
                                ? `<select data-role-id="${user.id}">
                                      <option value="user" ${user.role === 'user' ? 'selected' : ''}>user</option>
                                      <option value="admin" ${user.role === 'admin' ? 'selected' : ''}>admin</option>
                                      <option value="logistique" ${user.role === 'logistique' ? 'selected' : ''}>logistique</option>
                                  </select>`
                                : `<strong>${user.role}</strong>`}
                        </td>
                        <td>${user.verified ? 'oui' : 'non'}</td>
                        <td>
                            ${canEdit
                                ? `<button class="danger" data-del-user="${user.id}">Supprimer</button>`
                                : `<span style="color:#2e7d32;font-weight:700;">Superadmin</span>`}
                        </td>
                    `;
                    tbody.appendChild(tr);
                }

                tbody.querySelectorAll('select[data-role-id]').forEach(sel => {
                    sel.addEventListener('change', async () => {
                        const id = sel.getAttribute('data-role-id');
                        const msg = document.getElementById('msg');
                        const r = await fetch(`/admin/users/${id}/role`, {
                            method: 'PATCH',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ role: sel.value })
                        });
                        if (r.ok) {
                            msg.textContent = 'Role mis a jour.';
                            msg.style.color = '#2e7d32';
                        } else {
                            msg.textContent = 'Mise a jour role impossible.';
                            msg.style.color = '#c62828';
                            loadUsers();
                        }
                    });
                });

                tbody.querySelectorAll('button[data-del-user]').forEach(btn => {
                    btn.addEventListener('click', async () => {
                        const id = btn.getAttribute('data-del-user');
                        const msg = document.getElementById('msg');
                        const r = await fetch(`/admin/users/${id}`, { method: 'DELETE' });
                        if (r.ok) {
                            msg.textContent = `Compte ${id} supprime.`;
                            msg.style.color = '#2e7d32';
                            loadUsers();
                        } else {
                            msg.textContent = 'Suppression compte impossible.';
                            msg.style.color = '#c62828';
                        }
                    });
                });
            }

            loadUsers();
        </script>

        <script>
            (async function checkFaceIdStatus() {
                const el = document.getElementById('face-id-status');
                try {
                    const r = await fetch('/api/face-descriptor', { credentials: 'include' });
                    if (r.ok) {
                        const data = await r.json();
                        if (data.enrolled) {
                            el.innerHTML = '<span style="color:#2e7d32">&#10003; Face ID enregistré</span> — <a href="/admin/face-setup">Modifier</a>';
                        } else {
                            el.innerHTML = '<span style="color:#e90b35">&#9888; Aucun Face ID</span> — Cliquez sur le bouton pour en configurer un';
                        }
                    } else {
                        el.textContent = '';
                    }
                } catch(e) {
                    el.textContent = '';
                }
            })();
        </script>

    </div><!-- page -->
    </body>
    </html>
    """
    return HTMLResponse(html_content)


@app.get("/admin/messenger")
def admin_messenger_page(request: Request) -> Response:
    redirect = _require_superadmin_or_redirect(request)
    if redirect is not None:
        return redirect

    html_content = """
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Admin Messenger</title>
        <link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;600;700;800&display=swap" rel="stylesheet">
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body {
                font-family: 'Barlow', sans-serif;
                background: #f5f5f8;
                color: #11161f;
                display: flex;
                min-height: 100vh;
            }
            .admin-sidebar {
                width: 220px;
                min-height: 100vh;
                background: #11161f;
                color: #fff;
                display: flex;
                flex-direction: column;
                padding: 32px 0 24px 0;
                flex-shrink: 0;
            }
            .admin-sidebar h2 {
                font-size: 1.1em;
                text-transform: uppercase;
                letter-spacing: 0.08em;
                color: #e90b35;
                padding: 0 24px 24px 24px;
                border-bottom: 1px solid #2a3040;
            }
            .admin-nav {
                display: flex;
                flex-direction: column;
                margin-top: 18px;
                flex: 1;
            }
            .admin-nav a {
                text-decoration: none;
                color: #bfc5d0;
                padding: 12px 24px;
                font-size: 0.97em;
                font-weight: 600;
                transition: background 0.15s, color 0.15s;
            }
            .admin-nav a:hover { background: #1e2635; color: #fff; }
            .admin-nav a.active { background: #e90b35; color: #fff; }
            .admin-nav a.logout-link {
                margin-top: auto;
                color: #e90b35;
            }
            .admin-nav a.logout-link:hover { background: #e90b35; color: #fff; }
            .page {
                flex: 1;
                padding: 32px;
                overflow-y: auto;
            }
            .page-card {
                background: #fff;
                border-top: 4px solid #e90b35;
                box-shadow: 0 20px 50px rgba(17, 22, 31, 0.10);
                padding: 28px;
            }
            h1 {
                font-size: 1.35em;
                text-transform: uppercase;
                letter-spacing: 0.04em;
                margin-bottom: 8px;
            }
            p { color: #5f6675; margin-bottom: 18px; }
            table {
                width: 100%;
                border-collapse: collapse;
                margin-top: 10px;
            }
            th, td {
                border-bottom: 1px solid #eceff5;
                text-align: left;
                padding: 10px 6px;
                font-size: 0.92em;
            }
            th { text-transform: uppercase; font-size: 0.76em; color: #636b7a; }
            button {
                border: none;
                background: #11161f;
                color: #fff;
                padding: 10px 14px;
                cursor: pointer;
                font-weight: 700;
            }
            .danger { background: #e90b35; }
            .badge {
                display: inline-block;
                padding: 3px 10px;
                border-radius: 12px;
                font-size: 0.8em;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }
            .badge-pending  { background: #fff3cd; color: #856404; }
            .badge-approved { background: #d4edda; color: #155724; }
            .badge-rejected { background: #f8d7da; color: #721c24; }
            .approve-btn { background: #2e7d32; color: #fff; border: none; padding: 6px 12px; cursor: pointer; font-weight: 700; font-size: 0.85em; }
            .approve-btn:hover { background: #1b5e20; }
            #messenger-msg { margin-top: 10px; min-height: 20px; font-size: 0.88em; }
            @media (max-width: 700px) {
                body { flex-direction: column; }
                .admin-sidebar { width: 100%; min-height: unset; flex-direction: row; padding: 12px; align-items: center; flex-wrap: wrap; }
                .admin-sidebar h2 { border-bottom: none; padding: 0 16px 0 0; }
                .admin-nav { flex-direction: row; margin-top: 0; flex-wrap: wrap; }
                .admin-nav a { padding: 8px 12px; }
                .page { padding: 14px; }
            }
        </style>
    </head>
    <body>
        <aside class="admin-sidebar">
            <h2>Admin Panel</h2>
            <nav class="admin-nav">
                <a href="/chatbot">Chatbot</a>
                <a href="/client">Client ML</a>
                <a href="/admin">Matricules</a>
                <a href="/events">Evenements</a>
                <a href="/admin/ratings">Evaluations</a>
                <a href="/admin/bi-dashboard">Dashboard BI</a>
                <a href="/landing">Accueil</a>
                <a href="/admin/face-setup">Face ID</a>
                <a class="active" href="/admin/messenger">Messenger</a>
                <a href="/admin/nlp">NLP Analyse</a>
                <a href="/logout" class="logout-link">Logout</a>
            </nav>
        </aside>

        <div class="page">
            <div class="page-card" id="messenger-section">
                <h1>Messenger Groupe &mdash; Demandes d&rsquo;acc&egrave;s</h1>
                <p>G&eacute;rez les demandes d&rsquo;acc&egrave;s au chat Messenger groupe.</p>
                <div style="margin-bottom:12px;">
                    <button onclick="loadMessengerRequests()">Actualiser</button>
                </div>
                <table id="messenger-table">
                    <thead>
                        <tr>
                            <th>Utilisateur</th>
                            <th>Email</th>
                            <th>Statut</th>
                            <th>Demand&eacute; le</th>
                            <th>Trait&eacute; par</th>
                            <th>Trait&eacute; le</th>
                            <th>Note</th>
                            <th>Action</th>
                        </tr>
                    </thead>
                    <tbody id="messenger-rows"></tbody>
                </table>
                <div id="messenger-msg"></div>
            </div>
        </div>

        <script>
            function escHtmlAdmin(v) {
                return String(v || '')
                    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
                    .replace(/>/g,'&gt;').replace(/"/g,'&quot;')
                    .replace(/'/g,'&#39;');
            }

            async function loadMessengerRequests() {
                const tbody = document.getElementById('messenger-rows');
                const msgEl = document.getElementById('messenger-msg');
                msgEl.textContent = '';
                try {
                    const res = await fetch('/api/messenger/requests', { credentials: 'include' });
                    if (!res.ok) {
                        msgEl.textContent = 'Erreur chargement (' + res.status + ')';
                        msgEl.style.color = '#c62828';
                        return;
                    }
                    const data = await res.json();
                    if (!data.requests || !data.requests.length) {
                        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#636b7a;padding:18px;">Aucune demande pour le moment</td></tr>';
                        return;
                    }
                    tbody.innerHTML = '';
                    for (const req of data.requests) {
                        const statusBadge = req.status === 'pending'
                            ? '<span class="badge badge-pending">En attente</span>'
                            : req.status === 'approved'
                                ? '<span class="badge badge-approved">Approuv&eacute;</span>'
                                : '<span class="badge badge-rejected">Rejet&eacute;</span>';
                        const actions = req.status === 'pending'
                            ? `<button class="approve-btn" data-uid="${req.user_id}">Approuver</button>
                               <button class="danger" data-uid="${req.user_id}" style="margin-left:6px;padding:6px 12px;font-size:0.85em;">Rejeter</button>`
                            : '<span style="color:#636b7a;font-size:0.88em;">&mdash;</span>';
                        const tr = document.createElement('tr');
                        tr.innerHTML = `
                            <td>${escHtmlAdmin(req.username)}</td>
                            <td>${escHtmlAdmin(req.email)}</td>
                            <td>${statusBadge}</td>
                            <td>${escHtmlAdmin(req.requested_at || '\u2014')}</td>
                            <td>${escHtmlAdmin(req.reviewed_by_username || '\u2014')}</td>
                            <td>${escHtmlAdmin(req.reviewed_at || '\u2014')}</td>
                            <td>${escHtmlAdmin(req.review_note || '\u2014')}</td>
                            <td>${actions}</td>
                        `;
                        tbody.appendChild(tr);
                    }
                    tbody.querySelectorAll('.approve-btn').forEach(btn => {
                        btn.addEventListener('click', () => reviewMessengerRequest(btn.getAttribute('data-uid'), 'approve', ''));
                    });
                    tbody.querySelectorAll('.danger[data-uid]').forEach(btn => {
                        btn.addEventListener('click', () => {
                            const note = prompt('Motif du rejet (optionnel):') || '';
                            reviewMessengerRequest(btn.getAttribute('data-uid'), 'reject', note);
                        });
                    });
                } catch(e) {
                    msgEl.textContent = 'Erreur: ' + e.message;
                    msgEl.style.color = '#c62828';
                }
            }

            async function reviewMessengerRequest(userId, action, note) {
                const msgEl = document.getElementById('messenger-msg');
                try {
                    const res = await fetch('/api/messenger/requests/' + userId + '/review', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        credentials: 'include',
                        body: JSON.stringify({ action: action, note: note })
                    });
                    const payload = await res.json().catch(() => ({}));
                    if (!res.ok) {
                        msgEl.textContent = payload.detail || ('Erreur ' + res.status);
                        msgEl.style.color = '#c62828';
                        return;
                    }
                    msgEl.textContent = action === 'approve' ? 'Demande approuvee.' : 'Demande rejetee.';
                    msgEl.style.color = '#2e7d32';
                    loadMessengerRequests();
                } catch(e) {
                    msgEl.textContent = 'Erreur: ' + e.message;
                    msgEl.style.color = '#c62828';
                }
            }

            loadMessengerRequests();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html_content)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/admin/nlp")
def admin_nlp_page(request: Request) -> Response:
    redirect = _require_superadmin_or_redirect(request)
    if redirect is not None:
        return redirect

    html_content = """
    <!DOCTYPE html>
    <html lang="fr">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Admin NLP</title>
        <link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;600;700;800&display=swap" rel="stylesheet">
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body {
                font-family: 'Barlow', sans-serif;
                background: #f5f5f8;
                color: #11161f;
                display: flex;
                min-height: 100vh;
            }
            .admin-sidebar {
                width: 220px;
                min-height: 100vh;
                background: #11161f;
                color: #fff;
                display: flex;
                flex-direction: column;
                padding: 32px 0 24px 0;
                flex-shrink: 0;
            }
            .admin-sidebar h2 {
                font-size: 1.1em;
                text-transform: uppercase;
                letter-spacing: 0.08em;
                color: #e90b35;
                padding: 0 24px 24px 24px;
                border-bottom: 1px solid #2a3040;
            }
            .admin-nav {
                display: flex;
                flex-direction: column;
                margin-top: 18px;
                flex: 1;
            }
            .admin-nav a {
                text-decoration: none;
                color: #bfc5d0;
                padding: 12px 24px;
                font-size: 0.97em;
                font-weight: 600;
                transition: background 0.15s, color 0.15s;
            }
            .admin-nav a:hover { background: #1e2635; color: #fff; }
            .admin-nav a.active { background: #e90b35; color: #fff; }
            .admin-nav a.logout-link {
                margin-top: auto;
                color: #e90b35;
            }
            .admin-nav a.logout-link:hover { background: #e90b35; color: #fff; }
            .page {
                flex: 1;
                padding: 36px;
            }
            .page-card {
                background: #fff;
                border-top: 4px solid #11161f;
                box-shadow: 0 20px 45px rgba(17,22,31,0.10);
                padding: 24px;
                margin-bottom: 22px;
            }
            .page-card h1 {
                font-size: 1.45em;
                text-transform: uppercase;
                margin-bottom: 8px;
            }
            .page-card p {
                font-size: 0.95em;
                color: #636b7a;
                margin-bottom: 14px;
            }
            button {
                border: none;
                background: #11161f;
                color: #fff;
                font-family: inherit;
                font-weight: 700;
                font-size: 0.88em;
                padding: 8px 16px;
                cursor: pointer;
            }
            button:hover { opacity: 0.92; }
            table {
                width: 100%;
                border-collapse: collapse;
                font-size: 0.88em;
            }
            th {
                padding: 8px;
                text-align: left;
                border-bottom: 2px solid #dde3ee;
                background: #f0f3f8;
            }
            td {
                padding: 7px 8px;
                border-bottom: 1px solid #edf1f7;
            }
            @media (max-width: 960px) {
                body { flex-direction: column; }
                .admin-sidebar {
                    width: 100%;
                    min-height: auto;
                    padding: 16px 0;
                }
                .page { padding: 16px; }
            }
        </style>
    </head>
    <body>
        <aside class="admin-sidebar">
            <h2>Admin Panel</h2>
            <nav class="admin-nav">
                <a href="/chatbot">Chatbot</a>
                <a href="/client">Client ML</a>
                <a href="/admin">Matricules</a>
                <a href="/events">Evenements</a>
                <a href="/admin/ratings">Evaluations</a>
                <a href="/admin/bi-dashboard">Dashboard BI</a>
                <a href="/landing">Accueil</a>
                <a href="/admin/face-setup">Face ID</a>
                <a class="active" href="/admin/nlp">NLP Analyse</a>
                <a href="/logout" class="logout-link">Logout</a>
            </nav>
        </aside>

        <div class="page">
            <div class="page-card" id="nlp-section">
                <h1>Analyse NLP Discussions Messenger</h1>
                <p>Detection des tensions, leadership naturel et ambiance generale.</p>
                <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;">
                    <button id="nlp-run-btn">Analyser maintenant</button>
                    <button id="nlp-alert-btn" style="background:#b71c1c;">Envoyer alerte par mail</button>
                </div>
                <div id="nlp-result" style="display:none;">
                    <div id="nlp-cards" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:18px;"></div>
                    <div id="nlp-triggers-box" style="display:none;background:#fff3e0;border:1px solid #ffb300;padding:12px 16px;margin-bottom:14px;">
                        <strong>Mots declencheurs de tensions:</strong>
                        <span id="nlp-triggers"></span>
                    </div>
                    <table id="nlp-user-table">
                        <thead><tr>
                            <th>Utilisateur</th>
                            <th style="text-align:center;">Messages</th>
                            <th style="text-align:center;">Score tension</th>
                            <th style="text-align:center;">Score positif</th>
                            <th style="text-align:center;">Leadership</th>
                            <th style="text-align:center;">Vocal</th>
                        </tr></thead>
                        <tbody id="nlp-user-rows"></tbody>
                    </table>
                </div>
                <div id="nlp-msg" style="margin-top:10px;min-height:20px;font-size:0.88em;"></div>
            </div>
        </div>

        <script>
            function escHtmlAdmin(v) {
                return String(v || '')
                    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
                    .replace(/>/g,'&gt;').replace(/"/g,'&quot;')
                    .replace(/'/g,'&#39;');
            }

            (function() {
                const TENSION_COLORS = {
                    'calme': '#2e7d32',
                    'moderee': '#f57f17',
                    'modérée': '#f57f17',
                    'elevee': '#e65100',
                    'élevée': '#e65100',
                    'critique': '#b71c1c',
                };
                const AMBIANCE_COLORS = {
                    'positive': '#2e7d32',
                    'neutre': '#546e7a',
                    'mitigee': '#f57f17',
                    'mitigée': '#f57f17',
                    'tendue': '#b71c1c',
                };

                function makeCard(label, value, color) {
                    return `<div style="background:#fff;border:1px solid #dde3ee;padding:12px 14px;border-radius:6px;text-align:center;">
                        <div style="font-size:1.5rem;font-weight:800;color:${color};">${value}</div>
                        <div style="font-size:0.78rem;color:#636b7a;margin-top:4px;">${label}</div>
                    </div>`;
                }

                async function runNlpAnalysis() {
                    const btn = document.getElementById('nlp-run-btn');
                    const msgEl = document.getElementById('nlp-msg');
                    btn.disabled = true;
                    btn.textContent = 'Analyse en cours...';
                    msgEl.textContent = '';
                    try {
                        const res = await fetch('/api/messenger/analysis', { credentials: 'include' });
                        if (!res.ok) throw new Error('Erreur ' + res.status);
                        const d = await res.json();
                        renderNlpResult(d);
                    } catch(e) {
                        msgEl.textContent = 'Erreur: ' + e.message;
                        msgEl.style.color = '#c62828';
                    } finally {
                        btn.disabled = false;
                        btn.textContent = 'Analyser maintenant';
                    }
                }

                function renderNlpResult(d) {
                    document.getElementById('nlp-result').style.display = 'block';
                    const tc = TENSION_COLORS[d.tension_level] || '#546e7a';
                    const ac = AMBIANCE_COLORS[d.ambiance] || '#546e7a';
                    document.getElementById('nlp-cards').innerHTML = [
                        makeCard('Messages analyses', d.total_messages, '#11161f'),
                        makeCard('Niveau tension', (d.tension_level || '').toUpperCase(), tc),
                        makeCard('Score tension', d.tension_score + '‰', tc),
                        makeCard('Ambiance', (d.ambiance || '').toUpperCase(), ac),
                        makeCard('Positif', d.positive_ratio + '%', '#2e7d32'),
                        makeCard('Negatif', d.negative_ratio + '%', '#b71c1c'),
                        makeCard('Leader naturel', d.leader || '-', '#1565c0'),
                    ].join('');

                    const trigBox = document.getElementById('nlp-triggers-box');
                    if (d.tension_triggers && d.tension_triggers.length) {
                        document.getElementById('nlp-triggers').textContent = ' ' + d.tension_triggers.join(', ');
                        trigBox.style.display = 'block';
                    } else {
                        trigBox.style.display = 'none';
                    }

                    const tbody = document.getElementById('nlp-user-rows');
                    tbody.innerHTML = (d.user_stats || []).map(function(u) {
                        const isLeader = u.username === d.leader;
                        return `<tr style="${isLeader ? 'background:#e8f5e9;' : ''}">
                            <td>${escHtmlAdmin(u.username)}${isLeader ? ' <span style="font-size:0.75rem;background:#2e7d32;color:#fff;padding:2px 6px;border-radius:999px;">Leader</span>' : ''}</td>
                            <td style="text-align:center;">${u.message_count}</td>
                            <td style="text-align:center;color:${u.tension_hits > 0 ? '#b71c1c' : '#2e7d32'};">${u.tension_hits}</td>
                            <td style="text-align:center;color:#2e7d32;">${u.positive_hits}</td>
                            <td style="text-align:center;">${u.leadership_score}</td>
                            <td style="text-align:center;">${u.has_audio ? 'Audio ' + u.has_audio : '-'}</td>
                        </tr>`;
                    }).join('');

                    const msgEl = document.getElementById('nlp-msg');
                    if (d.alert_needed) {
                        msgEl.innerHTML = '<span style="color:#b71c1c;font-weight:700;">Tensions detectees. Vous pouvez notifier l admin par email.</span>';
                    } else {
                        msgEl.innerHTML = '<span style="color:#2e7d32;">Analyse terminee. Aucune alerte critique.</span>';
                    }
                }

                async function sendAlertEmail() {
                    const btn = document.getElementById('nlp-alert-btn');
                    const msgEl = document.getElementById('nlp-msg');
                    btn.disabled = true;
                    btn.textContent = 'Envoi...';
                    try {
                        const res = await fetch('/api/messenger/analysis/send-alert', { method: 'POST', credentials: 'include' });
                        if (!res.ok) throw new Error('Erreur ' + res.status);
                        const d = await res.json();
                        renderNlpResult(d.analysis);
                        msgEl.innerHTML = d.sent
                            ? '<span style="color:#2e7d32;">Email alerte envoye a l admin.</span>'
                            : '<span style="color:#f57f17;">Email non configure (SUPERADMIN_EMAIL manquant).</span>';
                    } catch(e) {
                        msgEl.textContent = 'Erreur: ' + e.message;
                        msgEl.style.color = '#c62828';
                    } finally {
                        btn.disabled = false;
                        btn.textContent = 'Envoyer alerte par mail';
                    }
                }

                document.getElementById('nlp-run-btn').addEventListener('click', runNlpAnalysis);
                document.getElementById('nlp-alert-btn').addEventListener('click', sendAlertEmail);
            })();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html_content)


# ---------------------------------------------------------------------------
# Matricule endpoints
# ---------------------------------------------------------------------------

@app.post("/validate-matricule")
def validate_matricule(payload: MatriculeValidateRequest, request: Request) -> dict[str, Any]:
    _require_auth_api(request)
    saisie = payload.matricule.strip().upper()
    with _get_db() as conn:
        row = conn.execute(
            "SELECT matricule FROM matricules WHERE matricule = ? AND active = 1",
            (saisie,),
        ).fetchone()
    if row:
        return {"valid": True, "matricule": row["matricule"]}
    raise HTTPException(status_code=403, detail="Matricule non autorisé")


@app.get("/admin/matricules")
def list_matricules(request: Request) -> dict[str, Any]:
    _require_superadmin_api(request)
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT matricule, created_at, active FROM matricules ORDER BY created_at"
        ).fetchall()
    return {"matricules": [dict(r) for r in rows]}


@app.post("/admin/matricules")
def add_matricule(payload: MatriculeAddRequest, request: Request) -> dict[str, Any]:
    _require_superadmin_api(request)
    matricule = payload.matricule.strip().upper()
    if not matricule:
        raise HTTPException(status_code=422, detail="Matricule vide")
    try:
        with _get_db() as conn:
            conn.execute(
                "INSERT INTO matricules (matricule) VALUES (?)", (matricule,)
            )
            conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Matricule déjà existant")
    return {"message": f"Matricule {matricule} ajouté", "matricule": matricule}


@app.delete("/admin/matricules/{matricule}")
def delete_matricule(matricule: str, request: Request) -> dict[str, Any]:
    _require_superadmin_api(request)
    m = matricule.strip().upper()
    with _get_db() as conn:
        result = conn.execute(
            "DELETE FROM matricules WHERE matricule = ?", (m,)
        )
        conn.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Matricule introuvable")
    return {"message": f"Matricule {m} supprimé"}


@app.get("/admin/users")
def list_users(request: Request) -> dict[str, Any]:
    _require_superadmin_api(request)
    requester = _resolve_request_identity(request) or {}
    requester_id = int(requester.get("id", 0) or 0)
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT id, username, email, role, verified, created_at FROM users ORDER BY id"
        ).fetchall()
    users = []
    for r in rows:
        role = str(r["role"])
        target_identity = {
            "id": int(r["id"]),
            "email": str(r["email"]).lower(),
            "role": role,
        }
        is_superadmin = _is_superadmin_identity(target_identity)
        users.append(
            {
                "id": int(r["id"]),
                "username": str(r["username"]),
                "email": str(r["email"]),
                "role": "superadmin" if is_superadmin else role,
                "is_superadmin": is_superadmin,
                "can_manage": (not is_superadmin) and int(r["id"]) != requester_id,
                "verified": int(r["verified"]) == 1,
                "created_at": r["created_at"],
            }
        )
    return {"users": users}


@app.patch("/admin/users/{user_id}/role")
def update_user_role(user_id: int, payload: UserRoleUpdateRequest, request: Request) -> dict[str, Any]:
    actor = _require_superadmin_api(request)
    actor_id = int(actor.get("id", 0) or 0)

    with _get_db() as conn:
        row = conn.execute("SELECT id, email, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Compte introuvable")
        target_identity = {
            "id": int(row["id"]),
            "email": str(row["email"]).lower(),
            "role": str(row["role"]),
        }
        if _is_superadmin_identity(target_identity):
            raise HTTPException(status_code=403, detail="Role du superadmin non modifiable")
        if int(row["id"]) == actor_id:
            raise HTTPException(status_code=403, detail="Impossible de modifier votre propre role")
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (payload.role, user_id))
        conn.commit()
    return {"message": "Role mis a jour", "user_id": user_id, "role": payload.role}


@app.delete("/admin/users/{user_id}")
def delete_user(user_id: int, request: Request) -> dict[str, Any]:
    actor = _require_superadmin_api(request)
    actor_id = int(actor.get("id", 0) or 0)

    with _get_db() as conn:
        row = conn.execute("SELECT id, email, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Compte introuvable")
        target_identity = {
            "id": int(row["id"]),
            "email": str(row["email"]).lower(),
            "role": str(row["role"]),
        }
        if _is_superadmin_identity(target_identity):
            raise HTTPException(status_code=403, detail="Superadmin non supprimable")
        if int(row["id"]) == actor_id:
            raise HTTPException(status_code=403, detail="Impossible de supprimer votre propre compte")
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    return {"message": "Compte supprime", "user_id": user_id}


@app.get("/admin/events")
def list_events(request: Request) -> dict[str, Any]:
    _require_superadmin_api(request)
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                e.id, e.title, e.event_date, e.location, e.description, e.image_path,
                e.created_at, e.updated_at,
                COALESCE(s.avg_rating, 0) AS avg_rating,
                COALESCE(s.rating_count, 0) AS rating_count,
                COALESCE(f.favorite_count, 0) AS favorite_count
            FROM events e
            LEFT JOIN (
                SELECT event_id, ROUND(AVG(stars), 2) AS avg_rating, COUNT(*) AS rating_count
                FROM event_ratings
                GROUP BY event_id
            ) s ON s.event_id = e.id
            LEFT JOIN (
                SELECT event_id, COUNT(*) AS favorite_count
                FROM event_favorites
                GROUP BY event_id
            ) f ON f.event_id = e.id
            ORDER BY COALESCE(e.event_date, e.created_at) DESC, e.id DESC
            """
        ).fetchall()
    events = []
    for r in rows:
        item = dict(r)
        item["avg_rating"] = round(float(item.get("avg_rating") or 0), 2)
        item["rating_count"] = int(item.get("rating_count") or 0)
        item["favorite_count"] = int(item.get("favorite_count") or 0)
        events.append(item)
    return {"events": events}


@app.get("/public/events")
def list_public_events(request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    _dispatch_event_reminders()
    current_user_id = int(identity.get("id", 0) or 0)
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                e.id,
                e.title,
                e.event_date,
                e.location,
                e.description,
                e.image_path,
                COALESCE(stats.avg_rating, 0) AS avg_rating,
                COALESCE(stats.rating_count, 0) AS rating_count,
                COALESCE(fav.favorite_count, 0) AS favorite_count,
                (
                    SELECT er2.stars
                    FROM event_ratings er2
                    WHERE er2.event_id = e.id AND er2.user_id = ?
                    LIMIT 1
                ) AS my_rating,
                (
                    SELECT 1
                    FROM event_favorites ef2
                    WHERE ef2.event_id = e.id AND ef2.user_id = ?
                    LIMIT 1
                ) AS is_favorite
            FROM events e
            LEFT JOIN (
                SELECT event_id, AVG(stars) AS avg_rating, COUNT(*) AS rating_count
                FROM event_ratings
                GROUP BY event_id
            ) stats ON stats.event_id = e.id
            LEFT JOIN (
                SELECT event_id, COUNT(*) AS favorite_count
                FROM event_favorites
                GROUP BY event_id
            ) fav ON fav.event_id = e.id
            ORDER BY COALESCE(e.event_date, e.created_at) DESC, e.id DESC
            LIMIT 100
            """
            ,
            (current_user_id, current_user_id),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        item["image_url"] = item.get("image_path")
        item["avg_rating"] = round(float(item.get("avg_rating") or 0), 2)
        item["rating_count"] = int(item.get("rating_count") or 0)
        item["favorite_count"] = int(item.get("favorite_count") or 0)
        item["my_rating"] = int(item["my_rating"]) if item.get("my_rating") is not None else None
        item["is_favorite"] = bool(item.get("is_favorite"))
        events.append(item)
    return {"events": events}


@app.post("/public/events/{event_id}/favorite")
def toggle_event_favorite(event_id: int, payload: EventFavoriteRequest, request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    user_id = int(identity.get("id", 0) or 0)
    username = str(identity.get("username", "") or "").strip() or "Utilisateur"
    email = str(identity.get("email", "") or "").strip().lower()
    action = str(payload.action or "toggle")

    with _get_db() as conn:
        event_row = conn.execute(
            "SELECT id, title FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if event_row is None:
            raise HTTPException(status_code=404, detail="Evenement introuvable")

        existing = conn.execute(
            "SELECT id FROM event_favorites WHERE event_id = ? AND user_id = ?",
            (event_id, user_id),
        ).fetchone()

        is_favorite = existing is not None
        if action == "add" and not is_favorite:
            conn.execute(
                """
                INSERT INTO event_favorites (event_id, user_id, username, email)
                VALUES (?, ?, ?, ?)
                """,
                (event_id, user_id, username, email),
            )
            is_favorite = True
        elif action == "remove" and is_favorite:
            conn.execute(
                "DELETE FROM event_favorites WHERE event_id = ? AND user_id = ?",
                (event_id, user_id),
            )
            is_favorite = False
        elif action == "toggle":
            if is_favorite:
                conn.execute(
                    "DELETE FROM event_favorites WHERE event_id = ? AND user_id = ?",
                    (event_id, user_id),
                )
                is_favorite = False
            else:
                conn.execute(
                    """
                    INSERT INTO event_favorites (event_id, user_id, username, email)
                    VALUES (?, ?, ?, ?)
                    """,
                    (event_id, user_id, username, email),
                )
                is_favorite = True

        stats = conn.execute(
            "SELECT COUNT(*) AS favorite_count FROM event_favorites WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        conn.commit()

    return {
        "message": "Favori mis a jour",
        "event_id": event_id,
        "is_favorite": is_favorite,
        "favorite_count": int(stats["favorite_count"] or 0),
    }


@app.get("/api/favorites")
def list_my_favorites(request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    user_id = int(identity.get("id", 0) or 0)
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                e.id,
                e.title,
                e.event_date,
                e.location,
                e.description,
                e.image_path,
                ef.created_at AS favorited_at
            FROM event_favorites ef
            JOIN events e ON e.id = ef.event_id
            WHERE ef.user_id = ?
            ORDER BY ef.created_at DESC, ef.id DESC
            """,
            (user_id,),
        ).fetchall()
    favorites = []
    for r in rows:
        item = dict(r)
        item["image_url"] = item.get("image_path")
        favorites.append(item)
    return {"favorites": favorites}


@app.post("/public/events/{event_id}/rating")
def rate_event(event_id: int, payload: EventRatingRequest, request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    user_id = int(identity.get("id", 0) or 0)
    username = str(identity.get("username", "")).strip() or "Utilisateur"
    email = str(identity.get("email", "")).strip().lower()
    stars = int(payload.stars)

    with _get_db() as conn:
        event_row = conn.execute(
            "SELECT id, title FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if event_row is None:
            raise HTTPException(status_code=404, detail="Evenement introuvable")

        conn.execute(
            """
            INSERT INTO event_ratings (event_id, user_id, username, email, stars, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(event_id, user_id)
            DO UPDATE SET
                stars = excluded.stars,
                username = excluded.username,
                email = excluded.email,
                updated_at = datetime('now')
            """,
            (event_id, user_id, username, email, stars),
        )

        stats = conn.execute(
            """
            SELECT ROUND(AVG(stars), 2) AS avg_rating, COUNT(*) AS rating_count
            FROM event_ratings
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        conn.commit()

    event_title = str(event_row["title"])
    recipients = _get_admin_notification_recipients()
    _send_event_rating_notification(recipients, event_title, username, stars)

    return {
        "message": "Evaluation enregistree",
        "event_id": event_id,
        "my_rating": stars,
        "avg_rating": float(stats["avg_rating"] or 0),
        "rating_count": int(stats["rating_count"] or 0),
    }


@app.get("/public/events/{event_id}/comments")
def list_event_comments(event_id: int, request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    current_user_id = int(identity.get("id", 0) or 0)

    with _get_db() as conn:
        event_row = conn.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone()
        if event_row is None:
            raise HTTPException(status_code=404, detail="Evenement introuvable")

        rows = conn.execute(
            """
            SELECT
                c.id,
                c.event_id,
                c.parent_comment_id,
                c.user_id,
                c.username,
                c.email,
                c.content,
                c.is_masked,
                c.created_at,
                c.updated_at,
                COALESCE(SUM(CASE WHEN r.reaction = 'like' THEN 1 ELSE 0 END), 0) AS like_count,
                COALESCE(SUM(CASE WHEN r.reaction = 'dislike' THEN 1 ELSE 0 END), 0) AS dislike_count,
                (
                    SELECT r2.reaction
                    FROM comment_reactions r2
                    WHERE r2.comment_id = c.id AND r2.user_id = ?
                    LIMIT 1
                ) AS my_reaction
            FROM event_comments c
            LEFT JOIN comment_reactions r ON r.comment_id = c.id
            WHERE c.event_id = ?
            GROUP BY c.id
            ORDER BY COALESCE(c.updated_at, c.created_at) DESC, c.id DESC
            LIMIT 100
            """,
            (current_user_id, event_id),
        ).fetchall()

    comments = []
    for r in rows:
        item = dict(r)
        item["like_count"] = int(item.get("like_count") or 0)
        item["dislike_count"] = int(item.get("dislike_count") or 0)
        comments.append(item)

    return {"event_id": event_id, "comments": comments}


@app.post("/public/events/{event_id}/comments")
def add_event_comment(event_id: int, payload: EventCommentRequest, request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    user_id = int(identity.get("id", 0) or 0)
    username = str(identity.get("username", "")).strip() or "Utilisateur"
    email = str(identity.get("email", "")).strip().lower()
    content = payload.content.strip()
    parent_comment_id = int(payload.parent_comment_id or 0) or None
    if not content:
        raise HTTPException(status_code=422, detail="Commentaire vide")
    matched_badword = _find_badword(content)
    is_masked = 1 if matched_badword else 0
    stored_content = MASKED_COMMENT_TEXT if matched_badword else content
    moderation_reason = "badword" if matched_badword else None

    with _get_db() as conn:
        event_row = conn.execute("SELECT id, title FROM events WHERE id = ?", (event_id,)).fetchone()
        if event_row is None:
            raise HTTPException(status_code=404, detail="Evenement introuvable")

        parent_row = None
        if parent_comment_id is not None:
            parent_row = conn.execute(
                """
                SELECT id, user_id, username, content, original_content
                FROM event_comments
                WHERE id = ? AND event_id = ?
                """,
                (parent_comment_id, event_id),
            ).fetchone()
            if parent_row is None:
                raise HTTPException(status_code=404, detail="Commentaire parent introuvable")

        cur = conn.execute(
            """
            INSERT INTO event_comments (
                event_id, parent_comment_id, user_id, username, email, content, original_content,
                is_masked, moderation_reason, matched_badword, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                event_id,
                parent_comment_id,
                user_id,
                username,
                email,
                stored_content,
                content,
                is_masked,
                moderation_reason,
                matched_badword,
            ),
        )
        comment_id = int(cur.lastrowid or 0)
        row = conn.execute(
            """
            SELECT
                id, event_id, parent_comment_id, user_id, username, email, content, original_content,
                is_masked, moderation_reason, matched_badword, created_at, updated_at
            FROM event_comments
            WHERE id = ?
            """,
            (comment_id,),
        ).fetchone()

        if matched_badword:
            _create_user_notification(
                conn,
                user_id,
                "comment_flagged",
                "Commentaire masque",
                f"Votre commentaire sur '{event_row['title']}' a ete masque pour contenu inapproprie.",
                event_id=event_id,
                comment_id=comment_id,
                actor_user_id=user_id,
                actor_username=username,
            )

        if parent_row is not None and int(parent_row["user_id"] or 0) != user_id:
            excerpt = str(parent_row["original_content"] or parent_row["content"] or "").strip()[:120]
            _create_user_notification(
                conn,
                int(parent_row["user_id"]),
                "comment_reply",
                "Nouvelle reponse a votre commentaire",
                f"{username} a repondu a votre commentaire: {excerpt}",
                event_id=event_id,
                comment_id=comment_id,
                actor_user_id=user_id,
                actor_username=username,
            )
        conn.commit()

    recipients = _get_admin_notification_recipients()
    if matched_badword:
        _send_flagged_comment_notification(
            recipients,
            str(event_row["title"]),
            username,
            content,
            matched_badword,
        )
    else:
        _send_event_comment_notification(recipients, str(event_row["title"]), username, content)

    comment = dict(row)
    comment["like_count"] = 0
    comment["dislike_count"] = 0
    comment["my_reaction"] = None
    return {"message": "Commentaire ajoute", "comment": comment}


@app.post("/public/comments/{comment_id}/reaction")
def react_to_comment(comment_id: int, payload: CommentReactionRequest, request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    user_id = int(identity.get("id", 0) or 0)
    reactor = str(identity.get("username", "")).strip() or "Utilisateur"
    reaction = payload.reaction

    with _get_db() as conn:
        comment_row = conn.execute(
            """
            SELECT c.id, c.event_id, c.user_id, c.username, c.content, e.title AS event_title
            FROM event_comments c
            JOIN events e ON e.id = c.event_id
            WHERE c.id = ?
            """,
            (comment_id,),
        ).fetchone()
        if comment_row is None:
            raise HTTPException(status_code=404, detail="Commentaire introuvable")

        if reaction == "none":
            conn.execute(
                "DELETE FROM comment_reactions WHERE comment_id = ? AND user_id = ?",
                (comment_id, user_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO comment_reactions (comment_id, user_id, reaction, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(comment_id, user_id)
                DO UPDATE SET
                    reaction = excluded.reaction,
                    updated_at = datetime('now')
                """,
                (comment_id, user_id, reaction),
            )

            owner_id = int(comment_row["user_id"] or 0)
            if owner_id and owner_id != user_id:
                _create_user_notification(
                    conn,
                    owner_id,
                    "comment_reaction",
                    "Nouvelle reaction sur votre commentaire",
                    f"{reactor} a mis {reaction} sur votre commentaire dans '{comment_row['event_title']}'.",
                    event_id=int(comment_row["event_id"]),
                    comment_id=comment_id,
                    actor_user_id=user_id,
                    actor_username=reactor,
                )

        stats = _get_comment_reaction_stats(conn, comment_id, user_id)
        conn.commit()

    if reaction in {"like", "dislike"}:
        recipients = _get_admin_notification_recipients()
        _send_comment_reaction_notification(
            recipients,
            str(comment_row["event_title"]),
            reactor,
            reaction,
            str(comment_row["username"] or "Utilisateur"),
            str(comment_row["content"] or ""),
        )

    return {
        "comment_id": comment_id,
        "event_id": int(comment_row["event_id"]),
        "my_reaction": stats["my_reaction"],
        "like_count": stats["like_count"],
        "dislike_count": stats["dislike_count"],
    }


@app.get("/api/notifications")
def list_user_notifications(request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    _dispatch_event_reminders()
    user_id = int(identity.get("id", 0) or 0)
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, notification_type, title, message, event_id, comment_id,
                   actor_user_id, actor_username, is_read, created_at
            FROM user_notifications
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 100
            """,
            (user_id,),
        ).fetchall()
    return {"notifications": [dict(r) for r in rows]}


@app.post("/api/notifications/read-all")
def read_all_notifications(request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    user_id = int(identity.get("id", 0) or 0)
    with _get_db() as conn:
        conn.execute(
            "UPDATE user_notifications SET is_read = 1 WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
    return {"message": "Notifications marquees comme lues"}


@app.get("/api/me")
def current_user_info(request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    return {
        "id": int(identity.get("id", 0) or 0),
        "username": str(identity.get("username", "") or ""),
        "email": str(identity.get("email", "") or ""),
        "role": str(identity.get("role", "user") or "user"),
        "is_superadmin": bool(identity.get("is_superadmin", False)),
    }


@app.get("/api/messenger/access-status")
def messenger_access_status(request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    user_id = int(identity.get("id", 0) or 0)

    with _get_db() as conn:
        row = conn.execute(
            """
            SELECT status, requested_at, reviewed_at, reviewed_by_user_id, review_note
            FROM messenger_access_requests
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

    if row is None:
        return {
            "status": "not_requested",
            "can_request": True,
            "can_chat": False,
        }

    status = str(row["status"] or "pending")
    return {
        "status": status,
        "requested_at": row["requested_at"],
        "reviewed_at": row["reviewed_at"],
        "reviewed_by_user_id": row["reviewed_by_user_id"],
        "review_note": row["review_note"],
        "can_request": status in {"rejected"},
        "can_chat": status == "approved",
    }


@app.post("/api/messenger/access-request")
def create_messenger_access_request(request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    user_id = int(identity.get("id", 0) or 0)
    username = str(identity.get("username", "") or "Utilisateur")

    with _get_db() as conn:
        existing = conn.execute(
            "SELECT status FROM messenger_access_requests WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO messenger_access_requests (user_id, status, requested_at, reviewed_at, reviewed_by_user_id, review_note)
                VALUES (?, 'pending', datetime('now'), NULL, NULL, NULL)
                """,
                (user_id,),
            )
        else:
            status = str(existing["status"] or "pending")
            if status == "approved":
                return {"status": "approved", "message": "Acces deja approuve"}
            conn.execute(
                """
                UPDATE messenger_access_requests
                SET status = 'pending', requested_at = datetime('now'), reviewed_at = NULL, reviewed_by_user_id = NULL, review_note = NULL
                WHERE user_id = ?
                """,
                (user_id,),
            )

        admin_rows = conn.execute(
            "SELECT id FROM users WHERE role IN ('admin', 'superadmin') AND verified = 1"
        ).fetchall()
        for admin in admin_rows:
            admin_id = int(admin["id"] or 0)
            if admin_id <= 0:
                continue
            _create_user_notification(
                conn,
                admin_id,
                "messenger_access_request",
                "Nouvelle demande Messenger",
                f"{username} demande l'acces au Messenger groupe.",
                actor_user_id=user_id,
                actor_username=username,
            )
        conn.commit()

    return {"status": "pending", "message": "Demande envoyee a l'admin"}


@app.get("/api/messenger/requests")
def list_messenger_access_requests(request: Request) -> dict[str, Any]:
    _require_admin_api(request)
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                mar.user_id,
                mar.status,
                mar.requested_at,
                mar.reviewed_at,
                mar.review_note,
                COALESCE(u.username, 'Utilisateur') AS username,
                COALESCE(u.email, '') AS email,
                COALESCE(ru.username, '') AS reviewed_by_username
            FROM messenger_access_requests mar
            JOIN users u ON u.id = mar.user_id
            LEFT JOIN users ru ON ru.id = mar.reviewed_by_user_id
            ORDER BY
                CASE mar.status WHEN 'pending' THEN 0 WHEN 'rejected' THEN 1 ELSE 2 END,
                mar.requested_at DESC
            LIMIT 200
            """
        ).fetchall()
    return {"requests": [dict(r) for r in rows]}


@app.post("/api/messenger/requests/{target_user_id}/review")
def review_messenger_access_request(
    target_user_id: int,
    payload: MessengerAccessReviewRequest,
    request: Request,
) -> dict[str, Any]:
    reviewer = _require_admin_api(request)
    reviewer_id = int(reviewer.get("id", 0) or 0)
    reviewer_name = str(reviewer.get("username", "") or "Admin")
    action_status = "approved" if payload.action == "approve" else "rejected"
    note = (payload.note or "").strip() or None

    with _get_db() as conn:
        user_row = conn.execute(
            "SELECT id, username FROM users WHERE id = ?",
            (target_user_id,),
        ).fetchone()
        if user_row is None:
            raise HTTPException(status_code=404, detail="Utilisateur introuvable")

        existing = conn.execute(
            "SELECT user_id FROM messenger_access_requests WHERE user_id = ?",
            (target_user_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO messenger_access_requests (user_id, status, requested_at, reviewed_at, reviewed_by_user_id, review_note)
                VALUES (?, ?, datetime('now'), datetime('now'), ?, ?)
                """,
                (target_user_id, action_status, reviewer_id, note),
            )
        else:
            conn.execute(
                """
                UPDATE messenger_access_requests
                SET status = ?, reviewed_at = datetime('now'), reviewed_by_user_id = ?, review_note = ?
                WHERE user_id = ?
                """,
                (action_status, reviewer_id, note, target_user_id),
            )

        user_name = str(user_row["username"] or "Utilisateur")
        if action_status == "approved":
            notif_title = "Acces Messenger approuve"
            notif_msg = "Votre acces au Messenger groupe est active."
        else:
            notif_title = "Acces Messenger refuse"
            notif_msg = "Votre demande d'acces Messenger a ete refusee."
            if note:
                notif_msg = f"{notif_msg} Motif: {note}"

        _create_user_notification(
            conn,
            int(user_row["id"]),
            "messenger_access_review",
            notif_title,
            notif_msg,
            actor_user_id=reviewer_id,
            actor_username=reviewer_name,
        )
        conn.commit()

    return {
        "message": "Statut mis a jour",
        "user_id": target_user_id,
        "username": user_name,
        "status": action_status,
        "review_note": note,
    }


@app.get("/api/messenger/messages")
def list_messenger_messages(request: Request, limit: int = 80) -> dict[str, Any]:
    identity = _require_auth_api(request)
    user_id = int(identity.get("id", 0) or 0)
    role = str(identity.get("role", "user"))
    with _get_db() as conn:
        access_row = conn.execute(
            "SELECT status FROM messenger_access_requests WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        has_access = access_row is not None and str(access_row["status"] or "") == "approved"
        if role not in ("admin", "superadmin") and not has_access:
            raise HTTPException(status_code=403, detail="Acces Messenger non autorise")

        safe_limit = max(1, min(int(limit or 80), 200))
        rows = conn.execute(
            """
            SELECT id, user_id, username, message_text, audio_path, created_at
            FROM messenger_messages
            ORDER BY id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()

    messages = []
    for row in reversed(rows):
        item = dict(row)
        item["is_mine"] = int(item.get("user_id") or 0) == user_id
        item["audio_url"] = item.get("audio_path")
        messages.append(item)
    return {"messages": messages}


@app.post("/api/messenger/messages")
def create_messenger_message(payload: MessengerMessageCreateRequest, request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    user_id = int(identity.get("id", 0) or 0)
    username = str(identity.get("username", "") or "Utilisateur")
    role = str(identity.get("role", "user"))
    text_value = (payload.text or "").strip()
    audio_data_url = (payload.audio_data_url or "").strip()

    if not text_value and not audio_data_url:
        raise HTTPException(status_code=422, detail="Message vide")

    with _get_db() as conn:
        access_row = conn.execute(
            "SELECT status FROM messenger_access_requests WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        has_access = access_row is not None and str(access_row["status"] or "") == "approved"
        if role not in ("admin", "superadmin") and not has_access:
            raise HTTPException(status_code=403, detail="Acces Messenger non autorise")

        audio_path = None
        if audio_data_url:
            audio_path = _store_messenger_audio_data_url(audio_data_url)

        # Keep send path fast: store now, transcribe later in background if needed.
        final_text = text_value or None

        cur = conn.execute(
            """
            INSERT INTO messenger_messages (user_id, username, message_text, audio_path)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, username, final_text, audio_path),
        )
        message_id = int(cur.lastrowid or 0)
        row = conn.execute(
            """
            SELECT id, user_id, username, message_text, audio_path, created_at
            FROM messenger_messages
            WHERE id = ?
            """,
            (message_id,),
        ).fetchone()
        conn.commit()

    message = dict(row)
    message["audio_url"] = message.get("audio_path")
    message["is_mine"] = True

    Thread(
        target=_postprocess_messenger_audio,
        args=(message_id, audio_path, final_text),
        daemon=True,
    ).start()

    return {"message": message}


@app.get("/admin/events/{event_id}/ratings")
def list_event_ratings(event_id: int, request: Request) -> dict[str, Any]:
    _require_superadmin_api(request)
    with _get_db() as conn:
        event_row = conn.execute("SELECT id, title FROM events WHERE id = ?", (event_id,)).fetchone()
        if event_row is None:
            raise HTTPException(status_code=404, detail="Evenement introuvable")
        rows = conn.execute(
            """
            SELECT id, user_id, username, email, stars, created_at, updated_at
            FROM event_ratings
            WHERE event_id = ?
            ORDER BY updated_at DESC
            """,
            (event_id,),
        ).fetchall()
    return {
        "event_id": event_id,
        "event_title": event_row["title"],
        "ratings": [dict(r) for r in rows],
    }


@app.get("/admin/events/{event_id}/favorites")
def list_event_favorites(event_id: int, request: Request) -> dict[str, Any]:
    _require_superadmin_api(request)
    with _get_db() as conn:
        event_row = conn.execute("SELECT id, title FROM events WHERE id = ?", (event_id,)).fetchone()
        if event_row is None:
            raise HTTPException(status_code=404, detail="Evenement introuvable")
        rows = conn.execute(
            """
            SELECT user_id, username, email, created_at
            FROM event_favorites
            WHERE event_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (event_id,),
        ).fetchall()
    return {
        "event_id": event_id,
        "event_title": event_row["title"],
        "favorite_count": len(rows),
        "favorites": [dict(r) for r in rows],
    }


@app.get("/admin/ratings/data")
def list_all_ratings(request: Request) -> dict[str, Any]:
    _require_superadmin_api(request)
    with _get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                er.id, er.event_id, e.title AS event_title,
                er.user_id, er.username, er.email, er.stars,
                er.created_at, er.updated_at
            FROM event_ratings er
            JOIN events e ON e.id = er.event_id
            ORDER BY er.updated_at DESC
            """
        ).fetchall()
        stats = conn.execute(
            """
            SELECT
                event_id, e.title AS event_title,
                ROUND(AVG(stars), 2) AS avg_rating,
                COUNT(*) AS rating_count,
                SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END) AS five_star,
                SUM(CASE WHEN stars = 4 THEN 1 ELSE 0 END) AS four_star,
                SUM(CASE WHEN stars = 3 THEN 1 ELSE 0 END) AS three_star,
                SUM(CASE WHEN stars = 2 THEN 1 ELSE 0 END) AS two_star,
                SUM(CASE WHEN stars = 1 THEN 1 ELSE 0 END) AS one_star
            FROM event_ratings er
            JOIN events e ON e.id = er.event_id
            GROUP BY er.event_id
            ORDER BY avg_rating DESC
            """
        ).fetchall()
        comments = conn.execute(
            """
            SELECT
                c.id,
                c.event_id,
                e.title AS event_title,
                c.user_id,
                c.username,
                c.email,
                c.content,
                c.original_content,
                c.is_masked,
                c.moderation_reason,
                c.matched_badword,
                c.created_at,
                c.updated_at,
                COALESCE(SUM(CASE WHEN r.reaction = 'like' THEN 1 ELSE 0 END), 0) AS like_count,
                COALESCE(SUM(CASE WHEN r.reaction = 'dislike' THEN 1 ELSE 0 END), 0) AS dislike_count
            FROM event_comments c
            JOIN events e ON e.id = c.event_id
            LEFT JOIN comment_reactions r ON r.comment_id = c.id
            GROUP BY c.id
            ORDER BY COALESCE(c.updated_at, c.created_at) DESC, c.id DESC
            """
        ).fetchall()
    return {
        "ratings": [dict(r) for r in rows],
        "stats_by_event": [dict(r) for r in stats],
        "comments": [dict(r) for r in comments],
    }


@app.get("/admin/ratings")
def ratings_page(request: Request) -> Response:
    redirect = _require_superadmin_or_redirect(request)
    if redirect is not None:
        return redirect
    ratings_file = Path(__file__).parent / "ratings.html"
    if not ratings_file.exists():
        raise HTTPException(status_code=500, detail="Page evaluations introuvable")
    return FileResponse(ratings_file, media_type="text/html; charset=utf-8")


def _normalize_event_datetime_required(raw_value: str | None) -> str:
    value = str(raw_value or "").strip()
    if not value:
        raise HTTPException(status_code=422, detail="Date et heure obligatoires")

    accepted_formats = (
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d-%m-%Y %H:%M",
    )
    for fmt in accepted_formats:
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

    raise HTTPException(status_code=422, detail="Date/heure invalide (ex: 2026-05-02T14:30)")


def _normalize_event_datetime_optional(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    value = str(raw_value).strip()
    if not value:
        return None
    return _normalize_event_datetime_required(value)


def _extract_gps_from_location(location: str) -> tuple[float, float] | None:
    value = (location or "").strip()
    if not value:
        return None

    # Expected admin map format: "... [GPS:9.123456,33.123456]" (lon,lat).
    gps_match = re.search(
        r"\[GPS:\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\]",
        value,
        flags=re.IGNORECASE,
    )
    if gps_match:
        lon = float(gps_match.group(1))
        lat = float(gps_match.group(2))
        return (lat, lon)

    # Fallback: accept a raw "lon,lat" or "lat,lon" pattern.
    raw_match = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", value)
    if raw_match:
        a = float(raw_match.group(1))
        b = float(raw_match.group(2))
        if abs(a) <= 90 and abs(b) <= 180:
            return (a, b)
        if abs(a) <= 180 and abs(b) <= 90:
            return (b, a)
    return None


def _weather_code_label_fr(code: int) -> str:
    labels: dict[int, str] = {
        0: "Ciel degage",
        1: "Plutot degage",
        2: "Partiellement nuageux",
        3: "Couvert",
        45: "Brouillard",
        48: "Brouillard givrant",
        51: "Bruine faible",
        53: "Bruine moderee",
        55: "Bruine forte",
        56: "Bruine verglacante faible",
        57: "Bruine verglacante forte",
        61: "Pluie faible",
        63: "Pluie moderee",
        65: "Pluie forte",
        66: "Pluie verglacante faible",
        67: "Pluie verglacante forte",
        71: "Neige faible",
        73: "Neige moderee",
        75: "Neige forte",
        77: "Grains de neige",
        80: "Averses faibles",
        81: "Averses moderees",
        82: "Averses violentes",
        85: "Averses de neige faibles",
        86: "Averses de neige fortes",
        95: "Orage",
        96: "Orage avec grele faible",
        99: "Orage avec grele forte",
    }
    return labels.get(int(code), "Conditions variables")


def _build_weather_advice(precip_probability: float, wind_kmh: float, weather_code: int) -> str:
    rainy_codes = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
    if weather_code in rainy_codes or precip_probability >= 55:
        return "Risque de pluie: prevoir un plan B ou une zone couverte."
    if wind_kmh >= 45:
        return "Vent soutenu: verifier la securite des installations exterieures."
    if precip_probability >= 30:
        return "Risque meteo modere: validez la logistique (abris, horaires flexibles)."
    return "Conditions globalement favorables pour maintenir l'evenement."


@app.post("/admin/events/weather-preview")
def get_event_weather_preview(payload: EventWeatherPreviewRequest, request: Request) -> dict[str, Any]:
    _require_superadmin_api(request)

    normalized_date = _normalize_event_datetime_required(payload.event_date)
    event_day = normalized_date[:10]
    today = datetime.now().strftime("%Y-%m-%d")
    if event_day < today:
        raise HTTPException(status_code=422, detail="Date d'evenement dans le passe")

    coords = _extract_gps_from_location(payload.location)
    if coords is None:
        raise HTTPException(
            status_code=422,
            detail="Lieu sans coordonnees GPS. Choisissez le lieu depuis la carte.",
        )

    lat, lon = coords
    weather_url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat:.6f}&longitude={lon:.6f}"
        "&daily=weathercode,temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max,windspeed_10m_max"
        "&timezone=auto"
        f"&start_date={event_day}&end_date={event_day}"
    )
    req = urllib.request.Request(weather_url)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if int(getattr(exc, "code", 0)) == 400:
            raise HTTPException(
                status_code=422,
                detail="Prevision indisponible pour cette date (fenetre meteo limitee)",
            ) from exc
        raise HTTPException(status_code=503, detail="Service meteo indisponible") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Service meteo indisponible") from exc

    daily = data.get("daily") or {}
    times = daily.get("time") or []
    if not times:
        raise HTTPException(status_code=422, detail="Aucune prevision meteo disponible pour cette date")

    weather_code = int((daily.get("weathercode") or [0])[0] or 0)
    temp_max = float((daily.get("temperature_2m_max") or [0.0])[0] or 0.0)
    temp_min = float((daily.get("temperature_2m_min") or [0.0])[0] or 0.0)
    precip = float((daily.get("precipitation_probability_max") or [0.0])[0] or 0.0)
    wind = float((daily.get("windspeed_10m_max") or [0.0])[0] or 0.0)

    weather_label = _weather_code_label_fr(weather_code)
    advice = _build_weather_advice(precip, wind, weather_code)

    return {
        "event_date": normalized_date,
        "forecast_date": event_day,
        "location": payload.location.strip(),
        "coords": {"lat": round(lat, 6), "lon": round(lon, 6)},
        "forecast": {
            "weather_code": weather_code,
            "weather_label": weather_label,
            "temperature_max_c": round(temp_max, 1),
            "temperature_min_c": round(temp_min, 1),
            "precipitation_probability_max": round(precip, 1),
            "windspeed_max_kmh": round(wind, 1),
        },
        "advice": advice,
    }


@app.post("/admin/events")
def add_event(payload: EventCreateRequest, request: Request) -> dict[str, Any]:
    _require_superadmin_api(request)
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Titre vide")

    event_date = _normalize_event_datetime_required(payload.event_date)
    location = payload.location.strip() if payload.location else None
    description = payload.description.strip() if payload.description else None

    with _get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO events (title, event_date, location, description, image_path, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            """,
            (title, event_date, location, description, None),
        )
        conn.commit()
    return {"message": "Evenement ajoute", "event_id": cur.lastrowid}


@app.post("/admin/events/upload")
def add_event_with_image(
    request: Request,
    title: str = Form(...),
    event_date: str = Form(...),
    location: str | None = Form(default=None),
    description: str | None = Form(default=None),
    image: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    _require_superadmin_api(request)
    title_clean = title.strip()
    if not title_clean:
        raise HTTPException(status_code=422, detail="Titre vide")

    image_path = _store_event_image(image)
    event_date_clean = _normalize_event_datetime_required(event_date)
    location_clean = location.strip() if location else None
    description_clean = description.strip() if description else None

    with _get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO events (title, event_date, location, description, image_path, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            """,
            (title_clean, event_date_clean, location_clean, description_clean, image_path),
        )
        conn.commit()
    return {"message": "Evenement ajoute", "event_id": cur.lastrowid, "image_path": image_path}


@app.patch("/admin/events/{event_id}")
def update_event(event_id: int, payload: EventUpdateRequest, request: Request) -> dict[str, Any]:
    _require_superadmin_api(request)
    with _get_db() as conn:
        row = conn.execute(
            "SELECT id, title, event_date, location, description FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Evenement introuvable")

        title = payload.title.strip() if payload.title is not None else str(row["title"])
        if not title:
            raise HTTPException(status_code=422, detail="Titre vide")

        event_date = _normalize_event_datetime_optional(payload.event_date) if payload.event_date is not None else row["event_date"]
        location = payload.location.strip() if payload.location is not None else row["location"]
        description = payload.description.strip() if payload.description is not None else row["description"]

        conn.execute(
            """
            UPDATE events
            SET title = ?, event_date = ?, location = ?, description = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                title,
                event_date if event_date else None,
                location if location else None,
                description if description else None,
                event_id,
            ),
        )
        conn.commit()
    return {"message": "Evenement mis a jour", "event_id": event_id}


@app.post("/admin/events/{event_id}/upload")
def update_event_with_image(
    event_id: int,
    request: Request,
    title: str = Form(...),
    event_date: str = Form(...),
    location: str | None = Form(default=None),
    description: str | None = Form(default=None),
    image: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    _require_superadmin_api(request)

    title_clean = title.strip()
    if not title_clean:
        raise HTTPException(status_code=422, detail="Titre vide")

    event_date_clean = _normalize_event_datetime_required(event_date)
    location_clean = location.strip() if location else None
    description_clean = description.strip() if description else None

    with _get_db() as conn:
        row = conn.execute(
            "SELECT id, image_path FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Evenement introuvable")

        image_path = row["image_path"]
        new_image_path = _store_event_image(image)
        if new_image_path:
            image_path = new_image_path

        conn.execute(
            """
            UPDATE events
            SET title = ?, event_date = ?, location = ?, description = ?, image_path = ?, updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                title_clean,
                event_date_clean,
                location_clean,
                description_clean,
                image_path,
                event_id,
            ),
        )
        conn.commit()

    return {"message": "Evenement mis a jour", "event_id": event_id, "image_path": image_path}


@app.delete("/admin/events/{event_id}")
def delete_event(event_id: int, request: Request) -> dict[str, Any]:
    _require_superadmin_api(request)
    with _get_db() as conn:
        result = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        conn.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Evenement introuvable")
    return {"message": "Evenement supprime", "event_id": event_id}


@app.get("/chatbot", response_class=FileResponse)
def chatbot(request: Request) -> Response:
    redirect = _require_admin_or_redirect(request)
    if redirect is not None:
        return redirect
    html_path = Path(__file__).parent / "chatbot.html"
    if html_path.exists():
        return FileResponse(html_path, media_type="text/html; charset=utf-8")
    return FileResponse(
        content="<h1>Chatbot non trouvé</h1>",
        media_type="text/html; charset=utf-8",
    )


@app.get("/client", response_class=FileResponse)
def client(request: Request) -> Response:
    redirect = _require_admin_or_redirect(request)
    if redirect is not None:
        return redirect
    html_path = Path(__file__).parent / "client.html"
    if html_path.exists():
        return FileResponse(html_path, media_type="text/html; charset=utf-8")
    return FileResponse(
        content="<h1>Client non trouvé</h1>",
        media_type="text/html; charset=utf-8",
    )


@app.get("/events")
def events_page(request: Request) -> Response:
    redirect = _require_superadmin_or_redirect(request)
    if redirect is not None:
        return redirect

    events_file = Path(__file__).parent / "events.html"
    if not events_file.exists():
        raise HTTPException(status_code=500, detail="Page evenements introuvable")
    return FileResponse(events_file, media_type="text/html; charset=utf-8")


@app.get("/api/face-descriptor")
def get_face_descriptor(request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    role = str(identity.get("role", "user"))
    if role not in ("admin", "superadmin", "logistique"):
        raise HTTPException(status_code=403, detail="Reservé aux admins")
    user_id = int(identity["id"])
    with _get_db() as conn:
        row = conn.execute(
            "SELECT descriptor, updated_at FROM face_descriptors WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return {"enrolled": False}
    return {"enrolled": True, "descriptor": row["descriptor"], "updated_at": row["updated_at"]}


@app.post("/api/face-descriptor")
async def save_face_descriptor(request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    role = str(identity.get("role", "user"))
    if role not in ("admin", "superadmin", "logistique"):
        raise HTTPException(status_code=403, detail="Reservé aux admins")
    user_id = int(identity["id"])
    body = await request.json()
    descriptor = body.get("descriptor")
    if not descriptor or not isinstance(descriptor, list):
        raise HTTPException(status_code=400, detail="Descripteur invalide")
    if len(descriptor) != 128:
        raise HTTPException(status_code=400, detail="Le descripteur doit avoir 128 valeurs")
    import json as _json
    descriptor_str = _json.dumps(descriptor)
    with _get_db() as conn:
        conn.execute(
            """
            INSERT INTO face_descriptors (user_id, descriptor, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET descriptor=excluded.descriptor, updated_at=excluded.updated_at
            """,
            (user_id, descriptor_str),
        )
        conn.commit()
    return {"ok": True}


@app.delete("/api/face-descriptor")
def delete_face_descriptor(request: Request) -> dict[str, Any]:
    identity = _require_auth_api(request)
    user_id = int(identity["id"])
    with _get_db() as conn:
        conn.execute("DELETE FROM face_descriptors WHERE user_id = ?", (user_id,))
        conn.commit()
    return {"ok": True}


@app.get("/admin/face-setup")
def admin_face_setup_page(request: Request) -> Response:
    redirect = _require_admin_or_redirect(request)
    if redirect is not None:
        return redirect
    setup_file = Path(__file__).parent / "admin_face_setup.html"
    if not setup_file.exists():
        raise HTTPException(status_code=500, detail="Page face-setup introuvable")
    return FileResponse(setup_file, media_type="text/html; charset=utf-8")


@app.get("/admin/bi-dashboard")
def admin_bi_dashboard_page(request: Request) -> Response:
    redirect = _require_admin_or_redirect(request)
    if redirect is not None:
        return redirect

    dashboard_file = Path(__file__).parent / "admin_bi_dashboard.html"
    if not dashboard_file.exists():
        raise HTTPException(status_code=500, detail="Page dashboard BI introuvable")
    return FileResponse(dashboard_file, media_type="text/html; charset=utf-8")


@app.get("/models")
def list_models(request: Request) -> dict[str, Any]:
    _require_auth_api(request)
    items: list[dict[str, Any]] = []
    for asset in MODEL_REGISTRY.values():
        metadata = _load_metadata(asset)
        items.append(
            {
                "key": asset.key,
                "task": asset.task,
                "model_path": str(asset.model_path.relative_to(ROOT_DIR)),
                "expected_features": metadata.get("features", []),
                "metrics": {
                    k: v
                    for k, v in metadata.items()
                    if k
                    in {
                        "r2_score",
                        "rmse",
                        "mae",
                        "cv_r2_mean",
                        "cv_r2_std",
                        "silhouette_score",
                        "davies_bouldin",
                        "calinski_harabasz",
                    }
                },
            }
        )
    return {"models": items}


@app.post("/predict/{model_key}")
def predict(model_key: str, request: PredictRequest, auth_request: Request) -> dict[str, Any]:
    _require_auth_api(auth_request)
    asset = MODEL_REGISTRY.get(model_key)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Modele inconnu: {model_key}")
    if asset.task == "timeseries":
        raise HTTPException(status_code=400, detail="Utilise /forecast/{model_key} pour les series temporelles")

    try:
        model = _load_model(asset)
        metadata = _load_metadata(asset)
        prepared, feature_order = _prepare_input(asset, model, request.features)
        prediction = model.predict(prepared)
        y_pred = _to_native(prediction[0])

        result: dict[str, Any] = {
            "model_key": model_key,
            "task": asset.task,
            "used_features": feature_order,
            "prediction": y_pred,
        }

        if asset.task == "classification" and hasattr(model, "predict_proba"):
            probabilities = model.predict_proba(prepared)[0]
            classes = [str(c) for c in getattr(model, "classes_", range(len(probabilities)))]
            result["probabilities"] = {
                classes[i]: float(probabilities[i]) for i in range(len(probabilities))
            }

        if asset.task == "clustering":
            segment_map = metadata.get("segment_map", {})
            segment_name = segment_map.get(str(y_pred))
            if segment_name is not None:
                result["segment"] = segment_name

        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erreur prediction: {exc}") from exc


@app.post("/forecast/{model_key}")
def forecast(model_key: str, request: ForecastRequest, auth_request: Request) -> dict[str, Any]:
    _require_auth_api(auth_request)
    asset = MODEL_REGISTRY.get(model_key)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Modele inconnu: {model_key}")
    if asset.task != "timeseries":
        raise HTTPException(status_code=400, detail="Cet endpoint est reserve aux modeles de series temporelles")

    try:
        model = _load_model(asset)

        if hasattr(model, "get_forecast"):
            forecast_values = model.get_forecast(steps=request.periods).predicted_mean
        elif hasattr(model, "forecast"):
            forecast_values = model.forecast(steps=request.periods)
        else:
            raise HTTPException(status_code=500, detail="Le modele ne supporte pas la methode forecast")

        return {
            "model_key": model_key,
            "task": asset.task,
            "periods": request.periods,
            "forecast": [float(_to_native(v)) for v in np.asarray(forecast_values)],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erreur forecast: {exc}") from exc
