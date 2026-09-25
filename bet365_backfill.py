import os
import sys
import time
from datetime import datetime, timedelta, timezone

import psycopg
import requests
from psycopg.types.json import Jsonb

BASE = "https://api.oddspapi.io/v4"
API_KEY = os.environ["ODDSPAPI_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]
SPORT_ID = 10
BOOKMAKER = "bet365"

# GitHub-hosted jobs may run for at most 6h. Leave a safety margin.
MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME_SECONDS", "20400"))  # 5h40m
HIST_INTERVAL = float(os.environ.get("HIST_INTERVAL_SECONDS", "5.05"))
WINDOW_HOURS = 47
START_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)

http = requests.Session()
http.headers.update({"Accept-Encoding": "gzip, deflate"})
last_hist_start = 0.0


def api_get(path, params, *, timeout=300, allow_404=False, historical=False):
    global last_hist_start
    p = dict(params)
    p["apiKey"] = API_KEY

    if historical:
        elapsed = time.monotonic() - last_hist_start
        if elapsed < HIST_INTERVAL:
            time.sleep(HIST_INTERVAL - elapsed)
        last_hist_start = time.monotonic()

    r = http.get(BASE + path, params=p, timeout=timeout)

    if r.status_code == 404 and allow_404:
        return None

    if r.status_code == 429:
        try:
            retry_ms = int(((r.json().get("error") or {}).get("retryMs")) or 5200)
        except Exception:
            retry_ms = 5200
        time.sleep(retry_ms / 1000 + 0.5)
        if historical:
            last_hist_start = time.monotonic()
        r = http.get(BASE + path, params=p, timeout=timeout)

    if not r.ok:
        raise RuntimeError(f"{path} HTTP {r.status_code}: {r.text[:500]}")

    return r.json()


def parse_ts(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def ensure_schema(cur):
    cur.execute("""
    CREATE TABLE IF NOT EXISTS archive_meta(
      key text PRIMARY KEY,
      value text NOT NULL
    );

    CREATE TABLE IF NOT EXISTS matches(
      fixture_id text PRIMARY KEY,
      kickoff_utc timestamptz,
      category_name text,
      tournament_id text,
      tournament_name text,
      season_id text,
      home_team text,
      away_team text,
      status_id integer,
      status_name text,
      raw_fixture jsonb,
      inserted_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS market_catalog(
      market_id text PRIMARY KEY,
      market_name text,
      market_type text,
      period text,
      handicap text,
      player_prop boolean,
      outcomes jsonb,
      updated_at timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS opening_odds(
      fixture_id text NOT NULL REFERENCES matches(fixture_id) ON DELETE CASCADE,
      bookmaker text NOT NULL DEFAULT 'Bet365',
      market_id text NOT NULL,
      market_name text,
      market_type text,
      period text,
      line_value text,
      player_prop boolean,
      outcome_id text NOT NULL,
      outcome_name text,
      player_id text NOT NULL DEFAULT '',
      opening_odds_decimal numeric,
      first_seen_utc timestamptz,
      PRIMARY KEY(fixture_id, bookmaker, market_id, outcome_id, player_id)
    );

    CREATE INDEX IF NOT EXISTS opening_odds_fixture_idx ON opening_odds(fixture_id);
    CREATE INDEX IF NOT EXISTS opening_odds_market_idx ON opening_odds(market_id);
    CREATE INDEX IF NOT EXISTS matches_kickoff_idx ON matches(kickoff_utc);

    CREATE TABLE IF NOT EXISTS scanned_fixtures(
      fixture_id text PRIMARY KEY,
      had_bet365_history boolean NOT NULL DEFAULT false,
      opening_rows integer NOT NULL DEFAULT 0,
      scanned_at timestamptz NOT NULL DEFAULT now(),
      error_text text
    );

    CREATE TABLE IF NOT EXISTS sync_state_fast(
      scope text PRIMARY KEY,
      window_start timestamptz NOT NULL,
      fixture_offset integer NOT NULL DEFAULT 0,
      total_fixtures_scanned bigint NOT NULL DEFAULT 0,
      total_opening_rows bigint NOT NULL DEFAULT 0,
      updated_at timestamptz NOT NULL DEFAULT now()
    );
    """)

    cur.execute("""
      INSERT INTO archive_meta(key,value) VALUES
      ('scope','football_only'),
      ('bookmaker','Bet365'),
      ('opening_definition','earliest active pre-match quote recorded by OddsPapi'),
      ('source_start','2026-01-01')
      ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """)

    cur.execute("""
      INSERT INTO sync_state_fast(scope,window_start,fixture_offset)
      VALUES('bet365_football_2026_fast', %s, 0)
      ON CONFLICT(scope) DO NOTHING
    """, (START_AT,))


def ensure_market_catalog(cur):
    cur.execute("SELECT count(*) FROM market_catalog")
    if cur.fetchone()[0] > 0:
        rows = {}
        outcomes = {}
        cur.execute("SELECT market_id,market_name,market_type,period,handicap,player_prop,outcomes FROM market_catalog")
        for mid, name, typ, period, handicap, player_prop, outs in cur.fetchall():
            rows[str(mid)] = {
                "market_name": name,
                "market_type": typ,
                "period": period,
                "handicap": handicap,
                "player_prop": player_prop,
            }
            if outs:
                for o in outs:
                    outcomes[(str(mid), str(o.get("outcomeId")))] = o.get("outcomeName")
        return rows, outcomes

    data = api_get("/markets", {"language": "en"}, timeout=120)
    rows, outcomes = {}, {}

    for m in data or []:
        if int(m.get("sportId") or -1) != SPORT_ID:
            continue
        mid = str(m.get("marketId"))
        mm = {
            "market_name": m.get("marketName"),
            "market_type": m.get("marketType"),
            "period": m.get("period"),
            "handicap": None if m.get("handicap") is None else str(m.get("handicap")),
            "player_prop": bool(m.get("playerProp")),
        }
        rows[mid] = mm
        outs = m.get("outcomes") or []
        for o in outs:
            outcomes[(mid, str(o.get("outcomeId")))] = o.get("outcomeName")

        cur.execute("""
          INSERT INTO market_catalog(
            market_id,market_name,market_type,period,handicap,player_prop,outcomes,updated_at
          ) VALUES(%s,%s,%s,%s,%s,%s,%s,now())
          ON CONFLICT(market_id) DO UPDATE SET
            market_name=excluded.market_name,
            market_type=excluded.market_type,
            period=excluded.period,
            handicap=excluded.handicap,
            player_prop=excluded.player_prop,
            outcomes=excluded.outcomes,
            updated_at=now()
        """, (
            mid, mm["market_name"], mm["market_type"], mm["period"],
            mm["handicap"], mm["player_prop"], Jsonb(outs)
        ))
    return rows, outcomes


def fixture_window(start):
    end = start + timedelta(hours=WINDOW_HOURS)
    data = api_get("/fixtures", {
        "sportId": SPORT_ID,
        "from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "statusId": 2,
        "language": "en",
    }, timeout=180, allow_404=True)
    if not data:
        return []
    return sorted(data, key=lambda x: (x.get("startTime") or "", str(x.get("fixtureId") or "")))


def save_match(cur, f):
    cur.execute("""
      INSERT INTO matches(
        fixture_id,kickoff_utc,category_name,tournament_id,tournament_name,season_id,
        home_team,away_team,status_id,status_name,raw_fixture
      ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
      ON CONFLICT(fixture_id) DO UPDATE SET
        kickoff_utc=excluded.kickoff_utc,
        category_name=excluded.category_name,
        tournament_id=excluded.tournament_id,
        tournament_name=excluded.tournament_name,
        season_id=excluded.season_id,
        home_team=excluded.home_team,
        away_team=excluded.away_team,
        status_id=excluded.status_id,
        status_name=excluded.status_name,
        raw_fixture=excluded.raw_fixture
    """, (
        str(f.get("fixtureId")),
        parse_ts(f.get("startTime")),
        f.get("categoryName"),
        None if f.get("tournamentId") is None else str(f.get("tournamentId")),
        f.get("tournamentName"),
        None if f.get("seasonId") is None else str(f.get("seasonId")),
        f.get("participant1Name"),
        f.get("participant2Name"),
        f.get("statusId"),
        f.get("statusName"),
        Jsonb(f),
    ))


def extract_opening_rows(fixture, hist, market_meta, outcome_names):
    kickoff = parse_ts(fixture.get("startTime"))
    book = (hist.get("bookmakers") or {}).get(BOOKMAKER) or {}
    markets = book.get("markets") or {}
    out = []

    for market_id, market_data in markets.items():
        mid = str(market_id)
        mm = market_meta.get(mid, {})

        for outcome_id, outcome_data in (market_data.get("outcomes") or {}).items():
            oid = str(outcome_id)

            for player_id, snapshots in (outcome_data.get("players") or {}).items():
                earliest = None
                for snap in snapshots or []:
                    ts = parse_ts(snap.get("createdAt"))
                    if ts is None or snap.get("price") is None:
                        continue
                    if kickoff is not None and ts >= kickoff:
                        continue
                    if snap.get("active") is not True:
                        continue
                    if earliest is None or ts < earliest[0]:
                        earliest = (ts, snap)

                if earliest is None:
                    continue

                ts, snap = earliest
                pid = "" if str(player_id) == "0" else str(player_id)
                out.append((
                    str(fixture["fixtureId"]), "Bet365", mid,
                    mm.get("market_name"), mm.get("market_type"), mm.get("period"),
                    mm.get("handicap"), mm.get("player_prop"),
                    oid, outcome_names.get((mid, oid)), pid,
                    snap.get("price"), ts
                ))
    return out


def scan_one(cur, conn, fixture, market_meta, outcome_names):
    fid = str(fixture["fixtureId"])

    cur.execute("SELECT 1 FROM scanned_fixtures WHERE fixture_id=%s", (fid,))
    if cur.fetchone():
        return 0, False, True

    save_match(cur, fixture)
    conn.commit()

    try:
        hist = api_get(
            "/historical-odds",
            {"fixtureId": fid, "bookmakers": BOOKMAKER},
            timeout=300,
            allow_404=True,
            historical=True,
        )
        if not hist:
            cur.execute("""
              INSERT INTO scanned_fixtures(fixture_id,had_bet365_history,opening_rows,error_text)
              VALUES(%s,false,0,null)
              ON CONFLICT(fixture_id) DO UPDATE SET
                had_bet365_history=false,opening_rows=0,scanned_at=now(),error_text=null
            """, (fid,))
            conn.commit()
            return 0, False, False

        rows = extract_opening_rows(fixture, hist, market_meta, outcome_names)
        inserted = 0
        for row in rows:
            cur.execute("""
              INSERT INTO opening_odds(
                fixture_id,bookmaker,market_id,market_name,market_type,period,line_value,
                player_prop,outcome_id,outcome_name,player_id,opening_odds_decimal,first_seen_utc
              ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(fixture_id,bookmaker,market_id,outcome_id,player_id) DO NOTHING
            """, row)
            inserted += cur.rowcount

        cur.execute("""
          INSERT INTO scanned_fixtures(fixture_id,had_bet365_history,opening_rows,error_text)
          VALUES(%s,true,%s,null)
          ON CONFLICT(fixture_id) DO UPDATE SET
            had_bet365_history=true,opening_rows=excluded.opening_rows,scanned_at=now(),error_text=null
        """, (fid, inserted))
        conn.commit()
        return inserted, True, False

    except Exception as exc:
        conn.rollback()
        with conn.cursor() as c2:
            save_match(c2, fixture)
            c2.execute("""
              INSERT INTO scanned_fixtures(fixture_id,had_bet365_history,opening_rows,error_text)
              VALUES(%s,false,0,%s)
              ON CONFLICT(fixture_id) DO UPDATE SET
                scanned_at=now(),error_text=excluded.error_text
            """, (fid, repr(exc)[:1500]))
        conn.commit()
        raise


def main():
    started = time.monotonic()
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    scanned_run = 0
    inserted_run = 0
    found_run = 0
    skipped_run = 0

    with psycopg.connect(DATABASE_URL) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            ensure_schema(cur)
            market_meta, outcome_names = ensure_market_catalog(cur)
            conn.commit()

            cur.execute("""
              SELECT window_start,fixture_offset,total_fixtures_scanned,total_opening_rows
              FROM sync_state_fast WHERE scope='bet365_football_2026_fast'
            """)
            window_start, offset, total_scanned, total_rows = cur.fetchone()

            while window_start < cutoff and (time.monotonic() - started) < MAX_RUNTIME:
                fixtures = fixture_window(window_start)

                if offset >= len(fixtures):
                    window_start = window_start + timedelta(hours=WINDOW_HOURS)
                    offset = 0
                    cur.execute("""
                      UPDATE sync_state_fast SET
                        window_start=%s,fixture_offset=0,updated_at=now()
                      WHERE scope='bet365_football_2026_fast'
                    """, (window_start,))
                    conn.commit()
                    continue

                fixture = fixtures[offset]

                try:
                    n, had, skipped = scan_one(cur, conn, fixture, market_meta, outcome_names)
                except Exception as exc:
                    print("SCAN_ERROR", fixture.get("fixtureId"), repr(exc), flush=True)
                    # Do not advance cursor on an unexpected error. Next run retries safely.
                    break

                if skipped:
                    skipped_run += 1
                else:
                    scanned_run += 1
                    total_scanned += 1
                    inserted_run += n
                    total_rows += n
                    if had:
                        found_run += 1

                offset += 1

                cur.execute("""
                  UPDATE sync_state_fast SET
                    window_start=%s,
                    fixture_offset=%s,
                    total_fixtures_scanned=%s,
                    total_opening_rows=%s,
                    updated_at=now()
                  WHERE scope='bet365_football_2026_fast'
                """, (window_start, offset, total_scanned, total_rows))
                conn.commit()

            cur.execute("SELECT count(*) FROM opening_odds")
            db_rows = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM scanned_fixtures")
            db_scanned = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM scanned_fixtures WHERE had_bet365_history=true")
            db_found = cur.fetchone()[0]

    print({
        "ok": True,
        "run_seconds": round(time.monotonic() - started, 1),
        "scanned_this_run": scanned_run,
        "bet365_fixtures_this_run": found_run,
        "opening_rows_inserted_this_run": inserted_run,
        "already_scanned_skipped_this_run": skipped_run,
        "cursor_window_start": window_start.isoformat(),
        "cursor_offset": offset,
        "db_scanned_fixtures": db_scanned,
        "db_bet365_fixtures": db_found,
        "db_opening_rows": db_rows,
    }, flush=True)


if __name__ == "__main__":
    main()
