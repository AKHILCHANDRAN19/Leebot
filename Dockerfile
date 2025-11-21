FROM mysterysd/wzmlx:v3

WORKDIR /app

COPY requirements.txt .
# Fix for PEP 668 error in modern Python images
RUN pip3 install --no-cache-dir -r requirements.txt --break-system-packages

COPY app.py .
RUN chmod +x app.py

CMD ["python3", "app.py"]
