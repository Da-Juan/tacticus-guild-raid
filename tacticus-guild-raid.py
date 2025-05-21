#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "google-api-python-client>=2.167.0",
#     "google-auth-httplib2>=0.2.0",
#     "google-auth-oauthlib>=1.2.1",
#     "pytz>=2025.2",
#     "requests>=2.32.3",
#     "schedule>=1.2.2",
# ]
# ///
"""Get raid season data from Tactius API and update Google sheet."""

import argparse
import contextlib
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path
from types import FrameType

import requests
import schedule
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import Resource, build

TACTICUS_API_URL = "https://api.tacticusgame.com/api/v1/guildRaid"

SCHEDULE_TIME = "08:55"

# Filter only Epic and Legendary tiers
TIERS = (3, 4)
SETS = {0: 4, 1: 4, 2: 4, 3: 5, 4: 5}
TIERS_NAMES = ("Common", "Uncommon", "Rare", "Epic", "Legendary")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

BOSSES = {
    "HiveTyrantGorgon": "Hive Tyrant (Hive fleet Gorgon)",
    "HiveTyrantKronos": "Hive Tyrant (Hive fleet Kronos)",
    "HiveTyrantLeviathan": "Hive Tyrant (Hive fleet Leviathan)",
    "TervigonGorgon": "Tervigon (Hive fleet Gorgon)",
    "TervigonKronos": "Tervigon (Hive fleet Kronos)",
    "TervigonLeviathan": "Tervigon (Hive fleet Leviathan)",
    "SilentKing": "Szarekh",
    "Ghazghkull": "Ghazghkull Mag Uruk Thraka",
    "Mortarion": "Mortarion",
    "ScreamerKiller": "Screamer-killer",
    "RogalDorn": "Rogal Dorn battle tank",
    "AvatarOfKhaine": "Avatar of Khaine",
    "Magnus": "Magnus",
    "Belisarius": "Belisarius Cawl",
}

SHEET_NAME_PREFIX = "Season "

SHEET_RANGES = {
    "30": {
        "boss_name": "Q2",
        "dmg": "Q4:Q33",
        "battles": "R4:R33",
    },
    "31": {
        "boss_name": "T2",
        "dmg": "T4:T33",
        "battles": "U4:U33",
    },
    "32": {
        "boss_name": "W2",
        "dmg": "W4:W33",
        "battles": "X4:X33",
    },
    "33": {
        "boss_name": "Z2",
        "dmg": "Z4:Z33",
        "battles": "AA4:AA33",
    },
    "34": {
        "boss_name": "AC2",
        "dmg": "AC4:AC33",
        "battles": "AD4:AD33",
    },
    "40": {
        "boss_name": "AF2",
        "dmg": "AF4:AF33",
        "battles": "AG4:AG33",
    },
    "41": {
        "boss_name": "AI2",
        "dmg": "AI4:AI33",
        "battles": "AJ4:AJ33",
    },
    "42": {
        "boss_name": "AL2",
        "dmg": "AL4:AL33",
        "battles": "AM4:AM33",
    },
    "43": {
        "boss_name": "AO2",
        "dmg": "AO4:AO33",
        "battles": "AP4:AP33",
    },
    "44": {
        "boss_name": "AR2",
        "dmg": "AR4:AR33",
        "battles": "AS4:AS33",
    },
}

logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s [%(levelname)s]: %(message)s", "%Y-%m-%d %H:%M:%S")
formatter.converter = time.gmtime
handler.setFormatter(formatter)
logger.addHandler(handler)

sentinel = True


def get_user_ids(service: Resource, spreadsheet_id: str) -> list[str]:
    """Get the list of user ids from the Google sheet."""

    users_range = "Players!B2:B31"

    sheet = service.spreadsheets()
    result = sheet.values().get(spreadsheetId=spreadsheet_id, range=users_range).execute()
    values = result.get("values", [])

    if not values:
        logger.error("No data found")
        return []

    return [v[0] for v in values]


def sheet_batch_update(service: Resource, spreadsheet_id: str, data: list) -> None:
    """Update the Google sheet."""

    body = {"valueInputOption": "RAW", "data": data}
    result = service.spreadsheets().values().batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()
    msg = f"{(result.get('totalUpdatedCells'))} cells updated"
    logger.info(msg)


def get_sheet_index(title: str, sheets: list) -> tuple[int, int] | None:
    """Get the id and index of a sheet.

    Returns None if the sheet does not exist.
    """

    for sheet in sheets:
        if sheet["properties"]["title"] == title:
            return int(sheet["properties"]["sheetId"]), int(sheet["properties"]["index"])

    return None


def create_sheet_if_not_exist(service: Resource, spreadsheet_id: str, title: str) -> None:
    """Create a sheet if it does not already exists."""

    result = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets = result.get("sheets")

    if get_sheet_index(title, sheets) is None:
        msg = f"Creating sheet '{title}'"
        logger.info(msg)

        template = get_sheet_index("Template", sheets)
        body = {
            "includeSpreadsheetInResponse": False,
            "requests": [
                {
                    "duplicateSheet": {
                        "sourceSheetId": template[0],
                        "insertSheetIndex": template[1] + 1,
                        "newSheetName": title,
                    }
                }
            ],
        }
        service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()


def init_db(db: sqlite3.Connection) -> None:
    """Initialize the database."""

    cursor = db.cursor()

    cursor.executescript(
        """
        begin;
        PRAGMA foreign_keys = ON;
        create table if not exists progress(season int primary key, tier int, level int);
        create table if not exists bosses(
            season int, tier int, level int, name text, constraint uc_stl unique(season, tier, level)
        );
        create table if not exists damages(
            tier int,
            level int,
            dmg int,
            userid text,
            completedon int unique,
            season int,
            foreign key(season) references progress(season)
        );
        commit;
        """
    )


def cleanup_db(db: sqlite3.Connection, season: str) -> None:
    """Remove obsolete data from the database."""

    cursor = db.cursor()

    cursor.executescript(
        f"""
        begin;
        delete from damages where season < {season};
        delete from bosses where season < {season};
        delete from progress where season < {season};
        commit;
        """
    )


def populate_database(db: sqlite3.Connection, season: str, previous_update: tuple[int, int], entries: list) -> None:
    """Populate the database with entries from the Tacticus API."""

    cursor = db.cursor()
    tier = level = 0
    updated = False

    # Make sure we have the season in progress table for the foreign key constraint
    query = f"insert or ignore into progress (season, tier, level) values ({season}, {tier}, {level})"
    cursor.execute(query)

    last_tier, last_level = previous_update

    for entry in entries:
        tier = entry["tier"]

        # Get only wanted tiers
        if (tier not in TIERS) or (tier < last_tier):
            continue

        level = entry["set"]
        # Get only wanted levels
        if tier >= last_tier and level < last_level:
            continue

        # Ignore Bomb damage type
        if entry["damageType"] == "Bomb":
            continue

        query = f"""
        insert or ignore into bosses (season, tier, level, name) values ({season}, {tier}, {level}, '{entry["type"]}')
        """
        cursor.execute(query)
        query = f"""
        insert or ignore into damages values(
            {tier},
            {level},
            {entry["damageDealt"]},
            '{entry["userId"]}',
            {entry["completedOn"]},
            {season}
        )
        """
        cursor.execute(query)
        updated = True

    if not updated:
        return

    query = f"insert or replace into progress values({season}, {tier}, {level})"
    cursor.execute(query)


def get_last_updated_boss(db: sqlite3.Connection, season: str) -> tuple[int, int]:
    """Get the last updated boss from the database."""

    cursor = db.cursor()

    query = f"select tier, level from progress where season = {season}"
    cursor.execute(query)
    if (result := cursor.fetchone()) is None:
        return (0, 0)

    return result


def get_last_updated_season(db: sqlite3.Connection) -> int:
    """Get the last updated season from the database."""

    result = 0

    cursor = db.cursor()

    query = "select season from progress order by season desc limit 1"
    cursor.execute(query)
    if (result := cursor.fetchone()) is None:
        return 0

    return result[0]


def update_spreadsheet(  # noqa: PLR0913
    db: sqlite3.Connection,
    service: Resource,
    spreadsheet_id: str,
    season: str,
    users: list[str],
    previous_update: tuple[int, int],
) -> None:
    """Update the spreadsheet with data gathered from Tacticus API."""

    cursor = db.cursor()

    last_tier, last_level = previous_update
    sheet_name = f"{SHEET_NAME_PREFIX}{season}"

    for tier in [t for t in TIERS if t >= last_tier]:
        for level in range(SETS[tier]):
            # Get only wanted levels
            if tier >= last_tier and level < last_level:
                continue

            query = f"select name from bosses where tier = {tier} and level = {level}"
            cursor.execute(query)
            row = cursor.fetchone()
            if row is None:
                continue
            boss_name = BOSSES[row[0]]
            boss_name_data = {
                "range": sheet_name + "!" + SHEET_RANGES[f"{tier}{level}"]["boss_name"],
                "majorDimension": "ROWS",
                "values": [[boss_name]],
            }
            msg = f"{TIERS_NAMES[tier]} {level + 1}: {boss_name}"
            logger.info(msg)
            query = f"""
                select userid, sum(dmg), count(userid) from damages
                where tier = {tier} and level = {level} and season = {season} group by userid
            """
            cursor.execute(query)
            damage_data = {
                "range": sheet_name + "!" + SHEET_RANGES[f"{tier}{level}"]["dmg"],
                "majorDimension": "COLUMNS",
                "values": [["" for _ in range(len(users))]],
            }
            battles_data = {
                "range": sheet_name + "!" + SHEET_RANGES[f"{tier}{level}"]["battles"],
                "majorDimension": "COLUMNS",
                "values": [["" for _ in range(len(users))]],
            }
            for row in cursor.fetchall():
                damage_data["values"][0][users.index(row[0])] = row[1]
                battles_data["values"][0][users.index(row[0])] = row[2]
            sheet_batch_update(service, spreadsheet_id, [boss_name_data, damage_data, battles_data])


def get_season_data(api_key: str, season: str = "") -> dict:
    """Fetch raid season data on Tacticus API."""

    msg = "Fetching "
    if season:
        url = f"{TACTICUS_API_URL}/{season}"
        msg += f"season {season}"
    else:
        url = TACTICUS_API_URL
        msg += "current season"
    msg += " raid data..."

    logger.info(msg)
    headers = {"accept": "application/json", "X-API-KEY": api_key}
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()

    return response.json()


def getenv_json(env: str, default: str = "") -> dict:
    """Get json from environment variable."""

    content = getenv(env, default)
    return json.loads(content)


def getenv(env: str, default: str = "") -> str:
    """Get environment variables.

    Lookup "env_FILE", "env", then raise an error.
    """

    ret = ""

    env_file = f"{env}_FILE"
    if env_file in os.environ:
        with contextlib.suppress(OSError):
            ret = Path(os.environ.get(env_file, default)).read_text().strip()
    elif env in os.environ:
        ret = os.environ.get(env, default)

    if not ret:
        msg = f"Environment variable {env} is required"
        raise ValueError(msg)

    return ret


def signal_handler(sig: int, _: FrameType | None) -> None:
    """Handle signal for a clean exit."""
    global sentinel  # noqa: PLW0603

    logger.info("Recieved signal %s, exiting.", sig)
    sentinel = False


def update_raid_data(
    db: sqlite3.Connection, api_key: str, spreadsheet_id: str, google_api_secret: dict, season: str = ""
) -> None:
    """Update the Google sheet with raid season data."""

    credentials = Credentials.from_service_account_info(google_api_secret, scopes=SCOPES)
    service = build("sheets", "v4", credentials=credentials)
    users = get_user_ids(service, spreadsheet_id)

    raid_data = get_season_data(api_key, season)
    season = raid_data["season"]

    previous_season = get_last_updated_season(db)
    previous_update = get_last_updated_boss(db, season)

    populate_database(db, season, previous_update, raid_data["entries"])

    msg = f"Raid data for season {season}..."
    logger.info(msg)

    create_sheet_if_not_exist(service, spreadsheet_id, f"{SHEET_NAME_PREFIX}{season}")

    update_spreadsheet(db, service, spreadsheet_id, season, users, previous_update)

    if int(season) > previous_season:
        cleanup_db(db, season)


def main() -> int:
    """Run the main program."""

    parser = argparse.ArgumentParser()
    parser.add_argument("season", nargs="?", default="", help="Season number to update")

    args = parser.parse_args()

    try:
        api_key = getenv("TACTICUS_API_KEY")
        spreadsheet_id = getenv("GUILD_RAID_SPREADSHEET_ID")
        google_api_secret = getenv_json("GOOGLE_API_CREDENTIALS")
    except ValueError:
        logger.exception("Missing environment variable")
        return 1

    db = sqlite3.connect(":memory:")
    db.autocommit = True
    init_db(db)

    schedule.every().day.at(SCHEDULE_TIME, "UTC").do(
        update_raid_data, db, api_key, spreadsheet_id, google_api_secret, args.season
    )

    # if season is provided it's a one shot run
    if args.season:
        schedule.run_all()
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, signal_handler)
        while sentinel:
            schedule.run_pending()
            time.sleep(1)

    schedule.clear()
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
