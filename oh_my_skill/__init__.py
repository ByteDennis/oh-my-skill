"""oh-my-skill — Claude-powered skill cards manager."""
import os
from importlib.resources import files as _pkg_files

__version__ = '0.1.0'


def _default_data_dir() -> str:
    """Pick a reasonable default for OMI_DATA_DIR.

    Order: $OMI_DATA_DIR (already-set) > /data (Docker convention, if writable)
    > $XDG_DATA_HOME/oh-my-skill > ~/.local/share/oh-my-skill.
    """
    if 'OMI_DATA_DIR' in os.environ:
        return os.environ['OMI_DATA_DIR']
    if os.path.isdir('/data') and os.access('/data', os.W_OK):
        return '/data'
    base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    return os.path.join(base, 'oh-my-skill')


def _bootstrap_env():
    """Set env defaults BEFORE submodules read them at import time. Idempotent."""
    data_dir = _default_data_dir()
    os.environ.setdefault('OMI_DATA_DIR', data_dir)
    os.environ.setdefault('SETTINGS_DB', os.path.join(data_dir, 'oh-my-skill.db'))
    os.environ.setdefault('SKILLCARDS_DB', os.path.join(data_dir, 'skillcards.db'))
    # Themes data ships with the package
    pkg_themes = str(_pkg_files('oh_my_skill').joinpath('themes_data'))
    os.environ.setdefault('OH_MY_THEMES_DIR', pkg_themes)
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


_bootstrap_env()
