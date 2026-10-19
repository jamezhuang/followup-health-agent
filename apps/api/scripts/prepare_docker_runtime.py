"""Prepare private Docker configuration without importing the application."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path, PureWindowsPath

from dotenv import dotenv_values
from sqlalchemy.engine import make_url


class ConfigError(Exception):
    """Safe error: messages contain field names, never configuration values."""


PATH_KEY = re.compile(r"(?:^|_)(?:PATH|DIR|ROOT|FILE|DB)$")
WIN_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def container_path(value: str, api_root, key: str) -> str:
    if value.startswith("/app/") or value == "/app":
        return value
    if WIN_ABSOLUTE.match(value) or value.startswith("\\\\"):
        try:
            relative = PureWindowsPath(value).relative_to(PureWindowsPath(str(api_root)))
        except ValueError:
            raise ConfigError(f"{key}: path is outside apps/api; an explicit mount is required") from None
        parts = relative.parts
    elif value.startswith("/"):
        try:
            relative = Path(value).resolve().relative_to(Path(api_root).resolve())
        except ValueError:
            raise ConfigError(f"{key}: absolute path needs an explicit mount") from None
        parts = relative.parts
    else:
        path = PureWindowsPath(value)
        if path.drive or path.root:
            raise ConfigError(f"{key}: ambiguous path")
        parts = tuple(part for part in path.parts if part != ".")
    if ".." in parts:
        raise ConfigError(f"{key}: parent-directory paths require explicit configuration")
    if parts and parts[0] not in {".cache", "data"}:
        raise ConfigError(f"{key}: storage must be under .cache or data for this launcher")
    return "/app" + ("/" + "/".join(parts) if parts else "")


def render_config(values, api_root, database_host: str):
    output = {}
    changed = []
    for key, value in values.items():
        if value is None:
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError("Invalid environment variable name")
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ConfigError(f"{key}: multiline values are not supported by Docker env files")
        converted = value
        if value.startswith(("postgresql://", "postgresql+", "postgres://", "sqlite://", "sqlite+")):
            try:
                url = make_url(value)
            except Exception:
                raise ConfigError(f"{key}: invalid database URL") from None
            if url.drivername.startswith("sqlite"):
                if url.database not in {None, "", ":memory:"}:
                    path = url.database
                    if re.match(r"^/[A-Za-z]:[\\/]", path):
                        path = path[1:]
                    url = url.set(database=container_path(path, api_root, key))
                    converted = url.render_as_string(hide_password=False)
            elif key == "RAG_DATABASE_URL" or (
                url.host in {"localhost", "127.0.0.1", "::1"}
                and url.port == 5433
            ):
                url = url.set(drivername="postgresql+psycopg", host=database_host, port=5432)
                converted = url.render_as_string(hide_password=False)
        elif not key.startswith("CRAWL4AI_") and (
            WIN_ABSOLUTE.match(value) or PATH_KEY.search(key)
        ) and value:
            converted = container_path(value, api_root, key)
        output[key] = converted
        if converted != value:
            changed.append(key)
    if not output.get("RAG_DATABASE_URL"):
        raise ConfigError("RAG_DATABASE_URL is missing")
    try:
        if not make_url(output["RAG_DATABASE_URL"]).drivername.startswith("postgresql"):
            raise ConfigError("RAG_DATABASE_URL must refer to PostgreSQL")
    except ConfigError:
        raise
    except Exception:
        raise ConfigError("RAG_DATABASE_URL is invalid") from None

    output["CRAWL4AI_ENABLED"] = "false"
    origins = [item.strip() for item in output.get("CORS_ORIGINS", "").split(",") if item.strip()]
    if "http://127.0.0.1:3100" not in origins:
        origins.append("http://127.0.0.1:3100")
    output["CORS_ORIGINS"] = ",".join(origins)

    # Docker --env-file does not remove quotes; Python dotenv does.
    docker_text = "".join(f"{key}={value}\n" for key, value in sorted(output.items()))
    dotenv_text = "".join(
        f"{key}={json.dumps(value, ensure_ascii=False)}\n"
        for key, value in sorted(output.items())
    )
    return docker_text, dotenv_text, changed


def atomic_private_write(destination: Path, content: str):
    descriptor, temporary = tempfile.mkstemp(prefix=".runtime-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--db-host", default="ai-consultation-v1-rag-db-1")
    arguments = parser.parse_args()
    root = arguments.api_root.resolve()
    source = root / ".env"
    if not source.is_file():
        raise ConfigError("apps/api/.env does not exist")
    if not (root / "data" / "knowledge.json").is_file():
        raise ConfigError("data/knowledge.json does not exist")
    values = dotenv_values(source, encoding="utf-8-sig")
    docker_text, dotenv_text, changed = render_config(values, root, arguments.db_host)
    destination = root / ".cache" / "docker-runtime"
    destination.mkdir(parents=True, exist_ok=True)
    atomic_private_write(destination / "api.env", docker_text)
    atomic_private_write(destination / "api.dotenv", dotenv_text)
    print("Docker runtime configuration prepared; original .env retained.")
    print("Translated field names:", ", ".join(changed) if changed else "none")
    print("Browser fallback in this core image: disabled")


if __name__ == "__main__":
    try:
        main()
    except ConfigError as error:
        print("Configuration error:", error, file=sys.stderr)
        raise SystemExit(2) from None
