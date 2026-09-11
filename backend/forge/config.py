"""Installation settings. Secrets must be supplied by the operator, never by an agent."""
from dataclasses import dataclass
import os
from pathlib import Path

@dataclass(frozen=True)
class Settings:
    local_execution_enabled: bool = os.getenv('FORGE_LOCAL_EXECUTION', 'false').lower() == 'true'
    orchestrator: str = os.getenv('ORCHESTRATOR', 'local')
    validation_mode: bool = os.getenv('FORGE_VALIDATION_MODE', 'false').lower() == 'true'
    frontend_dir: Path = Path(os.getenv('FRONTEND_DIR', str(Path(__file__).resolve().parents[2] / 'frontend' / 'dist'))).resolve()
    database_url: str = os.getenv('DATABASE_URL', 'sqlite:///./forge.db')
    data_root: Path = Path(os.getenv('DATA_ROOT', './data')).resolve()
    import_root: Path = Path(os.getenv('IMPORT_ROOT', './imports')).resolve()
    encryption_key: str = os.getenv('ENCRYPTION_KEY', '')
    admin_password: str = os.getenv('ADMIN_PASSWORD', '')
    desktop_token: str = os.getenv('FORGE_DESKTOP_TOKEN', '')
    desktop_control_token: str = os.getenv('FORGE_DESKTOP_CONTROL_TOKEN', '')
    temporal_address: str = os.getenv('TEMPORAL_ADDRESS', 'localhost:7233')
    temporal_namespace: str = os.getenv('TEMPORAL_NAMESPACE', 'default')
    task_queue: str = os.getenv('TEMPORAL_TASK_QUEUE', 'agent-forge')
    sandbox_url: str = os.getenv('SANDBOX_URL', 'http://localhost:8091')
    sandbox_token: str = os.getenv('SANDBOX_TOKEN', '')
    secure_cookie: bool = os.getenv('SECURE_COOKIE', 'false').lower() == 'true'
    max_active_runs: int = int(os.getenv('MAX_ACTIVE_RUNS', '2'))
    max_active_activities: int = int(os.getenv('MAX_ACTIVE_ACTIVITIES', '4'))
    max_project_mb: int = int(os.getenv('MAX_PROJECT_MB', '200'))
    origins: tuple[str, ...] = tuple(os.getenv('ALLOWED_ORIGINS', 'http://localhost:8080,http://127.0.0.1:8080,http://localhost:5173').split(','))

settings = Settings()
