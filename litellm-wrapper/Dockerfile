FROM ghcr.io/berriai/litellm:main-stable
COPY infisical_fetch.py /app/infisical_fetch.py
COPY litellm_entrypoint.sh /app/litellm_entrypoint.sh
RUN chmod +x /app/litellm_entrypoint.sh
ENTRYPOINT ["/app/litellm_entrypoint.sh"]
CMD ["--port", "4000"]
