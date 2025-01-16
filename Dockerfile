# Dockerfile
FROM python:3.11

# Create a working directory
WORKDIR /app

# Copy requirements first (for faster builds if requirements rarely change)
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the rest of the code
COPY . /app

# Use environment variables from .env (docker-compose will handle passing them in)
# But do NOT hardcode your token in the Dockerfile!

# Final command to run your bot
CMD ["python", "bot.py"]
