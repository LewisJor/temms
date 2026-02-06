"""
TEMMS configuration management.
"""

from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field
import yaml


class DatabaseConfig(BaseModel):
    """Database configuration."""
    path: Path = Field(default=Path("/var/lib/temms/temms.db"))


class StorageConfig(BaseModel):
    """Model storage configuration."""
    model_dir: Path = Field(default=Path("/var/lib/temms/models"))
    cache_dir: Path = Field(default=Path("/var/lib/temms/cache"))


class InferenceConfig(BaseModel):
    """Inference server configuration."""
    host: str = Field(default="0.0.0.0")
    http_port: int = Field(default=8080)
    grpc_port: int = Field(default=50051)
    max_batch_size: int = Field(default=1)
    timeout_ms: int = Field(default=5000)


class PolicyConfig(BaseModel):
    """Policy engine configuration."""
    policy_dir: Path = Field(default=Path("/etc/temms/policies"))
    evaluation_interval_s: int = Field(default=5)
    enable_auto_switching: bool = Field(default=True)


class SyncConfig(BaseModel):
    """Sync configuration."""
    enable_cloud_sync: bool = Field(default=False)
    cloud_endpoint: Optional[str] = None
    sync_interval_s: int = Field(default=300)


class Config(BaseModel):
    """Main TEMMS configuration."""
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    inference: InferenceConfig = Field(default_factory=InferenceConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)

    @classmethod
    def load(cls, path: Path) -> "Config":
        """Load configuration from YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)

    def save(self, path: Path) -> None:
        """Save configuration to YAML file."""
        with open(path, "w") as f:
            yaml.dump(self.model_dump(mode="json"), f, default_flow_style=False)

    @classmethod
    def default(cls) -> "Config":
        """Create default configuration."""
        return cls()
