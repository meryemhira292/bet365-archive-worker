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

_market_cache = None
_market_cache_at = 0.0


def authorized():
    return request.args.get("token") == ARCHIVE_TOKEN


def api_get(path, params, timeout=300, empty_on_404=False):
    params = dict(params)
    params["apiKey"] = API_KEY
    r = HTTP.get(BASE_URL + path, params=params, timeout=timeout)

    if r.status_code == 404 and empty_on_404:
        return None

    if r.status_code == 429:
        try:
            retry_ms = (r.json().get("error") or {}).get("retryMs", 5000)
        except Exception:
            retry_ms = 5000
        time.sleep(retry_ms / 1000 + 0.3)
        r = HTTP.get(BASE_URL + path, params=params, timeout=timeout)

    if not r.ok:
        body = r.text[:1000]
        raise RuntimeError(
            f"OddsPapi {path} returned HTTP {r.status_code}: {body}"
        )

    return r.json()


def parse_ts(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fixture_list(date_text):
    day = datetime.fromisoformat(date_text).replace(tzinfo=timezone.utc)
    end = day + timedelta(days=1)

    # Historical fixture discovery:
    # Do NOT filter by hasOdds/bookmaker here. Finished fixtures may no
    # longer have active current odds even though /historical-odds exists.
    data = api_get("/fixtures", {
        "sportId": SPORT_ID,
        "from": day.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "statusId": 2,
        "language": "en",
    }, timeout=120, empty_on_404=True)

    return data or []


def market_catalog():
    global _market_cache, _market_cache_at

    if _market_cache is not None and time.time() - _market_cache_at < 21600:
        return _market_cache

    data = api_get("/markets", {"language": "en"}, timeout=120)

    markets = {}
    outcomes = {}

    for m in data or []:
        if int(m.get("sportId") or -1) != SPORT_ID:
            continue

        mid = str(m.get("marketId"))
        markets[mid] = {
            "market_name": m.get("marketName"),
            "market_type": m.get("marketType"),
            "period": m.get("period"),
            "handicap": m.get("handicap"),
            "player_prop": bool(m.get("playerProp")),
        }

        for o in m.get("outcomes") or []:
            outcomes[(mid, str(o.get("outcomeId")))] = o.get("outcomeName")

    _market_cache = (markets, outcomes)
    _market_cache_at = time.time()
    return _market_cache


def opening_rows_for_fixture(fixture):
    fixture_id = fixture["fixtureId"]
    kickoff = parse_ts(fixture.get("startTime"))

    history = api_get("/historical-odds", {
        "fixtureId": fixture_id,
        "bookmakers": BOOKMAKER,
    }, timeout=300, empty_on_404=True)

    if not history:
        return []

    bookmaker = (history.get("bookmakers") or {}).get(BOOKMAKER) or {}
    if not bookmaker:
        return []

    markets_meta, outcome_names = market_catalog()
    rows = []

    for market_id, market_data in (bookmaker.get("markets") or {}).items():
        mid = str(market_id)
        meta = markets_meta.get(mid, {})

        for outcome_id, outcome_data in (market_data.get("outcomes") or {}).items():
            oid = str(outcome_id)

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
                    if snap.get("active") is True:
                        candidates.append((ts, snap))

                if not candidates:
                    continue

                ts, snap = min(candidates, key=lambda x: x[0])

                rows.append({
                    "fixture_id": fixture_id,
                    "kickoff_utc": fixture.get("startTime"),
                    "category": fixture.get("categoryName"),
                    "tournament": fixture.get("tournamentName"),
                    "tournament_id": fixture.get("tournamentId"),
                    "season_id": fixture.get("seasonId"),
                    "home_team": fixture.get("participant1Name") or "",
                    "away_team": fixture.get("participant2Name") or "",
                    "bookmaker": "Bet365",
                    "market_id": mid,
                    "market_name": meta.get("market_name"),
                    "market_type": meta.get("market_type"),
                    "period": meta.get("period"),
                    "line_value": meta.get("handicap"),
                    "player_prop": meta.get("player_prop"),
                    "outcome_id": oid,
                    "outcome_name": outcome_names.get((mid, oid)),
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
        "scope": "football / Bet365 / opening pre-match odds",
        "opening_definition": "earliest active pre-match quote recorded by OddsPapi"
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

    return jsonify({
        "date": date_text,
        "sport": "Soccer",
        "finished_fixture_count": len(rows),
        "sample": rows[:20]
    })


@app.get("/export")
def export_endpoint():
    if not authorized():
        return jsonify({"error": "unauthorized"}), 401

    date_text = request.args.get("date")
    if not date_text:
        return jsonify({"error": "date is required: YYYY-MM-DD"}), 400

    offset = max(int(request.args.get("offset", "0")), 0)
    limit = min(max(int(request.args.get("limit", "3")), 1), 8)

    fixtures = fixture_list(date_text)
    selected = fixtures[offset:offset + limit]

    all_rows = []
    processed = 0

    for fixture in selected:
        if processed:
            time.sleep(5.2)  # historical-odds cooldown
        try:
            all_rows.extend(opening_rows_for_fixture(fixture))
        except Exception as exc:
            print(f"fixture {fixture.get('fixtureId')} failed: {exc}", flush=True)
        processed += 1

    columns = [
        "fixture_id", "kickoff_utc", "category", "tournament",
        "tournament_id", "season_id", "home_team", "away_team",
        "bookmaker", "market_id", "market_name", "market_type",
        "period", "line_value", "player_prop", "outcome_id",
        "outcome_name", "player_id", "opening_odds_decimal",
        "first_seen_utc"
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
