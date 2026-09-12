# Multi-stage build: compile the Vite SPA with Node, then serve the static
# output from a tiny Nginx image (no Node runtime in the final image).
FROM node:22-alpine AS builder

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Vite bakes VITE_* variables into the built JS at BUILD time, not at
# container start -- so the backend's public URL must be known here, not
# just set as a runtime env var later. Override with
# `--build-arg VITE_API_BASE_URL=https://api.example.com` once the
# production backend URL is known; this default only works for local
# `docker compose` setups where both containers share a network.
ARG VITE_API_BASE_URL=http://localhost:8001
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

RUN npm run build


FROM nginx:1.27-alpine

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
