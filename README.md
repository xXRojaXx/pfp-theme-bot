# PFP Theme Bot

A Discord bot for group PFP theme elections and unique random individual PFP themes.

## Main behavior

### Group elections

- `/pfp new` — Admin: open a new election. Suggestions **and voting are available immediately**.
- `/pfp suggest` — Members: add their own theme idea at any time while the election is open.
- `/pfp random` — Members: get one random unused theme from the large theme bank; it is automatically added to the current election.
- `/pfp suggestions` — Members: view current suggestions and vote counts.
- `Vote` button — Members: vote at any time and change their vote until the election closes.
- `/pfp close` — Admin: close the election and announce the winner.
- `/pfp history` — Members: view previous group winners.
- `/pfp stats` — View theme-bank statistics.

`/pfp random` never gives a theme that is already in the current election. Previous winning themes are also excluded from future random suggestions, so completed PFP themes do not come back unless the history is intentionally recycled.

### Theme-bank administration

- `/pfp theme-add`
- `/pfp theme-disable`
- `/pfp theme-enable`
- `/pfp recycle-themes`

## Admin permissions

Admin-only commands are allowed when the member either:

1. Has the Discord role with ID `1537069353768329246` (`Admin`), or
2. Has Discord's native `Administrator` permission.

## Theme library

The project ships with a large built-in theme bank across movies, TV, games, cartoons, mythology, horror, animals, aesthetics, history, jobs, chaos/comedy, food, nature, seasonal themes, and music.

Use `/pfp stats` to see the exact number loaded by the running bot.

## Railway deployment

1. Create a Discord application and bot.
2. Invite it with the `bot` and `applications.commands` scopes.
3. Add `DISCORD_TOKEN` as a Railway variable.
4. `GUILD_ID` is already configured for server `1483821302463860798`. You may also set it explicitly as a Railway variable.
5. Add a Railway volume mounted at `/data`.
6. Keep `DATABASE_PATH=/data/pfp_theme_bot.db`.
7. Deploy.

The `/data` volume is important because SQLite stores all rounds, votes, assignments, custom themes, and used-theme history there.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Add your token to .env
python main.py
```

## Notes

Discord select menus support up to 25 options, so the group voting flow currently accepts up to 25 suggested themes per round. The unique random system is independent of that limit.
