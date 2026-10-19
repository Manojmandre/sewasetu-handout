
# -*- coding: utf-8 -*-
"""
Nightly deemed-approval job.

Applications that remain pending beyond the configured SLA are marked
DEEMED_APPROVED and applicants are notified by SMS.

Scheduled via cron at 02:00 daily. See deploy/crontab.
"""

import os
import sys
import logging
import configparser
from datetime import datetime, timedelta

import psycopg2
import requests


# Resolve the app directory and load the existing configuration.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "app.ini")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def load_config():
    """Load configuration and fail clearly if it is missing or invalid."""
    config = configparser.ConfigParser()

    if not os.path.isfile(CONFIG_PATH):
        raise FileNotFoundError(
            "Configuration file not found: %s" % CONFIG_PATH
        )

    with open(CONFIG_PATH, encoding="utf-8") as config_file:
        config.read_file(config_file)

    required_sections = ("database", "app", "pension")
    for section in required_sections:
        if not config.has_section(section):
            raise ValueError(
                "Missing [%s] section in configuration" % section
            )

    return config


def main():
    conn = None
    cur = None

    try:
        config = load_config()

        sla_days = config.getint("pension", "sla_days")
        sms_gateway_url = config.get(
            "app", "sms_gateway_url"
        ).rstrip("/")

        if sla_days < 0:
            raise ValueError("pension.sla_days cannot be negative")

        # Use one consistent timestamp for the cutoff and update.
        now = datetime.now()
        cutoff = now - timedelta(days=sla_days)

        conn = psycopg2.connect(
            host=config.get("database", "host"),
            port=config.getint("database", "port"),
            dbname=config.get("database", "name"),
            user=config.get("database", "user"),
            password=config.get("database", "password"),
            connect_timeout=10,
        )

        cur = conn.cursor()

        cur.execute(
            """
            UPDATE applications
               SET status = 'DEEMED_APPROVED',
                   decided_at = %s,
                   decided_by = 'RTPS-AUTO'
             WHERE status = 'PENDING'
               AND submitted_at < %s
            RETURNING application_no, mobile
            """,
            (now, cutoff),
        )

        rows = cur.fetchall()
        conn.commit()

        logger.info(
            "Deemed approval completed: %d applications approved",
            len(rows),
        )

        # Notify applicants after committing the database transaction.
        for app_no, mobile in rows:
            try:
                response = requests.post(
                    sms_gateway_url + "/api/send",
                    json={
                        "to": mobile,
                        "text": (
                            "Sewa Setu: your pension application %s "
                            "stands approved under the RTPS Act."
                        ) % app_no,
                    },
                    timeout=5,
                )
                response.raise_for_status()

            except requests.RequestException:
                # Do not log the applicant's phone number or message.
                logger.exception(
                    "SMS notification failed for application %s",
                    app_no,
                )

        return 0

    except Exception:
        # Roll back an uncommitted transaction if an error occurs.
        if conn is not None:
            try:
                conn.rollback()
            except psycopg2.Error:
                pass

        logger.exception("Deemed-approval job failed")
        return 1

    finally:
        if cur is not None:
            cur.close()

        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
