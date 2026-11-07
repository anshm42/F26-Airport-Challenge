import argparse

from .server import ControlServer


def main():
    parser = argparse.ArgumentParser(description="SmartGate dynamic update stream")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="serve the local run-control API")
    serve.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    with ControlServer(args.port) as server:
        print(f"Stream control: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
