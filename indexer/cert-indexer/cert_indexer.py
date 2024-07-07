import json
import logging
import os
import time

import pika
import pika.channel
import pika.spec
from dotenv import load_dotenv
from elasticsearch import Elasticsearch, helpers
from publicsuffixlist import PublicSuffixList

logging.basicConfig()
logger = logging.getLogger("cert-indexer")
logger.setLevel(logging.INFO)


class CertificateBatchIndexer:
    _elastic: Elasticsearch
    _index_name: str
    _psl: PublicSuffixList
    _cache: list[dict]

    def __init__(self, elastic: Elasticsearch, index_name: str) -> None:
        self._elastic = elastic
        self._index_name = index_name

        self._psl = PublicSuffixList()
        self._cache = list()

    def __filter_extensions(
        self,
        extensions: dict,
        allowed_extensions: list[str] = [
            "certificatePolicies",
            "extendedKeyUsage",
            "keyUsage",
        ],
    ) -> dict:
        filtered_extensions = dict()

        for ext in allowed_extensions:
            if ext in extensions:
                filtered_extensions.setdefault(ext, extensions[ext])

        return filtered_extensions

    def process_cert(self, message: dict) -> dict:
        leaf_cert = message["data"]["leaf_cert"]
        chain = message["data"]["chain"]

        public_suffixes = list(
            set([self._psl.publicsuffix(domain) for domain in leaf_cert["all_domains"]])
        )

        id = f"{message['data']['cert_index']}-{leaf_cert['sha1'].lower().replace(':', '')}"

        cert_doc = {
            # metadata
            "update_type": message["data"]["update_type"],
            "cert_index": message["data"]["cert_index"],
            "cert_link": message["data"]["cert_link"],
            "seen": int(message["data"]["seen"] * 1000),
            "ctlog_source_name": message["data"]["source"]["name"],
            # leaf cert
            "serial_number": leaf_cert["serial_number"],
            "fingerprint": leaf_cert["fingerprint"],
            "signature_algorithm": leaf_cert["signature_algorithm"],
            "not_after": leaf_cert["not_after"],
            "not_before": leaf_cert["not_before"],
            "lifetime": leaf_cert["not_after"] - leaf_cert["not_before"],
            "encoded_size": len(leaf_cert["as_der"]),
            "root_ca_name": leaf_cert["issuer"]["aggregated"],
            "subject": leaf_cert["subject"],
            "all_domains": leaf_cert["all_domains"],
            "all_public_suffixes": public_suffixes,
            "extensions": self.__filter_extensions(leaf_cert["extensions"]),
            "chain_length": len(chain),
            "chain": [],
        }

        for cert in chain:
            cert_doc["chain"].append(
                {
                    "serial_number": cert["serial_number"],
                    "fingerprint": cert["fingerprint"],
                    "signature_algorithm": cert["signature_algorithm"],
                    "not_after": cert["not_after"],
                    "not_before": cert["not_before"],
                    "extensions": self.__filter_extensions(cert["extensions"]),
                    "issuer": cert["issuer"],
                    "subject": cert["subject"],
                }
            )

        return {
            "_index": self._index_name,
            "_id": id,
            "_source": cert_doc,
        }

    def cert_batch_callback(self, message: bytes) -> None:
        try:
            certs = [self.process_cert(cert) for cert in json.loads(message)]

            for status_ok, response in helpers.streaming_bulk(self._elastic, certs):
                if not status_ok:
                    logger.error(response)
        except Exception as e:
            logger.error(f"Failed to process certificate batch: {e}")


def create_index(elastic: Elasticsearch, index_name: str) -> None:
    if elastic.indices.exists(index=index_name):
        logger.info(f"Index {index_name} already exists")
        return

    logger.info(f"Index {index_name} does not exist, will be created... ")

    with open("index_config.json") as index_config_file:
        index_config = json.load(index_config_file)
        settings = index_config["settings"]
        mappings = index_config["mappings"]

        elastic.indices.create(index=index_name, settings=settings, mappings=mappings)
        logger.info(f"... {index_name} was created successfully")


def main():
    load_dotenv()

    elastic_url = os.getenv("ELASTIC_URL")
    elastic_api_key = os.getenv("ELASTIC_API_KEY")
    elastic_ca_file = os.getenv("ELASTIC_CA_FILE")
    ctlog_index_name = os.getenv("CTLOG_INDEX_NAME")

    elastic = Elasticsearch(
        elastic_url, api_key=elastic_api_key, ca_certs=elastic_ca_file
    )
    ci = elastic.info()
    logger.info(
        f"Connected to {ci['name']} ({ci['cluster_name']}) at {elastic_url} running elasticsearch v{ci['version']['number']}"
    )
    create_index(elastic, ctlog_index_name)

    rabbitmq_host = os.getenv("RABBITMQ_HOST")
    rabbitmq_user = os.getenv("RABBITMQ_USER")
    rabbitmq_pass = os.getenv("RABBITMQ_PASSWORD")
    rabbitmq_queue = os.getenv("RABBITMQ_QUEUE_NAME")

    connection = pika.BlockingConnection(
        pika.ConnectionParameters(
            rabbitmq_host,
            credentials=pika.PlainCredentials(rabbitmq_user, rabbitmq_pass),
        )
    )
    channel = connection.channel()
    channel.queue_declare(queue=rabbitmq_queue, durable=True)

    certificate_indexer = CertificateBatchIndexer(elastic, ctlog_index_name)

    def callback(
        ch: pika.channel.Channel,
        method: pika.spec.Basic.Deliver,
        properties: pika.spec.BasicProperties,
        body: bytes,
    ):
        certificate_indexer.cert_batch_callback(body)
        ch.basic_ack(delivery_tag=method.delivery_tag)

    channel.basic_consume(queue=rabbitmq_queue, on_message_callback=callback)

    logger.info(f"Consume queue {rabbitmq_queue} from {rabbitmq_host}")
    channel.start_consuming()


if __name__ == "__main__":
    main()
