FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/opt/playwright DEBUG=False
WORKDIR /app
COPY requirements.txt ./
# PDFs use headless Chromium only; avoid bundling the unused full browser.
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /root/.cache
RUN groupadd --gid 1000 ibfs && useradd --uid 1000 --gid ibfs --create-home ibfs
COPY --chown=ibfs:ibfs . .
RUN chmod +x entrypoint.sh && mkdir -p media staticfiles && chown -R ibfs:ibfs media staticfiles
USER ibfs
EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
