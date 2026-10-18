#!/bin/sh
# Container entrypoint.
#   app     apply database migrations, then serve the API, dashboard and media streams
#   worker  campaigns, outbox delivery, connector sync, data retention
#   *       any other voiceagent command, e.g. `create-admin` or `reset-password`
set -e

case "$1" in
  app)
    voiceagent db-upgrade
    exec voiceagent serve --host 0.0.0.0 --port "${PORT:-8080}"
    ;;
  worker)
    exec voiceagent worker
    ;;
  *)
    exec voiceagent "$@"
    ;;
esac
