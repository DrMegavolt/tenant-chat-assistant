# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

ARG NGINX_IMAGE="nginx:1.29.8-alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de"

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

# Two roots, not one root with denied paths: the public listener cannot serve
# admin.html or admin.js because neither file exists inside /srv/public.
COPY frontend/public/ /srv/admin/
COPY frontend/public/index.html frontend/public/app.js frontend/public/embed.js \
     frontend/public/styles.css /srv/public/
COPY frontend/public/widget/ /srv/public/widget/

USER 10001:10001
EXPOSE 8080 8081
ENTRYPOINT ["/usr/local/bin/tenantchat-web"]
CMD ["nginx", "-g", "daemon off;"]
