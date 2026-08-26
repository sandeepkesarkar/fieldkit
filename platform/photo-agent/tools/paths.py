"""
paths.py — Shared VIDEO_TMP_DIR resolution for the photo-video agent.

process_photos.py (the producer) and check_approval.py / upload_facebook.py
(the two cleanup consumers) must resolve VIDEO_TMP_DIR identically, or
cleanup refuses to delete files it doesn't recognize as inside the allowed
tmp root. Issue #47 fixed the producer's resolution to use FIELDKIT_DATA_DIR
instead of the shared repo root, but the two consumers each carried their
own independent copy of the same logic and were missed by that fix — this
module is the single shared implementation so the three call sites can't
drift apart again.
"""

import os
from pathlib import Path


def get_video_tmp_root() -> Path:
    """Return the resolved VIDEO_TMP_DIR root.

    A relative VIDEO_TMP_DIR resolves against FIELDKIT_DATA_DIR (the
    per-client dir), never the shared fieldkit repo checkout — otherwise
    every client whose .env ships the same relative default collides on one
    shared tmp directory (issue #47).
    """
    tmp_raw = os.environ.get("VIDEO_TMP_DIR", "")
    data_dir = Path(os.environ["FIELDKIT_DATA_DIR"])
    if tmp_raw:
        p = Path(tmp_raw)
        return (p if p.is_absolute() else (data_dir / p)).resolve()
    return (data_dir / "photo-agent" / "tmp").resolve()
