import os
import time
import csv
import io
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request, Response

app = Flask(__name__)

BASE_URL = "https://api.oddspapi.io/v4"
API_KEY = os.environ["ODDSPAPI_API_KEY"]
ARCHIVE_TOKEN = os.environ["ARCHIVE_TOKEN"]

SPORT_ID = 10
BOOKMAKER = "bet365"
HTTP = requests.Session()


def authorized():
    return request.args.get("token") == ARCHIVE_TOKEN


def api_get(path, params, timeout=300):
    params = dict(params)
    params["apiKey"] = API_KEY
    r = HTTP.get(BASE_URL + path, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def parse_ts(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fixture_list(date_text):
    day = datetime.fromisoformat(date_text).replace(tzinfo=timezone.utc)
    end = day + timedelta(days=1) - timedelta(seconds=1)
    return api_get("/fixtures", {
        "sportId": SPORT_ID,
        "from": day.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "statusId": 2,
        "hasOdds": "true",
        "bookmakers": BOOKMAKER,
        "language": "en",
    }, timeout=120)


def opening_rows_for_fixture(fixture):
    fixture_id = fixture["fixtureId"]
    kickoff = parse_ts(fixture.get("startTime"))

    history = api_get("/historical-odds", {
        "fixtureId": fixture_id,
        "bookmakers": BOOKMAKER,
    }, timeout=300)

    bookmaker = (history.get("bookmakers") or {}).get(BOOKMAKER) or {}
    rows = []

    for market_id, market_data in (bookmaker.get("markets") or {}).items():
        for outcome_id, outcome_data in (market_data.get("outcomes") or {}).items():
            for player_id, snapshots in (outcome_data.get("players") or {}).items():
                candidates = []
                for snap in snapshots or []:
                    ts = parse_ts(snap.get("createdAt"))
                    if ts is None:
                        continue
                    if kickoff is not None and ts >= kickoff:
                        continue
                    if snap.get("price") is None:
                        continue
                    # Opening = first active PRE-MATCH quote recorded by OddsPapi.
                    if snap.get("active") is True:
                        candidates.append((ts, snap))

                if not candidates:
                    continue

                ts, snap = min(candidates, key=lambda x: x[0])
                rows.append({
                    "fixture_id": fixture_id,
                    "kickoff_utc": fixture.get("startTime"),
                    "tournament_id": fixture.get("tournamentId"),
                    "season_id": fixture.get("seasonId"),
                    "home_team": fixture.get("participant1Name") or "",
                    "away_team": fixture.get("participant2Name") or "",
                    "market_id": str(market_id),
                    "outcome_id": str(outcome_id),
                    "player_id": "" if str(player_id) == "0" else str(player_id),
                    "opening_odds_decimal": snap.get("price"),
                    "first_seen_utc": ts.isoformat(),
                })

    return rows


@app.get("/")
def home():
    return jsonify({
        "ok": True,
        "service": "bet365-archive-worker",
        "scope": "football / Bet365 / opening pre-match odds"
    })


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/fixtures")
def fixtures_endpoint():
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401
    date_text = request.args.get("date")
    if not date_text:
        return jsonify({"error": "date is required: YYYY-MM-DD"}), 400
    rows = fixture_list(date_text)
    return jsonify({"date": date_text, "count": len(rows), "fixtures": rows})


@app.get("/export")
def export_endpoint():
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401

    date_text = request.args.get("date")
    if not date_text:
        return jsonify({"error": "date is required: YYYY-MM-DD"}), 400

    offset = max(int(request.args.get("offset", "0")), 0)
    limit = min(max(int(request.args.get("limit", "3")), 1), 4)

    fixtures = fixture_list(date_text)
    selected = fixtures[offset:offset + limit]

    all_rows = []
    for i, fixture in enumerate(selected):
        if i:
            time.sleep(5.1)  # historical-odds endpoint cooldown
        all_rows.extend(opening_rows_for_fixture(fixture))

    columns = [
        "fixture_id", "kickoff_utc", "tournament_id", "season_id",
        "home_team", "away_team", "market_id", "outcome_id", "player_id",
        "opening_odds_decimal", "first_seen_utc"
    ]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    writer.writerows(all_rows)

    resp = Response(output.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="bet365_{date_text}_{offset}_{len(selected)}.csv"'
    )
    resp.headers["X-Fixture-Total"] = str(len(fixtures))
    resp.headers["X-Fixture-Offset"] = str(offset)
    resp.headers["X-Fixture-Count"] = str(len(selected))
    resp.headers["X-Odds-Rows"] = str(len(all_rows))
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
