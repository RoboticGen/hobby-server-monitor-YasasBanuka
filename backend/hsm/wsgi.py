"""
WSGI entrypoint for gunicorn.

Usage:
  gunicorn hsm.wsgi:app --bind 0.0.0.0:8000 -w 1

Why -w 1 (single worker)?
  SQLite is not safe for concurrent writers. With a single gunicorn worker,
  all writes are serialized within one process. For a single-server admin tool
  with a small number of users, this is sufficient.

  If throughput becomes a bottleneck, the next step would be to enable SQLite
  WAL mode (already done in db.py) and upgrade to multiple workers, since WAL
  allows concurrent reads + one writer.
"""
from dotenv import load_dotenv

# Load .env file before anything else.
# In production, environment is set via systemd EnvironmentFile instead.
load_dotenv()

from hsm.app import create_app  # noqa: E402

app = create_app()
