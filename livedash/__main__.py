"""Run the live dashboard: `python -m livedash`.

Environment overrides (see livedash/config.py): LIVEDASH_CSV_DIR, LIVEDASH_PORT,
LIVEDASH_ROTATE, LIVEDASH_REFRESH_OPEN, LIVEDASH_BLUR, …
"""

from .app import create_app
from .config import Config


def main() -> None:
    cfg = Config()
    app, _pf = create_app(cfg)
    print(f"[livedash] serving on http://{cfg.host}:{cfg.port}")
    app.run(host=cfg.host, port=cfg.port, debug=cfg.debug)


if __name__ == "__main__":
    main()
