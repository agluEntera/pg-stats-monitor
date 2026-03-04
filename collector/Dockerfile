FROM python:3.12-alpine

RUN apk add --no-cache dcron

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

COPY collect.py .
COPY crontab /etc/crontabs/root

CMD ["crond", "-f", "-l", "2"]
