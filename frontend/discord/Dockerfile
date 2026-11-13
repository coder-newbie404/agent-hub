FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY discord_frontend ./discord_frontend
RUN useradd --system --uid 10001 --no-create-home app
USER app
CMD ["python", "-m", "discord_frontend"]
