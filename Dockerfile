FROM python:3.12-slim
WORKDIR /app
COPY bot_detector.py .
CMD ["python3", "-u", "bot_detector.py"]
