
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

MATCHER_URL = "http://127.0.0.1:9000/updates"
RUN_ID = "local-test"


def send_event(event, index):
    message = event["message"]
    timestamp = event["timestamp"]

    headers = {
        "Content-Type": "application/json",
        "X-Stream-Run-Id": RUN_ID,
        "X-Stream-Event-Id": f"{RUN_ID}:{index}",
        "X-Stream-Simulated-Time": timestamp,
    }

    body = json.dumps(message).encode("utf-8")

    request = urllib.request.Request(
        MATCHER_URL,
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            response_body = response.read().decode("utf-8")
            print(
                f"[{index}] {message['action']}: "
                f"HTTP {response.status}"
            )
            if response_body:
                print(response_body)

    except urllib.error.HTTPError as error:
        print(f"\nERROR at event {index}: HTTP {error.code}")
        print(error.read().decode("utf-8"))
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print("Usage: python replay_local.py <scenario.json>")
        sys.exit(1)

    scenario_path = Path(sys.argv[1])

    if not scenario_path.exists():
        print(f"Scenario file not found: {scenario_path}")
        sys.exit(1)

    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    events = scenario.get("events", [])

    if not events:
        print("No events found in scenario.")
        sys.exit(1)

    print(f"Loaded {len(events)} events.")
    print(f"Sending events to {MATCHER_URL}\n")

    for index, event in enumerate(events):
        send_event(event, index)

    print("\nReplay completed.")

    print("\nFinal matcher health:")
    with urllib.request.urlopen(
        "http://127.0.0.1:9000/health"
    ) as response:
        print(response.read().decode("utf-8"))

    print("\nFinal assignments:")
    with urllib.request.urlopen(
        "http://127.0.0.1:9000/assignments"
    ) as response:
        print(response.read().decode("utf-8"))


if __name__ == "__main__":
    main()
