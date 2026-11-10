FROM python:3.12-slim

WORKDIR /app

# The bot uses Python standard library only (zero external pip dependencies)
COPY smc_bot/ /app/smc_bot/

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "-m", "smc_bot"]
CMD ["autotrade", "--broker", "paper", "--feed", "yahoo", "--timeframes", "M1", "M5", "--engine", "hierarchical", "--interval", "15"]

