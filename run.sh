#!/bin/bash
set -euo pipefail
echo "=> Waiting for DB to be online"
python manage.py wait_for_database -s 6

echo "=> Performing database migrations..."
python manage.py migrate

echo "=> Ensuring Superusers..."
python manage.py ensureadmin


# Start the first process
echo "=> Starting Server"
daphne -b 0.0.0.0 -p 80 --websocket_timeout -1 bank_server.asgi:application 