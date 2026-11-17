"""HTTP 스트레스 테스트용 uvicorn 서버 실행 진입점.

`tools/stress_http.py`가 빈 포트를 골라 이 모듈을 subprocess로 실행한다.
"""

import argparse
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from storage import MiniRedis  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--log-level", default="warning")
    args = parser.parse_args()

    app = create_app(MiniRedis())
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        workers=1,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
