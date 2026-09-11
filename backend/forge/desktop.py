"""Optional desktop trust boundary; all authority originates in the native main process."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import stat
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from .config import settings
from .db import LoginSession, Run, User, emit, session
from .security import token_hash
from .workspace import excluded, import_directory_snapshot


DIRECTORY_TICKET_TTL = 120
MAX_DIRECTORY_TICKETS = 32


def enabled() -> bool:
    return bool(settings.desktop_token)


def instance_fingerprint() -> str:
    """Let the native supervisor verify that this is its newly spawned backend."""
    # The health request carries the renderer token, so a wrong-port listener
    # could hash that header itself. Only our backend knows the control secret.
    return token_hash(settings.desktop_control_token)


def _matches_header(request: Request, name: str, expected: str) -> bool:
    values = request.headers.getlist(name)
    return bool(expected) and len(values) == 1 and secrets.compare_digest(values[0].encode(), expected.encode())


def request_denial(request: Request) -> str | None:
    """Protect every HTTP route, including static files, health and read-only APIs."""
    if not enabled(): return None
    if not _matches_header(request, 'x-forge-desktop', settings.desktop_token):
        return '桌面请求未授权'
    origin = request.headers.get('origin')
    if origin and origin != f'{request.url.scheme}://{request.url.netloc}':
        return '桌面请求来源未授权'
    return None


def require_control(request: Request):
    if not enabled(): raise HTTPException(404, '桌面接口未启用')
    # Browser fetches cannot gain OS directory/session authority. The independent
    # control secret is never injected into or returned to the renderer.
    if (request.headers.get('origin') is not None or
            not _matches_header(request, 'x-forge-desktop', settings.desktop_token) or
            not _matches_header(request, 'x-forge-desktop-control', settings.desktop_control_token)):
        raise HTTPException(403, '需要原生桌面控制权限')


@dataclass(frozen=True)
class DirectoryGrant:
    path: Path
    identity: tuple[int, int]
    expires: float


class DirectoryTickets:
    """Process-local, bounded capabilities, atomically consumed before any IO."""
    def __init__(self):
        self._grants: dict[str, DirectoryGrant] = {}
        self._lock = threading.Lock()

    def issue(self, raw_path: str) -> dict:
        source = Path(raw_path)
        if not source.is_absolute() or '..' in source.parts or '\x00' in raw_path or '\\' in raw_path:
            raise ValueError('必须选择绝对目录路径')
        try:
            selected = source.lstat()
            if not stat.S_ISDIR(selected.st_mode): raise ValueError('不允许导入符号链接或非目录路径')
            source = source.resolve(strict=True)
            info = source.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError('所选目录不存在或无法访问') from exc
        if (info.st_dev, info.st_ino) != (selected.st_dev, selected.st_ino):
            raise ValueError('所选目录已变化，请重新选择目录')
        if not stat.S_ISDIR(info.st_mode) or excluded(source):
            raise ValueError('所选路径不是可导入目录')
        # A directory picker grants a project, never the entire account or the
        # application's database/key store. Resolve both sides before comparing
        # so macOS aliases such as /var cannot bypass these boundaries.
        account_home = Path.home().resolve()
        platform_data = settings.data_root.resolve()
        imports = settings.import_root.resolve()
        if (source == Path(source.anchor) or account_home.is_relative_to(source) or
                source.is_relative_to(platform_data) or platform_data.is_relative_to(source) or
                imports.is_relative_to(source)):
            raise ValueError('请选择具体项目目录，不能导入主目录、平台数据或它们的父目录')
        token = secrets.token_urlsafe(32)
        moment = time.monotonic()
        with self._lock:
            self._grants = {key: grant for key, grant in self._grants.items() if grant.expires > moment}
            if len(self._grants) >= MAX_DIRECTORY_TICKETS:
                raise HTTPException(429, '待导入目录过多，请稍后重试')
            self._grants[token_hash(token)] = DirectoryGrant(source, (info.st_dev, info.st_ino), moment + DIRECTORY_TICKET_TTL)
        return {'ticket': token, 'path': str(source), 'name': source.name or str(source)}

    def consume(self, token: str) -> DirectoryGrant:
        with self._lock:
            grant = self._grants.pop(token_hash(token), None)
        if grant is None or grant.expires <= time.monotonic():
            raise ValueError('目录授权已过期或已使用，请重新选择目录')
        return grant


directory_tickets = DirectoryTickets()


def import_authorized_directory(token: str, project_id: str) -> tuple[str, str]:
    if not enabled(): raise ValueError('仅桌面模式支持目录授权导入')
    grant = directory_tickets.consume(token)
    base = import_directory_snapshot(grant.path, project_id, expected_identity=grant.identity)
    return base, str(grant.path)


class AuthorizeDirectory(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(min_length=1, max_length=4096)


def register_routes(app, terminal_states):
    router = APIRouter(prefix='/api/desktop', dependencies=[Depends(require_control)])

    @router.post('/session')
    def desktop_session(response: Response):
        with session() as s:
            user = s.scalar(select(User).where(User.enabled == True, User.role == 'admin').order_by(User.username, User.id).limit(1))
            if user is None: raise HTTPException(403, '没有可用的桌面管理员账号')
            token = secrets.token_urlsafe(40)
            expires = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
            s.add(LoginSession(token_hash=token_hash(token), user_id=user.id, expires_at=expires))
            emit(s, None, 'auth.desktop_login', {'actor': user.id})
            response.set_cookie('forge_session', token, httponly=True, samesite='strict', secure=settings.secure_cookie, max_age=43200, path='/')
            return {'id': user.id, 'username': user.username, 'role': user.role, 'enabled': user.enabled}

    @router.post('/authorize-directory')
    def authorize_directory(data: AuthorizeDirectory):
        return directory_tickets.issue(data.path)

    @router.get('/status')
    def desktop_status(request: Request):
        with session() as s:
            counts = dict(s.execute(select(Run.status, func.count()).where(Run.status.not_in(terminal_states)).group_by(Run.status)).all())
        return {'active_runs': sum(counts.values()), 'nonterminal_runs': counts,
                'dispatcher': getattr(request.app.state, 'dispatch_status', 'unknown'), 'version': request.app.version}

    app.include_router(router)
