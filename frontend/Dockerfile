FROM node:20-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Same-origin API via ingress (/api → backend-service)
ENV VITE_API_BASE_URL=
RUN npm run build

FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
# Readable by nginx's worker whatever modes the checkout had: a checkout made
# with umask 077 left public/ files (favicon.png, config.js) as 600, and nginx
# answered 403 for them.
RUN chmod -R a+rX /usr/share/nginx/html && chmod a+r /etc/nginx/conf.d/default.conf

EXPOSE 80
