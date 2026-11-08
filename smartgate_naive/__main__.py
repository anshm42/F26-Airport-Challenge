import argparse

from .server import NaiveServer


def main():
    parser = argparse.ArgumentParser(description="SmartGate greedy baseline matcher")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()
    with NaiveServer((args.host, args.port)) as server:
        print(f"Naive matcher: http://{args.host}:{server.server_port}/updates", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
