import csv
import io
import os
import time
from datetime import date, datetime, timedelta, timezone

import psycopg
import requests

DATABASE_URL = os.environ["DATABASE_URL"]

# We keep the existing GitHub secret name to avoid changing the workflow file.
# Its VALUE will be changed to the Render v2 archive token in the next step.
UPSTREAM_TOKEN = os.environ["ODDSPAPI_API_KEY"]

UPSTREAM = "https://bet365-archive-worker-v2.onrender.com"
START_DATE = date(2026, 1, 1)
BATCH_SIZE = 8

# GitHub job timeout is 350 min; stop safely before GitHub kills the job.
MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "20400"))

HTTP = requests.Session()


def ensure_schema(cur):
    cur.execute("""
    CREATE TABLE IF NOT EXISTS opening_odds_archive(
      fixture_id text NOT NULL,
      kickoff_utc timestamptz,
      category text,
      tournament text,
      tournament_id text,
      season_id text,
      home_team text,
      away_team text,
      bookmaker text NOT NULL,
      market_id text NOT NULL,
      market_name text,
      market_type text,
      period text,
      line_value text,
      player_prop text,
      outcome_id text NOT NULL,
      outcome_name text,
      player_id text NOT NULL DEFAULT '',
      opening_odds_decimal numeric,
      first_seen_utc timestamptz,
      PRIMARY KEY(fixture_id,bookmaker,market_id,outcome_id,player_id)
    );

    CREATE INDEX IF NOT EXISTS opening_odds_archive_kickoff_idx
      ON opening_odds_archive(kickoff_utc);

    CREATE INDEX IF NOT EXISTS opening_odds_archive_market_idx
      ON opening_odds_archive(market_id);

    CREATE TABLE IF NOT EXISTS github_backfill_state(
      scope text PRIMARY KEY,
      cursor_date date NOT NULL,
      fixture_offset integer NOT NULL DEFAULT 0,
      total_fixtures_processed bigint NOT NULL DEFAULT 0,
      total_rows_inserted bigint NOT NULL DEFAULT 0,
      updated_at timestamptz NOT NULL DEFAULT now()
    );
    """)

    cur.execute("""
      INSERT INTO github_backfill_state(scope,cursor_date,fixture_offset)
      VALUES('bet365_2026_render_proxy_v1', %s, 0)
      ON CONFLICT(scope) DO NOTHING
    """, (START_DATE,))


def get_fixture_count(day):
    r = HTTP.get(
        UPSTREAM + "/fixtures",
        params={"date": str(day), "token": UPSTREAM_TOKEN},
        timeout=180,
    )

    # Free Render instances can occasionally wake with a transient 502.
    if r.status_code in (502, 503, 504):
        time.sleep(12)
        r = HTTP.get(
            UPSTREAM + "/fixtures",
            params={"date": str(day), "token": UPSTREAM_TOKEN},
            timeout=180,
        )

    r.raise_for_status()
    data = r.json()
    return int(data.get("finished_fixture_count", 0))


def export_batch(day, offset):
    r = HTTP.get(
        UPSTREAM + "/export",
        params={
            "date": str(day),
            "offset": offset,
            "limit": BATCH_SIZE,
            "token": UPSTREAM_TOKEN,
        },
        timeout=600,
    )

    # Retry common free-instance wakeup / gateway errors once.
    if r.status_code in (502, 503, 504):
        time.sleep(12)
        r = HTTP.get(
            UPSTREAM + "/export",
            params={
                "date": str(day),
                "offset": offset,
                "limit": BATCH_SIZE,
                "token": UPSTREAM_TOKEN,
            },
            timeout=600,
        )

    r.raise_for_status()

    fixture_total = int(r.headers.get("X-Fixture-Total", "0"))
    fixture_count = int(r.headers.get("X-Fixture-Count", "0"))
    source_rows = int(r.headers.get("X-Odds-Rows", "0"))

    rows = list(csv.DictReader(io.StringIO(r.text)))

    return fixture_total, fixture_count, source_rows, rows


def insert_rows(cur, rows):
    inserted = 0

    for x in rows:
        cur.execute("""
          INSERT INTO opening_odds_archive(
            fixture_id,kickoff_utc,category,tournament,tournament_id,season_id,
            home_team,away_team,bookmaker,market_id,market_name,market_type,
            period,line_value,player_prop,outcome_id,outcome_name,player_id,
            opening_odds_decimal,first_seen_utc
          )
          VALUES(
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s
          )
          ON CONFLICT(fixture_id,bookmaker,market_id,outcome_id,player_id)
          DO NOTHING
        """, (
            x.get("fixture_id"),
            x.get("kickoff_utc") or None,
            x.get("category"),
            x.get("tournament"),
            x.get("tournament_id"),
            x.get("season_id"),
            x.get("home_team"),
            x.get("away_team"),
            x.get("bookmaker") or "Bet365",
            x.get("market_id"),
            x.get("market_name"),
            x.get("market_type"),
            x.get("period"),
            x.get("line_value"),
            x.get("player_prop"),
            x.get("outcome_id"),
            x.get("outcome_name"),
            x.get("player_id") or "",
            x.get("opening_odds_decimal") or None,
            x.get("first_seen_utc") or None,
        ))
        inserted += cur.rowcount

    return inserted


def main():
    started = time.monotonic()
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=1)

    batches_this_run = 0
    fixtures_this_run = 0
    source_rows_this_run = 0
    inserted_this_run = 0

    with psycopg.connect(DATABASE_URL) as conn:
        conn.autocommit = False

        with conn.cursor() as cur:
            ensure_schema(cur)
            conn.commit()

            cur.execute("""
              SELECT cursor_date,fixture_offset,total_fixtures_processed,total_rows_inserted
              FROM github_backfill_state
              WHERE scope='bet365_2026_render_proxy_v1'
            """)
            day, offset, total_fixtures, total_rows = cur.fetchone()

            while day <= cutoff and (time.monotonic() - started) < MAX_RUNTIME_SECONDS:
                total_for_day = get_fixture_count(day)

                if total_for_day == 0 or offset >= total_for_day:
                    day += timedelta(days=1)
                    offset = 0

                    cur.execute("""
                      UPDATE github_backfill_state
                      SET cursor_date=%s,fixture_offset=0,updated_at=now()
                      WHERE scope='bet365_2026_render_proxy_v1'
                    """, (day,))
                    conn.commit()
                    continue

                fixture_total, fixture_count, source_rows, rows = export_batch(day, offset)

                if fixture_count <= 0:
                    # Defensive: avoid an infinite loop.
                    raise RuntimeError(
                        f"Export returned zero fixtures at {day} offset={offset}; "
                        f"fixture_total={fixture_total}"
                    )

                inserted = insert_rows(cur, rows)

                offset += fixture_count
                total_fixtures += fixture_count
                total_rows += inserted

                fixtures_this_run += fixture_count
                source_rows_this_run += source_rows
                inserted_this_run += inserted
                batches_this_run += 1

                if offset >= fixture_total:
                    day += timedelta(days=1)
                    offset = 0

                cur.execute("""
                  UPDATE github_backfill_state
                  SET
                    cursor_date=%s,
                    fixture_offset=%s,
                    total_fixtures_processed=%s,
                    total_rows_inserted=%s,
                    updated_at=now()
                  WHERE scope='bet365_2026_render_proxy_v1'
                """, (day, offset, total_fixtures, total_rows))
                conn.commit()

                if batches_this_run % 10 == 0:
                    print({
                        "progress": True,
                        "batches_this_run": batches_this_run,
                        "fixtures_this_run": fixtures_this_run,
                        "source_rows_this_run": source_rows_this_run,
                        "inserted_this_run": inserted_this_run,
                        "cursor_date": str(day),
                        "cursor_offset": offset,
                        "total_fixtures_processed": total_fixtures,
                        "total_rows_inserted": total_rows,
                    }, flush=True)

            cur.execute("SELECT count(*) FROM opening_odds_archive")
            db_rows = cur.fetchone()[0]

    print({
        "ok": True,
        "run_seconds": round(time.monotonic() - started, 1),
        "batches_this_run": batches_this_run,
        "fixtures_this_run": fixtures_this_run,
        "source_rows_this_run": source_rows_this_run,
        "inserted_this_run": inserted_this_run,
        "cursor_date": str(day),
        "cursor_offset": offset,
        "total_fixtures_processed": total_fixtures,
        "total_rows_inserted": total_rows,
        "rows_in_database": db_rows,
    }, flush=True)


if __name__ == "__main__":
    main()
