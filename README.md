# RoboticGen Hobby Server Monitor (HSM)

HSM is a lightweight, secure dashboard for managing LXD containers on small homelab servers. It is designed to run efficiently on low-resource hardware (like WSL2 or a Raspberry Pi) without the heavy overhead of enterprise solutions.

---

## 1. Setup Guide (From Fresh Ubuntu)

### Prerequisites
- Fresh Ubuntu Machine
- Python 3.10+
- LXD installed and initialized (`sudo snap install lxd && sudo lxd init --auto`)
- Nginx installed (`sudo apt install nginx`)

### Step-by-Step Installation
1. **Prepare Directory & Clone**
   ```bash
   sudo mkdir -p /opt/hsm /etc/hsm
   sudo chown -R $USER:$USER /opt/hsm /etc/hsm
   git clone https://github.com/RoboticGen/hobby-server-monitor-YasasBanuka.git /opt/hsm
   ```

2. **Backend Setup**
   ```bash
   cd /opt/hsm/backend
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Frontend Setup**
   ```bash
   cd /opt/hsm/frontend
   npm ci
   npm run build
   ```

4. **Environment Variables**
   ```bash
   sudo cp /opt/hsm/backend/.env.example /etc/hsm/hsm.env
   sudo nano /etc/hsm/hsm.env
   # Fill in your Google OAuth credentials and JWT secret here.
   ```

5. **Systemd Services (API & Collector)**
   We run the API and Collector as separate systemd units for maximum reliability. The API runs as the `hsm` user in the `lxd` group to interact securely with the unix socket.
   ```bash
   sudo useradd -r -s /bin/false hsm
   sudo usermod -aG lxd hsm
   sudo chown -R hsm:hsm /opt/hsm/backend/data
   
   sudo cp /opt/hsm/systemd/*.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now hsm-api hsm-collector
   ```

6. **Nginx Reverse Proxy**
   Create a new file `/etc/nginx/sites-available/hsm`:
   ```nginx
   server {
       listen 80;
       server_name _;
       
       # Serve static Astro files directly for speed
       location / {
           root /opt/hsm/frontend/dist;
           index index.html;
           try_files $uri $uri/ /index.html;
       }
       
       # Proxy API calls to internal Gunicorn server
       location /api/ {
           proxy_pass http://127.0.0.1:8000;
           proxy_set_header Host $host;
       }
   }
   ```
   Enable it:
   ```bash
   sudo ln -s /etc/nginx/sites-available/hsm /etc/nginx/sites-enabled/
   sudo rm /etc/nginx/sites-enabled/default
   sudo systemctl restart nginx
   ```
   You can now reach the running dashboard via your server's IP in your browser!

---

## 2. Architecture

```mermaid
graph TD
    Client[Web Browser] -->|HTTPS/HTTP| Nginx[Nginx Reverse Proxy]
    Nginx -->|Static Files| Dist[/opt/hsm/frontend/dist]
    Nginx -->|/api/*| Gunicorn[Gunicorn:127.0.0.1:8000]
    Gunicorn -->|WSGI| Falcon[Falcon API]
    
    Falcon --> SQLite[(app.db SQLite)]
    Falcon --> LiveCache[live_cache.json]
    Falcon --> TSDB[(metrics.db TSDB)]
    
    Collector[Collector Daemon] -->|Every 10s| LXD[LXD Unix Socket]
    Collector --> LiveCache
    Collector --> TSDB
```

**How it works:**
The system is fundamentally decoupled. The **Collector Daemon** runs in an infinite loop entirely independent of the web server. It polls the LXD Unix Socket every 10 seconds and writes the instantaneous state to `live_cache.json`, while saving historical data to the `metrics.db` TSDB.
When a user hits the dashboard, **Nginx** serves the static Astro frontend instantly. The frontend makes an XHR call to the **Falcon API** (running on Gunicorn), which simply reads the `live_cache.json` and returns it. This guarantees that API response times are O(1) regardless of LXD API latency or the number of containers running on the host.

---

## 3. Data Model

### SQLite Schema (`app.db`)
- **`users`**: `id`, `email`, `name`, `role` (admin/user), `active`, `quota_ram_mb`, `quota_cpu_cores`, `quota_disk_gb`. Stores OAuth profiles and resource quotas.
- **`containers`**: `name`, `description`, `owner_id`. Supplements LXD data by tracking who owns what.
- **`container_assignments`**: `user_id`, `container_name`. Junction table granting non-admins access to specific containers.
- **`audit_log`**: `actor_email`, `action`, `target`, `detail`. Immutable ledger of destructive actions.
- **`revoked_tokens`**: Tracks invalidated JWT IDs for logout processing.

### TSDB Layout (`metrics.db`)
We abandoned TinyFlux in favor of a SQLite WAL-mode TSDB due to massive CSV I/O thrashing.
- **`container_metrics`**: High-resolution 10-second snapshots. Columns: `time`, `container`, `cpu_usage_pct`, `ram_used_mb`, `disk_used_gb`, `net_rx_bytes`, etc.
- **`container_metrics_hourly`**: Low-resolution rollups. A nightly cron job averages the 10-second data into this table and purges old raw data to bound disk growth infinitely.

---

## 4. API Reference

| Endpoint | Method | Role | Request | Response |
|---|---|---|---|---|
| `/api/auth/login` | GET | None | `?redirect=/` | 302 Redirect to Google OAuth |
| `/api/auth/callback` | GET | None | `?code=xxx&state=yyy` | 302 Redirect + `auth_token` Cookie |
| `/api/users` | GET | Admin | N/A | `{"users": [{"email": "...", "role": "..."}]}` |
| `/api/users` | POST | Admin | `{"email":"x@x.com", "role":"user"}` | 200 OK |
| `/api/containers` | GET | User | N/A | `{"containers": [{"name": "...", "status": "..."}]}` |
| `/api/containers` | POST | Admin | `{"name":"c1", "image":"ubuntu"}` | 201 Created |
| `/api/containers/{name}` | PATCH | User | `{"action": "start|stop"}` | 200 OK |
| `/api/containers/{name}/exec` | POST | User | `{"command": ["ls", "-l"]}` | `{"stdout": "...", "stderr": "..."}` |
| `/api/metrics/live` | GET | User | N/A | `{"containers": {"c1": {"cpu_usage_pct": 2.5}}}` |
| `/api/audit` | GET | Admin | N/A | `{"entries": [{"action": "container.delete", ...}]}` |

---

## 5. Security Notes

**Threat Model Summary:**
Our primary threat vectors are unauthorized container access, API abuse, and CSRF attacks against the admin panel. 
- **CSRF Mitigation:** We use dual cookies. OAuth state validation uses `SameSite=Lax`, while the core session JWT cookie uses `SameSite=Strict`.
- **JWT Signing:** Sessions are stateless and signed using `HS256`. 
- **Path Traversal:** Our static file sink (before Nginx transition) utilized strict `os.path.realpath` containment checks to prevent `../` attacks on the host filesystem.

**The LXD Privilege Decision:**
LXD is managed via a local Unix socket (`/var/snap/lxd/common/lxd/unix.socket`). Anyone with write access to this socket effectively has `root` privileges on the host machine because they can mount the host's `/` filesystem inside a privileged container.
To mitigate this, we do **not** run the web API as `root`. We created a dedicated unprivileged user (`hsm`) and added it to the `lxd` group. While this still carries heavy implications (the `lxd` group is root-equivalent), it completely prevents standard OS-level remote code execution exploits against the Falcon API from immediately compromising the host kernel. We rely on strict RBAC and strict input validation at the API edge to ensure only whitelisted commands and parameters ever reach the LXD socket.

---

## 6. Configuration

All configuration is done via environment variables (`.env`).

| Variable | Description |
|---|---|
| `JWT_SECRET` | Secret key used to symmetrically sign session cookies. |
| `JWT_EXPIRY_SECONDS` | Lifetime of the session cookie (Default: 28800). |
| `BOOTSTRAP_ADMIN_EMAIL` | The first Google email to log in becomes the root Admin. |
| `GOOGLE_CLIENT_ID` | Your Google Cloud OAuth Client ID. |
| `GOOGLE_CLIENT_SECRET` | Your Google Cloud OAuth Client Secret. |
| `GOOGLE_REDIRECT_URI` | Must match Google Console exactly (e.g. `https://domain.com/api/auth/callback`). |
| `RESEND_API_KEY` | Resend API key for sending user email invitations. |
| `RESEND_FROM_EMAIL` | Sender email address for invitations. |
| `LXD_SOCKET_PATH` | (Optional) Path to LXD socket. Auto-detects Snap/Apt paths if empty. |
| `LXD_MODE` | Set to `mock` for local Windows/macOS dev, or `real` for production. |
| `LXD_TIMEOUT` | Timeout in seconds for LXD API calls (Default: 120). |
| `DB_PATH` | Path to the SQLite metadata database (`data/app.db`). |
| `TSDB_PATH` | Path to the SQLite time-series database (`data/metrics.db`). |
| `LIVE_CACHE_PATH` | Path to the inter-process JSON cache (`data/live_cache.json`). |
| `COLLECTOR_INTERVAL_SECONDS` | How often the daemon polls LXD (Default: 10). |
| `METRICS_RAW_DAYS` | How many days to retain 10-second metric data before compaction. |
| `HOST` / `PORT` | Bind interface and port for the local dev server. |
| `COOKIE_SECURE` | Set to `true` in production to enforce HTTPS-only cookies. |
