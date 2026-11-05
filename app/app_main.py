import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from backend.security import LocalRequestGuard
import uvicorn

from backend.config import AppSettings
from backend.router import health_router, research_router, conversation_router


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logging.getLogger("mult_agents").setLevel(logging.INFO)
logging.getLogger("backend").setLevel(logging.INFO)


def create_app() -> FastAPI:
    settings = AppSettings()
    app = FastAPI(title=settings.app_name)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )
    app.add_middleware(LocalRequestGuard, allowed_origins=settings.cors_origins())
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]'])
    app.include_router(health_router)
    app.include_router(research_router)
    app.include_router(conversation_router)
    return app


app = create_app()


if __name__ == "__main__":
    runtime_settings = AppSettings()
    uvicorn.run(
        "app_main:app",
        host=runtime_settings.host,
        port=runtime_settings.port,
        reload=runtime_settings.app_env == "development",
    )
