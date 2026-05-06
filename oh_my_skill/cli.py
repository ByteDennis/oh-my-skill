"""`oh-my-skill` command-line entry point.

Runs the Flask app on a chosen port. Sensible defaults so the most common
usage is just `oh-my-skill`.
"""
import argparse
import os
import sys
import threading
import webbrowser

from oh_my_skill import __version__


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog='oh-my-skill',
        description='Claude-powered skill cards manager. Open a browser-based '
                    'editor for tagged markdown skill cards, with optional '
                    'AI extract / chat (requires `claude` on PATH) and '
                    'GitHub sync.',
    )
    p.add_argument('--port', type=int, default=int(os.environ.get('PORT', 5009)),
                   help='HTTP port (default: 5009 or $PORT)')
    p.add_argument('--host', default=os.environ.get('HOST', '127.0.0.1'),
                   help='Bind host (default: 127.0.0.1; use 0.0.0.0 to expose)')
    p.add_argument('--data-dir', default=None,
                   help='Where to store the SQLite DBs and chat workspaces '
                        '(default: ~/.local/share/oh-my-skill, or /data when '
                        'running in Docker)')
    p.add_argument('--no-browser', action='store_true',
                   help="Don't auto-open a browser tab")
    p.add_argument('--debug', action='store_true', help='Flask debug mode')
    p.add_argument('--version', action='version', version=f'oh-my-skill {__version__}')
    args = p.parse_args(argv)

    # IMPORTANT: set OMI_DATA_DIR before importing app (so __init__ bootstrap
    # picks it up). When --data-dir is omitted, the package default applies.
    if args.data_dir:
        os.environ['OMI_DATA_DIR'] = os.path.abspath(os.path.expanduser(args.data_dir))

    from oh_my_skill.app import app

    url = f'http://{"localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host}:{args.port}/'
    print(f'oh-my-skill {__version__} → {url}', file=sys.stderr)
    print(f'  data dir: {os.environ.get("OMI_DATA_DIR")}', file=sys.stderr)

    if not args.no_browser:
        # Open browser ~1s after server is up
        threading.Timer(1.0, lambda: _try_open(url)).start()

    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


def _try_open(url: str):
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == '__main__':
    sys.exit(main())
