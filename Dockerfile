FROM mysterysd/wzmlx:v3

WORKDIR /app

# Install python dependencies
COPY requirements.txt .

# FIX: Added --break-system-packages to bypass PEP 668
RUN pip3 install --no-cache-dir -r requirements.txt --break-system-packages

# Copy the bot code
COPY app.py .

# Set permissions
RUN chmod +x app.py

# Start the application
CMD ["python3", "app.py"]
