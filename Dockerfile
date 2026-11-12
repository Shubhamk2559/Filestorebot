FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Health-check endpoint (/health) listens here; Koyeb/Render/Heroku inject PORT automatically.
EXPOSE 8080
CMD ["python", "main.py"]
