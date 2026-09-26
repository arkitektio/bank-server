FROM python:3.12-slim
RUN pip install --no-cache-dir "aiohttp>=3.9,<4" "pyjwt[crypto]>=2.8,<3"
COPY app.py /app/app.py
EXPOSE 8000
CMD ["python", "/app/app.py"]
