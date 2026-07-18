"""ASGI entrypoint: ``uvicorn app.asgi:app --no-access-log``.

The access log must stay disabled in production because uvicorn logs full
request lines including query strings (callback signatures).
"""

from app.main import create_app

app = create_app()
