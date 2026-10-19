"""Relational persistence. No model/provider text is written to developer logs."""
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from sqlalchemy import create_engine, String, Text, Integer, JSON, select, delete, update, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session
from sqlalchemy.exc import IntegrityError

def now():
    return datetime.now(timezone.utc).isoformat()

class Base(DeclarativeBase):
    pass

class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(160), unique=True)
    password: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="user", server_default="user")
    profile: Mapped[dict] = mapped_column(JSON, default=dict)

class Login(Base):
    __tablename__ = "logins"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    expires: Mapped[int] = mapped_column(Integer)

class Consultation(Base):
    __tablename__ = "consultations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    title: Mapped[str] = mapped_column(String(80))
    updated_at: Mapped[str] = mapped_column(String(40))
    revision: Mapped[int] = mapped_column(Integer, default=1)

class Report(Base):
    __tablename__ = "reports"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer, default=1)

def password_hash(password):
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000)
    return salt + ":" + key.hex()

def password_matches(password, stored):
    salt, expected = stored.split(":")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return hmac.compare_digest(actual, expected)

class Store:
    def __init__(self, url):
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {}, pool_pre_ping=True)
        Base.metadata.create_all(self.engine)
        # Additive migration: existing accounts keep their data and default to user.
        if "role" not in {column["name"] for column in inspect(self.engine).get_columns("accounts")}:
            with self.engine.begin() as connection:
                connection.execute(text("ALTER TABLE accounts ADD COLUMN role VARCHAR(16) NOT NULL DEFAULT 'user'"))
        # Comparable password work even for an unknown login account.
        self.dummy_password = password_hash(secrets.token_hex(16))

    @staticmethod
    def public_account(account):
        return {"id": account.id, "email": account.email, "username": account.email, "role": account.role}

    def register(self, email, password, *, role="user"):
        if role not in {"user", "admin"}:
            raise ValueError("Unknown account role")
        account = Account(id=str(uuid4()), email=email, password=password_hash(password), profile={}, role=role)
        try:
            with Session(self.engine) as db:
                db.add(account); db.commit()
                return self.public_account(account)
        except IntegrityError:
            return None

    def login(self, email, password):
        with Session(self.engine) as db:
            account = db.scalar(select(Account).where(Account.email == email))
            valid = password_matches(password, account.password if account else self.dummy_password)
            if not account or not valid:
                return None
            token = secrets.token_urlsafe(40)
            db.execute(delete(Login).where(Login.expires < int(time.time())))
            db.add(Login(token_hash=hashlib.sha256(token.encode()).hexdigest(), owner=account.id, expires=int(time.time())+86400*7))
            db.commit()
            return token, self.public_account(account)

    def user(self, token):
        if not token:
            return None
        with Session(self.engine) as db:
            login = db.get(Login, hashlib.sha256(token.encode()).hexdigest())
            if not login or login.expires < time.time():
                return None
            account = db.get(Account, login.owner)
            return self.public_account(account) if account else None

    def logout(self, token):
        with Session(self.engine) as db:
            db.execute(delete(Login).where(Login.token_hash == hashlib.sha256(token.encode()).hexdigest())); db.commit()

    def profile(self, owner):
        with Session(self.engine) as db:
            return db.get(Account, owner).profile

    def set_profile(self, owner, values, source):
        with Session(self.engine) as db:
            account = db.get(Account, owner)
            profile = dict(account.profile)
            for field, value in values.items():
                if value.strip():
                    profile[field] = {"value": value.strip(), "source": source, "confirmed_at": now()}
                else:
                    profile.pop(field, None)
            account.profile = profile; db.commit()
            return profile

    def list_sessions(self, owner):
        with Session(self.engine) as db:
            rows = db.scalars(select(Consultation).where(Consultation.owner == owner).order_by(Consultation.updated_at.desc()).limit(100))
            return [{"id": r.id, "title": r.title, "updated_at": r.updated_at} for r in rows]

    def session(self, id, owner):
        with Session(self.engine) as db:
            row = db.scalar(select(Consultation).where(Consultation.id == id, Consultation.owner == owner))
            return (row.payload, row.revision) if row else None

    def create_session(self, payload, owner):
        with Session(self.engine) as db:
            db.add(Consultation(id=payload["id"], owner=owner, payload=payload, title="新的咨询", updated_at=now(), revision=1)); db.commit()

    def save_session(self, payload, owner, revision):
        title = next((m["content"][:35] for m in payload["messages"] if m["role"] == "user"), "新的咨询")
        with Session(self.engine) as db:
            saved = db.execute(update(Consultation).where(Consultation.id == payload["id"], Consultation.owner == owner, Consultation.revision == revision).values(payload=payload, title=title, updated_at=now(), revision=revision+1)).rowcount
            db.commit()
            return bool(saved)

    def delete_session(self, id, owner):
        with Session(self.engine) as db:
            db.execute(delete(Report).where(Report.session_id == id, Report.owner == owner))
            db.execute(delete(Consultation).where(Consultation.id == id, Consultation.owner == owner)); db.commit()

    def put_report(self, payload, owner):
        with Session(self.engine) as db:
            db.add(Report(id=payload["id"], owner=owner, session_id=payload["session_id"], payload=payload, revision=1)); db.commit()

    def report(self, id, owner):
        with Session(self.engine) as db:
            row = db.scalar(select(Report).where(Report.id == id, Report.owner == owner))
            return row.payload if row else None

    def reports(self, session_id, owner):
        with Session(self.engine) as db:
            return [row.payload for row in db.scalars(select(Report).where(Report.session_id == session_id, Report.owner == owner))]

    def confirm_report(self, payload, owner, revision):
        with Session(self.engine) as db:
            changed = db.execute(update(Report).where(Report.id == payload["id"], Report.owner == owner, Report.revision == revision).values(payload=payload, revision=revision+1)).rowcount
            db.commit()
            return bool(changed)

    def delete_report(self, id, owner):
        with Session(self.engine) as db:
            db.execute(delete(Report).where(Report.id == id, Report.owner == owner)); db.commit()
