from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class AdminCredentials(BaseModel):
    username: str
    password: str

class PanelConfig(BaseModel):
    base_url: str = Field(description='3X-UI base URL, e.g., https://panel.example.com')
    insecure: bool = False

class BotConfig(BaseModel):
    token: str
    admin_numeric_ids: List[int] = Field(default_factory=list)
    per_user_limit: int = 0

class AppConfig(BaseModel):
    panel: PanelConfig
    admin: AdminCredentials
    bot: BotConfig
    subscription_base_url: Optional[str] = ''

