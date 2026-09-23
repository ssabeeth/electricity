"""Streamlit Community Cloud entry point: the dashboard over the public track record.

Community Cloud runs this file from a clone of the repository and installs the
`requirements.txt` beside it, which carries only what the dashboard needs. There
is no pipeline here: the daily GitHub Actions job publishes forecasts and scores
to the `track-record` branch, and this app downloads that branch (at most once an
hour), loads it into the output layout the API reads, and serves the ordinary
dashboard with the API running in-process. The app never needs redeploying for
new data.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

REPO = os.environ.get("ELEC_RECORD_REPO", "https://github.com/ssabeeth/electricity")
TARBALL = REPO.replace("https://github.com/", "https://codeload.github.com/") + (
    "/tar.gz/refs/heads/track-record"
)
DATA = Path(tempfile.gettempdir()) / "elecprice"
os.environ["ELEC_DATA_DIR"] = str(DATA)
os.environ["ELEC_API_URL"] = "inprocess"
os.environ.setdefault("ELEC_RECORD_REPO", REPO)

import streamlit as st  # noqa: E402  (the environment above must be set first)

from elecprice.pipeline import track_record  # noqa: E402


@st.cache_data(ttl=3600, show_spinner=False)
def sync_record() -> dict[str, int]:
    """Download the track-record branch and load it into $ELEC_DATA_DIR/outputs.

    ELEC_RECORD_DIR points at a local checkout instead, for testing this file.
    """
    local = os.environ.get("ELEC_RECORD_DIR")
    if local:
        return track_record.materialise(Path(local), DATA / "outputs")
    with urllib.request.urlopen(TARBALL, timeout=60) as resp:
        payload = resp.read()
    record = DATA / "record"
    shutil.rmtree(record, ignore_errors=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        tar.extractall(DATA / "download", filter="data")
    # GitHub wraps the branch in one top-level directory named <repo>-<branch>.
    (top,) = (DATA / "download").iterdir()
    shutil.move(top, record)
    shutil.rmtree(DATA / "download", ignore_errors=True)
    return track_record.materialise(record, DATA / "outputs")


sync_record()
exec(compile((ROOT / "src/elecprice/serving/dashboard.py").read_text(), "dashboard.py", "exec"))
