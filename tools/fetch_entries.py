import argparse
import base64

import httpx
from construct import (
    Byte,
    Bytes,
    Embedded,
    Enum,
    GreedyBytes,
    GreedyRange,
    Int16ub,
    Int24ub,
    Int64ub,
    Struct,
    Terminated,
    this,
)
from OpenSSL import crypto

MerkleTreeHeader = Struct(
    "Version" / Byte,
    "MerkleLeafType" / Byte,
    "Timestamp" / Int64ub,
    "LogEntryType" / Enum(Int16ub, X509LogEntryType=0, PrecertLogEntryType=1),
    "Entry" / GreedyBytes,
)

Certificate = Struct("Length" / Int24ub, "CertData" / Bytes(this.Length))

CertificateChain = Struct(
    "ChainLength" / Int24ub,
    "Chain" / GreedyRange(Certificate),
)

PreCertEntry = Struct("LeafCert" / Certificate, Embedded(CertificateChain), Terminated)


def fetch_entries(log_url: str, start: int, end: int) -> list[dict]:
    if end < start:
        raise ValueError(f"End index cannot be smaller than start index!")

    url = f"{log_url}ct/v1/get-entries?start={start}&end={end}"
    print(f"Fetching entries from {url}")

    res = httpx.get(url).raise_for_status().json()

    return res["entries"]


def parse_entry(entry: dict) -> list[crypto.X509]:
    """Parses an entry.
    This function is based on code by Ryan Sears (fitblip), published under MIT license: https://github.com/CaliDog/Axeman
    see also: https://medium.com/cali-dog-security/parsing-certificate-transparency-lists-like-a-boss-981716dc506
    """
    leaf_cert = MerkleTreeHeader.parse(base64.b64decode(entry["leaf_input"]))

    print("Leaf Timestamp: {}".format(leaf_cert.Timestamp))
    print("Entry Type: {}".format(leaf_cert.LogEntryType))

    if leaf_cert.LogEntryType == "X509LogEntryType":
        # We have a normal x509 entry
        cert_data_string = Certificate.parse(leaf_cert.Entry).CertData
        chain = [crypto.load_certificate(crypto.FILETYPE_ASN1, cert_data_string)]

        # Parse the `extra_data` structure for the rest of the chain
        extra_data = CertificateChain.parse(base64.b64decode(entry["extra_data"]))
        for cert in extra_data.Chain:
            chain.append(crypto.load_certificate(crypto.FILETYPE_ASN1, cert.CertData))
    else:
        # We have a precert entry
        extra_data = PreCertEntry.parse(base64.b64decode(entry["extra_data"]))
        chain = [
            crypto.load_certificate(crypto.FILETYPE_ASN1, extra_data.LeafCert.CertData)
        ]

        for cert in extra_data.Chain:
            chain.append(crypto.load_certificate(crypto.FILETYPE_ASN1, cert.CertData))

    return chain


def main() -> None:
    # Example for X509LogEntryType: python3 fetch_entries.py -u https://ct.googleapis.com/logs/us1/argon2024/ -s 1820427081 -e 1820427081
    # Example for PrecertLogEntryType: python3 fetch_entries.py -u https://ct.googleapis.com/logs/eu1/xenon2025h2/ -s 2072379 -e 2072379
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-u", "--url", required=True, help="Base URL of the log with trailing slash"
    )
    parser.add_argument("-s", "--start", required=True, type=int, help="Start index")
    parser.add_argument("-e", "--end", required=True, type=int, help="End index")
    args = parser.parse_args()

    entries = fetch_entries(args.url, args.start, args.end)

    for entry in entries:
        chain = parse_entry(entry)
        print(f"{len(chain)} length of certificate chain")
        for cert in chain:
            print(cert.get_subject())


if __name__ == "__main__":
    main()
