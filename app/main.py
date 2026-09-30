import sys

import uvicorn

from app.application import create_app
from app.config import Secrets, load_config


def main() -> None:
    try:
        config = load_config()
        secrets = Secrets()
    except Exception as error:
        # ValidationError may include user inputs. Report fields/types only.
        print(f"Configuration invalid ({type(error).__name__}). Check .env and config/config.yaml.",
              file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.run(create_app(config, secrets), host=config.websocket.host,
                port=config.websocket.port, workers=1, access_log=False,
                log_config=None, ws="websockets-sansio", ws_max_size=2 * 1024 * 1024,
                timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
