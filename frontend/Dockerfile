# Build, then run the production server. NEXT_PUBLIC_* values are baked into the bundle at BUILD time.
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
COPY --from=build /app/package.json /app/package-lock.json ./
RUN npm ci --omit=dev
# owned by node, or "next start" cannot write its cache under .next/cache
COPY --from=build --chown=node:node /app/.next ./.next
USER node
EXPOSE 3000
# healthy only when Next really answers with a non-5xx page; node 22 has fetch built in, so no curl/wget added
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3   CMD ["node", "-e", "fetch('http://127.0.0.1:3000/',{signal:AbortSignal.timeout(3000)}).then(r=>process.exit(r.status<500?0:1),()=>process.exit(1))"]
CMD ["npx", "next", "start", "-p", "3000"]
