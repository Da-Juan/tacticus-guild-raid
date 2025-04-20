# Tacticus guild raid data

Fetch guild raid data from Tacticus API and store them in a Google Sheet to make statistics.

## Usage

You need to set 3 environment variables for the script to run:
* `GUILD_RAID_SPREADSHEET_ID`: the spreadsheet ID to update
* `GOOGLE_API_CREDENTIALS`: the path to the JSON file containing Google API credentials
* `TACTICUS_API_KEY`: the Tacticus API key with the `Guild Raid` scope

```
usage: tacticus-guild-raid.py [-h] [season]

positional arguments:
  season      Season number to update

options:
  -h, --help  show this help message and exit
```
