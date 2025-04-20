#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "google-api-python-client>=2.167.0",
#     "google-auth-httplib2>=0.2.0",
#     "google-auth-oauthlib>=1.2.1",
#     "requests>=2.32.3",
# ]
# ///
"""Get raid season data from Tactius API and update Google sheet."""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import Resource, build

TACTICUS_API_URL = "https://api.tacticusgame.com/api/v1/guildRaid/"

REQUIRED_ENV_VARS = ("GUILD_RAID_SPREADSHEET_ID", "GOOGLE_API_CREDENTIALS", "TACTICUS_API_KEY")

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


def get_user_ids(service: Resource, spreadsheet_id: str) -> list[str]:
    """Get the list of user ids from the Google sheet."""

    users_range = "Players!B2:B31"

    sheet = service.spreadsheets()
    result = sheet.values().get(spreadsheetId=spreadsheet_id, range=users_range).execute()
    values = result.get("values", [])

    if not values:
        print("No data found.")
        return []

    return [v[0] for v in values]


def sheet_batch_update(service: Resource, spreadsheet_id: str, data: list) -> None:
    """Update the Google sheet."""

    body = {"valueInputOption": "RAW", "data": data}
    result = service.spreadsheets().values().batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()
    print(f"{(result.get('totalUpdatedCells'))} cells updated.")


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
        print(f"Creating sheet '{title}'")

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
        create table if not exists bosses(tier int, level int, name text, constraint uc_tier_level unique(tier, level));
        create table if not exists damages(tier int, level int, dmg int, userid text);
        commit;
        """
    )


def populate_database(db: sqlite3.Connection, entries: list) -> None:
    """Populate the database with entries from the Tacticus API."""

    cursor = db.cursor()

    for entry in entries:
        # Get only wanted tiers
        if entry["tier"] not in TIERS:
            continue

        # Ignore Bomb damage type
        if entry["damageType"] == "Bomb":
            continue

        query = f"insert or ignore into bosses values({entry['tier']}, {entry['set']}, '{entry['type']}')"
        cursor.execute(query)
        query = f"""
        insert into damages values(
            {entry["tier"]},
            {entry["set"]},
            {int(entry["damageDealt"])}, '{entry["userId"]}'
        )
        """
        cursor.execute(query)

    cursor.execute("commit")


def update_spreadsheet(
    db: sqlite3.Connection, service: Resource, spreadsheet_id: str, sheet_name: str, users: list[str]
) -> None:
    """Update the spreadsheet with data gathered from Tacticus API."""

    cursor = db.cursor()
    for tier in TIERS:
        for level in range(SETS[tier]):
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
            print(f"{TIERS_NAMES[tier]} {level + 1}: {boss_name}")
            query = f"""
                select userid, sum(dmg), count(userid) from damages
                where tier = {tier} and level = {level} group by userid
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


def get_season_data(api_key: str, season: str) -> dict:
    """Fetch raid season data on Tacticus API."""

    print(f"Fetching season {season} raid data...")
    headers = {"accept": "application/json", "X-API-KEY": api_key}
    response = requests.get(TACTICUS_API_URL + season, headers=headers, timeout=10)
    response.raise_for_status()

    return response.json()


def main() -> int:
    """Run the main program."""

    parser = argparse.ArgumentParser()
    parser.add_argument("season", help="Season number to update")

    args = parser.parse_args()

    for variable in REQUIRED_ENV_VARS:
        if variable not in os.environ:
            print(f"ERROR: {variable} environment variable not found")
            return 1

    api_key = os.environ.get("TACTICUS_API_KEY", "")
    spreadsheet_id = os.environ.get("GUILD_RAID_SPREADSHEET_ID", "")
    google_api_secret = Path(os.environ.get("GOOGLE_API_CREDENTIALS", "")).expanduser()
    if not google_api_secret.exists():
        print(f"ERROR: '{google_api_secret}' is an invalid path")
        return 1

    credentials = Credentials.from_service_account_file(google_api_secret, scopes=SCOPES)
    service = build("sheets", "v4", credentials=credentials)
    users = get_user_ids(service, spreadsheet_id)

    db = sqlite3.connect(":memory:")
    init_db(db)

    season = args.season
    raid_data = get_season_data(api_key, season)

    populate_database(db, raid_data["entries"])

    print(f"Raid data for season {season}...")
    sheet_name = f"Season {season}"
    create_sheet_if_not_exist(service, spreadsheet_id, sheet_name)

    update_spreadsheet(db, service, spreadsheet_id, sheet_name, users)

    db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
