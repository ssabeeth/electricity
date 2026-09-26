"""The dbt project must compile for the BigQuery target and produce valid BigQuery SQL.

BigQuery is configured but not used until credentials exist, so this is the
next best thing: compile offline with a throwaway (never valid) service-account
key, then parse every compiled model and test with sqlglot's BigQuery dialect.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow
REPO = Path(__file__).resolve().parents[1]


def fake_keyfile(path: Path) -> Path:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "offline-compile",
                "private_key_id": "0",
                "private_key": pem,
                "client_email": "dbt@offline-compile.iam.gserviceaccount.com",
                "client_id": "0",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )
    return path


def test_project_compiles_to_valid_bigquery_sql(tmp_path):
    pytest.importorskip("dbt.adapters.bigquery")
    sqlglot = pytest.importorskip("sqlglot")
    env = os.environ | {
        "DBT_PROFILES_DIR": str(REPO / "dbt"),
        "DBT_TARGET": "bigquery",
        "BQ_AUTH_METHOD": "service-account",
        "GCP_PROJECT": "offline-compile",
        "GOOGLE_APPLICATION_CREDENTIALS": str(fake_keyfile(tmp_path / "sa.json")),
        "DBT_TARGET_PATH": str(tmp_path / "target"),
        "DBT_LOG_PATH": str(tmp_path / "logs"),
    }
    dbt = str(Path(sys.executable).parent / "dbt")
    result = subprocess.run(
        [dbt, "compile", "--no-populate-cache"],
        cwd=REPO / "dbt",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-3000:]
    compiled = list((tmp_path / "target" / "compiled").rglob("*.sql"))
    assert len(compiled) > 50
    failures = {}
    for f in compiled:
        try:
            sqlglot.parse(f.read_text(), read="bigquery")
        except sqlglot.errors.ParseError as exc:
            failures[f.name] = str(exc)[:200]
    assert not failures, failures
    calendar = next(f for f in compiled if f.name == "int_settlement_calendar.sql").read_text()
    assert "timestamp(datetime(" in calendar and "Europe/London" in calendar  # BigQuery macros
