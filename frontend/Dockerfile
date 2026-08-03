# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

ARG NODE_IMAGE="node:24.18.1-alpine3.23@sha256:c2cc26d8f991c2db236ad51a61efee843c482372d6d22570787309d511694110"
ARG NGINX_IMAGE="nginx:1.29.8-alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de"

# The bundles are built here rather than on a developer's laptop, so what nginx
# serves comes from package-lock.json and nothing else. `npm ci` fails outright
# if the lockfile disagrees with package.json, the same guarantee `uv sync
# --frozen` gives the Python images.
FROM ${NODE_IMAGE} AS build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM ${NGINX_IMAGE} AS runtime
ARG SOURCE_DATE_EPOCH="0"
ARG VCS_REF="unknown"
LABEL org.opencontainers.image.created="${SOURCE_DATE_EPOCH}" \
      org.opencontainers.image.revision="${VCS_REF}"

# uid 10001 matches every other image in this repository. The rendered site
# configuration is the only file the running container writes, so conf.d is the
# only directory it owns.
RUN addgroup --gid 10001 --system tenantchat \
    && adduser --uid 10001 --ingroup tenantchat --system --no-create-home --disabled-password tenantchat \
    && rm -f /etc/nginx/conf.d/default.conf \
    && chown 10001:10001 /etc/nginx/conf.d

COPY frontend/nginx/nginx.conf /etc/nginx/nginx.conf
COPY frontend/nginx/site.conf.template /etc/nginx/templates/site.conf.template
COPY --chmod=0555 frontend/nginx/entrypoint.sh /usr/local/bin/tenantchat-web

# Two roots, not one root with denied paths: the public root holds the public
# build and nothing else, and the admin root is served only under /admin/ behind
# auth. The two are separate Vite builds precisely so no chunk is shared between
# them — an admin asset cannot end up in the internet-facing root by accident.
COPY --from=build /build/dist/public/ /srv/public/
COPY --from=build /build/dist/admin/ /srv/admin/

USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/tenantchat-web"]
CMD ["nginx", "-g", "daemon off;"]
