#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "google-api-python-client>=2.167.0",
#     "google-auth-httplib2>=0.2.0",
#     "google-auth-oauthlib>=1.2.1",
#     "pytz>=2025.2",
#     "pyyaml>=6.0.3",
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
from typing import Any

import requests
import schedule
import yaml
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import Resource, build

DEFAULT_CONFIG = {
    "tacticus_api_url": "https://api.tacticusgame.com/api/v1/guildRaid",
    "sheet": {"name_prefix": "Season "},
}


DB_FILE = Path("tacticus-guild-raid.db")

SCHEDULE_TIME = "08:55"

TIERS_NAMES = ("Common", "Uncommon", "Rare", "Epic", "Legendary", "Mythic")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHEET_NAME_PREFIX = "Season "

logger = logging.getLogger()
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s [%(levelname)s]: %(message)s", "%Y-%m-%d %H:%M:%S")
formatter.converter = time.gmtime
handler.setFormatter(formatter)
logger.addHandler(handler)

sentinel = True


def deep_merge(source: dict[Any, Any], destination: dict[Any, Any]) -> dict[Any, Any]:
    """Deep merge dictionnaries."""
    for key, value in source.items():
        if isinstance(value, dict):
            # get node or create one
            node = destination.setdefault(key, {})
            deep_merge(value, node)
        else:
            destination[key] = value

    return destination


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
        if template is None:
            msg = "Unable to find template sheet"
            raise ValueError(msg)

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


def init_db() -> None:
    """Initialize the database."""

    db = sqlite3.connect(DB_FILE, autocommit=False)
    cursor = db.cursor()

    if not db.in_transaction:
        cursor.execute("begin")

    cursor.executescript(
        """
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
    db.close()


def cleanup_db(db: sqlite3.Connection, season: str) -> None:
    """Remove obsolete data from the database."""

    cursor = db.cursor()

    if not db.in_transaction:
        cursor.execute("begin")

    cursor.executescript(
        f"""
        delete from damages where season < {season};
        delete from bosses where season < {season};
        delete from progress where season < {season};
        commit;
        """
    )


def populate_database(
    db: sqlite3.Connection, config: dict[str, Any], season: str, previous_update: tuple[int, int], entries: list
) -> None:
    """Populate the database with entries from the Tacticus API."""

    cursor = db.cursor()
    tier = level = 0
    updated = False

    # Make sure we have the season in progress table for the foreign key constraint
    query = f"insert or ignore into progress (season, tier, level) values ({season}, {tier}, {level})"
    cursor.execute(query)
    db.commit()

    last_tier, last_level = previous_update

    for entry in entries:
        tier = entry["tier"]

        # Get only wanted tiers
        if (tier not in config["tiers"]) or (tier < last_tier):
            continue

        level = entry["set"]
        # Get only wanted levels
        if tier >= last_tier and level < last_level:
            continue

        # Ignore Bomb damage type
        if entry["damageType"] == "Bomb":
            continue

        if not db.in_transaction:
            cursor.execute("begin")
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
        db.commit()
        updated = True

    if not updated:
        return

    query = f"insert or replace into progress values({season}, {tier}, {level})"
    cursor.execute(query)
    db.commit()


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
    config: dict[str, Any],
    service: Resource,
    spreadsheet_id: str,
    season: str,
    users: list[str],
    previous_update: tuple[int, int],
) -> None:
    """Update the spreadsheet with data gathered from Tacticus API."""

    cursor = db.cursor()

    last_tier, last_level = previous_update
    sheet_name = f"{config['sheet']['name_prefix']}{season}"

    for tier in [t for t in config["tiers"] if t >= last_tier]:
        for level in range(config["sets"][tier]):
            # Get only wanted levels
            if tier >= last_tier and level < last_level:
                continue

            query = f"select name from bosses where tier = {tier} and level = {level} and season = {season}"
            cursor.execute(query)
            row = cursor.fetchone()
            if row is None:
                continue

            sheet_range = config["sheet"]["ranges"][f"{tier}{level}"]

            # Avoid errors when new boss are added
            boss_name = config["bosses"].get(row[0], row[0])
            boss_name_data = {
                "range": sheet_name + "!" + sheet_range["boss_name"],
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
                "range": sheet_name + "!" + sheet_range["dmg"],
                "majorDimension": "COLUMNS",
                "values": [["" for _ in range(len(users))]],
            }
            battles_data = {
                "range": sheet_name + "!" + sheet_range["battles"],
                "majorDimension": "COLUMNS",
                "values": [["" for _ in range(len(users))]],
            }
            for row in cursor.fetchall():
                try:
                    user = users.index(row[0])
                except IndexError:
                    logger.warning("Unkown user ID %s", row[0])
                    continue
                else:
                    damage_data["values"][0][user] = row[1]
                    battles_data["values"][0][user] = row[2]
            sheet_batch_update(service, spreadsheet_id, [boss_name_data, damage_data, battles_data])


def get_season_data(api_key: str, config: dict[str, Any], season: str = "") -> dict:
    """Fetch raid season data on Tacticus API."""

    msg = "Fetching "
    if season:
        url = f"{config['tacticus_api_url']}/{season}"
        msg += f"season {season}"
    else:
        url = config["tacticus_api_url"]
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
    api_key: str, spreadsheet_id: str, google_api_secret: dict, config: dict[str, Any], season: str = ""
) -> None:
    """Update the Google sheet with raid season data."""

    credentials = Credentials.from_service_account_info(google_api_secret, scopes=SCOPES)
    service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    users = get_user_ids(service, spreadsheet_id)

    raid_data = get_season_data(api_key, config, season)
    season = raid_data["season"]

    db = sqlite3.connect(DB_FILE, autocommit=False)

    previous_season = get_last_updated_season(db)
    previous_update = get_last_updated_boss(db, season)

    populate_database(db, config, season, previous_update, raid_data["entries"])

    msg = f"Raid data for season {season}..."
    logger.info(msg)

    create_sheet_if_not_exist(service, spreadsheet_id, f"{config['sheet']['name_prefix']}{season}")

    update_spreadsheet(db, config, service, spreadsheet_id, season, users, previous_update)

    if int(season) > previous_season:
        cleanup_db(db, season)

    db.close()


def load_config(cfg: str) -> dict[str, Any]:
    """Load configuration file."""
    loaded_config: dict[str, Any] = {}
    with Path(cfg).open("r") as f:
        loaded_config = yaml.safe_load(f)

    if not loaded_config:
        msg = "Empty configuration file"
        raise ValueError(msg)

    config = deep_merge(loaded_config, DEFAULT_CONFIG)

    if "sets" not in config:
        msg = "Missing sets dictionnary in configuration file"
        raise ValueError(msg)

    if "tiers" not in config:
        msg = "Missing tiers list in configuration file"
        raise ValueError(msg)

    if "ranges" not in config["sheet"]:
        msg = "Missing sheet ranges configuration in configuration file"
        raise ValueError(msg)

    for tier in config["tiers"]:
        for s in range(config["sets"][tier]):
            if f"{tier}{s}" not in config["sheet"]["ranges"]:
                msg = f"Missing sheet range configuration for tier {tier}{s}"
                raise ValueError(msg)

    return config


def main() -> int:
    """Run the main program."""

    parser = argparse.ArgumentParser()
    parser.add_argument("season", nargs="?", default="", help="Season number to update")
    parser.add_argument("-c", "--config", default="./config.yaml", help="Path to the configuration file")

    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except OSError:
        logger.exception("Unable to load configuration file")
        return 1
    except ValueError:
        logger.exception("Invalid configuration file")
        return 1

    try:
        api_key = getenv("TACTICUS_API_KEY")
        spreadsheet_id = getenv("GUILD_RAID_SPREADSHEET_ID")
        google_api_secret = getenv_json("GOOGLE_API_CREDENTIALS")
    except ValueError:
        logger.exception("Missing environment variable")
        return 1

    init_db()

    schedule.every().day.at(SCHEDULE_TIME, "UTC").do(
        update_raid_data,
        api_key,
        spreadsheet_id,
        google_api_secret,
        config,
        args.season,
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
