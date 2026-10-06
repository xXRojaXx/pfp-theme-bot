# PFP Theme Bot — Northflank

Discord PFP theme election bot. Commands are registered automatically in every server where the bot is installed.

This deployment keeps one shared election and theme bank. Installing it in another server does not create a separate election or migrate stored votes.

## What it does

- `/pfp new` — Admin opens an election. Suggestions and voting are available immediately.
- `/pfp suggest <theme>` — Any member adds their own theme idea.
- `/pfp random` — Any member asks the bot for one random theme from the built-in bank; it is added automatically to the election.
- `Vote` button — Members can vote and change their vote while the election is open.
- `/pfp suggestions` — Shows the current candidates and vote counts.
- `/pfp close` — Admin closes the election and the bot announces the winner.
- `/pfp history` — Shows previous winning themes.
- `/pfp stats` — Shows theme-bank statistics.
- `/pfp theme-add`, `/pfp theme-disable`, `/pfp theme-enable`, `/pfp recycle-themes` — Admin theme-bank controls.

The bot includes **1,091 built-in themes**.

## Admin access

Admin-only commands are allowed when the member either:

- has the Discord role `Admin` with ID `1537069353768329246`, or
- has Discord's native `Administrator` permission.

## No-repeat logic

- A random theme cannot duplicate another suggestion in the same election.
- A winning theme is added to the used-theme history and is excluded from later random selections.
- `/pfp recycle-themes confirm:True` intentionally makes historical winners eligible again.
- PostgreSQL advisory locks prevent simultaneous `/pfp random` calls from reserving the same theme.

## Discord voting limit

Discord select menus support a maximum of 25 choices, so each election is capped at **25 theme suggestions**. The random theme bank itself is much larger and is not limited to 25.

## Deploy on Northflank

### 1. GitHub

Upload this repository to GitHub, including:

- `main.py`
- `database.py`
- `themes.py`
- `requirements.txt`
- `Dockerfile`
- `.gitignore`
- `.env.example`

Do **not** commit your Discord token.

### 2. Create a Northflank project

Create a Developer Sandbox project.

### 3. Create PostgreSQL

Inside the project create an **Addon → PostgreSQL**.

The database does not need to be publicly accessible for the Discord bot.

### 4. Link the database to the bot service

Create a secret group from the PostgreSQL addon and expose its connection string to the service as either:

- `DATABASE_URL`, or
- `POSTGRES_URI`

The code accepts both.

### 5. Create the bot service

Create a **Combined Service** from the GitHub repository.

Use the included `Dockerfile`.

No public HTTP port is required: the bot connects outbound to Discord.

### 6. Environment variables

Add:

```text
DISCORD_TOKEN=<your private Discord bot token>
```

The database URL should come from the linked PostgreSQL addon.

`GUILD_ID` is no longer required. An old value can remain in Northflank without restricting registration to the previous server.

### 7. Deploy

After the container starts, logs should include:

```text
Logged in as ...
Built-in PFP themes loaded: 1091
Synced ... commands to guild <current server ID>.
```

Then test in Discord with `/pfp new`.

Commands are synchronized when the bot connects and when it joins another server. Successful registrations are cached until restart so reconnecting does not repeatedly synchronize them. A failure in one server is logged without stopping registration in the others.

If `/pfp` is missing, check the deployed revision and the `Synced ...` log for that server. The bot's installation must include the `bot` and `applications.commands` OAuth scopes. Members also need permission to use application commands in the target channel.

## Verify command registration locally

```text
python -m unittest discover -s tests -v
```

These tests mock Discord HTTP and database access; no bot token or live PostgreSQL database is needed.
