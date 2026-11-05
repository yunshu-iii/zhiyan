"""Download the fixed upstream MinIO source archive and verify its checksum."""
import hashlib
from pathlib import Path
import urllib.request

COMMIT = '9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a'
URL = 'https://codeload.github.com/minio/minio/tar.gz/' + COMMIT
SHA256 = '45521908307306e925c98d629e1c17d78c8b72b6ee242b1bfb1409f7d8ee5841'


def main():
    target = Path(__file__).resolve().parents[1] / 'infra/minio/minio-source.tar.gz'
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != SHA256:
            raise SystemExit('Existing archive checksum mismatch; refusing overwrite.')
        print('Verified existing source archive.')
        return
    request = urllib.request.Request(URL, headers={'User-Agent': 'zhiyan-source-setup'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != SHA256:
        raise SystemExit('Source archive size or checksum verification failed.')
    with target.open('xb') as handle:
        handle.write(data)
    print('Verified upstream source archive saved; its upstream license remains applicable.')


if __name__ == '__main__':
    main()
