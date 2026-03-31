import multiprocessing

# Uvicorn worker — required for ASGI (FastAPI)
worker_class = "uvicorn.workers.UvicornWorker"

# 2×CPU + 1 is the standard formula for async workers
workers = multiprocessing.cpu_count() * 2 + 1

# Bind to all interfaces; put a reverse proxy (nginx) in front in production
bind = "0.0.0.0:8000"

# Kill and restart a worker if it hangs for this many seconds
timeout = 30

# Keep idle connections alive for this many seconds (for nginx upstream)
keepalive = 5

# Restart workers after this many requests to avoid memory growth
max_requests = 1000
max_requests_jitter = 100  # ±100 so all workers don't restart at once

# Logging
accesslog = "-"   # stdout
errorlog  = "-"   # stdout
loglevel  = "info"
