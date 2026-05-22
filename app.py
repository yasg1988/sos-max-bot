from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import psycopg2
from fastapi import FastAPI, Request
from fastapi.responses import Response


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("sos.bot")

MAX_API_BASE = os.getenv("MAX_API_BASE", "https://platform-api.max.ru").rstrip("/")
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()
MAX_WEBHOOK_URL = os.getenv("MAX_WEBHOOK_URL", "").strip()
MAX_REGISTER_WEBHOOK = os.getenv("MAX_REGISTER_WEBHOOK", "true").lower() in {"1", "true", "yes", "on"}
ADMIN_USER_IDS = {
    item.strip()
    for item in os.getenv("ADMIN_USER_IDS", "11056660").split(",")
    if item.strip()
}

DB_HOST = os.getenv("DB_HOST", "").strip()
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "postgres").strip()
DB_USER = os.getenv("DB_USER", "").strip()
DB_PASSWORD = os.getenv("DB_PASSWORD", "").strip()
DB_SSLMODE = os.getenv("DB_SSLMODE", "disable").strip()

APP_RELEASE = os.getenv("APP_RELEASE", "2026-05-21-sos-prod-v1").strip()
PORT = int(os.getenv("PORT", "8000"))
FAMILY_CODE_TTL_MINUTES = int(os.getenv("FAMILY_CODE_TTL_MINUTES", "15"))
ORG_ALERT_COOLDOWN_MINUTES = int(os.getenv("ORG_ALERT_COOLDOWN_MINUTES", "10"))
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
BOT_USER_ID: int | None = None
BASE_DIR = Path(__file__).resolve().parent
RECOMMENDATION_IMAGES = {
    "child": BASE_DIR / "assets" / "child_recommendations.png",
    "parent": BASE_DIR / "assets" / "parent_recommendations.png",
}
RECOMMENDATION_IMAGE_PAYLOADS: dict[str, dict[str, Any]] = {}
HELP_IMAGES = {
    "child": BASE_DIR / "assets" / "help_children.png",
    "parent": BASE_DIR / "assets" / "help_parents.png",
}
HELP_IMAGE_PAYLOADS: dict[str, dict[str, Any]] = {}

NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9 .,'\"()\\-]{2,120}$")
INN_RE = re.compile(r"^\d{10}(\d{2})?$")
PHONE_RE = re.compile(r"^[0-9+() .\\-]{6,32}$")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


def clean_inn(value: str) -> str:
    return re.sub(r"\D+", "", value or "")


def trim_text(text: str, limit: int = 3900) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def safe_json_dump(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:4000]
    except Exception:
        return "<unserializable>"


def require_token() -> str:
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN is required")
    return MAX_BOT_TOKEN


def get_db_connection():
    if not all([DB_HOST, DB_USER, DB_PASSWORD]):
        raise RuntimeError("DB connection env vars are required")
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        sslmode=DB_SSLMODE,
    )


async def max_request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    token = require_token()
    url = f"{MAX_API_BASE}{path}"
    params = dict(kwargs.pop("params", {}) or {})
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["Authorization"] = token
    logger.info("MAX request method=%s path=%s params=%s", method, path, safe_json_dump(params))
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.request(method, url, params=params, headers=headers, **kwargs)
    logger.info("MAX response method=%s path=%s status=%s body=%s", method, path, response.status_code, response.text[:2000])
    response.raise_for_status()
    return response


async def answer_callback(callback_id: str, message: dict[str, Any] | None = None, notification: str | None = None) -> None:
    body: dict[str, Any] = {}
    if message is not None:
        body["message"] = message
    if notification is not None:
        body["notification"] = notification
    await max_request("POST", "/answers", params={"callback_id": callback_id}, json=body)


async def upload_image(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        logger.warning("Image asset not found: %s", path)
        return None
    upload_response = await max_request("POST", "/uploads", params={"type": "image"})
    upload_url = (upload_response.json() or {}).get("url")
    if not upload_url:
        logger.warning("MAX upload URL is empty for image=%s response=%s", path, upload_response.text[:1000])
        return None
    data = path.read_bytes()
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(upload_url, files={"data": (path.name, data, "image/png")})
    logger.info("MAX image upload status=%s body=%s", response.status_code, response.text[:1000])
    response.raise_for_status()
    payload = response.json() or {}
    if not payload.get("token"):
        logger.warning("MAX image upload response has no token: %s", safe_json_dump(payload))
    return payload


async def recommendation_image_attachment(kind: str) -> dict[str, Any] | None:
    path = RECOMMENDATION_IMAGES.get(kind)
    if path is None:
        return None
    if kind not in RECOMMENDATION_IMAGE_PAYLOADS:
        payload = await upload_image(path)
        if payload:
            RECOMMENDATION_IMAGE_PAYLOADS[kind] = payload
    payload = RECOMMENDATION_IMAGE_PAYLOADS.get(kind)
    if not payload:
        return None
    return {"type": "image", "payload": payload}


async def help_image_attachment(kind: str) -> dict[str, Any] | None:
    path = HELP_IMAGES.get(kind)
    if path is None:
        return None
    if kind not in HELP_IMAGE_PAYLOADS:
        payload = await upload_image(path)
        if payload:
            HELP_IMAGE_PAYLOADS[kind] = payload
    payload = HELP_IMAGE_PAYLOADS.get(kind)
    if not payload:
        return None
    return {"type": "image", "payload": payload}


def callback_button(text: str, payload: str) -> dict[str, Any]:
    return {"type": "callback", "text": text[:128], "payload": payload[:1024]}


def link_button(text: str, url: str) -> dict[str, Any]:
    return {"type": "link", "text": text[:128], "url": url}


def message_button(text: str) -> dict[str, Any]:
    return {"type": "message", "text": text[:128]}


def geo_button(text: str = "Отправить геолокацию") -> dict[str, Any]:
    return {"type": "request_geo_location", "text": text[:128]}


def contact_button(text: str = "Отправить телефон MAX") -> dict[str, Any]:
    return {"type": "request_contact", "text": text[:128]}


def keyboard_rows(buttons: list[dict[str, Any]], columns: int = 1) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = []
    row: list[dict[str, Any]] = []
    for button in buttons:
        row.append(button)
        if len(row) >= columns:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def map_buttons(lat: float, lon: float) -> list[list[dict[str, Any]]]:
    lat_s = f"{lat:.6f}"
    lon_s = f"{lon:.6f}"
    return [
        [link_button("Google Map", f"https://www.google.com/maps/search/?api=1&query={lat_s},{lon_s}")],
        [link_button("Яндекс карты", f"https://yandex.com/maps/?ll={lon_s}%2C{lat_s}&z=18&text={lat_s}%2C{lon_s}")],
        [link_button("Яндекс навигатор", f"https://yandex.ru/navi/?whatshere%5Bpoint%5D={lon_s},{lat_s}&whatshere%5Bzoom%5D=18")],
        [link_button("2Gis", f"https://2gis.ru/geo/{lon_s},{lat_s}")],
    ]


async def send_message(chat_id: int | str, text: str, buttons: list[dict[str, Any]] | None = None, columns: int = 1) -> None:
    body: dict[str, Any] = {"text": trim_text(text)}
    if buttons:
        body["attachments"] = [
            {
                "type": "inline_keyboard",
                "payload": {"buttons": keyboard_rows(buttons, columns=columns)},
            }
        ]
    await max_request("POST", "/messages", params={"chat_id": chat_id}, json=body)


async def send_message_rows(chat_id: int | str, text: str, rows: list[list[dict[str, Any]]]) -> None:
    body: dict[str, Any] = {"text": trim_text(text)}
    if rows:
        body["attachments"] = [
            {
                "type": "inline_keyboard",
                "payload": {"buttons": rows},
            }
        ]
    await max_request("POST", "/messages", params={"chat_id": chat_id}, json=body)


async def send_message_with_attachments(
    chat_id: int | str,
    text: str,
    attachments: list[dict[str, Any]],
    buttons: list[dict[str, Any]] | None = None,
    columns: int = 1,
) -> None:
    body: dict[str, Any] = {"text": trim_text(text)}
    body["attachments"] = list(attachments)
    if buttons:
        body["attachments"].append(
            {
                "type": "inline_keyboard",
                "payload": {"buttons": keyboard_rows(buttons, columns=columns)},
            }
        )
    await max_request("POST", "/messages", params={"chat_id": chat_id}, json=body)


def inline_keyboard_attachment(rows: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "type": "inline_keyboard",
        "payload": {"buttons": rows},
    }


def message_body(text: str, rows: list[list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"text": trim_text(text)}
    if rows:
        body["attachments"] = [inline_keyboard_attachment(rows)]
    return body


async def send_user_message(user_id: int | str, text: str, buttons: list[dict[str, Any]] | None = None, columns: int = 1) -> None:
    body: dict[str, Any] = {"text": trim_text(text)}
    if buttons:
        body["attachments"] = [
            {
                "type": "inline_keyboard",
                "payload": {"buttons": keyboard_rows(buttons, columns=columns)},
            }
        ]
    await max_request("POST", "/messages", params={"user_id": user_id}, json=body)


def execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def fetchone(sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchone()
    finally:
        conn.close()


def fetchall(sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return list(cur.fetchall())
    finally:
        conn.close()


def migrate() -> None:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            create schema if not exists sos_core;
            create schema if not exists sos_family;
            create schema if not exists sos_org;

            create table if not exists sos_core.users (
                user_id text primary key,
                chat_id text,
                first_name text,
                last_name text,
                username text,
                display_name text,
                raw_profile jsonb not null default '{}'::jsonb,
                roles jsonb not null default '[]'::jsonb,
                is_admin boolean not null default false,
                created_at timestamptz not null default now(),
                updated_at timestamptz not null default now(),
                last_seen_at timestamptz not null default now()
            );

            create table if not exists sos_core.user_states (
                user_id text primary key references sos_core.users(user_id) on delete cascade,
                state text not null,
                data jsonb not null default '{}'::jsonb,
                updated_at timestamptz not null default now()
            );

            create table if not exists sos_core.audit_log (
                id text primary key,
                actor_user_id text,
                chat_id text,
                action text not null,
                entity_type text,
                entity_id text,
                payload jsonb not null default '{}'::jsonb,
                created_at timestamptz not null default now()
            );

            create table if not exists sos_family.link_codes (
                code text primary key,
                parent_user_id text not null references sos_core.users(user_id) on delete cascade,
                expires_at timestamptz not null,
                used_at timestamptz,
                created_at timestamptz not null default now()
            );

            create table if not exists sos_family.links (
                id text primary key,
                parent_user_id text not null references sos_core.users(user_id) on delete cascade,
                child_user_id text not null references sos_core.users(user_id) on delete cascade,
                status text not null,
                created_at timestamptz not null default now(),
                decided_at timestamptz,
                unique(parent_user_id, child_user_id)
            );

            create table if not exists sos_family.alerts (
                id text primary key,
                child_user_id text not null references sos_core.users(user_id) on delete cascade,
                alert_type text not null,
                status text not null,
                latitude double precision,
                longitude double precision,
                location_payload jsonb not null default '{}'::jsonb,
                created_at timestamptz not null default now(),
                accepted_by_user_id text,
                accepted_at timestamptz,
                closed_at timestamptz
            );

            create table if not exists sos_family.alert_recipients (
                alert_id text not null references sos_family.alerts(id) on delete cascade,
                parent_user_id text not null references sos_core.users(user_id) on delete cascade,
                sent_at timestamptz,
                accepted_at timestamptz,
                primary key(alert_id, parent_user_id)
            );

            create table if not exists sos_org.organizations (
                id text primary key,
                org_type text not null,
                name text not null,
                inn text not null unique,
                status text not null default 'active',
                approval_chat_id text,
                alert_chat_id text,
                address text,
                comment text,
                created_by_user_id text,
                created_at timestamptz not null default now(),
                updated_at timestamptz not null default now()
            );

            create table if not exists sos_org.staff (
                id text primary key,
                user_id text not null references sos_core.users(user_id) on delete cascade,
                organization_id text not null references sos_org.organizations(id) on delete cascade,
                staff_type text not null,
                full_name text not null,
                phone text not null,
                position text,
                status text not null,
                created_at timestamptz not null default now(),
                decided_by_user_id text,
                decided_at timestamptz,
                blocked_at timestamptz,
                unique(user_id, organization_id)
            );

            create table if not exists sos_org.staff_requests (
                id text primary key,
                staff_id text not null references sos_org.staff(id) on delete cascade,
                organization_id text not null references sos_org.organizations(id) on delete cascade,
                user_id text not null references sos_core.users(user_id) on delete cascade,
                status text not null,
                created_at timestamptz not null default now(),
                decided_by_user_id text,
                decided_at timestamptz
            );

            create table if not exists sos_org.alerts (
                id text primary key,
                organization_id text not null references sos_org.organizations(id) on delete cascade,
                staff_id text not null references sos_org.staff(id) on delete cascade,
                alert_type text not null,
                status text not null,
                is_test boolean not null default false,
                created_at timestamptz not null default now(),
                accepted_by_user_id text,
                accepted_at timestamptz,
                closed_at timestamptz
            );

            create table if not exists sos_org.alert_chats (
                id text primary key,
                organization_id text not null references sos_org.organizations(id) on delete cascade,
                chat_id text not null,
                title text,
                is_active boolean not null default true,
                created_by_user_id text,
                created_at timestamptz not null default now(),
                unique(organization_id, chat_id)
            );

            create index if not exists sos_family_links_child_idx on sos_family.links(child_user_id, status);
            create index if not exists sos_org_staff_user_status_idx on sos_org.staff(user_id, status);
            create index if not exists sos_org_alerts_staff_created_idx on sos_org.alerts(staff_id, created_at desc);
            """
        )
        conn.commit()
    finally:
        conn.close()


def audit(actor_user_id: str | None, chat_id: str | None, action: str, entity_type: str | None = None, entity_id: str | None = None, payload: dict[str, Any] | None = None) -> None:
    execute(
        """
        insert into sos_core.audit_log (id, actor_user_id, chat_id, action, entity_type, entity_id, payload)
        values (%s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (new_id(), actor_user_id, chat_id, action, entity_type, entity_id, json.dumps(payload or {}, ensure_ascii=False)),
    )


def extract_user_profile(update: dict[str, Any]) -> dict[str, Any]:
    if isinstance(update.get("user"), dict):
        return update["user"]
    callback = update.get("callback")
    if isinstance(callback, dict) and isinstance(callback.get("user"), dict):
        return callback["user"]
    return update.get("message", {}).get("sender", {}) or {}


def extract_sender_id(update: dict[str, Any]) -> str | None:
    profile = extract_user_profile(update)
    value = profile.get("user_id")
    return str(value) if value is not None else None


def extract_chat_id(update: dict[str, Any]) -> str | None:
    if update.get("chat_id") is not None:
        return str(update["chat_id"])
    message = update.get("message", {}) or {}
    recipient = message.get("recipient", {}) or {}
    if recipient.get("chat_id") is not None:
        return str(recipient["chat_id"])
    if message.get("chat_id") is not None:
        return str(message["chat_id"])
    callback = update.get("callback") or {}
    user = callback.get("user") or {}
    if user.get("user_id") is not None:
        return str(user["user_id"])
    return None


def extract_text(update: dict[str, Any]) -> str:
    body = update.get("message", {}).get("body", {}) or {}
    if isinstance(body, dict):
        if isinstance(body.get("text"), str):
            text = body["text"].strip()
            if text:
                return text
    phone = extract_phone(update)
    if phone:
        return phone
    return ""


def extract_phone(update: dict[str, Any]) -> str:
    def phone_from_vcard(vcard: str) -> str:
        vcard = vcard.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
        for line in vcard.splitlines():
            if line.upper().startswith("TEL"):
                value = line.split(":", 1)[-1].strip()
                if value:
                    return value
        return ""

    def walk(value: Any) -> str:
        if isinstance(value, dict):
            contact = value.get("contact")
            if isinstance(contact, dict):
                for key in ("phone", "phone_number", "vcf_phone"):
                    if contact.get(key):
                        return str(contact[key]).strip()
                if isinstance(contact.get("vcf_info"), str):
                    phone = phone_from_vcard(contact["vcf_info"])
                    if phone:
                        return phone
            if value.get("type") == "contact":
                payload = value.get("payload") if isinstance(value.get("payload"), dict) else value
                for key in ("phone", "phone_number", "vcf_phone"):
                    if payload.get(key):
                        return str(payload[key]).strip()
                if isinstance(payload.get("vcf_info"), str):
                    phone = phone_from_vcard(payload["vcf_info"])
                    if phone:
                        return phone
            for nested in value.values():
                phone = walk(nested)
                if phone:
                    return phone
        elif isinstance(value, list):
            for item in value:
                phone = walk(item)
                if phone:
                    return phone
        return ""

    return walk(update)


def extract_callback_payload(update: dict[str, Any]) -> str:
    callback = update.get("callback")
    if isinstance(callback, dict) and isinstance(callback.get("payload"), str):
        return callback["payload"].strip()
    body = update.get("message", {}).get("body", {}) or {}
    callback_body = body.get("callback") if isinstance(body, dict) else None
    if isinstance(callback_body, dict) and isinstance(callback_body.get("payload"), str):
        return callback_body["payload"].strip()
    if isinstance(update.get("payload"), str):
        return update["payload"].strip()
    return ""


def extract_callback_id(update: dict[str, Any]) -> str | None:
    callback = update.get("callback")
    if isinstance(callback, dict):
        for key in ("callback_id", "callbackId", "id"):
            if callback.get(key) is not None:
                return str(callback[key])
    for key in ("callback_id", "callbackId"):
        if update.get(key) is not None:
            return str(update[key])
    return None


def extract_location(update: dict[str, Any]) -> tuple[float, float, dict[str, Any]] | None:
    def parse_coord(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(str(value).replace(",", "."))
        except (TypeError, ValueError):
            return None

    def walk(value: Any) -> tuple[float, float, dict[str, Any]] | None:
        if isinstance(value, dict):
            lat = parse_coord(value.get("latitude", value.get("lat")))
            lon = parse_coord(value.get("longitude", value.get("lon", value.get("lng"))))
            if lat is not None and lon is not None:
                return lat, lon, value
            for nested in value.values():
                found = walk(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        return None

    return walk(update)


def upsert_user(update: dict[str, Any], chat_id: str | None) -> str | None:
    profile = extract_user_profile(update)
    user_id = profile.get("user_id")
    if user_id is None:
        return None
    user_id = str(user_id)
    is_admin = user_id in ADMIN_USER_IDS
    roles = ["admin"] if is_admin else []
    execute(
        """
        insert into sos_core.users (user_id, chat_id, first_name, last_name, username, raw_profile, roles, is_admin)
        values (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
        on conflict (user_id) do update set
            chat_id = coalesce(excluded.chat_id, sos_core.users.chat_id),
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            username = excluded.username,
            raw_profile = excluded.raw_profile,
            is_admin = excluded.is_admin,
            roles = case
                when excluded.is_admin and not (sos_core.users.roles ? 'admin')
                    then sos_core.users.roles || '["admin"]'::jsonb
                else sos_core.users.roles
            end,
            updated_at = now(),
            last_seen_at = now()
        """,
        (
            user_id,
            chat_id,
            profile.get("first_name"),
            profile.get("last_name"),
            profile.get("username"),
            json.dumps(profile, ensure_ascii=False),
            json.dumps(roles, ensure_ascii=False),
            is_admin,
        ),
    )
    return user_id


def set_display_name(user_id: str, name: str) -> None:
    execute("update sos_core.users set display_name = %s, updated_at = now() where user_id = %s", (name.strip(), user_id))


def get_user(user_id: str) -> dict[str, Any] | None:
    row = fetchone("select user_id, chat_id, display_name, roles, is_admin from sos_core.users where user_id = %s", (user_id,))
    if not row:
        return None
    return {"user_id": row[0], "chat_id": row[1], "display_name": row[2], "roles": row[3] or [], "is_admin": row[4]}


def add_role(user_id: str, role: str) -> None:
    execute(
        """
        update sos_core.users
        set roles = case when roles ? %s then roles else roles || %s::jsonb end,
            updated_at = now()
        where user_id = %s
        """,
        (role, json.dumps([role]), user_id),
    )


def get_state(user_id: str) -> tuple[str, dict[str, Any]] | None:
    row = fetchone("select state, data from sos_core.user_states where user_id = %s", (user_id,))
    return (row[0], row[1] or {}) if row else None


def set_state(user_id: str, state: str, data: dict[str, Any] | None = None) -> None:
    execute(
        """
        insert into sos_core.user_states (user_id, state, data)
        values (%s, %s, %s::jsonb)
        on conflict (user_id) do update set state = excluded.state, data = excluded.data, updated_at = now()
        """,
        (user_id, state, json.dumps(data or {}, ensure_ascii=False)),
    )


def clear_state(user_id: str) -> None:
    execute("delete from sos_core.user_states where user_id = %s", (user_id,))


def main_buttons(user_id: str | None = None) -> list[dict[str, Any]]:
    has_child_sos = False
    has_profile = False
    is_child_only = False
    if user_id:
        has_child_sos = bool(
            fetchone(
                "select 1 from sos_family.links where child_user_id = %s and status = 'active' limit 1",
                (user_id,),
            )
        )
        user = get_user(user_id)
        roles = set((user or {}).get("roles") or [])
        family_roles = roles & {"child", "parent"}
        has_profile = bool(family_roles or has_child_sos)
        is_child_only = has_child_sos and "parent" not in family_roles

    if is_child_only:
        return [
            callback_button("Нужна помощь", "child:help"),
            callback_button("Еще", "child:more"),
        ]

    buttons = [
        callback_button("Я родитель", "parent:menu"),
        callback_button("Я ребенок", "child:menu"),
        callback_button("Помощь", "help:menu"),
        callback_button("Рекомендации безопасности", "guide:menu"),
    ]
    alert_buttons: list[dict[str, Any]] = []
    if has_child_sos:
        alert_buttons.append(callback_button("Нужна помощь", "child:sos"))
    if has_profile:
        buttons.append(callback_button("Мои данные / связи", "profile:menu"))
    buttons = alert_buttons + buttons
    return buttons


def main_menu_text(with_greeting: bool = False) -> str:
    prefix = "Здравствуйте! Я чат-бот Ошмазик.\n\n" if with_greeting else ""
    return (
        prefix
        + "Если ребенку нужна помощь, родители получат сигнал и геолокацию.\n"
        "Сервис работает даже при ограничении мобильного интернета по белым спискам.\n\n"
        "Выберите раздел:"
    )


async def show_main(chat_id: str, user_id: str | None = None, *, with_greeting: bool = False) -> None:
    user = get_user(user_id) if user_id else None
    roles = set((user or {}).get("roles") or [])
    has_child_sos = bool(
        user_id
        and fetchone(
            "select 1 from sos_family.links where child_user_id = %s and status = 'active' limit 1",
            (user_id,),
        )
    )
    if has_child_sos and "parent" not in roles:
        text = ("Здравствуйте! Я чат-бот Ошмазик.\n\n" if with_greeting else "") + "Если нужна помощь, нажми кнопку."
    else:
        text = main_menu_text(with_greeting)
    await send_message(chat_id, text, main_buttons(user_id))


async def ask_name(chat_id: str, user_id: str, next_state: str, role_label: str) -> None:
    set_state(user_id, "await_name", {"next": next_state, "role_label": role_label})
    await send_message(chat_id, f"Как вас называть в разделе «{role_label}»?\n\nВведите короткое имя без лишних персональных данных.")


def create_parent_code(parent_user_id: str) -> str:
    code = f"{secrets.randbelow(900000) + 100000}"
    expires_at = now_utc() + timedelta(minutes=FAMILY_CODE_TTL_MINUTES)
    execute(
        "insert into sos_family.link_codes (code, parent_user_id, expires_at) values (%s, %s, %s)",
        (code, parent_user_id, expires_at),
    )
    return code


async def parent_menu(chat_id: str, user_id: str) -> None:
    user = get_user(user_id)
    if not user or not user.get("display_name"):
        await ask_name(chat_id, user_id, "parent_menu", "родитель")
        return
    add_role(user_id, "parent")
    rows = fetchall(
        """
        select u.display_name, l.status
        from sos_family.links l
        join sos_core.users u on u.user_id = l.child_user_id
        where l.parent_user_id = %s and l.status in ('active', 'pending')
        order by l.created_at desc
        """,
        (user_id,),
    )
    children = "\n".join(f"- {name or 'Ребенок'}: {status}" for name, status in rows) or "Пока нет привязанных детей."
    has_active_child = any(status == "active" for _name, status in rows)
    buttons = [callback_button("Добавить ребенка", "parent:create_code")]
    if has_active_child:
        buttons.append(callback_button("Где мой ребенок?", "parent:locate"))
        buttons.append(callback_button("Управление детьми", "parent:children"))
    buttons.append(callback_button("Главное меню", "main:menu"))
    await send_message(
        chat_id,
        f"Раздел родителя\n\nДети:\n{children}",
        buttons,
    )


async def parent_children_menu(chat_id: str, user_id: str) -> None:
    rows = fetchall(
        """
        select l.id, u.display_name, l.status
        from sos_family.links l
        join sos_core.users u on u.user_id = l.child_user_id
        where l.parent_user_id = %s and l.status = 'active'
        order by u.display_name nulls last, l.created_at desc
        """,
        (user_id,),
    )
    if not rows:
        await send_message(chat_id, "Активных связей с детьми пока нет.", [callback_button("Назад", "parent:menu")])
        return
    buttons = [callback_button(f"Отключить: {name or 'Ребенок'}", f"parentchild:remove:{link_id}") for link_id, name, _status in rows]
    buttons.append(callback_button("Назад", "parent:menu"))
    await send_message(chat_id, "Выберите связь, которую нужно отключить.", buttons)


async def parent_locate_menu(chat_id: str, user_id: str) -> None:
    rows = fetchall(
        """
        select u.user_id, u.display_name, u.chat_id
        from sos_family.links l
        join sos_core.users u on u.user_id = l.child_user_id
        where l.parent_user_id = %s and l.status = 'active'
        order by u.display_name nulls last, l.created_at desc
        """,
        (user_id,),
    )
    if not rows:
        await send_message(chat_id, "Активных связей с детьми пока нет.", [callback_button("Назад", "parent:menu")])
        return
    buttons = [callback_button(name or "Ребенок", f"parentloc:req:{child_user_id}") for child_user_id, name, _child_chat_id in rows]
    buttons.append(callback_button("Назад", "parent:menu"))
    await send_message(chat_id, "У какого ребенка запросить геолокацию?", buttons)


async def child_menu(chat_id: str, user_id: str) -> None:
    user = get_user(user_id)
    if not user or not user.get("display_name"):
        await ask_name(chat_id, user_id, "child_menu", "ребенок")
        return
    add_role(user_id, "child")
    parents = fetchall(
        """
        select u.display_name
        from sos_family.links l
        join sos_core.users u on u.user_id = l.parent_user_id
        where l.child_user_id = %s and l.status = 'active'
        order by l.created_at desc
        """,
        (user_id,),
    )
    text = "Раздел ребенка\n\n"
    text += "Родители: " + (", ".join(row[0] or "Родитель" for row in parents) if parents else "пока не привязаны")
    if not parents:
        await send_message(
            chat_id,
            text,
            [
                callback_button("Ввести код родителя", "child:enter_code"),
                callback_button("Главное меню", "main:menu"),
            ],
        )
        return
    await send_message(
        chat_id,
        text,
        [
            callback_button("Ввести код родителя", "child:enter_code"),
            callback_button("Опасность", "child:sos"),
            callback_button("Я потерялся", "child:lost"),
            callback_button("Тестовая тревога", "child:test"),
            callback_button("Главное меню", "main:menu"),
        ],
    )


async def child_help_menu(chat_id: str) -> None:
    await send_message(
        chat_id,
        "Что случилось?",
        [
            callback_button("Опасность", "child:sos"),
            callback_button("Я потерялся", "child:lost"),
            callback_button("Тестовая тревога", "child:test"),
            callback_button("Назад", "main:menu"),
        ],
    )


async def child_more_menu(chat_id: str) -> None:
    await send_message(
        chat_id,
        "Дополнительно",
        [
            callback_button("Мои родители", "child:parents"),
            callback_button("Добавить родителя", "child:enter_code"),
            callback_button("Рекомендации безопасности", "guide:child"),
            callback_button("Мои данные", "profile:menu"),
            callback_button("Главное меню", "main:menu"),
        ],
    )


async def child_parents(chat_id: str, user_id: str) -> None:
    rows = fetchall(
        """
        select u.display_name
        from sos_family.links l
        join sos_core.users u on u.user_id = l.parent_user_id
        where l.child_user_id = %s and l.status = 'active'
        order by l.created_at desc
        """,
        (user_id,),
    )
    text = "Мои родители\n\n" + ("\n".join(f"- {row[0] or 'Родитель'}" for row in rows) if rows else "Пока нет привязанных родителей.")
    await send_message(text=text, chat_id=chat_id, buttons=[callback_button("Назад", "child:more")])


async def staff_menu(chat_id: str, user_id: str) -> None:
    rows = fetchall(
        """
        select s.id, o.name, o.org_type, s.status
        from sos_org.staff s
        join sos_org.organizations o on o.id = s.organization_id
        where s.user_id = %s
        order by s.created_at desc
        """,
        (user_id,),
    )
    lines = ["Раздел сотрудника организации"]
    if rows:
        lines.append("")
        lines.append("Ваши учреждения:")
        for _staff_id, org_name, org_type, status in rows:
            lines.append(f"- {org_name} ({org_type}): {status}")
    else:
        lines.append("\nВы пока не привязаны к учреждению.")
    buttons = [
        callback_button("Регистрация в школе", "staff:start:school"),
        callback_button("Регистрация в медорганизации", "staff:start:medical"),
    ]
    if any(status == "approved" for _staff_id, _org_name, _org_type, status in rows):
        buttons.extend(
            [
                callback_button("Тревожная кнопка", "staff:alert_menu"),
                callback_button("Тестовая тревога", "staff:test_menu"),
            ]
        )
    buttons.append(callback_button("Главное меню", "main:menu"))
    await send_message(
        chat_id,
        "\n".join(lines),
        buttons,
    )


def find_org_by_inn(inn: str, org_type: str | None = None) -> dict[str, Any] | None:
    if org_type:
        row = fetchone(
            "select id, org_type, name, inn, status, approval_chat_id, alert_chat_id from sos_org.organizations where inn = %s and org_type = %s",
            (inn, org_type),
        )
    else:
        row = fetchone(
            "select id, org_type, name, inn, status, approval_chat_id, alert_chat_id from sos_org.organizations where inn = %s",
            (inn,),
        )
    if not row:
        return None
    return {"id": row[0], "org_type": row[1], "name": row[2], "inn": row[3], "status": row[4], "approval_chat_id": row[5], "alert_chat_id": row[6]}


def get_org_alert_chat_ids(org_id: str) -> list[str]:
    rows = fetchall(
        """
        select chat_id
        from sos_org.alert_chats
        where organization_id = %s and is_active = true
        order by created_at
        """,
        (org_id,),
    )
    chat_ids = [str(row[0]) for row in rows]
    legacy = fetchone("select alert_chat_id from sos_org.organizations where id = %s", (org_id,))
    if legacy and legacy[0] and str(legacy[0]) not in chat_ids:
        chat_ids.append(str(legacy[0]))
    return chat_ids


async def start_staff_registration(chat_id: str, user_id: str, staff_type: str) -> None:
    set_state(user_id, "staff_await_inn", {"staff_type": staff_type})
    await send_message(chat_id, "Введите ИНН учреждения цифрами.\n\nПо ИНН бот найдет подключенную школу или медорганизацию.")


async def continue_staff_registration(chat_id: str, user_id: str, org: dict[str, Any], staff_type: str) -> None:
    set_state(user_id, "staff_await_full_name", {"staff_type": staff_type, "organization_id": org["id"]})
    await send_message(chat_id, f"Найдена организация:\n{org['name']}\nИНН: {org['inn']}\n\nВведите ваши ФИО.")


def create_staff_request(user_id: str, org_id: str, staff_type: str, full_name: str, phone: str, position: str | None = None) -> tuple[str, str]:
    staff_id = new_id()
    request_id = new_id()
    execute(
        """
        insert into sos_org.staff (id, user_id, organization_id, staff_type, full_name, phone, position, status)
        values (%s, %s, %s, %s, %s, %s, %s, 'pending')
        on conflict (user_id, organization_id) do update set
            staff_type = excluded.staff_type,
            full_name = excluded.full_name,
            phone = excluded.phone,
            position = excluded.position,
            status = case when sos_org.staff.status = 'blocked' then 'blocked' else 'pending' end,
            created_at = now()
        returning id
        """,
        (staff_id, user_id, org_id, staff_type, full_name, phone, position),
    )
    row = fetchone("select id from sos_org.staff where user_id = %s and organization_id = %s", (user_id, org_id))
    real_staff_id = row[0]
    execute(
        """
        insert into sos_org.staff_requests (id, staff_id, organization_id, user_id, status)
        values (%s, %s, %s, %s, 'pending')
        """,
        (request_id, real_staff_id, org_id, user_id),
    )
    return real_staff_id, request_id


async def notify_staff_request(request_id: str) -> None:
    row = fetchone(
        """
        select r.id, o.name, o.inn, o.org_type, o.approval_chat_id, s.full_name, s.phone, s.position, s.user_id
        from sos_org.staff_requests r
        join sos_org.staff s on s.id = r.staff_id
        join sos_org.organizations o on o.id = r.organization_id
        where r.id = %s
        """,
        (request_id,),
    )
    if not row:
        return
    request_id, org_name, inn, org_type, approval_chat_id, full_name, phone, position, user_id = row
    if not approval_chat_id:
        logger.warning("Organization has no approval_chat_id request_id=%s", request_id)
        return
    text = (
        "Новая заявка сотрудника\n\n"
        f"Тип: {org_type}\n"
        f"Организация: {org_name}\n"
        f"ИНН: {inn}\n"
        f"ФИО: {full_name}\n"
        f"Телефон: {phone}\n"
        f"Должность: {position or '-'}\n"
        f"MAX user_id: {user_id}"
    )
    await send_message(
        approval_chat_id,
        text,
        [
            callback_button("Подтвердить", f"staffreq:approve:{request_id}"),
            callback_button("Отказать", f"staffreq:reject:{request_id}"),
        ],
        columns=2,
    )


def active_staff(user_id: str) -> list[dict[str, Any]]:
    rows = fetchall(
        """
        select s.id, s.full_name, s.phone, s.position, o.id, o.name, o.org_type, o.alert_chat_id
        from sos_org.staff s
        join sos_org.organizations o on o.id = s.organization_id
        where s.user_id = %s and s.status = 'approved' and o.status = 'active'
        order by o.name
        """,
        (user_id,),
    )
    return [
        {"staff_id": r[0], "full_name": r[1], "phone": r[2], "position": r[3], "org_id": r[4], "org_name": r[5], "org_type": r[6], "alert_chat_id": r[7]}
        for r in rows
    ]


async def staff_alert_menu(chat_id: str, user_id: str, is_test: bool) -> None:
    staff_rows = active_staff(user_id)
    if not staff_rows:
        await send_message(chat_id, "У вас нет подтвержденных учреждений.", [callback_button("Регистрация", "staff:menu"), callback_button("Главное меню", "main:menu")])
        return
    buttons = [
        callback_button(item["org_name"][:60], f"staffalert:confirm:{item['staff_id']}:{1 if is_test else 0}")
        for item in staff_rows
    ]
    await send_message(chat_id, "Выберите учреждение для тестовой тревоги." if is_test else "Выберите учреждение для тревожной кнопки.", buttons + [callback_button("Назад", "staff:menu")])


def cooldown_remaining(staff_id: str) -> int:
    row = fetchone(
        """
        select created_at
        from sos_org.alerts
        where staff_id = %s and is_test = false
        order by created_at desc
        limit 1
        """,
        (staff_id,),
    )
    if not row:
        return 0
    elapsed = now_utc() - row[0]
    remaining = timedelta(minutes=ORG_ALERT_COOLDOWN_MINUTES) - elapsed
    return max(0, int(remaining.total_seconds() // 60) + 1)


async def create_org_alert(chat_id: str, user_id: str, staff_id: str, is_test: bool) -> None:
    row = fetchone(
        """
        select s.id, s.full_name, s.phone, s.position, o.id, o.name, o.org_type
        from sos_org.staff s
        join sos_org.organizations o on o.id = s.organization_id
        where s.id = %s and s.user_id = %s and s.status = 'approved' and o.status = 'active'
        """,
        (staff_id, user_id),
    )
    if not row:
        await send_message(chat_id, "Не найдено подтвержденное учреждение для тревоги.", [callback_button("Главное меню", "main:menu")])
        return
    staff_id, full_name, phone, position, org_id, org_name, org_type = row
    alert_chat_ids = get_org_alert_chat_ids(org_id)
    if not alert_chat_ids:
        await send_message(chat_id, "Для учреждения пока не назначен чат тревог. Обратитесь к администратору.", [callback_button("Главное меню", "main:menu")])
        return
    if not is_test:
        minutes = cooldown_remaining(staff_id)
        if minutes > 0:
            await send_message(chat_id, f"Повторная реальная тревога будет доступна через {minutes} мин. Тестовая тревога доступна без ограничения.")
            return
    alert_id = new_id()
    execute(
        """
        insert into sos_org.alerts (id, organization_id, staff_id, alert_type, status, is_test)
        values (%s, %s, %s, %s, 'sent', %s)
        """,
        (alert_id, org_id, staff_id, "staff_test" if is_test else "staff_alarm", is_test),
    )
    title = "ТЕСТОВАЯ ТРЕВОГА" if is_test else "ТРЕВОЖНАЯ КНОПКА"
    text = (
        f"{title}\n\n"
        f"Организация: {org_name}\n"
        f"Тип: {org_type}\n"
        f"ФИО: {full_name}\n"
        f"Телефон: {phone}\n"
        f"Должность: {position or '-'}\n"
        f"Время: {now_utc().strftime('%d.%m.%Y %H:%M:%S')} UTC"
    )
    for alert_chat_id in alert_chat_ids:
        await send_message(alert_chat_id, text, [callback_button("Принял", f"orgalert:accept:{alert_id}")])
    await send_message(chat_id, "Тестовая тревога отправлена." if is_test else "Тревога отправлена. Ожидайте подтверждения принятия.", [callback_button("Главное меню", "main:menu")])
    audit(user_id, chat_id, "org_alert_sent", "org_alert", alert_id, {"is_test": is_test, "organization_id": org_id, "alert_chat_ids": alert_chat_ids})


async def child_sos_start(chat_id: str, user_id: str, alert_type: str) -> None:
    parents = fetchall("select parent_user_id from sos_family.links where child_user_id = %s and status = 'active'", (user_id,))
    if not parents:
        await send_message(chat_id, "Сначала нужно привязать хотя бы одного родителя.", [callback_button("Ввести код родителя", "child:enter_code")])
        return
    set_state(user_id, "child_await_location", {"alert_type": alert_type})
    await send_message(
        chat_id,
        "Нажмите кнопку ниже и отправьте геолокацию. После этого родителям придет тревога.",
        [geo_button(), callback_button("Отмена", "child:menu")],
    )


async def send_family_alert(chat_id: str, user_id: str, location: tuple[float, float, dict[str, Any]], alert_type: str) -> None:
    lat, lon, payload = location
    user = get_user(user_id) or {}
    child_name = user.get("display_name") or "Ребенок"
    alert_id = new_id()
    execute(
        """
        insert into sos_family.alerts (id, child_user_id, alert_type, status, latitude, longitude, location_payload)
        values (%s, %s, %s, 'sent', %s, %s, %s::jsonb)
        """,
        (alert_id, user_id, alert_type, lat, lon, json.dumps(payload, ensure_ascii=False)),
    )
    parents = fetchall(
        """
        select u.user_id, u.chat_id, u.display_name
        from sos_family.links l
        join sos_core.users u on u.user_id = l.parent_user_id
        where l.child_user_id = %s and l.status = 'active'
        """,
        (user_id,),
    )
    label = "Нужна помощь" if alert_type == "sos" else "Ребенок потерялся" if alert_type == "lost" else "Тестовая тревога"
    for parent_user_id, parent_chat_id, _parent_name in parents:
        execute(
            "insert into sos_family.alert_recipients (alert_id, parent_user_id, sent_at) values (%s, %s, now()) on conflict do nothing",
            (alert_id, parent_user_id),
        )
        target = parent_chat_id or parent_user_id
        await send_message_rows(
            target,
            f"{label}\n\nРебенок: {child_name}\nГеолокация: {lat:.6f}, {lon:.6f}",
            map_buttons(lat, lon)
            + [
                [
                    callback_button("Принял", f"famalert:accept:{alert_id}"),
                    callback_button("Еду", f"famalert:coming:{alert_id}"),
                    callback_button("Закрыть", f"famalert:close:{alert_id}"),
                ]
            ],
        )
    await send_message(chat_id, "Тревога отправлена родителям.", [callback_button("Главное меню", "main:menu")])
    clear_state(user_id)
    audit(user_id, chat_id, "family_alert_sent", "family_alert", alert_id, {"alert_type": alert_type})


async def send_parent_location_response(
    chat_id: str,
    user_id: str,
    location: tuple[float, float, dict[str, Any]],
    data: dict[str, Any],
) -> None:
    lat, lon, _payload = location
    child = get_user(user_id) or {}
    child_name = child.get("display_name") or "Ребенок"
    parent_chat_id = data.get("parent_chat_id") or data.get("parent_user_id")
    if parent_chat_id:
        await send_message_rows(
            parent_chat_id,
            f"Геолокация ребенка\n\nРебенок: {child_name}\nГеолокация: {lat:.6f}, {lon:.6f}",
            map_buttons(lat, lon) + [[callback_button("Главное меню", "main:menu")]],
        )
    await send_message(chat_id, "Геолокация отправлена родителю.", [callback_button("Главное меню", "main:menu")])
    clear_state(user_id)
    audit(user_id, chat_id, "child_location_sent_to_parent", "family_link", None, {"parent_user_id": data.get("parent_user_id")})


async def guide_menu(chat_id: str, user_id: str) -> None:
    user = get_user(user_id) or {}
    roles = set(user.get("roles") or [])
    guide_roles = roles & {"child", "parent"}
    if len(guide_roles) != 1:
        await send_guide_choice(chat_id)
    elif "child" in guide_roles:
        await send_guide_with_image(chat_id, "child", child_guide())
    elif "parent" in guide_roles:
        await send_guide_with_image(chat_id, "parent", parent_guide())


async def send_guide_choice(chat_id: str) -> None:
    await send_message(
        chat_id,
        "Для кого показать рекомендации?",
        [
            callback_button("Для ребенка", "guide:child"),
            callback_button("Для родителя", "guide:parent"),
        ],
    )


async def send_guide_with_image(chat_id: str, kind: str, text: str) -> None:
    buttons = [callback_button("Главное меню", "main:menu")]
    with contextlib.suppress(Exception):
        image = await recommendation_image_attachment(kind)
        if image:
            try:
                await send_message_with_attachments(chat_id, text, [image], buttons)
                return
            except httpx.HTTPStatusError as exc:
                if "attachment.not.ready" not in exc.response.text:
                    raise
                await asyncio.sleep(2)
                await send_message_with_attachments(chat_id, text, [image], buttons)
                return
    logger.warning("Sending %s guide without image", kind)
    await send_message(chat_id, text, buttons)


async def help_menu(chat_id: str) -> None:
    await send_message(
        chat_id,
        "Для кого показать помощь?",
        [
            callback_button("Я родитель", "help:parent"),
            callback_button("Я ребенок", "help:child"),
            callback_button("Главное меню", "main:menu"),
        ],
    )


async def send_help_with_image(chat_id: str, kind: str, text: str) -> None:
    buttons = [callback_button("Главное меню", "main:menu")]
    with contextlib.suppress(Exception):
        image = await help_image_attachment(kind)
        if image:
            try:
                await send_message_with_attachments(chat_id, text, [image], buttons)
                return
            except httpx.HTTPStatusError as exc:
                if "attachment.not.ready" not in exc.response.text:
                    raise
                await asyncio.sleep(2)
                await send_message_with_attachments(chat_id, text, [image], buttons)
                return
    logger.warning("Sending %s help without image", kind)
    await send_message(chat_id, text, buttons)


def parent_help_text() -> str:
    return (
        "Помощь для родителя\n\n"
        "Этот бот помогает быстро получить сигнал от ребенка, если ему нужна помощь или если он потерялся.\n\n"
        "Что можно делать:\n\n"
        "1. Подключить ребенка\n"
        "Вы создаете одноразовый код, ребенок вводит его у себя в боте, после этого вы подтверждаете связь.\n\n"
        "2. Получать тревогу от ребенка\n"
        "Если ребенок нажмет кнопку помощи и отправит геолокацию, вам придет сообщение с координатами и кнопками для открытия места в картах.\n\n"
        "3. Открыть геолокацию в картах\n"
        "В сообщении будут кнопки: Google Map, Яндекс карты, Яндекс навигатор, 2Gis.\n\n"
        "4. Сообщить ребенку, что вы реагируете\n"
        "Под тревогой есть кнопки: Принял, Еду, Закрыть. Если нажать «Еду», ребенок получит сообщение, что родитель едет.\n\n"
        "5. Запросить геолокацию ребенка\n"
        "В разделе родителя можно нажать «Где мой ребенок?». Ребенку придет запрос отправить геолокацию. Геолокация отправляется только после действия ребенка.\n\n"
        "6. Отключить связь с ребенком\n"
        "Если связь больше не нужна, ее можно отключить в разделе управления детьми.\n\n"
        "Как подключить ребенка:\n\n"
        "1. Нажмите «Я родитель».\n"
        "2. Введите свое имя, если бот попросит.\n"
        "3. Нажмите «Добавить ребенка».\n"
        "4. Бот покажет одноразовый код.\n"
        "5. Передайте этот код ребенку.\n"
        "6. Ребенок должен открыть бот, нажать «Я ребенок» и ввести код.\n"
        "7. Когда вам придет запрос на связь, нажмите «Подтвердить».\n\n"
        "После этого ребенок будет подключен, а вы сможете получать тревоги и запрашивать геолокацию."
    )


def child_help_text() -> str:
    return (
        "Помощь для ребенка\n\n"
        "Этот бот нужен, чтобы быстро сообщить родителям, если тебе нужна помощь или если ты потерялся.\n\n"
        "Что можно делать:\n\n"
        "1. Подключиться к родителю\n"
        "Родитель дает тебе одноразовый код. Ты вводишь его в боте, и родитель подтверждает связь.\n\n"
        "2. Быстро попросить помощь\n"
        "После подключения в главном меню будет кнопка «Нужна помощь».\n\n"
        "3. Отправить тревогу родителям\n"
        "Можно выбрать «Опасность» или «Я потерялся». После этого бот попросит отправить геолокацию. Родители получат сообщение и смогут открыть место на карте.\n\n"
        "4. Отправить тестовую тревогу\n"
        "Кнопка «Тестовая тревога» нужна, чтобы вместе с родителями проверить, как все работает.\n\n"
        "5. Ответить на запрос родителя\n"
        "Если родитель нажмет «Где мой ребенок?», тебе придет просьба отправить геолокацию. Ты можешь отправить ее или отказаться.\n\n"
        "Как подключиться к родителю:\n\n"
        "1. Попроси родителя открыть бот и нажать «Я родитель».\n"
        "2. Родитель нажмет «Добавить ребенка» и получит код.\n"
        "3. Открой бот у себя.\n"
        "4. Нажми «Я ребенок».\n"
        "5. Введи свое имя, если бот попросит.\n"
        "6. Нажми «Ввести код родителя».\n"
        "7. Введи код, который дал родитель.\n"
        "8. Дождись, пока родитель подтвердит связь.\n\n"
        "После подтверждения в боте появится кнопка «Нужна помощь». Если что-то случилось, нажми ее и отправь геолокацию."
    )


def child_guide() -> str:
    return (
        "Рекомендации для ребенка\n\n"
        "1. Если потерялся, остановись и не уходи дальше.\n"
        "2. Нажми кнопку помощи и отправь геолокацию.\n"
        "3. Обратись к полицейскому, сотруднику магазина, врачу или женщине с ребенком.\n"
        "4. Не уходи с незнакомым человеком, даже если он говорит, что знает родителей.\n"
        "5. Держи телефон заряженным и отвечай родителям."
    )


def parent_guide() -> str:
    return (
        "Рекомендации для родителя\n\n"
        "1. Один раз проверьте с ребенком тестовую тревогу.\n"
        "2. Объясните, что кнопка помощи нажимается только при опасности или если ребенок потерялся.\n"
        "3. После тревоги сначала подтвердите принятие, затем звоните ребенку.\n"
        "4. Держите включенными уведомления MAX.\n"
        "5. При реальной опасности параллельно обращайтесь в 112."
    )


def staff_guide() -> str:
    return (
        "Рекомендации для сотрудника\n\n"
        "1. Реальная тревога отправляется в ответственный чат учреждения.\n"
        "2. Перед отправкой бот попросит подтверждение.\n"
        "3. Повторная реальная тревога доступна через 10 минут.\n"
        "4. Для проверки используйте тестовую тревогу.\n"
        "5. После принятия вызова вы получите уведомление."
    )


async def profile_menu(chat_id: str, user_id: str) -> None:
    user = get_user(user_id) or {}
    roles = ", ".join(role for role in (user.get("roles") or []) if role in {"child", "parent"}) or "не выбраны"
    family = fetchone("select count(*) from sos_family.links where (parent_user_id = %s or child_user_id = %s) and status = 'active'", (user_id, user_id))[0]
    children = fetchall(
        """
        select u.display_name
        from sos_family.links l
        join sos_core.users u on u.user_id = l.child_user_id
        where l.parent_user_id = %s and l.status = 'active'
        order by u.display_name nulls last, l.created_at desc
        """,
        (user_id,),
    )
    parents = fetchall(
        """
        select u.display_name
        from sos_family.links l
        join sos_core.users u on u.user_id = l.parent_user_id
        where l.child_user_id = %s and l.status = 'active'
        order by u.display_name nulls last, l.created_at desc
        """,
        (user_id,),
    )
    lines = [
        "Мои данные",
        "",
        f"Имя: {user.get('display_name') or '-'}",
        f"Роли: {roles}",
        f"Семейные связи: {family}",
    ]
    if children:
        lines.append("")
        lines.append("Дети:")
        lines.extend(f"- {name or 'Ребенок'}" for (name,) in children)
    if parents:
        lines.append("")
        lines.append("Родители:")
        lines.extend(f"- {name or 'Родитель'}" for (name,) in parents)
    buttons = [callback_button("Изменить имя", "profile:name")]
    if children:
        buttons.append(callback_button("Управление детьми", "parent:children"))
    buttons.append(callback_button("Главное меню", "main:menu"))
    await send_message(
        chat_id,
        "\n".join(lines),
        buttons,
    )


async def admin_menu(chat_id: str, user_id: str) -> None:
    if user_id not in ADMIN_USER_IDS:
        await send_message(chat_id, "Недостаточно прав.")
        return
    org_count = fetchone("select count(*) from sos_org.organizations")[0]
    pending = fetchone("select count(*) from sos_org.staff_requests where status = 'pending'")[0]
    await send_message(
        chat_id,
        f"Администрирование\n\nОрганизаций: {org_count}\nЗаявок сотрудников: {pending}\n\n"
        "Добавление учреждений и чатов можно выполнить кнопками ниже.\n"
        "Если удобнее командами: /add_org и /orgs.",
        [
            callback_button("Добавить организацию", "admin:add_org"),
            callback_button("Чат заявок", "admin:set_approval_chat"),
            callback_button("Чат тревог", "admin:add_alert_chat"),
            callback_button("Отключить сотрудника", "admin:disable_staff"),
            callback_button("Список организаций", "admin:orgs"),
            callback_button("Главное меню", "main:menu"),
        ],
    )


async def list_orgs(chat_id: str) -> None:
    rows = fetchall(
        """
        select o.org_type, o.name, o.inn, o.status, o.approval_chat_id,
               count(ac.id) filter (where ac.is_active = true) as alert_chat_count,
               o.alert_chat_id
        from sos_org.organizations o
        left join sos_org.alert_chats ac on ac.organization_id = o.id
        group by o.id
        order by o.created_at desc
        limit 30
        """
    )
    if not rows:
        await send_message(chat_id, "Организаций пока нет.")
        return
    lines = ["Организации:"]
    for org_type, name, inn, status, approval, alert_count, legacy_alert in rows:
        total_alert_chats = int(alert_count or 0) + (1 if legacy_alert else 0)
        lines.append(f"- {name} ({org_type}), ИНН {inn}, {status}, заявки: {'+' if approval else '-'}, чатов тревог: {total_alert_chats}")
    await send_message(chat_id, "\n".join(lines), [callback_button("Главное меню", "main:menu")])


async def admin_staff_list(chat_id: str, org_id: str, org_name: str, inn: str) -> None:
    rows = fetchall(
        """
        select s.id, s.full_name, s.phone, s.position, u.user_id
        from sos_org.staff s
        join sos_core.users u on u.user_id = s.user_id
        where s.organization_id = %s and s.status = 'approved'
        order by s.full_name
        limit 50
        """,
        (org_id,),
    )
    if not rows:
        await send_message(
            chat_id,
            f"В организации нет активных сотрудников.\n{org_name}\nИНН: {inn}",
            [callback_button("Администрирование", "admin:menu")],
        )
        return
    buttons = [
        callback_button(f"{full_name} · {phone}", f"adminstaff:disable:{staff_id}")
        for staff_id, full_name, phone, _position, _staff_user_id in rows
    ]
    buttons.append(callback_button("Администрирование", "admin:menu"))
    await send_message(
        chat_id,
        f"Активные сотрудники\n{org_name}\nИНН: {inn}\n\nВыберите сотрудника для отключения.",
        buttons,
    )


async def bind_chat(chat_id: str, user_id: str, text: str, bind_type: str) -> None:
    if user_id not in ADMIN_USER_IDS:
        await send_message(chat_id, "Команда доступна только администратору.")
        return
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await send_message(chat_id, "Укажите ИНН: /bind_alert 1234567890")
        return
    inn = clean_inn(parts[1])
    org = find_org_by_inn(inn)
    if not org:
        await send_message(chat_id, "Организация с таким ИНН не найдена.")
        return
    if bind_type == "alert":
        execute(
            """
            insert into sos_org.alert_chats (id, organization_id, chat_id, created_by_user_id)
            values (%s, %s, %s, %s)
            on conflict (organization_id, chat_id) do update set is_active = true
            """,
            (new_id(), org["id"], chat_id, user_id),
        )
    else:
        execute("update sos_org.organizations set approval_chat_id = %s, updated_at = now() where id = %s", (chat_id, org["id"]))
    audit(user_id, chat_id, f"bind_{bind_type}_chat", "organization", org["id"], {"inn": inn})
    await send_message(chat_id, f"Чат привязан к организации: {org['name']}\nНазначение: {'тревоги' if bind_type == 'alert' else 'заявки'}")


async def handle_text_state(chat_id: str, user_id: str, text: str, update: dict[str, Any]) -> bool:
    state_item = get_state(user_id)
    if not state_item:
        return False
    state, data = state_item
    if state.startswith("staff_") or state.startswith("admin_"):
        clear_state(user_id)
        await show_main(chat_id, user_id)
        return True

    if state == "await_name":
        if not NAME_RE.match(text):
            await send_message(chat_id, "Введите короткое имя: от 2 до 120 символов.")
            return True
        set_display_name(user_id, text)
        clear_state(user_id)
        next_state = data.get("next")
        if next_state == "parent_menu":
            await parent_menu(chat_id, user_id)
        elif next_state == "child_menu":
            await child_menu(chat_id, user_id)
        else:
            await show_main(chat_id, user_id)
        return True

    if state == "child_await_parent_code":
        code = re.sub(r"\D+", "", text)
        row = fetchone(
            """
            select code, parent_user_id
            from sos_family.link_codes
            where code = %s and used_at is null and expires_at > now()
            """,
            (code,),
        )
        if not row:
            await send_message(chat_id, "Код не найден или истек. Попросите родителя создать новый код.")
            return True
        _code, parent_user_id = row
        link_id = new_id()
        execute(
            """
            insert into sos_family.links (id, parent_user_id, child_user_id, status)
            values (%s, %s, %s, 'pending')
            on conflict (parent_user_id, child_user_id) do update set status = 'pending', created_at = now(), decided_at = null
            """,
            (link_id, parent_user_id, user_id),
        )
        execute("update sos_family.link_codes set used_at = now() where code = %s", (code,))
        clear_state(user_id)
        child = get_user(user_id) or {}
        parent = get_user(parent_user_id) or {}
        target = parent.get("chat_id") or parent_user_id
        await send_message(
            target,
            f"Ребенок {child.get('display_name') or 'без имени'} хочет привязаться к вам.",
            [
                callback_button("Подтвердить", f"familylink:approve:{user_id}"),
                callback_button("Отказать", f"familylink:reject:{user_id}"),
            ],
            columns=2,
        )
        await send_message(chat_id, "Запрос отправлен родителю. После подтверждения появится кнопка помощи.")
        return True

    if state == "child_await_location":
        location = extract_location(update)
        if not location:
            await send_message(chat_id, "Не вижу геолокацию. Нажмите кнопку «Отправить геолокацию».", [geo_button()])
            return True
        await send_family_alert(chat_id, user_id, location, data.get("alert_type", "sos"))
        return True

    if state == "child_await_parent_location":
        location = extract_location(update)
        if not location:
            await send_message(chat_id, "Не вижу геолокацию. Нажмите кнопку «Отправить геолокацию».", [geo_button()])
            return True
        await send_parent_location_response(chat_id, user_id, location, data)
        return True

    if state == "staff_await_inn":
        staff_type = data["staff_type"]
        inn = clean_inn(text)
        if not INN_RE.match(inn):
            await send_message(chat_id, "ИНН должен содержать 10 или 12 цифр.")
            return True
        org = find_org_by_inn(inn, staff_type)
        if not org:
            await send_message(chat_id, "Организация с таким ИНН не подключена. Проверьте ИНН или обратитесь к администратору.")
            return True
        if org["status"] != "active":
            await send_message(chat_id, "Организация найдена, но пока отключена.")
            return True
        await continue_staff_registration(chat_id, user_id, org, staff_type)
        return True

    if state == "staff_await_full_name":
        if not NAME_RE.match(text):
            await send_message(chat_id, "Введите ФИО текстом.")
            return True
        data["full_name"] = text
        set_state(user_id, "staff_await_phone", data)
        await send_message(chat_id, "Введите номер телефона.", [contact_button()])
        return True

    if state == "staff_await_phone":
        if not PHONE_RE.match(text):
            await send_message(chat_id, "Введите корректный номер телефона.", [contact_button()])
            return True
        data["phone"] = text
        set_state(user_id, "staff_await_position", data)
        await send_message(chat_id, "Введите должность или отправьте «-», если не нужно указывать.")
        return True

    if state == "staff_await_position":
        org_id = data["organization_id"]
        position = None if text.strip() == "-" else text.strip()
        staff_id, request_id = create_staff_request(user_id, org_id, data["staff_type"], data["full_name"], data["phone"], position)
        add_role(user_id, "staff")
        clear_state(user_id)
        await notify_staff_request(request_id)
        await send_message(chat_id, "Заявка отправлена на подтверждение. После одобрения появится тревожная кнопка.", [callback_button("Главное меню", "main:menu")])
        audit(user_id, chat_id, "staff_request_created", "staff", staff_id, {"request_id": request_id})
        return True

    if state == "admin_add_org_type":
        return False

    if state == "admin_add_org_name":
        if not NAME_RE.match(text):
            await send_message(chat_id, "Введите название учреждения.")
            return True
        data["name"] = text
        set_state(user_id, "admin_add_org_inn", data)
        await send_message(chat_id, "Введите ИНН учреждения.")
        return True

    if state == "admin_add_org_inn":
        inn = clean_inn(text)
        if not INN_RE.match(inn):
            await send_message(chat_id, "ИНН должен содержать 10 или 12 цифр.")
            return True
        if find_org_by_inn(inn):
            await send_message(chat_id, "Организация с таким ИНН уже есть.")
            return True
        org_id = new_id()
        execute(
            """
            insert into sos_org.organizations (id, org_type, name, inn, created_by_user_id)
            values (%s, %s, %s, %s, %s)
            """,
            (org_id, data["org_type"], data["name"], inn, user_id),
        )
        clear_state(user_id)
        audit(user_id, chat_id, "organization_created", "organization", org_id, {"inn": inn})
        await send_message(
            chat_id,
            f"Организация добавлена:\n{data['name']}\nИНН: {inn}\n\n"
            "Теперь можно назначить чат подтверждения заявок и один или несколько чатов тревог в админке.",
            [
                callback_button("Чат заявок", "admin:set_approval_chat"),
                callback_button("Чат тревог", "admin:add_alert_chat"),
                callback_button("Администрирование", "admin:menu"),
            ],
        )
        return True

    if state in {"admin_approval_await_inn", "admin_alert_await_inn", "admin_staff_disable_await_inn"}:
        inn = clean_inn(text)
        if not INN_RE.match(inn):
            await send_message(chat_id, "ИНН должен содержать 10 или 12 цифр.")
            return True
        org = find_org_by_inn(inn)
        if not org:
            await send_message(chat_id, "Организация с таким ИНН не найдена.")
            return True
        if state == "admin_staff_disable_await_inn":
            clear_state(user_id)
            await admin_staff_list(chat_id, org["id"], org["name"], inn)
            return True
        next_state = "admin_approval_await_chat_id" if state == "admin_approval_await_inn" else "admin_alert_await_chat_id"
        set_state(user_id, next_state, {"organization_id": org["id"], "org_name": org["name"], "inn": inn})
        await send_message(chat_id, f"Организация: {org['name']}\nИНН: {inn}\n\nВведите id чата. Для группового чата обычно это отрицательное число.")
        return True

    if state == "admin_approval_await_chat_id":
        chat_id_value = text.strip()
        if not re.fullmatch(r"-?\d{3,30}", chat_id_value):
            await send_message(chat_id, "Введите числовой id чата, например -123456789.")
            return True
        execute("update sos_org.organizations set approval_chat_id = %s, updated_at = now() where id = %s", (chat_id_value, data["organization_id"]))
        clear_state(user_id)
        audit(user_id, chat_id, "admin_set_approval_chat", "organization", data["organization_id"], {"chat_id": chat_id_value, "inn": data["inn"]})
        await send_message(chat_id, f"Чат подтверждения заявок сохранен.\nОрганизация: {data['org_name']}\nchat_id: {chat_id_value}", [callback_button("Администрирование", "admin:menu")])
        return True

    if state == "admin_alert_await_chat_id":
        chat_id_value = text.strip()
        if not re.fullmatch(r"-?\d{3,30}", chat_id_value):
            await send_message(chat_id, "Введите числовой id чата, например -123456789.")
            return True
        alert_chat_id = new_id()
        execute(
            """
            insert into sos_org.alert_chats (id, organization_id, chat_id, created_by_user_id)
            values (%s, %s, %s, %s)
            on conflict (organization_id, chat_id) do update set is_active = true
            """,
            (alert_chat_id, data["organization_id"], chat_id_value, user_id),
        )
        clear_state(user_id)
        audit(user_id, chat_id, "admin_add_alert_chat", "organization", data["organization_id"], {"chat_id": chat_id_value, "inn": data["inn"]})
        await send_message(chat_id, f"Чат тревог добавлен.\nОрганизация: {data['org_name']}\nchat_id: {chat_id_value}", [callback_button("Администрирование", "admin:menu")])
        return True

    return False


async def handle_callback(chat_id: str, user_id: str, payload: str, callback_id: str | None = None) -> None:
    parts = payload.split(":")
    if parts[0] in {"staff", "staffalert", "staffreq", "orgalert", "admin", "adminstaff"}:
        clear_state(user_id)
        await send_message(chat_id, "Раздел организаций вынесен в отдельный бот и здесь больше не используется.", [callback_button("Главное меню", "main:menu")])
        return
    if payload == "main:menu":
        clear_state(user_id)
        await show_main(chat_id, user_id)
        return
    if payload == "main:sos":
        buttons = []
        if fetchone("select 1 from sos_family.links where child_user_id = %s and status = 'active' limit 1", (user_id,)):
            buttons.extend([callback_button("Опасность", "child:sos"), callback_button("Я потерялся", "child:lost")])
        if not buttons:
            buttons = [callback_button("Я ребенок", "child:menu")]
        await send_message(chat_id, "Выберите тревожный сценарий.", buttons + [callback_button("Главное меню", "main:menu")])
        return
    if payload == "help:menu":
        await help_menu(chat_id)
        return
    if payload == "help:parent":
        await send_help_with_image(chat_id, "parent", parent_help_text())
        return
    if payload == "help:child":
        await send_help_with_image(chat_id, "child", child_help_text())
        return
    if payload == "parent:menu":
        await parent_menu(chat_id, user_id)
        return
    if payload == "parent:children":
        await parent_children_menu(chat_id, user_id)
        return
    if payload == "parent:locate":
        await parent_locate_menu(chat_id, user_id)
        return
    if payload == "parent:create_code":
        add_role(user_id, "parent")
        code = await asyncio.to_thread(create_parent_code, user_id)
        await send_message(chat_id, f"Одноразовый код для ребенка: {code}\n\nКод действует {FAMILY_CODE_TTL_MINUTES} минут. Ребенок должен открыть раздел «Я ребенок» и ввести этот код.")
        return
    if parts[0] == "parentloc" and len(parts) == 3:
        action, child_user_id = parts[1], parts[2]
        if action == "req":
            row = fetchone(
                """
                select u.user_id, u.display_name, u.chat_id
                from sos_family.links l
                join sos_core.users u on u.user_id = l.child_user_id
                where l.parent_user_id = %s and l.child_user_id = %s and l.status = 'active'
                """,
                (user_id, child_user_id),
            )
            if not row:
                await send_message(chat_id, "Активная связь с ребенком не найдена.", [callback_button("Раздел родителя", "parent:menu")])
                return
            _child_user_id, child_name, child_chat_id = row
            if not child_chat_id:
                await send_message(chat_id, "Не могу отправить запрос: у ребенка пока нет актуального чата с ботом.", [callback_button("Раздел родителя", "parent:menu")])
                return
            parent = get_user(user_id) or {}
            set_state(
                child_user_id,
                "child_await_parent_location",
                {
                    "parent_user_id": user_id,
                    "parent_chat_id": chat_id,
                    "parent_name": parent.get("display_name") or "Родитель",
                },
            )
            await send_message(
                child_chat_id,
                f"Родитель {parent.get('display_name') or 'Родитель'} просит отправить геолокацию.",
                [geo_button(), callback_button("Отказаться", "childloc:decline")],
            )
            await send_message(chat_id, f"Запрос геолокации отправлен ребенку {child_name or 'Ребенок'}.", [callback_button("Главное меню", "main:menu")])
            audit(user_id, chat_id, "parent_location_requested", "child", child_user_id)
            return
    if parts[0] == "childloc" and len(parts) == 2:
        if parts[1] == "decline":
            state_item = get_state(user_id)
            data = state_item[1] if state_item and state_item[0] == "child_await_parent_location" else {}
            parent_chat_id = data.get("parent_chat_id")
            child = get_user(user_id) or {}
            clear_state(user_id)
            await send_message(chat_id, "Запрос геолокации отклонен.", [callback_button("Главное меню", "main:menu")])
            if parent_chat_id:
                await send_message(parent_chat_id, f"Ребенок {child.get('display_name') or 'Ребенок'} отказался отправить геолокацию.", [callback_button("Главное меню", "main:menu")])
            return
    if parts[0] == "parentchild" and len(parts) == 3:
        action, link_id = parts[1], parts[2]
        row = fetchone(
            """
            select l.id, l.child_user_id, u.display_name, u.chat_id
            from sos_family.links l
            join sos_core.users u on u.user_id = l.child_user_id
            where l.id = %s and l.parent_user_id = %s and l.status = 'active'
            """,
            (link_id, user_id),
        )
        if not row:
            await send_message(chat_id, "Активная связь не найдена.", [callback_button("Управление детьми", "parent:children")])
            return
        _link_id, _child_user_id, child_name, child_chat_id = row
        if action == "remove":
            await send_message(
                chat_id,
                f"Отключить связь с ребенком {child_name or 'Ребенок'}?",
                [
                    callback_button("Да, отключить", f"parentchild:confirm_remove:{link_id}"),
                    callback_button("Назад", "parent:children"),
                ],
            )
            return
        if action == "confirm_remove":
            execute(
                "update sos_family.links set status = 'removed', decided_at = now() where id = %s and parent_user_id = %s and status = 'active'",
                (link_id, user_id),
            )
            parent = get_user(user_id) or {}
            await send_message(chat_id, f"Связь с ребенком {child_name or 'Ребенок'} отключена.", [callback_button("Управление детьми", "parent:children"), callback_button("Главное меню", "main:menu")])
            if child_chat_id:
                await send_message(child_chat_id, f"Родитель {parent.get('display_name') or 'Родитель'} отключил семейную связь.", [callback_button("Главное меню", "main:menu")])
            audit(user_id, chat_id, "family_link_removed", "family_link", link_id)
            return
    if payload == "child:menu":
        await child_menu(chat_id, user_id)
        return
    if payload == "child:help":
        await child_help_menu(chat_id)
        return
    if payload == "child:more":
        await child_more_menu(chat_id)
        return
    if payload == "child:parents":
        await child_parents(chat_id, user_id)
        return
    if payload == "child:enter_code":
        add_role(user_id, "child")
        set_state(user_id, "child_await_parent_code", {})
        await send_message(chat_id, "Введите одноразовый код, который дал родитель.")
        return
    if payload in {"child:sos", "child:lost", "child:test"}:
        add_role(user_id, "child")
        await child_sos_start(chat_id, user_id, payload.split(":")[1])
        return
    if parts[0] == "familylink" and len(parts) == 3:
        action, child_user_id = parts[1], parts[2]
        if action == "approve":
            execute("update sos_family.links set status = 'active', decided_at = now() where parent_user_id = %s and child_user_id = %s", (user_id, child_user_id))
            add_role(user_id, "parent")
            add_role(child_user_id, "child")
            child = get_user(child_user_id) or {}
            await send_message(chat_id, f"Связь с ребенком {child.get('display_name') or ''} подтверждена.", [callback_button("Главное меню", "main:menu")])
            if child.get("chat_id"):
                await send_message(child["chat_id"], "Родитель подтвердил связь. Теперь кнопка помощи активна.", [callback_button("Нужна помощь", "child:sos")])
        else:
            execute("update sos_family.links set status = 'rejected', decided_at = now() where parent_user_id = %s and child_user_id = %s", (user_id, child_user_id))
            await send_message(chat_id, "Запрос отклонен.")
        return
    if parts[0] == "famalert" and len(parts) == 3:
        action, alert_id = parts[1], parts[2]
        status = "accepted" if action in {"accept", "coming"} else "closed"
        execute("update sos_family.alerts set status = %s, accepted_by_user_id = %s, accepted_at = coalesce(accepted_at, now()), closed_at = case when %s = 'closed' then now() else closed_at end where id = %s", (status, user_id, status, alert_id))
        row = fetchone(
            """
            select a.child_user_id, u.chat_id, u.display_name, a.alert_type, a.latitude, a.longitude
            from sos_family.alerts a
            join sos_core.users u on u.user_id = a.child_user_id
            where a.id = %s
            """,
            (alert_id,),
        )
        actor = get_user(user_id) or {}
        await send_message(chat_id, "Статус тревоги обновлен.", [callback_button("Главное меню", "main:menu")])
        if row and row[1]:
            parent_name = actor.get("display_name") or "Родитель"
            child_text = "Тревога закрыта родителем."
            if action == "accept":
                child_text = f"{parent_name} принял тревогу."
            elif action == "coming":
                child_text = f"Родитель {parent_name} едет."
            await send_message(row[1], child_text)
        if action == "close" and callback_id and row and row[4] is not None and row[5] is not None:
            label = "Нужна помощь" if row[3] == "sos" else "Ребенок потерялся" if row[3] == "lost" else "Тестовая тревога"
            text = f"{label}\n\nРебенок: {row[2] or 'Ребенок'}\nГеолокация: {float(row[4]):.6f}, {float(row[5]):.6f}"
            with contextlib.suppress(Exception):
                await answer_callback(callback_id, message_body(text, map_buttons(float(row[4]), float(row[5]))))
        return
    if payload == "staff:menu":
        await staff_menu(chat_id, user_id)
        return
    if parts[0] == "staff" and parts[1] == "start" and len(parts) == 3:
        await start_staff_registration(chat_id, user_id, parts[2])
        return
    if payload == "staff:alert_menu":
        await staff_alert_menu(chat_id, user_id, is_test=False)
        return
    if payload == "staff:test_menu":
        await staff_alert_menu(chat_id, user_id, is_test=True)
        return
    if parts[0] == "staffalert" and parts[1] == "confirm" and len(parts) == 4:
        staff_id, is_test = parts[2], parts[3] == "1"
        await send_message(
            chat_id,
            "Отправить тестовую тревогу?" if is_test else "Подтвердите отправку реальной тревоги.",
            [
                callback_button("Отправить", f"staffalert:send:{staff_id}:{1 if is_test else 0}"),
                callback_button("Отмена", "staff:menu"),
            ],
            columns=2,
        )
        return
    if parts[0] == "staffalert" and parts[1] == "send" and len(parts) == 4:
        await create_org_alert(chat_id, user_id, parts[2], parts[3] == "1")
        return
    if parts[0] == "staffreq" and len(parts) == 3:
        action, request_id = parts[1], parts[2]
        row = fetchone(
            """
            select r.staff_id, r.organization_id, r.user_id, o.approval_chat_id, s.full_name
            from sos_org.staff_requests r
            join sos_org.organizations o on o.id = r.organization_id
            join sos_org.staff s on s.id = r.staff_id
            where r.id = %s and r.status = 'pending'
            """,
            (request_id,),
        )
        if not row:
            await send_message(chat_id, "Заявка уже обработана или не найдена.")
            return
        staff_id, org_id, applicant_user_id, approval_chat_id, full_name = row
        if user_id not in ADMIN_USER_IDS and str(chat_id) != str(approval_chat_id):
            await send_message(chat_id, "Недостаточно прав для обработки заявки.")
            return
        new_status = "approved" if action == "approve" else "rejected"
        execute("update sos_org.staff_requests set status = %s, decided_by_user_id = %s, decided_at = now() where id = %s", (new_status, user_id, request_id))
        execute("update sos_org.staff set status = %s, decided_by_user_id = %s, decided_at = now() where id = %s", (new_status, user_id, staff_id))
        if new_status == "approved":
            add_role(applicant_user_id, "staff")
        applicant = get_user(applicant_user_id) or {}
        await send_message(chat_id, f"Заявка {full_name} обработана: {new_status}.")
        if applicant.get("chat_id"):
            await send_message(applicant["chat_id"], "Ваша заявка подтверждена. Тревожная кнопка активна." if new_status == "approved" else "Ваша заявка отклонена.", [callback_button("Раздел сотрудника", "staff:menu")])
        audit(user_id, chat_id, f"staff_request_{new_status}", "staff_request", request_id)
        return
    if parts[0] == "orgalert" and parts[1] == "accept" and len(parts) == 3:
        alert_id = parts[2]
        row = fetchone(
            """
            select a.id, s.user_id, u.chat_id, s.full_name, o.name
            from sos_org.alerts a
            join sos_org.staff s on s.id = a.staff_id
            join sos_core.users u on u.user_id = s.user_id
            join sos_org.organizations o on o.id = a.organization_id
            where a.id = %s
            """,
            (alert_id,),
        )
        if not row:
            await send_message(chat_id, "Тревога не найдена.")
            return
        execute("update sos_org.alerts set status = 'accepted', accepted_by_user_id = %s, accepted_at = now() where id = %s and status <> 'accepted'", (user_id, alert_id))
        actor = get_user(user_id) or {}
        await send_message(chat_id, f"Вызов принят: {actor.get('display_name') or user_id}.")
        _alert_id, staff_user_id, staff_chat_id, full_name, org_name = row
        if staff_chat_id:
            await send_message(staff_chat_id, f"Ваш вызов принят.\nУчреждение: {org_name}\nОтветственный: {actor.get('display_name') or user_id}")
        audit(user_id, chat_id, "org_alert_accepted", "org_alert", alert_id)
        return
    if payload == "guide:menu":
        await guide_menu(chat_id, user_id)
        return
    if payload == "guide:child":
        await send_guide_with_image(chat_id, "child", child_guide())
        return
    if payload == "guide:parent":
        await send_guide_with_image(chat_id, "parent", parent_guide())
        return
    if payload == "guide:staff":
        await send_guide_choice(chat_id)
        return
    if payload == "profile:menu":
        await profile_menu(chat_id, user_id)
        return
    if payload == "profile:name":
        await ask_name(chat_id, user_id, "profile", "профиль")
        return
    if payload == "admin:menu":
        await admin_menu(chat_id, user_id)
        return
    if payload == "admin:orgs":
        await list_orgs(chat_id)
        return
    if payload == "admin:add_org":
        if user_id not in ADMIN_USER_IDS:
            await send_message(chat_id, "Недостаточно прав.")
            return
        await send_message(chat_id, "Выберите тип организации.", [callback_button("Школа", "admin:add_org_type:school"), callback_button("Медицина", "admin:add_org_type:medical")], columns=2)
        return
    if payload == "admin:set_approval_chat":
        if user_id not in ADMIN_USER_IDS:
            await send_message(chat_id, "Недостаточно прав.")
            return
        set_state(user_id, "admin_approval_await_inn", {})
        await send_message(chat_id, "Введите ИНН организации, для которой нужно назначить чат подтверждения заявок.")
        return
    if payload == "admin:add_alert_chat":
        if user_id not in ADMIN_USER_IDS:
            await send_message(chat_id, "Недостаточно прав.")
            return
        set_state(user_id, "admin_alert_await_inn", {})
        await send_message(chat_id, "Введите ИНН организации, для которой нужно добавить чат тревог.")
        return
    if payload == "admin:disable_staff":
        if user_id not in ADMIN_USER_IDS:
            await send_message(chat_id, "Недостаточно прав.")
            return
        set_state(user_id, "admin_staff_disable_await_inn", {})
        await send_message(chat_id, "Введите ИНН организации, в которой нужно отключить сотрудника.")
        return
    if parts[0] == "adminstaff" and len(parts) == 3:
        if user_id not in ADMIN_USER_IDS:
            await send_message(chat_id, "Недостаточно прав.")
            return
        action, staff_id = parts[1], parts[2]
        row = fetchone(
            """
            select s.id, s.user_id, s.full_name, s.phone, s.position, o.name, o.inn, u.chat_id
            from sos_org.staff s
            join sos_org.organizations o on o.id = s.organization_id
            join sos_core.users u on u.user_id = s.user_id
            where s.id = %s and s.status = 'approved'
            """,
            (staff_id,),
        )
        if not row:
            await send_message(chat_id, "Активный сотрудник не найден.", [callback_button("Администрирование", "admin:menu")])
            return
        _staff_id, staff_user_id, full_name, phone, position, org_name, inn, staff_chat_id = row
        if action == "disable":
            await send_message(
                chat_id,
                f"Отключить сотрудника?\n\nОрганизация: {org_name}\nИНН: {inn}\nФИО: {full_name}\nТелефон: {phone}\nДолжность: {position or '-'}",
                [
                    callback_button("Да, отключить", f"adminstaff:confirm_disable:{staff_id}"),
                    callback_button("Администрирование", "admin:menu"),
                ],
            )
            return
        if action == "confirm_disable":
            execute(
                """
                update sos_org.staff
                set status = 'blocked', blocked_at = now(), decided_by_user_id = %s, decided_at = now()
                where id = %s and status = 'approved'
                """,
                (user_id, staff_id),
            )
            await send_message(
                chat_id,
                f"Сотрудник отключен.\n\nОрганизация: {org_name}\nФИО: {full_name}",
                [callback_button("Отключить еще", "admin:disable_staff"), callback_button("Главное меню", "main:menu")],
            )
            if staff_chat_id:
                await send_message(staff_chat_id, f"Доступ сотрудника отключен.\nОрганизация: {org_name}", [callback_button("Главное меню", "main:menu")])
            audit(user_id, chat_id, "staff_disabled", "staff", staff_id, {"staff_user_id": staff_user_id, "inn": inn})
            return
    if parts[0] == "admin" and parts[1] == "add_org_type" and len(parts) == 3:
        set_state(user_id, "admin_add_org_name", {"org_type": parts[2]})
        await send_message(chat_id, "Введите название учреждения.")
        return
    await send_message(chat_id, "Не удалось обработать кнопку. Откройте главное меню.", [callback_button("Главное меню", "main:menu")])


async def process_update(update: dict[str, Any]) -> None:
    logger.info("Processing update=%s", safe_json_dump(update))
    chat_id = extract_chat_id(update)
    user_id = upsert_user(update, chat_id)
    if not chat_id or not user_id:
        logger.info("Ignoring update without chat_id/user_id")
        return
    if BOT_USER_ID is not None and str(user_id) == str(BOT_USER_ID):
        return
    update_type = update.get("update_type")
    if update_type == "message_callback":
        payload = extract_callback_payload(update)
        await handle_callback(chat_id, user_id, payload, extract_callback_id(update))
        return
    text = extract_text(update)
    state = get_state(user_id)
    if state and state[0] in {"child_await_location", "child_await_parent_location"}:
        if await handle_text_state(chat_id, user_id, text, update):
            return
    normalized_text = text.lower()
    if update_type == "bot_started" or normalized_text in {"/start", "старт"}:
        clear_state(user_id)
        await show_main(chat_id, user_id, with_greeting=True)
        return
    if "меню" in normalized_text:
        clear_state(user_id)
        await show_main(chat_id, user_id)
        return
    if text.startswith("/bind_alert"):
        await show_main(chat_id, user_id)
        return
    if text.startswith("/bind_approval"):
        await show_main(chat_id, user_id)
        return
    if text.startswith("/add_org"):
        await show_main(chat_id, user_id)
        return
    if text.startswith("/orgs"):
        await show_main(chat_id, user_id)
        return
    if await handle_text_state(chat_id, user_id, text, update):
        return
    await show_main(chat_id, user_id)


async def fetch_bot_user_id() -> None:
    global BOT_USER_ID
    response = await max_request("GET", "/me")
    payload = response.json()
    BOT_USER_ID = payload.get("user_id")
    logger.info("MAX bot profile loaded user_id=%s", BOT_USER_ID)


async def ensure_webhook() -> None:
    if not MAX_WEBHOOK_URL or not MAX_REGISTER_WEBHOOK:
        logger.info("Skipping webhook registration")
        return
    body = {"url": MAX_WEBHOOK_URL, "update_types": ["message_created", "message_callback", "bot_started"]}
    try:
        await max_request("POST", "/subscriptions", json=body)
        logger.info("MAX webhook registered: %s", MAX_WEBHOOK_URL)
    except Exception as exc:
        logger.warning("Failed to register MAX webhook: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    require_token()
    await asyncio.to_thread(migrate)
    await fetch_bot_user_id()
    await ensure_webhook()
    yield


app = FastAPI(title="SOS MAX Bot", lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, Any]:
    return {"status": "ok", "service": "sos-max-bot"}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "sos-max-bot",
        "release": APP_RELEASE,
        "bot_user_id": BOT_USER_ID,
        "webhook_url": MAX_WEBHOOK_URL or None,
        "admin_user_ids": sorted(ADMIN_USER_IDS),
    }


@app.post("/webhook/max")
async def webhook_max(request: Request) -> Response:
    payload = await request.json()
    try:
        await process_update(payload)
    except Exception:
        logger.exception("Failed to process MAX update")
    return Response(status_code=200)
