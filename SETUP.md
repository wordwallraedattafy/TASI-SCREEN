# TASI automation: setup (about 15 minutes, free)

This gives you two things:
1. **Fast scans.** A script downloads all prices once a day. The web page reads that file, so a full scan takes seconds.
2. **Background alerts.** New signals (after each trading day) and your price alerts (every ~10 minutes during market hours) are sent to your phone even when your computer is off.

## What is in this folder
| File | Purpose |
|---|---|
| `index.html` | The screener page (host it, or open it from your computer) |
| `icon-192.png`, `icon-512.png`, `icon.svg`, `manifest.webmanifest` | The app icon used when you save the page as a shortcut |
| `tasi_daily.py` | The script (no installation needed, standard Python only) |
| `config.json` | Which signals to watch and how to notify |
| `alerts.json` | Your price alerts (edit, or export from the page) |
| `tadawul_stocks.csv` | The stock list (edit to add new listings) |
| `.github/workflows/tasi.yml` | Tells GitHub when to run the script |
| `data/` and `state.json` | Created by the script (prices, and what was already sent) |

## Steps
1. **Create a free GitHub account** and a new repository (button "New"). A *public* repository gives unlimited free run time and free web hosting for the page. A *private* one keeps your alerts hidden but allows only 2,000 free minutes a month and no free page hosting (see "Privacy" below).
2. **Upload all files**: on the repository page choose *Add file, Upload files* and drag in everything from this folder, keeping the `.github` folder. If the `.github` folder does not upload, choose *Add file, Create new file*, type the name `.github/workflows/tasi.yml` and paste the file's text.
3. **Add your notification channel** in *Settings, Secrets and variables, Actions, New repository secret*. Use any of these names (at least one):
   - `NTFY_TOPIC`: a long random topic name. Install the free ntfy app and subscribe to the same name.
   - `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`: from @BotFather, and your chat ID.
   - `DISCORD_WEBHOOK`: the webhook URL.
4. **Run it once by hand**: open the *Actions* tab, choose *TASI daily data and alerts*, *Run workflow*, mode `daily`. The first run takes a few minutes and builds `data/tasi_data.json`. It sends no notification the first time (it only saves a baseline).
5. **Turn on the page** (public repository): *Settings, Pages*, source `main` branch, folder `/ (root)`. Your page will be at `https://YOUR-NAME.github.io/REPOSITORY/`. Rename nothing: it reads `data/tasi_data.json` automatically and shows "Daily data file" as the first source in Settings.
6. **Set your price alerts**: in the page, add alerts in the *Price alerts* tab and press *Export alerts.json*, then upload that file to the repository (replace `alerts.json`). Or edit `alerts.json` directly:
   `{ "code": "2222", "price": 28.50, "dir": "auto" }`  (`dir` can be `auto`, `up` or `down`)

## Schedule (Riyadh time, Sunday to Thursday)
- 15:30: daily prices, signals, notification of NEW signals.
- 10:00 to 14:50, every ~10 minutes: price alerts.

GitHub's timer is not exact and can be 5 to 30 minutes late, and Yahoo prices can be delayed about 15 minutes, so alerts are not instant.

## Changing the rules
Edit `config.json`: moving averages, RSI period and level, Fibonacci levels, how many recent days count as "new", minimum volume, or `watchlist_only` with a list of codes to alert only for those shares.
To reset a price alert that already fired, delete its entry in `state.json` (or change the alert's price).

## Testing without waiting
- *Run workflow* with mode `alerts` runs the price check immediately, even outside market hours.
- On your own computer: `python tasi_daily.py daily --dry-run --limit 20` prints the notification instead of sending it.

## Privacy
- Secrets (tokens, topic) are never visible in the repository.
- In a public repository, `alerts.json`, `state.json` and the data are visible to anyone. Use a private repository if that matters. Then the free page hosting is not available: download `data/tasi_data.json` from the repository now and then, open the page from your computer and choose that file under Settings, Daily data file.

## If something goes wrong
- *Actions* tab shows each run and its log. A red run usually means Yahoo refused the download; the script keeps the previous data and tries again at the next run.
- GitHub pauses scheduled runs in a repository with no activity for 60 days. The daily data commit counts as activity, but if runs stop, press *Enable workflow* in the Actions tab.
- This is a screening aid, not investment advice.

## Saving the page as a shortcut with its icon
Open your GitHub Pages address (not the file from your disk), then:
- **iPhone (Safari):** Share button, then *Add to Home Screen*.
- **Android (Chrome):** the three-dot menu, then *Install app* or *Add to Home screen*.
- **Windows or Mac (Chrome or Edge):** the install icon in the address bar, or menu, *Save and share* (Chrome) / *Apps* (Edge), then *Install*.
The green chart icon is used for the shortcut. Upload `icon-192.png`, `icon-512.png` and `manifest.webmanifest` next to `index.html` in the repository so the installed app also gets the icon and name.
