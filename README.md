# RoboticGen Hobby Server Monitor (HSM)

HSM is a lightweight, secure dashboard for managing LXD containers on small homelab servers. It is designed to run efficiently on low-resource hardware (like WSL2 or a Raspberry Pi) without the heavy overhead of enterprise solutions.

## Architecture

HSM is split into two main components that communicate via a local file cache and a SQLite time-series database.

1. **Backend API (`backend/hsm/`)**:
   - Built with **Falcon**, an extremely fast, bare-metal Python web framework.
   - Served via **Gunicorn** (1 worker) to ensure safe, serialized writes to SQLite and TinyFlux.
   - Exposes RESTful endpoints for container management, metrics, and user administration.
   - Serves the static compiled frontend files.

2. **Metric Collector Daemon (`backend/hsm/collector/`)**:
   - A standalone background process that polls LXD every 10 seconds.
   - Writes live metrics to a `live_cache.json` file for fast, lock-free reads by the API.
   - Persists historical data to a **SQLite-backed TSDB**.
   - Runs a nightly compaction job to roll up 10-second data into hourly averages, preventing unbounded disk growth.

3. **Frontend (`frontend/`)**:
   - Built with **Astro** in pure static mode. No Node.js server required in production!
   - Uses **Vanilla CSS** and native browser APIs (no React/Vue overhead).
   - Features **uPlot** for blazing fast, 45KB interactive charts.
   - Implements a stunning Dark Theme with modern typography (Inter/JetBrains Mono).

## Security

Security is implemented with a defense-in-depth approach:
- **Default Deny**: `AuthMiddleware` blocks all requests by default. Endpoints must be explicitly allowlisted.
- **OAuth 2.0 Only**: No passwords stored. Authentication is delegated to Google OAuth.
- **Stateless Sessions**: Uses signed JWTs stored in `HttpOnly`, `SameSite=Lax` cookies.
- **Input Sanitization**: All container config parameters (CPU, RAM, Disk) are strictly validated and clamped to the host's physical limits and the user's assigned quota.
- **Immutable Audit Log**: Every configuration change or destructive action (create, delete, exec) is permanently logged to SQLite.
- **LXD Isolation**: The API never runs as root. It communicates via the LXD Unix socket, which is secured by Unix group permissions (`lxd` group).

## Installation (Production)

### 1. Prerequisites
- Linux OS (Ubuntu recommended)
- Python 3.10+
- LXD installed and initialized (`sudo snap install lxd && sudo lxd init --auto`)
- Nginx (optional, for reverse proxying and HTTPS)

### 2. Setup the Environment
```bash
sudo mkdir -p /opt/hsm /etc/hsm
sudo chown -R $USER:$USER /opt/hsm /etc/hsm

# Clone the repository
git clone https://github.com/RoboticGen/hobby-server-monitor-YasasBanuka.git /opt/hsm

# Create the backend virtual environment
cd /opt/hsm/backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Build the Frontend
```bash
cd /opt/hsm/frontend
npm install
npm run build
```

### 4. Configure Secrets
Copy the example config and fill in your Google OAuth credentials and a secure JWT secret:
```bash
sudo cp /opt/hsm/backend/.env.example /etc/hsm/hsm.env
sudo nano /etc/hsm/hsm.env
```

### 5. Initialize the Database
The databases (`data/app.db` and `data/metrics.db`) will be automatically initialized when the backend starts for the first time. No manual script required.

### 6. Install Systemd Services
We run the API and Collector as separate systemd units for maximum reliability.
First, ensure you have a dedicated user in the `lxd` group:
```bash
sudo useradd -r -s /bin/false hsm
sudo usermod -aG lxd hsm
sudo chown -R hsm:hsm /opt/hsm/backend/data
```

Install the units:
```bash
sudo cp /opt/hsm/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hsm-api hsm-collector
```

The dashboard will now be available at `http://<your-server-ip>:8000/`.

## API Documentation

- `GET /healthz`: System health check.
- `GET /api/containers`: List containers available to the current user.
- `POST /api/containers`: Create a new container (Admin only).
- `GET /api/containers/{name}`: Get container details.
- `PATCH /api/containers/{name}`: Start, stop, or restart a container.
- `POST /api/containers/{name}/exec`: Run a command in the container.
- `GET /api/metrics/live`: Get live CPU/RAM/Net stats for all accessible containers.
- `GET /api/metrics/{name}/history`: Get time-series historical data.
- `GET /api/users`: List users (Admin only).
- `POST /api/users`: Invite a new user (Admin only).
- `GET /api/audit`: View the immutable audit log (Admin only).
