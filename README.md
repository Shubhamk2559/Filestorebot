# File Sharing Telegram Bot (button-only)

Async Python bot (python-telegram-bot v21 + MongoDB/Motor). The **only typed command is `/start`**;
everything else is inline buttons and conversational prompts. Settings live in MongoDB and are cached in memory.

```
main.py              entry point (polling + health server + auto-delete worker)
config.py            env vars (BOT_TOKEN, API_ID, API_HASH, MONGO_URI, OWNER_ID)
database/db.py      Motor collections + settings cache
handlers/start.py   /start, force-sub gate, "I've Joined", file delivery
handlers/admin.py   the whole admin panel (buttons + state machine)
handlers/upload.py  admin uploads, batch mode, link creation
handlers/broadcast.py  broadcast with progress + flood control
utils/              helpers, keyboards, membership check, shortener, scheduler, web server (/health, /dl, /ad)
```

## 1. Get the credentials
| Variable | Where |
|---|---|
| `BOT_TOKEN` | Telegram **@BotFather** -> /newbot |
| `API_ID`, `API_HASH` | https://my.telegram.org -> API development tools (optional for this PTB build) |
| `MONGO_URI` | MongoDB Atlas free cluster -> Connect -> Drivers (allow your host IP / 0.0.0.0/0) |
| `OWNER_ID` | Message **@userinfobot** to see your numeric ID |

## 2. Deploy on a VPS (recommended: Docker + automatic HTTPS)
1. Point a domain / subdomain (A record) to your VPS IP. Open ports **80** and **443** in the VPS firewall.
2. In MongoDB Atlas -> Network Access, allow the VPS IP (or 0.0.0.0/0).
3. Upload this folder to the VPS, then:
```bash
cp .env.example .env && nano .env     # BOT_TOKEN, OWNER_ID, MONGO_URI, API_ID, API_HASH, DOMAIN
bash deploy.sh                        # installs Docker if needed, builds, starts bot + Caddy (HTTPS)
```
Update later: upload the new files and run `bash deploy.sh` again. Logs: `docker compose logs -f bot`.

`BASE_URL` is set automatically to `https://$DOMAIN` (needed for Monetag ads and Stream/Download links).
Only **one** instance may run per `BOT_TOKEN` (stop old deployments on Koyeb/Replit/Render first).

**Without Docker:**
```bash
python3 -m venv venv && . venv/bin/activate && pip install -r requirements.txt tgcrypto && python main.py
```
Put your own reverse proxy (nginx/Caddy, `proxy_buffering off`) in front of port 8080 and set `BASE_URL`.

**Koyeb / Render / Heroku:** deploy as a Docker service, add the env vars, health check path `/health`.

## 3. First-time setup (inside Telegram)
1. Create a **private channel** for storage and add the bot as **admin** (Post Messages).
2. Send `/start` as the owner -> Admin Panel -> **Database Channel** -> forward any message from that channel.
3. Add your force-sub channels (bot must be admin in each). Done - send any file to get a link.

## 4. Panel buttons
- **Create Batch** - *Send files* then **Done** = one link for all; or forward the **first + last** post from the DB channel.
- **My Links** - list, view views/count, **Revoke** any link (owner sees all links).
- **Database Channel** - forward a message from it / send ID / @username; bot verifies admin rights.
- **Force-Sub** - add/remove channels (public or private), reorder with up/down, enable/disable.
- **Start Image / Caption** - photo + HTML text; placeholders `{first_name}`, `{mention}`.
- **Join Buttons** - channel buttons are automatic; add custom ones as `Text - URL` (`|` for same row).
- **Auto-Delete** - Off / 5 / 10 / 30 min (survives restarts). **Protect Content** - blocks forwarding/saving.
- **File Caption** - custom caption, or strip the original caption.
- **Shortener** - optional API URL + key (GPLinks-style `?api=KEY&url=LINK`).
- **Broadcast** - send/forward a message, confirm, watch live progress (flood-safe).
- **Stats** - users, files, links, banned, blocked. **Ban/Unban** - forward a message or send an ID.
- **Admins** (owner only) - add/remove by forwarded message or ID. **Reset** - defaults (keeps admins + DB channel).

## 5. Behaviour notes
- **Daily limit = 10 links per user** (1 link = 1 count, every file inside the link is delivered). 5 second cooldown between two link opens. Admins are exempt.
- Users get files **only** via `t.me/<bot>?start=<token>`; tokens are random, stored in MongoDB.
- Non-admin uploads/messages are ignored. Admins skip force-sub.
- If the bot loses admin rights in a channel, users are not blocked and the owner gets a warning message.
- Hidden-forward users/channels can't be read from forwards: send the numeric ID instead.
