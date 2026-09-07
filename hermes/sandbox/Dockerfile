FROM nikolaik/python-nodejs:python3.11-nodejs20
# Fleet runs on Melbourne time — bake it in so `date`, logs and any local-time code in the sandbox
# are AEST/AEDT (matches the Hermes `timezone: Australia/Melbourne` config that governs cron firing).
ENV TZ=Australia/Melbourne
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-server unzip zip git curl ca-certificates rsync tzdata \
 && ln -sf /usr/share/zoneinfo/Australia/Melbourne /etc/localtime \
 && echo Australia/Melbourne > /etc/timezone \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /var/run/sshd
# HTML->PNG render capability for the producer (template cards). Chromium (Debian pkg) pulls all its own
# system libs at BUILD time, where we are root — so the runtime no-root/no-sudo sandbox user still gets a
# working headless browser. + common fonts so cards render with sane fallbacks when a brand font is absent.
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-dejavu fonts-liberation fonts-noto-core \
 && rm -rf /var/lib/apt/lists/*
# Web Intelligenz brand fonts, installed SYSTEM-WIDE so `font-family:'Jost'` / `'Open Sans'` in a card
# template resolve from disk instead of depending on a fonts.googleapis.com round-trip mid-render. The
# templates still carry their Google Fonts <link> as a fallback, but a render must not be able to
# silently substitute Liberation Sans for Jost and ship a card that only looks subtly wrong.
# Open Sans is Debian-packaged; Jost is not, so it is pinned to a google/fonts commit (variable weight
# axis — fontconfig exposes the named instances, verified with `fc-match Jost:weight=bold`).
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-open-sans \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /usr/local/share/fonts/jost \
 && curl -fsSL -o "/usr/local/share/fonts/jost/Jost[wght].ttf" \
      https://raw.githubusercontent.com/google/fonts/91b26e2e0231f5aa85f5470a7b23d6d732ab15fc/ofl/jost/Jost%5Bwght%5D.ttf \
 && chmod 644 "/usr/local/share/fonts/jost/Jost[wght].ttf" \
 && fc-cache -f \
 && fc-list | grep -q Jost \
 && fc-list | grep -q "Open Sans"
COPY render-card /usr/local/bin/render-card
COPY qa-shot /usr/local/bin/qa-shot
RUN chmod +x /usr/local/bin/render-card /usr/local/bin/qa-shot
# rclone is the sandbox's R2 capability (setup-sandbox.sh step 5 provisions its CONFIG at runtime; the
# BINARY must be baked in). Was previously runtime-installed into the container and lost on every recreate.
RUN apt-get update \
 && apt-get install -y --no-install-recommends rclone \
 && rm -rf /var/lib/apt/lists/*
# Pillow: qa-shot uses it to compress a QA screenshot PNG into a small JPEG that fits ClickUp's base64
# attach cap (the fleet's R2 token can't mint working presigned URLs, so the bytes ride back as base64).
RUN pip install --no-cache-dir --break-system-packages pillow
# key-only, no root, no passwords
RUN printf '\nPasswordAuthentication no\nPermitRootLogin no\nPubkeyAuthentication yes\nAllowUsers sandbox\nX11Forwarding no\nAllowTcpForwarding no\n' >> /etc/ssh/sshd_config
RUN useradd -m -s /bin/bash sandbox && mkdir -p /home/sandbox/.ssh
# The Hermes terminal defaults its working dir to /workspace; create it so `cd /workspace` doesn't fail.
RUN mkdir -p /workspace && chown sandbox:sandbox /workspace
COPY sandbox_key.pub /home/sandbox/.ssh/authorized_keys
RUN chown -R sandbox:sandbox /home/sandbox/.ssh && chmod 700 /home/sandbox/.ssh && chmod 600 /home/sandbox/.ssh/authorized_keys
EXPOSE 22
CMD ["/usr/sbin/sshd","-D","-e"]
