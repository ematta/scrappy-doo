FROM mcr.microsoft.com/playwright@sha256:6e3fb709361521eb27161606e6da6d2674e5e89fa16516cb37d6fdd91372273d
USER root
RUN sed -i 's|azure.ports.ubuntu.com|ports.ubuntu.com|g' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update && apt-get install -y --no-install-recommends libnss3-tools xvfb xauth && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/scrappy /home/scrappy && chown 1000:1000 /home/scrappy \
    && npm install --prefix /opt/scrappy --omit=dev --no-package-lock @playwright/mcp@0.0.85
COPY manifest.json /opt/scrappy/manifest.json
RUN node -e 'const m=require("/opt/scrappy/manifest.json"), p=require("/opt/scrappy/node_modules/@playwright/mcp/package.json"), b=require("/opt/scrappy/node_modules/playwright-core/browsers.json").browsers.find(x=>x.name==="chromium"); if(p.version!==m.playwright_mcp_version || p.dependencies.playwright!==m.playwright_dependency || b.browserVersion!==m.browser_version || b.revision!==m.browser_revision) process.exit(1)'
ENV HOME=/home/scrappy
WORKDIR /home/scrappy
COPY entrypoint.sh /usr/local/bin/scrappy-browser-entrypoint
RUN chmod 755 /usr/local/bin/scrappy-browser-entrypoint
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/scrappy-browser-entrypoint"]
