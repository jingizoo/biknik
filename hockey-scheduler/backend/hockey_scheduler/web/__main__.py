import argparse
import os

from .server import serve

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hockey Scheduler demo server")
    # Cloud hosts (Render, etc.) inject $PORT and require binding to 0.0.0.0.
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()
    serve(args.host, args.port)
