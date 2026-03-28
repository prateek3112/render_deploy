FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/

# Render uses port 10000 by default, but reads $PORT env var
EXPOSE 10000

CMD ["python", "app.py"]
