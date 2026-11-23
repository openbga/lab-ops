"""Failed sign-ins are counted in the database (CACHES, core.cache, core migration 0007), so every
server process of a deployment counts the same attempts, and the table holds no name typed at
sign-in and no network address."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.test import Client as HttpClient

from accounts.models import User
from core import demo

PASSWORD = "synthetic-password-729!x"
TABLE = "lab_ops_cache"
REFUSED = "Too many failed login attempts"
ROOT = Path(settings.BASE_DIR)

# Another server process: the same settings and database, its own memory. It signs in once (or
# fails to `times` times) with Django's test client and prints what the page said.
OTHER_PROCESS = """
import json, sys
import django
django.setup()
from django.test import Client
login, password, times = sys.argv[1], sys.argv[2], int(sys.argv[3])
http = Client(REMOTE_ADDR="198.51.100.7")
for _ in range(times):
    response = http.post("/accounts/login/", {"login": login, "password": password})
print(json.dumps({
    "status": response.status_code,
    "refused": b"Too many failed login attempts" in response.content,
    "signed_in": "_auth_user_id" in http.session,
}))
"""


def other_process(login, password, times=1):
    database = connection.settings_dict
    url = (
        f"postgresql://{database['USER']}:{database['PASSWORD']}@{database['HOST']}:"
        f"{database['PORT']}/{database['NAME']}"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"TEST_DATABASE_NAME", "DATABASE_URL"}
    }
    env.update(
        DEBUG="true",
        DATABASE_URL=url,
        SECRET_KEY=settings.SECRET_KEY,
        DJANGO_SETTINGS_MODULE="config.settings",
    )
    result = subprocess.run(
        [sys.executable, "-c", OTHER_PROCESS, login, password, str(times)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture
def technician(transactional_db):
    cache.clear()
    yield User.objects.create_user("technical", "technician@example.invalid", PASSWORD)
    cache.clear()


def fail(http, login, times):
    for _ in range(times):
        http.post("/accounts/login/", {"login": login, "password": "wrong-guess-729"})


def test_failures_in_one_process_refuse_the_account_in_another(technician):
    fail(HttpClient(REMOTE_ADDR="203.0.113.9"), "technical", 5)

    other = other_process("technical", PASSWORD)

    assert other["refused"] and not other["signed_in"]


def test_failures_in_another_process_refuse_the_account_here(technician):
    other = other_process("technician@example.invalid", "wrong-guess-729", times=5)
    assert not other["signed_in"]

    http = HttpClient(REMOTE_ADDR="203.0.113.10")
    response = http.post(
        "/accounts/login/", {"login": "technician@example.invalid", "password": PASSWORD}
    )

    assert REFUSED.encode() in response.content
    assert "_auth_user_id" not in http.session


def test_a_successful_sign_in_elsewhere_does_not_reset_the_count(technician):
    fail(HttpClient(REMOTE_ADDR="203.0.113.11"), "technical", 4)
    assert other_process("technical", PASSWORD)["signed_in"]  # its own attempt is given back
    fail(HttpClient(REMOTE_ADDR="203.0.113.12"), "technical", 1)

    http = HttpClient(REMOTE_ADDR="203.0.113.13")
    response = http.post("/accounts/login/", {"login": "technical", "password": PASSWORD})

    assert REFUSED.encode() in response.content


@pytest.mark.django_db
def test_the_table_holds_keyed_digests_not_names_or_addresses():
    cache.clear()
    User.objects.create_user("digest_person", "digest@example.invalid", PASSWORD)
    HttpClient(REMOTE_ADDR="203.0.113.77").post(
        "/accounts/login/", {"login": "Digest_Person", "password": "wrong-guess-729"}
    )
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT cache_key, value FROM "{TABLE}"')
        rows = cursor.fetchall()
    assert rows
    for key, value in rows:
        assert len(key) == 64 and all(character in "0123456789abcdef" for character in key)
        for secret in ("digest", "203.0.113.77", "allauth", "login_failed"):
            assert secret not in key.lower() and secret not in value.lower()


@pytest.mark.django_db
def test_the_cache_is_the_database_table_and_reset_demo_leaves_it_alone():
    assert settings.CACHES["default"]["BACKEND"] == "django.core.cache.backends.db.DatabaseCache"
    assert settings.CACHES["default"]["LOCATION"] == TABLE
    assert TABLE in connection.introspection.table_names()
    # Not an application table: the demonstration's emptiness check and erase never read it.
    assert TABLE not in demo.business_tables()
    cache.set("synthetic-entry", 1, 60)
    assert TABLE not in demo.nonempty_tables()


def test_the_migration_creates_and_removes_the_table(transactional_db):
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate([("core", "0006_second_factor_rule")])
    try:
        assert TABLE not in connection.introspection.table_names()
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
    assert TABLE in connection.introspection.table_names()


def test_failures_arriving_together_are_each_counted(technician):
    """Twelve wrong passwords for one account at the same moment, each on its own database
    connection as separate server processes would be: at most five are checked."""
    import threading

    from django.db import connections

    barrier = threading.Barrier(12)
    outcomes = []

    def attempt(number):
        try:
            http = HttpClient(REMOTE_ADDR=f"203.0.113.{100 + number}")
            barrier.wait(timeout=30)
            response = http.post(
                "/accounts/login/", {"login": "technical", "password": "wrong-guess-729"}
            )
            outcomes.append(REFUSED.encode() in response.content)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=attempt, args=(number,)) for number in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert len(outcomes) == 12
    assert outcomes.count(False) <= 5  # checked against the password at most five times
    http = HttpClient(REMOTE_ADDR="203.0.113.200")
    response = http.post("/accounts/login/", {"login": "technical", "password": PASSWORD})
    assert REFUSED.encode() in response.content
