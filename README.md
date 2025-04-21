# Tacticus guild raid data

Fetch guild raid data from Tacticus API and store them in a Google Sheet to make statistics.

## Usage

You need to set 3 environment variables for the script to run:
* `GUILD_RAID_SPREADSHEET_ID`: the spreadsheet ID to update
* `GOOGLE_API_CREDENTIALS`: the path to the JSON file containing Google API credentials
* `TACTICUS_API_KEY`: the Tacticus API key with the `Guild Raid` scope

Each variable name can be suffixed with `_FILE` and be set to the path of a file containing the variable value.

If a season is provided on the command line it will do a single run to update the given season.  
If no season is provided, it will run on a schedule every day at 8:55 AM UTC.
```
usage: tacticus-guild-raid.py [-h] [season]

positional arguments:
  season      Season number to update

options:
  -h, --help  show this help message and exit
```
