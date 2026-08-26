FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

ENV PORT=8066
EXPOSE 8066

CMD ["python", "admin.py"]
