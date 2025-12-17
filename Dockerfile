FROM python:3.9-alpine

WORKDIR /app

# Install minimal dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# Run the bot
CMD ["python", "arb_bot.py"]
