"""Serialize destructive QA drills on the designated, synthetic-only database."""

import os
from contextlib import contextmanager
from urllib.parse import urlsplit

import psycopg


HOST = 'ep-holy-breeze-b98rfxye.c-7.eu-central-1.aws.neon.tech'


@contextmanager
def exclusive_drill():
    source = os.environ['DATABASE_URL']
    uri = urlsplit(source)
    if (
        os.environ.get('WASLA_ENV') != 'staging'
        or uri.hostname != HOST
        or uri.path != '/wasla_staging'
    ):
        raise RuntimeError('The dedicated Wasla Staging database is required')
    with psycopg.connect(source, autocommit=True) as lock:
        acquired = lock.execute(
            "SELECT pg_try_advisory_lock(hashtextextended('wasla.staging.drill', 0))"
        ).fetchone()[0]
        if not acquired:
            raise RuntimeError('Another Staging drill is running')
        yield
