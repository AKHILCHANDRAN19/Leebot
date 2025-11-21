FROM mysterysd/wzmlx:v3

WORKDIR /app

# Install python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy the bot code
COPY app.py .

# Set permissions (just in case)
RUN chmod +x app.py

# Start the application
CMD ["python3", "app.py"]
