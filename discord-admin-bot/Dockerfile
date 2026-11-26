FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY admin_bot ./admin_bot
RUN useradd --create-home bot
USER bot
CMD ["python", "-m", "admin_bot"]
