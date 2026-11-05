"""Generate local configuration without copying secrets from another project."""
from pathlib import Path
import os
import secrets

ROOT = Path(__file__).resolve().parents[1]
env_path = ROOT / '.env'
infra_path = ROOT / '.local/infra.env'


def main():
    if env_path.exists() or infra_path.exists():
        raise SystemExit('Existing local configuration found; no files were overwritten.')
    password = secrets.token_hex(24)
    storage_password = secrets.token_hex(24)
    (ROOT / '.local/logs').mkdir(parents=True, exist_ok=True)
    (ROOT / '.local/state').mkdir(parents=True, exist_ok=True)
    env_text = (ROOT / '.env.example').read_text(encoding='utf-8')
    env_text = env_text.replace('POSTGRES_DSN=', f'POSTGRES_DSN=postgresql://zhiyan:{password}@127.0.0.1:5432/zhiyan')
    with env_path.open('x', encoding='utf-8') as handle:
        handle.write(env_text)
    with infra_path.open('x', encoding='utf-8') as handle:
        handle.write(f'POSTGRES_USER=zhiyan\nPOSTGRES_PASSWORD={password}\nPOSTGRES_DB=zhiyan\n'
                     f'MINIO_ROOT_USER=zhiyan-storage\nMINIO_ROOT_PASSWORD={storage_password}\n')
    if os.name != 'nt':
        env_path.chmod(0o600)
        infra_path.chmod(0o600)
    print('Local configuration created. Add your own provider keys to .env.')


if __name__ == '__main__':
    main()
