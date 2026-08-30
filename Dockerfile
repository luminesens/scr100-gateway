FROM python:3.12-slim

WORKDIR /srv/scr100-gateway

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8768

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8768"]
