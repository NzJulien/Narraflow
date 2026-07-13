FROM python:3.11-slim

WORKDIR /srv
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY frontend ../frontend

# Secrets (FIREWORKS_API_KEY, etc.) are injected at run time, e.g.:
#   docker run --env-file backend/.env -p 8000:8000 narraflow
# Never bake API keys into the image.

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Demo UI:      http://localhost:8000/app/
# API status:   http://localhost:8000/
