"""Workspace-local PostgreSQL setup on Windows; no system service or admin install."""
import argparse
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import tarfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
VERSION = "17.6.0"
ARTIFACT = f"embedded-postgres-binaries-windows-amd64-{VERSION}.jar"
URL = f"https://repo.maven.apache.org/maven2/io/zonky/test/postgres/embedded-postgres-binaries-windows-amd64/{VERSION}/{ARTIFACT}"


def command(*args):
    subprocess.run([str(a) for a in args], check=True,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["setup", "start", "stop"], default="setup", nargs="?")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("Use docker compose on Linux/macOS, or configure an existing local PostgreSQL")
    LOCAL.mkdir(exist_ok=True)
    binaries = LOCAL / "postgres" / "bin"
    data = LOCAL / "pgdata"
    if args.action == "stop":
        command(binaries / "pg_ctl.exe", "-D", data, "-m", "fast", "stop")
        return
    if not binaries.exists():
        archive = LOCAL / "postgres.jar"
        print("Downloading pinned PostgreSQL test binaries from Maven Central...")
        import truststore
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with urllib.request.urlopen(URL, context=context, timeout=60) as response, archive.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        expected = urllib.request.urlopen(URL + ".sha256", context=context, timeout=30).read().decode().strip().split()[0]
        if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise SystemExit("PostgreSQL archive checksum mismatch")
        with zipfile.ZipFile(archive) as bundle:
            member = next(n for n in bundle.namelist() if n.endswith(".txz"))
            target = LOCAL / "postgres.txz"
            target.write_bytes(bundle.read(member))
        with tarfile.open(target) as bundle:
            bundle.extractall(LOCAL / "postgres", filter="data")
    initial = not (data / "PG_VERSION").exists()
    admin_file = LOCAL / "pg_admin_password"
    if initial:
        admin_file.write_text(secrets.token_urlsafe(30), encoding="utf-8")
        command(binaries / "initdb.exe", "-D", data, "-U", "qa_admin", "--pwfile", admin_file,
                "--auth-host=scram-sha-256", "--auth-local=scram-sha-256", "--encoding=UTF8", "--locale=C")
        with (data / "postgresql.conf").open("a", encoding="utf-8") as stream:
            stream.write("\nlisten_addresses = '127.0.0.1'\nport = 55432\n")
    status = subprocess.run([str(binaries / "pg_ctl.exe"), "-D", str(data), "status"], capture_output=True)
    if status.returncode:
        command(binaries / "pg_ctl.exe", "-D", data, "-l", LOCAL / "postgres.log", "-w", "start")
    if args.action == "start":
        return
    env_path = ROOT / ".env"
    if env_path.exists():
        print("PostgreSQL running; existing .env preserved.")
        return
    import psycopg
    from psycopg import sql
    passwords = {name: secrets.token_urlsafe(24) for name in ("qa_app", "qa_state", "qa_verifier")}
    with psycopg.connect(host="127.0.0.1", port=55432, dbname="postgres", user="qa_admin",
                         password=admin_file.read_text().strip(), autocommit=True) as conn:
        for name, password in passwords.items():
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(name), sql.Literal(password)))
        for name, owner in (("qa_app_test", "qa_app"), ("qa_agent_state", "qa_state")):
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(owner)))
            conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name)))
        conn.execute("GRANT CONNECT ON DATABASE qa_app_test TO qa_verifier")
        conn.execute("ALTER ROLE qa_verifier SET default_transaction_read_only = on")
    with psycopg.connect(host="127.0.0.1", port=55432, dbname="qa_app_test", user="qa_admin",
                         password=admin_file.read_text().strip(), autocommit=True) as conn:
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.execute("GRANT USAGE ON SCHEMA public TO qa_verifier")
        conn.execute("ALTER DEFAULT PRIVILEGES FOR ROLE qa_app IN SCHEMA public GRANT SELECT ON TABLES TO qa_verifier")
    content = (ROOT / ".env.example").read_text(encoding="utf-8")
    for role in passwords:
        content = content.replace(f"{role}:CHANGE_ME", f"{role}:{passwords[role]}")
    content = content.replace("TEST_USER_A_PASSWORD=CHANGE_ME", "TEST_USER_A_PASSWORD=" + secrets.token_urlsafe(24))
    content = content.replace("TEST_USER_B_PASSWORD=CHANGE_ME", "TEST_USER_B_PASSWORD=" + secrets.token_urlsafe(24))
    env_path.write_text(content, encoding="utf-8")
    print("Local PostgreSQL ready on 127.0.0.1:55432. Separate databases and read-only verifier created.")
    print("Generated test credentials saved in ignored .env. LLM credentials remain unconfigured.")


if __name__ == "__main__":
    main()
