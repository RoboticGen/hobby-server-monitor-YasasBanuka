# Hobby Server Monitor (HSM) - Final Report

## Time Spent
- **Backend (API & Database):** ~15 hours. The bulk of this time was spent migrating the time-series storage from TinyFlux to SQLite WAL mode and ensuring concurrent read/write safety between the API and Collector.
- **LXD Integration (Collector):** ~8 hours. Moving from expensive `exec` calls to polling `/proc/uptime` offsets took significant tuning to achieve O(N) complexity constraints.
- **Frontend (Astro & UI):** ~12 hours. Building the dashboard, parsing live metrics via the `live_cache.json`, and writing minimal vanilla CSS.
- **Infrastructure & Debugging:** ~10 hours. Setting up Nginx, securing systemd unit files, establishing the CI/CD pipeline, and troubleshooting port bindings and Nginx reverse proxy routing.

## Issues Encountered and Solutions
1. **The N+1 Query Problem:** Initially, listing a user's allocated resources (`_get_user_allocation`) made an LXD API call for *every single container* in a loop. **Solution:** Rewrote the function to call `client.instances.all()` once and filter locally, changing the page load time from seconds to milliseconds.
2. **TinyFlux I/O Thrashing:** Early on, we used TinyFlux for metric storage. We quickly realized that appending to a CSV file every 10 seconds for dozens of containers caused massive I/O spikes. **Solution:** Migrated entirely to a SQLite TSDB (`metrics.db`) running in WAL mode with hourly compaction jobs.
3. **Cookie Redirection Drops:** Our initial OAuth implementation used `SameSite=Strict` for the session cookie. This caused Google's redirect callback to drop the cookie, forcing users to log in twice. **Solution:** We split the cookies—using `SameSite=Lax` temporarily for the OAuth state validation, and then setting the permanent session JWT cookie to `Strict`.
4. **Orphan Container Wipes:** The collector was designed to delete DB records of containers that no longer existed in LXD. During a temporary LXD API crash, the API returned an empty list, and our collector wiped the entire database. **Solution:** Added a safeguard where if the LXD response list is empty, the orphan sync is safely aborted.

## What I Learned
- **Systemd Hardening:** I learned how to lock down background processes. Using `ProtectSystem=strict` and `PrivateTmp=yes` prevents a compromised Python script from writing anywhere on the host machine except for the designated `/opt/hsm/backend/data` directory.
- **SQLite Concurrency:** SQLite is traditionally single-user, but enabling Write-Ahead Logging (`PRAGMA journal_mode=WAL`) turns it into a highly capable embedded database capable of handling our background writer and web readers simultaneously.
- **Decoupled Architecture:** Separating the metric collector into its own daemon process completely removed the risk of the dashboard API timing out during heavy LXD loads.

## Bonus Features Implemented
- **Automated CI/CD Pipeline:** Added a GitHub Actions workflow (`ci.yml`) that lints the backend and uses passwordless SSH to deploy, run `npm ci`, and seamlessly restart systemd services on push to `main`.
- **Nginx Reverse Proxy & Static Serving:** Hardened the frontend by letting Nginx serve the Astro static bundle directly from disk, routing only `/api/` traffic to the backend bound strictly to `127.0.0.1`.
- **Thread-Local DB Connections:** Implemented thread-local storage for SQLite connections (`db.py`) to safely scale Gunicorn workers in the future without implementing a heavy connection pool like PgBouncer.
- **Nightly TSDB Compaction:** Wrote a task that rolls up 10-second data into hourly averages and drops the raw data, preventing the database from growing indefinitely.

## Resource Measurements
Our strict O(N) complexity constraints succeeded. The system footprint is extremely lean:
- **Idle RAM:** The entire Python backend API and Collector daemon consume less than **50MB of RAM** combined (measured via `htop` RES memory).
- **CPU:** The Collector daemon peaks at ~1% CPU every 10 seconds during the polling phase.
- **Storage:** The `live_cache.json` is < 5KB. The SQLite TSDB grows at a fixed, predictable rate and plateaus due to the nightly compaction script.
- **Frontend:** Initial page load is < 200KB uncompressed, as we relied on Astro's static generation rather than a heavy React SPA.

## Known Limitations
- **Multi-Node LXD:** The collector currently assumes a single, local LXD socket. It does not natively cluster across a fleet of remote LXD nodes.
- **LXD Network Driver Dependency:** Network usage metrics rely on standard `eth0` bridging. Containers using highly customized network setups might not report RX/TX bytes accurately to the LXD API.

## AI Tool Usage
I utilized an advanced AI agent during this project. 
- **What I used it for:** I primarily leaned on the AI for architectural brainstorming (like deciding between SQLite vs Redis for the live cache) and diagnosing difficult Linux infrastructure bugs (such as systemd reloading constraints and Nginx server-block proxy issues).
- **What I accepted:** The AI's recommendation to use `PRAGMA journal_mode=WAL` for SQLite concurrency was brilliant and instantly solved our database locking issues. I also accepted its regex pattern for email validation.
- **What I rejected/fixed:** The AI initially suggested keeping TinyFlux and just batching writes. I rejected this because even batched CSV rewrites are fundamentally unscalable for time-series data. I instructed the AI to pivot entirely to a SQLite-backed TSDB instead, which proved to be the correct architectural choice. I also had to correct the AI when it attempted to bind Gunicorn to `0.0.0.0` after we had already agreed to hide it behind Nginx on `127.0.0.1`.
