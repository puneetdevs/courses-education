FROM node:20-alpine AS builder
WORKDIR /app
RUN --mount=type=cache,target=/app/node_modules \
    --mount=type=bind,source=package.json,target=/app/package.json \
    npm install
COPY . .
RUN npm run build

FROM node:20-alpine AS final
WORKDIR /app
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
CMD [ "node", "server.js" ]