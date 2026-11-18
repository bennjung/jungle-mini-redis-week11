"""Run the Mini Redis server and the weather demo in one terminal."""

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main():
    commands = [
        [sys.executable, "-u", "app.py"],
        [
            sys.executable, "-u", "-m", "uvicorn", "demo.weather_app:app",
            "--host", "127.0.0.1", "--port", "8000", "--workers", "1",
        ],
    ]
    processes = []
    try:
        print("데모: http://127.0.0.1:8000 (종료: Ctrl+C)", flush=True)
        for command in commands:
            processes.append(subprocess.Popen(command, cwd=ROOT))
        while True:
            for process in processes:
                try:
                    code = process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    continue
                print(f"서버가 종료되었습니다 (종료 코드: {code}).", flush=True)
                return code or 1
    except KeyboardInterrupt:
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    sys.exit(main())
