echo "Reloading services, this might take a while"

cd traefik
docker compose pull && docker compose up -d --remove-orphans

cd ../elastic
docker compose pull && docker compose up -d --remove-orphans --build

cd ../certstream
docker compose pull && docker compose up -d --remove-orphans

cd ../indexer
docker compose pull && docker compose up -d --remove-orphans --build

cd ../metrics
docker compose pull && docker compose up -d --remove-orphans
# make sure the prometheus confguration is reloaded if the container was not restarted/recreated:
echo "Reload prometheus configuration"
docker run --rm --network=metrics curlimages/curl:latest -X POST -si http://prometheus:9090/-/reload

echo "Reloaded all services:"
docker ps --format "table {{.Names}}\t{{.RunningFor}}"