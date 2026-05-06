"""Color + font theme catalogs.

Resolves `colors.json` / `fonts.json` from the first hit of:
  1. $OH_MY_THEMES_DIR  (env override; plain dir or with `themes/` subdir)
  2. The bundled `oh_my_skill/themes_data/` (default after pip install)
  3. /opt/oh-my-themes  (Docker dev convention)
"""
import json
import os
from importlib.resources import files as _pkg_files


def _candidate_dirs():
    env = os.environ.get('OH_MY_THEMES_DIR')
    if env:
        yield env
    try:
        yield str(_pkg_files('oh_my_skill').joinpath('themes_data'))
    except Exception:
        pass
    yield '/opt/oh-my-themes'
    yield os.path.join(os.path.dirname(__file__), '..', '..', 'oh-my-themes-vendor')


def _resolve(name):
    for d in _candidate_dirs():
        for cand in (os.path.join(d, name), os.path.join(d, 'themes', name)):
            if os.path.isfile(cand):
                return cand
    return None


def _read(name):
    p = _resolve(name)
    if not p:
        return []
    with open(p) as f:
        return json.load(f)


def list_colors():
    return _read('colors.json')


def list_fonts():
    return _read('fonts.json')
