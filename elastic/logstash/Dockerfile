FROM docker.elastic.co/logstash/logstash:8.13.4
RUN rm -f /usr/share/logstash/pipeline/logstash.conf && bin/logstash-plugin install logstash-input-websocket
