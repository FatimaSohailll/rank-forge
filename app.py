"""Vercel ASGI entry point; preserve the API's existing flat-module imports."""
from pathlib import Path
import os
import sys

# NLTK requires owner-private resources. Stage the build's bundled dictionary in /tmp,
# which is writable on Vercel, without downloading anything during a request.
import shutil
import tempfile

_bundled_data = Path(__file__).resolve().parent / ".nltk_data"
_nltk_cache = None
if (_bundled_data / "corpora" / "cmudict").is_dir():
    _nltk_cache = tempfile.TemporaryDirectory(prefix="rankforge-nltk-")
    shutil.copytree(_bundled_data / "corpora", Path(_nltk_cache.name) / "corpora")
    os.environ["NLTK_DATA"] = _nltk_cache.name

API_DIR = str(Path(__file__).resolve().parent / "backend")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

from main import app  # noqa: E402,F401
