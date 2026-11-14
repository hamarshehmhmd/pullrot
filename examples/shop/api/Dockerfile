ARG NODE_VERSION=20
FROM node:${NODE_VERSION}-alpine AS build
WORKDIR /app
COPY . .
RUN npm ci && npm run build

FROM openjdk:17-jdk-slim AS reports
COPY reports /reports

FROM gcr.io/distroless/nodejs20-debian12
COPY --from=build /app/dist /app
COPY --from=bitnami/kubectl:1.30.2 /opt/bitnami/kubectl/bin/kubectl /usr/local/bin/kubectl
CMD ["/app/server.js"]
