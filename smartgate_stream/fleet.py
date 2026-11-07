"""Extract a small, reproducible Pearson fleet pool from OpenSky's 2025 CSV."""

import argparse
import csv
import hashlib
import json
import re
from datetime import date
from pathlib import Path


DATA = Path(__file__).resolve().parents[1] / "data"
SOURCE_URL = "https://s3.opensky-network.org/data-samples/metadata/aircraft-database-complete-2025-08.csv"
SNAPSHOT_DATE = "2025-08-01"
ICAO_TO_IATA = {
    "BCS1": "221", "BCS3": "223", "B38M": "7M8", "B737": "73G", "B738": "738",
    "A319": "319", "A320": "320", "A321": "321", "A21N": "32Q", "A333": "333",
    "A332": "332", "B789": "789", "B788": "788", "B77W": "77W", "B772": "772",
    "CRJ9": "CR9", "DH8D": "DH4", "E75L": "E75", "E75S": "E75", "E295": "295",
    "A359": "359", "B748": "74H", "A388": "388", "B712": "717",
}


def import_fleet(path):
    profiles = json.loads((DATA / "stream_profiles.json").read_text(encoding="utf-8"))["carriers"]
    by_icao = {icao: profile for profile in profiles for icao in profile["operatorIcaos"]}
    allowed_types = {p["carrier"]: {t for s in p["services"] for t in s["aircraft"]} for p in profiles}
    records, conflicts = {}, set()
    source_rows = 0
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, quotechar="'", strict=True)
        required = {"icao24", "registration", "typecode", "operatorIcao", "operatorIata", "status", "regUntil"}
        if not required <= set(reader.fieldnames or []):
            raise ValueError("not an OpenSky complete aircraft CSV (single-quoted columns required)")
        for row in reader:
            source_rows += 1
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"malformed CSV row {source_rows}")
            icao = row["operatorIcao"].strip().upper()
            profile = by_icao.get(icao)
            if profile is None:
                continue
            source_iata = row["operatorIata"].strip().upper()
            if source_iata and source_iata not in profile["operatorIatas"]:
                continue
            carrier = profile["carrier"]
            aircraft = ICAO_TO_IATA.get(row["typecode"].strip().upper())
            tail = row["registration"].strip().upper()
            hex_code = row["icao24"].strip().lower()
            if aircraft not in allowed_types[carrier] or not re.fullmatch(profile["registrationPattern"], tail):
                continue
            if not re.fullmatch(r"[0-9a-f]{6}", hex_code):
                continue
            if row["status"].strip().lower() not in ("", "active", "valid", "registered"):
                continue
            if row["regUntil"].strip():
                try:
                    if date.fromisoformat(row["regUntil"].strip()) <= date.fromisoformat(SNAPSHOT_DATE):
                        continue
                except ValueError:
                    continue
            record = {"carrier": carrier, "registration": tail, "aircraftType": aircraft,
                      "icaoType": row["typecode"].strip().upper(), "icao24": hex_code,
                      "operatorIcao": icao, "operatorIata": source_iata}
            if tail in records and records[tail] != record:
                conflicts.add(tail)
            records[tail] = record
    selected = sorted((r for tail, r in records.items() if tail not in conflicts),
                      key=lambda r: (r["carrier"], r["aircraftType"], r["registration"]))
    missing = set(allowed_types) - {r["carrier"] for r in selected}
    if missing:
        raise ValueError(f"snapshot has no eligible aircraft for: {', '.join(sorted(missing))}")
    with Path(path).open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {"source": SOURCE_URL, "snapshotMonth": "2025-08", "sourceSha256": digest,
            "sourceRows": source_rows, "excludedConflictingRegistrations": sorted(conflicts),
            "scope": "OpenSky metadata, filtered by explicit operator ICAO, national registration format, and route-profile aircraft types. Historical metadata is incomplete and does not establish current airworthiness or ownership. No owner-name inference.",
            "records": selected}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="aircraft-database-complete-2025-08.csv")
    parser.add_argument("output", type=Path, help="new JSON subset (must not exist)")
    args = parser.parse_args()
    try:
        fleet = import_fleet(args.input)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(json.dumps(fleet, indent=2) + "\n")
    except (ValueError, OSError, csv.Error) as error:
        parser.error(str(error))
    print(f"Wrote {len(fleet['records'])} aircraft to {args.output}")
