"""Daily aggregate statistics for the Аралтыш MAX channel."""
import argparse
import asyncio
import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app import get_db_connection, send_message

ZONE = ZoneInfo("Europe/Moscow")
RECOVERY_START = datetime(2026, 11, 4, tzinfo=ZONE)
BASE_USERS, BASE_PARENTS, BASE_CHILDREN = 701, 380, 195


def build_report(now=None):
    now = now or datetime.now(ZONE)
    today = now.astimezone(ZONE).date()
    yesterday = today - timedelta(days=1)
    start = datetime.combine(yesterday, time.min, tzinfo=ZONE)
    end = datetime.combine(today, time.min, tzinfo=ZONE)
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute("""SELECT count(*), count(*) FILTER (WHERE roles ? 'parent'),
                       count(*) FILTER (WHERE roles ? 'child') FROM sos_core.users
                       WHERE created_at >= %s""", (RECOVERY_START,))
        users, parents, children = cur.fetchone()
        users += BASE_USERS
        parents += BASE_PARENTS
        children += BASE_CHILDREN
        cur.execute("SELECT count(*) FROM sos_family.links WHERE status = 'active'")
        links = cur.fetchone()[0]
        cur.execute("""SELECT count(*), count(*) FILTER (WHERE roles ? 'parent'),
                       count(*) FILTER (WHERE roles ? 'child') FROM sos_core.users
                       WHERE created_at >= %s AND created_at < %s""", (start, end))
        new_users, new_parents, new_children = cur.fetchone()
        cur.execute("SELECT count(*) FROM sos_family.links WHERE status = 'active' AND created_at >= %s AND created_at < %s", (start, end))
        new_links = cur.fetchone()[0]
        cur.execute("""SELECT count(*), count(*) FILTER (WHERE created_at >= %s AND created_at < %s)
                       FROM sos_family.alerts""", (start, end))
        alerts, new_alerts = cur.fetchone()
    return (
        f"📊 Аралтыш — ежедневная статистика\n"
        f"За {yesterday:%d.%m.%Y} (московское время)\n\n"
        f"Пользователей: {users} (+{new_users} за сутки)\n"
        f"Родителей: {parents} (+{new_parents} новых пользователей за сутки)\n"
        f"Детей: {children} (+{new_children} новых пользователей за сутки)\n"
        f"Учтённых активных связей: {links} (+{new_links} создано и остаётся активными)\n"
        f"Учтённых тревожных сигналов: {alerts} (+{new_alerts} за сутки)"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = build_report()
    if args.dry_run:
        print(report)
        return
    chat_id = os.environ.get("ARALTYSH_STATS_CHAT_ID", "").strip()
    if not chat_id:
        raise RuntimeError("ARALTYSH_STATS_CHAT_ID is not configured")
    asyncio.run(send_message(chat_id, report))


if __name__ == "__main__":
    main()
